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
| `POST /scans/{id}/intents` | 用 `kind`、`start_us`、`end_us`、`reason`、声音指标必填的 `channel` 和可选 `record`（制作记录号）登记有意的黑场、冻帧或静音 |
| `POST /scans/{id}/intent-corrections` | 用 `intent_seq`、更正后的 `start_us`/`end_us`、`reason` 和可选 `record` 追加制作方更正。原意图保留为 `superseded`，更正事件生成带 `corrected_from_seq` 链的新现行意图；同一意图只能更正一次 |
| `POST /scans/{id}/rulings` | 用 `hit_id`、`outcome`、`reason` 和 `reviewer` 操作单个候选。`outcome` 为 `accept`、`hold` 或 `reset`，全部事件保留在裁决史中 |
| `GET /scans/{id}/review` | 读取按当前意图台账、更正链及裁决投影的复核状态，含逐微秒片段的采信依据 |
| `POST /compare` | 以 `before`、`after` 扫描 ID 及可选的 `minimum_permille` 对照共享时线上的候选 |
| `POST /scans/{id}/evidence` | 按 1 至 6 个 `hit_ids`、可选 `padding_ms` 和 `frames` 生成即时 ZIP。每条证据含时标映射和可用媒体 |
| `POST /scans/{id}/seals` | 接收同一摘片选项，冻结扫描、裁决及媒体，返回封包描述符 |
| `GET /seals/{id}`、`/seals/{id}/verify`、`/seals/{id}/bundle` | 读取描述、校验包哈希与文件索引，或下载已校验的 ZIP |

上传限制为 48 MiB，源时长限 120 秒。允许的容器、编码、轨道与采样范围定义在 `slatewatch/media.py`。`coverage.sound: null` 表示未选伴音，带缺口的已选伴音有实际覆盖区间。波形证据对未覆盖采样补零，`clock.json` 单列补零区间，供复核时区别源内数字静音。画格是为判读技术信号生成的灰阶缩略图。

输入、未找到和冲突分别返回 400、404、409。工位并发槽位用完返回 503。不同规则或流选择可产生不同扫描，原始母带和既有冻结包可继续读取。

## 交接、更正与采信片段

制作意图只追加、不改写。每条意图可带 `record` 制作记录号；两条来自不同制作记录的重叠意图可以合起来覆盖一个候选。制作方更正时调用 `intent-corrections`：系统追加更正事件，把原意图标记为 `superseded` 并保留原区间、原理由，再生成带 `corrected_from_seq` 链的现行意图。`review` 的 `intents` 是带 `active`/`superseded` 状态的台账，`intent_events` 与 `intent_corrections` 保留原始事件流。

`review` 同时把每个候选切成微秒片段 `segments`，交接人可逐段判断：

- `expected` 仍有采信依据：实测区间内存在适用指标、适用声道的现行意图；片段附带支持它的意图序号。
- `pending` 恢复待查：有实测画面或声音，但更正后已无意图支持，需要重新核对。
- `unmeasured` 从无可证实画面：片段落在覆盖空洞（画面缺口或伴音缺口）上，任何人工裁决都不会把它改成已测。

人工裁决优先于自动投影，`accept` 后 `reset` 会回落到按当前意图重新判断，但裁决事件全部留在 `rulings` 里，先前为什么放行仍可查。声音意图必须指定声道，另一声道的意图不能支持本声道候选。冻结包封入当时的完整台账与裁决史；更正之后再冻结会得到不同的包，而更正前已冻结的包仍可独立验真并保留当时的放行结论。旧微秒意图事件、旧扫描和已冻结交接件继续按原样可读。

## 复核运行

`python -m slatewatch.walkthrough all` 在临时保管库里生成素材并启动真实回环 HTTP 服务，逐步打印 API 观测。也可以把 `all` 换成 `intake`、`policy`、`scan`、`intent`、`review`、`compare`、`evidence`、`publish`、`catalog`、`verify` 或 `handoff`。`handoff` 用一轮片头黑场和一次带声道限定的静音意图，验证两条制作记录的重叠覆盖、更正后片段的采信/待查/空洞分类、accept 后 reset 的裁决史，以及更正前后两个冻结包各自可读。`python -m slatewatch.desk_walkthrough` 走工作台的 HTML 入口并复核实际 PNG、WAV、冻结包、意图台账和更正表单。自动测试套件暂缓。
