"""SQLite descriptors and atomically published, content-addressed byte objects."""
from contextlib import contextmanager
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .clock import Conflict, Missing
from .rules import canonical


class Vault:
    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / 'objects'
        self.objects.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'slatewatch.sqlite3'
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS descriptors (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    document BLOB NOT NULL,
                    PRIMARY KEY(kind, id)
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    seq INTEGER PRIMARY KEY,
                    scan_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    document BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS review_scan ON review_events(scan_id, seq);
                CREATE TABLE IF NOT EXISTS handoff_events (
                    seq INTEGER PRIMARY KEY,
                    handoff_id TEXT NOT NULL,
                    document BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoff_event ON handoff_events(handoff_id, seq);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, key, required=True):
        with self.connect() as connection:
            row = connection.execute('SELECT document FROM descriptors WHERE kind=? AND id=?', (kind, key)).fetchone()
        if row:
            return json.loads(row[0])
        if required:
            raise Missing(f'{kind} {key!r} does not exist')
        return None

    def put(self, kind, record):
        document = canonical(record)
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO descriptors VALUES (?, ?, ?)', (kind, record['id'], document))
            existing = connection.execute('SELECT document FROM descriptors WHERE kind=? AND id=?',
                                          (kind, record['id'])).fetchone()[0]
            if existing != document:
                raise Conflict(f'{kind} identity conflicts with the existing record')
        return record

    def recent(self, kind, limit=35):
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT document FROM descriptors WHERE kind=? ORDER BY rowid DESC LIMIT ?', (kind, limit)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, scan_id, kind, record):
        with self.connect() as connection:
            cursor = connection.execute('INSERT INTO review_events(scan_id, kind, document) VALUES (?, ?, ?)',
                                        (scan_id, kind, canonical(record)))
            return {'seq': cursor.lastrowid, **record}

    def timeline(self, scan_id):
        with self.connect() as connection:
            rows = connection.execute('SELECT seq, kind, document FROM review_events WHERE scan_id=? ORDER BY seq',
                                      (scan_id,)).fetchall()
        intents, rulings = [], []
        for seq, kind, document in rows:
            (intents if kind == 'intent' else rulings).append({'seq': seq, **json.loads(document)})
        return intents, rulings

    def append_handoff(self, handoff_id, record):
        with self.connect() as connection:
            cursor = connection.execute('INSERT INTO handoff_events(handoff_id, document) VALUES (?, ?)',
                                        (handoff_id, canonical(record)))
            return {'seq': cursor.lastrowid, **record}

    def handoff_timeline(self, handoff_id):
        with self.connect() as connection:
            rows = connection.execute('SELECT seq, document FROM handoff_events WHERE handoff_id=? ORDER BY seq',
                                      (handoff_id,)).fetchall()
        return [{'seq': seq, **json.loads(document)} for seq, document in rows]

    def object_path(self, digest):
        return self.objects / digest

    def publish(self, path, digest):
        destination = self.object_path(digest)
        try:
            os.link(path, destination)
        except FileExistsError:
            if digest_file(destination) != digest:
                raise Conflict('an existing byte object failed its content address')
        return destination

    @contextmanager
    def staged(self, data):
        with tempfile.NamedTemporaryFile(dir=self.objects, prefix='.incoming-') as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            yield Path(temp.name), sha256(data).hexdigest()


def digest_file(path):
    with path.open('rb') as source:
        return file_digest(source, 'sha256').hexdigest()
