"""Session-scoped offline rehearsal of silence rules on cached programme sound."""
from collections import OrderedDict
from threading import Lock
from uuid import uuid4

from .clock import Conflict, InputError, Missing, Span, shape
from .media import Picture, decode
from .rules import Thresholds
from .sound import SoundMeter

TUNING = ('audio_window_ms', 'silence_rms', 'silence_min_ms')
TUNING_NAMES = {'audio_window_ms': '音窗', 'silence_rms': '静音门槛', 'silence_min_ms': '最短事件'}
TUNING_UNITS = {'audio_window_ms': ' 毫秒', 'silence_rms': '', 'silence_min_ms': ' 毫秒'}
RELATION_LABELS = {'merged': '合并', 'split': '拆开', 'dropped': '不再成立', 'introduced': '新成立'}
SESSION_LIMIT = 4
CACHE_LIMIT = 64 * 1024 * 1024
RUN_LIMIT = 32


class Session:
    """Prepared material for one editing session; never persisted to the vault."""

    def __init__(self, scan, master, policy, pcm, size):
        self.token = uuid4().hex
        self.scan_id = scan['id']
        self.master_id = master['id']
        self.master = {'id': master['id'], 'label': master['label'], 'sha256': master['sha256'],
                       'sound': master['media']['sound']}
        self.policy = {'id': policy['id'], 'label': policy['label'],
                       'thresholds': policy['thresholds']}
        self.tuning = {key: policy['thresholds'][key] for key in TUNING}
        self.track = scan['tracks']['sound']
        self.pcm = pcm
        self.size = size
        self.extent = scan['extent']
        self.coverage = scan['coverage']['sound']
        self.baseline_hits = [hit for hit in scan['hits'] if hit['kind'] == 'silence']
        self.baseline_values = {}
        self.rehearsals = []


class RehearsalCache:
    """Bound the memory retained by offline rehearsal sessions."""

    def __init__(self):
        self.entries = OrderedDict()
        self.total = 0
        self.lock = Lock()

    def put(self, session):
        with self.lock:
            while self.entries and (len(self.entries) >= SESSION_LIMIT
                                    or self.total + session.size > CACHE_LIMIT):
                _token, old = self.entries.popitem(last=False)
                self.total -= old.size
            self.entries[session.token] = session
            self.total += session.size
        return session

    def get(self, token):
        with self.lock:
            if token not in self.entries:
                raise Missing('此预演材料已过期，请重新准备。')
            self.entries.move_to_end(token)
            return self.entries[token]

    def record(self, session, data):
        record = rehearse(session, data)
        with self.lock:
            if len(session.rehearsals) >= RUN_LIMIT:
                raise InputError('本次预演次数已达上限，请重新准备材料。')
            record['id'] = f'rehearsal-{len(session.rehearsals) + 1}'
            session.rehearsals.append(record)
        return record


def meter_for(track, rules, pcm):
    meter = SoundMeter(track, rules)
    for block in pcm:
        meter.push(block)
    return meter


def silence_hits(meter):
    _coverage, hits, _stats = meter.finish()
    return sorted((hit for hit in hits if hit['kind'] == 'silence'),
                  key=lambda hit: (hit['start_us'], hit['channel'] or 0, hit['end_us']))


def without_identity(hits):
    return [{key: value for key, value in hit.items() if key != 'id'} for hit in hits]


def prepare(vault, scan_id, cache):
    scan = vault.get('scan', scan_id)
    if not scan['tracks']['sound']:
        raise InputError('此扫描未选伴音轨，无法预演静音规则。')
    master = vault.get('master', scan['master_id'])
    policy = vault.get('policy', scan['policy_id'])
    path = vault.object_path(master['sha256'])
    pcm = [entry for entry in decode(path, scan['tracks']) if not isinstance(entry, Picture)]
    size = sum(len(block.samples) * block.samples.itemsize for block in pcm)
    session = Session(scan, master, policy, pcm, size)
    rules = Thresholds(**policy['thresholds'])
    baseline = meter_for(session.track, rules, pcm)
    session.baseline_values = {channel: list(baseline.window_values(channel))
                               for channel in range(baseline.channels)}
    if silence_hits(baseline) != without_identity(session.baseline_hits):
        raise Conflict('预演材料与基准扫描不一致，请重新扫描。')
    return describe(cache.put(session))


def describe(session):
    return {'token': session.token, 'id': session.token, 'session_id': session.token,
            'scan_id': session.scan_id, 'master': session.master,
            'policy': session.policy, 'tuning': session.tuning, 'extent': session.extent,
            'coverage': session.coverage, 'baseline_hits': session.baseline_hits,
            'rehearsals': session.rehearsals}


def rehearse(session, data):
    shape(data, (), TUNING)
    tuning = dict(session.tuning)
    tuning.update(data)
    rules = Thresholds.from_request({**session.policy['thresholds'], **tuning})
    tuning = {key: getattr(rules, key) for key in TUNING}
    trial = meter_for(session.track, rules, session.pcm)
    values = {channel: list(trial.window_values(channel)) for channel in range(trial.channels)}
    hits = silence_hits(trial)
    changes = [f'{TUNING_NAMES[key]} {session.tuning[key]}→{tuning[key]}{TUNING_UNITS[key]}'
               for key in TUNING if session.tuning[key] != tuning[key]]
    explanation = explain(session, rules, values, hits)
    return {'id': '', 'parameters': tuning, 'changes': changes, 'candidates': hits, 'hits': hits,
            'partial_windows': [span.as_dict() for span, value in values[0] if value is None],
            'explanation': explanation,
            'merged': [event for event in explanation if event['relation'] == 'merged'],
            'split': [event for event in explanation if event['relation'] == 'split'],
            'dropped': [event for event in explanation if event['relation'] == 'dropped'],
            'introduced': [event for event in explanation if event['relation'] == 'introduced']}


def overlaps(left, right):
    return left['start_us'] < right['end_us'] and right['start_us'] < left['end_us']


def local_windows(values, span):
    return [(at, value) for at, value in values if at.intersect(span)]


def silent_run_us(values, span, rms_limit):
    """Longest contiguous run of silent windows intersecting span, in microseconds."""
    best = current = 0
    end = None
    for at, value in local_windows(values, span):
        if value is not None and value <= rms_limit:
            current = current + at.duration if end == at.start else at.duration
            best = max(best, current)
            end = at.end
        else:
            current, end = 0, None
    return best


def explain(session, rules, values, hits):
    events = []
    base_rules = session.policy['thresholds']
    for channel in range(1, session.track['channels'] + 1):
        before = [hit for hit in session.baseline_hits if hit['channel'] == channel]
        after = [hit for hit in hits if hit['channel'] == channel]
        base_values = session.baseline_values[channel - 1]
        new_values = values[channel - 1]
        for hit in after:
            merged = [base for base in before if overlaps(base, hit)]
            if len(merged) > 1:
                events.append({'relation': 'merged', 'channel': channel, 'before': merged,
                               'after': [hit], 'reasons': merged_reasons(merged, base_values)})
        for hit in before:
            split = [trial for trial in after if overlaps(hit, trial)]
            if len(split) > 1:
                events.append({'relation': 'split', 'channel': channel, 'before': [hit],
                               'after': split, 'reasons': split_reasons(split, new_values, rules)})
            elif not split:
                events.append({'relation': 'dropped', 'channel': channel, 'before': [hit],
                               'after': [], 'reasons': dropped_reasons(hit, new_values, rules)})
        for hit in after:
            if not any(overlaps(base, hit) for base in before):
                events.append({'relation': 'introduced', 'channel': channel, 'before': [],
                               'after': [hit],
                               'reasons': introduced_reasons(hit, base_values, base_rules)})
    for event in events:
        event['label'] = RELATION_LABELS[event['relation']]
    return events


def merged_reasons(before, base_values):
    reasons = []
    for left, right in zip(before, before[1:]):
        bridge = Span(left['end_us'], right['start_us'])
        values = [value for _at, value in local_windows(base_values, bridge) if value is not None]
        detail = f'基准窗值最高 {max(values)}，' if values else ''
        reasons.append(f'间隔段 {bridge.start}–{bridge.end} 微秒{detail}新参数下并入静音窗')
    return reasons


def split_reasons(after, new_values, rules):
    reasons = []
    for left, right in zip(after, after[1:]):
        region = Span(left['end_us'], right['start_us'])
        windows = local_windows(new_values, region)
        if any(value is None for _at, value in windows):
            reasons.append(f'间隔段 {region.start}–{region.end} 微秒音窗未覆盖齐，按规不计，候选断开')
        values = [value for _at, value in windows if value is not None]
        if values:
            reasons.append(f'间隔段 {region.start}–{region.end} 微秒窗值 {min(values)}–{max(values)} '
                           f'超过静音门槛 {rules.silence_rms}，候选拆开')
    return reasons


def dropped_reasons(hit, new_values, rules):
    span = Span.parse(hit)
    reasons = []
    if any(value is None for _at, value in local_windows(new_values, span)):
        reasons.append('缺口旁音窗未覆盖齐，按规剔除')
    run = silent_run_us(new_values, span, rules.silence_rms)
    values = [value for _at, value in local_windows(new_values, span) if value is not None]
    if run:
        reasons.append(f'剩余连续静音 {run // 1000} 毫秒不足最短事件 {rules.silence_min_ms} 毫秒')
    elif values:
        reasons.append(f'窗值 {min(values)}–{max(values)} 超过静音门槛 {rules.silence_rms}')
    else:
        reasons.append('覆盖内没有完整音窗')
    return reasons


def introduced_reasons(hit, base_values, base_rules):
    span = Span.parse(hit)
    run = silent_run_us(base_values, span, base_rules['silence_rms'])
    minimum = base_rules['silence_min_ms']
    if run < minimum * 1000:
        return [f'基准参数下连续静音 {run // 1000} 毫秒不足最短事件 {minimum} 毫秒，新参数下成立']
    return [f'基准静音门槛 {base_rules["silence_rms"]} 下窗值超限，新参数下成立']
