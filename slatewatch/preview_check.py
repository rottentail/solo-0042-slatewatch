"""Re-runnable evidence-preview verification over a real loopback HTTP desk.

The run builds the deterministic ``grid-cells`` master, scans it (the scan has
a picture-gap hit and a freeze hit), then cross-checks four evidence surfaces
against ground truth decoded independently from the master:

1. the on-demand evidence ZIP's PGM frames and clock mapping;
2. the browser preview's PNG frames served from the in-process preview cache;
3. a frozen seal bundle, whose PGM members are read with an independent parser;
4. negative controls that prove the checks actually catch the three failure
   modes: a wrong source cell, a pixel-changing PNG/PGM conversion, and a
   picture hole filled from the previous frame.

Run it with ``python -m slatewatch.preview_check``.
"""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
from zipfile import ZipFile

from . import evidence_check as check
from .http import PREFIX, Server
from .reels import (DUP_CELLS, EXPECTED_GRID_FRAMES, GRID_HOLE, TWIN_CELLS, make)

PADDING_MS = 120
FRAMES = 4


def http_get(origin, path):
    with urlopen(Request(origin + path), timeout=20) as response:
        return response.headers.get_content_type(), response.read()


def http_post_json(origin, path, payload):
    request = Request(origin + path, data=json.dumps(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def http_post_form(desk_origin, path, fields):
    request = Request(desk_origin + path, data=urlencode(fields).encode(), method='POST',
                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                               'Origin': desk_origin})
    with urlopen(request, timeout=30) as response:
        return response.url, response.headers.get_content_type(), response.read()


def build_scan(origin, workdir):
    master_path = make(workdir / 'grid-cells.mkv', 'grid-cells')
    request = Request(origin + '/masters?label=grid-cells&sound=none',
                      data=master_path.read_bytes(), method='POST',
                      headers={'Content-Type': 'application/octet-stream'})
    with urlopen(request, timeout=30) as response:
        master = json.loads(response.read())
    policy = http_post_json(origin, '/policies', {'label': '格子核验规则'})
    scan = http_post_json(origin, '/scans',
                          {'master_id': master['id'], 'policy_id': policy['id']})
    return master, scan


def hit_map(scan):
    kinds = {hit['kind']: hit for hit in scan['hits']}
    missing = {'picture_gap', 'freeze'} - set(kinds)
    if missing:
        raise check.CheckFailure(f'grid-cells scan must produce {sorted(missing)} hits')
    return kinds


class SpanProxy:
    def __init__(self, start, end):
        self.start = start
        self.end = end


def load_truth(origin, scan, workdir):
    with urlopen(Request(origin + f"/masters/{scan['master_id']}"), timeout=20) as response:
        master = json.loads(response.read())
    truth = check.ground_truth(workdir / 'grid-cells.mkv', master['media'])
    coverage = [SpanProxy(span['start_us'], span['end_us']) for span in scan['coverage']['picture']]
    return master, truth, coverage


def evidence_zip(origin, scan, hit):
    payload = {'hit_ids': [hit['id']], 'padding_ms': PADDING_MS, 'frames': FRAMES}
    request = Request(origin + f"/scans/{scan['id']}/evidence",
                      data=json.dumps(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        return response.read()


def zip_mapping_and_loader(blob, hit_id):
    archive = ZipFile(BytesIO(blob))
    mapping = json.loads(archive.read(f'{hit_id}/clock.json'))

    def loader(name):
        return archive.read(f'{hit_id}/{name}')

    return mapping, loader, archive


def preview_assets(desk_origin, api_origin, scan, hit):
    route = f"/desk/scans/{scan['id']}/preview"
    final_url, _mime, page = http_post_form(desk_origin, route, {
        'hit': hit['id'], 'padding_ms': str(PADDING_MS), 'frames': str(FRAMES)})
    token = final_url.rsplit('/desk/previews/', 1)[1]
    _mime, html = http_get(desk_origin, f'/desk/previews/{token}')
    # The preview mapping is not embedded as JSON; reconstruct it from the
    # evidence ZIP mapping and the page's figure order, then fetch each PNG.
    blob = evidence_zip(api_origin, scan, hit)
    mapping, _pgm_loader, archive = zip_mapping_and_loader(blob, hit['id'])
    page_text = html.decode('utf-8')
    png_names = []
    for entry in mapping['picture']:
        if entry['file']:
            png_name = entry['file'].removesuffix('.pgm') + '.png'
            if png_name not in page_text:
                raise check.CheckFailure(f'preview page does not expose {png_name}')
            png_names.append(png_name)
    assets = {}
    for png_name in png_names:
        _mime, data = http_get(desk_origin, f'/desk/previews/{token}/{png_name}')
        if not data.startswith(check.PNG_MAGIC):
            raise check.CheckFailure(f'{png_name} was not served as a PNG')
        assets[png_name] = data
    archive.close()

    def loader(name):
        return assets[name]

    png_mapping = {'picture': [
        {**entry, 'file': entry['file'].removesuffix('.pgm') + '.png'} if entry['file'] else entry
        for entry in mapping['picture']]}
    return token, png_mapping, loader


def seal_bundle(origin, scan, hit):
    payload = {'hit_ids': [hit['id']], 'padding_ms': PADDING_MS, 'frames': FRAMES}
    sealed = http_post_json(origin, f"/scans/{scan['id']}/seals", payload)
    with urlopen(Request(origin + f"/seals/{sealed['id']}/bundle"), timeout=30) as response:
        bundle = response.read()
    return sealed, bundle


def verify_bundle_pgm(sealed, bundle, truth, coverage, expected_hit_ids):
    """Read every frozen PGM through the independent parser only."""
    with ZipFile(BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        descriptor = json.loads(archive.read('index.json'))
        evidence = json.loads(archive.read('evidence.json'))
        if descriptor['id'] != sealed['id']:
            raise check.CheckFailure('seal descriptor id does not match the served record')
        seen = []
        for mapping in evidence:
            hit_id = mapping['hit_id']
            if hit_id not in expected_hit_ids:
                raise check.CheckFailure(f'unexpected excerpt {hit_id} in sealed bundle')

            def loader(name, _hit_id=hit_id):
                data = archive.read(f'evidence/{_hit_id}/{name}')
                # Independent read path: parse before any comparison.
                check.read_pgm(data)
                return data

            check.verify_mapping(mapping, truth, coverage, loader, f'seal:{hit_id}')
            frame_files = {n.rsplit('/', 1)[1] for n in names
                           if n.startswith(f'evidence/{hit_id}/frame-')}
            mapped_files = {entry['file'] for entry in mapping['picture'] if entry['file']}
            if frame_files != mapped_files:
                raise check.CheckFailure(
                    f'seal frame membership {sorted(frame_files)} != clock mapping {sorted(mapped_files)}')
            seen.append(hit_id)
        if sorted(seen) != sorted(expected_hit_ids):
            raise check.CheckFailure(f'seal is missing excerpts: {sorted(set(expected_hit_ids) - set(seen))}')
    return {'members': len(names), 'verified_excerpts': len(evidence)}


def run(server, root):
    host, port = server.server_address
    api_origin = f'http://{host}:{port}{PREFIX}'
    desk_origin = f'http://{host}:{port}'
    workdir = root / 'grid-media'
    workdir.mkdir(parents=True, exist_ok=True)

    master, scan = build_scan(api_origin, workdir)
    if scan['coverage']['sound'] is not None:
        raise check.CheckFailure('grid-cells must be registered without a sound track')
    hits = hit_map(scan)
    gap_hit, freeze_hit = hits['picture_gap'], hits['freeze']
    _master_descriptor, truth, coverage = load_truth(api_origin, scan, workdir)
    if len(truth) != EXPECTED_GRID_FRAMES:
        raise check.CheckFailure(
            f'ground truth expects {EXPECTED_GRID_FRAMES} decoded frames, got {len(truth)}')
    coverage_spans = [(span.start, span.end) for span in coverage]
    if coverage_spans != [(0, 2_000_000), (2_520_000, 4_000_000)]:
        raise check.CheckFailure(f'unexpected picture coverage {coverage_spans}')

    report = {'master': master['sha256'][:16], 'scan': scan['id'], 'checks': []}

    # 1. On-demand evidence ZIP, PGM read independently.
    gap_blob = evidence_zip(api_origin, scan, gap_hit)
    gap_mapping, gap_loader, gap_zip = zip_mapping_and_loader(gap_blob, gap_hit['id'])
    gap_verdicts = check.verify_mapping(gap_mapping, truth, coverage, gap_loader, 'zip:picture_gap')
    hole_targets = [v.target_us for v in gap_verdicts if v.status == 'missing']
    if len(hole_targets) != 2 or not all(GRID_HOLE[0] * 1_000_000 <= t < GRID_HOLE[1] * 1_000_000
                                         for t in hole_targets):
        raise check.CheckFailure(f'picture-hole targets were not preserved: {hole_targets}')

    freeze_blob = evidence_zip(api_origin, scan, freeze_hit)
    freeze_mapping, freeze_loader, freeze_zip = zip_mapping_and_loader(freeze_blob, freeze_hit['id'])
    freeze_verdicts = check.verify_mapping(freeze_mapping, truth, coverage, freeze_loader, 'zip:freeze')

    # Cell invariants on a head frame (one-pixel twin mark present).
    head_pixels = check.read_pgm(gap_loader('frame-01.pgm'))
    invariant = check.verify_cell_invariants(head_pixels, 'zip:frame-01')
    if check.cell_block(head_pixels, DUP_CELLS[0]) != check.cell_block(head_pixels, DUP_CELLS[1]):
        raise check.CheckFailure('duplicate gray cells must be byte-identical in the head frame')

    # Both excerpts independently land on the same boundary raster: the gap
    # excerpt keeps the last pre-hole frame (2,520,000us) and the freeze
    # excerpt's first frame (2,560,000us) is the identical frozen raster.
    boundary_gap = next(entry for entry in gap_mapping['picture'] if entry['source_us'] == 2_520_000)
    boundary_freeze = next(entry for entry in freeze_mapping['picture']
                           if entry['source_us'] == freeze_mapping['picture'][0]['source_us'])
    gap_boundary_pixels = check.read_pgm(gap_loader(boundary_gap['file']))
    freeze_boundary_pixels = check.read_pgm(freeze_loader(boundary_freeze['file']))
    if gap_boundary_pixels != freeze_boundary_pixels:
        raise check.CheckFailure('the two excerpts must select the shared boundary gray cells')
    shared = check.verify_shared_cell(gap_boundary_pixels, freeze_boundary_pixels)
    # A frozen frame keeps twin cells identical (the one-pixel mark is head-only).
    frozen_pixels = check.read_pgm(freeze_loader('frame-01.pgm'))
    frozen_twin = check.verify_frozen_twin(frozen_pixels)
    gap_zip.close()
    freeze_zip.close()
    report['checks'].append({'surface': 'evidence-zip-pgm',
                             'frames': [v.as_dict() for v in gap_verdicts + freeze_verdicts],
                             'hole_targets_us': hole_targets, 'cell_invariants': invariant,
                             'shared_boundary_cell': shared, 'frozen_twin': frozen_twin})

    # 2. Browser preview PNGs, decoded without the desk's own PNG routine.
    _gap_token, gap_png_mapping, gap_png_loader = preview_assets(desk_origin, api_origin, scan, gap_hit)
    check.verify_mapping(gap_png_mapping, truth, coverage, gap_png_loader, 'preview:picture_gap')
    freeze_token, freeze_png_mapping, freeze_png_loader = preview_assets(desk_origin, api_origin, scan, freeze_hit)
    check.verify_mapping(freeze_png_mapping, truth, coverage, freeze_png_loader, 'preview:freeze')
    # PNG and PGM must expose identical pixels on the comparable (covered) frames.
    with ZipFile(BytesIO(gap_blob)) as gap_archive:
        for entry, png_name in zip((e for e in gap_mapping['picture'] if e['file']),
                                   (e['file'] for e in gap_png_mapping['picture'] if e['file'])):
            packaged_pgm = gap_archive.read(f"{gap_hit['id']}/{entry['file']}")
            if check.read_png(gap_png_loader(png_name)) != check.read_pgm(packaged_pgm):
                raise check.CheckFailure(f'preview PNG {png_name} diverges from the packaged PGM pixels')
    report['checks'].append({'surface': 'browser-preview-png', 'preview_token': freeze_token,
                             'frames': len(freeze_png_mapping['picture'])})

    # 3. Frozen seal bundles: independent PGM reads, membership and hash index.
    # The gap bundle carries missing cells; the freeze bundle carries only
    # covered frames. Both must survive the independent frozen-PGM read path.
    sealed_gap, gap_bundle = seal_bundle(api_origin, scan, gap_hit)
    gap_seal_report = verify_bundle_pgm(sealed_gap, gap_bundle, truth, coverage, [gap_hit['id']])
    sealed_freeze, freeze_bundle = seal_bundle(api_origin, scan, freeze_hit)
    freeze_seal_report = verify_bundle_pgm(sealed_freeze, freeze_bundle, truth, coverage, [freeze_hit['id']])
    # The frozen PGM and the browser preview still agree on every comparable pixel.
    with ZipFile(BytesIO(freeze_bundle)) as archive:
        for entry in freeze_mapping['picture']:
            if not entry['file']:
                continue
            frozen = check.read_pgm(archive.read(f"evidence/{freeze_hit['id']}/{entry['file']}"))
            png_name = entry['file'].removesuffix('.pgm') + '.png'
            if frozen != check.read_png(freeze_png_loader(png_name)):
                raise check.CheckFailure(f'sealed PGM {entry["file"]} disagrees with the preview PNG')
    # The gap seal must preserve the hole as missing entries, not frame files.
    with ZipFile(BytesIO(gap_bundle)) as archive:
        gap_evidence = json.loads(archive.read('evidence.json'))[0]
        missing = [entry['target_us'] for entry in gap_evidence['picture'] if entry['file'] is None]
        if missing != hole_targets:
            raise check.CheckFailure(f'frozen gap bundle altered hole entries: {missing}')
    report['checks'].append({'surface': 'frozen-seal-pgm',
                             'gap_bundle': gap_seal_report, 'freeze_bundle': freeze_seal_report,
                             'frozen_hole_targets_us': missing})

    # 4. Negative controls: each planted defect must be detected.
    with ZipFile(BytesIO(gap_blob)) as archive:
        def zip_loader(name):
            return archive.read(f"{gap_hit['id']}/{name}")
        wrong = check.assert_detects_wrong_source(gap_mapping, truth, coverage, zip_loader, 'negative:wrong-source')
        tamper_pgm = check.assert_detects_pixel_tamper(gap_mapping, truth, coverage, zip_loader,
                                                       'negative:pgm-pixel')
        filled = check.assert_detects_filled_hole(gap_mapping, truth, coverage, zip_loader,
                                                  'negative:filled-hole')
    tamper_png = check.assert_detects_pixel_tamper(gap_png_mapping, truth, coverage, gap_png_loader,
                                                   'negative:png-pixel')
    report['checks'].append({'surface': 'negative-controls',
                             'wrong_source_detected': wrong,
                             'pgm_pixel_tamper_detected': tamper_pgm,
                             'png_pixel_tamper_detected': tamper_png,
                             'filled_hole_detected': filled,
                             'twin_pair': list(TWIN_CELLS), 'duplicate_pair': list(DUP_CELLS)})
    return report


def main():
    with TemporaryDirectory(prefix='slatewatch-preview-check-') as directory:
        root = Path(directory)
        with Server(('127.0.0.1', 0), root / 'vault') as server:
            runner = Thread(target=server.serve_forever)
            runner.start()
            try:
                report = run(server, root)
            finally:
                server.shutdown()
                runner.join()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('证据预览核验：源格选择、PNG/PGM 像素一致性、缺画格保持与冻结包独立读法全部通过')


if __name__ == '__main__':
    main()
