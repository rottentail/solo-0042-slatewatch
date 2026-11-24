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

「版次比对」显示同节目时标上的匹配和各版独有候选。「冻结包」保留冻结当时的结论，并提供独立核验与下载。浏览器预览默认持久化到保管库 `previews/`，软件重启后同一 `/desk/previews/<token>` 地址仍可复听与核对；旧程序的进程内预览令牌不写盘，切换后会得到明确的缺失提示。有长期交接需求时，仍应使用即时证据下载或冻结包。

## 预览存储与切换演练

预览目录 `<root>/previews/<token>/preview.json` 保存原摘片选项（`hit_ids`、`padding_ms`、`frames`）、完整 `clock` 映射（含 `filled_sample_ranges` 补样范围）、资产 SHA-256 索引，以及 `assets/` 下转换好的 PNG 与 `sound.wav`。写入采用临时目录加原子改名。预览地址沿用旧缓存的 32 位十六进制令牌；同一摘片表单再次提交会签发新令牌，对旧地址执行 `rebuild` 则按留档选项原地重建。

地址状态机有四种可观测状态，HTML 页用 `data-preview-state` 与中文标题区分。`GET /desk/previews/<token>/state`（另有 `/status` 语义状态码、`/verify` 重算 SHA-256 核对、`.json`、`/manifest.json`、`?format=json` 和 `/slatewatch/v1/previews/<token>` 别名）返回同一份 JSON 文档：

| `state` | `/state` 与 HTML | `/status` | 含义与处置 |
| --- | --- | --- | --- |
| `ready` | 200 | 200 | 描述符与 PNG/WAV 完整，可复听、核对哈希与补样范围 |
| `expired` | 200 + 状态体 | 410 | TTL、手动 `expire`、容量淘汰或资产丢失；选项与补样范围仍留档，源可用时可 `rebuild`；资产字节地址返回 410 |
| `source_unavailable` | 200 + 状态体 | 503 | `objects/<master_sha256>` 源母带不在位（或母带描述符缺失）；已落盘 PNG/WAV 仍可核对，但不能重新摘片；`rebuild` 返回 503 |
| `missing` | 404 | 404 | 地址无描述符，常见于切换前旧进程内存令牌；按提示重新摘片即可 |

JSON 文档同时给出 `state`、`status`、`ok/ready/expired/source_unavailable` 布尔别名与建议的 `http_status`，便于不同校验脚本取值。

可用 `--preview-store memory` 回到旧程序的纯进程缓存，用 `--preview-ttl <秒>` 调整保留期（`0` 为不过期），也可用环境变量 `SLATEWATCH_PREVIEW_STORE`、`SLATEWATCH_PREVIEW_TTL_SECONDS` 配置。运维可直接把某条 `preview.json` 的 `expires_at` 改成过去时间模拟到期。

一条命令演练完整切换（真实样片与回环 HTTP，临时保管库，退出码非 0 即失败）：

```sh
python -m slatewatch.switchover_walkthrough
python -m slatewatch.switchover_walkthrough --root /tmp/slatewatch-drill   # 保留现场
```

演练依次验证：旧 `memory` 进程建预览和冻结包 → 停服 → `file` 模式同库启动，旧内存令牌 404 且带迁移提示、旧冻结包验真通过 → 再重启后同地址的选项、PNG/WAV 哈希和补样范围与新证据 ZIP 逐项一致 → `expired`（410）与移除源对象后的 `source_unavailable`（503）互异且恢复路径可用 → 切回 `memory` 模式冻结包仍验真 → 用 `git archive HEAD` 导出的切换前程序独立 `seal.verify` 同一保管库成功。`python -m slatewatch.preview_walkthrough` 与 `cutover_walkthrough` 是同一演练的别名。冻结包格式、SQLite 描述符和 `objects/` 布局未改，新增 `previews/` 目录不影响旧程序验真；复制整个保管库时会一并带走预览。

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
| `POST /scans/{id}/evidence` | 按 1 至 6 个 `hit_ids`、可选 `padding_ms` 和 `frames` 生成即时 ZIP。每条证据含时标映射和可用媒体 |
| `POST /scans/{id}/seals` | 接收同一摘片选项，冻结扫描、裁决及媒体，返回封包描述符 |
| `GET /seals/{id}`、`/seals/{id}/verify`、`/seals/{id}/bundle` | 读取描述、校验包哈希与文件索引，或下载已校验的 ZIP |

上传限制为 48 MiB，源时长限 120 秒。允许的容器、编码、轨道与采样范围定义在 `slatewatch/media.py`。`coverage.sound: null` 表示未选伴音，带缺口的已选伴音有实际覆盖区间。波形证据对未覆盖采样补零，`clock.json` 单列补零区间，供复核时区别源内数字静音。画格是为判读技术信号生成的灰阶缩略图。

输入、未找到和冲突分别返回 400、404、409。工位并发槽位用完返回 503。不同规则或流选择可产生不同扫描，原始母带和既有冻结包可继续读取。

## 复核运行

`python -m slatewatch.walkthrough all` 在临时保管库里生成素材并启动真实回环 HTTP 服务，逐步打印 API 观测。也可以把 `all` 换成 `intake`、`policy`、`scan`、`intent`、`review`、`compare`、`evidence`、`publish`、`catalog` 或 `verify`。`python -m slatewatch.desk_walkthrough` 走工作台的 HTML 入口并复核实际 PNG、WAV 和冻结包。自动测试套件暂缓。
