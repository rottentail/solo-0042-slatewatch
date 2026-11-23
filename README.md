# Slatewatch

Slatewatch 是面向录播交付的节目母带技审台。浏览器工作台使用 HTML、CSS 和少量 JavaScript，由 Python 和 PyAV 提供检测与证据。技术人员可以登记母带与规则、查看画面和各声道覆盖、复核技术候选、对照两轮扫描，并固定当时的审片结论和媒体证据。

## 运行

需要 Python 3.12 或以上和 PyAV。安装 wheel 时无需另装 `ffmpeg` 命令行。启动前请准备有写权限的本地磁盘目录。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m compileall -q slatewatch
python -m slatewatch.http --root ./slatewatch-vault
```

服务默认监听 `127.0.0.1:8974`，可通过 `--port`、`--host` 和 `--slots` 改变监听及并行请求数。它设计给受信任的本地审片工位。保管库含 SQLite 描述符和 SHA-256 寻址的媒体、封包字节对象。迁移数据时，先停止服务再复制整个保管库。

浏览器打开 <http://127.0.0.1:8974/desk>。在「母带与规则」登记媒体和规则后开始扫描。点选时间轴或候选列表中的异常可以播放伴音、查看样本对应的源画格，也可记录制作意图和人工裁决。浏览器画格以 PNG 展示，原 ZIP 仍包含 PGM 和时间映射。伴音未选轨会单独标明，摘片合成的零采样按原轨范围列出。

「版次比对」显示同节目时标上的匹配和各版独有候选。「冻结包」保留冻结当时的结论，并提供独立核验与下载。浏览器预览是当前进程内的临时结果。有长期交接需求时，使用即时证据下载或冻结包。

## 一次审片

另开终端并激活虚拟环境。下面的 `MASTER_ID`、`POLICY_ID`、`SCAN_ID` 和 `HIT_ID` 用上一步响应里的实际 ID 替换。

```sh
python -m slatewatch.reels basalt /tmp/basalt.mkv
BASE=http://127.0.0.1:8974/slatewatch/v1
curl -fsS "$BASE/health"
curl -fsS "$BASE/masters?label=Basalt" -H 'Content-Type: application/octet-stream' --data-binary @/tmp/basalt.mkv
curl -fsS "$BASE/policies" -H 'Content-Type: application/json' -d '{"label":"工位默认"}'
curl -fsS "$BASE/scans" -H 'Content-Type: application/json' -d '{"master_id":"MASTER_ID","policy_id":"POLICY_ID"}'
curl -fsS "$BASE/scans/SCAN_ID/review"
curl -fsS "$BASE/scans/SCAN_ID/evidence" -H 'Content-Type: application/json' -d '{"hit_ids":["HIT_ID"],"padding_ms":80,"frames":3}' -o evidence.zip
```

`basalt` 包含片头黑场、静止画面、左声道静音、右声道满幅削顶和内部音频覆盖空洞。`revision` 缩短片头黑场并消除削顶。`picture-hole` 在画面中跳过一段时间，`no-sound` 省略伴音流，`fractional` 用 30000/1001 帧率生成画格。各样片的信号布局可复现，容器字节及派生 ID 可能随生成变化。

## 接口

路径以 `/slatewatch/v1` 为前缀。POST 除母带上传外接收 JSON，时间区间用整数微秒 `[start_us, end_us)`。扫描及封包操作可同步返回媒体计算结果，大文件请求可延长客户端读取超时。

| 方法与路径 | 输入和结果 |
| --- | --- |
| `GET /health` | 确认工位服务就绪 |
| `POST /masters` | `application/octet-stream`，查询参数 `label`，可提供 `picture` 流索引，`sound` 为索引、`first` 或 `none`。返回素材哈希、选定流和流表 |
| `GET /masters/{id}` | 读取已登记流描述及来源 |
| `POST /policies`、`GET /policies/{id}` | 用 `label` 和可选 `thresholds` 生成规则版，或读取旧版。刻度及默认值见 `slatewatch/rules.py` |
| `POST /scans`、`GET /scans/{id}` | 以 `master_id` 和 `policy_id` 获取覆盖区间、候选及固定规则，也可读取已存扫描 |
| `POST /scans/{id}/intents` | 用 `kind`、`start_us`、`end_us`、`reason` 和可选 `channel` 登记有意的黑场、冻帧或静音 |
| `POST /scans/{id}/rulings` | 用 `hit_id`、`outcome`、`reason` 和 `reviewer` 操作单个候选。`outcome` 为 `accept`、`hold` 或 `reset` |
| `GET /scans/{id}/review` | 读取按当前意图及裁决投影的复核状态 |
| `POST /compare` | 以 `before`、`after` 扫描 ID 及可选的 `minimum_permille` 对照共享时线上的候选 |
| `POST /scans/{id}/delivery` | 按所选声道做等长滑动窗滚动交付检查，见下 |
| `POST /scans/{id}/evidence` | 按 1 至 6 个 `hit_ids`、可选 `padding_ms` 和 `frames` 生成即时 ZIP。每条证据含时标映射和可用媒体 |
| `POST /scans/{id}/seals` | 接收同一摘片选项，冻结扫描、裁决及媒体，返回封包描述符 |
| `GET /seals/{id}`、`/seals/{id}/verify`、`/seals/{id}/bundle` | 读取描述、校验包哈希与文件索引，或下载已校验的 ZIP |

上传限制为 48 MiB，源时长限 120 秒。允许的容器、编码、轨道与采样范围定义在 `slatewatch/media.py`。`coverage.sound: null` 表示未选伴音，带缺口的已选伴音有实际覆盖区间。波形证据对未覆盖采样补零，`clock.json` 单列补零区间，供复核时区别源内数字静音。画格是为判读技术信号生成的灰阶缩略图。

## 滚动交付检查

`POST /scans/{id}/delivery` 面向交付方按等长滑动时间段复核所选声道（`channel` 从 1 起）。可选参数为 `window_ms`（50–10000，默认 250）、`hop_ms`（不大于窗长，默认等于窗长）、`min_measured_permille`（1–1000，默认 900）、`clip_permille`（默认沿用规则版）与 `peak_code_limit`（1–32768，默认 32767）。窗端点吸附到原采样网格，响应里的 `first_source_sample`/`end_source_sample` 可直接回到原 PCM 采样。

每窗同时报告峰值代码、RMS、数字静音比例与满幅采样比例，因此异常短峰不会被长段平均吞掉。静音与满幅比例的分母是窗内**实测采样数**，覆盖洞补零不按安静计入。每窗给出三种状态：

- `measured`：整窗都有实测，量测为精确值，只有精确窗才会按限值判 `clip_ratio` 或 `peak_code` 违规；
- `bounded`：窗内含覆盖洞、实测占比达到下限要求，量测只知下界（`bounds: lower_bound`），真实值可能更高；
- `blank`：整窗无实测（断流），不产出任何量测。

实测占比低于 `min_measured_permille` 的窗带 `coverage_hole` 原因，空白窗带 `unmeasured_window` 原因；二者与精确窗的限值违规一样令 `blocking: true`，并汇总在 `blocked_window_indexes` 中，即真正阻塞送审的段落。`hole_sample_ranges` 与 `covered_sample_ranges` 用采样区间标出洞与实测出处。浏览器在「滚动交付」页按相同口径展示，未选伴音的扫描拒绝该检查。


输入、未找到和冲突分别返回 400、404、409。工位并发槽位用完返回 503。不同规则或流选择可产生不同扫描，原始母带和既有冻结包可继续读取。

## 复核运行

`python -m slatewatch.walkthrough all` 在临时保管库里生成素材并启动真实回环 HTTP 服务，逐步打印 API 观测。也可以把 `all` 换成 `intake`、`policy`、`scan`、`intent`、`review`、`compare`、`evidence`、`publish`、`delivery`、`catalog` 或 `verify`。`python -m slatewatch.desk_walkthrough` 走工作台的 HTML 入口并复核实际 PNG、WAV、滚动交付页和冻结包。自动测试套件暂缓。
