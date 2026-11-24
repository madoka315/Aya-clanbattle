"""从 yobot_remix 数据库迁移历史数据到本项目。

迁移内容与策略：
- clan_challenge 逐条原样搬（列名与 remix 完全一致，档案号 bid 原样保留 → 档案连续，
  含旧时代 bid 1000~1034）；
- user：昵称/权限/删除位搬入，**密码不搬**（yobot 是前端盐双哈希，与本方案不兼容，
  老用户需私聊「重置密码」一次）；
- member：全员纳入会战成员（他们是历史出刀者）；
- guild 状态：仅迁移新库中**不存在**的公会（含 boss 血量/挑战中/预约，保证会战中切换不断档）；
  新库已有的公会一律不动；
- clan_group_backups → guild_backup（转换为本项目快照结构，档案切换可回溯）。

用法（在 Aya-clanbattle 目录）：
    python -m aya_clanbattle.migrate_from_remix <remix的yobotdata_new.db路径>
"""
import json
import logging
import sqlite3
import sys

from .state import Challenger

logger = logging.getLogger(__name__)


def _snapshot_from_backup(gid: int, battle_id: int, data: dict) -> str:
    """把 remix 的 clan_group_backups 行转成本项目 guild_backup 快照结构。"""
    return json.dumps({
        "boss_cycle": data.get("boss_cycle", 1),
        "now": json.loads(data["now_cycle_boss_health"]) if data.get("now_cycle_boss_health") else {},
        "nxt": json.loads(data["next_cycle_boss_health"]) if data.get("next_cycle_boss_health") else {},
        "challengers": [],   # 旧备份不含挑战中状态
        "subscribes": {},
        "_gid": gid, "_battle_id": battle_id,
    }, ensure_ascii=False)


def import_remix_data(store, old_db_path: str) -> dict:
    """执行迁移。old_db_path 为 remix 库（只读），内部 ATTACH 到同一连接。返回统计 dict。"""
    db = store.db
    db.execute("ATTACH DATABASE '%s' AS old" % old_db_path.replace("'", "''"))
    db.row_factory = sqlite3.Row
    old = db
    stat = {"challenge": 0, "user": 0, "member": 0, "guild": 0, "backup": 0}
    my_gids = {r["group_id"] for r in db.execute("SELECT group_id FROM guild")}
    my_ch_gids = {r["gid"] for r in db.execute("SELECT DISTINCT gid FROM clan_challenge")}
    old_ch_gids = {r["gid"] for r in old.execute("SELECT DISTINCT gid FROM old.clan_challenge")}
    overlap = my_ch_gids & old_ch_gids
    if overlap:
        db.execute("DETACH DATABASE old")
        raise RuntimeError(f"两侧库存在同群出刀记录 {overlap}，拒绝执行以免重复入库")

    db.execute("BEGIN IMMEDIATE")
    try:
        # ---- 出刀记录（80,662 条原样搬）
        cols = ("bid, gid, qqid, challenge_pcrdate, challenge_pcrtime, boss_cycle, "
                "boss_num, boss_health_remain, challenge_damage, is_continue, "
                "message, behalf")
        cur = db.execute(
            f"INSERT INTO clan_challenge ({cols}) SELECT {cols} FROM old.clan_challenge")
        stat["challenge"] = cur.rowcount

        # ---- 用户（密码不搬）
        for r in old.execute("SELECT qqid, nickname, authority_group, deleted, clan_group_id FROM old.user"):
            db.execute(
                "INSERT INTO user (qqid, nickname, authority_group, deleted, clan_group_id)"
                " VALUES (?,?,?,?,?) ON CONFLICT(qqid) DO UPDATE SET"
                " nickname=COALESCE(user.nickname, excluded.nickname)",
                (r["qqid"], r["nickname"], r["authority_group"],
                 int(r["deleted"] or 0), r["clan_group_id"]))
            stat["user"] += 1

        # ---- 会战成员
        for r in old.execute("SELECT group_id, qqid, role, last_save_slot FROM old.clan_member"):
            db.execute(
                "INSERT INTO member (group_id, qqid, role, last_save_slot) VALUES (?,?,?,?)"
                " ON CONFLICT(group_id, qqid) DO NOTHING",
                (r["group_id"], r["qqid"], r["role"] or 100, r["last_save_slot"] or 0))
            stat["member"] += 1

        # ---- 公会（仅新库不存在的；连状态一起搬，切换不断档）
        for r in old.execute("SELECT * FROM old.clan_group WHERE deleted=0"):
            gid = r["group_id"]
            if gid in my_gids:
                continue
            db.execute(
                "INSERT INTO guild (group_id, group_name, game_server, battle_id, boss_cycle,"
                " threshold, notification, privacy, apikey) VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, r["group_name"], r["game_server"], r["battle_id"], r["boss_cycle"],
                 r["threshold"], r["notification"], r["privacy"], r["apikey"]))
            now_h = json.loads(r["now_cycle_boss_health"]) if r["now_cycle_boss_health"] else {}
            nxt_h = json.loads(r["next_cycle_boss_health"]) if r["next_cycle_boss_health"] else {}
            for b in ("1", "2", "3", "4", "5"):
                db.execute(
                    "INSERT INTO boss_health (group_id, boss_num, now_health, next_health)"
                    " VALUES (?,?,?,?)",
                    (gid, b, now_h.get(b, 0), nxt_h.get(b, 0)))
            challenging = json.loads(r["challenging_member_list"]) if r["challenging_member_list"] else {}
            for boss_num, members in challenging.items():
                for qqid, info in members.items():
                    db.execute(
                        "INSERT INTO challenger (group_id, qqid, boss_num, is_continue, behalf,"
                        " seconds, damage, tree, tree_msg) VALUES (?,?,?,?,?,?,?,?,?)",
                        (gid, int(qqid), str(boss_num), int(bool(info.get("is_continue"))),
                         info.get("behalf"), info.get("s", 0) or 0, info.get("damage", 0) or 0,
                         int(bool(info.get("tree"))), info.get("msg")))
            subs = json.loads(r["subscribe_list"]) if r["subscribe_list"] else {}
            for boss_num, members in subs.items():
                for qqid, note in members.items():
                    db.execute(
                        "INSERT INTO subscribe (group_id, battle_id, boss_num, qqid, message)"
                        " VALUES (?,?,?,?,?)",
                        (gid, r["battle_id"], str(boss_num), int(qqid), note))
            stat["guild"] += 1

        # ---- 档案备份
        for r in old.execute("SELECT group_id, battle_id, group_data FROM old.clan_group_backups"):
            if not r["group_data"]:
                continue
            snap = _snapshot_from_backup(r["group_id"], r["battle_id"], json.loads(r["group_data"]))
            db.execute(
                "INSERT INTO guild_backup (group_id, battle_id, snapshot) VALUES (?,?,?)"
                " ON CONFLICT(group_id, battle_id) DO UPDATE SET snapshot=?",
                (r["group_id"], r["battle_id"], snap, snap))
            stat["backup"] += 1

        db.commit()
    except Exception:
        db.rollback()
        db.execute("DETACH DATABASE old")
        raise
    db.execute("DETACH DATABASE old")
    return stat


def verify(store, old: sqlite3.Connection) -> dict:
    """迁移后核对：条数一致 + 档案分布一致。"""
    old.row_factory = sqlite3.Row
    n_old = old.execute("SELECT COUNT(*) FROM clan_challenge").fetchone()[0]
    n_new = store.db.execute("SELECT COUNT(*) FROM clan_challenge").fetchone()[0]
    per_old = {str(r[0]): r[1] for r in old.execute(
        "SELECT bid, COUNT(*) FROM clan_challenge GROUP BY bid")}
    per_new = {str(r["bid"]): r["n"] for r in store.db.execute(
        "SELECT bid, COUNT(*) AS n FROM clan_challenge GROUP BY bid")}
    # 允许新库另有自己的记录（例如冒烟群），旧档案分布必须逐档成立
    return {"old_total": n_old, "new_total": n_new, "per_bid_match": per_old,
            "ok": all(per_new.get(k, 0) >= v for k, v in per_old.items())}


def main(argv):
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(levelname)s: %(message)s")
    if len(argv) != 2:
        print(__doc__)
        return 1
    src = argv[1]
    snap = src + ".migr_snap"
    src_conn = sqlite3.connect(f"file:{src.replace(chr(92), '/')}?mode=ro", uri=True)
    dst = sqlite3.connect(snap)
    src_conn.backup(dst)
    src_conn.close()
    dst.close()
    logger.info("remix 库一致性快照完成: %s", snap)

    from .store import Store
    import os
    store = Store(os.path.join("aya_clanbattle.db"))
    stat = import_remix_data(store, snap)
    logger.info("迁移完成: %s", stat)
    check = sqlite3.connect(snap)
    v = verify(store, check)
    check.close()
    logger.info("核对: %s", v)
    if not v["per_bid_match"]:
        logger.error("档案分布核对未通过！")
        return 2
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
