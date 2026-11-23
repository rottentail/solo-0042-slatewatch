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

`basalt` 包含片头黑场、静止画面、左声道静音、右声道满幅削顶和内部音频覆盖空洞。`revision` 缩短片头黑场并消除削顶。`picture-hole` 在画面中跳过一段时间，`no-sound` 省略伴音流，`fractional` 用 30000/1001 帧率生成画格。`headline` 的伴音从整卷零点开始、首个画面在 1.0 秒后才出现（片头只有伴音），随后画面还停过一段；`headtrim` 保留同一画面节目但把片头剪到 0.4 秒，用于验证同一章节表跨卷导入。各样片的信号布局可复现，容器字节及派生 ID 可能随生成变化。

## 章节参考轨

制作章节表的时标可能从节目画面起点（`picture_time_us`）或播放器整卷零点（`reel_time_us`）计算。导入时未声明原点的章节会同时按两种读法落位：只有一种读法落在可取画面上时章节被唯一确定（例如整卷读法落进片头只有伴音、没有画面的区间）；两种读法都可取时，章节只能呈现在一个候选范围内，不替制作方静默选边。已确定章节和候选范围章节在扫描页分两张表呈现，并都能从章节跳回该位置实际可取的技术异常。

章节与异常的关联以扫描身份为前提。把同一张章节表导入另一份剪掉片头的节目并携带旧 `links` 时，凡指向新扫描中不存在异常的关联都列为“未能承接的旧关联”，保留原异常 ID 且不会按位置就近改挂到另一条异常上。

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
| `POST /scans/{id}/chapters` | 导入制作章节表（`sheet`，每行含标题与画面/整卷两种时标及可选声明原点），可带 `links` 承接旧关联。返回已确定与候选章节的导入事件 |
| `GET /scans/{id}/chapters/track` | 读取章节参考轨：已确定章节、只能落在候选范围的章节、关联与未能承接的旧关联 |
| `POST /scans/{id}/chapter-link` | 用 `chapter_seq` 和可选 `hit_id` 把章节关联到本扫描真实存在的异常，省略 `hit_id` 即清除关联 |
| `POST /compare` | 以 `before`、`after` 扫描 ID 及可选的 `minimum_permille` 对照共享时线上的候选 |
| `POST /scans/{id}/evidence` | 按 1 至 6 个 `hit_ids`、可选 `padding_ms` 和 `frames` 生成即时 ZIP。每条证据含时标映射和可用媒体 |
| `POST /scans/{id}/seals` | 接收同一摘片选项，冻结扫描、裁决及媒体，返回封包描述符 |
| `GET /seals/{id}`、`/seals/{id}/verify`、`/seals/{id}/bundle` | 读取描述、校验包哈希与文件索引，或下载已校验的 ZIP |

上传限制为 48 MiB，源时长限 120 秒。允许的容器、编码、轨道与采样范围定义在 `slatewatch/media.py`。`coverage.sound: null` 表示未选伴音，带缺口的已选伴音有实际覆盖区间。波形证据对未覆盖采样补零，`clock.json` 单列补零区间，供复核时区别源内数字静音。画格是为判读技术信号生成的灰阶缩略图。

输入、未找到和冲突分别返回 400、404、409。工位并发槽位用完返回 503。不同规则或流选择可产生不同扫描，原始母带和既有冻结包可继续读取。

## 复核运行

`python -m slatewatch.walkthrough all` 在临时保管库里生成素材并启动真实回环 HTTP 服务，逐步打印 API 观测。也可以把 `all` 换成 `intake`、`policy`、`scan`、`intent`、`review`、`compare`、`evidence`、`publish`、`catalog`、`verify` 或 `chapters`。`python -m slatewatch.desk_walkthrough` 走工作台的 HTML 入口并复核实际 PNG、WAV、冻结包和章节参考轨。自动测试套件暂缓。
