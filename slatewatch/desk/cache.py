"""Preview material retention.

Two interchangeable stores share the :class:`PreviewStore` surface:

* :class:`PreviewCache` is the legacy bounded, process-only memory cache.  A
  token minted by an old process disappears when that process stops.
* :class:`FilePreviewStore` persists the descriptor, original excerpt options,
  evidence mapping and the browser assets (PNG/WAV) under the workstation
  vault, so the same ``/desk/previews/<id>`` address keeps working after a
  software restart.

Addresses always resolve to one of the :data:`PREVIEW_STATES`.  ``expired``
covers TTL, capacity eviction and lost cached bytes; ``source_unavailable``
means the master byte object required to rebuild/renew the preview is gone.
"""

from collections import OrderedDict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from time import time
from uuid import uuid4

from ..clock import InputError, Missing
from ..rules import identifier

PREVIEW_LIMIT = 24 * 1024 * 1024
CACHE_LIMIT = 48 * 1024 * 1024

#: Seven days is the default workstation retention for temporary previews.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
#: File-backed previews may use more disk than the old process cache.
FILE_CACHE_LIMIT = 256 * 1024 * 1024
FILE_ENTRY_LIMIT = 128

STATE_READY = 'ready'
STATE_EXPIRED = 'expired'
STATE_SOURCE_UNAVAILABLE = 'source_unavailable'
STATE_MISSING = 'missing'
PREVIEW_STATES = (STATE_READY, STATE_EXPIRED, STATE_SOURCE_UNAVAILABLE, STATE_MISSING)

#: Stable machine-readable reason carried by ``expired`` descriptors.
EXPIRY_TTL = 'ttl'
EXPIRY_MANUAL = 'manual'
EXPIRY_CAPACITY = 'capacity'
EXPIRY_ASSETS_LOST = 'assets_lost'

_SAFE_KEY = re.compile(r'[A-Za-z0-9_.-]{1,96}')


class PreviewUnavailable(Exception):
    """A known preview address is in a non-ready state.

    ``http_status`` follows conventional semantics: 410 for expired material
    and 503 when the source master cannot be reached.  HTML callers may still
    choose to render an inline state panel instead of a bare error response.
    """

    def __init__(self, state, message, http_status=410, reason=None, record=None):
        super().__init__(message)
        self.state = state
        self.http_status = http_status
        self.reason = reason
        self.record = record


def safe_member(name):
    """Accept only simple file names produced by :mod:`slatewatch.evidence`."""
    if not isinstance(name, str) or not name or name in ('.', '..'):
        return False
    if '/' in name or '\\' in name or '\0' in name:
        return False
    return Path(name).name == name and not name.startswith('.tmp-')


class PreviewCache:
    """Legacy bounded in-process cache (the "old program" behaviour)."""

    file_backed = False
    persistence = 'memory'

    def __init__(self):
        self.entries = OrderedDict()
        #: Side-table for migration metadata; the entry tuple itself stays
        #: ``(mapping, assets, size)`` exactly as in the legacy cache.
        self.metadata = {}
        self.total = 0
        self.lock = RLock()

    def put(self, mapping, assets, *, selection=None, source=None, key=None):
        size = sum(len(data) for data, _mime in assets.values())
        if size > PREVIEW_LIMIT:
            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        key = key or uuid4().hex
        meta = {'selection': selection, 'source': source or {}, 'created_at': time()}
        with self.lock:
            if key in self.entries:
                _m, _a, old_size = self.entries.pop(key)
                self.total -= old_size
            while self.entries and (len(self.entries) >= 12 or self.total + size > CACHE_LIMIT):
                old_key, (_mapping, _assets, old_size) = self.entries.popitem(last=False)
                self.metadata.pop(old_key, None)
                self.total -= old_size
            self.entries[key] = (mapping, assets, size)
            self.metadata[key] = meta
            self.total += size
        return key

    def get(self, key):
        with self.lock:
            if key not in self.entries:
                raise Missing('此预览已过期，请从审片记录重新摘片。')
            self.entries.move_to_end(key)
            mapping, assets, _size = self.entries[key]
            return mapping, assets

    def describe(self, key):
        with self.lock:
            if key not in self.entries:
                raise Missing('此预览已过期，请从审片记录重新摘片。')
            self.entries.move_to_end(key)
            mapping, assets, _size = self.entries[key]
            meta = self.metadata.get(key, {})
        return {
            'format': 1,
            'id': key,
            'state': STATE_READY,
            'persistence': self.persistence,
            'scan_id': mapping.get('scan_id'),
            'hit_id': mapping.get('hit_id'),
            'selection': meta.get('selection') or {
                'hit_ids': [mapping.get('hit_id')], 'padding_ms': None, 'frames': None,
            },
            'created_at': meta.get('created_at'),
            'expires_at': None,
            'ttl_seconds': None,
            'source': dict(meta.get('source') or {}, available=None),
            'span': {'start_us': mapping.get('start_us'), 'end_us': mapping.get('end_us')},
            'mapping': mapping,
            'page_url': f'/desk/previews/{key}',
            'filled_sample_ranges': (mapping.get('sound') or {}).get('filled_sample_ranges')
            if mapping.get('sound') else None,
            'assets': {
                name: {'mime': mime, 'size': len(data),
                       'sha256': sha256(data).hexdigest(), 'available': True}
                for name, (data, mime) in assets.items()
            },
        }

    def names(self):
        with self.lock:
            return list(self.entries)


class FilePreviewStore:
    """Persist preview descriptors and assets below ``<vault>/previews``."""

    file_backed = True
    persistence = 'file'

    def __init__(self, root, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time,
                 max_bytes=FILE_CACHE_LIMIT, max_entries=FILE_ENTRY_LIMIT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if ttl_seconds is None or ttl_seconds <= 0:
            self.ttl_seconds = None
        else:
            self.ttl_seconds = float(ttl_seconds)
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._clock = clock
        self.lock = RLock()

    # ----- identity and paths -------------------------------------------

    def key_for(self, scan_id, selection):
        """Stable address for one scan + exact excerpt options."""
        return identifier('preview', {'scan_id': scan_id, 'selection': selection})

    def _check_key(self, key):
        if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key):
            raise Missing('工作台没有此预览地址。')
        return key

    def directory(self, key):
        return self.root / self._check_key(key)

    def descriptor_path(self, key):
        return self.directory(key) / 'preview.json'

    def _asset_path(self, key, name):
        if not safe_member(name):
            raise Missing('没有此项预览材料。')
        return self.directory(key) / 'assets' / name

    def now(self):
        return self._clock()

    # ----- writing -------------------------------------------------------

    def put(self, mapping, assets, *, selection=None, source=None, key=None):
        size = sum(len(data) for data, _mime in assets.values())
        if size > PREVIEW_LIMIT:
            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        if key is not None:
            key = self._check_key(key)
        else:
            # Same token shape as the legacy process cache; persistence is
            # what makes the address survive a restart.
            key = uuid4().hex
        scan_id = mapping.get('scan_id')
        for name in assets:
            if not safe_member(name):
                raise InputError('预览材料名称不合法。')
        moment = self.now()
        record = {
            'format': 1,
            'id': key,
            'state': STATE_READY,
            'persistence': self.persistence,
            'scan_id': scan_id,
            'hit_id': mapping.get('hit_id'),
            'selection': selection or {'hit_ids': [mapping.get('hit_id')]},
            'created_at': moment,
            'updated_at': moment,
            'expires_at': moment + self.ttl_seconds if self.ttl_seconds else None,
            'ttl_seconds': self.ttl_seconds,
            'source': source or {},
            'span': {'start_us': mapping.get('start_us'), 'end_us': mapping.get('end_us')},
            'mapping': mapping,
            'assets': {
                name: {'mime': mime, 'size': len(data), 'sha256': sha256(data).hexdigest()}
                for name, (data, mime) in assets.items()
            },
        }
        payload = json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8')
        with self.lock:
            temporary = self.root / f'.staging-{key}-{uuid4().hex[:8]}'
            assets_dir = temporary / 'assets'
            assets_dir.mkdir(parents=True)
            try:
                for name, (data, _mime) in assets.items():
                    target = assets_dir / name
                    target.write_bytes(data)
                descriptor = temporary / 'preview.json'
                descriptor.write_bytes(payload)
                os.chmod(temporary / 'preview.json', 0o644)
                final = self.root / key
                if final.exists():
                    shutil.rmtree(final)
                os.replace(temporary, final)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
            self._apply_retention(keep=key)
        return key

    def _write_record(self, record):
        path = self.descriptor_path(record['id'])
        temporary = path.with_name(f'.preview-{uuid4().hex[:8]}.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    # ----- reading and state resolution ---------------------------------

    def peek(self, key):
        """Return the raw descriptor without any state transition."""
        path = self.descriptor_path(key)
        if not path.is_file():
            raise Missing('找不到此预览地址；若它来自切换前旧进程的内存缓存，重启后不会保留，请按原摘片选项重新生成。')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise PreviewUnavailable(STATE_EXPIRED, '预览描述符无法读取，材料可能已过期或损坏。',
                                     http_status=410, reason='descriptor_corrupt') from exc
        if not isinstance(record, dict) or record.get('id') != key:
            raise Missing('预览描述符与地址不符。')
        return record

    def is_due(self, record):
        expires = record.get('expires_at')
        return isinstance(expires, (int, float)) and self.now() >= float(expires)

    def missing_assets(self, record):
        missing = []
        for name, meta in record.get('assets', {}).items():
            path = self._asset_path(record['id'], name)
            if not path.is_file() or path.stat().st_size != meta.get('size'):
                missing.append(name)
        return missing

    def expire(self, key, reason=EXPIRY_MANUAL):
        """Turn the address into an audit tombstone and remove asset bytes."""
        with self.lock:
            record = self.peek(key)
            if record.get('state') != STATE_EXPIRED:
                record['state'] = STATE_EXPIRED
                record['expired_at'] = self.now()
            record['expiry_reason'] = reason
            assets_dir = self.directory(key) / 'assets'
            if assets_dir.exists():
                shutil.rmtree(assets_dir, ignore_errors=True)
            for meta in record.get('assets', {}).values():
                meta['available'] = False
            record['updated_at'] = self.now()
            self._write_record(record)
            return record

    #: Alternative verb used by some cutover scripts.
    mark_expired = expire

    def classify(self, record, source_available=None):
        """Resolve the observable state, applying lazy TTL/file transitions."""
        if record.get('state') == STATE_EXPIRED or self.is_due(record):
            reason = record.get('expiry_reason') or EXPIRY_TTL
            tombstone = self.expire(record['id'], reason)
            raise PreviewUnavailable(
                STATE_EXPIRED, '此预览材料已过期，原摘片选项仍可核对，并可在源可用时重新摘片。',
                http_status=410, reason=tombstone.get('expiry_reason', reason), record=tombstone)
        missing = self.missing_assets(record)
        if missing:
            if source_available is False:
                raise PreviewUnavailable(
                    STATE_SOURCE_UNAVAILABLE,
                    '预览文件不完整，且源母带对象当前不可用，无法恢复摘片。',
                    http_status=503, reason='assets_lost_and_source_missing', record=record)
            tombstone = self.expire(record['id'], EXPIRY_ASSETS_LOST)
            raise PreviewUnavailable(
                STATE_EXPIRED, '预览文件已缺失，可按留档的原摘片选项重新摘片。',
                http_status=410, reason=EXPIRY_ASSETS_LOST, record=tombstone)
        if source_available is False:
            raise PreviewUnavailable(
                STATE_SOURCE_UNAVAILABLE,
                '源母带对象当前不可用；已落盘的 PNG/WAV 仍可核对，但不能重新摘片或续期。',
                http_status=503, reason='source_missing', record=record)
        return STATE_READY

    def load_ready(self, key, source_available=None):
        record = self.peek(key)
        self.classify(record, source_available)
        return record

    def get(self, key):
        """Legacy two-value contract: ``(mapping, {name: (bytes, mime)})``."""
        record = self.load_ready(key)
        assets = {}
        for name, meta in record['assets'].items():
            assets[name] = ((self.directory(key) / 'assets' / name).read_bytes(), meta['mime'])
        return record['mapping'], assets

    def asset(self, key, name):
        record = self.peek(key)
        if record.get('state') == STATE_EXPIRED or self.is_due(record):
            self.classify(record)  # raises the matching PreviewUnavailable
        if name not in record.get('assets', {}):
            raise Missing('没有此项预览材料。')
        path = self._asset_path(key, name)
        if not path.is_file():
            raise PreviewUnavailable(STATE_EXPIRED, '此预览材料文件已不存在。', 410,
                                     reason=EXPIRY_ASSETS_LOST, record=record)
        meta = record['assets'][name]
        return path.read_bytes(), meta.get('mime', 'application/octet-stream'), meta

    def describe(self, key, source_available=None):
        record = self.peek(key)
        try:
            self.classify(record, source_available)
            state = STATE_READY
            error = None
        except PreviewUnavailable as exc:
            state = exc.state
            error = exc
            record = exc.record or record
        document = self._public_record(record, state, source_available)
        if error is not None:
            document['error'] = str(error)
            document['reason'] = error.reason
            document['http_status'] = error.http_status
        else:
            document['http_status'] = 200
        return document

    def _public_record(self, record, state, source_available):
        assets = {}
        for name, meta in record.get('assets', {}).items():
            path = self.directory(record['id']) / 'assets' / name
            available = meta.get('available', path.is_file())
            assets[name] = {**meta, 'available': bool(available),
                            'url': f'/desk/previews/{record["id"]}/{name}'}
        source = dict(record.get('source') or {})
        if source_available is not None:
            source['available'] = bool(source_available)
        sound = (record.get('mapping') or {}).get('sound')
        return {
            'format': record.get('format', 1),
            'id': record['id'],
            'state': state,
            # Aliases for clients using status/boolean state vocabularies.
            'status': state,
            'ok': state == STATE_READY,
            'ready': state == STATE_READY,
            'expired': state == STATE_EXPIRED,
            'source_unavailable': state == STATE_SOURCE_UNAVAILABLE,
            'persistence': self.persistence,
            'scan_id': record.get('scan_id'),
            'hit_id': record.get('hit_id'),
            'selection': record.get('selection'),
            'options': record.get('selection'),
            'created_at': record.get('created_at'),
            'updated_at': record.get('updated_at'),
            'expires_at': record.get('expires_at'),
            'expired_at': record.get('expired_at'),
            'ttl_seconds': record.get('ttl_seconds'),
            'expiry_reason': record.get('expiry_reason'),
            'source': source,
            'span': record.get('span'),
            'mapping': record.get('mapping'),
            'filled_sample_ranges': (sound or {}).get('filled_sample_ranges') if sound else None,
            'assets': assets,
            'page_url': f'/desk/previews/{record["id"]}',
        }

    def names(self):
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and self.descriptor_path(p.name).is_file())

    def summaries(self):
        rows = []
        for key in self.names():
            try:
                record = self.peek(key)
            except (Missing, PreviewUnavailable):
                continue
            rows.append({
                'id': key,
                'state': STATE_EXPIRED if record.get('state') != STATE_EXPIRED and self.is_due(record)
                else record.get('state', STATE_READY),
                'scan_id': record.get('scan_id'),
                'hit_id': record.get('hit_id'),
                'created_at': record.get('created_at'),
                'expires_at': record.get('expires_at'),
            })
        return list(reversed(rows))

    # ----- retention -----------------------------------------------------

    def _apply_retention(self, keep):
        try:
            entries = []
            total = 0
            for key in self.names():
                if key == keep:
                    continue
                size = sum(f.stat().st_size for f in (self.root / key).rglob('*') if f.is_file())
                entries.append((key, size, (self.descriptor_path(key).stat().st_mtime)))
                total += size
            entries.sort(key=lambda item: item[2])
            count = len(entries) + 1
            for key, size, _mtime in entries:
                if count <= self.max_entries and total <= self.max_bytes:
                    break
                try:
                    self.expire(key, EXPIRY_CAPACITY)
                except (Missing, PreviewUnavailable):
                    continue
                total -= size
                count -= 1
        except OSError:
            # Retention must never break publishing a fresh preview.
            pass


# Natural aliases for callers and tests using the migration vocabulary.
PreviewStore = FilePreviewStore
PersistentPreviewCache = FilePreviewStore
DiskPreviewCache = FilePreviewStore


def migrate_entries(memory_cache, file_store):
    """Drain a live :class:`PreviewCache` into a :class:`FilePreviewStore`.

    This covers a graceful in-process handoff while the old cache is still
    resident: tokens and metadata are preserved.  Entries that died with an
    old process cannot be reconstructed (their bytes only existed in memory)
    and are reported separately by the ``missing`` state after cutover.
    Returns the list of migrated tokens.
    """
    migrated = []
    with memory_cache.lock:
        items = [(key, entry, memory_cache.metadata.get(key, {}))
                 for key, entry in memory_cache.entries.items()]
    for key, (mapping, assets, _size), meta in items:
        file_store.put(mapping, assets, selection=meta.get('selection'),
                       source=meta.get('source'), key=key)
        migrated.append(key)
    return migrated


def open_store(root=None, mode=None, ttl_seconds=None):
    """Build the workstation preview store from explicit or environment config."""
    mode = mode or os.environ.get('SLATEWATCH_PREVIEW_STORE', 'file')
    if mode == 'memory':
        return PreviewCache()
    if mode != 'file':
        raise InputError('SLATEWATCH_PREVIEW_STORE 只能是 file 或 memory。')
    if ttl_seconds is None:
        raw = os.environ.get('SLATEWATCH_PREVIEW_TTL_SECONDS')
        if raw:
            if not raw.isascii() or not raw.isdecimal():
                raise InputError('SLATEWATCH_PREVIEW_TTL_SECONDS 需要非负整数秒。')
            ttl_seconds = int(raw)
        else:
            ttl_seconds = DEFAULT_TTL_SECONDS
    return FilePreviewStore(Path(root) / 'previews', ttl_seconds=ttl_seconds) if root else FilePreviewStore(
        Path('slatewatch-vault') / 'previews', ttl_seconds=ttl_seconds)
