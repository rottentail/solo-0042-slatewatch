"""Read recent records and resolve readable labels without exposing byte paths."""

from .render import fragment, page
from .text import options, safe


def label(vault, scan):
    master = vault.get('master', scan['master_id'])
    return f'{master["label"]} · {policy_name(vault, scan)} · {scan["id"][-7:]}'


def policy_name(vault, scan):
    return vault.get('policy', scan['policy_id'])['label']


def scan_choices(vault, selected=''):
    scans = vault.recent('scan')
    if selected and all(item['id'] != selected for item in scans):
        item = vault.get('scan', selected, required=False)
        if item:
            scans.append(item)
    return options(((item['id'], label(vault, item)) for item in scans), selected)


def sidebar(vault, current):
    items = []
    scans = vault.recent('scan')
    for scan in scans:
        master = vault.get('master', scan['master_id'])
        key = scan['id']
        active = ' aria-current="page"' if key == current else ''
        items.append(f'<a href="/desk/scans/{key}"{active}><strong>{safe(master["label"])}</strong>'
                     f'<small>{safe(policy_name(vault, scan))} · {len(scan["hits"])} 项</small></a>')
    return fragment('sidebar', items=''.join(items) or '<p class="muted">暂无扫描</p>')


def intake(vault):
    masters = vault.recent('master')
    policies = vault.recent('policy')
    return page('母带与规则', 'intake', 'intake',
                masters=options(((m['id'], m['label']) for m in masters)),
                policies=options(((p['id'], p['label']) for p in policies)),
                pending='' if masters and policies else ' disabled')
