"""Internal verification for the sound window and rule boundaries.

This module is not exposed through the HTTP API and adds no user-facing
configuration.  It gives maintainers two independent ways to check the
refactored sound pipeline:

* :func:`media_check` builds one 44.1 kHz stereo clip whose packets cross
  window boundaries, whose coverage hole only drains part of a window, and
  whose two channels clip over different sample ranges.  The clip is decoded
  exactly once and the same native PCM feeds both grid phases.  Every window
  statistic is then recomputed independently straight from those decoded
  blocks.
* :func:`rule_check` feeds hand-built :class:`~slatewatch.sound.Window`
  objects straight into the rule synthesis, without touching media or PCM,
  to pin down the full-window gate, gap-induced candidate breaks, channel
  isolation and minimum event length boundary.
"""
from array import array
from dataclasses import asdict
from fractions import Fraction
from math import isqrt, pi, sin
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import json

import av

from .clock import Span, sample_boundary
from .hits import candidates
from .media import PCM, decode, inspect
from .rules import Thresholds
from .sound import SoundMeter, Window, channel_hits, window_reading

RATE = 44100
CHANNELS = 2
WINDOW_MS = 50
STEP_US = WINDOW_MS * 1000
WINDOW_SAMPLES = RATE * WINDOW_MS // 1000
TOTAL_SAMPLES = WINDOW_SAMPLES * 16
# The planned last sample lands one full window before the table end so the
# final grid window is partial by construction, exercising the full-window
# gate at the clip tail on both phases.
COVERED_END = WINDOW_SAMPLES * 15
# 441 samples equal an exact 10 ms span at 44.1 kHz, which is representable
# on every supported container time base, so contiguous packets never
# accumulate timestamp drift.  Five packets cover one 50 ms window, and the
# packet phase differs from the window grid phase.
FRAME_SAMPLES = 441
# The single coverage hole.  It stays inside one phase-zero window while the
# half-shifted grid spreads it across two windows, so the two grids disagree
# about which windows are decidable.  The 882-sample span is an exact 20 ms
# interval at 44.1 kHz, which the container's 1 ms audio time base represents
# without rounding, so the hole survives decoding at its exact position.
HOLE = (12000, 12882)
SILENCE = (WINDOW_SAMPLES * 3, WINDOW_SAMPLES * 9)
LEFT_CLIP = (WINDOW_SAMPLES * 10, WINDOW_SAMPLES * 12)
RIGHT_CLIP = (WINDOW_SAMPLES * 11, WINDOW_SAMPLES * 14)
TONE_AMPLITUDE = 1000
PHASE_US = STEP_US // 2


def _clip_value(sample, channel):
    if channel == 0 and LEFT_CLIP[0] <= sample < LEFT_CLIP[1]:
        return 32767
    if channel == 1 and RIGHT_CLIP[0] <= sample < RIGHT_CLIP[1]:
        return -32768
    return 0


def generated_samples():
    """Return the deterministic stereo sample table used by the test clip."""
    values = array('h')
    for sample in range(COVERED_END):
        tone = round(TONE_AMPLITUDE * sin(2 * pi * 440 * sample / RATE))
        silent = SILENCE[0] <= sample < SILENCE[1]
        left = _clip_value(sample, 0) or (0 if silent else tone)
        right = _clip_value(sample, 1) or tone
        values.extend((left, right))
    return values


def _audio_frame(first, count, values):
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    frame.sample_rate = RATE
    frame.time_base = Fraction(1, RATE)
    frame.pts = first
    frame.planes[0].update(values[first * CHANNELS:(first + count) * CHANNELS].tobytes())
    return frame


def planned_frames():
    """Plan contiguous decode packets around the single coverage hole.

    Each packet is FRAME_SAMPLES long and 441 does not divide the 2205-sample
    window on the same phase as the packet grid, so every window is assembled
    from samples that arrived in different decode packets.  The hole is the
    only place where a packet boundary coincides with missing coverage.
    """
    frames = []
    for seg_start, seg_end in ((0, HOLE[0]), (HOLE[1], COVERED_END)):
        cursor = seg_start
        while cursor < seg_end:
            count = min(FRAME_SAMPLES, seg_end - cursor)
            frames.append((cursor, count))
            cursor += count
    for _, count in frames:
        assert count <= FRAME_SAMPLES
    return frames


def reference_missing(blocks):
    """Sample positions absent from the blocks the decode pass delivered."""
    covered = set()
    for block in blocks:
        covered.update(range(block.first_sample, block.end_sample))
    return set(range(COVERED_END)) - covered


def build_clip(path):
    """Mux a short stereo master whose packets split around window edges.

    Packets are contiguous inside two coverage segments, with one 882-sample
    hole between them.  Matroska stores audio on a 1 ms grid, but the hole is
    an exact 20 ms span, so it survives the decode pass at its exact source
    sample position.
    """
    values = generated_samples()
    frames = planned_frames()
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=Fraction(25))
        video.width, video.height, video.pix_fmt = 16, 16, 'gray'
        audio = container.add_stream('pcm_s16le', rate=RATE)
        audio.layout = 'stereo'
        emitted = 0
        for index in range(20):
            frame = av.VideoFrame(16, 16, 'gray')
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            container.mux(video.encode(frame))
            second = Fraction(index, 25)
            while emitted < len(frames) and Fraction(frames[emitted][0], RATE) <= second:
                first, count = frames[emitted]
                container.mux(audio.encode(_audio_frame(first, count, values)))
                emitted += 1
        container.mux(video.encode(None))
        container.mux(audio.encode(None))
    return Path(path), [first for first, _ in frames]


def reference_grid(blocks, phase_us, thresholds):
    """Recompute every window statistic independently from decoded PCM.

    Windows follow the same grid math as the production code, but coverage,
    energy and clipping are summed here directly from the decoded blocks'
    sample positions instead of reusing WindowGrid, so an accumulator
    regression cannot validate itself.  Container timestamp rounding is
    respected because the blocks carry their actual first sample.
    """
    rows = []
    key = (0 - phase_us * RATE) // (STEP_US * RATE)
    while True:
        start = sample_boundary(key * STEP_US + phase_us, RATE)
        end = sample_boundary((key + 1) * STEP_US + phase_us, RATE)
        if start >= TOTAL_SAMPLES:
            break
        if not any(block.first_sample < end and block.end_sample > start for block in blocks):
            key += 1
            continue
        window = Window(start, end, 0, [0, 0], [0, 0])
        for block in blocks:
            overlap_start = max(start, block.first_sample)
            overlap_end = min(end, block.end_sample)
            if overlap_start >= overlap_end:
                continue
            for sample in range(overlap_start, overlap_end):
                window.covered += 1
                source_index = sample - block.first_sample
                for channel in range(CHANNELS):
                    code = block.samples[source_index * CHANNELS + channel]
                    window.energy[channel] += code * code
                    if code == 32767 or code == -32768:
                        window.clipped[channel] += 1
        readings = []
        for channel in range(CHANNELS):
            readings.append({
                'rms': window_reading(window, channel, 'silence', thresholds),
                'clip': window_reading(window, channel, 'clip', thresholds),
            })
        rows.append({'key': key, 'start_sample': start, 'end_sample': end,
                     'covered': window.covered, 'capacity': window.capacity,
                     'energy': window.energy, 'clipped': window.clipped,
                     'readings': readings})
        key += 1
    return rows


def _expected_hits(reference, thresholds, phase_us):
    """Run the same shared candidate logic over the independent reference."""
    findings = []
    for channel in range(CHANNELS):
        for kind, minimum_ms in (('silence', thresholds.silence_min_ms),
                                 ('clip', thresholds.clip_min_ms)):
            bins = []
            for row in reference:
                window = Window(row['start_sample'], row['end_sample'], row['covered'],
                                row['energy'], row['clipped'])
                span_us = row['key'] * STEP_US + phase_us
                bins.append((Span(span_us, span_us + STEP_US),
                             window_reading(window, channel, kind, thresholds)))
            findings.extend(candidates(bins, kind, minimum_ms * 1000, channel + 1))
    return sorted(findings, key=lambda hit: (hit['start_us'], hit['kind'], hit['channel']))


def media_check(directory=None):
    """Decode the constructed clip once and compare both grids end to end."""
    thresholds = Thresholds()
    planned = planned_frames()
    with TemporaryDirectory(prefix='slatewatch-soundcheck-') as scratch:
        directory = Path(directory) if directory else Path(scratch)
        path, frame_firsts = build_clip(directory / 'grid-phases.mkv')
        manifest = inspect(path, None, 'first')
        meter = SoundMeter(manifest['sound'], thresholds, extra_phases=(PHASE_US,))
        blocks = []
        for entry in decode(path, manifest):
            if not isinstance(entry, PCM):
                continue
            blocks.append(entry)
            meter.push(entry)
        decoded_blocks = len(blocks)
        missing = reference_missing(blocks)
        reference = {
            0: reference_grid(blocks, 0, thresholds),
            PHASE_US: reference_grid(blocks, PHASE_US, thresholds),
        }
        coverage, scan_hits, stats = meter.finish()
        phase_grid = meter.grids[1]
        observed = {
            'scan': [meter.readings(meter.scan_grid), meter.findings(meter.scan_grid)],
            'shifted': [meter.readings(phase_grid), meter.findings(phase_grid)],
        }
        checks = []

        def check(name, condition, detail):
            checks.append({'name': name, 'passed': bool(condition), 'detail': detail})
            if not condition:
                raise AssertionError(f'{name}: {detail}')

        check('one decode pass feeds both grids',
              decoded_blocks == len(meter.blocks) and decoded_blocks >= 2,
              {'decoded_pcm_blocks': decoded_blocks, 'muxed_audio_frames': len(frame_firsts)})
        check('packets straddle window boundaries',
              any(block.first_sample % WINDOW_SAMPLES for block in blocks),
              {'block_first_samples': [block.first_sample for block in blocks[:8]],
               'window_samples': WINDOW_SAMPLES, 'planned_frames': planned[:8]})
        hole_width = HOLE[1] - HOLE[0]
        check('coverage hole only drains part of one window', hole_width < WINDOW_SAMPLES,
              {'hole': list(HOLE), 'window_samples': WINDOW_SAMPLES})
        check('the main hole survives the container as a decoded discontinuity',
              any(blocks[i + 1].first_sample - blocks[i].end_sample >= hole_width - 5
                  for i in range(len(blocks) - 1)),
              {'block_gaps': [(blocks[i].end_sample, blocks[i + 1].first_sample)
                              for i in range(len(blocks) - 1)
                              if blocks[i + 1].first_sample != blocks[i].end_sample][:6],
               'planned_hole': list(HOLE)})
        uncovered_count = len(missing)
        check('coverage records the two segments separated by the hole',
              len(coverage) == 2 and coverage[0].start == 0
              and coverage[1].end == meter.blocks[-1].end
              and coverage[1].start > coverage[0].end,
              {'coverage': [span.as_dict() for span in coverage],
               'last_block': meter.blocks[-1].as_dict()})
        check('sample total excludes every omitted sample',
              stats['samples'] == COVERED_END - uncovered_count,
              {**stats, 'expected_samples': COVERED_END - uncovered_count,
               'omitted': uncovered_count, 'main_hole': hole_width})

        for phase, grid in ((0, meter.scan_grid), (PHASE_US, phase_grid)):
            rows = meter.readings(grid)
            expected_rows = reference[phase]
            check(f'phase {phase}us: window count matches reference',
                  len(rows) == len(expected_rows),
                  {'observed': len(rows), 'reference': len(expected_rows)})
            for row, expected in zip(rows, expected_rows):
                assert row['span']['start_us'] == expected['key'] * STEP_US + phase
                for channel, measured in enumerate(row['channels']):
                    assert measured['covered'] == expected['covered']
                    assert measured['capacity'] == expected['capacity']
                    assert measured['complete'] == (expected['covered'] == expected['capacity'])
                    assert measured['rms'] == (isqrt(expected['energy'][channel] // expected['capacity'])
                                               if expected['covered'] == expected['capacity'] else None)
                    assert measured['clip_permille'] == (
                        expected['clipped'][channel] * 1000 // expected['capacity']
                        if expected['covered'] == expected['capacity'] else None)
            expected_hits = _expected_hits(expected_rows, thresholds, phase)
            actual_hits = meter.findings(grid)
            check(f'phase {phase}us: candidates match reference rule run',
                  actual_hits == expected_hits,
                  {'observed': actual_hits, 'reference': expected_hits})
            check(f'phase {phase}us: incomplete windows never decide',
                  all(measured['silence_value'] is None and measured['clip_value'] is None
                      for row in rows for measured in row['channels']
                      if not measured['complete']),
              {'incomplete_windows': [row['span'] for row in rows
                                      if any(not c['complete'] for c in row['channels'])]})

        shifted = observed['shifted'][1]
        scan = observed['scan'][1]
        scan_silence = [hit for hit in scan if hit['kind'] == 'silence']
        shifted_silence = [hit for hit in shifted if hit['kind'] == 'silence']
        incomplete_phase_zero = [
            row['span'] for row in observed['scan'][0]
            if any(not channel['complete'] for channel in row['channels'])]
        incomplete_shifted = [
            row['span'] for row in observed['shifted'][0]
            if any(not channel['complete'] for channel in row['channels'])]
        check('the main hole drains only part of one phase-zero window',
              len(incomplete_phase_zero) == 2
              and {'start_us': 5 * STEP_US, 'end_us': 6 * STEP_US} in incomplete_phase_zero
              and {'start_us': 14 * STEP_US, 'end_us': 15 * STEP_US} in incomplete_phase_zero,
              {'incomplete_phase_zero': incomplete_phase_zero})
        check('the shifted grid distributes the same hole across two windows',
              len(incomplete_shifted) > len(incomplete_phase_zero),
              {'incomplete_phase_zero': incomplete_phase_zero,
               'incomplete_shifted': incomplete_shifted})
        check('neither grid turns the interrupted silence into a full-window hit',
              not scan_silence and not shifted_silence,
              {'scan_silence': scan_silence, 'shifted_silence': shifted_silence})
        scan_clip = sorted((hit['channel'], hit['bins']) for hit in scan if hit['kind'] == 'clip')
        shifted_clip = sorted((hit['channel'], hit['bins']) for hit in shifted if hit['kind'] == 'clip')
        check('channels keep distinct clipping distributions and never cross',
          [channel for channel, _ in scan_clip] == [1, 2]
          and [channel for channel, _ in shifted_clip] == [1, 2]
          and all(bins >= 2 for _, bins in scan_clip)
          and all(bins >= 2 for _, bins in shifted_clip),
          {'scan': scan_clip, 'shifted': shifted_clip})
        check('the shifted grid changes the measured clipping support',
          scan_clip != shifted_clip,
          {'scan': scan_clip, 'shifted': shifted_clip})
        check('the two grids report different measured statistics',
              observed['scan'][0] != observed['shifted'][0],
              {'phase_zero_window_count': len(observed['scan'][0]),
               'shifted_window_count': len(observed['shifted'][0])})
        return {'clip': str(path), 'rate': RATE, 'channels': CHANNELS,
                'window_ms': WINDOW_MS, 'phases_us': [0, PHASE_US],
                'checks': checks,
                'scan_hits': scan, 'shifted_hits': shifted,
                'scan_windows': observed['scan'][0], 'shifted_windows': observed['shifted'][0]}


def _full_window(start_sample, energy, clipped, covered=WINDOW_SAMPLES):
    return Window(start_sample, start_sample + WINDOW_SAMPLES, covered,
                  [energy[0], energy[1]], [clipped[0], clipped[1]])


def rule_check():
    """Exercise rule synthesis with known windows and no media decode."""
    thresholds = Thresholds(silence_rms=80, silence_min_ms=150, clip_permille=100,
                            clip_min_ms=150, audio_window_ms=WINDOW_MS)
    step = STEP_US
    checks = []

    def check(name, condition, detail):
        checks.append({'name': name, 'passed': bool(condition), 'detail': detail})
        if not condition:
            raise AssertionError(f'{name}: {detail}')

    # 1. A window missing part of its samples can never decide either metric.
    partial = _full_window(0, (0, 0), (2000, 0), covered=WINDOW_SAMPLES - 1)
    check('partial window is not silence even with zero energy',
          window_reading(partial, 0, 'silence', thresholds) is None,
          {'covered': partial.covered, 'capacity': partial.capacity})
    check('partial window is not clipping even above threshold',
          window_reading(partial, 0, 'clip', thresholds) is None,
          {'clipped': partial.clipped[0]})

    # 2. A missing grid slot breaks the run; each single-window fragment is
    #    below the 150 ms minimum, so neither side becomes a candidate.
    windows = {
        0: _full_window(0, (0, 0), (0, 0)),
        # slot 1 is absent on purpose
        2: _full_window(WINDOW_SAMPLES * 2, (0, 0), (0, 0)),
    }
    rows = [(Span(key * step, (key + 1) * step), window) for key, window in windows.items()]
    hits = [hit for hit in channel_hits(rows, CHANNELS, thresholds) if hit['kind'] == 'silence']
    check('missing window breaks the candidate into sub-minimum fragments',
          hits == [], {'hits': hits})

    # 3. Channels do not cross: only channel one is silent here.
    loud = (TONE_AMPLITUDE ** 2) * WINDOW_SAMPLES
    separate = {
        key: _full_window(key * WINDOW_SAMPLES, (0, loud), (0, 0))
        for key in range(3)
    }
    rows = [(Span(key * step, (key + 1) * step), window) for key, window in separate.items()]
    hits = channel_hits(rows, CHANNELS, thresholds)
    check('silence is attributed to one channel only',
          [(hit['kind'], hit['channel'], hit['bins']) for hit in hits] == [('silence', 1, 3)],
          {'hits': hits})

    # 4. Minimum event length boundary: three windows pass, two windows fail.
    long_rows = [(Span(key * step, (key + 1) * step),
                  _full_window(key * WINDOW_SAMPLES, (0, 0), (0, 0))) for key in range(3)]
    short_rows = long_rows[:2]
    long_hits = channel_hits(long_rows, 1, thresholds)
    short_hits = channel_hits(short_rows, 1, thresholds)
    check('three full windows meet the 150 ms minimum event length',
          [hit['kind'] for hit in long_hits] == ['silence'], {'hits': long_hits})
    check('two full windows stay below the minimum event length',
          short_hits == [], {'hits': short_hits})

    # 5. Clip threshold boundary uses the same full-window gate.
    below = WINDOW_SAMPLES * 99 // 1000
    above = (WINDOW_SAMPLES * 100 + 999) // 1000
    below_window = _full_window(0, (0, 0), (below, 0))
    above_window = _full_window(0, (0, 0), (above, 0))
    check('clipping below permille threshold is not a finding',
          window_reading(below_window, 0, 'clip', thresholds) is None,
          {'permille': below * 1000 // WINDOW_SAMPLES})
    check('clipping at permille threshold is reported',
          window_reading(above_window, 0, 'clip', thresholds) == 100,
          {'permille': above * 1000 // WINDOW_SAMPLES})
    return {'checks': checks, 'thresholds': asdict(thresholds)}


WORKFLOWS = {'media': media_check, 'rules': rule_check}


def main():
    parser = argparse.ArgumentParser(description='Verify sound window and rule internal boundaries')
    parser.add_argument('workflow', choices=(*WORKFLOWS, 'all'))
    parser.add_argument('--directory', help='keep the constructed clip in this directory')
    options = parser.parse_args()
    names = tuple(WORKFLOWS) if options.workflow == 'all' else (options.workflow,)
    for name in names:
        if name == 'media':
            result = media_check(options.directory)
        else:
            result = rule_check()
        print(json.dumps({'workflow': name, 'observed': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
