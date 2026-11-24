#!/usr/bin/env python3
"""可复跑构建入口：生成双音轨母带、盲包与答案底册。

用法：
    .venv/bin/python training/build_blind_lab.py
"""
from pathlib import Path
import sys

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
for p in (str(REPO_ROOT), str(TRAIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lab.builder import main  # noqa: E402

if __name__ == "__main__":
    main()
