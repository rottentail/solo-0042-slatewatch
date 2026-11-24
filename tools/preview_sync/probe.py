"""Drive the real Slatewatch service with the three clock-decision inputs.

Runs entirely over loopback HTTP against ``slatewatch.http.Server`` and returns
source-level facts (coverage, hits) and the exact browser-facing evidence
mapping (clock.json plus WAV header). Nothing here is mocked: the same PyAV
decode path and evidence packer used in production produce the numbers.
"""
from io import BytesIO
import json
import wave
from zipfile import ZipFile

from slatewatch.reels import make
from tools.preview_sync.engine import ENGINE_SCENARIOS  # shared scenario catalogue

# One hit per reel is enough to exercise every clock mapping branch.
PREFERRED_HIT = {
    'no-sound': 'black',
    'lead-pad': 'black',           # padding 120 ms reaches before span zero -> leading pad
    'fractional-hole': 'picture_gap',
}


def _evidence(app, scan, hit, padding_ms=120, frames=4):
    raw = app.evidence(scan['id'], {'hit_ids': [hit['id']],
                                    'padding_ms': padding_ms, 'frames': frames})
    with ZipFile(BytesIO(raw)) as archive:
        mapping = json.loads(archive.read(f"{hit['id']}/clock.json"))
        wav = None
        if any(n.endswith('sound.wav') for n in archive.namelist()):
            with wave.open(BytesIO(archive.read(f"{hit['id']}/sound.wav"))) as header:
                wav = {'channels': header.getnchannels(), 'rate': header.getframerate(),
                       'frames': header.getnframes(),
                       'seconds_exact': header.getnframes() / header.getframerate()}
    return mapping, wav


def observe(server, workdir):
    app = server.app
    policy = app.policy({'label': 'clock-decision'})
    report = {}
    for reel in ('no-sound', 'lead-pad', 'fractional-hole'):
        path = make(workdir / f'{reel}.mkv', reel)
        sound = None if reel == 'no-sound' else 'first'
        master = app.master(path.read_bytes(), reel, None, sound)
        scan = app.scan({'master_id': master['id'], 'policy_id': policy['id']})
        hit = next(h for h in scan['hits'] if h['kind'] == PREFERRED_HIT[reel])
        mapping, wav = _evidence(app, scan, hit)
        picture_present = [f for f in mapping['picture'] if f['file']]
        report[reel] = {
            'tracks': {'picture_rate': master['media']['picture']['average_rate'],
                       'sound_rate': None if master['media']['sound'] is None
                       else master['media']['sound']['rate']},
            'coverage': scan['coverage'],
            'hits': [{'kind': h['kind'], 'start_us': h['start_us'], 'end_us': h['end_us'],
                      'channel': h['channel']} for h in scan['hits']],
            'evidence': {
                'span_us': [mapping['start_us'], mapping['end_us']],
                'picture_points': [{'target_us': f['target_us'], 'source_us': f['source_us'],
                                    'present': f['file'] is not None} for f in mapping['picture']],
                'first_present_frame_us': picture_present[0]['source_us'] if picture_present else None,
                'frame_target_to_source_offsets_us': sorted({
                    f['source_us'] - f['target_us'] for f in picture_present}),
                'wav': wav,
                'sound_mapping': mapping['sound'],
            },
        }
    _assert_invariants(report)
    report['_scenarios'] = [s.__dict__ for s in ENGINE_SCENARIOS]
    return report


def _assert_invariants(report):
    """The design doc's guarantees rest on these facts; fail if inputs drift."""
    # no-sound: no WAV exists at all and the picture timeline is whole.
    assert report['no-sound']['coverage']['sound'] is None
    assert report['no-sound']['evidence']['wav'] is None
    assert report['no-sound']['coverage']['picture'] == [
        {'start_us': 0, 'end_us': 4_000_000}]
    # lead-pad: the excerpt WAV starts with 200 ms / 1600 synthesised zero samples.
    lead_sound = report['lead-pad']['evidence']['sound_mapping']
    assert lead_sound['filled_sample_ranges'] == [{'first': 0, 'end': 1600}]
    assert report['lead-pad']['evidence']['wav']['frames'] == 4800
    assert report['lead-pad']['coverage']['sound'][0]['start_us'] == 200_000
    # fractional-hole: the 2.0-2.5 s picture hole survives as two coverage spans
    # and two absent picture points inside one evidence excerpt.
    fh = report['fractional-hole']
    assert fh['coverage']['picture'] == [
        {'start_us': 0, 'end_us': 2_002_000}, {'start_us': 2_503_000, 'end_us': 4_004_000}]
    missing = [p for p in fh['evidence']['picture_points'] if not p['present']]
    assert len(missing) == 2 and fh['evidence']['sound_mapping']['clock_offset_us'] == 1_882_000
