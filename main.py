"""Aya-clanbattle 入口：读配置 → 数据层/规则层/服务 → OneBot 反向 WS + 网页面板（同端口）。"""
import asyncio
import json
import logging
import os
import sys
import time

from aiohttp import web

from aya_clanbattle.commands import ClanBattleService
from aya_clanbattle.gameconfig import GameConfig
from aya_clanbattle.onebot import OneBotServer
from aya_clanbattle.store import Store
from aya_clanbattle.webapp import WebApp

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")


async def daily_jobs(service: ClanBattleService, webapp=None):
    """每天 05:05（PCR 换日后）自动同步群名/成员/头像，并拉取当期 boss 数据。"""
    while True:
        lt = time.localtime()
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 5, 5, 0, 0, 0, -1))
        if target <= time.time():
            target += 86400
        await asyncio.sleep(target - time.time())
        try:
            msg = await service.daily_sync()
            logging.getLogger(__name__).info("每日同步: %s", msg)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("每日同步失败")
        if webapp is not None:
            try:
                boss_msg = await webapp.fetch_boss_data()
                logging.getLogger(__name__).info("每日 boss 数据拉取: %s",
                                                 boss_msg.replace("\n", " | "))
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("每日 boss 数据拉取失败")


async def periodic_cleanup(store: Store):
    """每小时清理过期网页会话。"""
    while True:
        try:
            store.cleanup_expired_sessions()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("会话清理失败")
        await asyncio.sleep(3600)


async def _run(config: dict, config_path: str):
    basedir = os.path.dirname(os.path.abspath(__file__))
    store = Store(os.path.join(basedir, config.get("db_path", "aya_clanbattle.db")))
    cfg = GameConfig.load(config)
    from aya_clanbattle import images
    images.configure(font_path=config.get("font_path"),
                     avatar_dir=config.get("avatar_dir"))
    service = ClanBattleService(store, cfg, config)
    service.config_path = config_path
    server = OneBotServer(service, access_token=config.get("access_token", ""),
                          ws_path=config.get("ws_path", "/onebot/v11/ws"))
    webapp = WebApp(service, server, config, config_path)

    app = web.Application()
    server.register(app)      # OneBot 反向 WS 端点
    webapp.register(app)      # 网页 + 面板 API + SSE
    runner = web.AppRunner(app)
    await runner.setup()
    host = config.get("host", "0.0.0.0")
    port = config.get("port", 9223)
    # localhost → 双栈回环（NapCat 连 localhost 可能走 IPv6）
    addrs = ["127.0.0.1", "::1"] if host in ("localhost", "loopback") else [host]
    for addr in addrs:
        site = web.TCPSite(runner, addr, port)
        await site.start()
    print(f"Aya-clanbattle 已启动：http://localhost:{port}/  "
          f"协议端接入 {config.get('ws_path', '/onebot/v11/ws')} 或 /ws/")
    daily = asyncio.create_task(daily_jobs(service, webapp))
    try:
        await periodic_cleanup(store)
    finally:
        daily.cancel()
        await runner.cleanup()
        store.close()


def main():
    basedir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(basedir, "config.json")
    if not os.path.exists(config_path):
        print("缺少 config.json，请复制 config.example.json 后填写")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config = json.load(f)
    try:
        asyncio.run(_run(config, config_path))
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
