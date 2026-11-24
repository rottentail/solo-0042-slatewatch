"""Application boundary for media intake, measurement, review and sealed evidence."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from .clock import Conflict, InputError, Missing, integer, shape, text
from .compare import compare
from .evidence import extract
from .media import inspect
from .rehearse import (TUNING, RehearsalCache, describe as describe_rehearsal,
                       prepare as prepare_rehearsal_session)
from .review import adjudicate, find_hit, intent, ruling
from .rules import canonical, identifier, new_policy
from .scan import measure, scan_identity
from .seal import pack, verify
from .store import Vault


class Slatewatch:
    def __init__(self, root):
        self.vault = Vault(root)
        self.rehearsals = RehearsalCache()

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

    def prepare_rehearsal(self, scan_id):
        return prepare_rehearsal_session(self.vault, scan_id, self.rehearsals)

    def prepare_rehearsal_request(self, data):
        shape(data, (), ('scan_id', 'master_id', 'policy_id'))
        if data.get('scan_id'):
            return self.prepare_rehearsal(text(data['scan_id'], 'scan_id'))
        if data.get('master_id') and data.get('policy_id'):
            scan = self.scan({'master_id': text(data['master_id'], 'master_id'),
                              'policy_id': text(data['policy_id'], 'policy_id')})
            return self.prepare_rehearsal(scan['id'])
        raise InputError('provide scan_id or master_id and policy_id')

    def try_rehearse(self, scan_id, data):
        session = self.prepare_rehearsal(scan_id)
        record = self.rehearse(session['token'], data)
        return {'token': session['token'], **record}

    def rehearsal_session(self, token):
        return describe_rehearsal(self.rehearsals.get(token))

    def rehearse(self, token, data):
        session = self.rehearsals.get(token)
        return self.rehearsals.record(session, data)

    def confirm_rehearsal(self, token, data):
        shape(data, (), ('rehearsal_id', 'label', 'policy_label', *TUNING))
        session = self.rehearsals.get(token)
        if data.get('rehearsal_id'):
            wanted = text(data['rehearsal_id'], 'rehearsal_id')
            rehearsal = next((item for item in session.rehearsals if item['id'] == wanted), None)
            if rehearsal is None:
                raise Missing('此预演不存在或已过期。')
        elif any(key in data for key in TUNING):
            rehearsal = self.rehearsals.record(
                session, {key: data[key] for key in TUNING if key in data})
        else:
            raise InputError('provide rehearsal_id or tuning parameters')
        label = data.get('label') or data.get('policy_label') or f'静音配方 {rehearsal["id"]}'
        thresholds = {**session.policy['thresholds'], **rehearsal['parameters']}
        policy = self.policy({'label': label, 'thresholds': thresholds})
        master_path = self.vault.object_path(session.master['sha256'])
        if not master_path.is_file():
            raise Conflict('母带文件当前不可读，请恢复共享母带连接后再确认预演。')
        scan = self.scan({'master_id': session.master_id, 'policy_id': policy['id']})
        official = [{key: value for key, value in hit.items() if key != 'id'}
                    for hit in scan['hits'] if hit['kind'] == 'silence']
        if official != rehearsal['candidates']:
            raise Conflict('预演结果与正式扫描不一致，请重新预演。')
        return {'policy': policy, 'scan': scan,
                'confirmed': {'master_id': session.master_id,
                              'master_sha256': session.master['sha256'],
                              'sound_track': session.track,
                              'baseline_policy_id': session.policy['id'],
                              'rehearsal_id': rehearsal['id'],
                              'parameters': rehearsal['parameters']}}

    def add_intent(self, scan_id, data):
        scan = self.vault.get('scan', scan_id)
        return self.vault.append(scan_id, 'intent', intent(scan, data))

    def add_ruling(self, scan_id, data):
        scan = self.vault.get('scan', scan_id)
        return self.vault.append(scan_id, 'ruling', ruling(scan, data))

    def review(self, scan_id):
        scan = self.vault.get('scan', scan_id)
        return adjudicate(scan, *self.vault.timeline(scan_id))

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
