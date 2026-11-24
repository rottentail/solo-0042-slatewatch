"""Entry point: regenerate inputs, observe the service, simulate both clocks."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from slatewatch.http import Server
from tools.preview_sync import probe
from tools.preview_sync.engine import ENGINE_SCENARIOS, Timeline, summary, sweep


def build_timeline(reel, observed):
    entry = observed[reel]['evidence']
    present = [p for p in entry['picture_points'] if p['present']]
    frames = [p['source_us'] for p in present]
    coverage = observed[reel]['coverage']['picture']
    # Half-open frame ends come from the next present frame, last frame ends at
    # its coverage span end - identical to picture.PictureMeter's accounting.
    frame_end = [frames[i + 1] if i + 1 < len(frames) else coverage[-1]['end_us']
                 for i in range(len(frames))]
    coverage_spans = [(span['start_us'], span['end_us']) for span in coverage]
    sound = entry['sound_mapping']
    if sound is None:
        rate = first = 0
        filled = []
    else:
        rate, first = sound['sample_rate'], sound['first_source_sample']
        # clock.json lists the *uncovered* (zero-filled) sample ranges; invert.
        holes = [(z['first'], z['end']) for z in sound['filled_sample_ranges']]
        filled = []
        cursor = first
        for h_first, h_end in holes:
            if cursor < h_first:
                filled.append((cursor, h_first))
            cursor = h_end
        if cursor < sound['end_source_sample']:
            filled.append((cursor, sound['end_source_sample']))
    return Timeline(entry['span_us'][0], entry['span_us'][1], frames, frame_end,
                    coverage_spans, rate, first, filled)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='only run the service probe')
    parser.add_argument('--engine', action='store_true',
                        help='rerun simulation on a saved probe report (needs --report)')
    parser.add_argument('--out', type=Path, help='write JSON report to this path')
    parser.add_argument('--report', type=Path,
                        help='a prior --probe/--out JSON; lets --engine run without the service')
    options = parser.parse_args()
    both = not options.probe and not options.engine
    report = {}
    if options.engine and options.report:
        report['probe'] = json.loads(options.report.read_text(encoding='utf-8'))['probe']
    with TemporaryDirectory(prefix='slatewatch-clock-') as directory:
        workdir = Path(directory)
        if options.probe or both:
            with Server(('127.0.0.1', 0), workdir / 'vault') as server:
                thread = Thread(target=server.serve_forever)
                thread.start()
                try:
                    report['probe'] = probe.observe(server, workdir)
                finally:
                    server.shutdown()
                    thread.join()
        if 'probe' not in report:
            raise SystemExit('--engine needs probe data: rerun without flags, or pass --report from a prior --probe --out run')
        if options.engine or both:
            report['simulation'] = {}
            for reel in ('no-sound', 'lead-pad', 'fractional-hole'):
                timeline = build_timeline(reel, report['probe'])
                report['simulation'][reel] = summary(
                    sweep(timeline, ENGINE_SCENARIOS, seeds=range(1, 25)))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if options.out:
        options.out.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
