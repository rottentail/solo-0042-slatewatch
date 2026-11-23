"""Side-by-side frame cross-check at requested programme instants."""
import json
import re
from io import BytesIO
from urllib.parse import urlencode
from zipfile import ZipFile

from ..clock import InputError
from . import forms
from .library import scan_choices
from .preview import png
from .render import page
from .text import FLAGS, VERDICTS, safe, timestamp


def parse_points(raw):
    tokens = [token for token in re.split(r'[,\s]+', raw.strip()) if token]
    if not tokens:
        raise InputError('请给出至少一个节目时点。')
    points = []
    for token in tokens:
        if not token.isascii() or not token.isdecimal() or len(token) > 12:
            raise InputError('节目时点需要微秒整数值，以逗号或空格分隔。')
        points.append(int(token))
    return points


def request_data(params):
    data = {'left': params.get('left', ''), 'right': params.get('right', ''),
            'points_us': parse_points(params.get('points', ''))}
    if params.get('tolerance_milli'):
        data['tolerance_milli'] = forms.number(params, 'tolerance_milli')
    return data


def frame_cell(files, token, tag, side):
    if not side['covered']:
        hole = side['hole']
        start = timestamp(hole['start_us']) if hole['start_us'] is not None else '片头之前'
        end = timestamp(hole['end_us']) if hole['end_us'] is not None else '片尾之后'
        return (f'<figure><div class="missing-frame">无画格覆盖</div>'
                f'<figcaption><strong>短洞</strong><small>{start} – {end}</small></figcaption></figure>')
    name = files[tag].replace('/', '-').removesuffix('.pgm') + '.png'
    window = side['window']
    held = ' · 长格' if side['long_frame'] else ''
    return (f'<figure><img src="/desk/previews/{token}/{name}" alt="画格">'
            f'<figcaption><strong>源帧 {timestamp(side["source_us"])}</strong>'
            f'<small>窗口 {timestamp(window["start_us"])} – {timestamp(window["end_us"])} · '
            f'格长 {side["duration_us"] / 1000:.3f} ms / 标称 {side["nominal_us"] / 1000:.3f} ms{held}</small>'
            f'</figcaption></figure>')


def row_html(row, token):
    flags = ''.join(f'<span class="flag">{FLAGS[flag]}</span>' for flag in row['flags'])
    cells = ''.join(frame_cell(row['files'], token, tag, row[tag]) for tag in ('left', 'right'))
    measures = row['metrics']
    detail = ''
    if measures:
        detail = (f'<p class="muted">内容差 {measures["delta_milli"]}‰ · 去亮度差 {measures["centered_delta_milli"]}‰ · '
                  f'边缘差 {measures["edge_milli"]}‰ · 亮度偏移 {measures["luma_shift_milli"]}‰ · '
                  f'原时标差 {row["source_delta_us"]:+d} µs</p>')
    return (f'<section class="probe"><header><h3>{timestamp(row["point_us"])}</h3>'
            f'<span class="state {row["verdict"]}">{VERDICTS[row["verdict"]]}</span>{flags}</header>'
            f'<div class="frames">{cells}</div>{detail}</section>')


def show(app, cache, params):
    left, right = params.get('left', ''), params.get('right', '')
    points, tolerance = params.get('points', ''), params.get('tolerance_milli', '')
    results = ''
    if left or right or points:
        if not left or not right or not points:
            raise InputError('需同时选定两份扫描并给出节目时点。')
        bundle = app.matchup_evidence(request_data(params))
        with ZipFile(BytesIO(bundle)) as archive:
            report = json.loads(archive.read('matchup.json'))
            assets = {}
            for row in report['points']:
                for tag in ('left', 'right'):
                    name = row['files'][tag]
                    if name:
                        flat = name.replace('/', '-').removesuffix('.pgm') + '.png'
                        assets[flat] = (png(archive.read(name)), 'image/png')
        token = cache.put(report, assets)
        summary = report['summary']
        counts = ' · '.join(f'{VERDICTS[key]} {summary[key]}' for key in
                            ('same_frame', 'timestamp_offset', 'different_content',
                             'left_missing', 'right_missing', 'both_missing') if summary[key])
        query = urlencode({'left': left, 'right': right, 'points': points,
                           **({'tolerance_milli': tolerance} if tolerance else {})})
        rows = ''.join(row_html(row, token) for row in report['points'])
        results = (f'<section aria-label="对拍结果"><h2>对拍结果</h2>'
                   f'<p>{counts} · 标记 {summary["flagged"]} 处</p>'
                   f'<p><a class="button" href="/desk/matchup/evidence?{query}">下载对拍证据 ZIP</a></p>'
                   f'{rows}</section>')
    return page('画格对拍', 'matchup', 'matchup',
                left=scan_choices(app.vault, left), right=scan_choices(app.vault, right),
                points=safe(points), tolerance=safe(tolerance or '4000'), results=results)
