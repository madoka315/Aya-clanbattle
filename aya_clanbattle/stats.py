"""统计聚合：网页统计页与群聊「战报」共用同一份数据口径。

从 webapp._act_get_statistics 抽出，避免两处算法漂移。
"""
from .gameconfig import load_boss_id_name
from .pcr_time import pcr_date_str


def build_statistics(store, cfg, st, battle_id="current", name_of=str) -> dict:
    """按档案（current/all/具体 bid）聚合每日趋势、成员榜、各王主力、时刻分布与下班榜。"""
    conds, args = ["gid=?"], [st.group_id]
    if battle_id not in (None, "current", "all"):
        conds.append("bid=?"); args.append(int(battle_id))
    elif battle_id == "current":
        conds.append("bid=?"); args.append(st.battle_id)
    rows = store.db.execute(
        f"SELECT * FROM clan_challenge WHERE {' AND '.join(conds)} ORDER BY cid", args).fetchall()
    bosses_names = load_boss_id_name()
    days: dict = {}
    members: dict = {}
    bosses: dict = {}
    hours = [0] * 24        # 出刀时刻分布（本地时刻，5 点换日对齐）
    day_blades = {}         # (qqid, pcrdate) -> 当日记录（时序，算完成时刻）
    boss_members = {}       # (boss, qqid) -> 出刀次数（各王主力）
    boss_full = {}          # (boss, qqid) -> [完整刀伤害和, 完整刀数]（刀均伤害榜）
    for r in rows:
        ds = pcr_date_str(r["challenge_pcrdate"], st.game_server)
        d = days.setdefault(ds, {"blades": 0, "damage": 0})
        d["blades"] += 1
        d["damage"] += r["challenge_damage"]
        m = members.setdefault(r["qqid"], {"blades": 0, "damage": 0, "tails": 0})
        m["blades"] += 1
        m["damage"] += r["challenge_damage"]
        if r["boss_health_remain"] == 0 and not r["is_continue"]:
            m["tails"] += 1
        b = bosses.setdefault(str(r["boss_num"]), {"damage": 0, "count": 0})
        b["damage"] += r["challenge_damage"]
        b["count"] += 1
        hours[(5 + r["challenge_pcrtime"] // 3600) % 24] += 1
        day_blades.setdefault((r["qqid"], r["challenge_pcrdate"]), []).append(r)
        bk = (str(r["boss_num"]), r["qqid"])
        boss_members[bk] = boss_members.get(bk, 0) + 1
        # 刀均伤害只统计完整刀（尾刀 remain=0、补偿刀 is_continue=1 都不计）
        if not r["is_continue"] and r["boss_health_remain"] > 0:
            fu = boss_full.setdefault(bk, [0, 0])
            fu[0] += r["challenge_damage"]
            fu[1] += 1
    # 最速下班榜：出满 3 刀（收尾/补偿 0.5 加权）的完成时刻，最早完成靠前
    fastest = []
    done_by_m: dict = {}
    for (q, _d), recs in day_blades.items():
        w = 0.0
        for rec in recs:
            w = round(w + (0.5 if (rec["is_continue"] or rec["boss_health_remain"] == 0) else 1.0), 1)
            if w >= 3:
                done_by_m.setdefault(q, []).append(rec["challenge_pcrtime"])
                break
    for q, ts in done_by_m.items():
        fastest.append({"qqid": q, "name": name_of(q),
                        "avg": sum(ts) / len(ts), "days": len(ts), "best": min(ts)})
    fastest.sort(key=lambda x: x["avg"])
    slowest = [{"qqid": q, "name": name_of(q),
                "avg": sum(ts) / len(ts), "days": len(ts), "worst": max(ts)}
               for q, ts in done_by_m.items()]
    slowest.sort(key=lambda x: x["avg"], reverse=True)
    boss_list = []
    for k in sorted(bosses, key=int):
        v = bosses[k]
        icon = cfg.icon_id(st.game_server, int(k) - 1)
        tops = sorted(((q, n) for (bb, q), n in boss_members.items() if bb == k),
                      key=lambda x: x[1], reverse=True)[:3]
        avg_tops = sorted(
            ((q, total / n, n) for (bb, q), (total, n) in boss_full.items()
             if bb == k and n > 0),
            key=lambda x: (-x[1], -x[2], x[0]))[:3]
        boss_list.append({"boss": k, **v,
                          "name": bosses_names.get(k, {}).get(icon, icon),
                          "icon_id": icon,
                          "top": [{"qqid": q, "name": name_of(q), "count": n}
                                  for q, n in tops],
                          "top_avg": [{"qqid": q, "name": name_of(q),
                                       "avg": round(avg), "count": n}
                                      for q, avg, n in avg_tops]})
    return {"battle_id": battle_id,
            "days": [{"date": k, **v} for k, v in sorted(days.items())],
            "members": [{"qqid": q, "name": name_of(q), **v}
                        for q, v in members.items()],
            "bosses": boss_list,
            "hours": hours,
            "fastest": fastest[:10],
            "slowest": slowest[:10]}
