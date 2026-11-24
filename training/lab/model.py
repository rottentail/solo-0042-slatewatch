"""盲标练习的确定性设计规格（本模块含答案，禁止进入盲包）。

时间一律以秒描述、在生成时换算为采样 / 画格。所有关键时点都落在
200 ms 网格上（同时是 25 fps 的整 5 帧、16 kHz 的整 3200 采样、50 ms
扫描窗的整 4 窗），从而让“实录数字静音”和“无覆盖空洞”在盲听 WAV
里呈现为完全相同的 0.8 s 全零平台。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRAIN = REPO / "training"
BLIND = TRAIN / "blind_pack"
ASSETS = BLIND / "assets"
DEBRIEF = TRAIN / "debrief"
MASTERS = DEBRIEF / "masters"
TRUTH_JSON = DEBRIEF / "truth.json"
PAIRING_JSON = DEBRIEF / "scan_pairing.json"
VERIFY_JSON = DEBRIEF / "verification_report.json"
VERIFY_MD = DEBRIEF / "verification_report.md"
LEAK_JSON = DEBRIEF / "leak_audit.json"
LEAK_MD = DEBRIEF / "leak_audit.md"
DEBRIEF_DOC = DEBRIEF / "复盘手册-答案与真值.md"
BLIND_ZIP = BLIND / "Slatewatch盲标练习_盲包.zip"
BLIND_HTML = BLIND / "index.html"

# ---- 媒体常量（所有条目严格一致，杜绝时长 / 长度提示） -----------------
RATE = 16_000
CHANNELS = 2
FPS = 25
WIDTH = 320
HEIGHT = 180
DURATION_S = 6.4
TOTAL_SAMPLES = int(RATE * DURATION_S)      # 102400 / 声道
FRAME_COUNT = round(DURATION_S * FPS)       # 160
CHUNK = RATE // FPS                         # 640 采样 = 一帧 = 40 ms

# 两处外观完全相同的 0.8 s 全零平台 L1 / L2。
PLATEAUS: dict[str, tuple[float, float]] = {
    "L1": (1.6, 2.4),
    "L2": (3.2, 4.0),
}
PLATEAU_SAMPLES = {
    key: (int(a * RATE), int(b * RATE)) for key, (a, b) in PLATEAUS.items()
}

# 声画同步判据：独立核查时 |起振-白闪| <= 容差即判同步。
SYNC_TOL_US = 140_000

# 两条物理音轨使用完全相同的流名 —— 本题的核心陷阱。
SHARED_STREAM_NAME = "主混 MIX"

STATIONS = [
    {"shape": "圆形左标", "sound": "一声清亮高频「嗒」（木鱼 / 铃）", "freq": 880, "x": 56},
    {"shape": "方形中标", "sound": "一声中频闷「梆」", "freq": 520, "x": 160},
    {"shape": "三角右标", "sound": "一声低沉有力的「咚」（鼓）", "freq": 196, "x": 264},
]


@dataclass(frozen=True)
class Hit:
    t: float
    station: int
    amp: int = 10_500
    dur_ms: int = 95

    @property
    def freq(self) -> int:
        return STATIONS[self.station]["freq"]


@dataclass(frozen=True)
class Item:
    bid: str            # 盲包中性编号 BL-01 ...
    code: str           # 内部 / 复盘编号 A1 ...
    group: str          # "sync"（可据声画识轨）| "ambiguous"（必须无法确认）
    scene: str          # 画面描述（参训者可见）
    correct: tuple[Hit, ...]   # 与画面 / 制作记录一致的主混打点
    wrong: tuple[Hit, ...]     # 参考 / 错轨打点（ambiguous 时与 correct 相同）
    correct_label: str | None  # 密封答案："jia" | "yi" | None(无法确认)
    main_first: bool           # 母带中主混是否为第一条音频流
    main_hole: str             # 主混的无覆盖平台编号；另一个平台为其实录静音
    note: str = ""
    extra_record: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ambiguous(self) -> bool:
        return self.group == "ambiguous"


def _h(spec: tuple[float, int] | tuple[float, int, int, int]) -> Hit:
    return Hit(*spec)  # type: ignore[arg-type]


# 盲包顺序刻意交错，避免“后一组都答无法确认”的结构泄漏。
# correct_label / main_first / main_hole 都是密封真值，不写进任何盲包文件。
ITEMS: list[Item] = [
    Item(
        bid="BL-01", code="A1", group="sync",
        scene="摄影棚打点台：滑锤依次滑向圆、方、三角三个站位，接触站位的瞬间出现矩形白闪。",
        correct=tuple(map(_h, [(0.8, 0), (2.8, 1), (4.4, 2)])),
        wrong=tuple(map(_h, [(0.8, 0), (3.0, 1), (4.8, 2)])),
        correct_label="yi", main_first=True, main_hole="L1",
        note="错轨保留了第一个打点，却在后两个打点漂移。",
    ),
    Item(
        bid="BL-02", code="B1", group="ambiguous",
        scene="小型演奏位：击槌先后落在左、右两个站位，落点有白闪；中段有两段动作留白。",
        correct=tuple(map(_h, [(1.0, 0), (4.6, 2)])),
        wrong=tuple(map(_h, [(1.0, 0), (4.6, 2)])),
        correct_label=None, main_first=True, main_hole="L1",
        extra_record=("两处留白窗口内没有规定动作，也没有必须出现的台词或演奏声。",),
    ),
    Item(
        bid="BL-03", code="A2", group="sync",
        scene="同型打点台的另一条镜头：滑锤仍按圆 → 方 → 三角顺序击打，接触瞬间白闪。",
        correct=tuple(map(_h, [(1.0, 0), (3.0, 1), (4.8, 2)])),
        wrong=tuple(map(_h, [(1.0, 0), (2.6, 1), (4.2, 2)])),
        correct_label="jia", main_first=False, main_hole="L2",
        note="错轨第一个打点对齐，后两个打点提前。",
    ),
    Item(
        bid="BL-04", code="B2", group="ambiguous",
        scene="独奏站位：击槌依次落在左、中、右三个站位，落点白闪；画面里同样有两段等长留白。",
        correct=tuple(map(_h, [(0.8, 0), (2.8, 1), (5.2, 2)])),
        wrong=tuple(map(_h, [(0.8, 0), (2.8, 1), (5.2, 2)])),
        correct_label=None, main_first=False, main_hole="L2",
        extra_record=("制作记录只要求这三处动作能听到对应现场声，未记载任何流号或覆盖安排。",),
    ),
    Item(
        bid="BL-05", code="A3", group="sync",
        scene="打点台第三条：滑锤长距离移动后击打三个站位，接触瞬间白闪。",
        correct=tuple(map(_h, [(0.6, 0), (2.6, 1), (5.0, 2)])),
        wrong=tuple(map(_h, [(0.6, 0), (3.0, 1), (4.4, 2)])),
        correct_label="yi", main_first=False, main_hole="L1",
        note="错轨保留首点，第二点延后、第三点提前，无法用整体平移解释。",
    ),
    Item(
        bid="BL-06", code="A4", group="sync",
        scene="打点台第四条：三次击打间隔不同，接触瞬间白闪；三种音色对应三种站位。",
        correct=tuple(map(_h, [(1.2, 0), (2.8, 1), (4.2, 2)])),
        wrong=tuple(map(_h, [(1.2, 0), (2.6, 1), (5.0, 2)])),
        correct_label="jia", main_first=True, main_hole="L2",
        note="错轨首点对齐，第二点略提前、第三点大幅延后。",
    ),
]

BY_BID = {item.bid: item for item in ITEMS}

# 盲包中使用的中性候选标签。
LABELS = (("jia", "甲"), ("yi", "乙"))


def plateau_other(name: str) -> str:
    return "L2" if name == "L1" else "L1"


def label_cn(label: str) -> str:
    return dict(LABELS)[label]
