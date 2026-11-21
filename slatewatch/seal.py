"""Build frozen, independently verifiable inspection packages."""
from hashlib import sha256
from io import BytesIO
import json
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import Conflict
from .rules import canonical, identifier
from .store import digest_file


def index_for(files):
    return {name: {'size': len(data), 'sha256': sha256(data).hexdigest()}
            for name, data in sorted(files.items())}


def pack(manifest, excerpts):
    files = {}
    maps = []
    for members, mapping in excerpts:
        maps.append(mapping)
        for name, data in members.items():
            files[f"evidence/{mapping['hit_id']}/{name}"] = data
    files['evidence.json'] = canonical(maps)
    files['scan.json'] = canonical(manifest['scan'])
    files['review.json'] = canonical(manifest['review'])
    index = {'format': 1, 'master_sha256': manifest['master_sha256'],
             'scan_id': manifest['scan']['id'], 'review_status': manifest['review']['status'],
             'members': index_for(files)}
    seal_id = identifier('seal', index)
    record = {'id': seal_id, **index}
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
            raise Conflict('bundle hash differs from sealed record')
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != {'index.json', *record['members']}:
                raise Conflict('bundle membership differs from sealed record')
            current = json.loads(archive.read('index.json'))
            expected = {name: value for name, value in record.items() if name != 'sha256'}
            if current != expected:
                raise Conflict('bundle index differs from sealed record')
            for name, expected_member in record['members'].items():
                data = archive.read(name)
                if len(data) != expected_member['size'] or sha256(data).hexdigest() != expected_member['sha256']:
                    raise Conflict(f'bundle member {name!r} failed verification')
    except (OSError, BadZipFile, ValueError, EOFError) as exc:
        if isinstance(exc, Conflict):
            raise
        raise Conflict('sealed bundle cannot be read') from exc
    return {'id': record['id'], 'status': 'verified', 'sha256': record['sha256'],
            'members': len(record['members']), 'scan_id': record['scan_id'],
            'review_status': record['review_status']}
