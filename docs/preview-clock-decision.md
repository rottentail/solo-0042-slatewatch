# 预览跟片 + 复听：页面主时钟选型决策

状态：待评审 · 适用版本：下一版浏览器工作台（预览页同时播放画格与 WAV）
可重跑证据：`python -m tools.preview_sync.runner`（需要本仓库运行环境，见文末「复跑」）

## 1. 决策一句话

**预览页以节目微秒（`clock.json` 的整数微秒时线）作为页面主时钟；WAV 只作为从设备按节目位置调度，并对其输出时钟做周期性再锚定。** 不把 WAV 的 `audio.currentTime` 当作主时钟。原因是三种目标输入里有一种根本没有伴音，另外两种的对齐正确性都依赖只有节目微秒时线才携带的覆盖/补零事实。

## 2. 现状与问题边界

当前预览页（`slatewatch/desk/preview.py`）把证据画格以静态 `<img>` 列出、把 `sound.wav` 交给一个独立 `<audio controls>` 播放，两者没有共同的播放位置。下一版要求「同时跟片和复听」，必须选一个页面主时钟来驱动：

- 画格切换（含缺画格占位）；
- WAV 播放位置、播放/暂停/拖动；
- 时间游标、候选区间高亮和补零/缺口标注。

后端已经把所有事实统一在容器共享起点的节目微秒时线上（`PROJECT_SPEC.md` 处理约定、`slatewatch/clock.py`），画面按解码显示时标量测、不用平均帧率重建时刻（`slatewatch/picture.py:22` 的 `nominal_duration` 仅用于末帧时长提示），证据映射里同时给出目标采样点和实际源帧（`slatewatch/evidence.py:60` 起的 `finish()`）。本决策只讨论浏览器侧如何消费这些既有事实，不改变扫描与证据格式。

## 3. 两个候选主时钟

| 维度 | A. 节目微秒主时钟（推荐） | B. WAV 主时钟 |
| --- | --- | --- |
| 页面位置来源 | `performance.timeOrigin + performance.now()` 锚定到 `clock.json` 的 `start_us`，输出整数节目微秒 | `<audio>.currentTime`（秒，浮点）乘 WAV 采样率再换算节目微秒 |
| 画格调度 | 用节目位置查 `picture[].source_us` 与覆盖区间 | 先把 WAV 秒换算成节目微秒，再查同一张表 |
| 伴音调度 | `audio.currentTime = (节目位置 − span.start − clock_offset)`，仅作从设备 | 天然主设备，画格跟随音频 |
| 无伴音输入 | 同一套逻辑，仅停用音频从设备 | **无时钟可依**，必须退回 A，等于存在两套播放模型 |
| 覆盖/补零标注 | 直接按节目位置比对覆盖区间与 `filled_sample_ranges` | WAV 本身无法区分补零与源数字静音，仍须回查节目时线 |
| 失步恢复 | 检测音频从设备偏差，单次再锚定（seek） | 主时钟不会报错，漂移/停顿会直接变成画格错误且无基准可发现 |

换算公式（两种方案都绕不开，证据映射已提供全部参数）：

```
节目微秒 → WAV 采样：sample = first_source_sample + round((us − start_us − clock_offset_us) · rate / 1e6)
WAV 秒   → 节目微秒：us = start_us + clock_offset_us + round(currentTime · 1e6)
```

## 4. 三种输入的实测源事实

以下数字由 `tools/preview_sync/probe.py` 通过真实回环 HTTP 服务、真实 PyAV 解码与真实证据打包产生，不是手算估值。每次重跑容器字节与派生 ID 可能变化，但时线数值由信号布局决定，保持稳定。

### 4.1 无伴音（`no-sound`，25 fps，无音频轨）

- 画面覆盖：`[0, 4 000 000)` 单段；`coverage.sound = null`（区别于「有轨但有洞」）。
- 黑场候选 `[0, 480000)`，取 120 ms 留边、4 画格后证据区间 `[0, 600000)`，无 WAV。
- 画格目标点 → 实际源帧（微秒）：`75000→40000`、`225000→200000`、`375000→360000`、`525000→520000`；偏差恒为半个帧间隔量级（≤ 40 ms 的取整，见 4.3 说明）。

**保证对照**

- 方案 A：可保证画格严格按源 PTS 序列切换，覆盖/缺口标注与扫描页时间轴完全同源；没有音频从设备，无需任何音频对齐。
- 方案 B：不存在 WAV，主时钟缺失。若此时退回方案 A，则代码路径分叉、测试矩阵翻倍，并且用户在同一页面看到的播放语义会随「是否选伴音轨」变化。**此输入单独即足以排除 B 作为统一主时钟。**

### 4.2 开头有补零（`lead-pad`，25 fps；伴音首个采样为 #1600，即 200 ms 处才出现）

- 画面覆盖 `[0, 4 000 000)`；伴音覆盖 `[200000, 2 000 000)` 与 `[2 200000, 3 200000)` 两段，前面 200 ms 和内部 200 ms 都是源级空洞。
- 黑场证据区间 `[0, 600000)`；WAV 为 8000 Hz、双声道、4800 帧 = 0.600 s，`first_source_sample=0`、`end_source_sample=4800`、`clock_offset_us=0`。
- `filled_sample_ranges = [{first:0, end:1600}]`：WAV 的前 1600 个采样（前 200 ms）是摘片器写入的**合成零**，不是源数据（`slatewatch/evidence.py:41` 的 PCM 零初始化与 `evidence.py:77` 的未覆盖区间记录）。

**保证对照**

- 方案 A：可保证播放到 `[0, 200000)` 时，画面正常跟片、波形区明确标注「补零」而非静音，游标位置与候选/覆盖区间在同一微秒标尺上；内部空洞 `[2 000000, 2 200000)` 同理。
- 方案 B：WAV 文件本身无法区分「合成零」与「源数字静音」——两者 PCM 都是 0。页面只有用节目位置回查 `filled_sample_ranges` 才能正确标注，即 B 在最关键的补零语义上仍然依赖 A 的时线。此外 WAV 局部时间 0 对应节目 `start_us=0` 只是本例巧合；当 `clock_offset_us ≠ 0`（见 4.3，实测为 1 882 000）时，B 必须先做一次偏移标定，标定误差直接成为系统性画格偏差（仿真中 80 ms 标定误差 → 316 个 tick 画格错位，见第 6 节）。

### 4.3 分数帧率且中间有洞（`fractional-hole`，30000/1001 fps；2.0–2.5 s 跳帧；伴音连续）

- 画面覆盖两段：`[0, 2 002 000)` 与 `[2 503 000, 4 004 000)`，中间 501 ms 是画面缺口（超过 `picture_gap_ms=250` 阈值，产生 `picture_gap` 候选）。
- 帧间隔实测 33 367/33 333 µs 交替（后端 `round(pts·time_base·1e6)` 的整数微秒结果），**不是** 用 29.97 的平均帧率重建；洞两侧最近帧为 1 969 000 与 2 503 000。
- 伴音 8000 Hz 连续覆盖 `[0, 3 200000)`；证据区间 `[1 882 000, 2 623 000)`，WAV 5928 帧 = 0.741 s，`clock_offset_us=1 882 000`、无补零段。
- 画格目标点 → 源帧：`1 974 625→1 969 000`（ present）、`2 159 875→null`、`2 345 125→null`（两点落在洞里，`file=null`）、`2 530 375→2 503 000`（present）。

**保证对照**

- 方案 A：可保证游标进入 `[2 002 000, 2 503 000)` 时稳定渲染「无画格覆盖」占位而不是冻结上一帧；出洞瞬间切到 2 503 000 源帧。分数帧边界直接采用后端整数 PTS，页面不做任何帧率算术，因此不存在累积取整误差。
- 方案 B：WAV 秒 → 节目微秒经过 `round(currentTime·1e6)`，再用最近源帧二分匹配。该输入帧间隔约 33.37 ms，单次换算最坏可把位置放到相邻帧的另一侧，产生一帧（约 33 ms）的切换提前/滞后；洞边界处则可能在 501 ms 缺口内错误画出真实帧（仿真 `hole_violations` 可复现）。B 没有任何独立于音频的手段知道洞的存在。

## 5. 两种主时钟各自能保证的对齐（汇总）

| 保证项 | A. 节目微秒 | B. WAV |
| --- | --- | --- |
| 无伴音输入可跟片 | ✅ 原生支持 | ❌ 无时钟，必须退回 A |
| 画格切换与扫描时轴同源（整数 PTS，无帧率重建） | ✅ | ⚠️ 依赖一次秒↔微秒换算和偏移标定 |
| 画面缺口内只显示占位、不画源帧 | ✅ 位置查覆盖区间即可 | ❌ 音频时钟对洞无感知，需回查 A 的数据 |
| 补零与源数字静音分开标注 | ✅ 节目位置查 `filled_sample_ranges` | ❌ PCM 不可区分，仍需回查节目时线 |
| 长节目末端音画累积偏差有界 | ✅ 可周期再锚定从设备 | ❌ 漂移即主时钟误差，无基准发现 |
| 解码卡顿时画面仍停在正确节目位置 | ✅ 墙钟标尺不受管线停顿影响 | ❌ 主时钟冻结，恢复后整体错位（见 6.3） |
| 音频设备输出的采样级平滑 | ⚠️ 需要再锚定逻辑（seek 门限） | ✅ 天然由音频输出驱动 |

唯一 B 天然占优的是音频输出平滑度；A 通过「偏差超过门限才 seek、否则不打断播放」的从设备策略即可达到同等听感（实现见第 8 节）。

## 6. 可观察的偏差与三层时间区分

评审和验收必须把三层时间分开记录，任何一层的数字都不能冒充另一层：

1. **实际源样本（source）**：后端解码得到的整数事实——帧 PTS（`source_us`）、PCM 采样编号、覆盖区间、`filled_sample_ranges`。浏览器不可修改，只可查询。
2. **浏览器解码/播放进度（player）**：`audio.currentTime`、`requestVideoFrameCallback`（本方案画格是 `<img>`，对应为 rAF 触发时刻）、`readyState`、`waiting/playing/stalled` 事件、seek 完成时间。
3. **用户所见画格（shown）**：rAF 回调里实际写入 DOM 的 `<img>`/占位元素，以及当时屏幕游标文本。

`tools/preview_sync/engine.py` 用确定性种子 LCG 对第 2、3 层建模（24 个种子/场景），第 1 层直接读取探针产出的真实 `clock.json`。实测摘要（每格为 24 次运行的最坏值，完整 JSON 由 runner 输出）：

| 场景 | 策略 | 画格策略错位 tick | 洞内误画帧 | 卡顿期错位 tick / 最大滞后 | 锁定残差 | 补零误标 tick |
| --- | --- | --- | --- | --- | --- | --- |
| nominal（仅 rAF ±4 ms 抖动） | A | 0 | 0 | 0 / 0 | 0 | 0 |
| nominal | B | 3 | 1 | 0 / 0 | 0 | 0 |
| WAV 漂移 4000 ppm | A | 0 | 0 | 0 / 0 | 0 | 0 |
| WAV 漂移 4000 ppm | B | 6 | 1 | 0 / 0 | — | 0 |
| 解码卡顿 120 ms（lead-pad，有伴音） | A | 0 | 0 | 0 / 0 | 0 | 0 |
| 解码卡顿 120 ms（lead-pad） | B | 200 | 1 | 110 / 121 ms | — | 音频停摆 120 tick |
| 解码卡顿 120 ms（fractional-hole，卡顿落在洞内） | A | 0 | 0 | 0 / 0 | 0 | 0 |
| 解码卡顿 120 ms（fractional-hole） | B | 0（洞内无帧可切） | 0 | 0 / 121 ms | — | 补零标注失配 388 tick |
| 初始锁定误差 80 ms | A | 0 | 0 | 0 / 0 | 0 | 0 |
| 初始锁定误差 80 ms | B | 316 | 0 | — | 82 ms | 195（lead-pad）/ 736（fractional-hole） |
| 无伴音 + 任意音频类故障 | A | 全部 0 | 0 | — | — | — |
| 无伴音 + 任意音频类故障 | B | 时钟不存在，行为未定义 | — | — | — | — |

要点解读：

- **nominal 下 B 也有 3 个错位 tick**：`currentTime` 4 ms 量化 + rAF 抖动在帧边界两侧翻转，属结构性而非偶发。
- **卡顿是 B 最危险的失败模式**：音频管线停顿时主时钟冻结，恢复后画格整体滞后且页面没有任何独立基准可以发现；A 的墙钟标尺继续走，最多表现为音频从设备短暂落后、随后一次再锚定。
- **`hole_violations`（洞内误画帧）只在 B 出现**：A 的位置在洞内查不到覆盖帧，占位是确定性输出。
- 仿真输入是摘片级短区间（0.6–0.74 s），漂移类故障的绝对值按节目长度线性放大；真实 120 s 上限节目里，4000 ppm 漂移末端偏差可达约 480 ms。

浏览器落地时需要在预览页暴露（可折叠的「时钟诊断」区，默认收起）：

- `position_programme_us`（主时钟输出）、`audio.currentTime` 与反算节目微秒之差 `audio_residual_us`；
- 最近一次再锚定时间、seek 前后残差；
- 当前 shown 帧的 `source_us` 与目标点 `target_us`；
- `waiting/stalled` 事件计数与累计时长；
- 位置是否落在画面覆盖洞 / 补零采样区间（布尔标注，直接来自映射）。

## 7. 能改变选型的条件

推荐结论 A 在以下任一条件被证据推翻时应重新评审，而不是无条件坚持：

1. **出现纯音频审片形态**：未来需求以听感为主、画面降为可选，且不再登记无伴音母带——此时 B（或 Web Audio `AudioContext.currentTime` 硬件时钟）可成为主时钟，画面降级为从设备。
2. **实测设备时钟漂移反转**：在目标工位硬件上，`performance.now()` 相对音频输出时钟的漂移稳定大于音频链路漂移（现状假设相反；需要用第 6 节诊断字段在真实工位采样至少一个验收周期）。若 A 的两次再锚定间隔因此短到影响听感（建议门限：每分钟 ≥ 6 次 seek），改为「音频硬件时钟为主、节目微秒为标注层」的双时钟。
3. **WAV 变为流式/分片且自带可靠时标**：若证据不再是完整 WAV 而是带独立 PTS 的音频流（如封装内原轨回放），B 可直接读源 PTS 而不是 `currentTime` 反算，4.2/4.3 的标定与量化劣势消失。
4. **证据映射不再随包下发**：若 `clock.json` 被裁剪、页面拿不到覆盖区间与补零范围，则 A 的覆盖标注优势无法实现——但此时 B 同样无法标注，正确动作是拒绝该裁剪而不是换时钟。
5. **验收门限收紧到采样级**：若要求声画偏差小于一个音频采样（8 kHz 下 125 µs），两种方案都不够，需要 Web Audio 按采样调度并由后端输出逐样本时间表，属另立项目。

不变量（任何选型下都不妥协）：无伴音与有伴音预览共用同一套播放模型；补零、覆盖洞、源静音三者的标注只能来自源映射，不得从 PCM 电平或画面冻结推断。

## 8. 落地方案（基于现有代码的最小改动）

后端无需改动：`preview.create()` 已把完整 `clock.json` 与 WAV 一并放入预览缓存（`slatewatch/desk/preview.py:30`）。前端新增一个静态脚本（沿用 `desk/static/intake.js` 的无依赖风格），核心状态机：

```js
// 伪代码，字段名与 clock.json 保持一致
const map = await (await fetch(`/desk/previews/${key}/clock.json`)).json(); // 需新增只读映射路由或内联在页面
const audio = document.querySelector('audio');            // 无伴音时不存在
const origin = performance.now();                          // 墙钟锚点
let startWall = origin, startProgramme = map.start_us, paused = true;

function programmePositionUs(now) {
  return paused ? startProgramme
                : startProgramme + Math.round((now - startWall) * 1000);
}
function frameAt(us) {                                      // 直接查源映射 + 覆盖
  if (!coveredByPicture(us, map)) return null;             // 洞内返回 null → 渲染占位
  return nearestFrameNotAfter(us, map.picture);
}
function tick(now) {
  const us = programmePositionUs(now);
  render(frameAt(us), us);                                  // shown 层
  markPadding(us, map.sound);                               // 补零/覆盖标注
  if (audio) {
    const want = (us - map.start_us - (map.sound.clock_offset_us ?? 0)) / 1e6;
    const residual = want - audio.currentTime;
    if (Math.abs(residual) > 0.08) audio.currentTime = want; // 80ms 门限，避免频繁 seek
    recordDiagnostic(us, residual);
  }
  requestAnimationFrame(tick);
}
```

配套页面改动：预览模板（`desk/views/preview.html`）把静态 `<figure>` 列表换成单个带数据属性的画格容器 + 缺画格占位元素；`desk/render.py` 的 `ASSETS` 登记新脚本；新增 `GET /desk/previews/{key}/clock.json` 只读路由（缓存里已有该映射，约 10 行，与现有 PNG/WAV 资产路由同构）。验收复用 `desk_walkthrough.py` 模式，新增断言：无伴音页无音频元素仍可播放、洞内渲染占位、补零区间出现标注。

## 9. 复跑

```sh
# 环境（仓库声明 Python 3.12+；本机示例用 uv 准备）
uv python install 3.12 && uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 1) 三种输入经真实服务产出源事实 + 两种时钟的确定性仿真（JSON 报告）
PYTHONPATH=. .venv/bin/python -m tools.preview_sync.runner --out /tmp/clock-report.json

# 2) 只看服务侧事实；之后可在不启动服务的情况下重放仿真
.venv/bin/python -m tools.preview_sync.runner --probe --out /tmp/probe.json
.venv/bin/python -m tools.preview_sync.runner --engine --report /tmp/probe.json --out /tmp/sim.json

# 3) 回归：现有 API 与工作台流程不受影响
.venv/bin/python -m slatewatch.walkthrough all
.venv/bin/python -m slatewatch.desk_walkthrough

# 4) 单独重生成某一输入并直接检查解码事实
.venv/bin/python -m slatewatch.reels fractional-hole /tmp/fh.mkv
```

仿真器的场景参数（漂移 ppm、卡顿时刻与长度、`currentTime` 量化、rAF 抖动、初始锁定误差）集中在 `engine.ENGINE_SCENARIOS`，随机种子固定（`seeds=range(1,25)`），改参数即可做敏感性分析，输出可直接 diff。
