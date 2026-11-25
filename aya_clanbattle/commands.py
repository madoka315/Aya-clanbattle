"""群聊指令层：正则解析（移植自 yobot_remix kernel.py，文案与格式保持一致）
→ rules 规则层 → 落库 → 回复与通知文本。

服务不直接发消息：handle_* 返回 Outgoing 列表，由 onebot 层统一发送。
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass

from . import rules, stats, timecalc
from .gameconfig import GameConfig
from .pcr_time import pcr_datetime
from .rules import RuleError
from .store import Store

logger = logging.getLogger(__name__)

VERSION = "Aya-clanbattle v0.1"

SERVER = {"日": "jp", "台": "tw", "国": "cn"}
UNIT = {"W": 10000, "w": 10000, "万": 10000, "k": 1000, "K": 1000, "千": 1000}

NOT_IN_GUILD = "未加入公会，请先发送“加入公会”"


def _exact(*words):
    """整条消息精确匹配（忽略大小写）。"""
    pool = {w.casefold() for w in words}
    return lambda cmd: cmd.casefold() in pool


def _prefix(*prefixes):
    """前缀匹配：参数由 handler 自己解析（纯计算类）。"""
    return lambda cmd: cmd.startswith(prefixes)


def _rx(pattern: str):
    """正则匹配，仅用于路由判定；参数仍由 handler 解析。"""
    rx = re.compile(pattern, re.IGNORECASE)
    return lambda cmd: bool(rx.match(cmd))


@dataclass(frozen=True)
class Cmd:
    """一条指令声明。match 只做路由；权限与群锁由统一入口执行。"""
    name: str
    handler: str
    match: object
    perm: str = "public"  # public / member / group_admin / admin_or_super
    lock: bool = True
    deny: str = ""  # perm 不满足时的回复（admin 类必填）


# 顺序即优先级，first-match-wins。宽前缀（尾刀/报刀/报伤害）必须放最后。
CMD_LIST = [
    Cmd("version", "_h_version", _exact("ver", "v", "version"), lock=False),
    Cmd("timecalc_combine", "_h_combine_calc", _prefix("合刀计算"), lock=False),
    Cmd("timecalc_pad", "_h_pad_calc", _prefix("垫刀计算"), lock=False),
    Cmd("timecalc_axis", "_h_comp_timeline", _prefix("补偿轴"), lock=False),
    Cmd("panel", "_h_panel", _exact("面板", "后台"), lock=False),
    Cmd("status", "_h_status", _exact("状态")),
    Cmd("day_table", "_h_day_table", _exact("查刀")),
    Cmd("tree_query", "_h_query_tree", _exact("查树", "查1", "查2", "查3", "查4", "查5")),
    Cmd("join_all", "_h_join", _exact("加入全部成员"), perm="group_admin",
        deny="只有管理员才可以加入全部成员"),
    Cmd("join", "_h_join", _rx(r"^加入[公工行]会(?: *\[CQ:at,qq=\d+\])? *$")),
    Cmd("create", "_h_create", _rx(r"^创建(?:[日台韩国]服)?[公工行]会$")),
    Cmd("reset", "_h_reset_progress", _exact("重置进度"), perm="admin_or_super",
        deny="只有管理员或主人可使用重置进度功能"),
    Cmd("grant", "_h_grant_role", _rx(r"^权限")),
    Cmd("subscribe", "_h_subscribe", _rx(r"^预约"), perm="member"),
    Cmd("day_summary", "_h_day_summary", _rx(r"^出刀(?:记录|情况|状况|详情)$")),
    Cmd("my_blades", "_h_my_blades", _rx(r"^刀伤 *(?:\[CQ:at,qq=\d+\])? *$")),
    Cmd("stats", "_h_stats_report", _exact("战报", "统计"), lock=False),
    Cmd("undo", "_h_undo", _exact("撤销"), perm="member"),
    Cmd("tree", "_h_tree", _rx(r"^挂树"), perm="member"),
    Cmd("apply", "_h_apply", _rx(r"^(?:进[1-5]|申请出刀)"), perm="member"),
    Cmd("cancel", "_h_cancel", _rx(r"^取消")),
    Cmd("give_up", "_h_give_up", _rx(r"^不[打进]了")),
    Cmd("save_slot", "_h_save_slot", _rx(r"^sl")),
    Cmd("report_hurt", "_h_report_hurt", _rx(r"^(?:更新|报伤)")),
    Cmd("tail", "_h_report_defeat", _rx(r"^(?:尾刀|尾[1-5])")),
    Cmd("report", "_h_report_damage", _rx(r"^(?:报刀|刀[1-5])")),
]


@dataclass
class Outgoing:
    group_id: int
    text: str


# 卡图已体现的回复行（_challenge_lines 输出）：回图时从文本中剥掉，只留确认/补偿刀提示
_CARD_LINE_RE = re.compile(
    r"^\d+周目\d王，剩余|^(?:目前无人在挑战这个boss|当前有\d+人正在挑战这个boss|-{20}|->)")


def _keep_reply_core(text: str) -> str:
    return "\n".join(ln for ln in text.split("\n")
                     if ln.strip() and not _CARD_LINE_RE.match(ln))


class ClanBattleService:
    """群聊指令入口。api 为 onebot 的 API 调用器（未连接时为 None）。"""

    def __init__(self, store: Store, cfg: GameConfig, setting: dict):
        self.store = store
        self.cfg = cfg
        self.setting = setting
        self.api = None  # 由 onebot 层注入：await api.call(action, **params)
        self._group_locks: dict[int, asyncio.Lock] = {}
        self.mutation_hooks = []  # (gid, notice_text) -> None，web 层注册做 SSE 推送
        self.config_path: str | None = None  # super-admin 自动登记时回写 config.json

    def _emit(self, gid: int, text: str) -> None:
        if not text:
            return
        for hook in self.mutation_hooks:
            try:
                hook(gid, text)
            except Exception:  # noqa: BLE001
                logger.exception("mutation hook 失败")

    def _save_config(self) -> None:
        if not self.config_path:
            return
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.setting, f, ensure_ascii=False, indent=4)

    # ------------------------------------------------------------ 入口
    @staticmethod
    def match(cmd: str) -> "Cmd | None":
        """按 CMD_LIST 顺序首个命中即返回；未命中返回 None。"""
        for c in CMD_LIST:
            if c.match(cmd):
                return c
        return None

    async def handle_group(self, ctx: dict) -> list[Outgoing]:
        cmd = ctx["raw_message"].strip()
        c = self.match(cmd)
        if c is None:
            logger.debug("未识别的群指令: %s", cmd[:50])
            return []
        # 纯计算/只读指令不排群锁，避免阻塞出刀
        if c.lock:
            async with self.group_lock(ctx["group_id"]):
                return await self._run(ctx, c, cmd)
        return await self._run(ctx, c, cmd)

    def group_lock(self, gid: int) -> asyncio.Lock:
        """每群串行锁：群指令与网页写操作共用，状态读-改-写不得与另一写者交错。"""
        return self._group_locks.setdefault(gid, asyncio.Lock())

    def _check_perm(self, c: Cmd, ctx: dict) -> str | None:
        """统一权限：返回拒绝文案则直接回复，None 表示放行。"""
        if c.perm == "public":
            return None
        if c.perm == "member":
            self._require_member(ctx["group_id"], ctx["user_id"])
            return None
        if c.perm == "group_admin":
            return None if self._is_admin_ctx(ctx) else c.deny
        if c.perm == "admin_or_super":
            ok = (self._is_admin_ctx(ctx)
                  or ctx["user_id"] in self.setting.get("super-admin", []))
            return None if ok else c.deny
        return None

    async def _run(self, ctx: dict, c: Cmd, cmd: str) -> list[Outgoing]:
        gid = ctx["group_id"]
        try:
            deny = self._check_perm(c, ctx)
            if deny:
                return [Outgoing(gid, deny)]
            out = await getattr(self, c.handler)(ctx, cmd)
        except RuleError as e:
            out = [str(e)]
        except Exception:  # noqa: BLE001  指令处理兜底，不让异常打断事件循环
            logger.exception("指令处理失败: %s", cmd)
            return [Outgoing(gid, f"指令处理失败，请查看日志\n{self._panel_url(gid)}")]
        return [Outgoing(gid, t) for t in out if t]

    async def sync_members(self, gid: int, member_list: list) -> int:
        """全量同步群成员（昵称/群名片/角色）。仅管理员显式触发（加入全部成员/网页加成员）。"""
        for member in member_list:
            qqid = member["user_id"]
            self.store.user_get_or_create(qqid, member.get("card") or member.get("nickname"))
            role = 100 if member.get("role") == "member" else 10
            self.store.upsert_member(gid, qqid, role)
        return len(member_list)

    async def update_existing_members(self, gid: int, member_list: list) -> int:
        """每日同步用：只更新已在会战成员表里的人（群员≠会战成员，不自动纳新）。"""
        existing = {m["qqid"] for m in self.store.members(gid)}
        n = 0
        for member in member_list:
            qqid = member["user_id"]
            if qqid not in existing:
                continue
            user = self.store.user_get_or_create(qqid)
            card = member.get("card") or member.get("nickname")
            if card and user["nickname"] != card:
                self.store.db.execute("UPDATE user SET nickname=? WHERE qqid=?",
                                      (card, qqid))
            role = 100 if member.get("role") == "member" else 10
            self.store.upsert_member(gid, qqid, role)
            n += 1
        self.store.db.commit()
        return n

    async def daily_sync(self) -> str:
        """每日自动同步：群名、成员昵称/名片、全员头像（05:05 触发）。"""
        if self.api is None:
            return "协议端未连接"
        gids = [r["group_id"] for r in self.store.db.execute("SELECT group_id FROM guild")]
        for gid in gids:
            try:
                info = await self.api.call("get_group_info", group_id=gid)
                # 整份保存必须锁内重读：拉群名期间群指令可能已改血量/出刀状态
                async with self.group_lock(gid):
                    st = self.store.get_guild(gid)
                    name = (info or {}).get("group_name")
                    if st and name and st.group_name != name:
                        st.group_name = name
                        self.store.save_guild_state(st)
            except Exception:  # noqa: BLE001
                logger.warning("群 %s 群名同步失败", gid)
            try:
                member_list = await self.api.call("get_group_member_list", group_id=gid)
                await self.update_existing_members(gid, member_list)
            except Exception:  # noqa: BLE001
                logger.warning("群 %s 成员同步失败", gid)
        from . import images
        qqids = [r["qqid"] for r in self.store.db.execute("SELECT DISTINCT qqid FROM member")]
        await images.download_avatars(qqids)
        return f"已同步 {len(gids)} 个公会"

    # ------------------------------------------------------------ 私聊指令
    async def handle_private(self, ctx: dict) -> str | None:
        cmd = ctx["raw_message"].strip()
        if cmd in ("登录", "登陆"):
            return self._make_login_url(ctx["user_id"])
        if cmd == "重置密码":
            return self._reset_password(ctx["user_id"])
        return None

    def _make_login_url(self, qqid: int) -> str:
        from urllib.parse import urljoin
        self.store.user_get_or_create(qqid)
        if not self.setting.get("super-admin"):
            # 首位登录者自动成为主人（对齐 yobot）
            self.setting.setdefault("super-admin", []).append(qqid)
            self.store.set_authority(qqid, 1)
            self._save_config()
        elif qqid in self.setting["super-admin"]:
            self.store.set_authority(qqid, 1)
        code = self.store._rand(6)
        self.store.set_login_code(qqid, code)
        base = self.setting.get("public_address", "")
        return ("已生成登录链接，60 秒内有效且仅可使用一次：\n"
                + urljoin(base, f"login/?qqid={qqid}&key={code}"))

    def _reset_password(self, qqid: int) -> str:
        pwd = self.store._rand(8)
        self.store.user_get_or_create(qqid)
        self.store.set_password(qqid, pwd, must_change=True, kick=True)
        return f"您的临时密码是：{pwd}\n请登录后尽快在面板中修改。"

    # ------------------------------------------------------------ 工具
    def _panel_url(self, gid: int) -> str:
        from urllib.parse import urljoin
        return urljoin(self.setting.get("public_address", ""),
                       f"clan/{gid}/")

    def _state(self, gid: int):
        st = self.store.get_guild(gid)
        if st is None:
            raise rules.GroupError("本群未初始化，请发送“创建国服公会”")
        return st

    def _require_member(self, gid: int, qqid: int):
        if not self.store.is_member(gid, qqid):
            raise rules.UserError(NOT_IN_GUILD)

    def _authority(self, qqid: int) -> int:
        row = self.store.user_get_or_create(qqid)
        return row["authority_group"]

    def _is_admin_ctx(self, ctx: dict) -> bool:
        return ctx.get("sender", {}).get("role") in ("owner", "admin")

    def _name_of(self, qqid: int):
        return self.store.nickname_of(qqid)

    def _today(self, st):
        return pcr_datetime(st.game_server)

    def _flush(self, st, result: rules.RuleResult) -> list[str]:
        """生成通知文本 + 落库：先处理通知对状态的消费（预约清除），再整份保存，
        否则"预约已被提醒消耗"不会持久化，后续报刀会重复提醒。"""
        texts = []
        for notice in result.notices:
            kind = notice[0]
            if kind == "tree_down":
                texts.append(rules.build_tree_down_text(notice[2]))
            elif kind == "subscribe_remind":
                text = rules.build_subscribe_remind(st, notice[1], self._name_of)
                rules.clear_subscribes(st, notice[1])  # 无条件清（合刀全静默时也消耗预约）
                if text:
                    texts.append(text)
        self.store.save_guild_state(st)
        return texts

    # ------------------------------------------------------------ 1 创建
    async def _h_create(self, ctx, cmd):
        m = re.match(r"^创建(?:([日台韩国])服)?[公工行]会$", cmd)
        if not m:
            return []
        if m.group(1) == "韩":
            return ["暂不支持韩服，仅支持国服/日服/台服"]
        gid = ctx["group_id"]
        if self.store.get_guild(gid) is not None:
            return ["群已经存在"]
        server = SERVER.get(m.group(1), "cn")
        self.store.create_guild(gid, self.cfg, server,
                                group_name=ctx.get("sender", {}).get("card"))
        return ["公会创建成功，请发送“加入公会”加入，"
                f"网页面板：{self._panel_url(gid)}"]

    # ------------------------------------------------------------ 2 加入
    async def _h_join(self, ctx, cmd):
        gid = ctx["group_id"]
        if cmd == "加入全部成员":  # 管理员权限已由派发层校验
            if self.api is None:
                return ["协议端未连接，暂时无法获取群成员列表"]
            member_list = await self.api.call("get_group_member_list", group_id=gid)
            await self.sync_members(gid, member_list)
            return ["本群所有成员已添加记录"]
        m = re.match(r"^加入[公工行]会 *(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        if m.group(1):
            if not self._is_admin_ctx(ctx):
                return ["只有管理员才可以加入其他成员"]
            qqid = int(m.group(1))
        else:
            qqid = ctx["user_id"]
            self.store.user_get_or_create(
                qqid, ctx.get("sender", {}).get("card") or ctx.get("sender", {}).get("nickname"))
        role = 100 if ctx.get("sender", {}).get("role") == "member" else 10
        self.store.upsert_member(gid, qqid, role)
        return [f"[CQ:at,qq={qqid}]已加入本公会"]

    # ------------------------------------------------------------ 3 状态
    def _boss_dict(self, st, i: int) -> dict:
        """单个 boss 的状态卡数据（状态图/交互指令回卡共用）。i 为 0 基下标。"""
        from . import images
        from .gameconfig import load_boss_id_name
        names = load_boss_id_name()
        icon = self.cfg.icon_id(st.game_server, i)
        name = names.get(str(i + 1), {}).get(icon, icon)
        b = str(i + 1)
        now, nxt_hp = st.now[b], st.nxt[b]
        is_next = now == 0
        blocked = is_next and not rules.check_next_boss(self.cfg, st, b)
        cycle = st.boss_cycle + 1 if is_next else st.boss_cycle
        chs = []
        for ch in st.challengers.values():
            if ch.boss_num != b:
                continue
            chs.append({"qqid": ch.qqid, "name": self._name_of(ch.qqid),
                        "comp": bool(ch.is_continue),
                        "behalf": self._name_of(ch.behalf) if ch.behalf else None,
                        "seconds": ch.seconds, "damage": ch.damage,
                        "tree": bool(ch.tree), "comment": ch.comment})
        subs = [{"qqid": q, "name": self._name_of(q)[:images.CHIP_NAME_MAX],
                 "note": note} for q, note in st.subscribes.get(b, {}).items()]
        return {"num": i + 1, "name": name, "icon": icon, "cycle": cycle,
                "is_next": is_next, "blocked": blocked,
                "health": 0 if blocked else (nxt_hp if is_next else now),
                "full": 0 if blocked else self.cfg.full_health(st.game_server, cycle, b),
                "challengers": chs, "subs": subs}

    async def _with_boss_card(self, st, boss_num, texts: list,
                              keep_text: bool = True, hit: dict | None = None) -> list:
        """boss 交互指令成功后回卡图；keep_text=False 时仅回图（通知类消息保留）。
        hit 非空时在图上叠加本次伤害（血条残影+跳伤）。"""
        try:
            i = int(boss_num) - 1
            if not 0 <= i < 5:
                return texts
            from . import images
            card = self._boss_dict(st, i)
            await images.ensure_avatars([c["qqid"] for c in card["challengers"]]
                                        + [s["qqid"] for s in card["subs"]],
                                        timeout=3.0)
            img = images.render_boss_card(card, hit=hit)
            cq = images.to_cq_image(img)
            if not keep_text:
                return [cq] + list(texts[1:])
            head = _keep_reply_core(texts[0]) if texts else ""
            # 文本与卡图并入同一条消息（图随文末），通知类消息仍单独发
            merged = (f"{head}\n" if head else "") + cq
            return [merged] + list(texts[1:])
        except Exception:  # noqa: BLE001
            logger.exception("boss 卡渲染失败")
            return texts

    async def _h_status(self, ctx, cmd):
        if cmd != "状态":
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        d, _ = self._today(st)
        records = self.store.day_records(gid, st.battle_id, d)
        try:
            from . import images
            # 首次渲染就把全群头像补齐/更新（超时 4s 不阻塞出图）
            await images.ensure_avatars([m["qqid"] for m in self.store.members(gid)],
                                        timeout=4.0)
            rows_d, total, comp_left = self._day_rows(st, d)
            # 成员级待出补偿（口径同 _day_rows：尾刀 +1、补偿刀 -1），供状态图补偿卡
            comps = []
            for r in rows_d:
                n = (sum(1 for c in r["recs"] if c["boss_health_remain"] == 0 and not c["is_continue"])
                     - sum(1 for c in r["recs"] if c["is_continue"]))
                if n > 0:
                    comps.append({"qqid": r["qqid"], "name": self._name_of(r["qqid"]), "count": n})
            quota = len(rows_d) * 3
            level = self.cfg.level_by_cycle_of(st.game_server, st.boss_cycle)
            lbc = self.cfg.level_by_cycle[st.game_server]
            nxt = lbc[level + 1] if level + 1 < len(lbc) else None
            next_hint = (f"{nxt[0]}周目进入下一阶段" if nxt else "已是最终阶段")  # 只提示左端周目（用户拍板）
            bosses = [self._boss_dict(st, i) for i in range(5)]
            from .pcr_time import pcr_date_str
            img = images.render_panel_status(
                st.group_name or str(gid), pcr_date_str(d, st.game_server),
                {"blades": total, "quota": quota, "comp_left": comp_left,
                 "level": chr(65 + level), "next_hint": next_hint, "comps": comps}, bosses)
            return [images.to_cq_image(img)]
        except Exception:  # noqa: BLE001  渲染失败回退文本
            logger.exception("状态图渲染失败")
            return [rules.status_summary(self.cfg, st, self._name_of)]

    # ------------------------------------------------------------ 4 报刀
    async def _h_report_damage(self, ctx, cmd):
        m = (re.match(r"^(?:报刀|刀) ?(?:[\-\=]([1-5]))? ?(\d+)?([Ww万Kk千])? *(补偿|补|b|bc|B|BC|Bc|bC)?"
                      r" *(?:\[CQ:at,qq=(\d+)\])? *(昨[日天])?$", cmd)
             or re.match(r"^(?:报刀|刀) ?([1-5])? (\d+)?([Ww万Kk千])? *(补偿|补|b|bc|B|BC|Bc|bC)?"
                         r" *(?:\[CQ:at,qq=(\d+)\])? *(昨[日天])?$", cmd))
        if not m:
            return ["报刀格式:\n报刀 100w（需先申请出刀）\n报刀 -1 100w（-1表示报在1王）"]
        unit = UNIT.get(m.group(3), 1)
        return await self._do_challenge(
            ctx, defeat=False,
            damage=int(m.group(2) or 0) * unit,
            is_continue=bool(m.group(4)),
            behalf=m.group(5), boss_num=m.group(1),
            previous_day=bool(m.group(6)))

    # ------------------------------------------------------------ 5 尾刀
    async def _h_report_defeat(self, ctx, cmd):
        m = re.match(r"^(?:尾刀|尾) ?([1-5])? *(补偿|补|b|bc|B|BC|Bc|bC)?"
                     r" ?(?:\[CQ:at,qq=(\d+)\])? *(昨[日天])?$", cmd)
        if not m:
            return []
        return await self._do_challenge(
            ctx, defeat=True, damage=None,
            is_continue=bool(m.group(2)),
            behalf=m.group(3), boss_num=m.group(1),
            previous_day=bool(m.group(4)))

    def do_challenge(self, gid: int, operator: int, *, defeat: bool, damage,
                     behalfed: int | None = None, is_continue: bool = False,
                     boss_num=None, previous_day: bool = False) -> tuple[str, list[str]]:
        """报刀/尾刀共用流程（群指令与网页 API 共用）。返回 (回复文本, 通知文本列表)。"""
        self._require_member(gid, operator)
        st = self._state(gid)
        d, t = self._today(st)
        guild_today_count = self.store.day_record_count(gid, st.battle_id, d)
        # 代报必须校验真实出刀者身份：否则可给任意 QQ 记账，删成员也拦不住（旧版 challenge 语义）
        real = int(behalfed) if behalfed else operator
        if real != operator:
            self._require_member(gid, real)
        target_day = d - 1 if previous_day else d
        result = rules.challenge(
            self.cfg, st, operator, defeat, damage,
            behalfed=behalfed, is_continue=is_continue, boss_num=boss_num,
            records_today=self.store.day_records(gid, st.battle_id, d, qqid=real),
            records_target=self.store.day_records(gid, st.battle_id, target_day, qqid=real),
            guild_today_count=guild_today_count,
            previous_day=previous_day, pcr_d=d, pcr_t=t,
            name_of=self._name_of)
        # 出刀日志与状态快照必须同事务：任一失败一起回滚，避免血量与日志不一致
        with self.store.batch():
            for rec in result.records:
                self.store.insert_record(st, rec, result.pcrdate, result.pcrtime)
            texts = [result.reply] + self._flush(st, result)
        self._emit(gid, result.reply)
        return result.reply, texts

    async def _do_challenge(self, ctx, *, defeat, damage, is_continue,
                            behalf, boss_num, previous_day) -> list[str]:
        gid, user_id = ctx["group_id"], ctx["user_id"]
        st = self._state(gid)
        real = int(behalf) if behalf else user_id
        ch = st.challengers.get(str(real))
        bn = boss_num or (ch.boss_num if ch else None)  # 尾刀会消掉申请，先取
        before_hp = 0
        if bn:
            now_hp = st.now.get(str(bn), 0)
            before_hp = now_hp if now_hp > 0 else st.nxt.get(str(bn), 0)
        _, texts = self.do_challenge(
            gid, user_id, defeat=defeat, damage=damage,
            behalfed=int(behalf) if behalf else None,
            is_continue=is_continue, boss_num=boss_num, previous_day=previous_day)
        if bn:
            # do_challenge 内部另取状态并变更（_flush 落库），回图必须重取最新态
            hit = None
            if not previous_day and before_hp > 0:
                dmg = before_hp if defeat else int(damage or 0)
                if dmg > 0:
                    hit = {"qqid": real, "name": self._name_of(real), "damage": dmg,
                           "before": before_hp, "defeat": defeat}
            texts = await self._with_boss_card(self._state(gid), bn, texts, hit=hit)
        return texts

    # ------------------------------------------------------------ 6 撤销
    async def _h_undo(self, ctx, cmd):
        if cmd != "撤销":
            return []
        gid, user_id = ctx["group_id"], ctx["user_id"]
        st = self._state(gid)
        last = self.store.last_record(gid, st.battle_id)
        bn = last["boss_num"] if last else None
        result = rules.undo(st, user_id, self._authority(user_id), last,
                            self.cfg, name_of=self._name_of)
        with self.store.batch():  # 撤日志 + 回血同一事务
            if last is not None:
                self.store.delete_record(last["cid"])
            self.store.save_guild_state(st)
        self._emit(gid, result.reply)
        if bn:
            return await self._with_boss_card(st, bn, [result.reply])
        return [result.reply]

    # ------------------------------------------------------------ 7 预约
    async def _h_subscribe(self, ctx, cmd):
        # 合刀标记位置不限：预约4:合 / 预约4: 合 / 预约4合 / 预约合4 / 预约4 合刀…（全半角冒号均可）
        m = re.match(r"^预约\s*(合)?\s*([1-5]|表)\s*(合[^:]*)?(?:[:：]\s*(.*))?"
                     r"(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        user_id = int(m.group(5)) if m.group(5) else ctx["user_id"]
        note = " ".join(p.strip() for p in (m.group(1), m.group(3), m.group(4))
                        if p and p.strip())
        result = rules.subscribe(st, user_id, m.group(2), note,
                                 name_of=self._name_of)
        self.store.save_guild_state(st)
        self._emit(gid, result.reply)
        if m.group(2) != "表":
            return await self._with_boss_card(st, m.group(2), [result.reply])
        return [result.reply]

    # ------------------------------------------------------------ 8 业绩表
    async def _h_day_table(self, ctx, cmd):
        if cmd != "查刀":
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        d, _ = self._today(st)
        rows, total, comp_left = self._day_rows(st, d)
        if not st.group_name and self.api is not None:
            try:
                info = await self.api.call("get_group_info", group_id=gid)
                if info and info.get("group_name"):
                    st.group_name = info["group_name"]
                    self.store.save_guild_state(st)
            except Exception:  # noqa: BLE001
                pass
        from .pcr_time import pcr_date_str
        quota = len(rows) * 3
        try:
            from . import images
            await images.ensure_avatars([r["qqid"] for r in rows])
            rows_view = [{"qqid": r["qqid"], "name": self._name_of(r["qqid"]), "fin": r["fin"],
                          "sl": r["sl_today"], "recs": r["recs"]} for r in rows]
            img = images.render_day_table(
                st.group_name or str(gid), pcr_date_str(d, st.game_server),
                rows_view, total, quota, comp_left)
            return [images.to_cq_image(img) + f"\n详细数据前往面板查看：\n{self._panel_url(gid)}"]
        except Exception:  # noqa: BLE001  渲染失败回退文本
            logger.exception("查刀记录表渲染失败")
            zero = [self._name_of(r["qqid"]) for r in rows if not r["recs"]]
            return [f"今日出刀 {total:g}/{quota}，待出补偿 {comp_left:g}；未出刀：{'、'.join(zero) or '无'}"
                    + f"\n详细数据前往面板查看：\n{self._panel_url(gid)}"]

    def _day_rows(self, st, d: int):
        """当天全员出刀行：[{qqid, fin, sl_today, recs, blades}] + (总数, 待出补偿)。"""
        rows, comp_left, total = [], 0, 0.0
        for member in self.store.members(st.group_id):
            recs = self.store.day_records(st.group_id, st.battle_id, d, qqid=member["qqid"])
            fin = 0.0
            blades = []
            for c in recs:
                comp = bool(c["is_continue"])
                tail = c["boss_health_remain"] == 0 and not comp
                if tail:
                    fin += 0.5
                    comp_left += 1
                elif comp:
                    fin += 0.5
                    comp_left -= 1
                else:
                    fin += 1
                blades.append(f"({c['boss_cycle']}-{c['boss_num']})"
                              f"{c['challenge_damage']}{'(补)' if comp else ''}"
                              f"{'(代)' if c['behalf'] else ''}")
            if recs:
                total += fin
            rows.append({"qqid": member["qqid"], "fin": fin, "recs": recs,
                         "blades": blades,
                         "sl_today": member["last_save_slot"] == d})
        rows.sort(key=lambda r: (-r["fin"], r["qqid"]))
        return rows, total, comp_left

    def score_rows(self, st) -> list[dict]:
        """业绩表结构化行（群聊文本与网页共用）。"""
        scores: dict[int, dict] = {}
        for member in self.store.members(st.group_id):
            u = self.store.user(member["qqid"])
            if u and u["deleted"]:
                continue
            challenges = self.store.records_of_member(st.group_id, st.battle_id, member["qqid"])
            for info in challenges:
                score = 0
                if info["boss_health_remain"] > 0 and not info["is_continue"]:
                    score = 1
                elif info["boss_health_remain"] == 0 and not info["is_continue"]:
                    score = 1 if info["challenge_damage"] >= st.threshold else 0.5
                elif info["is_continue"]:
                    score = 1 if info["challenge_damage"] >= st.threshold else 0.5
                scorer = info["behalf"] or info["qqid"]
                item = scores.setdefault(scorer, {"score": 0, "full": 0, "end": 0, "comp": 0})
                item["score"] += score
                if info["boss_health_remain"] > 0 and not info["is_continue"]:
                    item["full"] += 1
                elif info["boss_health_remain"] == 0 and not info["is_continue"]:
                    item["end"] += 1
                elif info["is_continue"]:
                    item["comp"] += 1
        ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
        return [{"qqid": q, "name": self._name_of(q), **item} for q, item in ranked]

    def _score_table(self, st) -> list[str]:
        lines = [f"{r['name']}  分数：{r['score']:g}  整刀：{r['full']}"
                 f"  尾刀：{r['end']}  小尾刀：{r['comp']}" for r in self.score_rows(st)]
        return lines or ["本档案还没有出刀记录"]

    # ------------------------------------------------------------ 9 出刀记录
    async def _h_day_summary(self, ctx, cmd):
        if not re.match(r"^出刀(记录|情况|状况|详情) *$", cmd):
            return []
        st = self._state(ctx["group_id"])
        d, _ = self._today(st)
        rows, total, comp_left = self._day_rows(st, d)
        zero_members = [r["qqid"] for r in rows if not r["recs"]]
        dist: dict = {}
        for r in rows:
            if r["recs"]:
                dist[r["fin"]] = dist.get(r["fin"], 0) + 1
        lines = [f"待出补偿刀数量：{comp_left:g}",
                 f"已出0刀的成员数量：{len(zero_members)}"]
        for i, qqid in enumerate(zero_members):
            lines.append(f"{'┖' if i == len(zero_members) - 1 else '┣'}{self._name_of(qqid)}")
        for num in sorted(dist):
            lines.append(f"已出{num:g}刀：{dist[num]}")
        lines.append(f"今天已出 {total:g}/{len(rows) * 3}")
        return ["\n".join(lines)]

    # ------------------------------------------------------------ 10 刀伤
    async def _h_my_blades(self, ctx, cmd):
        m = re.match(r"^刀伤 *(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        d, _ = self._today(st)
        qqid = int(m.group(1)) if m.group(1) else ctx["user_id"]
        knifes = self.store.day_records(gid, st.battle_id, d, qqid=qqid)
        num = 0
        rows = []
        for i in knifes:
            comp = bool(i["is_continue"])
            tail = i["boss_health_remain"] == 0 and not comp
            num += 0.5 if (tail or comp) else 1
            rows.append({"idx": f"{num:g}", "cycle": i["boss_cycle"], "boss": i["boss_num"],
                         "damage": i["challenge_damage"],
                         "kind": "tail" if tail else ("comp" if comp else "full"),
                         "behalf": self._name_of(i["behalf"]) if i["behalf"] else None})
        try:
            from . import images
            await images.ensure_avatars([qqid], timeout=3.0)
            from .pcr_time import pcr_date_str
            img = images.render_member_table(
                st.group_name or str(gid), pcr_date_str(d, st.game_server),
                self._name_of(qqid), qqid, rows, num)
            return [images.to_cq_image(img) + f"\n详细数据前往面板查看：\n{self._panel_url(gid)}"]
        except Exception:  # noqa: BLE001  渲染失败回退文本
            logger.exception("个人出刀表渲染失败")
            reply = f"{self._name_of(qqid)}的今日出刀：\n"
            for r in rows:
                kind = {"tail": "(尾刀)", "comp": "(补偿刀)", "full": ""}[r["kind"]]
                reply += (f"第{r['idx']}刀：{r['cycle']}周目{r['boss']}王 "
                          f"造成{r['damage']}伤害{kind}\n")
            if not rows:
                reply += "尚未出刀\n"
            elif num < 3:
                reply += f"您还差{3 - num:g}刀下班，加油！\n"
            else:
                reply += "今日出刀已完成！\n"
            return [reply + "详细数据请发送[查刀]"]

    # ------------------------------------------------------------ 战报（网页统计同源长图）
    async def _h_stats_report(self, ctx, cmd):
        if cmd not in ("战报", "统计"):
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        data = stats.build_statistics(self.store, self.cfg, st, "current", self._name_of)
        if not data["days"]:
            return ["本档案还没有出刀记录"]
        from . import images
        latest = data["days"][-1]["date"]
        title = f"{int(latest[5:7])}月会战战报"
        range_text = (f"{data['days'][0]['date']} ~ {latest}"
                      f" · {len(data['members'])} 名成员出刀")
        await images.ensure_avatars([m["qqid"] for m in data["members"]], timeout=4.0)
        img = images.render_stats_report(title, range_text, data,
                                         st.group_name or "")
        return [images.to_cq_image(img)]

    # ------------------------------------------------------------ 11 挂树
    async def _h_tree(self, ctx, cmd):
        m = re.match(r"挂树 *([1-5])? *(?:\[CQ:at,qq=(\d+)\])? *(?:[\:：](.*))? *$", cmd)
        if not m:
            return []
        gid, user_id = ctx["group_id"], ctx["user_id"]
        st = self._state(gid)
        d, _ = self._today(st)
        extra = (m.group(3) or "").strip() or None
        target = int(m.group(2)) if m.group(2) else user_id
        self._require_member(gid, target)
        ch = st.challengers.get(str(target))
        bn = m.group(1) or (ch.boss_num if ch else None)
        result = rules.put_on_the_tree(
            self.cfg, st, user_id, message=extra,
            boss_num=m.group(1) if m.group(1) else False,
            behalfed=target if m.group(2) else None,
            records_today=self.store.day_records(gid, st.battle_id, d, qqid=target),
            name_of=self._name_of)
        texts = [result.reply] + self._flush(st, result)
        self._emit(gid, result.reply)
        if bn:
            texts = await self._with_boss_card(st, bn, texts, keep_text=False)
        return texts

    # ------------------------------------------------------------ 12 申请出刀
    async def _h_apply(self, ctx, cmd):
        m = re.match(r"^(?:进|申请出刀)(| )([1-5]) *(补偿|补|b|bc|B|BC|Bc|bC)?"
                     r" *(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        gid, user_id = ctx["group_id"], ctx["user_id"]
        st = self._state(gid)
        d, _ = self._today(st)
        target = int(m.group(4)) if m.group(4) else user_id
        self._require_member(gid, target)
        result = rules.apply_for_challenge(
            self.cfg, st, target, m.group(2),
            is_continue=bool(m.group(3)),
            behalf=user_id if m.group(4) else None,
            records_today=self.store.day_records(gid, st.battle_id, d, qqid=target),
            name_of=self._name_of)
        self.store.save_guild_state(st)
        self._emit(gid, result.reply)
        return await self._with_boss_card(st, m.group(2), [result.reply], keep_text=False)

    # ------------------------------------------------------------ 13 取消
    async def _h_cancel(self, ctx, cmd):
        m = re.match(r"^取消 *([1-5]|申请出刀|申请|出刀|出刀all|报伤害|sl|预约)"
                     r" *([1-5])? *(?:\[CQ:at,qq=(\d+)\])? *$", cmd, re.IGNORECASE)
        if not m:
            return []
        gid = ctx["group_id"]
        user_id = int(m.group(3)) if m.group(3) else ctx["user_id"]
        self._require_member(gid, user_id)
        st = self._state(gid)
        b = m.group(1)
        if b in ("出刀", "申请", "申请出刀"):
            ch = st.challengers.get(str(user_id))
            bn = ch.boss_num if ch else None
            result = rules.cancel_blade(st, user_id)
            self.store.save_guild_state(st)
            self._emit(gid, result.reply)
            if bn:
                return await self._with_boss_card(st, bn, [result.reply], keep_text=False)
            return [result.reply]
        if b == "出刀all":
            result = rules.cancel_blade(st, user_id, cancel_type=0)
            self.store.save_guild_state(st)
            self._emit(gid, result.reply)
            return [result.reply]
        if b == "报伤害":
            reply = rules.report_hurt(st, user_id, 0, 0, clean=True).reply
            self.store.save_guild_state(st)
            self._emit(gid, reply)
            return [reply]
        if b.lower() == "sl":
            d, _ = self._today(st)
            member = self.store.member(gid, user_id)
            reply, slot, _ = rules.save_slot(st, user_id, d,
                                             member["last_save_slot"] if member else 0,
                                             clean_flag=True)
            if member:
                self.store.set_last_save_slot(gid, user_id, slot)
            return [reply]
        if b == "预约":
            result = rules.subscribe_cancel(st, m.group(2), user_id)
            self.store.save_guild_state(st)
            self._emit(gid, result.reply)
            return [result.reply]
        return []

    # ------------------------------------------------------------ 14 不打(了)
    async def _h_give_up(self, ctx, cmd):
        m = re.match(r"^不(?:打|进)了 *(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        gid = ctx["group_id"]
        user_id = int(m.group(1)) if m.group(1) else ctx["user_id"]
        self._require_member(gid, user_id)
        st = self._state(gid)
        ch = st.challengers.get(str(user_id))
        bn = ch.boss_num if ch else None
        result = rules.cancel_blade(st, user_id)
        self.store.save_guild_state(st)
        self._emit(gid, result.reply)
        if bn:
            return await self._with_boss_card(st, bn, [result.reply], keep_text=False)
        return [result.reply]

    # ------------------------------------------------------------ 15 面板
    async def _h_panel(self, ctx, cmd):
        if len(cmd) != 2:
            return []
        return [f"公会战面板：\n{self._panel_url(ctx['group_id'])}\n"
                f"建议添加到浏览器收藏夹或桌面快捷方式"]

    # ------------------------------------------------------------ 16 SL
    async def _h_save_slot(self, ctx, cmd):
        m = re.match(r"^sl *([\?？])? *(?:\[CQ:at,qq=(\d+)\])? *([\?？])? *$",
                     cmd, re.IGNORECASE)
        if not m:
            return []
        gid = ctx["group_id"]
        user_id = int(m.group(2)) if m.group(2) else ctx["user_id"]
        self._require_member(gid, user_id)
        st = self._state(gid)
        d, _ = self._today(st)
        only_check = bool(m.group(1) or m.group(3))
        member = self.store.member(gid, user_id)
        reply, slot, dropped = rules.save_slot(
            st, user_id, d, member["last_save_slot"] if member else 0,
            only_check=only_check)
        if not only_check:
            with self.store.batch():  # SL 记录与状态快照同事务
                if member is None:
                    self.store.upsert_member(gid, user_id)
                self.store.set_last_save_slot(gid, user_id, slot)
                self.store.save_guild_state(st)
            self._emit(gid, reply)
        return [reply]

    # ------------------------------------------------------------ 17 报伤害
    async def _h_report_hurt(self, ctx, cmd):
        m = re.match(r"^(?:打了|报伤害|更新出刀)*(?::|：)?(?:剩| |)"
                     r"(?:(\d+(?:s|S|秒))?(?:打了| |)(\d+)(?:w|W|万| |))? *(.*?)?"
                     r"(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        # 纯留言（如「更新打完了」）是既有习惯，不要求带数字
        if not m:
            return ["格式出错(O×O)\n如“更新出刀：200w 不想出”\n“报伤害 2s200w”\n“报伤害 3s300w@xxx”"]
        gid = ctx["group_id"]
        user_id = int(m.group(4)) if m.group(4) else ctx["user_id"]
        self._require_member(gid, user_id)
        st = self._state(gid)
        if not rules.find_challenger(st, user_id):
            return ["你都没申请出刀，报啥子伤害啊 (╯‵□′)╯︵┻━┻"]
        s = m.group(1) or 1
        if s != 1:
            s = int(re.sub(r"([a-z]|[A-Z]|秒)", "", s))
        hurt = int(m.group(2) or 0)
        comment = m.group(3) or None
        result = rules.report_hurt(st, user_id, int(s), hurt, comment=comment)
        self.store.save_guild_state(st)
        self._emit(gid, result.reply)
        return [result.reply]

    # ------------------------------------------------------------ 18 权限
    async def _h_grant_role(self, ctx, cmd):
        m = re.match(r"^权限 *(?:\[CQ:at,qq=(\d+)\])? *$", cmd)
        if not m:
            return []
        gid = ctx["group_id"]
        if m.group(1):
            if not self._is_admin_ctx(ctx):
                return ["只有管理员才可以申请权限"]
            qqid = int(m.group(1))
        else:
            qqid = ctx["user_id"]
        user = self.store.user_get_or_create(qqid)
        self.store.upsert_member(gid, qqid)
        if user["authority_group"] >= 10:
            new_auth = 100 if ctx.get("sender", {}).get("role") == "member" else 10
            self.store.set_authority(qqid, new_auth)
        return [f"[CQ:at,qq={qqid}]已成功申请权限"]

    # ------------------------------------------------------------ 20 重置进度
    async def _h_reset_progress(self, ctx, cmd):
        if cmd != "重置进度":
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        empty_bid = self._available_empty_battle_id(gid)
        if st.battle_id == empty_bid:
            return ["当前档案记录为空，无需重置"]
        old = st.battle_id
        with self.store.batch():  # 备份旧档案 / 切新档案 / 实时状态一次提交
            self.store.switch_archive(st, self.cfg, empty_bid)
            self.store.save_guild_state(st)
        reply = f"进度已重置\n档案编号： {old} -> {empty_bid}"
        self._emit(gid, reply)
        return [reply]

    def _available_empty_battle_id(self, gid: int) -> int:
        rows = self.store.db.execute(
            "SELECT bid FROM clan_challenge WHERE gid=? GROUP BY bid ORDER BY bid",
            (gid,)).fetchall()
        bids = [r["bid"] for r in rows]
        for i, b in enumerate(bids):
            if i != b:
                return i
        return len(bids)

    # ------------------------------------------------------------ 30 查树
    async def _h_query_tree(self, ctx, cmd):
        if len(cmd) != 2:
            return []
        m = re.match(r"^查(树|[1-5]) *$", cmd)
        if not m:
            return []
        gid = ctx["group_id"]
        st = self._state(gid)
        msg = m.group(1)
        if msg == "树":
            reply = ""
            found = False
            for boss in rules.BOSS_NUMS:
                on_tree = [(q, c.tree_msg) for q, c in st.challengers.items()
                           if c.boss_num == boss and c.tree]
                if on_tree:
                    found = True
                    reply += f"{boss}王挂树的成员：\n"
                    for q, m_ in on_tree:
                        reply += f"{self._name_of(int(q))}:{m_ or ''}\n"
            return [reply if found else "当前在任意Boss上无人挂树"]
        boss = msg
        lines = rules._challenge_lines(self.cfg, st, boss, self._name_of)
        on_tree = [(q, c.tree_msg) for q, c in st.challengers.items()
                   if c.boss_num == boss and c.tree]
        if on_tree:
            lines.append(f"{boss}王挂树的成员：")
            for q, m_ in on_tree:
                lines.append(f"{self._name_of(int(q))}:{m_ or ''}")
        else:
            lines.append(f"没有成员在{boss}王挂树")
        return ["\n".join(lines)]

    # ------------------------------------------------------------ 40 ver
    async def _h_version(self, ctx, cmd):
        if cmd.casefold() in ("ver", "v", "version"):
            return [VERSION]
        return []

    # ------------------------------------------------------------ 50/51/52 时间计算
    async def _h_combine_calc(self, ctx, cmd):
        args = cmd.split()[1:]
        if len(args) != 3:
            return [timecalc.COMBINE_USAGE]
        try:
            return [timecalc.combine_calc(*(float(x) for x in args))]
        except ValueError:
            return [timecalc.COMBINE_USAGE]

    async def _h_pad_calc(self, ctx, cmd):
        args = cmd.split()[1:]
        if len(args) != 4:
            return [timecalc.PAD_USAGE]
        try:
            return [timecalc.pad_calc(*(float(x) for x in args))]
        except ValueError:
            return [timecalc.PAD_USAGE]

    async def _h_comp_timeline(self, ctx, cmd):
        try:
            return [timecalc.comp_timeline(cmd.split()[1:])]
        except ValueError as e:
            return [str(e)]
