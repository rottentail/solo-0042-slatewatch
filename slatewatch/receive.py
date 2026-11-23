"""Organize sealed packages from peer workstations into one receiving directory.

Review workstations freeze the same programme independently and ship their
bundles off-site.  This offline tool verifies every incoming bundle against
its own embedded index, stores each distinct member byte stream exactly
once, and keeps every package's own index so each freeze can be rebuilt or
re-verified on its own: its scan version, member bytes and human rulings.
Material that cannot be trusted is quarantined and listed in the report.
"""
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import Conflict, InputError, Missing
from .rules import canonical, identifier
from .store import digest_file

SUPPORTED_FORMAT = 1
INDEX_KEYS = ('format', 'master_sha256', 'scan_id', 'review_status', 'members')
REVIEW_STATUSES = ('pending', 'hold', 'pass')
SEAL_ID = re.compile(r'seal-[0-9a-f]{24}')
SCAN_ID = re.compile(r'scan-[0-9a-f]{24}')
DIGEST = re.compile(r'[0-9a-f]{64}')


def read_bundle(path):
    """Verify one bundle against its embedded index; return record, raw index and members."""
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise Conflict('bundle lists a member more than once')
            if 'index.json' not in names:
                raise Conflict('bundle has no index.json')
            raw_index = archive.read('index.json')
            record = _index_record(raw_index)
            if set(names) != {'index.json', *record['members']}:
                raise Conflict('bundle membership differs from its index')
            members = {}
            for name, expected in record['members'].items():
                data = archive.read(name)
                if len(data) != expected['size'] or sha256(data).hexdigest() != expected['sha256']:
                    raise Conflict(f'bundle member {name!r} failed verification')
                members[name] = data
    except (OSError, BadZipFile, ValueError, EOFError, RuntimeError, NotImplementedError) as exc:
        if isinstance(exc, Conflict):
            raise
        raise Conflict('sealed bundle cannot be read') from exc
    return record, raw_index, members


def organize(sources, root):
    """Accept verifiable bundles into the receiving directory and write a report."""
    root = Path(root)
    objects = root / 'objects'
    packages = root / 'packages'
    quarantine = root / 'rejected'
    for directory in (objects, packages, quarantine):
        directory.mkdir(parents=True, exist_ok=True)
    paths = list(_expand(sources))
    if not paths:
        raise InputError('no source bundles found')
    report = {'format': SUPPORTED_FORMAT, 'accepted': [], 'duplicates': [], 'rejected': []}
    for path in paths:
        _receive_one(path, objects, packages, quarantine, report)
    report['shared_objects'] = _shared_objects(packages)
    report['divergent_members'] = _divergent_members(packages)
    _publish(root / 'report.json', canonical(report))
    return report


def verify_package(root, seal_id):
    """Re-verify one received package against the stored byte objects."""
    root = Path(root)
    record = _load_record(root, seal_id)
    objects = root / 'objects'
    for name, expected in record['members'].items():
        path = objects / expected['sha256']
        if not path.is_file():
            raise Conflict(f'received object for member {name!r} is missing')
        if path.stat().st_size != expected['size'] or digest_file(path) != expected['sha256']:
            raise Conflict(f'received object for member {name!r} failed verification')
    return {'id': record['id'], 'status': 'verified', 'members': len(record['members']),
            'scan_id': record['scan_id'], 'review_status': record['review_status'],
            'master_sha256': record['master_sha256']}


def rebuild_package(root, seal_id, out_path):
    """Rebuild one package's bundle from the stored objects and its kept index."""
    root = Path(root)
    out_path = Path(out_path)
    if out_path.exists():
        raise InputError(f'output {str(out_path)!r} already exists')
    record = _load_record(root, seal_id)
    verify_package(root, seal_id)
    objects = root / 'objects'
    files = {name: (objects / expected['sha256']).read_bytes()
             for name, expected in record['members'].items()}
    files['index.json'] = (root / 'packages' / f'{seal_id}.json').read_bytes()
    with ZipFile(out_path, 'w') as archive:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    rebuilt, _, _ = read_bundle(out_path)
    if rebuilt != record:
        raise Conflict('rebuilt bundle differs from the received record')
    return {'id': seal_id, 'status': 'verified', 'path': str(out_path),
            'members': len(record['members'])}


def _index_record(raw):
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise Conflict('bundle index is not valid JSON') from exc
    if not isinstance(record, dict):
        raise Conflict('bundle index is not a JSON object')
    fmt = record.get('format')
    if type(fmt) is not int:
        raise Conflict('bundle index has no readable format')
    if fmt < SUPPORTED_FORMAT:
        raise Conflict(f'index format {fmt} is older than supported format {SUPPORTED_FORMAT}')
    if fmt > SUPPORTED_FORMAT:
        raise Conflict(f'index format {fmt} is newer than supported format {SUPPORTED_FORMAT}')
    if sorted(record) != sorted(('id', *INDEX_KEYS)):
        raise Conflict('bundle index fields differ from the supported layout')
    if not isinstance(record['id'], str) or not SEAL_ID.fullmatch(record['id']):
        raise Conflict('bundle index has no valid seal identity')
    if identifier('seal', {key: record[key] for key in INDEX_KEYS}) != record['id']:
        raise Conflict('bundle index identity does not match its content')
    if not isinstance(record['master_sha256'], str) or not DIGEST.fullmatch(record['master_sha256']):
        raise Conflict('bundle index has no valid master digest')
    if not isinstance(record['scan_id'], str) or not SCAN_ID.fullmatch(record['scan_id']):
        raise Conflict('bundle index has no valid scan identity')
    if record['review_status'] not in REVIEW_STATUSES:
        raise Conflict('bundle index has an unknown review status')
    _member_table(record['members'])
    return record


def _member_table(members):
    if not isinstance(members, dict) or not members:
        raise Conflict('bundle index has no member table')
    for name, entry in members.items():
        if not _safe_name(name):
            raise Conflict(f'bundle member name {name!r} is not a safe relative path')
        if not isinstance(entry, dict) or sorted(entry) != ['sha256', 'size']:
            raise Conflict(f'bundle member {name!r} has an incomplete index entry')
        if type(entry['size']) is not int or entry['size'] < 0:
            raise Conflict(f'bundle member {name!r} has an invalid size')
        if not isinstance(entry['sha256'], str) or not DIGEST.fullmatch(entry['sha256']):
            raise Conflict(f'bundle member {name!r} has an invalid digest')


def _safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and str(path) == name and '..' not in path.parts


def _expand(sources):
    for source in sources:
        path = Path(source)
        if path.is_dir():
            yield from sorted(entry for entry in path.iterdir() if entry.is_file())
        elif path.is_file():
            yield path
        else:
            raise InputError(f'source {str(source)!r} does not exist')


def _receive_one(path, objects, packages, quarantine, report):
    source = path.name
    try:
        record, raw_index, members = read_bundle(path)
    except Conflict as exc:
        kept = _quarantine(quarantine, path)
        report['rejected'].append({'source': source, 'reason': str(exc), 'kept': f'rejected/{kept}'})
        return
    seal_id = record['id']
    target = packages / f'{seal_id}.json'
    if target.exists():
        report['duplicates'].append({'id': seal_id, 'source': source})
        return
    new_objects = sum(_store_object(objects, data, sha256(data).hexdigest()) for data in members.values())
    _publish(target, raw_index)
    report['accepted'].append({'id': seal_id, 'source': source, 'scan_id': record['scan_id'],
                               'review_status': record['review_status'],
                               'master_sha256': record['master_sha256'],
                               'members': len(members), 'new_objects': new_objects})


def _store_object(objects, data, digest):
    destination = objects / digest
    if destination.exists():
        if digest_file(destination) != digest:
            raise Conflict('an existing byte object failed its content address')
        return 0
    with tempfile.NamedTemporaryFile(dir=objects, prefix='.incoming-', delete=False) as temp:
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
        staged = Path(temp.name)
    try:
        os.link(staged, destination)
    except FileExistsError:
        if digest_file(destination) != digest:
            raise Conflict('an existing byte object failed its content address')
        return 0
    finally:
        staged.unlink()
    return 1


def _publish(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.incoming-', delete=False) as temp:
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
        staged = Path(temp.name)
    os.replace(staged, path)


def _quarantine(quarantine, source):
    destination = quarantine / source.name
    counter = 1
    while destination.exists():
        destination = quarantine / f'{source.stem}.{counter}{source.suffix}'
        counter += 1
    shutil.copyfile(source, destination)
    return destination.name


def _records(packages):
    return [json.loads(path.read_bytes()) for path in sorted(packages.glob('seal-*.json'))]


def _shared_objects(packages):
    uses, sizes = {}, {}
    for record in _records(packages):
        for name, entry in record['members'].items():
            uses.setdefault(entry['sha256'], {}).setdefault(record['id'], []).append(name)
            sizes[entry['sha256']] = entry['size']
    return [{'sha256': digest, 'size': sizes[digest],
             'names': {seal_id: sorted(names) for seal_id, names in sorted(holders.items())}}
            for digest, holders in sorted(uses.items()) if len(holders) > 1]


def _divergent_members(packages):
    uses = {}
    for record in _records(packages):
        for name, entry in record['members'].items():
            uses.setdefault(name, {})[record['id']] = entry['sha256']
    return [{'name': name, 'packages': dict(sorted(holders.items()))}
            for name, holders in sorted(uses.items()) if len(set(holders.values())) > 1]


def _load_record(root, seal_id):
    if not isinstance(seal_id, str) or not SEAL_ID.fullmatch(seal_id):
        raise InputError('a seal id looks like seal- followed by 24 hex characters')
    path = Path(root) / 'packages' / f'{seal_id}.json'
    if not path.is_file():
        raise Missing(f'package {seal_id!r} was not received')
    return _index_record(path.read_bytes())


def _verify_all(root, seal_ids):
    root = Path(root)
    if not root.is_dir():
        raise InputError(f'receiving directory {str(root)!r} does not exist')
    ids = seal_ids or sorted(path.stem for path in (root / 'packages').glob('seal-*.json'))
    results, failed = [], False
    for seal_id in ids:
        try:
            results.append(verify_package(root, seal_id))
        except (Conflict, Missing, InputError) as exc:
            results.append({'id': seal_id, 'status': 'failed', 'reason': str(exc)})
            failed = True
    print(canonical(results).decode())
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description='Organize sealed Slatewatch packages from peer workstations')
    commands = parser.add_subparsers(dest='command', required=True)
    organize_parser = commands.add_parser('organize', help='accept bundles into the receiving directory')
    organize_parser.add_argument('sources', nargs='+', type=Path)
    organize_parser.add_argument('--root', type=Path, required=True)
    verify_parser = commands.add_parser('verify', help='re-verify received packages')
    verify_parser.add_argument('--root', type=Path, required=True)
    verify_parser.add_argument('seal_ids', nargs='*')
    rebuild_parser = commands.add_parser('rebuild', help='rebuild the bundle of one received package')
    rebuild_parser.add_argument('--root', type=Path, required=True)
    rebuild_parser.add_argument('seal_id')
    rebuild_parser.add_argument('out', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'organize':
            print(canonical(organize(args.sources, args.root)).decode())
        elif args.command == 'verify':
            raise SystemExit(_verify_all(args.root, args.seal_ids))
        else:
            print(canonical(rebuild_package(args.root, args.seal_id, args.out)).decode())
    except (Conflict, Missing, InputError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()
