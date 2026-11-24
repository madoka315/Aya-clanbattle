"""PCR 游戏内时间：换日时间为各国本地凌晨 5 点（国/台 UTC+8、日/韩 UTC+9）。

pcr_time_offset 是加在 UTC 纪元时间戳上的小时数（移植自 yobot_remix，数值不变）：
偏移 3 = UTC 21:00 翻日 = UTC+8 的本地 5:00；偏移 4 = UTC 20:00 = UTC+9 的本地 5:00。
注意：这个数不是"本地几点换日"，不要误读成凌晨 3/4 点。"""
import datetime
import time

PCR_TIME_OFFSET = {"jp": 4, "tw": 3, "cn": 3}


def pcr_tzinfo(area: str) -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=PCR_TIME_OFFSET[area]))


def pcr_datetime(area: str, dt=None) -> tuple[int, int]:
    """返回 (pcr日期[自纪元天数], 当日秒数)。dt 可为 None(当前)/时间戳/datetime。"""
    if dt is None:
        ts = int(time.time())
    elif isinstance(dt, int):
        ts = dt
    elif isinstance(dt, datetime.datetime):
        ts = int(dt.timestamp())
    else:
        raise ValueError(f"cannot parse {type(dt)} to pcrdatetime")
    ts += PCR_TIME_OFFSET[area] * 3600
    return divmod(ts, 86400)


def pcr_timestamp(d: int, t: int, area: str) -> int:
    return 86400 * d + t - PCR_TIME_OFFSET[area] * 3600


def pcr_date_str(d: int, area: str = "cn") -> str:
    """PCR 日 d 的日历标签：d 从当地凌晨 5 点开始（各国均为 5 点换日），
    纪元 + d 天 + 5 小时即该日 05:00 的本地时刻。"""
    return (datetime.datetime(1970, 1, 1)
            + datetime.timedelta(days=d, hours=5)).strftime("%Y-%m-%d")
