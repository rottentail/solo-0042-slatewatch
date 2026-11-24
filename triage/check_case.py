"""Maintainer-only cross-check: runner output vs generator answer key.

This is the *only* file that reads both ``triage/key`` and the runner report.
It never contributes evidence to the hand-over; it only proves the drill is
solvable from ``inputs/`` alone and that the runner's classification matches
the generator's intended case.  Investigators do not run this.

  python -m triage.check_case            # after build_case and run_triage
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def main():
    key_path = ROOT / 'key' / 'answer_key.json'
    report_path = ROOT / 'report' / 'triage.json'
    if not key_path.exists():
        raise SystemExit('missing triage/key/answer_key.json; run build_case first')
    if not report_path.exists():
        raise SystemExit('missing triage/report/triage.json; run run_triage first')
    key = json.loads(key_path.read_text(encoding='utf-8'))['truth']
    report = json.loads(report_path.read_text(encoding='utf-8'))
    obs = report['observations']
    cls = report['classification']
    checks = {
        'card: three targets share one source': (
            obs['zip_card_entry']['targets_us'] == key['card_targets_us']
            and obs['zip_card_entry']['sources_us'] == key['card_sources_us']
            and len(set(key['card_sources_us'])) == 1),
        'card: member pixels identical': (
            obs['zip_card_entry']['members_pixel_identical'] == key['card_pixels_identical']),
        'hole: targets null in frozen evidence': (
            obs['zip_hole_entry']['targets_us'] == key['hole_targets_us']
            and obs['zip_hole_entry']['pictures_null'] == key['hole_pictures_null']),
        'hole: sound zero-filled': (
            obs['zip_hole_entry']['sound_fill'] == key['hole_sound_fill']),
        'web png equals zip card (browse)': (
            obs['web_png']['equals_zip_card_frame'] == key['web_png_equals_zip_card']),
        'zip card equals current card (browse)': (
            obs['web_png']['equals_current_derivation_at_web_label']
            == key['zip_card_equals_current_card_browse']),
        'zip card equals current long (browse)': (
            obs['cross_version']['current_long_pixels_equal_to_web']
            == key['zip_card_equals_current_long_browse']),
        'current long vs insert differ at full resolution': (
            obs['full_resolution_probe']['full_pixels_equal']
            == key['current_full_long_equals_current_full_insert']),
        'current sources match key': (
            obs['web_png']['current_source_at_web_label_us'] == key['current_card_source_us']
            and obs['cross_version']['current_long_source_us'] == key['current_long_source_us']),
        'exactly one config for the three card labels': (
            len(obs['zip_card_entry']['unique_config_for_three_labels']) == 1
            and obs['zip_card_entry']['unique_config_for_three_labels'][0]['padding_ms'] == 0
            and obs['zip_card_entry']['unique_config_for_three_labels'][0]['frames'] == 3),
        'no other hit yields the same three labels': (
            obs['zip_card_entry']['same_labels_from_other_hits'] == []),
        'class counts': (
            len(cls['excluded_as_configuration']) == 3
            and len(cls['confirmed_other_source_version']) == 1
            and len(cls['pixel_identical_unattributable']) == 2
            and len(cls['frozen_record_only']) == 2),
        'archive bytes not present': (
            report['observations']['integrity']['archive_bytes_available'] is False
            and report['observations']['integrity']['current_equals_archived_master'] is False),
    }
    failed = [name for name, ok in checks.items() if not ok]
    print(json.dumps({'checks': len(checks), 'failed': failed,
                      'passed': not failed}, ensure_ascii=False, indent=2))
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
