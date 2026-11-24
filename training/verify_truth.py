#!/usr/bin/env python3
"""独立核查入口：重算真值 + 真实扫描配对 + 反泄漏审计。

用法：
    .venv/bin/python training/verify_truth.py
退出码 0 表示全部关键核查通过；非 0 表示有核查失败。
"""
from pathlib import Path
import sys

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
for p in (str(REPO_ROOT), str(TRAIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lab.audit import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
