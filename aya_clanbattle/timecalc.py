"""合刀 / 垫刀 / 补偿轴计算。

语义与文案 1:1 移植自 hoshino `pcrtimecalc`（原模块用 sympy 解一元一次方程，
这里改为等价闭式解，去掉外部依赖）。纯函数、无 IO。
"""
import math

TIMELINE_HELP = "\n使用方法：补偿轴 [返还时间] [时间1] [时间2] ...\n例：补偿轴 121 118 110 107 58"
COMBINE_USAGE = "请输入：合刀计算 刀1伤害 刀2伤害 剩余血量\n如：合刀计算 50 60 70"
PAD_USAGE = "请输入：垫刀计算 boss血量 当前伤害 当前显示秒数 目标补偿秒数\n如：垫刀计算 1700 1700 34 56"


def combine_calc(d1: float, d2: float, rest: float) -> str:
    """两刀合刀：分别算出谁先出时，另一刀能获得的补偿秒数。"""
    if d1 <= 0 or d2 <= 0:
        raise ValueError(COMBINE_USAGE)
    if d1 + d2 < rest:
        return "醒醒！这两刀是打不死boss的\n"
    dd1 = min(d1, rest)
    dd2 = min(d2, rest)
    res1 = min(math.ceil((1 - (rest - dd1) / dd2) * 90 + 20), 90)
    res2 = min(math.ceil((1 - (rest - dd2) / dd1) * 90 + 20), 90)
    reply = f"刀1伤害：{d1}\n刀2伤害：{d2}\nBOSS血量：{rest}\n"
    if d1 >= rest or d2 >= rest:
        reply += "注：\n"
        if d1 >= rest:
            reply += f"第一刀可直接秒杀boss，伤害按 {rest} 计算\n将补偿剩余时间+10s\n"
        if d2 >= rest:
            reply += f"第二刀可直接秒杀boss，伤害按 {rest} 计算\n将补偿剩余时间+10s\n"
    reply += f"刀1先出，另一刀可获得 {res1} 秒补偿刀\n"
    reply += f"刀2先出，另一刀可获得 {res2} 秒补偿刀\n"
    return reply


def pad_calc(total: float, dmg: float, current: float, aim: float) -> str:
    """垫刀：反解达到目标补偿秒数所需的垫伤。"""
    if dmg <= 0 or current >= 90:
        raise ValueError(PAD_USAGE)
    warn = ""
    if current <= 1 or aim < 20 or aim > 90:
        warn = "可能发生错误：没那么夸张的秒数\n"
    # 110 - ((total - x) / dmg * (90 - current)) - aim = 0 的闭式解
    x = total - dmg * (110 - aim) / (90 - current)
    xs = f"{x:.2f}"
    return (warn
            + f"boss血量：{total}；当前伤害：{dmg}；当前秒数：{current}\n-----\n"
            + f"目标补偿{aim}秒需要的垫伤为{xs}（大于{xs}则补偿{aim + 1}秒）")


def _time_converter(time: int) -> int:
    """PCR 轴的 分:秒（压缩成数字）转十进制秒。"""
    if not 0 <= time <= 130 or 60 <= time <= 99:
        raise ValueError("time out of range")
    return time - 40 if time >= 100 else time


def _pretty_time(time: int) -> str:
    if time <= 0:
        return "0:00"
    if time < 60:
        return ("0:%d" if time >= 10 else "0:0%d") % time
    return ("1:%d" if time >= 70 else "1:0%d") % (time - 60)


def comp_timeline(tokens: list[str]) -> str:
    """补偿轴：tokens=[返还时间, 时间1, 时间2, ...]（均为纯数字字符串）。"""
    for t in tokens:
        if not t.isdigit():
            raise ValueError("错误：非纯数字时间\"%s\"\n时间无需符号分隔分秒；如1:03输入103即可" % t)
    if len(tokens) <= 1:
        raise ValueError("参数不足：请输入返还时间及至少一个原始轴时间" + TIMELINE_HELP)
    if len(tokens) >= 35:
        raise ValueError("原始轴过长，请分开两次计算")
    try:
        remain = _time_converter(int(tokens[0]))
        origin = [_time_converter(int(t)) for t in tokens[1:]]
    except ValueError:
        raise ValueError("时间超出范围，请检查输入时间轴\n请确保输入时间均在130-100及059-001之间")
    used = 90 - remain
    new = [t - used for t in origin]
    ret = "返还时间%s\n" % _pretty_time(remain)
    ret += "原始轴 补偿轴"
    for o, n in zip(origin, new):
        ret += "\n%s     %s" % (_pretty_time(o), _pretty_time(n))
    return ret
