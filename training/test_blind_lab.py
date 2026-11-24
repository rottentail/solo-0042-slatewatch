#!/usr/bin/env python3
"""盲标练习交付契约测试（仅用标准库 unittest；有 pytest 也可收集）。

没有 PyAV 时自动 skip，避免在未装媒体依赖的解释器里报收集错误。
直接运行：
    .venv/bin/python training/test_blind_lab.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
for p in (str(REPO_ROOT), str(TRAIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import av  # noqa: F401
    HAVE_AV = True
except Exception:  # pragma: no cover - 环境无 av 时跳过
    HAVE_AV = False


@unittest.skipUnless(HAVE_AV, "PyAV 不可用，跳过盲标媒体契约测试")
class BlindLabContract(unittest.TestCase):
    truth = None
    rep = None
    leak = None

    @classmethod
    def setUpClass(cls) -> None:
        from lab import audit, model
        cls.model = model
        cls.audit = audit
        if not model.TRUTH_JSON.exists():
            from lab import builder
            builder.build()
        cls.truth = json.loads(model.TRUTH_JSON.read_text("utf-8"))
        cls.rep = audit.Report()
        observed = audit.recompute_all(cls.truth, cls.rep)
        audit.service_scans(cls.truth, cls.rep, observed)
        cls.leak = audit.leak_audit(cls.truth, cls.rep)

    def test_all_critical_checks_pass(self) -> None:
        failed = [f"{c['id']}: {c['detail']}" for c in self.rep.checks
                  if not c["ok"] and c["severity"] == "critical"]
        self.assertEqual(failed, [])

    def test_two_groups_and_balance(self) -> None:
        items = self.truth["items"]
        sync = [i for i in items if i["group"] == "sync"]
        amb = [i for i in items if i["group"] == "ambiguous"]
        self.assertEqual(len(sync), 4)
        self.assertEqual(len(amb), 2)
        labels = sorted(i["correct_label"] for i in sync)
        self.assertEqual(labels, ["jia", "jia", "yi", "yi"])
        for it in amb:
            self.assertIsNone(it["correct_label"])
            self.assertTrue(it["assets"]["wav_bytes_equal"])

    def test_every_item_has_crossed_plateaus(self) -> None:
        for it in self.truth["items"]:
            self.assertEqual(set(it["plateau_us"]), {"L1", "L2"})
            self.assertIn(it["main_hole"], ("L1", "L2"))
            self.assertEqual(it["role_hole"]["main"], it["main_hole"])
            # 参考轨的无覆盖平台必须与主混互换。
            self.assertNotEqual(it["main_hole"], it["role_hole"]["ref"])

    def test_distributable_zip_clean(self) -> None:
        import zipfile
        self.assertTrue(self.model.BLIND_ZIP.exists())
        with zipfile.ZipFile(self.model.BLIND_ZIP) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith("index.html") for n in names))
        bad = [n for n in names if ".." in Path(n).parts
               or n.endswith(".mkv") or n.endswith("truth.json")
               or "/lab/" in n or n.endswith(".py")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
