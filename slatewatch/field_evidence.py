"""Local, source-timestamped evidence for cross-field combing findings."""
from .clock import Span
from .fields import bob_deinterlace, crop, field_pgm, pgm
from .media import field_decode
from .rules import canonical

EVIDENCE_VERSION = 1


def _evidence_frames(scan, finding, samples):
    start, end = finding['start_us'], finding['end_us']
    frames = [entry for entry in scan['frames'] if start <= entry['start_us'] < end]
    if not frames:
        return []
    # Spread the requested samples across the hit and always include the frame
    # carrying the strongest comb signature.
    picks = [frames[min(len(frames) - 1, i * len(frames) // samples)] for i in range(samples)]
    peak = max(frames, key=lambda entry: entry['comb_permille'])
    if peak not in picks:
        picks[-1] = peak
    unique = []
    for frame in picks:
        if frame not in unique:
            unique.append(frame)
    return unique


def field_evidence(path, scan, finding, samples, padding_ms):
    targets = _evidence_frames(scan, finding, samples)
    padding = padding_ms * 1000
    window = Span(max(0, finding['start_us'] - padding),
                  min(scan['extent']['end_us'], finding['end_us'] + padding))
    raster = {}
    for frame in field_decode(path, scan['tracks']):
        if window.start <= frame.timestamp_us < window.end:
            raster[frame.timestamp_us] = frame
    members = {}
    mapping = []
    for index, entry in enumerate(targets):
        frame = raster.get(entry['start_us'])
        if frame is None:
            mapping.append({**entry, 'files': []})
            continue
        prefix = f'frame-{index + 1:02d}'
        files = {
            f'{prefix}-woven.pgm': pgm(frame.width, frame.height, frame.pixels),
            f'{prefix}-field-top.pgm': field_pgm(frame, 0),
            f'{prefix}-field-bottom.pgm': field_pgm(frame, 1),
            f'{prefix}-deinterlaced-bob.pgm': pgm(frame.width, frame.height,
                                                   bob_deinterlace(frame)),
        }
        local = crop(frame, _stored_points(scan, entry['start_us']))
        if local is not None:
            files[f'{prefix}-comb-crop.pgm'] = pgm(local['width'], local['height'], local['pixels'])
        members.update(files)
        mapping.append({
            **entry,
            'source_us': frame.timestamp_us,
            'raster': {'width': frame.width, 'height': frame.height},
            'crop': None if local is None else {key: value for key, value in local.items()
                                                 if key != 'pixels'},
            'files': sorted(files),
        })
    document = {'version': EVIDENCE_VERSION, 'hit_id': finding['id'],
                'kind': finding['kind'], **Span(finding['start_us'], finding['end_us']).as_dict(),
                'window_us': window.as_dict(), 'dominant_field_order': scan['dominant_field_order'],
                'thresholds': scan['field_thresholds'], 'frames': mapping,
                'note': 'woven=source frame; field-*=separated parity fields; '
                        'deinterlaced-bob=reference line-doubled output; comb-crop=local evidence'}
    return members, {finding['id']: document}


def _stored_points(scan, start_us):
    return scan.get('comb_points', {}).get(str(start_us), [])


def build_field_bundle(path, scan, findings, samples, padding_ms):
    files = {}
    documents = []
    for finding in findings:
        members, document = field_evidence(path, scan, finding, samples, padding_ms)
        hit_id = finding['id']
        for name, body in members.items():
            files[f'evidence/{hit_id}/{name}'] = body
        documents.append(document[hit_id])
    files['field-evidence.json'] = canonical(documents)
    files['field-inspection.json'] = canonical(scan)
    return files, documents
