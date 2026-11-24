"""公会战实时状态模型。

设计原则（相对 yobot_remix 的改进）：不再把状态塞 JSON 文本字段，
boss 血量/挑战者/预约都是显式结构，由 store 层落库、rules 层原地修改。"""
from dataclasses import dataclass, field

from .gameconfig import GameConfig

BOSS_NUMS = ("1", "2", "3", "4", "5")


@dataclass
class Challenger:
    """一个正在挑战（或挂树）的成员。每人同一时刻最多一条。"""
    qqid: int
    boss_num: str
    is_continue: bool = False
    behalf: int | None = None        # 代刀人（实际操作者）
    seconds: int = 0                 # 报伤害：剩余秒数
    damage: int = 0                  # 报伤害：预计伤害
    tree: bool = False
    tree_msg: str | None = None
    comment: str | None = None       # 更新出刀留言
    started_at: int = 0              # 申请时间（展示按申请序，对齐 yobot）


@dataclass
class GuildState:
    group_id: int
    group_name: str | None = None
    battle_id: int = 0               # 档案号
    game_server: str = "cn"
    boss_cycle: int = 1
    threshold: int = 4_000_000       # 业绩表尾刀计分阈值
    notification: int = 0xFFFF       # 网页操作群通知位（fire-and-forget）
    privacy: int = 2                 # 0x1 游客可看 0x2 允许 api
    apikey: str | None = None        # 外部统计 API 密钥
    now: dict[str, int] = field(default_factory=dict)    # 现周目剩余血量
    nxt: dict[str, int] = field(default_factory=dict)    # 下周目剩余血量
    challengers: dict[str, Challenger] = field(default_factory=dict)  # str(qqid) -> Challenger
    subscribes: dict[str, dict[int, str]] = field(default_factory=dict)  # boss_num -> {qqid: 留言}


def fresh_state(group_id: int, cfg: GameConfig, server: str = "cn",
                battle_id: int = 0, cycle: int = 1, **kwargs) -> GuildState:
    """新建/重置公会状态：满血的当前与下周目。"""
    st = GuildState(group_id=group_id, game_server=server,
                    battle_id=battle_id, boss_cycle=cycle, **kwargs)
    for boss in BOSS_NUMS:
        st.now[boss] = cfg.full_health(server, cycle, boss)
        st.nxt[boss] = cfg.full_health(server, cycle + 1, boss)
    return st
