"""Render the offline silence-recipe rehearsal workbench for one session."""

from ..rehearse import RELATION_LABELS
from .render import fragment, page
from .text import interval, safe

RELATIONS = RELATION_LABELS


def candidate_rows(candidates):
    return ''.join(
        f'<tr><th scope="row">静音</th><td>伴音 {hit["channel"]}</td>'
        f'<td class="time">{interval(hit)}</td><td>{hit["bins"]} 窗</td>'
        f'<td>{hit["minimum"]}–{hit["maximum"]}</td></tr>'
        for hit in candidates
    ) or '<tr><td colspan="5">没有静音候选。</td></tr>'


def event_item(event):
    before = '、'.join(interval(hit) for hit in event['before']) or '无'
    after = '、'.join(interval(hit) for hit in event['after']) or '无'
    reasons = ''.join(f'<li>{safe(reason)}</li>' for reason in event['reasons'])
    relation = event['relation']
    return (f'<li><strong class="tag-{relation}">{RELATIONS[relation]}</strong>（伴音 {event["channel"]}）'
            f'{before} → {after}<ul>{reasons}</ul></li>')


def run_section(token, policy_label, record):
    changes = '；'.join(record['changes']) or '参数与基准一致'
    partial = '、'.join(interval(span) for span in record['partial_windows']) or '无'
    events = ''.join(event_item(event) for event in record['explanation']) or '<li>候选没有结构性变化。</li>'
    return fragment('rehearsal_run', token=token, anchor=f'run-{record["id"]}', title=safe(record['id']),
                    changes=safe(changes), partial=safe(partial), rows=candidate_rows(record['candidates']),
                    events=events, run_id=record['id'],
                    label=safe(f'{policy_label}·{record["id"]}'))


def show(app, token, selected):
    desc = app.rehearsal_session(token)
    runs = desc['rehearsals']
    current = next((record for record in runs if record['id'] == selected),
                   runs[-1] if runs else None)
    prefill = current['parameters'] if current else desc['tuning']
    sections = ''.join(run_section(token, desc['policy']['label'], record)
                       for record in reversed(runs))
    return page('静音配方预演', 'scan', 'rehearsal',
                token=token, master=safe(desc['master']['label']),
                source=safe(desc['master']['sha256'][:12]),
                policy=safe(desc['policy']['label']), scan=desc['scan_id'],
                baseline_rows=candidate_rows(desc['baseline_hits']),
                window=prefill['audio_window_ms'], rms=prefill['silence_rms'],
                min_ms=prefill['silence_min_ms'],
                runs=sections or '<p class="muted">还没有预演结果，调整参数后试算。</p>')
