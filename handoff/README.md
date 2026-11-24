# R044 外部班组声画错位交接（交付方总说明）

> 本文件及 `case-r044/coordinator/` 是**交付方/汇合方材料**，包含双方结论和专用台账，不能整包发给任何一家外部班组。
> 外发时只发送下面两个 ZIP 中的一个：
>
> - 画面方：[`case-r044/packs/picture-team-pack.zip`](case-r044/packs/picture-team-pack.zip)
> - 伴音方：[`case-r044/packs/sound-team-pack.zip`](case-r044/packs/sound-team-pack.zip)

本演练用仓库内的标准库生成器制作可实物打印、可听辨、可哈希复核的素材。生成器是
[`../slatewatch/handoff_lab.py`](../slatewatch/handoff_lab.py)，沿用 `slatewatch/reels.py`
的 25 fps、8 kHz 采样坐标约定，但不需要安装 PyAV。

## 1. 两家实际收到的材料

### 画面方（只能看纸 / PNG / 画面记录）

ZIP：`case-r044/packs/picture-team-pack.zip`

- `picture-team/picture-handout.pdf`：18 页 A4 打印件，含画格和画面洞记录页；
- `picture-team/print-sheet.html`：同一套材料的可打印网页；
- `picture-team/frames/F021.png … F029.png`：第一次相似动作的连续画格，准确接触格为 `F025`；
- `picture-team/frames/F047.png … F050.png`、`F055.png … F058.png`：第二次动作前后画格；
- `picture-team/picture-roll.csv`：纸面卷号记录，明确 `F051–F054` 是画面洞；
- `picture-team/picture-return-form.csv` / `.md`：可填写回报。

画面包不含 WAV、音频说明、汇合台账、返修单或伴音方回件。

### 伴音方（只收普通 WAV）

ZIP：`case-r044/packs/sound-team-pack.zip`

- `sound-team/alignment-call.wav`：RIFF/WAVE、PCM16、单声道、8000 Hz、32,000 采样；
- `sound-team/README-伴音方.md`：说明零基样本地址和听辨代码；
- `sound-team/listening-return-form.csv` / `.md`：可填写回报。

WAV 只有标准 `fmt ` 和 `data` chunk；独立校验确认没有 `LIST/INFO`、`id3 `、`bext`
或自定义 chunk。伴音方不需要、也不能读取自定义元数据或产品内时标。正式地址定义为：

```text
address 0 = WAV data 中的第一个单声道采样
source_us = address * 125        # 仅交付方台账使用；不告诉 WAV 接收方作为前置结论
```

分发包 SHA-256（当前确定性产物）：

```text
462e4c1a7084eac63810efc201b0563695c02d73cba7d58df5669e092893eb00  picture-team-pack.zip
dcbb1338ddb84ea7e3d5ec616ebd1b5c98b042e4d1494a36f2495386606ccaa4  sound-team-pack.zip
54894606dcb7ce14291a857f863220d228070c9ccf24b73c97ca585ae961be54  alignment-call.wav
```

可用以下命令重新计算：

```sh
sha256sum handoff/case-r044/packs/*.zip handoff/case-r044/sound-team/alignment-call.wav
```

## 2. 实际可填写的回报

空白表随各自 ZIP 发出：

- 画面方：`picture-team/picture-return-form.csv`
- 伴音方：`sound-team/listening-return-form.csv`

也提供同名 Markdown 表，供打印后手填。

本次可复现演练已经放入两份“班组回件”（由生成器按素材信号事实确定性代填，用作汇合演示；
真实项目中应替换为外部班组交回的文件）：

- `case-r044/returns/inbox/picture-return-R044.csv`
- `case-r044/returns/inbox/sound-return-R044.csv`

回报中没有互相引用：画面件不提 WAV 样本结论，伴音件不提画格号或画面结论。

## 3. 交付方汇合结果

运行：

```sh
python3 -m slatewatch.handoff_lab reconcile --root handoff/case-r044
```

生成：

- 唯一一件真错位返修单：
  - `case-r044/coordinator/repair-orders/RO-R044-01.md`
  - `case-r044/coordinator/repair-orders/RO-R044-01.json`
- 一个待核查夹，收容缺证和相似但错配的回报：
  - `case-r044/coordinator/verification-cases/VC-R044-02.md`
  - `case-r044/coordinator/verification-cases/VC-R044-02.json`
- 汇总：
  - `case-r044/coordinator/reconciliation-report.md`
  - `case-r044/coordinator/reconciliation-report.json`

### 准予返修的真错位

- 画面回报：`P-R044-01`，准确接触格 `F025`；交付方映射为 `1,000,000 us`。
- 伴音回报：`A-R044-01`，WAV 零基样本地址 `8960`；交付方映射为 `1,120,000 us`。
- 声音晚 `120 ms`，超过 `±80 ms` 容差。
- 返修动作：伴音相对画面提前 `960` 个 8 kHz 样本，即 `120 ms`。
- 因此只开 `RO-R044-01` 这一张返修单。

### 留待核查的两类回报

1. **缺证：`P-R044-02`**  
   纸面编号从 `F050` 跳到 `F055`，`F051–F054` 是画面洞。班组只能给出括号，
   不能指出唯一接触格；不能把推测格写成画面证据。

2. **相似但错配：`A-R044-02`**  
   样本 `17200` 有类似敲击起点的短咔声，但交付方台账显示它是零补样区间
   `[16400, 17200)` 的后边界，缺少真实敲击的低频主体和余响。若单凭相邻姿势，
   把 `F050` 与 `17200` 配对，会得到看似 `+150 ms` 的第二处延迟；该配对无效。

另有 `A-R044-03` 在样本 `17680` 听到第二次完整敲击，可作为后续核查线索；
但画面准确接触格仍缺失，所以 `VC-R044-02` 继续扣留，不开第二张返修单，也不并入
`RO-R044-01`。

## 4. 独立复跑依据

### 一键生成、汇合并校验

在仓库根目录执行：

```sh
python3 -m slatewatch.handoff_lab all --root handoff/case-r044
```

该命令只使用 Python 标准库，会：

1. 重新生成全部 PNG、PDF、HTML、CSV 和普通 WAV；
2. 重新打两个隔离 ZIP；
3. 写入交付方专用 `private-ledger.json`；
4. 汇合 inbox 中的两份回报并生成返修单/待核查夹；
5. 校验哈希、WAV chunk、补样区间、攻击/余响能量、ZIP 隔离和最终单据数量。

单独校验：

```sh
python3 -m slatewatch.handoff_lab verify --root handoff/case-r044
```

当前结果为 `ok: true`，38 项检查全部通过，并写出
`case-r044/coordinator/verification-result.json`。

### 干净目录的确定性复跑

```sh
rm -rf /tmp/slatewatch-hh-a /tmp/slatewatch-hh-b
python3 -m slatewatch.handoff_lab all --root /tmp/slatewatch-hh-a
python3 -m slatewatch.handoff_lab all --root /tmp/slatewatch-hh-b
diff -r /tmp/slatewatch-hh-a /tmp/slatewatch-hh-b
```

两个干净目录应字节一致；ZIP 内时间固定为 1980-01-01，避免归档时间干扰复跑。

### 不依赖本仓库结论的伴音抽查

任何标准 WAV 工具都可读取。Python 标准库示例：

```sh
python3 - <<'PY'
import wave
p='handoff/case-r044/sound-team/alignment-call.wav'
with wave.open(p,'rb') as w:
    print(w.getnchannels(), w.getsampwidth()*8, w.getframerate(), w.getnframes())
PY
```

应输出：

```text
1 16 8000 32000
```

## 5. 外发边界检查清单

- [x] 画面 ZIP 中没有 `.wav`、音频目录、台账、返修单或伴音回件。
- [x] 伴音 ZIP 中没有 PNG/PDF/HTML 画格、画面记录、台账或画面回件。
- [x] WAV 无自定义元数据 chunk；回报字段只要求 WAV 起点后的样本地址和听辨结果。
- [x] 两份班组回报均未引用对方结论。
- [x] 真错位形成唯一返修单 `RO-R044-01`。
- [x] 缺证与相似但错配项进入 `VC-R044-02`，状态为 `held_needs_verification`。
