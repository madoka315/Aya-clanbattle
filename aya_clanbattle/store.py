"""数据层：SQLite（WAL）。

与 yobot_remix 的差异：
- 公会实时状态（血量/挑战者/预约）归一化成表，不再塞 JSON 文本字段；
- clan_challenge 列名与 remix 完全一致（含修正后的 boss_health_remain 拼写），
  便于把现库 80,662 条历史记录原样搬入；
- 档案切换保持 yobot 语义：整份实时状态快照进 guild_backup，切回即还原。
"""
import json
import sqlite3
from contextlib import contextmanager

from .gameconfig import GameConfig
from .state import BOSS_NUMS, Challenger, GuildState, fresh_state
from .rules import ChallengeRecord, RuleError

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild (
    group_id     INTEGER PRIMARY KEY,
    group_name   TEXT,
    game_server  TEXT NOT NULL DEFAULT 'cn',
    battle_id    INTEGER NOT NULL DEFAULT 0,
    boss_cycle   INTEGER NOT NULL DEFAULT 1,
    threshold    INTEGER NOT NULL DEFAULT 4000000,
    notification INTEGER NOT NULL DEFAULT 65535,
    privacy      INTEGER NOT NULL DEFAULT 2,
    apikey       TEXT
);
CREATE TABLE IF NOT EXISTS boss_health (
    group_id    INTEGER NOT NULL,
    boss_num    TEXT NOT NULL,
    now_health  INTEGER NOT NULL,
    next_health INTEGER NOT NULL,
    PRIMARY KEY (group_id, boss_num)
);
CREATE TABLE IF NOT EXISTS challenger (
    group_id    INTEGER NOT NULL,
    qqid        INTEGER NOT NULL,
    boss_num    TEXT NOT NULL,
    is_continue INTEGER NOT NULL DEFAULT 0,
    behalf      INTEGER,
    seconds     INTEGER NOT NULL DEFAULT 0,
    damage      INTEGER NOT NULL DEFAULT 0,
    tree        INTEGER NOT NULL DEFAULT 0,
    tree_msg    TEXT,
    comment     TEXT,
    started_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, qqid)
);
CREATE TABLE IF NOT EXISTS subscribe (
    group_id  INTEGER NOT NULL,
    battle_id INTEGER NOT NULL DEFAULT 0,
    boss_num  TEXT NOT NULL,
    qqid      INTEGER NOT NULL,
    message   TEXT,
    PRIMARY KEY (group_id, battle_id, boss_num, qqid)
);
CREATE TABLE IF NOT EXISTS clan_challenge (
    cid                INTEGER PRIMARY KEY AUTOINCREMENT,
    bid                INTEGER NOT NULL DEFAULT 0,
    gid                INTEGER NOT NULL,
    qqid               INTEGER NOT NULL,
    challenge_pcrdate  INTEGER NOT NULL,
    challenge_pcrtime  INTEGER NOT NULL,
    boss_cycle         INTEGER NOT NULL,
    boss_num           TEXT NOT NULL,
    boss_health_remain INTEGER NOT NULL,
    challenge_damage   INTEGER NOT NULL,
    is_continue        INTEGER NOT NULL DEFAULT 0,
    message            TEXT,
    behalf             INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cc_gid_bid ON clan_challenge (gid, bid);
CREATE INDEX IF NOT EXISTS idx_cc_qq_date ON clan_challenge (qqid, challenge_pcrdate);
CREATE INDEX IF NOT EXISTS idx_cc_day ON clan_challenge (gid, bid, challenge_pcrdate);
CREATE TABLE IF NOT EXISTS member (
    group_id       INTEGER NOT NULL,
    qqid           INTEGER NOT NULL,
    role           INTEGER NOT NULL DEFAULT 100,
    last_save_slot INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, qqid)
);
CREATE TABLE IF NOT EXISTS user (
    qqid                 INTEGER PRIMARY KEY,
    nickname             TEXT,
    authority_group      INTEGER NOT NULL DEFAULT 100,
    password             TEXT,
    salt                 TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    privacy              INTEGER NOT NULL DEFAULT 5,
    login_code           TEXT,
    login_code_available INTEGER NOT NULL DEFAULT 0,
    login_code_expire    INTEGER NOT NULL DEFAULT 0,
    clan_group_id        INTEGER,
    deleted              INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_login (
    qqid        INTEGER NOT NULL,
    auth_cookie TEXT NOT NULL,
    expire_time INTEGER NOT NULL DEFAULT 0,
    last_time   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (qqid, auth_cookie)
);
CREATE TABLE IF NOT EXISTS guild_backup (
    group_id  INTEGER NOT NULL,
    battle_id INTEGER NOT NULL,
    snapshot  TEXT NOT NULL,
    PRIMARY KEY (group_id, battle_id)
);
CREATE TABLE IF NOT EXISTS web_session (
    sid        TEXT PRIMARY KEY,
    qqid       INTEGER NOT NULL,
    csrf_token TEXT NOT NULL,
    created    INTEGER NOT NULL,
    expire     INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._tx_depth = 0  # >0 表示处于 batch() 事务中，内部写操作不再各自提交

    def close(self):
        self.db.close()

    # ------------------------------------------------------------ 事务
    @contextmanager
    def batch(self):
        """把多条写操作合并到同一事务：出刀日志与状态快照要么同时生效、要么同时回滚。
        嵌套调用复用最外层事务。"""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self._tx_depth = 1
        try:
            yield
        except BaseException:
            self._tx_depth = 0
            self.db.rollback()
            raise
        self._tx_depth = 0
        self.db.commit()

    def _commit(self):
        """batch() 内的中间写不提交，由最外层统一提交/回滚。"""
        if not self._tx_depth:
            self.db.commit()

    # ------------------------------------------------------------ 公会状态
    def get_guild(self, gid: int) -> GuildState | None:
        row = self.db.execute("SELECT * FROM guild WHERE group_id=?", (gid,)).fetchone()
        if row is None:
            return None
        st = GuildState(
            group_id=gid, group_name=row["group_name"], battle_id=row["battle_id"],
            game_server=row["game_server"], boss_cycle=row["boss_cycle"],
            threshold=row["threshold"], notification=row["notification"],
            privacy=row["privacy"], apikey=row["apikey"])
        for r in self.db.execute(
                "SELECT * FROM boss_health WHERE group_id=?", (gid,)):
            st.now[r["boss_num"]] = r["now_health"]
            st.nxt[r["boss_num"]] = r["next_health"]
        for r in self.db.execute(
                "SELECT * FROM challenger WHERE group_id=? ORDER BY started_at, qqid", (gid,)):
            st.challengers[str(r["qqid"])] = Challenger(
                qqid=r["qqid"], boss_num=r["boss_num"],
                is_continue=bool(r["is_continue"]), behalf=r["behalf"],
                seconds=r["seconds"], damage=r["damage"], tree=bool(r["tree"]),
                tree_msg=r["tree_msg"], comment=r["comment"],
                started_at=r["started_at"])
        for r in self.db.execute(
                "SELECT * FROM subscribe WHERE group_id=?", (gid,)):
            st.subscribes.setdefault(r["boss_num"], {})[r["qqid"]] = r["message"]
        return st

    def save_guild_state(self, st: GuildState) -> None:
        # batch() 内只写不提交，保证与出刀日志同事务
        if self._tx_depth:
            self._save_state_rows(st)
        else:
            with self.db:
                self._save_state_rows(st)

    def _save_state_rows(self, st: GuildState) -> None:
        db = self.db
        db.execute(
            "INSERT INTO guild (group_id, group_name, game_server, battle_id, boss_cycle,"
            " threshold, notification, privacy, apikey) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(group_id) DO UPDATE SET group_name=?, game_server=?,"
            " battle_id=?, boss_cycle=?, threshold=?, notification=?, privacy=?, apikey=?",
            (st.group_id, st.group_name, st.game_server, st.battle_id, st.boss_cycle,
             st.threshold, st.notification, st.privacy, st.apikey,
             st.group_name, st.game_server, st.battle_id, st.boss_cycle,
             st.threshold, st.notification, st.privacy, st.apikey))
        db.execute("DELETE FROM boss_health WHERE group_id=?", (st.group_id,))
        db.executemany(
            "INSERT INTO boss_health (group_id, boss_num, now_health, next_health)"
            " VALUES (?,?,?,?)",
            [(st.group_id, b, st.now[b], st.nxt[b]) for b in BOSS_NUMS])
        db.execute("DELETE FROM challenger WHERE group_id=?", (st.group_id,))
        db.executemany(
            "INSERT INTO challenger (group_id, qqid, boss_num, is_continue, behalf,"
            " seconds, damage, tree, tree_msg, comment, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(st.group_id, c.qqid, c.boss_num, int(c.is_continue), c.behalf,
              c.seconds, c.damage, int(c.tree), c.tree_msg, c.comment, c.started_at)
             for c in st.challengers.values()])
        db.execute("DELETE FROM subscribe WHERE group_id=?", (st.group_id,))
        db.executemany(
            "INSERT INTO subscribe (group_id, battle_id, boss_num, qqid, message)"
            " VALUES (?,?,?,?,?)",
            [(st.group_id, st.battle_id, b, q, m)
             for b, subs in st.subscribes.items() for q, m in subs.items()])

    def create_guild(self, gid: int, cfg: GameConfig, server: str = "cn",
                     group_name: str | None = None) -> GuildState:
        st = fresh_state(gid, cfg, server, group_name=group_name)
        self.save_guild_state(st)
        return st

    # ------------------------------------------------------------ 档案系统
    def _snapshot(self, st: GuildState) -> dict:
        return {
            "game_server": st.game_server,  # 档案绑定服务器：跨服还原会拿错血量表
            "boss_cycle": st.boss_cycle,
            "now": dict(st.now), "nxt": dict(st.nxt),
            "challengers": [{"qqid": c.qqid, "boss_num": c.boss_num,
                             "is_continue": c.is_continue, "behalf": c.behalf,
                             "seconds": c.seconds, "damage": c.damage,
                             "tree": c.tree, "tree_msg": c.tree_msg,
                             "comment": c.comment, "started_at": c.started_at}
                            for c in st.challengers.values()],
            "subscribes": {b: dict(s) for b, s in st.subscribes.items()},
        }

    def _restore_snapshot(self, st: GuildState, snap: dict) -> None:
        st.boss_cycle = snap["boss_cycle"]
        st.now = dict(snap["now"])
        st.nxt = dict(snap["nxt"])
        st.challengers = {str(c["qqid"]): Challenger(**c) for c in snap["challengers"]}
        # JSON 对象的键只能是字符串，往返后预约的 QQ 键会由 int 变 str；
        # 不归一化会导致取消误报"还没有预约"、再预约出现 int/str 两份并撞 UNIQUE
        st.subscribes = {str(b): {int(q): m for q, m in s.items()}
                         for b, s in snap["subscribes"].items()}

    def switch_archive(self, st: GuildState, cfg: GameConfig, new_bid: int,
                       server: str | None = None) -> None:
        """yobot 语义：备份当前状态到 (gid, 当前bid)，还原 (gid, new_bid) 的备份或新建。
        档案绑定服务器：拒绝跨服还原；server 参数用于"切服时新建档案"
        （旧档案按旧服备份，新档案按新服满血初始化）。"""
        target_server = server or st.game_server
        row = self.db.execute(
            "SELECT snapshot FROM guild_backup WHERE group_id=? AND battle_id=?",
            (st.group_id, new_bid)).fetchone()
        snap = json.loads(row["snapshot"]) if row else None
        if snap is not None:
            snap_server = snap.get("game_server", st.game_server)
            if snap_server != target_server:
                raise RuleError(f"目标档案属于 {snap_server.upper()} 服，"
                                "请先切换到对应服务器再切档")
        old_bid = st.battle_id
        self.db.execute(
            "INSERT INTO guild_backup (group_id, battle_id, snapshot) VALUES (?,?,?)"
            " ON CONFLICT(group_id, battle_id) DO UPDATE SET snapshot=?",
            (st.group_id, old_bid, json.dumps(self._snapshot(st), ensure_ascii=False),
             json.dumps(self._snapshot(st), ensure_ascii=False)))
        st.battle_id = new_bid
        st.game_server = target_server
        if snap is not None:
            self._restore_snapshot(st, snap)
        else:
            fresh = fresh_state(st.group_id, cfg, target_server,
                                battle_id=new_bid, cycle=1)
            st.boss_cycle, st.now, st.nxt = 1, fresh.now, fresh.nxt
            st.challengers.clear()
            st.subscribes.clear()
        self._commit()

    def archive_record_counts(self, gid: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT bid, COUNT(*) AS n FROM clan_challenge WHERE gid=? GROUP BY bid ORDER BY bid",
            (gid,)).fetchall()
        return [{"battle_id": r["bid"], "record_count": r["n"]} for r in rows]

    def clear_archive(self, gid: int, bid: int) -> None:
        self.db.execute("DELETE FROM clan_challenge WHERE gid=? AND bid=?", (gid, bid))
        self.db.commit()

    # ------------------------------------------------------------ 出刀日志
    def insert_record(self, st: GuildState, rec: ChallengeRecord,
                      pcrdate: int, pcrtime: int) -> int:
        cur = self.db.execute(
            "INSERT INTO clan_challenge (bid, gid, qqid, challenge_pcrdate, challenge_pcrtime,"
            " boss_cycle, boss_num, boss_health_remain, challenge_damage, is_continue,"
            " message, behalf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (st.battle_id, st.group_id, rec.qqid, pcrdate, pcrtime, rec.boss_cycle,
             rec.boss_num, rec.health_remain, rec.damage, int(rec.is_continue),
             rec.message, rec.behalf))
        self._commit()
        return cur.lastrowid

    def day_records(self, gid: int, bid: int, pcrdate: int,
                    qqid: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM clan_challenge WHERE gid=? AND bid=? AND challenge_pcrdate=?")
        args = [gid, bid, pcrdate]
        if qqid is not None:
            sql += " AND qqid=?"
            args.append(qqid)
        sql += " ORDER BY cid"
        return [dict(r) for r in self.db.execute(sql, args)]

    def day_record_count(self, gid: int, bid: int, pcrdate: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM clan_challenge WHERE gid=? AND bid=? AND challenge_pcrdate=?",
            (gid, bid, pcrdate)).fetchone()[0]

    def last_record(self, gid: int, bid: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM clan_challenge WHERE gid=? AND bid=? ORDER BY cid DESC LIMIT 1",
            (gid, bid)).fetchone()
        return dict(row) if row else None

    def delete_record(self, cid: int) -> None:
        self.db.execute("DELETE FROM clan_challenge WHERE cid=?", (cid,))
        self._commit()

    # ------------------------------------------------------------ 成员与用户
    def is_member(self, gid: int, qqid: int) -> bool:
        return self.db.execute("SELECT 1 FROM member WHERE group_id=? AND qqid=?",
                               (gid, qqid)).fetchone() is not None

    def member(self, gid: int, qqid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM member WHERE group_id=? AND qqid=?",
                               (gid, qqid)).fetchone()

    def upsert_member(self, gid: int, qqid: int, role: int = 100) -> None:
        self.db.execute(
            "INSERT INTO member (group_id, qqid, role) VALUES (?,?,?)"
            " ON CONFLICT(group_id, qqid) DO UPDATE SET role=?",
            (gid, qqid, role, role))
        self._commit()

    def drop_members(self, gid: int, qqids: list[int]) -> int:
        cur = self.db.executemany(
            "DELETE FROM member WHERE group_id=? AND qqid=?",
            [(gid, q) for q in qqids])
        self.db.commit()
        return cur.rowcount

    def members(self, gid: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT m.qqid, u.nickname, m.role, m.last_save_slot FROM member m"
            " LEFT JOIN user u ON u.qqid = m.qqid WHERE m.group_id=? ORDER BY m.qqid",
            (gid,)).fetchall()
        return [dict(r) for r in rows]

    def set_last_save_slot(self, gid: int, qqid: int, value: int) -> None:
        self.db.execute(
            "UPDATE member SET last_save_slot=? WHERE group_id=? AND qqid=?",
            (value, gid, qqid))
        self._commit()

    def set_authority(self, qqid: int, value: int) -> None:
        self.db.execute("UPDATE user SET authority_group=? WHERE qqid=?",
                        (value, qqid))
        self.db.commit()

    # ------------------------------------------------------------ 密码/登录码/会话
    def set_password(self, qqid: int, plain: str, must_change: bool = False,
                     kick: bool = False) -> None:
        """密码 = sha256(明文 + 用户盐)。盐缺失时生成。"""
        import hashlib
        row = self.db.execute("SELECT salt FROM user WHERE qqid=?", (qqid,)).fetchone()
        salt = row["salt"] if row and row["salt"] else self._rand(16)
        self.db.execute(
            "UPDATE user SET salt=?, password=?, must_change_password=?, privacy=0"
            " WHERE qqid=?",
            (salt, hashlib.sha256((plain + salt).encode()).hexdigest(),
             int(must_change), qqid))
        if kick:
            self.db.execute("DELETE FROM web_session WHERE qqid=?", (qqid,))
        self.db.commit()

    def verify_password(self, qqid: int, plain: str) -> bool:
        import hashlib
        row = self.db.execute("SELECT salt, password, privacy FROM user WHERE qqid=?",
                              (qqid,)).fetchone()
        if row is None or not row["password"] or not row["salt"]:
            return False
        if row["privacy"] >= 5:
            return False  # 锁定
        if hashlib.sha256((plain + row["salt"]).encode()).hexdigest() == row["password"]:
            if row["privacy"]:
                self.db.execute("UPDATE user SET privacy=0 WHERE qqid=?", (qqid,))
                self.db.commit()
            return True
        self.db.execute("UPDATE user SET privacy=privacy+1 WHERE qqid=?", (qqid,))
        self.db.commit()
        return False

    def is_locked(self, qqid: int) -> bool:
        row = self.db.execute("SELECT privacy FROM user WHERE qqid=?", (qqid,)).fetchone()
        return bool(row and row["privacy"] >= 5)

    def set_login_code(self, qqid: int, code: str, ttl: int = 60) -> None:
        self.db.execute(
            "UPDATE user SET login_code=?, login_code_available=1, login_code_expire=?"
            " WHERE qqid=?", (code, int(self._now()) + ttl, qqid))
        self.db.commit()

    def check_login_code(self, qqid: int, code: str) -> bool:
        row = self.db.execute(
            "SELECT login_code, login_code_available, login_code_expire FROM user WHERE qqid=?",
            (qqid,)).fetchone()
        if row is None or not row["login_code_available"]:
            return False
        if row["login_code"] != code or row["login_code_expire"] < self._now():
            return False
        self.db.execute("UPDATE user SET login_code_available=0 WHERE qqid=?", (qqid,))
        self.db.commit()
        return True

    def create_session(self, qqid: int, days: int = 7) -> tuple[str, str]:
        import time as _time
        sid, csrf = self._rand(32), self._rand(16)
        now = int(_time.time())
        self.db.execute(
            "INSERT INTO web_session (sid, qqid, csrf_token, created, expire) VALUES (?,?,?,?,?)",
            (sid, qqid, csrf, now, now + days * 86400))
        self.db.commit()
        return sid, csrf

    def get_session(self, sid: str):
        if not sid:
            return None
        row = self.db.execute("SELECT * FROM web_session WHERE sid=?", (sid,)).fetchone()
        if row is None or row["expire"] < self._now():
            return None
        return row

    def drop_session(self, sid: str) -> None:
        self.db.execute("DELETE FROM web_session WHERE sid=?", (sid,))
        self.db.commit()

    def drop_user_sessions(self, qqid: int) -> None:
        self.db.execute("DELETE FROM web_session WHERE qqid=?", (qqid,))
        self.db.commit()

    def cleanup_expired_sessions(self) -> None:
        self.db.execute("DELETE FROM web_session WHERE expire < ?", (self._now(),))
        self.db.commit()

    @staticmethod
    def _now() -> int:
        import time as _time
        return int(_time.time())

    @staticmethod
    def _rand(n: int) -> str:
        import random
        import string
        chars = string.ascii_uppercase + string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(n))

    def records_of_member(self, gid: int, bid: int, qqid: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM clan_challenge WHERE gid=? AND bid=? AND qqid=?"
            " ORDER BY challenge_pcrdate, cid", (gid, bid, qqid)).fetchall()
        return [dict(r) for r in rows]

    def user_get_or_create(self, qqid: int, nickname: str | None = None) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM user WHERE qqid=?", (qqid,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO user (qqid, nickname) VALUES (?,?)",
                            (qqid, nickname))
            self.db.commit()
            row = self.db.execute("SELECT * FROM user WHERE qqid=?", (qqid,)).fetchone()
        elif nickname and row["nickname"] != nickname:
            self.db.execute("UPDATE user SET nickname=? WHERE qqid=?", (nickname, qqid))
            self.db.commit()
            row = self.db.execute("SELECT * FROM user WHERE qqid=?", (qqid,)).fetchone()
        return row

    def user(self, qqid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM user WHERE qqid=?", (qqid,)).fetchone()

    def nickname_of(self, qqid: int) -> str:
        row = self.db.execute("SELECT nickname FROM user WHERE qqid=?", (qqid,)).fetchone()
        return (row["nickname"] if row and row["nickname"] else None) or str(qqid)
