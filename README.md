# Slatewatch

Slatewatch 是面向录播交付的节目母带技术审片服务。它接收已经剪定的音视频文件，标出黑场、冻帧、滚动或重复的周期条纹、单声道静音、削顶和音画覆盖空洞。技术人员可以登记制作意图、提交人工裁决、对照两轮扫描，再把指定异常的画格和波形连同当时的审片结论打成可校验的证据包。

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

`basalt` 包含片头黑场、静止画面、左声道静音、右声道满幅削顶和内部音频覆盖空洞。`revision` 缩短片头黑场并消除削顶。`picture-hole` 在画面中跳过一段时间，`no-sound` 省略伴音流，`fractional` 用 30000/1001 帧率生成画格。`stripes` 专门覆盖淡条纹：水平滚动、相隔 1.2 秒硬切的同周期水平滚动、静止的水平重复纹理、静止的垂直重复纹理和垂直滚动各占 1.2 秒，条纹幅度仅约 ±6 灰阶，声轨干净以免干扰判读。各样片的信号布局可复现，容器字节及派生 ID 可能随生成变化。

### 周期条纹

条纹候选（`kind: "stripe"`）从缩小的灰阶画格逐帧拟合空间周期：横向按行均值、纵向按列均值做单频最小二乘，只有该频率解释了足够大的亮度方差、幅度不低于 `stripe_luma_span` 的半个峰峰值、且重复达到三个周期时才记入量测格。淡不淡不是门槛：一两个灰阶的干净条纹照样计入，平滑渐变或杂乱纹理即使对比很强也不会通过。

连续帧只有方向、周期相同且相位步进平稳时才并成一段。静止纹理的相位几乎不动，滚动条纹每帧前进固定相位；两者之间的硬切即使方向、周期完全一致也会断开，因为判据还要相位步进保持“静止”或“移动”的同一状态。任一帧失去周期、覆盖中断或相位跳变同样结束该段，所以硬切两侧的纹理不会被拼成同一段技术干扰。

候选带 `direction`（`horizontal`/`vertical`，指条纹横躺还是竖立）、`period`（缩小栅格上的像素周期，源周期按其缩放比折算）、`purity_permille`（本段末帧的方差占比），以及 `peak_us`——本段中条纹最明显的原时标画格。区间、计数与幅度上下限沿用其它候选的字段。灵敏度、方差占比门槛与最短段长由规则版中的 `stripe_luma_span`、`stripe_bins_permille`、`stripe_min_ms` 给出。

条纹段摘片时，`clock.json` 的 `peak_us` 指向最明显的画格，`picture` 映射中会有一个画格的 `target_us` 正好取该源时刻，便于直接核对方向与周期。

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

`python -m slatewatch.walkthrough all` 在临时保管库里生成素材并启动真实回环 HTTP 服务，逐步打印可核对的观测。也可以把 `all` 换成 `intake`、`policy`、`scan`、`intent`、`review`、`compare`、`evidence`、`publish`、`catalog`、`verify` 或 `stripes`。`stripes` 用硬切样片核对横竖条纹的方向、区间和原时标画格。这里依赖真实媒体和产品入口，自动测试套件暂缓。
