"""Recipient disclosure lists, frozen details and authorization forms."""

from . import forms
from .render import page
from .text import STATUS, safe


def index(app):
    records = app.vault.recent('disclosure')
    rows = []
    for item in records:
        key = item['id']
        rows.append(
            f'<tr><th><a href="/desk/disclosures/{key}">{key}</a></th>'
            f'<td>{safe(item["recipient"])}</td><td>{STATUS[item["review_status"]]}</td>'
            f'<td><code>{safe(item["sha256"][:16])}</code></td></tr>'
        )
    return page('披露件', 'disclosures', 'disclosures',
                rows=''.join(rows) or '<tr><td colspan="4">暂无披露件</td></tr>')


def detail(app, key, verified=False):
    item = app.vault.get('disclosure', key)
    if verified:
        app.verify_disclosure(key)
    state = '<p role="status" class="good">全包哈希、成员索引与各文件 SHA-256 均已通过校验。</p>' if verified else ''
    return page('披露件详情', 'disclosures', 'disclosure', key=key,
                recipient=safe(item['recipient']), digest=safe(item['sha256']),
                status=STATUS[item['review_status']], scan=safe(item['scan_id']),
                master=safe(item['master_sha256']), members=len(item['members']),
                verified=state)


def form(scan, hit):
    """Authorization form rendered inside the finding panel."""
    sound = scan['tracks']['sound']
    channels = sound['channels'] if sound else 0
    if hit['channel'] is not None:
        channel_note = f'该伴音异常只授权源声道 {hit["channel"]}，不随包带出其他声道。'
        channel_field = (f'<input name="channel" value="{hit["channel"]}" type="hidden">'
                         f'<p class="muted">{channel_note}</p>')
    elif channels:
        boxes = ''.join(
            f'<label class="channel"><input type="checkbox" name="channel-{n}" value="1" checked>'
            f' 声道 {n}</label>' for n in range(1, channels + 1)
        )
        channel_field = f'<fieldset class="channels"><legend>授权声道（未勾选的声道不会出现在包内）</legend>{boxes}</fieldset>'
    else:
        channel_field = '<p class="muted">本扫描未选伴音轨，披露件只含画面材料。</p>'
    return (
        '<details class="disclosure-form"><summary>制作授权披露件</summary>'
        '<form method="post" action="/desk/scans/' + safe(scan['id']) + '/disclosure" class="actions">'
        f'<input name="hit" value="{safe(hit["id"])}" type="hidden">'
        '<label>接收方<input name="recipient" required maxlength="120" placeholder="承包商名称"></label>'
        '<label>授权留边（毫秒，0 为不带出窗口外材料）'
        '<input name="padding_ms" type="number" min="0" max="2000" value="0" required></label>'
        '<label>画格数<input name="frames" type="number" min="1" max="8" value="3" required></label>'
        f'{channel_field}'
        '<label>授权说明（可选）<input name="note" maxlength="350"></label>'
        '<button class="primary" type="submit">生成冻结披露件</button>'
        '</form></details>'
    )


def parse_form(scan, data):
    """Convert a browser form into one service-layer disclosure grant."""
    from ..clock import InputError

    hit_id = data.get('hit', '')
    if not hit_id:
        raise InputError('缺少候选编号。')
    grant = {'hit_id': hit_id,
             'padding_ms': forms.number(data, 'padding_ms', 0),
             'frames': forms.number(data, 'frames', 3)}
    sound = scan['tracks']['sound']
    if sound:
        channels = [n for n in range(1, sound['channels'] + 1) if data.get(f'channel-{n}') == '1']
        if data.get('channel'):
            channels = [forms.number(data, 'channel')]
        if not channels:
            raise InputError('披露件至少需要授权一个伴音声道，或选择画面类候选。')
        grant['channels'] = channels
    return {'recipient': data.get('recipient', ''),
            'note': data.get('note') or None, 'grants': [grant]}
