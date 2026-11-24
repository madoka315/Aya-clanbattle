"""OneBot v11 反向 WebSocket 服务端。

NapCat 等协议端作为 WS 客户端连进来，本进程是服务端：
- 事件（群/私聊消息）→ ClanBattleService.handle_group → 收集 Outgoing 统一回发；
- API 调用（send_group_msg 等）走 echo 对账的异步 Future；
- 纯文本回复随机插入零宽空格（移植 main.py 的防风控魔改）。
"""
import asyncio
import json
import logging
import random

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

ZWSP = "﻿"


def insert_zwsp(text: str) -> str:
    """每个汉字前以 20% 概率插入零宽空格（CQ 码为 ASCII，不受影响）。"""
    out = []
    for ch in text:
        if "一" <= ch <= "鿿" and random.random() < 0.2:
            out.append(ZWSP)
        out.append(ch)
    return "".join(out)


class OneBotServer:
    def __init__(self, service, *, access_token: str = "",
                 ws_path: str = "/onebot/v11/ws"):
        self.service = service
        service.api = self
        self.token = access_token
        self.ws_path = ws_path
        self._ws: web.WebSocketResponse | None = None
        self._echo_seq = 0
        self._pending: dict = {}

    # ------------------------------------------------------------ WS 服务
    def register(self, app: web.Application) -> None:
        # 主路径之外兼容常见默认路径（如 NapCat 的 /ws/）
        seen = set()
        for p in (self.ws_path, "/ws", "/ws/"):
            if p not in seen:
                app.router.add_get(p, self._ws_handler)
                seen.add(p)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if self.token:
            auth = request.headers.get("Authorization", "")
            query_token = request.query.get("access_token", "")
            if auth != f"Bearer {self.token}" and query_token != self.token:
                return web.Response(status=401, text="invalid token")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("协议端已连接: %s", request.remote)
        old = self._ws
        self._ws = ws
        if old is not None and not old.closed:   # 重连顶掉旧连接
            await old.close()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._on_payload(ws, payload)
        finally:
            if self._ws is ws:
                self._ws = None
                logger.info("协议端已断开")
        return ws

    # ------------------------------------------------------------ 事件
    async def _on_payload(self, ws, payload: dict) -> None:
        if "post_type" not in payload:
            await self._on_api_response(payload)
            return
        post_type = payload.get("post_type")
        if post_type == "meta_event":
            sub = payload.get("meta_event_type")
            if sub == "lifecycle":
                logger.info("协议端 lifecycle: %s", payload.get("sub_type"))
            return
        if post_type == "message":
            # 不能在接收循环里 await 事件处理：API 调用的回执要靠同一个循环读回，
            # 内联处理会死锁（aiocqhttp 同款做法是派发成独立 task）
            asyncio.create_task(self._on_message(payload))
            return
        # notice / request：本阶段不处理

    def _extract_text(self, payload: dict) -> str:
        raw = payload.get("raw_message")
        if raw:
            return raw
        message = payload.get("message")
        if isinstance(message, list):   # NapCat 数组消息兜底
            parts = []
            for seg in message:
                if seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
                else:
                    parts.append(f"[CQ:{seg.get('type')},"
                                 + ",".join(f"{k}={v}" for k, v in seg.get("data", {}).items())
                                 + "]")
            return "".join(parts)
        if isinstance(message, str):
            return message
        return ""

    async def _on_message(self, payload: dict) -> None:
        if payload.get("message_type") not in ("group", "private"):
            return
        sender = payload.get("sender") or {}
        ctx = {
            "group_id": payload.get("group_id"),
            "user_id": payload.get("user_id") or sender.get("user_id"),
            "raw_message": self._extract_text(payload),
            "sender": sender,
        }
        if payload.get("message_type") == "group":
            outs = await self.service.handle_group(ctx)
            for out in outs:
                try:
                    await self.send_group(out.group_id, out.text)
                except Exception:  # noqa: BLE001  发送失败（如被风控）只记日志
                    logger.exception("群消息发送失败 group=%s", out.group_id)
        else:
            reply = await self.service.handle_private(ctx) if hasattr(
                self.service, "handle_private") else None
            if reply:
                try:
                    await self.send_private(ctx["user_id"], reply)
                except Exception:  # noqa: BLE001
                    logger.exception("私聊发送失败 user=%s", ctx["user_id"])

    # ------------------------------------------------------------ API 调用
    async def call(self, action: str, timeout: float = 30, **params) -> dict:
        if self._ws is None or self._ws.closed:
            raise ConnectionError("协议端未连接")
        self._echo_seq += 1
        echo = f"api-{self._echo_seq}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_json({"action": action, "params": params, "echo": echo})
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("retcode") != 0:
            raise RuntimeError(f"API {action} 失败: retcode={resp.get('retcode')} "
                               f"{resp.get('wording') or resp.get('message') or ''}")
        return resp.get("data")

    async def _on_api_response(self, payload: dict) -> None:
        echo = payload.get("echo")
        fut = self._pending.get(str(echo))
        if fut and not fut.done():
            fut.set_result(payload)

    # ------------------------------------------------------------ 发送收口
    async def send_group(self, group_id: int, text: str) -> dict:
        return await self.call("send_group_msg", group_id=group_id,
                               message=insert_zwsp(text))

    async def send_private(self, user_id: int, text: str) -> dict:
        return await self.call("send_private_msg", user_id=user_id,
                               message=insert_zwsp(text))


def make_app(server: OneBotServer) -> web.Application:
    app = web.Application()
    server.register(app)
    return app


async def run(server: OneBotServer, host: str, port: int) -> web.AppRunner:
    """启动并返回 runner（调用方负责 graceful shutdown）。"""
    runner = web.AppRunner(make_app(server))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("OneBot WS 服务已启动 ws://%s:%s%s", host, port, server.ws_path)
    return runner
