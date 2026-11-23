"""Combine same-clock measurements with selected-track coverage facts."""
from dataclasses import asdict

from .clock import Span, uncovered
from .hits import Run, bind_hits, hit
from .media import Picture, decode
from .picture import PictureMeter
from .rules import Thresholds, identifier
from .sound import SoundMeter

MEASUREMENT_VERSION = 2


def scan_identity(asset, policy):
    return identifier('scan', [asset['id'], policy['id'], MEASUREMENT_VERSION])


def measure(asset, path, policy):
    rules = Thresholds(**policy['thresholds'])
    tracks = asset['media']
    picture = PictureMeter(tracks['picture'], rules)
    sound = SoundMeter(tracks['sound'], rules) if tracks['sound'] else None
    for entry in decode(path, tracks):
        if isinstance(entry, Picture):
            picture.push(entry)
        else:
            sound.push(entry)
    picture_coverage, findings, picture_stats = picture.finish()
    sound_coverage, sound_stats = [], None
    if sound is not None:
        sound_coverage, sound_findings, sound_stats = sound.finish()
        findings.extend(sound_findings)
    extent = Span(0, max(picture_coverage[-1].end, sound_coverage[-1].end if sound_coverage else 0))
    lanes = [('picture_gap', picture_coverage, rules.picture_gap_ms)]
    if sound is not None:
        lanes.append(('sound_gap', sound_coverage, rules.sound_gap_ms))
    for kind, coverage, minimum in lanes:
        for gap in uncovered(coverage, extent):
            if gap.duration >= minimum * 1000:
                findings.append(hit(Run(gap, 1, gap.duration, gap.duration), kind, None))
    scan_id = scan_identity(asset, policy)
    return {'id': scan_id, 'master_id': asset['id'], 'policy_id': policy['id'],
            'analysis_version': MEASUREMENT_VERSION, 'thresholds': asdict(rules),
            'extent': extent.as_dict(), 'tracks': tracks,
            'coverage': {'picture': [s.as_dict() for s in picture_coverage],
                         'sound': [s.as_dict() for s in sound_coverage] if sound else None},
            'metrics': {'picture': picture_stats, 'sound': sound_stats},
            'hits': bind_hits(scan_id, findings)}
