"""Deterministic simulation of the two candidate page master clocks.

A real browser cannot be unit-tested from this repository, so the observable
scheduling semantics of the two master-clock choices are modelled here on the
same data the page receives: the clock.json picture mapping, coverage spans and
the WAV header/mapping. All randomness comes from a seeded LCG so the numbers
are rerunnable on any machine.

Three time layers are tracked separately, matching the design doc vocabulary:

* source time  - integer programme microseconds and source sample numbers the
  backend measured from decoded PTS;
* player time  - where the media pipeline has actually advanced, including WAV
  output drift, decoder stalls and currentTime quantisation;
* shown time   - the frame the rAF loop paints and the audio sample position at
  the same wall-clock tick.
"""
from bisect import bisect_right
from dataclasses import dataclass

SECOND = 1_000_000
TICK_US = 1_000  # one simulation tick == one millisecond of wall time


@dataclass(frozen=True)
class Scenario:
    """Player behaviour over one playback; times in microseconds."""

    name: str
    drift_ppm: float            # WAV output rate minus programme rate
    stall_at_us: int            # wall time of decoder stall, -1 = none
    stall_for_us: int           # stall length (only the WAV pipeline freezes)
    current_quant_us: int       # audio.currentTime reporting granularity
    raf_jitter_us: int          # rAF callback scheduling jitter amplitude
    initial_lock_error_us: int  # one-shot currentTime->programme offset error


# Failure-mode catalogue shared with probe.py. Excerpts in this repo are
# 0.6–0.75 s long, so stalls land inside the excerpt; sizes are deliberately
# above the doc's acceptance thresholds so the two policies differ visibly.
ENGINE_SCENARIOS = (
    Scenario('nominal', 0, -1, 0, 4_000, 4_000, 0),
    Scenario('wav_drift_4000ppm', 4000, -1, 0, 4_000, 4_000, 0),
    Scenario('decode_stall_120ms', 0, 350_000, 120_000, 4_000, 4_000, 0),
    Scenario('lock_error_80ms', 0, -1, 0, 4_000, 4_000, 80_000),
)


def lcg(seed):
    state = seed & 0xFFFFFFFF

    def draw():
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return state / 0x100000000

    return draw


@dataclass
class Timeline:
    """Source facts for one excerpt, taken verbatim from clock.json."""

    span_start: int
    span_end: int
    frames: list               # sorted source_us of present frames
    frame_end_us: list         # each frame's half-open display end
    coverage: list             # (start_us, end_us) picture coverage spans
    rate: int                  # WAV sample rate, 0 when no sound track
    first_sample: int          # WAV sample index at span_start, 0 no track
    filled_ranges: list        # (first_sample, end_sample) actually present

    def covered(self, programme_us):
        idx = bisect_right(self.coverage, (programme_us, 10**18)) - 1
        return idx >= 0 and self.coverage[idx][0] <= programme_us < self.coverage[idx][1]

    def frame_at(self, programme_us):
        """Frame visible at a programme position, or None across a picture hole."""
        if not self.covered(programme_us):
            return None
        idx = bisect_right(self.frames, programme_us) - 1
        if idx < 0 or programme_us >= self.frame_end_us[idx]:
            return None
        return self.frames[idx]

    def audio_state_at(self, programme_us):
        """(source sample, covered-by-real-source) the WAV should emit here."""
        if self.rate == 0:
            return None, None
        sample = self.first_sample + round((programme_us - self.span_start) * self.rate / SECOND)
        covered = any(first <= sample < end for first, end in self.filled_ranges)
        return sample, covered


def simulate(timeline, scenario, policy, seed):
    """policy: 'programme_us' (wall ruler) or 'wav' (audio element ruler)."""
    assert policy in ('programme_us', 'wav')
    draw = lcg(seed)
    duration = timeline.span_end - timeline.span_start
    ticks = duration // TICK_US
    wav_offset = -scenario.initial_lock_error_us  # WAV ruler's idea of programme pos
    frame_errors, policy_errors, hole_violations, stall_diverged = [], 0, 0, 0
    lock_residual, drift_error, pad_mislabel = 0, 0, 0
    max_stall_lag = audio_stall_ticks = 0
    for tick in range(ticks):
        wall = tick * TICK_US
        stalled = (scenario.stall_at_us >= 0
                   and scenario.stall_at_us <= wall < scenario.stall_at_us + scenario.stall_for_us)
        # Layer 1: ground-truth programme position (what the source timeline says).
        programme = wall
        # Layer 2: the WAV output clock advances only while the pipeline runs;
        # it carries output-rate drift and the one-shot lock error.
        if not stalled:
            wav_offset += round(TICK_US * (1 + scenario.drift_ppm / 1_000_000))
        wav_offset = min(wav_offset, duration)
        if policy == 'programme_us':
            clock_position = programme
        else:
            raw = max(0, wav_offset)
            clock_position = raw - raw % max(scenario.current_quant_us, 1)
        # Layer 3: what the viewer actually sees after rAF scheduling jitter.
        jitter = round((draw() - 0.5) * 2 * scenario.raf_jitter_us)
        shown_programme = min(max(clock_position + jitter, 0), duration)
        expected = timeline.frame_at(programme)
        scheduled = timeline.frame_at(clock_position)   # before jitter: policy truth
        shown = timeline.frame_at(shown_programme)
        # Hole painting counts only when the *policy* (not rAF jitter) lies.
        if expected is None and scheduled is not None:
            hole_violations += 1
        if expected is not None and shown != expected:
            frame_errors.append(abs((shown or expected) - expected))
        if expected is not None and scheduled != expected:
            policy_errors += 1
        if stalled:
            lag = programme - clock_position
            max_stall_lag = max(max_stall_lag, lag)
            if policy == 'wav' and scheduled != expected:
                stall_diverged += 1
        if stalled and timeline.rate > 0:
            _sample, covered_now = timeline.audio_state_at(programme)
            if policy == 'wav' and covered_now:
                audio_stall_ticks += 1
        if scenario.initial_lock_error_us and tick < 200:
            lock_residual = max(lock_residual, abs(programme - clock_position))
        if scenario.drift_ppm:
            drift_error = max(drift_error, abs(programme - clock_position))
        # Zero-filled WAV samples: only clock.json can tell pad from real silence.
        _sample, covered = timeline.audio_state_at(programme)
        if covered is False and abs(programme - clock_position) > scenario.current_quant_us:
            pad_mislabel += 1
    return {
        'scenario': scenario.name, 'policy': policy, 'seed': seed,
        'frame_error_max_us': max(frame_errors, default=0),
        'frame_error_rms_us': round((sum(e * e for e in frame_errors)
                                     / max(len(frame_errors), 1)) ** 0.5) if frame_errors else 0,
        'policy_frame_mismatch_ticks': policy_errors,
        'hole_violations': hole_violations,
        'stall_diverged_ticks': stall_diverged,
        'stall_max_lag_us': max_stall_lag,
        'audio_stall_ticks': audio_stall_ticks,
        'lock_residual_max_us': lock_residual,
        'drift_error_max_us': drift_error,
        'pad_mislabel_ticks': pad_mislabel,
        'has_sound': timeline.rate > 0,
    }


def sweep(timeline, scenarios, seeds=range(1, 25)):
    return [simulate(timeline, scenario, policy, seed)
            for scenario in scenarios for policy in ('programme_us', 'wav')
            for seed in seeds]


def summary(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row['scenario'], row['policy']), []).append(row)
    out = []
    for (_scenario, _policy), items in sorted(grouped.items()):
        out.append({
            'scenario': _scenario, 'policy': _policy, 'runs': len(items),
            'has_sound': items[0]['has_sound'],
            'frame_error_max_us': max(i['frame_error_max_us'] for i in items),
            'frame_error_rms_worst_us': max(i['frame_error_rms_us'] for i in items),
            'policy_frame_mismatch_ticks': max(i['policy_frame_mismatch_ticks'] for i in items),
            'hole_violations': max(i['hole_violations'] for i in items),
            'stall_diverged_ticks': max(i['stall_diverged_ticks'] for i in items),
            'stall_max_lag_us': max(i['stall_max_lag_us'] for i in items),
            'audio_stall_ticks': max(i['audio_stall_ticks'] for i in items),
            'lock_residual_max_us': max(i['lock_residual_max_us'] for i in items),
            'drift_error_max_us': max(i['drift_error_max_us'] for i in items),
            'pad_mislabel_ticks': max(i['pad_mislabel_ticks'] for i in items),
        })
    return out
