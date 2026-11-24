"""网页层：登录/会话、SSE 实时推送、面板 API（与群指令共用 rules 层）、通知位镜像。

- 会话：服务端 SQLite 会话表 + HttpOnly cookie（7 天），CSRF token 校验写操作；
- SSE：每公会一组订阅队列，群聊或网页的任何状态变更都实时推送；
- 通知位镜像：fire-and-forget（用户拍板：不做离线补发，防恢复后轰炸二次风控）；
- 登录：私聊"登录"换 60 秒一次性链接，或"重置密码"拿临时密码走密码登录。
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import secrets

from aiohttp import web

from . import images, rules, stats
from .pcr_time import pcr_datetime
from .rules import RuleError

logger = logging.getLogger(__name__)

COOKIE = "aya_session"
SESSION_DAYS = 7
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# 通知位（沿用 yobot 位分配）
NOTIFY = {"addrecord": 0x01, "undo": 0x02, "apply": 0x04, "cancel": 0x08,
          "tree": 0x08, "add_subscribe": 0x40, "cancel_subscribe": 0x80,
          "modify": 0x100, "save_slot": 0x200}
# 网页批量私聊催刀：相邻两条之间的间隔（秒，区间内随机），防风控
PRIVATE_REMIND_INTERVAL = (3, 10)


def pick_current_battle(infos: list, now) -> dict | None:
    """当期会战条目：优先年月匹配今天（yobot auto_get_boss_data 语义）；
    跨月窗口（satroki 常在新战前几日才录入新档）回退最近一个已开始的条目，
    否则跨月整月都拉不到数据。全部未开始时取最早一条。"""
    for info in infos:
        if info["year"] == now.year and info["month"] == now.month:
            return info
    if not infos:
        return None
    import datetime
    started = [i for i in infos
               if datetime.datetime.fromisoformat(i["startTime"]) <= now]
    pick = max if started else min
    return pick(started or infos,
                key=lambda i: datetime.datetime.fromisoformat(i["startTime"]))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class SSEHub:
    def __init__(self):
        self._subs: dict[int, set] = {}

    def subscribe(self, gid: int) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=16)
        self._subs.setdefault(gid, set()).add(q)
        return q

    def unsubscribe(self, gid: int, q: asyncio.Queue) -> None:
        self._subs.get(gid, set()).discard(q)

    def publish(self, gid: int, event: dict) -> None:
        for q in list(self._subs.get(gid, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class WebApp:
    def __init__(self, service, onebot, setting: dict, config_path: str | None = None):
        self.service = service
        self.onebot = onebot
        self.setting = setting
        self.config_path = config_path
        self.hub = SSEHub()
        with open(os.path.join(os.path.dirname(__file__), "boss_id_name.json"),
                  encoding="utf-8") as f:
            self.boss_id_name = json.load(f)
        self._ensure_cfg_tables()  # 启动即补齐三服配置并绑定运行配置
        service.mutation_hooks.append(self._on_mutation)

    # ------------------------------------------------------------ SSE
    def _on_mutation(self, gid: int, notice: str) -> None:
        try:
            st = self.service.store.get_guild(gid)
            if st is None:
                return
            self.hub.publish(gid, {"bossData": self.boss_data(st),
                                   "base_cycle": st.boss_cycle,
                                   "notice": notice})
        except Exception:  # noqa: BLE001
            logger.exception("SSE publish 失败")

    # ------------------------------------------------------------ 通用
    def _json(self, data, status: int = 200) -> web.Response:
        return web.json_response(data, status=status)

    def _session(self, request):
        return self.service.store.get_session(request.cookies.get(COOKIE, ""))

    def _user(self, request):
        sess = self._session(request)
        if sess is None:
            return None, None
        return sess, self.service.store.user(sess["qqid"])

    def _is_super(self, user_row) -> bool:
        return bool(user_row and (
            user_row["authority_group"] == 1
            or user_row["qqid"] in self.setting.get("super-admin", [])))

    def _page(self, name: str) -> web.FileResponse:
        resp = web.FileResponse(os.path.join(STATIC_DIR, name))
        resp.headers["Cache-Control"] = "no-cache"  # 前端文件随部署即更，杜绝旧缓存
        return resp

    async def _static(self, request):
        rel = request.match_info.get("path", "")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return web.Response(status=404, text="404")
        resp = web.FileResponse(full)
        ext = os.path.splitext(full)[1].lower()
        # 代码类资源 no-cache；图片类资源允许一天缓存
        if ext in (".html", ".js", ".css", ".json"):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "max-age=86400"
        return resp

    def _save_config(self) -> None:
        if self.config_path:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.setting, f, ensure_ascii=False, indent=4)

    def _ensure_cfg_tables(self) -> None:
        """三张 boss 表按服补默认值并让 cfg 与 setting 共享同一对象。
        拉取/预置更新后统一调用：运行配置即时生效，失败服也不会缺键。"""
        from .gameconfig import (DEFAULT_BOSS_HP, DEFAULT_BOSS_ID,
                                 DEFAULT_LEVEL_BY_CYCLE, merge_with_defaults)
        self.setting["boss"] = merge_with_defaults(DEFAULT_BOSS_HP,
                                                   self.setting.get("boss"))
        self.setting["level_by_cycle"] = merge_with_defaults(
            DEFAULT_LEVEL_BY_CYCLE, self.setting.get("level_by_cycle"))
        self.setting["boss_id"] = merge_with_defaults(DEFAULT_BOSS_ID,
                                                      self.setting.get("boss_id"))
        self.service.cfg.boss_hp = self.setting["boss"]
        self.service.cfg.level_by_cycle = self.setting["level_by_cycle"]
        self.service.cfg.boss_id = self.setting["boss_id"]

    # ------------------------------------------------------------ 页面路由
    def register(self, app: web.Application) -> None:
        def route(method: str, path: str, handler):
            # 尾斜杠两种形态都接受（/clan/123 与 /clan/123/ 等价）
            app.router.add_route(method, path, handler)
            if path != "/":
                alt = path.rstrip("/") if path.endswith("/") else path + "/"
                app.router.add_route(method, alt, handler)

        route("GET", "/", self.index)
        route("GET", "/login/", self.login_page)
        route("POST", "/login/api/", self.login_api)
        route("GET", "/logout/", self.logout)
        route("GET", "/api/my_guilds/", self.my_guilds)
        route("GET", "/avatar/{qq}", self.avatar)
        route("GET", "/api/whoami/", self.whoami)
        route("POST", "/admin/api/", self.admin_api)
        route("GET", "/static/{path:.+}", self._static)
        # 顺序重要：先具体路由，再 /clan/{gid}/ 的兜底页面
        route("GET", "/clan/{gid}/statistics/api/", self.statistics_api)
        route("POST", "/clan/{gid}/api/", self.clan_api)
        route("GET", "/clan/{gid}/events", self.clan_events)
        route("GET", "/clan/{gid}/", self.panel_page)
        route("GET", "/clan/{gid}/{tail:.+}", self.panel_page)

    async def index(self, request):
        sess, _ = self._user(request)
        if sess is None:
            raise web.HTTPFound("/login/")
        # 迁移后用户名下可能有多个公会：不再自动跳转，由前端列出公会选择
        return self._page("panel.html")

    async def login_page(self, request):
        qqid = request.query.get("qqid")
        key = request.query.get("key")
        if qqid and key:  # 一次性链接直达
            ok = self.service.store.check_login_code(int(qqid), key)
            if ok:
                sid, _csrf = self.service.store.create_session(int(qqid))
                resp = web.HTTPFound("/")
                resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400,
                                httponly=True, samesite="Lax")
                raise resp
            raise web.HTTPFound("/login/?error=expired")
        return self._page("login.html")

    async def login_api(self, request):
        body = await request.json()
        store = self.service.store
        try:
            qqid = int(body["qqid"])
        except (KeyError, ValueError, TypeError):
            return self._json({"code": 30, "message": "参数错误"})
        if body.get("mode") == "code":
            if not store.check_login_code(qqid, body.get("key", "")):
                return self._json({"code": 20, "message": "登录链接无效或已过期"})
        else:
            if store.is_locked(qqid):
                return self._json({"code": 21, "message": "密码错误次数过多，账号已锁定，请私聊机器人重置密码"})
            if not store.verify_password(qqid, body.get("pwd", "")):
                return self._json({"code": 22, "message": "QQ号或密码不正确"})
        sid, _ = store.create_session(qqid)
        user = store.user(qqid)
        resp = self._json({"code": 0,
                           "must_change_password": bool(user["must_change_password"])})
        resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="Lax")
        return resp

    async def logout(self, request):
        sess = self._session(request)
        if sess:
            self.service.store.drop_session(sess["sid"])
        resp = web.HTTPFound("/login/")
        resp.del_cookie(COOKIE)
        raise resp

    async def avatar(self, request):
        qq = request.match_info["qq"]
        if not qq.isdigit():
            return web.Response(status=404)
        path = os.path.join(images._avatar_dir, f"{qq}.jpg")
        if os.path.isfile(path):
            resp = web.FileResponse(path)
        else:
            resp = web.FileResponse(os.path.join(STATIC_DIR, "default_avatar.png"))
        resp.headers["Cache-Control"] = "max-age=86400"
        return resp

    async def whoami(self, request):
        sess, user = self._user(request)
        if sess is None:
            # 游客态：带 gid 且该群开了 privacy&0x1 时返回只读身份，供面板继续加载
            gid = request.query.get("gid", "")
            st = self.service.store.get_guild(int(gid)) if gid.isdigit() else None
            if st is not None and (st.privacy & 0x1):
                return self._json({"code": 0, "user_id": 0, "nickname": "游客",
                                   "is_super": False, "is_admin": False,
                                   "must_change_password": False, "csrf": None,
                                   "guest": True})
            return self._json({"code": 10, "message": "未登录"})
        return self._json({"code": 0, "user_id": sess["qqid"], "csrf": sess["csrf_token"],
                           "nickname": user["nickname"] if user else str(sess["qqid"]),
                           "is_super": self._is_super(user),
                           "is_admin": bool(user and user["authority_group"] < 100),
                           "must_change_password": bool(user and user["must_change_password"])})

    async def my_guilds(self, request):
        sess, _ = self._user(request)
        if sess is None:
            return self._json({"code": 10, "message": "未登录"})
        rows = self.service.store.db.execute(
            "SELECT m.group_id, g.group_name FROM member m"
            " LEFT JOIN guild g ON g.group_id = m.group_id WHERE m.qqid=?",
            (sess["qqid"],)).fetchall()
        return self._json({"code": 0, "guilds": [
            {"group_id": r["group_id"], "group_name": r["group_name"] or r["group_id"]}
            for r in rows]})

    async def panel_page(self, request):
        sess, _ = self._user(request)
        if sess is None:
            # 游客模式：privacy&0x1 时允许未登录打开只读面板（含 /progress/ 等历史子路径）
            gid = request.match_info.get("gid", "")
            st = self.service.store.get_guild(int(gid)) if gid.isdigit() else None
            if st is None or not (st.privacy & 0x1):
                raise web.HTTPFound(f"/login/?callback={request.path}")
        return self._page("panel.html")

    # ------------------------------------------------------------ SSE
    async def clan_events(self, request):
        gid = int(request.match_info["gid"])
        sess, user = self._user(request)
        st = self.service.store.get_guild(gid)
        if st is None:
            return self._json({"code": 20, "message": "Group not exists"}, 404)
        # 与只读动作同权：成员或主人；其余（含已登录非成员）仅 privacy&0x1 游客态可订阅
        is_member = bool(sess and self.service.store.is_member(gid, sess["qqid"]))
        if not (is_member or self._is_super(user)) and not (st.privacy & 0x1):
            return self._json({"code": 10, "message": "未登录"}, 401)
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = self.hub.subscribe(gid)
        try:
            init = {"bossData": self.boss_data(st), "base_cycle": st.boss_cycle, "notice": None}
            await resp.write(f"data: {json.dumps(init, ensure_ascii=False)}\n\n".encode())
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    await resp.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(gid, q)
        return resp

    # ------------------------------------------------------------ 数据构建
    def boss_data(self, st) -> dict:
        server = st.game_server
        data = {}
        for i, b in enumerate(("1", "2", "3", "4", "5")):
            now, nxt = st.now[b], st.nxt[b]
            is_next = now == 0
            # 已击破且跨阶段/超出两周目窗口：该王本轮不可再挑战，展示为下周目 0 血
            blocked = is_next and not rules.check_next_boss(self.service.cfg, st, b)
            cycle = st.boss_cycle + 1 if is_next else st.boss_cycle
            icon = self.service.cfg.icon_id(server, i)
            data[int(b)] = {
                "num": int(b), "cycle": cycle, "is_next": is_next,
                "blocked": blocked,
                "health": 0 if blocked else (nxt if is_next else now),
                "full_health": 0 if blocked else self.cfg_full(server, cycle, b),
                "icon_id": icon,
                "name": self.boss_id_name.get(b, {}).get(icon, icon),
                "challengers": [{
                    "qqid": c.qqid, "name": self.service._name_of(c.qqid),
                    "is_continue": c.is_continue,
                    "behalf": self.service._name_of(c.behalf) if c.behalf else None,
                    "behalf_qqid": c.behalf,  # 给前端判断"我是否是这个申请的代刀者"
                    "seconds": c.seconds, "damage": c.damage,
                    "comment": c.comment, "tree": c.tree, "tree_msg": c.tree_msg,
                } for c in st.challengers.values() if c.boss_num == b],
                "subscribers": [
                    {"qqid": q, "name": self.service._name_of(q), "note": m}
                    for q, m in st.subscribes.get(b, {}).items()],
            }
        return data

    def cfg_full(self, server, cycle, boss_num) -> int:
        try:
            return self.service.cfg.full_health(server, cycle, boss_num)
        except (KeyError, IndexError):
            return 0

    # ------------------------------------------------------------ 面板 API
    async def clan_api(self, request):
        try:
            gid = int(request.match_info["gid"])
        except ValueError:
            return self._json({"code": 20, "message": "Group not exists"}, 404)
        # 先读请求体、再读状态：请求体是慢速上传，期间群指令/其他网页请求可能已改状态；
        # 先取快照再等 body，会让旧快照在稍后整份保存时回滚并发写入（血量 41M → 42M）
        body = await request.json()
        action = body.get("action", "")
        store = self.service.store
        st = store.get_guild(gid)
        sess, user = self._user(request)
        if st is None:
            # 群未初始化时仅放行 create_guild（网页侧补齐的等价操作）
            if (sess and user and action == "create_guild"
                    and body.get("csrf") == sess["csrf_token"]
                    and (user["authority_group"] < 100 or self._is_super(user))):
                server = body.get("game_server", "cn")
                if server not in ("cn", "jp", "tw"):
                    return self._json({"code": 30, "message": "服务器只能是 cn/jp/tw"})
                async with self.service.group_lock(gid):  # 并发建会只允许一次
                    if store.get_guild(gid) is None:
                        store.create_guild(gid, self.service.cfg, server)
                        return self._json({"code": 0, "message": "公会创建成功"})
                st = store.get_guild(gid)
            if st is None:
                return self._json({"code": 20, "message": "Group not exists"}, 404)

        # 游客与"已登录但非本群成员"同权：仅在 privacy&0x1 时开放 4 个公开只读动作
        guest_actions = ("get_data", "get_member_list", "get_challenge", "get_score_table")
        read_actions = guest_actions + ("get_user_challenge", "get_subscribers",
                                        "get_archives", "get_setting", "get_statistics")
        is_member = store.is_member(gid, sess["qqid"]) if sess else False
        is_admin = bool(sess and user and user["authority_group"] < 100)
        is_super = bool(sess and self._is_super(user))
        if sess is None:
            if not (st.privacy & 0x1) or action not in guest_actions:
                return self._json({"code": 10, "message": "未登录"}, 401)
        else:
            # 读操作免 CSRF（无副作用），写操作必须携带
            if action not in ("get_data", "get_member_list", "get_challenge",
                              "get_user_challenge", "get_subscribers", "get_score_table",
                              "get_archives", "get_setting", "get_statistics"):
                if body.get("csrf") != sess["csrf_token"]:
                    return self._json({"code": 15, "message": "CSRF 校验失败"})
            # 成员或主人才能读取本群数据；其余登录账号按游客处理（旧版成员门禁）
            if (not is_member and not is_super
                    and not (action in guest_actions and (st.privacy & 0x1))):
                msg = ("只有公会成员可以查看" if action in read_actions
                       else "只有公会成员可以操作")
                return self._json({"code": 11, "message": msg}, 403)
            # 设置含 apikey/通知位/隐私位，且会惰性生成 apikey：管理员门禁
            if action == "get_setting" and not ((is_admin and is_member) or is_super):
                return self._json({"code": 11, "message": "需要公会战管理员权限"}, 403)

        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                return self._json({"code": 32, "message": "unknown action"})
            if action in ("get_data", "get_member_list", "get_challenge",
                          "get_user_challenge", "get_subscribers", "get_score_table",
                          "get_archives", "get_setting"):
                return await handler(request, gid, st, sess, user, body)
            if sess is None:
                return self._json({"code": 10, "message": "未登录"}, 401)
            if not is_member and not self._is_super(user):
                return self._json({"code": 11, "message": "只有公会成员可以操作"}, 403)
            if action in ("modify", "send_remind", "drop_member", "add_member",
                          "put_setting", "switch_data_slot", "clear_data_slot",
                          "create_guild"):
                if not (is_admin and is_member) and not self._is_super(user):
                    return self._json({"code": 11, "message": "需要公会战管理员权限"}, 403)
            # 写操作与群指令共用每群锁，且锁内重读状态：任何读-改-写都不与另一写者交错
            async with self.service.group_lock(gid):
                fresh = store.get_guild(gid)
                return await handler(request, gid, fresh or st, sess, user, body)
        except RuleError as e:
            return self._json({"code": 10, "message": str(e)})
        except KeyError as e:
            return self._json({"code": 31, "message": f"缺少参数: {e}"})
        except Exception as e:  # noqa: BLE001
            logger.exception("web api 失败 %s", action)
            return self._json({"code": 40, "message": f"服务器错误: {e}"}, 500)

    # ---- 只读动作
    async def _act_get_data(self, request, gid, st, sess, user, body):
        if not st.group_name and self.onebot is not None:
            try:  # 群名缺失（网页建会）时懒拉取一次
                info = await self.onebot.call("get_group_info", group_id=gid)
                name = (info or {}).get("group_name")
                if name:
                    # 拉取期间状态可能已被群指令改写：锁内重读，只补 group_name，不整份覆盖旧快照
                    async with self.service.group_lock(gid):
                        fresh = self.service.store.get_guild(gid)
                        if fresh is not None and not fresh.group_name:
                            fresh.group_name = name
                            self.service.store.save_guild_state(fresh)
                    st.group_name = (fresh.group_name if fresh is not None else None) or name
            except Exception:  # noqa: BLE001
                pass
        self_data = {"user_id": sess["qqid"] if sess else 0, "nickname": None,
                     "is_member": False, "is_admin": False,
                     "must_change_password": False, "csrf": None}
        if sess:
            u = self.service.store.user(sess["qqid"])
            self_data.update({
                "nickname": u["nickname"] if u else str(sess["qqid"]),
                "is_member": self.service.store.is_member(gid, sess["qqid"]),
                "is_admin": bool(u and u["authority_group"] < 100),
                "is_super": self._is_super(u),
                "must_change_password": bool(u and u["must_change_password"]),
                "csrf": sess["csrf_token"],
            })
        d, _ = pcr_datetime(st.game_server)
        asyncio.ensure_future(images.ensure_avatars(
            [m["qqid"] for m in self.service.store.members(gid)], timeout=8))
        # 刀数口径：收尾/补偿各算 0.5（与记录页、业绩表一致）
        today_blades = 0.0
        today_counts = {}
        for c in self.service.store.day_records(gid, st.battle_id, d):
            w = 0.5 if (c["is_continue"] or c["boss_health_remain"] == 0) else 1.0
            today_blades = round(today_blades + w, 1)
            today_counts[c["qqid"]] = round(today_counts.get(c["qqid"], 0) + w, 1)
        # 前端状态量：撤销可用性 / 今日已SL / 补偿刀可用
        last_rec = self.service.store.last_record(gid, st.battle_id)
        self_data["can_undo"] = bool(last_rec) and (
            last_rec["qqid"] == self_data["user_id"] or self_data["is_admin"])
        m_self = self.service.store.member(gid, self_data["user_id"])
        self_data["sl_today"] = bool(m_self and m_self["last_save_slot"] == d)
        recs_self = self.service.store.day_records(gid, st.battle_id, d,
                                                   qqid=self_data["user_id"])
        _, _, _, comp_left = rules._blade_stats(recs_self)
        self_data["comp_available"] = comp_left > 0
        return self._json({
            "code": 0,
            "groupData": {"group_id": gid, "group_name": st.group_name,
                          "game_server": st.game_server, "cycle": st.boss_cycle,
                          "battle_id": st.battle_id,
                          "pcrdate": d,
                          "today_blades": today_blades,
                          "quota": len(self.service.store.members(gid)) * 3},
            "today_counts": today_counts,
            "bossData": self.boss_data(st),
            "members": self.service.store.members(gid),
            "selfData": self_data,
        })

    async def _act_get_member_list(self, request, gid, st, sess, user, body):
        return self._json({"code": 0, "members": self.service.store.members(gid)})

    async def _act_get_challenge(self, request, gid, st, sess, user, body):
        ts = body.get("ts")
        d = pcr_datetime(st.game_server, int(ts))[0] if ts else pcr_datetime(st.game_server)[0]
        today, _ = pcr_datetime(st.game_server)
        return self._json({"code": 0,
                           "challenges": self.service.store.day_records(gid, st.battle_id, d),
                           "today": today})

    async def _act_get_user_challenge(self, request, gid, st, sess, user, body):
        qqid = int(body["qqid"])
        u = self.service.store.user(qqid)
        return self._json({"code": 0,
                           "challenges": self.service.store.records_of_member(
                               gid, st.battle_id, qqid),
                           "game_server": st.game_server,
                           "user_info": {"qqid": qqid,
                                         "nickname": u["nickname"] if u else str(qqid)}})

    async def _act_get_subscribers(self, request, gid, st, sess, user, body):
        subs = []
        for b in ("1", "2", "3", "4", "5"):
            for q, m in st.subscribes.get(b, {}).items():
                subs.append({"boss": int(b), "qqid": q,
                             "name": self.service._name_of(q), "message": m})
        return self._json({"code": 0, "group_name": st.group_name, "subscribers": subs})

    async def _act_get_score_table(self, request, gid, st, sess, user, body):
        return self._json({"code": 0, "rows": self.service.score_rows(st),
                           "threshold": st.threshold})

    async def _act_get_archives(self, request, gid, st, sess, user, body):
        return self._json({"code": 0,
                           "counts": self.service.store.archive_record_counts(gid),
                           "battle_id": st.battle_id})

    async def _act_get_statistics(self, request, gid, st, sess, user, body):
        """统计聚合（阶段3）：按档案或全部记录，输出每日趋势/成员榜/boss 占比。"""
        battle_id = body.get("battle_id", "current")
        data = stats.build_statistics(self.service.store, self.service.cfg, st,
                                      battle_id, self.service._name_of)
        return self._json({"code": 0, **data})

    async def _act_get_setting(self, request, gid, st, sess, user, body):
        async with self.service.group_lock(gid):  # 惰性补 apikey 也是写，锁内重读后再落库
            st = self.service.store.get_guild(gid) or st
            if not st.apikey:  # yobot 建会即生成；Aya 惰性补发，保证统计 API 可用
                st.apikey = self.service.store._rand(16)
                self.service.store.save_guild_state(st)
        return self._json({"code": 0,
                           "groupData": {"group_name": st.group_name,
                                         "game_server": st.game_server,
                                         "battle_id": st.battle_id},
                           "privacy": st.privacy, "notification": st.notification,
                           "apikey": st.apikey})

    # ---- 写动作（成员）
    async def _act_apply(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        target = int(body["behalf"]) if body.get("behalf") else operator
        self.service._require_member(gid, target)
        d, _ = pcr_datetime(st.game_server)
        result = rules.apply_for_challenge(
            self.service.cfg, st, target, body["boss_num"],
            is_continue=bool(body.get("is_continue")),
            behalf=operator if body.get("behalf") else None,
            records_today=self.service.store.day_records(gid, st.battle_id, d, qqid=target),
            name_of=self.service._name_of)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "apply", result.reply,
                     behalf=target if body.get("behalf") else None)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_cancelapply(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        target = int(body["behalf"]) if body.get("behalf") else operator
        if target != operator and user["authority_group"] >= 100:
            # 普通用户仅可取消自己的申请或自己代刀提交的申请
            ch = st.challengers.get(str(target))
            if ch is None or (ch.behalf or 0) != operator:
                return self._json({"code": 11, "message": "无权取消他人的申请"})
        result = rules.cancel_blade(st, target)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "cancel", result.reply, behalf=target)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_addrecord(self, request, gid, st, sess, user, body):
        reply, texts = self.service.do_challenge(
            gid, sess["qqid"], defeat=bool(body.get("defeat")),
            damage=body.get("damage"), behalfed=body.get("behalf"),
            is_continue=bool(body.get("is_continue")),
            boss_num=body.get("boss_num"))
        self._mirror(st, "addrecord", reply)       # 操作回执：受网页同步开关控制
        self._send_rule_notices(gid, texts[1:])    # 业务通知（下树/预约）：不受开关控制
        fresh = self.service.store.get_guild(gid)  # do_challenge 内部重新加载过状态
        return self._json({"code": 0, "message": reply, "bossData": self.boss_data(fresh)})

    async def _act_undo(self, request, gid, st, sess, user, body):
        last = self.service.store.last_record(gid, st.battle_id)
        result = rules.undo(st, sess["qqid"], user["authority_group"], last,
                            self.service.cfg, name_of=self.service._name_of)
        with self.service.store.batch():  # 撤日志 + 回血同一事务
            if last is not None:
                self.service.store.delete_record(last["cid"])
            self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "undo", result.reply)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_put_on_the_tree(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        target = int(body["behalf"]) if body.get("behalf") else operator
        self.service._require_member(gid, target)
        d, _ = pcr_datetime(st.game_server)
        boss_num = body.get("boss_num")
        result = rules.put_on_the_tree(
            self.service.cfg, st, operator,
            boss_num=boss_num if boss_num not in (None, "") else False,
            behalfed=target if body.get("behalf") else None,
            records_today=self.service.store.day_records(gid, st.battle_id, d, qqid=target),
            name_of=self.service._name_of)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "tree", result.reply, behalf=target)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_update_status(self, request, gid, st, sess, user, body):
        """更新出刀/挂树的剩余秒数、伤害（万）与留言（等同群聊"更新出刀/报伤害"）。"""
        qqid = int(body["qqid"])
        operator = sess["qqid"]
        ch = st.challengers.get(str(qqid))
        if ch is None:
            return self._json({"code": 10, "message": "该成员当前没有申请出刀或挂树"})
        if (qqid != operator and user["authority_group"] >= 100
                and (ch.behalf or 0) != operator):
            return self._json({"code": 11, "message": "无权修改他人的出刀状态"})
        seconds = body.get("seconds")
        result = rules.update_status(
            st, qqid,
            seconds=1 if seconds is None else int(seconds),  # 缺省=1 秒（同群聊报伤害）
            damage=int(body.get("damage") or 0),
            comment=body.get("message"),
            name_of=self.service._name_of)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_take_it_of_the_tree(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        target = int(body["behalf"]) if body.get("behalf") else operator
        if target != operator and user["authority_group"] >= 100:
            return self._json({"code": 11, "message": "只有管理员可以帮他人下树"})
        result = rules.take_it_of_the_tree(st, target)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "tree", result.reply, behalf=target)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_save_slot(self, request, gid, st, sess, user, body):
        target = int(body["member"])
        operator = sess["qqid"]
        if target != operator and not (user["authority_group"] < 100
                                       or self._is_super(user)):
            return self._json({"code": 11, "message": "只有管理员可以代记 SL"}, 403)
        self.service._require_member(gid, target)
        d, _ = pcr_datetime(st.game_server)
        member = self.service.store.member(gid, target)
        reply, slot, _ = rules.save_slot(
            st, target, d, member["last_save_slot"] if member else 0,
            clean_flag=not body.get("status", True))
        if member is None:
            self.service.store.upsert_member(gid, target)
        with self.service.store.batch():  # 成员 SL 与状态快照同事务
            self.service.store.set_last_save_slot(gid, target, slot)
            self.service.store.save_guild_state(st)
        self.service._emit(gid, reply)
        self._mirror(st, "save_slot",
                     f"{self.service._name_of(target)}已{'添加' if body.get('status', True) else '取消'}SL记录")
        return self._json({"code": 0, "message": reply})

    async def _act_add_subscribe(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        result = rules.subscribe(st, operator, str(body["boss_num"]),
                                 body.get("message") or "", name_of=self.service._name_of)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        u = self.service.store.user(operator)
        nick = (u["nickname"] if u else None) or str(operator)
        text = f"{nick}已预约{body['boss_num']}号boss"
        if body.get("message"):
            text += "\n留言：" + body["message"]
        self._mirror(st, "add_subscribe", text)
        return self._json({"code": 0, "message": result.reply or "预约成功"})

    async def _act_cancel_subscribe(self, request, gid, st, sess, user, body):
        operator = sess["qqid"]
        qq = int(body["qq"]) if body.get("qq") else operator
        if qq != operator and user["authority_group"] >= 100:
            return self._json({"code": 11, "message": "无权取消他人的预约"})
        result = rules.subscribe_cancel(st, body["boss_num"], qq)
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        u = self.service.store.user(operator)
        nick = (u["nickname"] if u else None) or str(operator)
        u2 = self.service.store.user(qq)
        nick2 = (u2["nickname"] if u2 else None) or str(qq)
        self._mirror(st, "cancel_subscribe", f"{nick2}已取消预约{body['boss_num']}号boss")
        return self._json({"code": 0, "message": result.reply})

    async def _act_change_password(self, request, gid, st, sess, user, body):
        pwd = body.get("pwd", "")
        if len(pwd) < 6:
            return self._json({"code": 30, "message": "密码至少 6 位"})
        self.service.store.set_password(sess["qqid"], pwd)
        return self._json({"code": 0, "message": "密码已修改"})

    # ---- 管理动作
    async def _act_modify(self, request, gid, st, sess, user, body):
        result = rules.modify(self.service.cfg, st, body.get("cycle"), body["bossData"])
        self.service.store.save_guild_state(st)
        self.service._emit(gid, result.reply)
        self._mirror(st, "modify", result.reply)
        return self._json({"code": 0, "message": result.reply,
                           "bossData": self.boss_data(st)})

    async def _act_send_remind(self, request, gid, st, sess, user, body):
        memberlist = body.get("memberlist") or []
        if not memberlist:
            return self._json({"code": 30, "message": "请先勾选成员"})
        text = (" ".join(f"[CQ:at,qq={q}]" for q in memberlist)
                + f"\n=======\n{self.service._name_of(sess['qqid'])}提醒您及时完成今日出刀")
        if body.get("send_private_msg"):
            if not self.setting.get("allow_bulk_private", True):
                return self._json({"code": 12, "message": "私聊通知已禁用"})
            # 批量私聊：后台串行发送，相邻两条间隔 3~10 秒（防风控）
            asyncio.get_running_loop().create_task(self._send_private_batch(
                [int(q) for q in memberlist],
                f"{self.service._name_of(sess['qqid'])}提醒您及时完成今日出刀"))
        else:
            asyncio.get_running_loop().create_task(self._safe_send_group(gid, text))
        self.service._emit(gid, text)
        return self._json({"code": 0, "message": "发送成功"})

    async def _act_drop_member(self, request, gid, st, sess, user, body):
        n = self.service.store.drop_members(gid, [int(q) for q in body["memberlist"]])
        return self._json({"code": 0, "message": f"已删除{n}条记录"})

    async def _act_add_member(self, request, gid, st, sess, user, body):
        qqid = int(body["qqid"])
        self.service.store.user_get_or_create(qqid)
        role = int(body.get("role", 100))
        self.service.store.upsert_member(gid, qqid, role)
        self.service._emit(gid, f"[CQ:at,qq={qqid}]已加入本公会")
        return self._json({"code": 0, "message": "ok"})

    async def _act_create_guild(self, request, gid, st, sess, user, body):
        if st is not None:
            return self._json({"code": 21, "message": "群已经存在"})
        server = body.get("game_server", "cn")
        if server not in ("cn", "jp", "tw"):
            return self._json({"code": 30, "message": "服务器只能是 cn/jp/tw"})
        self.service.store.create_guild(gid, self.service.cfg, server)
        return self._json({"code": 0, "message": "公会创建成功"})

    async def _act_put_setting(self, request, gid, st, sess, user, body):
        server = body["game_server"]
        if server not in ("cn", "jp", "tw"):
            return self._json({"code": 30, "message": "服务器只能是 cn/jp/tw"})
        st.notification = int(body["notification"])
        # 0x2（允许 api）没有前端开关，设置页只管理 0x1（游客可看）：
        # 整值覆盖会把它清掉，导致 apikey 导出接口静默失效
        st.privacy = (int(body["privacy"]) & ~0x2) | (st.privacy & 0x2)
        if body.get("apikey") is not None:
            st.apikey = body["apikey"] or None
        msg = "success"
        if server != st.game_server:
            # 切服必须开新档案：旧档案绑定旧服保留，实时状态按新服满血重置
            bids = [c["battle_id"] for c in self.service.store.archive_record_counts(gid)]
            new_bid = max([st.battle_id] + bids) + 1
            with self.service.store.batch():
                self.service.store.switch_archive(st, self.service.cfg, new_bid,
                                                  server=server)
                self.service.store.save_guild_state(st)
            msg = f"服务器已切换为 {server.upper()}，并新建档案 #{new_bid}"
        else:
            self.service.store.save_guild_state(st)
        return self._json({"code": 0, "message": msg, "battle_id": st.battle_id})

    async def _act_switch_data_slot(self, request, gid, st, sess, user, body):
        with self.service.store.batch():  # 备份旧档案 + 还原新档案 + 实时状态一次提交
            self.service.store.switch_archive(st, self.service.cfg, int(body["battle_id"]))
            self.service.store.save_guild_state(st)
        self.service._emit(gid, f"已切换至 {st.battle_id} 号档案")
        return self._json({"code": 0, "message": "success", "battle_id": st.battle_id,
                           "bossData": self.boss_data(st)})

    async def _act_clear_data_slot(self, request, gid, st, sess, user, body):
        bid = int(body["battle_id"]) if body.get("battle_id") is not None else st.battle_id
        self.service.store.clear_archive(gid, bid)
        return self._json({"code": 0, "message": f"{bid} 号档案记录已清空"})

    async def _act_grant_role(self, request, gid, st, sess, user, body):
        if not self._is_super(user):
            # 权限设置=主人专属：管理员互相升降会破坏整个权限体系（含 set_authority 全站表）
            return self._json({"code": 11, "message": "只有主人可以设置权限"})
        qqid, role = int(body["qqid"]), int(body["role"])
        if role not in (10, 100):
            return self._json({"code": 30, "message": "role 只能是 10 或 100"})
        target = self.service.store.user(qqid)
        if target and target["authority_group"] == 1:
            # 主人账号（authority=1）不可降级，含主人自降——降级即失去全站主人身份
            return self._json({"code": 31, "message": "主人账号不可被降级"})
        self.service.store.user_get_or_create(qqid)
        self.service.store.set_authority(qqid, role)
        self.service.store.upsert_member(gid, qqid, role)
        return self._json({"code": 0, "message": "ok"})

    # ---- 站点管理（super-admin）
    async def admin_api(self, request):
        sess, user = self._user(request)
        if sess is None:
            return self._json({"code": 10, "message": "未登录"}, 401)
        if not self._is_super(user):
            return self._json({"code": 11, "message": "需要主人权限"}, 403)
        body = await request.json()
        if body.get("csrf") != sess["csrf_token"]:
            return self._json({"code": 15, "message": "CSRF 校验失败"})
        action = body.get("action")
        if action == "get_global_setting":
            setting = {k: v for k, v in self.setting.items()
                       if k not in ("boss", "level_by_cycle", "boss_id")}
            return self._json({"code": 0, "setting": setting})
        if action == "put_global_setting":
            for k, v in body.get("setting", {}).items():
                if k not in ("super-admin",):
                    self.setting[k] = v
            self._save_config()
            return self._json({"code": 0, "message": "success"})
        if action == "get_boss_preset":
            # config 缺键时回退 DEFAULT 表展示（游戏运行时用的就是默认值，展示 0 会误导保存）
            from .gameconfig import DEFAULT_BOSS_HP, DEFAULT_BOSS_ID, DEFAULT_LEVEL_BY_CYCLE
            return self._json({"code": 0,
                               "boss_id": self.setting.get("boss_id") or DEFAULT_BOSS_ID,
                               "boss": self.setting.get("boss") or DEFAULT_BOSS_HP,
                               "level_by_cycle": self.setting.get("level_by_cycle") or DEFAULT_LEVEL_BY_CYCLE,
                               "names": self.boss_id_name})
        if action == "set_boss_preset":
            server = body.get("server")
            boss_id, boss, lbc = body.get("boss_id"), body.get("boss"), body.get("level_by_cycle")
            if (server not in ("cn", "jp", "tw") or not isinstance(boss_id, list) or len(boss_id) != 5
                    or not isinstance(boss, list) or len(boss) != 4 or any(len(row) != 5 for row in boss)
                    or not isinstance(lbc, list) or len(lbc) != 4):
                return self._json({"code": 30, "message": "数据格式不正确"})
            try:
                boss_id = [str(int(x)) for x in boss_id]
                boss = [[max(0, int(round(float(h)))) for h in row] for row in boss]
                lbc = [[int(a), int(b_)] for a, b_ in lbc]
            except (TypeError, ValueError):
                return self._json({"code": 30, "message": "数据格式不正确"})
            # 键赋值走共享外层字典 → cfg（GameConfig.load 持引用）即时生效；默认值情形重绑一次
            self.setting.setdefault("boss", {})[server] = boss
            self.setting.setdefault("level_by_cycle", {})[server] = lbc
            self.setting.setdefault("boss_id", {})[server] = boss_id
            self._ensure_cfg_tables()  # 补默认服 + 统一重绑 cfg
            self._save_config()
            return self._json({"code": 0, "message": f"{server.upper()} boss 预置已更新（下次拉取会覆盖手动修改）"})
        if action == "fetch_boss_data":
            return await self._fetch_boss_data()
        return self._json({"code": 32, "message": "unknown action"})

    async def _fetch_boss_data(self):
        msg = await self.fetch_boss_data()
        return self._json({"code": 0, "message": msg,
                           "boss_fetch": self.setting.get("boss_fetch")})

    async def fetch_boss_data(self) -> str:
        """拉取当期 boss 数据并更新配置，返回多行结果消息。

        每日定时任务（main.daily_jobs）与手动按钮共用；最近拉取时间/结果记入
        config `boss_fetch`，供前端小字展示。匹配见 pick_current_battle：
        年月=今天优先，跨月回退最近一期已开始的（satroki 新月档常延迟录入）。
        单服失败不影响其他服。"""
        import datetime
        import time as _time
        import aiohttp
        now = datetime.datetime.now(datetime.timezone.utc)
        back_msg = []
        for server in ("cn", "jp", "tw"):
            url = f"https://pcr.satroki.tech/api/Quest/GetClanBattleInfos?s={server}"
            try:
                async with aiohttp.ClientSession() as ses:
                    async with ses.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        infos = await resp.json()
                info = pick_current_battle(infos, now)
                if info is None:
                    back_msg.append(f"{server}未获取到当期数据")
                    continue
                boss_id, boss_hp, lap_from = [], [], []
                for phase in info["phases"]:
                    lap_from.append(phase["lapFrom"])
                    boss_hp.append([b["hp"] for b in phase["bosses"]])
                    if phase is info["phases"][0]:
                        boss_id = [str(b["unitId"]) for b in phase["bosses"]]
                        for i, b in enumerate(phase["bosses"], start=1):
                            self.boss_id_name.setdefault(str(i), {})[str(b["unitId"])] = b["name"]
                ranges = [[lap_from[i], lap_from[i + 1] - 1]
                          for i in range(len(lap_from) - 1)] + [[lap_from[-1], 999]]
                self.setting.setdefault("boss", {})[server] = boss_hp
                self.setting.setdefault("level_by_cycle", {})[server] = ranges
                self.setting.setdefault("boss_id", {})[server] = boss_id
                tag = f"{info['year']}-{info['month']:02d} {info['title']}"
                if info["year"] == now.year and info["month"] == now.month:
                    back_msg.append(f"{server}更新当期boss数据成功（{tag}）")
                else:
                    back_msg.append(f"{server}当月无数据，已按最近一期更新（{tag}）")
            except Exception as e:  # noqa: BLE001
                back_msg.append(f"{server}更新失败: {e}")
        self.setting["boss_fetch"] = {"time": _time.strftime("%Y-%m-%d %H:%M"),
                                      "msg": "\n".join(back_msg)}
        self._ensure_cfg_tables()  # 补默认服（保留失败服旧值）+ 重绑运行配置
        self._save_config()
        with open(os.path.join(os.path.dirname(__file__), "boss_id_name.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.boss_id_name, f, ensure_ascii=False, indent=4)
        return "\n".join(back_msg)

    # ---- 出刀记录 JSON API（yobot statistics/api 同语义，供外部工具）
    async def statistics_api(self, request):
        gid = int(request.match_info["gid"])
        store = self.service.store
        st = store.get_guild(gid)
        if st is None:
            return self._json({"code": 20, "message": "Group not exists"})
        apikey = request.query.get("apikey")
        if apikey:
            # 通过 apikey 外部访问
            if not (st.privacy & 0x2):
                return self._json({"code": 11, "message": "api not allowed"})
            if apikey != st.apikey:
                return self._json({"code": 12, "message": "Invalid apikey"})
        else:
            # 内部直接访问：登录成员或管理员
            sess, user = self._user(request)
            if sess is None:
                return self._json({"code": 10, "message": "Not logged in"})
            if not store.is_member(gid, sess["qqid"]) and not (
                    user and user["authority_group"] < 100):
                return self._json({"code": 11, "message": "Insufficient authority"})
        battle_id = request.query.get("battle_id")
        conds, args = ["gid=?"], [gid]
        if battle_id is not None:
            if battle_id.isdigit():
                conds.append("bid=?")
                args.append(int(battle_id))
            elif battle_id == "all":
                pass
            elif battle_id == "current":
                conds.append("bid=?")
                args.append(st.battle_id)
            else:
                return self._json({"code": 20,
                                   "message": f'unexceptd value "{battle_id}" for battle_id'})
        rows = store.db.execute(
            f"SELECT * FROM clan_challenge WHERE {' AND '.join(conds)}", args).fetchall()
        resp = self._json({"code": 0, "message": "OK", "api_version": 1,
                           "challenges": [dict(r) for r in rows],
                           "groupinfo": {"group_id": gid, "group_name": st.group_name,
                                         "game_server": st.game_server,
                                         "battle_id": st.battle_id},
                           "members": store.members(gid)})
        if st.privacy & 0x2:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # ------------------------------------------------------------ 镜像
    def _send_rule_notices(self, gid: int, texts: list[str]) -> None:
        """规则业务通知（可以下树/新王预约提醒）：与网页操作镜像开关无关，
        网页操作也必须送达；只在调用方未发送过的通知列表上使用，保证每条只发一次。"""
        if self.onebot is None:
            return
        for text in texts:
            if text:
                asyncio.get_running_loop().create_task(self._safe_send_group(gid, text))

    def _mirror(self, st, kind: str, text: str, behalf: int | None = None) -> None:
        bit = NOTIFY.get(kind, 0)
        if not (st.notification & bit) or self.onebot is None:
            return
        if behalf:
            text = f"[CQ:at,qq={behalf}]{text}"
        asyncio.get_running_loop().create_task(self._safe_send_group(st.group_id, text))

    async def _safe_send_group(self, gid: int, text: str) -> None:
        try:
            await self.onebot.send_group(gid, text)
        except Exception:  # noqa: BLE001  风控/未连接：fire-and-forget，只记日志
            logger.warning("镜像通知发送失败 group=%s（已按约定丢弃）", gid)

    async def _send_private_batch(self, qqids: list[int], text: str) -> None:
        """批量私聊：串行发送，相邻两条随机间隔 3~10 秒（防风控）。"""
        for i, qqid in enumerate(qqids):
            if i:
                await asyncio.sleep(random.uniform(*PRIVATE_REMIND_INTERVAL))
            await self._safe_send_private(qqid, text)

    async def _safe_send_private(self, qqid: int, text: str) -> None:
        try:
            await self.onebot.send_private(qqid, text)
        except Exception:  # noqa: BLE001
            logger.warning("私聊通知发送失败 user=%s（已按约定丢弃）", qqid)
