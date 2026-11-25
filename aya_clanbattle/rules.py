"""公会战规则纯函数层。

语义 1:1 移植自 yobot_remix realize.py（三刀/补偿/两周期窗口/周目推进/挂树/SL/档案），
但不做任何 IO：状态在 GuildState 上原地修改，出刀日志与群通知以返回值交给调用方。
- 调用方需注入：today/targetday 出刀记录、成员身份、昵称解析、当前 PCR 日期。
- 所有回复文案保持与 yobot_remix 一致（用户群已习惯）。

规则函数约定：
    返回 RuleResult(reply, records, notices)
    records: ChallengeRecord 列表，由调用方写库（含 gid/bid 由调用方补）
    notices: ('tree_down', boss_num) / ('subscribe_remind', boss_num)，
             由调用方按 kind 生成文本并发送（subscribe_remind 的清理由
             caller 调 clear_subscribes() 完成）
"""
from dataclasses import dataclass, field
from typing import Callable

from .gameconfig import GameConfig
from .state import BOSS_NUMS, Challenger, GuildState
from .pcr_time import pcr_datetime


# ---------------------------------------------------------------- 异常
class RuleError(Exception):
    """规则拒绝，message 即给用户的回复。"""


class UserError(RuleError):
    pass


class GroupError(RuleError):
    pass


class InputError(RuleError):
    pass


# ---------------------------------------------------------------- 结果与日志
@dataclass
class ChallengeRecord:
    """一刀的日志（调用方写库，补 gid/bid/日期时间）。"""
    qqid: int
    boss_cycle: int
    boss_num: str
    health_remain: int
    damage: int
    is_continue: bool
    message: str | None = None
    behalf: int | None = None


@dataclass
class RuleResult:
    reply: str = ""
    records: list[ChallengeRecord] = field(default_factory=list)
    notices: list[tuple] = field(default_factory=list)
    pcrdate: int | None = None   # challenge() 回填：本刀实际落库的 PCR 日期（昨日报刀时已前移）
    pcrtime: int | None = None


# ---------------------------------------------------------------- 内部工具
def _stat_rows(challenges: list[dict]) -> list[dict]:
    """兼容两种记录形态：rules 内部键(health_remain)与 store 行键(boss_health_remain)。"""
    out = []
    for c in challenges:
        if "health_remain" not in c and "boss_health_remain" in c:
            out.append({"health_remain": c["boss_health_remain"],
                        "is_continue": bool(c["is_continue"])})
        else:
            out.append(c)
    return out


def _blade_stats(challenges: list[dict]) -> tuple[int, int, int, int]:
    """(完整刀数, 尾刀数, 已用补偿数, 剩余补偿数)。
    与 yobot 一致：收尾刀(非补偿)不算完整刀，补偿刀算完整刀。"""
    challenges = _stat_rows(challenges)
    finished = sum(1 for c in challenges if c["health_remain"] != 0 or c["is_continue"])
    tail = sum(1 for c in challenges if c["health_remain"] == 0 and not c["is_continue"])
    comp_used = sum(1 for c in challenges if c["is_continue"])
    comp_left = len(challenges) - finished - comp_used
    return finished, tail, comp_used, comp_left


def _cycle_and_pool(state: GuildState, boss_num: str) -> tuple[int, dict]:
    """该 boss 实际可打周目与血量池：现周目打完同号王且下周目有血 → 下周目。"""
    if state.now[boss_num] == 0 and state.nxt[boss_num] != 0:
        return state.boss_cycle + 1, state.nxt
    if state.now[boss_num] == 0 and state.nxt[boss_num] == 0:
        raise InputError("只能挑战2个周目内的同个boss")
    return state.boss_cycle, state.now


def check_next_boss(cfg: GameConfig, state: GuildState, boss_num: str) -> bool:
    """boss 能否继续挑战下周目（两周期窗口且不跨阶段）。"""
    if state.now[boss_num] == 0 and state.nxt[boss_num] == 0:
        return False
    if cfg.level_by_cycle_of(state.game_server, state.boss_cycle) != \
       cfg.level_by_cycle_of(state.game_server, state.boss_cycle + 1):
        return False
    return True


def find_challenger(state: GuildState, qqid: int) -> Challenger | None:
    return state.challengers.get(str(qqid))


def _challenge_lines(cfg, state, boss_num, name_of) -> list[str]:
    """单个 boss 的挑战者信息行（移植 challenger_info_small）。"""
    lines = []
    now_hp = state.now[boss_num]
    next_hp = state.nxt[boss_num]
    real_hp = next_hp if now_hp == 0 else now_hp
    cycle = state.boss_cycle + 1 if now_hp == 0 else state.boss_cycle
    lines.append(f"{cycle}周目{boss_num}王，剩余{real_hp:,}血")
    if now_hp == 0 and not check_next_boss(cfg, state, boss_num):
        return lines
    boss_challengers = {q: c for q, c in state.challengers.items()
                        if c.boss_num == boss_num}
    if not boss_challengers:
        lines.append("目前无人在挑战这个boss")
        return lines
    lines.append(f"当前有{len(boss_challengers)}人正在挑战这个boss")
    lines.append("-" * 20)
    for qid, info in boss_challengers.items():
        line = f"->{name_of(int(qid))}"
        if info.is_continue:
            line += "(补偿)"
        if info.behalf:
            line += f"({name_of(info.behalf)}代刀)"
        if 11000 >= info.damage > 0:
            line += f", 剩{info.seconds}秒，打了{info.damage}万伤害"
        elif info.damage > 11000:
            line += f", 剩{info.seconds}秒，打了{info.damage}伤害"
        if info.comment:
            line += f"({info.comment})"
        if info.tree:
            line += ", 已挂树"
        lines.append(line)
    lines.append("-" * 20)
    return lines


# ---------------------------------------------------------------- 申请出刀
def apply_for_challenge(cfg: GameConfig, state: GuildState, qqid: int, boss_num,
                        is_continue: bool = False, behalf: int | None = None,
                        records_today: list[dict] | None = None,
                        name_of: Callable[[int], str] = str) -> RuleResult:
    boss_num = str(boss_num)
    if boss_num not in BOSS_NUMS:
        raise InputError("boss编号只能是1~5")
    if find_challenger(state, qqid):
        raise GroupError("你已经申请过了 (╯‵□′)╯︵┻━┻")
    if (not check_next_boss(cfg, state, boss_num)
            and state.now[boss_num] == 0):
        raise GroupError("只能挑战2个周目内且不跨阶段的同个boss，请等待该周目的boss全部击杀完毕")

    records_today = records_today or []
    finished, tail, used, comp_left = _blade_stats(records_today)
    if is_continue and comp_left == 0:
        raise GroupError("您没有补偿刀")
    # 完整刀配额用尽但还有待用补偿刀 → 自动转补偿刀（一天多杀时合法存在）
    if finished + comp_left >= 3 and comp_left != 0:
        is_continue = True
    if finished >= 3 and comp_left == 0:
        raise GroupError("今日已出了3次完整刀")

    import time as _time
    state.challengers[str(qqid)] = Challenger(
        qqid=qqid, boss_num=boss_num, is_continue=is_continue, behalf=behalf,
        started_at=int(_time.time()))  # 申请时间：yobot 按申请序展示挑战者
    info = [f"{name_of(qqid)}已开始挑战boss"]
    info += _challenge_lines(cfg, state, boss_num, name_of)
    return RuleResult(reply="\n".join(info))


def cancel_blade(state: GuildState, qqid: int, cancel_type: int = 1,
                 boss_num=None) -> RuleResult:
    """cancel_type: 0 全部 / 1 本人 / 2 特定boss所有人"""
    if cancel_type == 0:
        state.challengers.clear()
        return RuleResult(reply="已取消所有")
    if cancel_type == 1:
        ch = find_challenger(state, qqid)
        if ch is None:
            raise GroupError("你都没申请出刀，取啥子消啊 (╯‵□′)╯︵┻━┻")
        del state.challengers[str(qqid)]
        return RuleResult(reply="取消申请出刀成功")
    # type 2
    boss_num = str(boss_num)
    for q in [q for q, c in state.challengers.items() if c.boss_num == boss_num]:
        del state.challengers[q]
    return RuleResult()


# ---------------------------------------------------------------- 报刀/尾刀
def challenge(cfg: GameConfig, state: GuildState, operator: int, defeat: bool,
              damage=None, behalfed: int | None = None, is_continue: bool = False,
              boss_num=None, *, records_today: list[dict] | None = None,
              records_target: list[dict] | None = None, guild_today_count: int = 0,
              previous_day: bool = False, pcr_d: int = 0, pcr_t: int = 0,
              name_of: Callable[[int], str] = str) -> RuleResult:
    """记录伤害。operator=操作者，behalfed=真正出刀的人（代刀）。"""
    if (not defeat) and damage is None:
        raise InputError("未击败boss需要提供伤害值")
    if (not defeat) and damage < 0:
        raise InputError("伤害不可以是负数")

    behalf = None
    qqid = operator
    if behalfed is not None:
        behalfed = int(behalfed)
        behalf = operator
        qqid = behalfed
    if qqid == behalf:
        behalf = None

    ch = find_challenger(state, qqid)
    # 昨日补录是纯历史记录：不使用、也不消耗今天的申请与补偿标记
    carried_comp = bool((not previous_day) and ch and ch.is_continue and boss_num
                        and str(boss_num) == ch.boss_num)
    # 已申请且指定了报刀 boss → 先取消原申请（昨日补录不动今天的申请）
    if (not previous_day) and boss_num and ch:
        cancel_blade(state, qqid)
        ch = None
    # 已申请未指定 boss → 用申请的 boss
    if not boss_num and ch:
        boss_num = ch.boss_num
    if not boss_num:
        raise GroupError("又不申请出刀又不说打哪个王，报啥子刀啊 (╯‵□′)╯︵┻━┻")
    boss_num = str(boss_num)

    # 昨日的配额/补偿按目标日记录算；今天的行为保持不变
    if previous_day:
        records = records_target or []
    else:
        records = records_target if records_target is not None else (records_today or [])
    finished, _tail, _used, comp_left = _blade_stats(records)

    if previous_day:
        cycle, pool = state.boss_cycle, state.now
        # 昨日：完整刀额度用尽且有补偿 → 自动按补偿刀记
        is_continue = is_continue or (comp_left != 0 and finished + comp_left >= 3)
        live = pool.get(boss_num, 0) > 0
    else:
        if ch is None:
            apply_for_challenge(cfg, state, qqid, boss_num, is_continue=is_continue,
                                behalf=behalf, records_today=records_today, name_of=name_of)
        cycle, pool = _cycle_and_pool(state, boss_num)
        applied = find_challenger(state, qqid)
        # 残留补偿申请自愈：补偿刀按当日记录结算（不跨日），新一天已无补偿时
        # 申请自动降级为普通刀，避免普通报刀被「您没有补偿刀」卡死
        is_continue = is_continue or (comp_left > 0 and (
            carried_comp or bool(applied and applied.is_continue)))
        live = True

    if (not previous_day) and (not defeat) and damage >= pool[boss_num]:
        raise InputError("伤害超出剩余血量，如击败请使用尾刀")

    d, t = pcr_d, pcr_t
    if previous_day:
        if guild_today_count != 0:
            raise GroupError("今日报刀记录不为空，无法将记录添加到昨日")
        d -= 1
        t += 86400

    if is_continue and comp_left == 0:
        raise GroupError("您没有补偿刀")
    if finished >= 3 and comp_left == 0:
        if previous_day:
            raise InputError("昨日上报次数已达到3次")
        raise InputError("今日上报次数已达到3次")

    # 昨日只在“当前周目的这个王还活着”时同步实时状态；
    # 王已进入下一周目/已击破时只写历史记录，不碰今天的血量
    affects_state = (not previous_day) or live
    if defeat:
        boss_health_remain = 0
        if affects_state:
            challenge_damage = pool[boss_num]
            pool[boss_num] = 0
        else:
            challenge_damage = 0
    else:
        challenge_damage = damage
        if previous_day:
            boss_health_remain = 1  # 哨兵值：只表示“这一刀不是尾刀”
            if affects_state and damage < pool[boss_num]:
                pool[boss_num] -= damage
            else:
                affects_state = False  # 伤害超过当前血量：只记历史，不动实时状态
        else:
            boss_health_remain = pool[boss_num] - damage
            pool[boss_num] -= damage

    result = RuleResult()
    result.records.append(ChallengeRecord(
        qqid=qqid, boss_cycle=cycle, boss_num=boss_num,
        health_remain=boss_health_remain, damage=challenge_damage,
        is_continue=is_continue, behalf=behalf))

    if defeat and affects_state:
        all_clear = sum(1 for hp in state.now.values() if hp == 0)
        if all_clear == 5:
            old_cycle = state.boss_cycle
            # 跨阶段时，新周目的全部王都因升周目而重新可打，需要补发预约提醒
            phase_changed = (cfg.level_by_cycle_of(state.game_server, old_cycle)
                             != cfg.level_by_cycle_of(state.game_server, old_cycle + 1))
            state.boss_cycle = old_cycle + 1
            next_level = cfg.level_by_cycle_of(state.game_server, state.boss_cycle + 1)
            for b in BOSS_NUMS:                      # 下周目血量挪到现在
                moved_zero = state.nxt[b] == 0
                state.now[b] = state.nxt[b]
                if moved_zero or phase_changed:
                    result.notices.append(("subscribe_remind", b))
            for i, b in enumerate(BOSS_NUMS):        # 新下周目血量
                state.nxt[b] = cfg.boss_hp[state.game_server][next_level][i]
        else:
            pool[boss_num] = 0

    # 收尾后的状态清理与通知（先捕获挂树名单再清挑战状态）
    if defeat:
        if affects_state:
            on_tree = [int(q) for q, c in state.challengers.items()
                       if c.boss_num == boss_num and c.tree]
            if on_tree:
                result.notices.append(("tree_down", boss_num, on_tree))
            cancel_blade(state, qqid, cancel_type=2, boss_num=boss_num)
            if check_next_boss(cfg, state, boss_num):
                result.notices.append(("subscribe_remind", boss_num))
    elif not previous_day:
        try:
            cancel_blade(state, qqid)
        except RuleError:
            pass

    # 精简一行概览：伤害/击败已由回图（血条变化+跳伤）表达，文本只留刀数信息
    if defeat:
        # 补偿收尾：已完成刀数+1、补偿余量-1；普通收尾：已完成不加、补偿余量+1
        blade_word = "尾余刀" if is_continue else "收尾刀"
        done = finished + 1 if is_continue else finished
        comp_now = comp_left - 1 if is_continue else comp_left + 1
    else:
        blade_word = "剩余刀" if is_continue else "完整刀"
        done = finished + 1
        comp_now = comp_left - 1 if is_continue else comp_left
    parts = [f"⚔️{'昨日' if previous_day else ''}{blade_word}", f"完成{done}刀"]
    if comp_now > 0:
        parts.append(f"剩{comp_now}刀补")
    msg = " · ".join(parts) + "\n"
    msg += "\n".join(_challenge_lines(cfg, state, boss_num, name_of))
    result.reply = msg
    result.pcrdate, result.pcrtime = d, t
    return result


def undo(state: GuildState, operator: int, operator_authority: int,
         last: dict, cfg: GameConfig,
         name_of: Callable[[int], str] = str) -> RuleResult:
    """撤销最后一刀。last 为最后一刀的记录 dict（含 boss_cycle/boss_num/damage/qqid）。"""
    if last is None:
        raise GroupError("本群无出刀记录")
    if last["qqid"] != operator and operator_authority >= 100:
        raise UserError("无权撤销")

    last_num = str(last["boss_num"])
    last_cycle = last["boss_cycle"]
    # 兼容两种记录形态：rules 键(damage)与 store 行键(challenge_damage)
    last_damage = last["damage"] if "damage" in last else last["challenge_damage"]
    level = cfg.level_by_cycle_of(state.game_server, last_cycle)

    if last_cycle < state.boss_cycle:
        # 撤销的是切换周目的那一刀：整体回退一个周目
        for b in BOSS_NUMS:
            state.nxt[b] = state.now[b]
            state.now[b] = 0
        state.now[last_num] = last_damage
        state.boss_cycle = last_cycle
    else:
        pool = state.nxt if last_cycle != state.boss_cycle else state.now
        pool[last_num] += last_damage
        full = cfg.boss_hp[state.game_server][level][int(last_num) - 1]
        if pool[last_num] > full:
            pool[last_num] = full

    return RuleResult(reply=f"{name_of(last['qqid'])}的出刀记录已被撤销")


# ---------------------------------------------------------------- 挂树
def put_on_the_tree(cfg: GameConfig, state: GuildState, operator: int, message=None,
                    boss_num=False, behalfed: int | None = None,
                    records_today: list[dict] | None = None,
                    name_of: Callable[[int], str] = str) -> RuleResult:
    """挂树（可代挂）。boss_num=False 时沿用申请的 boss。"""
    challenger = behalfed or operator
    behalf = None
    if behalfed:
        behalf = operator
        if behalf == challenger:
            behalf = None

    if boss_num is False:
        ch = find_challenger(state, challenger)
        if ch is None:
            raise GroupError("你既没申请出刀，也没说挂哪个，挂啥子树啊 (╯‵□′)╯︵┻━┻")
        boss_num = ch.boss_num
    boss_num = str(boss_num)

    if find_challenger(state, challenger) is None:
        # 挂树允许自动申请出刀：先试完整刀，补偿不足转补偿刀
        try:
            apply_for_challenge(cfg, state, challenger, boss_num,
                                behalf=behalf, records_today=records_today, name_of=name_of)
        except GroupError as e1:
            if "完整" in str(e1):
                try:
                    apply_for_challenge(cfg, state, challenger, boss_num,
                                        is_continue=True, behalf=behalf,
                                        records_today=records_today, name_of=name_of)
                except GroupError as e2:
                    if "补偿" in str(e2):
                        raise GroupError("你今天都下班了，挂啥子树啊 (╯‵□′)╯︵┻━┻")
                    raise GroupError(str(e2))
            else:
                raise GroupError(str(e1))
    else:
        if find_challenger(state, challenger).boss_num != boss_num:
            raise GroupError("你申请的王和挂树的王不一样，怎么挂树啊 (╯‵□′)╯︵┻━┻")

    for item in state.challengers.values():
        if item.qqid == challenger and item.tree:
            raise GroupError("您已经在树上了")

    ch = state.challengers[str(challenger)]
    ch.tree = True
    if behalf:
        ch.tree_msg = f"[「{name_of(behalf)}」代挂]{message or ''}"
    else:
        ch.tree_msg = message
    return RuleResult(reply=f"{name_of(challenger)}挂树惹~ (っ °Д °;)っ")


def take_it_of_the_tree(state: GuildState, qqid: int, mode: int = 0,
                        boss_num=None) -> RuleResult:
    """mode 0: 本人下树；mode 1: boss 被击败时通知该 boss 所有树上的人（不摘除，由清挑战状态完成）。"""
    if mode == 0:
        ch = find_challenger(state, qqid)
        if ch is None:
            raise GroupError("你都没申请出刀，下啥子树啊 (╯‵□′)╯︵┻━┻")
        if not ch.tree:
            raise GroupError("你都没挂树，下啥子树啊 (╯‵□′)╯︵┻━┻")
        ch.tree = False
        ch.tree_msg = None
        return RuleResult(reply="下树惹~ _(:з)∠)_")
    boss_num = str(boss_num)
    on_tree = [int(q) for q, c in state.challengers.items()
               if c.boss_num == boss_num and c.tree]
    if on_tree:
        return RuleResult(notices=[("tree_down", boss_num, on_tree)])
    return RuleResult()


def build_tree_down_text(on_tree_qqids: list[int]) -> str:
    return "可以下树惹~ _(:з)∠)_\n" + "\n".join(f"[CQ:at,qq={q}]" for q in on_tree_qqids)


# ---------------------------------------------------------------- SL
def save_slot(state: GuildState, qqid: int, today_pcrdate: int, last_save_slot: int,
              only_check: bool = False, clean_flag: bool = False):
    """返回 (reply, 新last_save_slot, 是否清了挑战状态)。设置 SL 会同时清掉该成员的出刀/挂树状态。"""
    if only_check:
        used = (last_save_slot == today_pcrdate)
        return ("今日已使用SL" if used else "今日未使用SL", last_save_slot, False)
    if clean_flag:
        if last_save_slot != today_pcrdate:
            raise UserError("您今天还没有SL过")
        return ("已取消SL。若已申请/挂树，需重新报告。", 0, False)
    if last_save_slot == today_pcrdate:
        raise UserError("您今天已经SL过了，该不会退游戏了吧？ Σ(っ °Д °;)っ")
    dropped = str(qqid) in state.challengers
    state.challengers.pop(str(qqid), None)
    return ("已记录SL。若已申请/挂树，需重新报告。 Σ(っ °Д °;)っ", today_pcrdate, dropped)


# ---------------------------------------------------------------- 报伤害
def report_hurt(state: GuildState, qqid: int, seconds: int, damage: int,
                clean: bool = False, comment: str | None = None) -> RuleResult:
    ch = find_challenger(state, qqid)
    if ch is None:
        raise GroupError("你都没申请出刀，报啥子伤害啊 (╯‵□′)╯︵┻━┻")
    if clean:
        if ch.damage == 0:
            return RuleResult(reply="您还没有报伤害呢")
        ch.seconds = 0
        ch.damage = 0
        ch.comment = None
        return RuleResult(reply="取消成功~")
    ch.seconds = seconds
    ch.damage = damage
    if comment:
        ch.comment = comment
    return RuleResult(reply="已记录伤害，小心不要手滑哦~ ♪(´▽｀)")


def update_status(state: GuildState, qqid: int, seconds: int, damage: int,
                  comment: str | None = None,
                  name_of: Callable[[int], str] = str) -> RuleResult:
    """更新出刀/挂树的剩余秒数、伤害（万）与留言（网页"更新出刀"）。
    与群聊报伤害不同：留言可清空。"""
    ch = find_challenger(state, qqid)
    if ch is None:
        raise GroupError("该成员当前没有申请出刀或挂树")
    ch.seconds = max(0, int(seconds or 0))
    ch.damage = max(0, int(damage or 0))
    ch.comment = (comment or "").strip() or None
    return RuleResult(reply=f"{name_of(qqid)}的出刀状态已更新")


# ---------------------------------------------------------------- 预约
def subscribe(state: GuildState, qqid: int, msg, note: str = "",
              name_of: Callable[[int], str] = str) -> RuleResult:
    if not msg:
        raise GroupError("您预约了一个空气")
    if msg == "表":
        if not state.subscribes:
            raise GroupError("目前没有人预约任意一个Boss")
        lines = ["预约表："]
        for boss_num in sorted(state.subscribes, key=int):
            lines.append(f"==={boss_num}号Boss===")
            for q, note_ in state.subscribes[boss_num].items():
                lines.append(f"{name_of(q)}" + (f"：{note_}" if note_ else ""))
        lines.append("=" * 12)
        return RuleResult(reply="\n".join(lines))
    boss_num = str(msg)
    if boss_num not in BOSS_NUMS:
        raise InputError("boss编号只能是1~5")
    subs = state.subscribes.setdefault(boss_num, {})
    if qqid in subs:
        raise GroupError("你已经预约过这个boss啦 (╯‵□′)╯︵┻━┻")
    subs[qqid] = note
    if "合" in (note or ""):
        return RuleResult(reply="")  # 合刀预约静默：不发成功提示（用户拍板）
    return RuleResult(reply=f"预约{boss_num}王成功！下个{boss_num}王出现时会at提醒。")


def subscribe_cancel(state: GuildState, boss_num, qqid: int | None = None) -> RuleResult:
    if not boss_num:
        raise GroupError("您取消了个寂寞")
    boss_num = str(boss_num)
    if qqid is None:
        state.subscribes.pop(boss_num, None)
    else:
        subs = state.subscribes.get(boss_num, {})
        if qqid not in subs:
            raise GroupError("您还没有预约这个boss")
        del subs[qqid]
        if not subs:
            state.subscribes.pop(boss_num, None)
    return RuleResult(reply="取消成功~")


def build_subscribe_remind(state: GuildState, boss_num,
                           name_of: Callable[[int], str] = str) -> str | None:
    """新 boss 出现时的预约提醒文本（"合"刀预约静默跳过）。None=无需提醒。"""
    boss_num = str(boss_num)
    subs = state.subscribes.get(boss_num)
    if not subs:
        return None
    at_lines = [f"[CQ:at,qq={uid}]" + (f"：{note}" if note else "")
                for uid, note in subs.items() if "合" not in (note or "")]
    if not at_lines:
        return None  # 全是合刀预约：静默跳过，不发提醒文字
    return f"船新的{boss_num}王来惹~ _(:з)∠)_\n" + "\n".join(at_lines)


def clear_subscribes(state: GuildState, boss_num) -> None:
    state.subscribes.pop(str(boss_num), None)


# ---------------------------------------------------------------- 管理操作
def modify(cfg: GameConfig, state: GuildState, cycle: int | None,
           boss_data: dict) -> RuleResult:
    """网页管理员改状态（移植 realize.modify）。
    boss_data: {boss_num: {'health': int, 'is_next': bool}}"""
    cycle = cycle or state.boss_cycle
    if cycle < 1:
        raise InputError("周目数不能为负")
    now_level = cfg.level_by_cycle_of(state.game_server, cycle)
    next_level = cfg.level_by_cycle_of(state.game_server, cycle + 1)
    for boss_num, data in boss_data.items():
        boss_num = str(boss_num)
        next_full = cfg.full_health(state.game_server, cycle + 1, boss_num)
        if data["is_next"]:
            if now_level == next_level:
                state.now[boss_num] = 0
                state.nxt[boss_num] = data["health"]
            else:
                raise InputError("设置为下个周目的BOSS与当前周目BOSS不可处于不同阶段。")
        else:
            state.now[boss_num] = data["health"]
            state.nxt[boss_num] = next_full
    state.boss_cycle = cycle
    return RuleResult(reply="boss状态已修改")


def query_tree(state: GuildState, boss_num=None) -> dict:
    """查树：{boss_num: [(qqid, 留言), ...]}，boss_num=None 查全部。"""
    result = {b: [] for b in BOSS_NUMS}
    for q, c in state.challengers.items():
        if c.tree and (boss_num is None or c.boss_num == str(boss_num)):
            result[c.boss_num].append((c.qqid, c.tree_msg))
    return result


# ---------------------------------------------------------------- 状态总览
def status_summary(cfg: GameConfig, state: GuildState,
                   name_of: Callable[[int], str] = str) -> str:
    lines = []
    for boss in BOSS_NUMS:
        lines += _challenge_lines(cfg, state, boss, name_of)
        lines.append("")
    return "\n".join(lines).rstrip()
