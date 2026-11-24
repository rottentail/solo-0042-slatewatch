"""Rerunnable clock-selection evidence for the follow-picture / relisten preview.

These tools do not change the scan service. They reuse ``slatewatch.reels`` to
regenerate three known inputs, drive the real HTTP service through evidence
extraction (``probe.py``), and simulate two candidate page master clocks on the
exact mappings the browser will receive (``engine.py``).

  python -m tools.preview_sync.runner                       # full probe + simulation
  python -m tools.preview_sync.runner --probe --out f.json  # service/API observations only
  python -m tools.preview_sync.runner --engine --report f.json  # replay simulation offline
"""
