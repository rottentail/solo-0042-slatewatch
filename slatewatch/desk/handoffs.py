"""Blind second-workstation review pages bound to one frozen evidence package."""

import json
from zipfile import ZipFile

from ..clock import Conflict
from .preview import png
from .render import fragment, page
from .text import KINDS, badge, interval, lane, safe, timestamp

SECOND = {'accept': '通过', 'hold': '扣留'}
SECOND_STATE = {'accept': 'accepted', 'hold': 'held'}
RELATIONS = {'agree': '一致', 'disagree': '分歧', 'no_prior': '冻结时无人工裁决', 'unknown': '证据分歧，无法核对'}


def index(app):
    rows = []
    for item in app.vault.recent('handoff'):
        view = app.handoff_view(item['id'])
        done, total = len(view['rulings']), len(item['hit_ids'])
        state = '<span class="state pass">可核对</span>' if view['complete'] else '<span class="state pending">盲核进行中</span>'
        rows.append(f'<tr><th><a href="/desk/handoffs/{item["id"]}">{item["id"]}</a></th>'
                    f'<td><code>{item["seal_sha256"][:16]}</code></td><td>{total} 项</td>'
                    f'<td>{done}/{total} {state}</td></tr>')
    return page('盲核交接', 'handoffs', 'handoffs',
                rows=''.join(rows) or '<tr><td colspan="4">暂无盲核交接，请从冻结包详情发起。</td></tr>')


def load_evidence(app, record):
    """Read the verified frozen package; never exposes the sealed review."""
    try:
        app.verify(record['seal_id'])
    except Conflict as exc:
        raise Conflict('冻结证据未通过校验，无法据此盲核。') from exc
    seal_record = app.vault.get('seal', record['seal_id'])
    path = app.vault.object_path(seal_record['sha256'])
    with ZipFile(path) as archive:
        maps = {mapping['hit_id']: mapping for mapping in json.loads(archive.read('evidence.json'))}
        assets = {}
        for hit_id, mapping in maps.items():
            for frame in mapping['picture']:
                if frame['file']:
                    raw = archive.read(f'evidence/{hit_id}/{frame["file"]}')
                    name = f'{hit_id}--{frame["file"].removesuffix(".pgm")}.png'
                    assets[name] = (png(raw), 'image/png')
            if mapping['sound']:
                assets[f'{hit_id}--sound.wav'] = (archive.read(f'evidence/{hit_id}/sound.wav'), 'audio/wav')
    return maps, assets


def frames_html(base, hit_id, mapping):
    frames = []
    for item in mapping['picture']:
        label = timestamp(item['target_us'])
        if item['file']:
            name = f'{hit_id}--{item["file"].removesuffix(".pgm")}.png'
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        frames.append(f'<figure>{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    return ''.join(frames)


def sound_html(base, hit_id, mapping):
    track = mapping['sound']
    if track is None:
        return '<p class="muted">未选伴音轨</p>'
    filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
    return (f'<audio controls preload="metadata" src="{base}/{hit_id}--sound.wav"></audio>'
            f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
            f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
            f'<p>补零区间 <code>{filled or "无"}</code></p>')


def show(app, cache, key):
    view = app.handoff_view(key)
    scan = app.vault.get('scan', view['scan_id'])
    master = app.vault.get('master', scan['master_id'])
    maps, assets = load_evidence(app, view)
    token = cache.put({'handoff': key}, assets)
    base = f'/desk/previews/{token}'
    own = {event['hit_id']: event for event in view['rulings']}
    hits = {hit['id']: hit for hit in scan['hits']}
    sections = []
    for hit_id in view['hit_ids']:
        hit = hits[hit_id]
        mapping = maps[hit_id]
        event = own.get(hit_id)
        note = (f'已判读 · {SECOND[event["outcome"]]} · {safe(event["reviewer"])} · {safe(event["reason"])}'
                if event else '尚未判读')
        sections.append(fragment('blindhit', handoff=key, hit=hit_id, title=KINDS[hit['kind']],
                                 lane=lane(hit), span=interval(hit), note=note,
                                 frames=frames_html(base, hit_id, mapping),
                                 sound=sound_html(base, hit_id, mapping)))
    done, total = len(own), len(view['hit_ids'])
    state = f'<span class="state {"pass" if view["complete"] else "pending"}">判读进度 {done}/{total}</span>'
    reconcile = (f'<a class="button primary" href="/desk/handoffs/{key}/reconciliation">核对结论</a>'
                 if view['complete'] else '<span class="muted">判读齐后开放核对</span>')
    return page('盲核判读', 'handoffs', 'handoff', key=key, master=safe(master['label']),
                fingerprint=safe(view['seal_sha256'][:16]), progress=state, reconcile=reconcile,
                sections=''.join(sections))


def reconciliation(app, key):
    result = app.reconciliation(key)
    if result['evidence']['status'] == 'verified':
        evidence = ('<p role="status" class="good">同一冻结证据核验通过，两人判读基于同一包。</p>')
    else:
        evidence = (f'<p role="alert" class="error">证据确实不一样：{safe(result["evidence"]["detail"])}'
                    '，结论分歧不能归因于判读。</p>')
    if result['old_conclusion'] == 'changed':
        old = f'<p role="alert" class="error">旧结论已变化：{len(result["changed_hit_ids"])} 项候选的当前裁决与冻结时不一致。</p>'
    elif result['old_conclusion'] == 'unchanged':
        old = '<p role="status" class="good">旧结论未变化：当前裁决与冻结时一致。</p>'
    else:
        old = '<p role="alert" class="error">证据分歧，旧结论无法核对。</p>'
    rows = []
    for hit in result['hits']:
        second = hit['second']
        frozen = badge(hit['frozen_status']) if hit['frozen_status'] else '<span class="muted">不可知</span>'
        current = badge(hit['current_status'])
        if hit['old_changed']:
            current += ' <span class="state pending">已变化</span>'
        blind = (f'<span class="state {SECOND_STATE[second["outcome"]]}">{SECOND[second["outcome"]]}</span>'
                 f'<br><small>{safe(second["reviewer"])} · {safe(second["reason"])}</small>')
        rows.append(f'<tr><th>{KINDS[hit["kind"]]}</th><td>{lane(hit)}</td>'
                    f'<td class="time">{interval(hit)}</td><td>{frozen}</td><td>{current}</td>'
                    f'<td>{blind}</td><td>{RELATIONS[hit["relation"]]}</td></tr>')
    return page('盲核核对', 'handoffs', 'reconciliation', key=key, evidence=evidence, old=old,
                rows=''.join(rows), scan=safe(result['scan_id']), seal=safe(result['seal_id']))
