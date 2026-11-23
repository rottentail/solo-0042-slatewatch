"""Application boundary for media intake, measurement, review and sealed evidence."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from .chapters import carry_links, link_event, parse_sheet, project
from .clock import InputError, Missing, Span, integer, shape, text
from .compare import compare
from .evidence import extract
from .media import inspect
from .review import adjudicate, find_hit, intent, ruling
from .rules import canonical, identifier, new_policy
from .scan import measure, scan_identity
from .seal import pack, verify
from .store import Vault


class Slatewatch:
    def __init__(self, root):
        self.vault = Vault(root)

    def master(self, data, label, picture, sound):
        name = text(label, 'label')
        with self.vault.staged(data) as (path, digest):
            media = inspect(path, picture, sound)
            record = {'label': name, 'size': len(data), 'sha256': digest, 'media': media}
            record['id'] = identifier('master', record)
            self.vault.publish(path, digest)
        return self.vault.put('master', record)

    def policy(self, data):
        return self.vault.put('policy', new_policy(data))

    def scan(self, data):
        shape(data, ('master_id', 'policy_id'))
        asset = self.vault.get('master', text(data['master_id'], 'master_id'))
        policy = self.vault.get('policy', text(data['policy_id'], 'policy_id'))
        existing = self.vault.get('scan', scan_identity(asset, policy), required=False)
        if existing:
            return existing
        path = self.vault.object_path(asset['sha256'])
        return self.vault.put('scan', measure(asset, path, policy))

    def add_intent(self, scan_id, data):
        scan = self.vault.get('scan', scan_id)
        return self.vault.append(scan_id, 'intent', intent(scan, data))

    def add_ruling(self, scan_id, data):
        scan = self.vault.get('scan', scan_id)
        return self.vault.append(scan_id, 'ruling', ruling(scan, data))

    def review(self, scan_id):
        scan = self.vault.get('scan', scan_id)
        return adjudicate(scan, *self.vault.timeline(scan_id))

    def import_chapters(self, scan_id, data):
        shape(data, ('sheet',), ('links',))
        scan = self.vault.get('scan', scan_id)
        sheet = self.vault.put('sheet', parse_sheet(data['sheet']))
        projection = project(scan, sheet)
        links = data.get('links', [])
        active, unresolved = carry_links(scan, sheet, links)
        event = {
            'sheet_id': sheet['id'], 'head_offset_us': projection['head_offset_us'],
            'chapters': [*projection['determined'], *projection['candidates']],
            'determined_sequences': [chapter['seq'] for chapter in projection['determined']],
            'candidate_sequences': [chapter['seq'] for chapter in projection['candidates']],
            'links': active, 'unresolved_links': unresolved,
        }
        return self.vault.append(scan_id, 'chapter_import', event)

    def chapter_track(self, scan_id):
        scan = self.vault.get('scan', scan_id)
        event, manual_links = self.vault.chapters(scan_id)
        if event is None:
            return {'scan_id': scan_id, 'sheet_id': None, 'chapters': [], 'unresolved_links': []}
        # Unresolved links carried from another scan are kept visible on their chapter;
        # they are never silently reattached to this scan's findings.
        unresolved = {link['chapter_seq']: link for link in event['unresolved_links']}
        by_chapter = {**unresolved, **{link['chapter_seq']: link
                                       for link in [*event['links'], *manual_links]}}
        hit_index = {hit['id']: hit for hit in scan['hits']}
        chapters = []
        for chapter in event['chapters']:
            record = {key: value for key, value in chapter.items()}
            record['state'] = 'determined' if chapter['seq'] in event['determined_sequences'] else 'candidate'
            link = by_chapter.get(chapter['seq'])
            record['link'] = None
            record['available_hits'] = self._overlapping_hits(scan, chapter, record['state'])
            if link is not None and link['hit_id'] is not None:
                hit = hit_index.get(link['hit_id'])
                record['link'] = {'hit_id': link['hit_id'], 'status': 'active' if hit else 'unresolved'}
                if hit:
                    record['link']['hit'] = hit
            chapters.append(record)
        return {'scan_id': scan_id, 'sheet_id': event['sheet_id'],
                'head_offset_us': event['head_offset_us'], 'chapters': chapters,
                'unresolved_links': event['unresolved_links']}

    def link_chapter(self, scan_id, data):
        scan = self.vault.get('scan', scan_id)
        event, _manual = self.vault.chapters(scan_id)
        if event is None:
            raise Missing('import a chapter table before linking its chapters')
        record = link_event(scan, event, data)
        return self.vault.append(scan_id, 'chapter_link', record)

    @staticmethod
    def _overlapping_hits(scan, chapter, state):
        """Findings a reviewer can actually jump to from a chapter position."""
        if state == 'determined':
            target = Span(chapter['time_us'], chapter['time_us'] + 1)
        else:
            target = Span(chapter['range']['start_us'], chapter['range']['end_us'])
        result = []
        for hit in scan['hits']:
            if target.intersect(Span(hit['start_us'], hit['end_us'])):
                result.append({'id': hit['id'], 'kind': hit['kind'], 'channel': hit['channel'],
                               'start_us': hit['start_us'], 'end_us': hit['end_us']})
        return result

    def compare(self, data):
        shape(data, ('before', 'after'), ('minimum_permille',))
        earlier = self.vault.get('scan', text(data['before'], 'before'))
        later = self.vault.get('scan', text(data['after'], 'after'))
        minimum = integer(data.get('minimum_permille', 300), 'minimum_permille', 1, 1000)
        return compare(earlier, later, minimum)

    def _selection(self, scan_id, data):
        shape(data, ('hit_ids',), ('padding_ms', 'frames'))
        ids = data['hit_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 6:
            raise InputError('provide one to six finding identifiers')
        ids = [text(key, 'finding identifier') for key in ids]
        if len(set(ids)) != len(ids):
            raise InputError('finding identifiers must be unique')
        scan = self.vault.get('scan', scan_id)
        asset = self.vault.get('master', scan['master_id'])
        hits = [find_hit(scan, key) for key in sorted(ids)]
        padding = integer(data.get('padding_ms', 120), 'padding_ms', 0, 2000)
        frames = integer(data.get('frames', 3), 'frames', 1, 8)
        return scan, asset, hits, padding, frames

    def evidence(self, scan_id, data):
        scan, asset, hits, padding, frames = self._selection(scan_id, data)
        path = self.vault.object_path(asset['sha256'])
        excerpts = extract(path, scan, hits, padding, frames)

        buf = BytesIO()
        with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as archive:
            for members, mapping in excerpts:
                prefix = mapping['hit_id']
                archive.writestr(f'{prefix}/clock.json', canonical(mapping))
                for name, body in members.items():
                    archive.writestr(f'{prefix}/{name}', body)
        return buf.getvalue()

    def seal(self, scan_id, data):
        scan, asset, hits, padding, frames = self._selection(scan_id, data)
        review = adjudicate(scan, *self.vault.timeline(scan_id))
        path = self.vault.object_path(asset['sha256'])
        excerpts = extract(path, scan, hits, padding, frames)
        record, data = pack({'scan': scan, 'review': review, 'master_sha256': asset['sha256']}, excerpts)
        with self.vault.staged(data) as (source, digest):
            self.vault.publish(source, digest)
        return self.vault.put('seal', {**record, 'sha256': digest})

    def verify(self, seal_id):
        record = self.vault.get('seal', seal_id)
        return verify(self.vault.object_path(record['sha256']), record)

    def download(self, seal_id):
        verification = self.verify(seal_id)
        return self.vault.object_path(verification['sha256']).read_bytes()
