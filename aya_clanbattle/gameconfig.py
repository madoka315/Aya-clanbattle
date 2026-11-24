"""游戏数值配置：boss 血量表 / 周目-阶段映射。
数值来自 yobot_config.json 同款结构（可在 config.json 里维护并由网页端一键更新）。"""
import copy
from dataclasses import dataclass

# 默认血量表（与 yobot_remix 当前配置一致：国/台同表）
DEFAULT_BOSS_HP = {
    "cn": [
        [9000000, 12000000, 15000000, 18000000, 23000000],
        [42000000, 48000000, 50000000, 52000000, 60000000],
        [70000000, 80000000, 90000000, 100000000, 120000000],
        [1320000000, 1360000000, 1400000000, 1440000000, 1480000000],
    ],
    "jp": [
        [9000000, 12000000, 15000000, 18000000, 23000000],
        [12000000, 15000000, 20000000, 23000000, 30000000],
        [50000000, 56000000, 64000000, 70000000, 85000000],
        [1000000000, 1040000000, 1080000000, 1120000000, 1160000000],
    ],
    "tw": [
        [9000000, 12000000, 15000000, 18000000, 23000000],
        [42000000, 48000000, 50000000, 52000000, 60000000],
        [70000000, 80000000, 90000000, 100000000, 120000000],
        [1320000000, 1360000000, 1400000000, 1440000000, 1480000000],
    ],
}
DEFAULT_LEVEL_BY_CYCLE = {
    s: [[-2, 0], [1, 6], [7, 22], [23, 999]] for s in ("cn", "jp", "tw")
}
# 当期 boss 头像 unitId（可用网页端"拉取当期 boss 数据"更新）
DEFAULT_BOSS_ID = {
    "cn": ["302100", "320800", "307201", "301501", "300201"],
    "jp": ["305700", "309000", "305900", "305800", "300100"],
    "tw": ["302100", "320800", "307201", "301501", "300201"],
}


def merge_with_defaults(default: dict, loaded) -> dict:
    """默认表 + 配置表按服合并（深拷贝）：保证 cn/jp/tw 始终齐全，
    且运行配置不与模块默认对象共享引用（避免缺键时误以为已生效）。"""
    out = copy.deepcopy(default)
    if isinstance(loaded, dict):
        for server, table in loaded.items():
            out[server] = copy.deepcopy(table)
    return out


@dataclass
class GameConfig:
    boss_hp: dict                      # server -> level -> [hp1..hp5]
    level_by_cycle: dict               # server -> [[起周目, 止周目], ...]
    boss_id: dict                      # server -> [unitId1..unitId5]

    @classmethod
    def load(cls, data: dict | None = None) -> "GameConfig":
        data = data or {}
        return cls(
            boss_hp=merge_with_defaults(DEFAULT_BOSS_HP, data.get("boss")),
            level_by_cycle=merge_with_defaults(DEFAULT_LEVEL_BY_CYCLE,
                                               data.get("level_by_cycle")),
            boss_id=merge_with_defaults(DEFAULT_BOSS_ID, data.get("boss_id")),
        )

    def icon_id(self, server: str, idx: int) -> str:
        try:
            return str(self.boss_id[server][idx])
        except (KeyError, IndexError):
            return "0"

    def level_by_cycle_of(self, server: str, cycle: int) -> int:
        """周目 -> 阶段(0=A,1=B,2=C,3=D)"""
        level = 0
        for lo, hi in self.level_by_cycle[server]:
            if lo <= cycle <= hi:
                return level
            level += 1
        return level

    def full_health(self, server: str, cycle: int, boss_num: str | int) -> int:
        return self.boss_hp[server][self.level_by_cycle_of(server, cycle)][int(boss_num) - 1]


import json as _json

_BOSS_NAME_CACHE = None


def load_boss_id_name() -> dict:
    """boss unitId -> 名称映射（boss_id_name.json），进程内缓存。"""
    global _BOSS_NAME_CACHE
    if _BOSS_NAME_CACHE is None:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boss_id_name.json")
        with open(path, encoding="utf-8") as f:
            _BOSS_NAME_CACHE = _json.load(f)
    return _BOSS_NAME_CACHE
