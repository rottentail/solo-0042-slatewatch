"""Supplementary evidence for a frozen package without altering the original seal.

A supplement is a separately published, independently verifiable bundle.  It never
rewrites the sealed ZIP or its descriptor: the original package keeps its hash and
membership, while every supplement names the seal it extends, the original bundle
hash, and an explicit basis that distinguishes evidence cut from the same historical
observation from evidence produced during a fresh review of a later scan.
"""
from hashlib import sha256
from io import BytesIO
import json
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import Conflict
from .rules import canonical, identifier
from .store import digest_file

BASES = ('historical', 'current')


def pack(seal, basis, scan, master_sha256, source_master_sha256, review, reason, excerpts):
    files = {}
    maps = []
    for members, mapping in excerpts:
        maps.append(mapping)
        for name, data in members.items():
            files[f"supplement/{mapping['hit_id']}/{name}"] = data
    files['supplement-evidence.json'] = canonical(maps)
    files['supplement-review.json'] = canonical(review)
    manifest = {
        'format': 1,
        'seal_id': seal['id'],
        'seal_sha256': seal['sha256'],
        'basis': basis,
        'scan_id': scan['id'],
        'master_sha256': master_sha256,
        'source_master_sha256': source_master_sha256,
        'same_master': master_sha256 == source_master_sha256,
        'review_status': review['status'],
        'reason': reason,
    }
    files['supplement.json'] = canonical(manifest)
    index = {
        'format': 1,
        'kind': 'supplement',
        'seal_id': seal['id'],
        'seal_sha256': seal['sha256'],
        'basis': basis,
        'scan_id': scan['id'],
        'source_master_sha256': source_master_sha256,
        'review_status': review['status'],
        'members': {name: {'size': len(data), 'sha256': sha256(data).hexdigest()}
                    for name, data in sorted(files.items())},
    }
    supplement_id = identifier('supplement', index)
    record = {'id': supplement_id, **index}
    files['index.json'] = canonical(record)
    buf = BytesIO()
    with ZipFile(buf, 'w') as archive:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return record, buf.getvalue()


def verify(path, record):
    try:
        if digest_file(path) != record['sha256']:
            raise Conflict('supplement hash differs from its record')
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != {'index.json', *record['members']}:
                raise Conflict('supplement membership differs from its record')
            current = json.loads(archive.read('index.json'))
            expected = {name: value for name, value in record.items() if name != 'sha256'}
            if current != expected:
                raise Conflict('supplement index differs from its record')
            manifest = json.loads(archive.read('supplement.json'))
            if manifest['seal_id'] != record['seal_id'] or manifest['basis'] != record['basis']:
                raise Conflict('supplement lineage does not match its record')
            if manifest['seal_sha256'] != record['seal_sha256']:
                raise Conflict('supplement is bound to a different frozen package')
            for name, expected_member in record['members'].items():
                data = archive.read(name)
                if len(data) != expected_member['size'] or sha256(data).hexdigest() != expected_member['sha256']:
                    raise Conflict(f'supplement member {name!r} failed verification')
    except (OSError, BadZipFile, ValueError, EOFError) as exc:
        if isinstance(exc, Conflict):
            raise
        raise Conflict('supplement bundle cannot be read') from exc
    return {'id': record['id'], 'status': 'verified', 'sha256': record['sha256'],
            'members': len(record['members']), 'seal_id': record['seal_id'],
            'basis': record['basis'], 'scan_id': record['scan_id'],
            'review_status': record['review_status']}
