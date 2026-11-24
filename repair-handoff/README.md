# R045 返修棚三格位交接样件

这个目录保存一次可复跑的完整演练：从固定生成的源母带解码出候选，做出**可离线发出的三格位交接 ZIP**，并在发件端保留完整台账、母带和第二种取法。

## 直接取用

- **发给返修棚的交接件**：[`outgoing/repair-desk-R045.zip`](outgoing/repair-desk-R045.zip)
  - 解压后打开 `handoff-sheet.html`（也可看 `handoff-sheet.md`）。
  - 恰好 3 个独立画格：`frames/slot-1.png`、`slot-2.png`、`slot-3.png`。
  - 恰好 1 个单声道 WAV：`audio/R045-primary-mono.wav`。
  - 随单地址清单：`addresses.json` 与可读版 `addresses.md`。
  - 离线自检：解压根目录运行 `python3 verify_handoff.py`；也可运行 `sha256sum -c SHA256SUMS`。
  - 不需要母带、网络或 Slatewatch 服务端。

- **发件端备查材料**：[`sender-reference/`](sender-reference/)（整目录归档：[`sender-reference-R045.zip`](sender-reference-R045.zip)）
  - `source/repair-freeze-lab-r045.mkv`：真实生成并被解码的源母带。
  - `source/scan.json`、`source/inspect.json`：Slatewatch 扫描与流描述。
  - `frame-ledger/`：洞两侧全部候选源格、PNG、CSV/JSON 地址和哈希。
  - `selection-memo.md`：首选取法、另一种三格位取法，以及何时优先换图/换 WAV。
  - `alternative-selection/`：第二套已通过离线校验的 3 格 + 1 WAV 包。
  - `verification-report.txt`：两套包的实际校验输出。
  - `repair-desk-R045.zip`：发出 ZIP 的发件端副本。

## 样件中的关键事实

- 无图段：`5,000,000–6,000,000 μs`。
- `V08-03500ms` 是覆盖 `3.5–4.5 s` 的同一长格；目标 `T-L1=3.7 s`、`T-L2=4.2 s` 都映射到它。
- 洞前 `V09-04500ms` 与洞后 `V10-06000ms` 的 80×48 灰阶像素 SHA-256 完全相同，但源格 ID、PTS 和覆盖区间不同；没有一个源格跨过无图段。
- WAV 同时包含：
  - 源覆盖内的实录数字零声 `SIL-REC`（`4.7–5.0 s`）；
  - 源 PCM 无覆盖、摘片器补出的零段 `FILL-INNER`（`5.6–5.8 s`）和 `FILL-LATER`（`6.8–7.1 s`）；
  - 无图段前后的源内强标记音 `L-BEFORE`、`R-AFTER`，用于证明两侧伴音确实在源 PCM 覆盖中。

## 复跑

正式环境按项目要求使用 Python 3.12+ 与锁定版 PyAV：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python repair-handoff/build_repair_handoff.py
```

构建器会重建 `outgoing/` 与 `sender-reference/`，并在结束前实际运行两套纯标准库校验器。系统 Python 3.11 环境中可用 ABI 兼容的 PyAV 18.x，并通过 `PYTHONPATH` 提供：

```sh
PYTHONPATH=/tmp/slatewatch-pydeps:/workspace \
  python3 repair-handoff/build_repair_handoff.py
```

收件方校验器 `verify_handoff.py` 只使用 Python 标准库，不需要安装 PyAV。
