"""Blind second-workstation review bound to one frozen evidence package."""
from hashlib import sha256
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import Conflict, InputError, Missing, shape, text
from .review import OUTCOMES
from .rules import canonical, identifier

VERDICTS = {'accepted': 'accept', 'held': 'hold'}


def create(seal_record, maps):
    """Derive a blind handoff from a verified seal and its evidence mappings."""
    hit_ids = sorted(mapping['hit_id'] for mapping in maps)
    if not hit_ids:
        raise InputError('sealed package carries no finding evidence')
    record = {'format': 1, 'seal_id': seal_record['id'], 'scan_id': seal_record['scan_id'],
              'seal_sha256': seal_record['sha256'], 'master_sha256': seal_record['master_sha256'],
              'hit_ids': hit_ids}
    return {'id': identifier('handoff', record), **record}


def blinded(handoff, seal_record, bundle_path):
    """Re-pack the frozen evidence without the first workstation's review."""
    names = ['evidence.json', 'scan.json']
    names += sorted(name for name in seal_record['members'] if name.startswith('evidence/'))
    files = {}
    with ZipFile(bundle_path) as archive:
        for name in names:
            data = archive.read(name)
            member = seal_record['members'][name]
            if len(data) != member['size'] or sha256(data).hexdigest() != member['sha256']:
                raise Conflict(f'frozen member {name!r} failed verification')
            files[name] = data
    manifest = {'format': 1, 'handoff_id': handoff['id'], 'seal_id': handoff['seal_id'],
                'seal_sha256': handoff['seal_sha256'], 'scan_id': handoff['scan_id'],
                'master_sha256': handoff['master_sha256'], 'hit_ids': handoff['hit_ids'],
                'members': {name: seal_record['members'][name] for name in names}}
    files['handoff.json'] = canonical(manifest)
    buf = BytesIO()
    with ZipFile(buf, 'w') as archive:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return buf.getvalue()


def ruling(handoff, data):
    shape(data, ('hit_id', 'outcome', 'reason', 'reviewer'))
    hit_id = text(data['hit_id'], 'hit_id')
    if hit_id not in handoff['hit_ids']:
        raise Missing('finding is not part of this blind handoff')
    if not isinstance(data['outcome'], str) or data['outcome'] not in OUTCOMES:
        raise InputError('outcome must be accept, hold or reset')
    return {'hit_id': hit_id, 'outcome': data['outcome'],
            'reason': text(data['reason'], 'reason', 350), 'reviewer': text(data['reviewer'], 'reviewer')}


def progress(handoff, events):
    """Latest standing blind ruling per finding; reset retracts one's own."""
    latest = {}
    for event in events:
        if event['outcome'] == 'reset':
            latest.pop(event['hit_id'], None)
        else:
            latest[event['hit_id']] = event
    return latest, all(hit_id in latest for hit_id in handoff['hit_ids'])


def view(handoff, events):
    """Blind-safe handoff state: the receiver's own rulings, never the old verdicts."""
    latest, complete = progress(handoff, events)
    return {**handoff, 'complete': complete,
            'rulings': [latest[hit_id] for hit_id in handoff['hit_ids'] if hit_id in latest]}


def reconcile(handoff, scan, frozen_review, current_review, events, evidence):
    """Compare both workstations against the same frozen evidence.

    Disagreements are only attributable to judgment when the frozen package
    verifies (evidence is identical) and the frozen verdict still matches the
    live one (the old conclusion has not changed).
    """
    latest, complete = progress(handoff, events)
    if not complete:
        raise Conflict('盲核判读尚未提交完整，不能核对旧裁决。')
    frozen_by_id = {hit['id']: hit for hit in frozen_review['hits']} if frozen_review else {}
    current_by_id = {hit['id']: hit for hit in current_review['hits']}
    scan_hits = {hit['id']: hit for hit in scan['hits']}
    rows = []
    counts = dict.fromkeys(('agree', 'disagree', 'no_prior', 'unknown'), 0)
    changed = []
    for hit_id in handoff['hit_ids']:
        second = latest[hit_id]
        frozen = frozen_by_id.get(hit_id)
        frozen_status = frozen['status'] if frozen else None
        current_status = current_by_id[hit_id]['status']
        old_changed = None if frozen is None else frozen_status != current_status
        if old_changed:
            changed.append(hit_id)
        if frozen is None:
            relation = 'unknown'
        elif VERDICTS.get(frozen_status) is None:
            relation = 'no_prior'
        elif VERDICTS[frozen_status] == second['outcome']:
            relation = 'agree'
        else:
            relation = 'disagree'
        counts[relation] += 1
        base = scan_hits[hit_id]
        rows.append({'hit_id': hit_id, 'kind': base['kind'], 'channel': base['channel'],
                     'start_us': base['start_us'], 'end_us': base['end_us'],
                     'frozen_status': frozen_status, 'current_status': current_status,
                     'old_changed': old_changed, 'second': second, 'relation': relation})
    if frozen_review is None:
        old_conclusion = 'unknown'
    else:
        old_conclusion = 'changed' if changed else 'unchanged'
    return {'handoff_id': handoff['id'], 'seal_id': handoff['seal_id'], 'scan_id': handoff['scan_id'],
            'evidence': evidence, 'old_conclusion': old_conclusion, 'changed_hit_ids': changed,
            'hits': rows, 'summary': {'scope': len(handoff['hit_ids']), **counts, 'old_changed': len(changed)}}
