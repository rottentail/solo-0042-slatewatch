# 取证归属排查演练（triage）

这是交给下一班重跑的材料，不是产品的一部分，也不改动 `slatewatch/`。

## 目录

| 路径 | 作用 |
| --- | --- |
| `inputs/` | 收到即用：网页另存的 PNG、冻结 ZIP、当前母带、对照映射、交接说明、`chain.json` |
| `report/` | `run_triage.py` 的输出：`REPORT.md` 与 `triage.json`（可重跑覆盖） |
| `key/` | 生成侧答案，**仅维护者交叉核对**，排查人员不得读取 |
| `build_case.py` | 维护者重建 `inputs/` 与 `key/`，用仓库自有素材生成器，不改产品 |
| `run_triage.py` | 排查人员入口，只读 `inputs/` 与仓库代码 |
| `check_case.py` | 维护者核对 `report/` 与 `key/` 一致，排查人员不跑 |

## 接班怎么做

```sh
python -m triage.run_triage        # 只读 inputs/，写 report/
```

先读 `inputs/notes/RECEIVED.md`，再读 `report/REPORT.md`。

## 维护者怎么重建与核对

```sh
python -m triage.build_case --force   # 重建 inputs/ 与 key/（会换掉母带字节与封包 ID）
python -m triage.run_triage
python -m triage.check_case           # 必须 passed
```

`triage/key/` 已加入 `.gitignore`。生成时写入的答案不会被 `run_triage.py`
或 `check_case.py` 的结论部分引用；`check_case.py` 只做维护者交叉核对。
