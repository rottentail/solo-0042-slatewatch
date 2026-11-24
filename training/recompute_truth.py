#!/usr/bin/env python3
"""只做“独立重算真值”，把从媒体字节解出的观测写到 debrief/recomputed_truth.json。

不依赖生成器的判定函数；可单独运行：
    .venv/bin/python training/recompute_truth.py [--stdout]
"""
from pathlib import Path
import json
import sys

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
for p in (str(REPO_ROOT), str(TRAIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lab import model  # noqa: E402
from lab.audit import Report, RECOMPUTED_JSON, recompute_all  # noqa: E402


def main() -> int:
    truth = json.loads(model.TRUTH_JSON.read_text("utf-8"))
    rep = Report()
    observed = recompute_all(truth, rep)
    RECOMPUTED_JSON.parent.mkdir(parents=True, exist_ok=True)
    RECOMPUTED_JSON.write_text(json.dumps(observed, ensure_ascii=False, indent=2), "utf-8")
    print(f"[recompute] observations -> {RECOMPUTED_JSON}")
    print(f"[recompute] {sum(c['ok'] for c in rep.checks)}/{len(rep.checks)} "
          f"integrity observations consistent with sealed key")
    if "--stdout" in sys.argv:
        print(json.dumps(observed, ensure_ascii=False, indent=2))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
