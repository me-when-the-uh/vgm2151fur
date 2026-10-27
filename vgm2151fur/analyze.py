"""Turn YM2151, sample chips and SCC/AY-8910 writes into notes and a row grid."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace

from vgm2151fur.ay8910 import (
    AY_DEFAULT_CLOCK,
    AY_VOICES,
    FX_ENV_HI,
    FX_ENV_LO,
    FX_ENV_SET,
    FX_MODE,
    FX_NOISE,
    ay_note,
    collect_ay8910,
)
from vgm2151fur.c140 import (
    C140Sample, c140_rate, collect_c140, hit_played_freq, merge_step_writes,
)
from vgm2151fur.c352 import (
    C352_DEFAULT_CLOCK,
    C352_REFIT_SAMPLES,
    C352Sample,
    c352_rate,
    collect_c352,
    hit_played_freq as c352_played_freq,
    merge_step_writes as c352_merge_steps,
)
from vgm2151fur.fm import CARRIERS, FMPatch, OpRegs, OPN_CARRIERS
from vgm2151fur.oki import (
    FURNACE_C4,
    AdpcmSample,
    OkiSample,
    collect_msm6258,
    collect_oki,
    msm6258_div_fx,
)
from vgm2151fur.pcm import PcmSample, build_pcm_bank, pitch_to_note, rate_to_note
from vgm2151fur.pitch import (
    E5_CENTER, NOTE_MAX, furnace_period_note, module_clock, pitch_table,
)
from vgm2151fur.scc import (
    SCC_DEFAULT_CLOCK,
    SCC_MIN_PERIOD,
    SCC_REFIT_SAMPLES,
    SCC_VOICES,
    collect_scc,
    scc_note,
)
from vgm2151fur.sn76489 import (
    PSG_DEFAULT_CLOCK,
    PSG_DIVIDER,
    PSG_FURNACE_CLOCK,
    PSG_MAX_PERIOD,
    collect_sn76489,
    psg_note,
)
from vgm2151fur.segapcm import SegapcmSample, collect_segapcm, segapcm_rate
from vgm2151fur.timing import (
    MIN_ROW_SAMPLES,
    TARGET_TICK_SAMPLES,
    Grid,
    condense_row,
    estimate_grid,
    place_tick,
    refine_pcm_grid,
)
from vgm2151fur.vgm import VGM_RATE, VgmFile
from vgm2151fur.ym2203 import (
    YM2203_DEFAULT_CLOCK,
    YM2203_SSG_DIVIDER,
    YM2203State,
    opn_carrier_octave,
    opn_note_for,
    opn_pitch_units,
)
from vgm2151fur.ym2612 import (
    EXT_FREQ_OP,
    YM2612_DEFAULT_CLOCK,
    YM2612State,
    ym2612_note_for,
)
from vgm2151fur.ym2612dac import (
    DacPlay,
    DacSample,
    collect_dac,
    group_dac_plays,
)


# Official YM2151 KC note nibble → semitone from C of this octave.
# Valid codes: C#=0, D=1, D#=2, E=4, F=5, F#=6, G=8, G#=9, A=10, A#=12, B=13,
# C(next octave)=14.  3/7/11/15 are unused and alias 4/8/12/14.
_KC_SEMI = {
    0: 1,   # C#
    1: 2,   # D
    2: 3,   # D#
    3: 4,   # unused → E
    4: 4,   # E
    5: 5,   # F
    6: 6,   # F#
    7: 7,   # unused → G
    8: 7,   # G
    9: 8,   # G#
    10: 9,  # A
    11: 10, # unused → A#
    12: 10, # A#
    13: 11, # B
    14: 12, # C of next octave
    15: 12, # unused → C of next octave
}


def kc_to_furnace_note(kc: int) -> int:
    """YM2151 KC → Furnace note (0 = C-(-5), 108 = C-4, 179 = B-9)."""
    octave = (kc >> 4) & 7
    semi = _KC_SEMI[kc & 0x0F]
    return max(0, min(179, (octave + 5) * 12 + semi))


def furnace_note_name(n: int) -> str:
    if n == 180:
        return "OFF"
    names = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")
    octv = n // 12 - 5
    return f"{names[n % 12]}{octv}"


def kf_to_e5(kf: int) -> int:
    """YM2151 KF register → Furnace E5xx fine tune.

    The fraction is the top six bits (`ch_block_freq = (KC << 6) | (KF >> 2)`),
    one E5 unit per KF step; values above 0xBF saturate.
    """
    return max(0, min(255, 0x80 + (kf >> 2)))


def kc_kf_units(kc: int, kf: int) -> int:
    """Absolute pitch code in 1/64 semitone units (fraction = KF >> 2)."""
    return (
        (((kc >> 4) & 7) * 12 + _KC_SEMI[kc & 0x0F]) * 64 + (kf >> 2)
    )


# Furnace's default YM2151 clock. Furnace keeps notes at their musical pitch
# at any clock, so this is the reference the transposition is measured from.
OPM_DEFAULT_CLOCK = 3579545

# One sample-chip trigger is often a key-on plus its own pitch and volume
# writes.  A real drum interval is hundreds of samples, never tens.
_ONSET_MERGE_SAMPLES = 100


def opm_pitch_offset(clock: int | None) -> int:
    """Clock → note transposition in 1/64 semitone units.

    Playback is a ratio: registers written for a 4 MHz OPM sound
    12·log2(clock/default) = +1.92 st above their meaning at 3.579545 MHz.
    """
    if not clock or clock == OPM_DEFAULT_CLOCK:
        return 0
    return round(768.0 * math.log2(clock / OPM_DEFAULT_CLOCK))


def _shift_note_e5(note: int, e5: int, offset: int) -> tuple[int, int]:
    """Shift a note/E5 pair by `offset` 1/64-semitone units."""
    total = note * 64 + (e5 - 0x80) + offset
    shifted, frac = divmod(total, 64)
    return max(0, min(179, shifted)), 0x80 + frac


def transpose_opm_events(events: list[NoteEvent], clock: int | None) -> None:
    """Move FM note/E5 pairs for a rip whose OPM clock is not the default.

    Sweeps are deltas from the note. Shifting the note moves the whole
    trajectory.  PCM (ch 8-9) is pitched by its own chip's clock and stays put.
    """
    offset = opm_pitch_offset(clock)
    if not offset:
        return
    for ev in events:
        # DAC notes select a sample-map slot, not a musical pitch; a grid
        # transpose would point them at the wrong (or an empty) slot.
        if ev.on and not ev.dac and ev.ch < 8 and 0 <= ev.note < 180:
            ev.note, ev.e5 = _shift_note_e5(ev.note, ev.e5, offset)


def pcm_pan_fx(vl: int, vr: int) -> int:
    """Furnace 08xy from K007232 L/R volumes (0–15).

    08xy is two independent levels (playback.cpp, effect 0x08): x is left,
    y is right. The loud side stays at 15 so the volume column is not scaled
    down a second time. 0xFF is both, 0xF0 is left, 0x0F is right.
    """
    vl = max(0, min(15, int(vl)))
    vr = max(0, min(15, int(vr)))
    peak = max(vl, vr)
    if peak <= 0:
        return 0x00
    left = (vl * 15 + peak // 2) // peak
    right = (vr * 15 + peak // 2) // peak
    return ((left & 15) << 4) | (right & 15)


def vol15(raw: int, scale: float = 255.0) -> int:
    """K007232 volume register → Furnace 0-15.

    The chip mixes a voice as sample * (vol * 2) / 32768, so the register is
    linear amplitude and 255 is full scale.  A few drivers treat the register
    as a 0-15 preset instead: pass 15 for those files.
    """
    if raw <= 0:
        return 0
    return max(1, min(15, int(round(raw * 15.0 / scale))))


@dataclass
class NoteEvent:
    sample: int
    ch: int  # 0-7 FM, 8-9 PCM
    on: bool
    note: int  # furnace 0-179, or 180 off
    ins: int
    vol: int  # 0-127 FM, 0-15 PCM; -1 = omit
    e5: int  # 0x80 = none
    pan: int  # 0xLR, 0 means omit
    pcm: bool = False
    # YM2612 DAC sample on FM6.  The note value indexes the instrument's
    # sample map, not a musical pitch.
    dac: bool = False
    # Key-off sample (0 = still playing at the end / unknown).
    end: int = 0
    # Mid-note pitch writes: (sample, 1/64-semitone delta from the note pitch).
    # Encoders turn them into 01xx/02xx ramps.
    sweep: tuple[tuple[int, int], ...] = ()
    # Mid-note volume steps: (sample, chip level, 08xy balance or None). On a
    # slide note the third value is the FA01/FA00 flag instead.
    vol_pts: tuple[tuple[int, int, int | None], ...] = ()
    # Mid-note carrier TL steps: (sample, absolute YM2151 carrier TL). The
    # writer maps them onto the volume column relative to the instrument.
    tl_pts: tuple[tuple[int, int], ...] = ()
    # PCM pan 0x00 is a real "both off"; OKI/MSM6258 are mono and must not
    # write 08xx.
    no_pan: bool = False
    # C352 quad output: the four volume registers (front L/R, rear L/R),
    # 0-255. When set, they carry the level and the volume column is pinned.
    quad: tuple[int, int, int, int] | None = None
    # Mid-note changes to those four, as (sample, fl, fr, rl, rr).
    quad_pts: tuple[tuple[int, int, int, int, int], ...] = ()
    # SCC/AY only: raw (sample, register) pitch writes. A phase-2 pass turns
    # them into instrument macros once the tick rate is known.
    tone_pitch_pts: tuple[tuple[int, int], ...] = ()
    # SCC only: (sample, 32 raw wave-RAM bytes) whenever the image changed.
    tone_wave_pts: tuple[tuple[int, bytes], ...] = ()
    # SCC only: the wavetable index the note itself starts on (-1 = no wave).
    tone_wave: int = -1
    tone_period_offset: int = 0
    # 0 = not a tone-chip note, 1 = SCC, 2 = AY. The phase-2 pass finds the
    # instrument for these once the tick rate is known.
    tone_chip: int = 0
    # Tone chip clock and divider: SCC 16, AY-8910 8, YM2203 SSG 16.
    tone_clock: int = 0
    tone_div: int = 8
    # AY only: (sample, effect, value), including the state at the note start.
    tone_fx_pts: tuple[tuple[int, int, int], ...] = ()
    # 0 = volume macro or column, 1 = FA01 from the note, 2 = FA00 on the note.
    vol_slide: int = 0
    # Base pitch units moved by one 01xx/02xx step and by one E5 step. OPM 1,
    # PCM chips 0.5, YM2203 OPN register 2.
    sweep_div: float = 1.0
    # E5 range the writer may use. OPM fine tune spans half a semitone and the
    # OPN f-num is 11 bits.
    e5_lo: int = 0x40
    e5_hi: int = 0xBF
    # YM2203/YM2612 FM only: (sample, block, fnum) writes while the note is
    # held. A phase-2 pass turns them into a pitch macro on an instrument copy.
    fm_pitch_pts: tuple[tuple[int, int, int], ...] = ()
    # A short step trajectory as row effects: (sample, delta in 1/64
    # semitones). No instrument copy is needed. Thousands of octave switches
    # stay under the 250-instrument limit.
    pitch_steps: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ToneIns:
    """One SCC/AY instrument: a starting wave plus per-tick macro values.

    ``pitch`` is the engine's ``pitch`` unit (1/128 semitone) and lands on the
    platform's ``std.pitch``, which for a period table adds straight to the
    register, so a driver's own pitch trajectory plays back exactly. ``waves``
    is the wave macro's wavetable list, which is how a driver's mid-note
    waveform morph survives. Each macro has its own step rate, because the
    format stores one speed per macro.
    """

    wave: int = 0
    pitch: tuple[int, ...] = ()
    pitch_speed: int = 1
    pitch_loop: int = 255
    waves: tuple[int, ...] = ()
    wave_speed: int = 1
    wave_loop: int = 255
    # Per-tick 0-15 register levels. The platform computes outVol = vol *
    # macro / 15. A column of 15 makes the macro the register value.
    vol: tuple[int, ...] = ()
    vol_speed: int = 1
    vol_loop: int = 255
    # AY register motion a pattern row would quantize away, read as the duty,
    # wave, ex2 and ex5 macros. Empty when the register stays put.
    noise: tuple[int, ...] = ()
    noise_speed: int = 1
    noise_loop: int = 255
    mode: tuple[int, ...] = ()
    mode_speed: int = 1
    mode_loop: int = 255
    env_shape: tuple[int, ...] = ()
    env_shape_speed: int = 1
    env_shape_loop: int = 255
    env_period: tuple[int, ...] = ()
    env_period_speed: int = 1
    env_period_loop: int = 255


@dataclass
class Song:
    vgm: VgmFile
    t0: int
    loop_sample: int | None
    speed: int
    hz: float
    fm_patches: list[FMPatch]
    pcm_samples: list[PcmSample]
    events: list[NoteEvent]
    warnings: list[str] = field(default_factory=list)
    oki_samples: list[OkiSample] = field(default_factory=list)
    msm6258_samples: list[AdpcmSample] = field(default_factory=list)
    segapcm_samples: list[SegapcmSample] = field(default_factory=list)
    c140_samples: list[C140Sample] = field(default_factory=list)
    c352_samples: list[C352Sample] = field(default_factory=list)
    # (sample, channel, effect, value) placed before notes. MSM6258 divider.
    channel_fx: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Chip-wide YM2151 LFO effects: (sample, effect, value[, tick offset]).
    # 17xx speed, 18xx wave, 1Exx AMD, 1Fxx PMD.
    chip_fx: list[tuple[int, int, int] | tuple[int, int, int, int]] = field(
        default_factory=list
    )
    # How hz was chosen ("" = default 60 Hz). Shown in --report.
    grid_note: str = ""
    # Rows per measured 16th (1 = one row per 16th). On SCC/AY this is the
    # 16th's tick count divided by the written row's tick count, and it can
    # be fractional. Highlight uses round(4 * this) and round(16 * this).
    row_subdiv: float = 1
    # Furnace pattern highlight. 4 and 16 mark the beat and the bar when one
    # row is a 16th. An SCC/AY row with several rows per 16th scales both,
    # and the status-bar BPM stays the quarter-note tempo.
    hilight_a: int = 4
    hilight_b: int = 16
    # The FM chip the track was written for: "ym2151" (8 channels),
    # "ym2203" (3 FM + 3 SSG channels per instance) or "ym2612" (6 FM
    # channels per instance).
    fm_chip: str = "ym2151"
    # YM2203 instances that appear in the module, in channel order. Each one
    # owns six channels: three FM then three SSG.
    ym2203_ids: list[int] = field(default_factory=list)
    # The source used channel 3's per-operator frequencies (3-slot mode).
    # The module then uses Furnace's YM2203 Ext system, whose 9 channels are
    # FM1, FM2, the four channel-3 operators, then the three SSG voices.
    ym2203_ext: bool = False
    # SSG notes collected from the YM2203s (the notes themselves ride on the
    # events; this count feeds the report and the instrument binding).
    ym2203_ssg_count: int = 0
    # YM2612 instances in channel order.  Each one owns six channels
    # (nine in the extended-channel-3 layout).
    ym2612_ids: list[int] = field(default_factory=list)
    # The source used channel 3's per-operator frequencies (special mode).
    # The module then uses Furnace's YM2612 Ext system, whose 9 channels are
    # FM1, FM2, the four channel-3 operators, then FM4-6.
    ym2612_ext: bool = False
    # The source entered CSM mode.  The per-operator frequencies still map to
    # the Ext layout; the timer-generated key-ons are not synthesized.
    ym2612_csm: bool = False
    # K007232 output level relative to the YM2151, from the rip's own mix
    # (extra header) when present. Written to the .fur chip output.
    k007232_volume: float = 0.20
    # Set by loop finding. The row `Bxx` jumps from. None keeps the jump on
    # the last content row, which is the VGM loop length.
    loop_end_sample: int | None = None
    # The sample where the loop wraps, set with loop_end_sample. Notes at or
    # past it are the recording's copy of the head and stay unwritten, at
    # whichever row rate a rendition reads them.
    loop_cut_sample: int | None = None
    # The source drove the C352 rear pair and the events carry it, so the
    # module's C352 systems are written with `quadOutput` set.
    c352_quad: bool = False
    # K051649/K052539: `scc_ids` per chip, `scc_waves` for its instruments,
    # `scc_ins` per distinct (wave, pitch macro, wave macro), `scc_count` notes.
    scc_ids: list[int] = field(default_factory=list)
    scc_waves: list[bytes] = field(default_factory=list)
    scc_ins: list[ToneIns] = field(default_factory=list)
    scc_count: int = 0
    # AY-8910 channels: one instance per chip id, `ay_count` notes, and one
    # instrument per distinct pitch macro. The hardware envelope, noise
    # frequency and channel mode ride on channel_fx.
    ay_ids: list[int] = field(default_factory=list)
    ay_ins: list[ToneIns] = field(default_factory=list)
    ay_count: int = 0
    # Genesis SN76489: four channels (three tone + noise) after the FM block.
    psg_ids: list[int] = field(default_factory=list)
    psg_ins: list[ToneIns] = field(default_factory=list)
    psg_count: int = 0
    dac_samples: list[DacSample] = field(default_factory=list)
    dac_count: int = 0
    dac_stats: dict = field(default_factory=dict)

    @property
    def samples_per_row(self) -> float:
        return (VGM_RATE / self.hz) * self.speed

    @property
    def bpm(self) -> float:
        """Quarter-note tempo implied by the measured 16th note."""
        return (self.hz / self.speed / max(1, self.row_subdiv)) * 15.0

    def row_of(self, sample: int) -> int:
        """Nearest row (round half up)."""
        return max(0, int(math.floor((sample - self.t0) / self.samples_per_row + 0.5)))

    def place(self, sample: int, stats: dict | None = None) -> tuple[int, int]:
        """Return (row, delay_ticks): the closest playable position.

        EDxx delays a channel's row by whole ticks. At speed 1 the nearest row
        is exact enough; at higher speeds floor + nearest tick keeps the error
        under half a tick. `stats["ed_rolled"]` counts offsets too large for
        the row's tick count.
        """
        return place_tick(
            sample, self.t0, self.samples_per_row, self.speed,
            VGM_RATE / self.hz, stats=stats,
        )

    def delay_ticks(self, sample: int, row: int) -> int:
        """Ticks after the row start; 0 if on-grid (compat wrapper)."""
        _r, d = self.place(sample)
        return d


class YM2151State:
    def __init__(self) -> None:
        self.rl_fb_con = [0xC0] * 8
        self.kc = [0] * 8
        self.kf = [0] * 8
        self.pms_ams = [0] * 8
        self.ops = [[OpRegs() for _ in range(4)] for _ in range(8)]
        self.kon = [False] * 8

    def write(self, reg: int, val: int) -> None:
        if 0x20 <= reg <= 0x27:
            self.rl_fb_con[reg - 0x20] = val
        elif 0x28 <= reg <= 0x2F:
            self.kc[reg - 0x28] = val
        elif 0x30 <= reg <= 0x37:
            self.kf[reg - 0x30] = val
        elif 0x38 <= reg <= 0x3F:
            self.pms_ams[reg - 0x38] = val
        elif reg >= 0x40:
            ch = reg & 7
            op = (reg >> 3) & 3
            slot = self.ops[ch][op]
            page = reg & 0xE0
            if page == 0x40:
                slot.dt_mul = val
            elif page == 0x60:
                slot.tl = val
            elif page == 0x80:
                slot.ks_ar = val
            elif page == 0xA0:
                slot.am_d1r = val
            elif page == 0xC0:
                slot.dt2_d2r = val
            elif page == 0xE0:
                slot.d1l_rr = val

    def snapshot(self, ch: int) -> FMPatch:
        con = self.rl_fb_con[ch]
        pa = self.pms_ams[ch]
        ops = tuple(
            OpRegs(
                dt_mul=self.ops[ch][i].dt_mul,
                tl=self.ops[ch][i].tl,
                ks_ar=self.ops[ch][i].ks_ar,
                am_d1r=self.ops[ch][i].am_d1r,
                dt2_d2r=self.ops[ch][i].dt2_d2r,
                d1l_rr=self.ops[ch][i].d1l_rr,
            )
            for i in range(4)
        )
        return FMPatch(
            alg=con & 7,
            fb=(con >> 3) & 7,
            ams=pa & 3,
            pms=(pa >> 4) & 7,
            ops=ops,  # type: ignore[arg-type]
        )

    def pan_byte(self, ch: int) -> int:
        rl = (self.rl_fb_con[ch] >> 6) & 3
        if rl == 3:
            return 0xFF
        if rl == 2:
            return 0x0F  # R
        if rl == 1:
            return 0xF0  # L
        return 0x00

    def carrier_tl(self, ch: int) -> int:
        """Loudest carrier's TL (min) for the channel's current algorithm."""
        tls = [
            self.ops[ch][i].tl & 0x7F
            for i in CARRIERS.get(self.rl_fb_con[ch] & 7, (3,))
        ]
        return min(tls) if tls else 0x7F


# Furnace chip-wide OPM effects (see doc/7-systems/ym2151.md).
FX_LFO_SPEED = 0x17
FX_LFO_WAVE = 0x18
FX_LFO_AMD = 0x1E
FX_LFO_PMD = 0x1F


def _tone_steps(
    freq_pts: list[tuple[int, int]], gate: int, refit: int, merge_gap: int = 600,
) -> list[tuple[int, int]]:
    """Slide and vibrato inside one note, as (sample, period).

    `merge_gap` collapses a low/high byte pair. The SCC default also
    collapses writes up to 600 samples apart.
    """
    return [
        p for p in merge_step_writes(freq_pts, gap=merge_gap)
        if p[1] and p[0] >= gate + refit
    ]


# A period step of this many semitones, held for `min_hold` samples, is a new
# note. SCC and AY drivers retune with the gate open. The phrase would
# otherwise be one note. Smaller steps are vibrato or a slide.
_NOTE_STEP_SEMITONES = 0.75
_NOTE_HOLD_SAMPLES = 735  # one 60 Hz frame; SCC merge gap and minimum hold
_AY_MERGE_GAP = 16  # low/high byte pair, and a second write in the same tick
_SSG_VOLUME_TRIM = 1  # AY step; the YM2203 SSG is hotter than VGMPlay's split
_AY_NOTE_HOLD = 1470  # two frames: a one-frame AY step is the pitch macro


def _period_semitones(clock: float, divider: int, period: int, offset: int) -> float:
    note, e5 = furnace_period_note(
        module_clock(clock), divider, max(1, period), period_offset=offset,
    )
    return note + (e5 - E5_CENTER) / 128.0


def segment_periods(
    points: list[tuple[int, int]],
    end: int,
    clock: float,
    divider: int,
    offset: int,
    *,
    merge_gap: int = _NOTE_HOLD_SAMPLES,
    min_hold: int = _NOTE_HOLD_SAMPLES,
    drop_short: bool = True,
) -> list[tuple[int, int, list[tuple[int, int]]]]:
    """Split a period timeline into notes.

    Returns (start_sample, period, intra) per note. A step of at least
    0.75 semitone held for `min_hold` starts a new note. Smaller steps stay
    in `intra`.
    """
    if not points:
        return []
    merged = merge_step_writes(list(points), gap=merge_gap)
    cur_t, cur_p = merged[0]
    cur_s = _period_semitones(clock, divider, cur_p, offset)
    intra: list[tuple[int, int]] = []
    segs: list[tuple[int, int, list[tuple[int, int]]]] = []
    for i in range(1, len(merged)):
        t, period = merged[i]
        hold_until = merged[i + 1][0] if i + 1 < len(merged) else end
        held = hold_until - t
        step = abs(_period_semitones(clock, divider, period, offset) - cur_s)
        new_s = _period_semitones(clock, divider, period, offset)
        if step >= _NOTE_STEP_SEMITONES and held >= min_hold:
            segs.append((cur_t, cur_p, intra))
            cur_t, cur_p = t, period
            cur_s = new_s
            intra = []
        elif step >= _NOTE_STEP_SEMITONES and drop_short:
            continue
        else:
            intra.append((t, period))
            cur_s = new_s
    segs.append((cur_t, cur_p, intra))
    return segs


def _value_at(pts, start: int, default):
    """The value in force at ``start`` (the last write at or before it)."""
    cur = default
    for item in pts:
        if item[0] <= start:
            cur = item[1]
        else:
            break
    return cur


def _tone_pitch_tick_scale(vgm: VgmFile, hz: float, speed: int) -> int:
    """Raise hz and speed until tone pitch macros see every write.

    The row length stays the same and the tick gets finer. A driver that
    writes the period faster than the module tick would otherwise alias.
    """
    groups: dict[tuple[str, int, int], list[int]] = {}
    for w in vgm.writes:
        if w.chip in ("ym2203_ssg", "ay8910") and w.reg % 2 == 0 and w.reg < 6:
            key = (w.chip, w.chip_id, w.reg // 2)
        elif w.chip == "k051649" and w.reg % 2 == 0 and w.reg < 10:
            key = (w.chip, w.chip_id, w.reg // 2)
        elif (hz < 100 and w.chip == "ym2203"
              and w.reg in (0xA0, 0xA1, 0xA2)):
            # A 60 Hz fallback is especially coarse for a driver that sweeps
            # the YM2203 f-num between key-ons (Psychic 5 Credit). Only lift
            # the fallback; a measured FM lattice already has a fine tick.
            key = (w.chip, w.chip_id, w.reg - 0xA0)
        elif (hz < 100 and w.chip == "ym2612"
              and (w.reg & 0xFF & 0xFC) in (0xA0, 0xA4)):
            # The same for a Genesis driver that rewrites the YM2612 f-num
            # every frame (IceCap): a 60 Hz fallback tick would alias it.
            key = (
                w.chip, w.chip_id,
                (w.reg & 0xFF & 0xFB) + (0x100 if w.reg & 0x100 else 0),
            )
        else:
            continue
        groups.setdefault(key, []).append(w.sample)
    intervals = [
        b - a
        for times in groups.values()
        for a, b in zip(times, times[1:])
        if b - a >= 8
    ]
    if len(intervals) < 8:
        return 1
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if median <= 0:
        return 1
    k = int(math.ceil((VGM_RATE / hz) / median))
    return max(1, min(k, 32, 255 // max(1, speed)))


def _musical_row(grid: Grid) -> tuple[float, int, int, str]:
    """One row per measured 16th, one tick per 60 Hz frame.

    Furnace's BPM is `60*hz/(highlight*speed)`. Highlight 4 makes this 15
    times the row rate.
    """
    frame = VGM_RATE / 60.0
    # `pulse` is the note value. `unit` may have been lengthened so an FM
    # rip stays inside the order budget, and that length is not a 16th.
    row = grid.pulse or grid.unit
    speed = max(1, min(255, int(round(row / frame))))
    hz = VGM_RATE * speed / row
    note = (
        f"{grid.source}, {grid.confidence * 100:.0f}% within 6%; "
        f"row = measured 16th, {speed} ticks/row"
    )
    return hz, speed, 1, note


def _placed_row(sample: int, spr: float, speed: int, hz: float) -> int:
    """The row ``Song.place`` assigns at t0 = 0."""
    if speed <= 1:
        return max(0, int(math.floor(sample / spr + 0.5)))
    row = int(sample // spr)
    tick = VGM_RATE / hz
    delay = int(round((sample - row * spr) / tick))
    if delay >= speed:
        row += 1
    return max(0, row)


def _tone_collisions(
    groups: dict[int, list[int]], spr: float, speed: int, hz: float,
) -> int:
    """Same-channel note-ons that ``Song.place`` would drop onto one cell."""
    n = 0
    for times in groups.values():
        seen: set[int] = set()
        for sample in times:
            row = _placed_row(sample, spr, speed, hz)
            if row in seen:
                n += 1
            seen.add(row)
    return n


def _fit_tone_subdivision(
    hz: float, speed: int, events: list[NoteEvent], note: str,
) -> tuple[float, int, float, str]:
    """Drop ticks per row until each note has its own cell.

    Returns the new (hz, speed, subdiv, note) with the largest speed that
    reduces same-channel collisions.
    """
    tick = VGM_RATE / hz
    groups: dict[int, list[int]] = {}
    for event in events:
        if event.on and event.tone_chip:
            groups.setdefault(event.ch, []).append(event.sample)
    if not groups or speed <= 1:
        return hz, speed, 1.0, note
    best_speed = speed
    best_hits = _tone_collisions(groups, tick * speed, speed, hz)
    for spd in range(speed - 1, 0, -1):
        if best_hits == 0:
            break
        hits = _tone_collisions(groups, tick * spd, spd, hz)
        if hits < best_hits:
            best_speed, best_hits = spd, hits
    if best_speed == speed:
        return hz, speed, 1.0, note
    ratio = speed / best_speed
    head = note.split("; row =", 1)[0]
    fitted = (
        f"{head}; row = {best_speed} ticks of the measured 16th's tick "
        f"({speed} per 16th); highlight {round(4 * ratio)} keeps the quarter-note tempo"
    )
    return hz, best_speed, ratio, fitted


def _absorb_detune(event: NoteEvent, pitch: tuple[int, ...], speed: int) -> tuple[int, ...]:
    """Fold a one-step tuning offset into the note.

    A macro that only settles on a constant detune is the note's pitch.
    Vibrato stays a macro. Returns the remaining pitch macro.
    """
    if not pitch or speed != 1:
        return pitch
    if pitch[0] == 0:
        i = 0
        while i < len(pitch) and pitch[i] == 0:
            i += 1
        if i == len(pitch):
            return ()
        # A zero prefix and then one held offset is the note settling on its
        # pitch. A value that comes back (vibrato) stays a macro.
        if any(v != pitch[i] for v in pitch[i:]):
            return pitch
        tail = pitch[i]
    else:
        if any(v != pitch[0] for v in pitch):
            return pitch
        tail = pitch[0]
    if not tail or abs(tail) > 127:
        return pitch
    fine = (event.e5 - E5_CENTER) + tail
    while fine > 127 and event.note < NOTE_MAX:
        event.note += 1
        fine -= 128
    while fine < -128 and event.note > 0:
        event.note -= 1
        fine += 128
    event.e5 = E5_CENTER + max(-128, min(127, fine))
    return ()


def _macro_if_moving(
    pts: list[tuple[int, int]], start: int, ticks: int, tick_samples: float,
) -> tuple[tuple[int, ...], int, int]:
    """A per-tick macro, or nothing when the register never moves."""
    if len(pts) < 2 or all(v == pts[0][1] for _t, v in pts):
        return (), 1, 255
    return _macro_series(_tick_values(pts, start, ticks, tick_samples))


def _plain_tone(spec: ToneIns, tone_chip: int) -> ToneIns:
    """The same timbre with no per-note motion, so notes can share it."""
    if tone_chip == 1:
        return ToneIns(wave=spec.wave)
    return ToneIns()


def _take_tone(
    spec: ToneIns,
    mapping: dict[ToneIns, int],
    bucket: list[ToneIns],
    others: int,
    cap: int,
    tone_chip: int,
) -> tuple[int, ToneIns]:
    """Index of `spec` in `bucket`.

    Past the instrument cap, share a plain instrument of the same wave.
    """
    found = mapping.get(spec)
    if found is not None:
        return found, spec
    if len(bucket) + others < cap:
        idx = len(bucket)
        mapping[spec] = idx
        bucket.append(spec)
        return idx, spec
    plain = _plain_tone(spec, tone_chip)
    found = mapping.get(plain)
    if found is not None:
        return found, plain
    for i, existing in enumerate(bucket):
        if existing == plain:
            return i, existing
    if len(bucket) + others < cap:
        idx = len(bucket)
        mapping[plain] = idx
        bucket.append(plain)
        return idx, plain
    return 0, bucket[0] if bucket else plain


def _bind_tone_instruments(
    events: list[NoteEvent],
    scc_ins: list[ToneIns],
    ay_ins: list[ToneIns],
    psg_ins: list[ToneIns],
    tone_ins_base: int,
    scc_clock: int,
    ay_clock: int,
    psg_clock: int,
    hz: float,
    total_samples: int,
) -> None:
    """Fill `scc_ins` / `ay_ins` and point each tone note at one.

    Pitch, wave and AY register motion that changes during a note become
    macros. AY notes bind first. The stored indices follow the SCC list.
    """
    if not any(e.tone_chip for e in events):
        return
    tick_samples = VGM_RATE / hz if hz else VGM_RATE / 60.0
    track_end = total_samples or max((e.sample for e in events), default=0)
    scc_map: dict[ToneIns, int] = {}
    ay_map: dict[ToneIns, int] = {}
    psg_map: dict[ToneIns, int] = {}
    ins_cap = max(1, 250 - tone_ins_base)
    bound: list[tuple[NoteEvent, int]] = []
    tone_order = [e for e in events if e.on and e.tone_chip == 2]
    tone_order += [e for e in events if e.on and e.tone_chip == 1]
    tone_order += [e for e in events if e.on and e.tone_chip == 3]
    for e in tone_order:
        end = e.end or track_end
        # 255 macro values times the largest speed byte. Long SSG pads can
        # hold a sparse late pitch step; a 4096-tick cutoff used to drop it.
        ticks = max(1, min(int((end - e.sample) / tick_samples) + 1, 65025))
        if e.tone_chip == 1:
            clock, divider = scc_clock, 16
            maximum = 0xFFF
        elif e.tone_chip == 3:
            # The SMS core defaults to the full COLOR_NTSC clock (not the
            # half-clock AY default) and a 10-bit period with divider 64.
            clock = e.tone_clock or psg_clock or PSG_DEFAULT_CLOCK
            if abs(clock - PSG_FURNACE_CLOCK) <= 1:
                clock = PSG_FURNACE_CLOCK
            divider = PSG_DIVIDER
            maximum = PSG_MAX_PERIOD
        else:
            # AY and the YM2203's SSG share the instrument path but not the
            # clock or Furnace's divider (8 vs 16).
            clock = e.tone_clock or ay_clock
            divider = e.tone_div or 8
            maximum = 0xFFF
        tbl = (
            pitch_table(float(clock), divider, maximum=maximum)
            if e.tone_chip == 3
            else pitch_table(module_clock(clock), divider)
        )
        base_off = (e.note << 7) + (e.e5 - E5_CENTER)
        pitch: tuple[int, ...] = ()
        pitch_speed, pitch_loop = 1, 255
        if e.tone_pitch_pts:
            pts = [(e.sample, 0)] + [
                (t, tbl.offset_for(e.note, per + e.tone_period_offset) - base_off)
                for t, per in e.tone_pitch_pts
            ]
            series = _tick_values(pts, e.sample, ticks, tick_samples)
            if any(series):
                pitch, pitch_speed, pitch_loop = _macro_series(series)
                pitch = _absorb_detune(e, pitch, pitch_speed)
        wave_vals: tuple[int, ...] = ()
        wave_speed, wave_loop = 1, 255
        if e.tone_wave_pts:
            pts = [(e.sample, max(0, e.tone_wave))] + list(e.tone_wave_pts)
            wave_vals, wave_speed, wave_loop = _macro_series(
                _tick_values(pts, e.sample, ticks, tick_samples),
            )
        column_vol = e.vol
        vol_vals: tuple[int, ...] = ()
        vol_speed, vol_loop = 1, 255
        series: list[int] = []
        if e.vol_pts and e.tone_chip != 3:
            # PSG volume is the raw 4-bit register and the SMS platform reads
            # it straight from the column; a std.vol macro would scale it.
            pts = [(e.sample, max(0, e.vol))] + [
                (t, max(0, v)) for t, v, _ in e.vol_pts
            ]
            series = _tick_values(pts, e.sample, ticks, tick_samples)
            folded = _fold_saw(series)
            # Past 255 ticks a non-repeating series is stored as every Nth
            # value. A one-level saw is played as a slide instead, so the
            # ramp still moves on every tick.
            thin = (
                folded is None
                and len(series) > MACRO_MAX
                and _cycle(series) is None
                and _unit_saw(series)
            )
            if folded is not None:
                vol_vals, vol_speed, vol_loop = folded
            elif thin:
                e.vol = series[0]
                e.vol_slide = _slide_open(series)
            else:
                vol_vals, vol_speed, vol_loop = _macro_series(series)
                used = len(scc_ins) + len(ay_ins)
                # AY volume is the 4-bit register. Step-2 / step-4 rounding
                # is the SCC list getting long, and it stays on the SCC.
                if (
                    e.tone_chip == 1
                    and vol_vals
                    and used > 160
                    and not _unit_saw(vol_vals)
                ):
                    step = 2 if used <= 208 else 4
                    vol_vals = tuple((v // step) * step for v in vol_vals)
                    if len(set(vol_vals)) <= 1:
                        vol_vals = ()
        noise = mode = env_shape = env_period = ()
        noise_speed = mode_speed = env_shape_speed = env_period_speed = 1
        noise_loop = mode_loop = env_shape_loop = env_period_loop = 255
        if e.tone_fx_pts:
            noise, noise_speed, noise_loop = _macro_if_moving(
                [(t, val) for t, cmd, val in e.tone_fx_pts if cmd == FX_NOISE],
                e.sample, ticks, tick_samples,
            )
            mode, mode_speed, mode_loop = _macro_if_moving(
                [(t, (val + 1) & 7) for t, cmd, val in e.tone_fx_pts if cmd == FX_MODE],
                e.sample, ticks, tick_samples,
            )
            env_shape, env_shape_speed, env_shape_loop = _macro_if_moving(
                [(t, (val >> 4) & 0x0F) for t, cmd, val in e.tone_fx_pts if cmd == FX_ENV_SET],
                e.sample, ticks, tick_samples,
            )
            lo = hi = None
            period_pts: list[tuple[int, int]] = []
            for t, cmd, val in e.tone_fx_pts:
                if cmd == FX_ENV_LO:
                    lo = val
                elif cmd == FX_ENV_HI:
                    hi = val
                else:
                    continue
                if lo is not None and hi is not None:
                    period_pts.append((t, lo | (hi << 8)))
            env_period, env_period_speed, env_period_loop = _macro_if_moving(
                period_pts, e.sample, ticks, tick_samples,
            )
        spec = ToneIns(
            wave=max(0, e.tone_wave),
            pitch=pitch, pitch_speed=pitch_speed, pitch_loop=pitch_loop,
            waves=wave_vals, wave_speed=wave_speed, wave_loop=wave_loop,
            vol=vol_vals, vol_speed=vol_speed, vol_loop=vol_loop,
            noise=noise, noise_speed=noise_speed, noise_loop=noise_loop,
            mode=mode, mode_speed=mode_speed, mode_loop=mode_loop,
            env_shape=env_shape, env_shape_speed=env_shape_speed,
            env_shape_loop=env_shape_loop,
            env_period=env_period, env_period_speed=env_period_speed,
            env_period_loop=env_period_loop,
        )
        if e.tone_chip == 1:
            idx, kept = _take_tone(spec, scc_map, scc_ins, len(ay_ins), ins_cap, 1)
        elif e.tone_chip == 3:
            idx, kept = _take_tone(
                spec, psg_map, psg_ins, len(scc_ins) + len(ay_ins), ins_cap, 3,
            )
        else:
            # AY runs first, so the SCC list is still empty and `others` is 0.
            idx, kept = _take_tone(spec, ay_map, ay_ins, 0, ins_cap, 2)
        bound.append((e, idx))
        if e.tone_chip == 3:
            # The SMS column is the raw 4-bit register; keep vol_pts as
            # pattern steps rather than folding them into a volume macro.
            pass
        elif kept.vol:
            # Column 15 makes the macro the register (SCC: vol*macro/15,
            # AY: macro + column - 15).
            e.vol_pts = ()
            e.vol = 15
            e.vol_slide = 0
        elif e.vol_slide or (vol_vals and series and _unit_saw(vol_vals)):
            # No volume macro on the instrument the note actually got, so the
            # column is the register. A one-level saw keeps moving one step
            # per tick from there.
            if not e.vol_slide:
                e.vol = series[0]
                e.vol_slide = _slide_open(series)
            e.vol_pts = _saw_anchors(series, e.sample, tick_samples)
        elif vol_vals:
            # The decay did not fit in the instrument list. The column steps
            # are the copy that still reaches the pattern.
            e.vol = column_vol
        else:
            e.vol = column_vol
            e.vol_pts = ()
    n_scc = len(scc_ins)
    n_ay = len(ay_ins)
    for event, idx in bound:
        if event.tone_chip == 1:
            event.ins = tone_ins_base + idx
        elif event.tone_chip == 3:
            event.ins = tone_ins_base + n_scc + n_ay + idx
        else:
            event.ins = tone_ins_base + n_scc + idx


# The format stores a macro length in one byte, so 255 is the longest macro
# that can be declared. 256 wraps to 0 and the reader then never consumes the
# data block, reporting "invalid macro code" on the bytes after it.
MACRO_MAX = 255


def _tick_values(
    pts: list[tuple[int, int]], start: int, ticks: int, tick_samples: float,
) -> list[int]:
    """Value in force at each tick from `start` (each write holds until the
    next one)."""
    out: list[int] = []
    i = 0
    cur = pts[0][1] if pts else 0
    for k in range(ticks):
        t = start + k * tick_samples
        while i < len(pts) and pts[i][0] <= t:
            cur = pts[i][1]
            i += 1
        out.append(cur)
    return out


def _cycle(values: list[int], limit: int = 128) -> int | None:
    """Length of the shortest exact repeating cycle of a series (2 or more).

    A series that only rarely changes must not pass as a one-step cycle, so
    the repeat has to be exact.
    """
    n = len(values)
    for p in range(2, min(limit, n // 2) + 1):
        if all(values[i] == values[i - p] for i in range(p, n)):
            return p
    return None


def _macro_series(series: list[int]) -> tuple[tuple[int, ...], int, int]:
    """(values, speed, loop) for one tone macro from a per-tick series.

    A constant series needs no macro at all; a periodic one becomes a single
    cycle that loops; anything else is stored whole, thinned by whole ticks
    only when it does not fit the format's one-byte length. A macro that does
    not loop drops its trailing run of one value, since the engine holds the
    last value anyway - that is what lets two notes whose morph differs only
    in how long it lasts share one instrument.
    """
    if not series or len(set(series)) == 1:
        return (), 1, 255
    speed = 1
    loop = 255
    if len(series) > MACRO_MAX:
        p = _cycle(series)
        if p is not None:
            return tuple(series[:p]), 1, 0
        speed = min(255, (len(series) + MACRO_MAX - 1) // MACRO_MAX)
        series = series[::speed]
    p = _cycle(series)
    if p is not None and p < len(series):
        return tuple(series[:p]), speed, 0
    while len(series) > 1 and series[-1] == series[-2]:
        series = series[:-1]
    return tuple(series), speed, loop


def _unit_saw(values: list[int] | tuple[int, ...]) -> bool:
    """True when the series steps by one level, mostly downward.

    Holds (a repeated level) are ignored. A jump back to the top of a tooth
    counts against the ratio, so a real saw still passes and a melody does not.
    """
    if len(values) < 3:
        return False
    deltas = [b - a for a, b in zip(values, values[1:])]
    nonzero = [d for d in deltas if d]
    if not nonzero:
        return False
    ones = sum(1 for d in nonzero if abs(d) == 1)
    if ones * 4 < len(nonzero) * 3:
        return False
    down = sum(1 for d in nonzero if d == -1)
    return down >= len(nonzero) - down


def _slide_open(series: list[int]) -> int:
    """1 if the note starts by stepping down, 2 if the opening level holds."""
    if len(series) >= 2 and series[1] == series[0]:
        return 2
    return 1


def _saw_anchors(
    series: list[int], start: int, tick_samples: float,
) -> tuple[tuple[int, int, int], ...]:
    """Volume anchors after tick 0 for an FA slide.

    The note cell covers tick 0. A tick the running slide already produces
    (one level down) is skipped. A hold writes the level and stops the slide;
    any other step re-arms it. The third element is 1 to keep sliding, 0 to hold.
    """
    anchors: list[tuple[int, int, int]] = []
    n = len(series)
    if n < 2:
        return ()
    sliding = series[1] == series[0] - 1
    i = 1
    while i < n:
        if sliding and series[i] == series[i - 1] - 1:
            i += 1
            continue
        level = series[i]
        if series[i] == series[i - 1] or (i + 1 < n and series[i + 1] == level):
            anchors.append((int(round(start + i * tick_samples)), level, 0))
            i += 1
            while i < n and series[i] == level:
                i += 1
            sliding = False
            continue
        anchors.append((int(round(start + i * tick_samples)), level, 1))
        sliding = True
        i += 1
    return tuple(anchors)


def _fold_saw(series: list[int]) -> tuple[tuple[int, ...], int, int] | None:
    """A repeating one-level saw as ``(tooth, speed 1, loop 0)``.

    The tooth may begin mid-ramp, and the last tooth may be cut short. A hold
    after the saw is left to the ordinary macro path: looping it would restart
    the ramp. One extra tick of the final level still folds, because the tick
    count rounds the note end onto the next frame.
    """
    if len(series) < 3:
        return None
    end = len(series)
    while end > 1 and series[end - 1] == series[end - 2]:
        end -= 1
    if len(series) - end > 1:
        return None
    body = series[:end]
    if not _unit_saw(body):
        return None
    jumps = [i for i in range(1, len(body)) if body[i] > body[i - 1]]
    cycle: list[int] | None = None
    phase = 0
    if len(jumps) >= 2:
        gaps = [b - a for a, b in zip(jumps, jumps[1:])]
        p = gaps[0]
        if not (2 <= p <= 64) or any(g != p for g in gaps):
            return None
        phase = jumps[0]
        if phase + p > len(body):
            return None
        cycle = body[phase:phase + p]
        if any(body[i] != cycle[(i - phase) % p] for i in range(len(body))):
            return None
    elif len(jumps) == 1:
        p = jumps[0]
        if not (2 <= p <= 64) or p >= len(body):
            return None
        phase = 0
        cycle = body[:p]
        if any(body[i] != cycle[i % p] for i in range(len(body))):
            return None
    else:
        return None
    if any(cycle[i] != cycle[0] - i for i in range(len(cycle))):
        return None
    start = (-phase) % len(cycle)
    rotated = cycle[start:] + cycle[:start]
    if rotated[0] != body[0]:
        return None
    return tuple(rotated), 1, 0


def _flush_pms_ramp(
    acc: dict[int, list],
    ins: int,
    ramp: list[tuple[int, int, int]],
    kon_sample: int,
) -> None:
    """Record one note's PMS/AMS writes (drop the key-off reset to 0)."""
    if ins < 0 or not ramp:
        return
    pms: list[int] = []
    ams: list[int] = []
    deltas: list[int] = []
    prev_t = kon_sample
    for t, p, a in ramp:
        if pms and p == pms[-1] and a == ams[-1]:
            continue
        if p == 0 and a == 0 and pms:
            # The reset switches vibrato off. Keep it as the final step.
            # A macro holds its last value for the rest of the song.
            pms.append(0)
            ams.append(0)
            deltas.append(deltas[-1] if deltas else max(1, t - prev_t))
            break
        pms.append(p)
        ams.append(a)
        deltas.append(max(1, t - prev_t))
        prev_t = t
    if not pms:
        return
    # Key-on is always PMS 0; the first write is usually 1 a few frames later.
    if pms[0] != 0:
        pms.insert(0, 0)
        ams.insert(0, 0)
        deltas.insert(0, deltas[0] if deltas else 735)
    acc.setdefault(ins, []).append((pms, ams, deltas))


def _pick_lfo_macros(
    ramps: list[tuple[list[int], list[int], list[int]]],
    samples_per_tick: float,
) -> tuple[tuple[int, ...], tuple[int, ...], int]:
    """Driver-scripted vibrato: PMS/AMS step up as the note is held.

    The drivers increment the sensitivity about every 0.1 s of sustain, and
    the macro holds each value for `speed` ticks, so the step duration is
    converted with the song's real tick rate (44100/hz), not a fixed frame.
    """
    if not ramps:
        return (), (), 1
    # Prefer notes that actually complete a ramp (PMS reached >= 4).
    long_ones = [r for r in ramps if max(r[0]) >= 4]
    pool = long_ones or ramps
    seqs = Counter(tuple(p) for p, _a, _d in pool)
    best_p, n = seqs.most_common(1)[0]
    if n < 1 or max(best_p) < 2:
        return (), (), 1
    ams_seqs = Counter(
        tuple(a[: len(best_p)]) for p, a, _d in pool if tuple(p) == best_p
    )
    best_a = ams_seqs.most_common(1)[0][0] if ams_seqs else tuple(0 for _ in best_p)
    if len(best_a) < len(best_p):
        best_a = best_a + (best_a[-1] if best_a else 0,) * (len(best_p) - len(best_a))
    # Collapse a trailing hold (7,7,7,…) to one step; macros hold the last value.
    while len(best_p) >= 2 and best_p[-1] == best_p[-2]:
        best_p = best_p[:-1]
        best_a = best_a[:-1]
    step_samples: list[int] = []
    for p, _a, d in pool:
        if tuple(p) != best_p:
            continue  # only the ramps the macro actually models
        # d[0] is the note-on -> first-write lead-in, not a step.
        step_samples.extend(d[1: len(best_p)])
    if not step_samples:
        step_samples = [int(samples_per_tick)]
    med = sorted(step_samples)[len(step_samples) // 2]
    speed = max(1, min(255, int(round(med / samples_per_tick))))
    return tuple(best_p), tuple(best_a[: len(best_p)]), speed


def suggest_speed(keyon_samples: list[int], hz: float = 60.0) -> int:
    """Pick ticks/row from inter-onset frames. Faithful default is 1."""
    if len(keyon_samples) < 8:
        return 1
    frame = VGM_RATE / hz
    frames = [int(round(s / frame)) for s in keyon_samples]
    diffs = [b - a for a, b in zip(frames, frames[1:]) if 1 <= (b - a) <= 16]
    if not diffs:
        return 1
    hist = Counter(diffs)
    # Prefer a 3–8 frame grid if it is a real peak, else speed 1 (one row per frame).
    candidates = [(d, c) for d, c in hist.most_common() if 3 <= d <= 8]
    total = sum(hist.values())
    if candidates and candidates[0][1] >= max(12, total * 0.18):
        return candidates[0][0]
    return 1


def _finalize_sweep(
    ev: NoteEvent,
    pts: list[tuple[int, int, int]],
    base_kc: int,
    base_kf: int,
) -> None:
    """Attach a note's mid-note pitch writes to it.

    Drivers write KC/KF as a pair a few samples apart; merge those pairs, or
    the write-order transient enters the trajectory. The note pitch comes from
    the first pair when it lands right after the key-on, otherwise from the
    pre-key-on registers (the note really started at the old pitch).
    """
    if not pts:
        return
    merged: list[tuple[int, int, int]] = []
    for p in pts:
        if merged and p[0] - merged[-1][0] <= 16:
            merged[-1] = p
        else:
            merged.append(p)
    if merged[0][0] - ev.sample <= 120:
        ref_kc, ref_kf = merged[0][1], merged[0][2]
    else:
        ref_kc, ref_kf = base_kc, base_kf
    if ev.on and (ref_kc, ref_kf) != (base_kc, base_kf):
        ev.note = kc_to_furnace_note(ref_kc)
        ev.e5 = kf_to_e5(ref_kf)
    r0 = kc_kf_units(ref_kc, ref_kf)
    deltas = tuple((s, kc_kf_units(kc, kf) - r0) for s, kc, kf in merged)
    # The note's own pitch counts as a trajectory point. Without it a single
    # held mid-note step has no spread and is dropped.
    vals = [0] + [d for _s, d in deltas]
    if max(vals) - min(vals) >= 4:
        ev.sweep = deltas


def _emit_ay_notes(
    events: list[NoteEvent],
    channel_fx: list[tuple[int, int, int, int]],
    hits,
    offs,
    at: dict[int, int],
    clock: int,
    divider: int,
    ins_base: int,
    total_samples: int = 0,
) -> int:
    """Append notes and key-offs for one AY-compatible register stream.

    Used for both a stand-alone AY-8910 (divider 8) and the YM2203's SSG
    (divider 16).  The channel base map decides where each chip instance
    lands; the pitch macros use the matching Furnace pitch table.
    """
    count = 0
    for h in hits:
        base = at.get(h.chip_id)
        if base is None:
            continue
        ch = base + h.voice
        st = h.sample_time
        gate_end = h.off_sample or (total_samples or st)
        if h.opened_tone or h.freq_pts:
            segs = segment_periods(
                [(h.sample_time, h.period), *h.freq_pts],
                gate_end, clock, divider, 0,
                merge_gap=_AY_MERGE_GAP,
                min_hold=_AY_NOTE_HOLD,
                drop_short=False,
            )
        else:
            segs = [(h.sample_time, h.period, [])]
        buckets: list[list[tuple[int, int, int]]] = [[] for _ in segs]
        for t, fx, val in h.fx_pts:
            idx = 0
            for i, (sst, _per, _intra) in enumerate(segs):
                if sst <= t:
                    idx = i
                else:
                    break
            buckets[idx].append((t, fx, val))
        for i, (sst, period, intra) in enumerate(segs):
            nxt = segs[i + 1][0] if i + 1 < len(segs) else 0
            seg_end = nxt or h.off_sample
            bound = nxt or gate_end
            note, e5 = ay_note(clock, period, divider)
            steps = _tone_steps(intra, sst, 0, _AY_MERGE_GAP)
            trim = _SSG_VOLUME_TRIM if divider >= 16 else 0
            vol = max(0, _value_at(h.vol_pts, sst, h.vol) - trim)
            vol_pts = tuple(
                (t, max(0, v - trim), None)
                for t, v in h.vol_pts if sst < t < bound
            )
            prev_st = segs[i - 1][0] if i else -1
            latest: dict[int, int] = {}
            for t, fx, val in buckets[i]:
                if t > sst:
                    continue
                if fx == FX_ENV_SET and i > 0 and t <= prev_st:
                    continue
                latest[fx] = val
            for fx, val in latest.items():
                channel_fx.append((sst, ch, fx, val))
            events.append(NoteEvent(
                sample=sst,
                ch=ch,
                on=True,
                note=note,
                ins=ins_base,
                vol=vol,
                e5=e5,
                pan=0,
                end=seg_end,
                vol_pts=vol_pts,
                tone_pitch_pts=tuple(steps),
                tone_chip=2,
                tone_clock=clock,
                tone_div=divider,
                tone_fx_pts=tuple(buckets[i]),
            ))
            count += 1
    for off in offs:
        base = at.get(off.chip_id)
        if base is None:
            continue
        events.append(NoteEvent(
            sample=off.sample_time, ch=base + off.voice, on=False, note=180,
            ins=-1, vol=-1, e5=0x80, pan=0,
        ))
    return count


def _steppable_pitch(
    pts: tuple[tuple[int, int, int], ...], max_reversals: int = 8,
    tolerance: float = 2.0, min_segment_samples: float = 400.0,
) -> bool:
    """True when a pattern row can follow this trajectory.

    Glides and slow curves have few direction changes. A vibrato that
    reverses within a row needs a macro.
    """
    if len(pts) < 3:
        return True
    reversals = 0
    direction = 0
    last_change = pts[0][0]
    prev = opn_pitch_units(pts[0][1], pts[0][2])
    for t, b, f in pts[1:]:
        cur = opn_pitch_units(b, f)
        d = cur - prev
        prev = cur
        if d > tolerance:
            new = 1
        elif d < -tolerance:
            new = -1
        else:
            continue
        if direction and new != direction:
            if t - last_change < min_segment_samples:
                return False
            last_change = t
            reversals += 1
            if reversals > max_reversals:
                return False
        direction = new
    return True


def _bind_fm_pitch(
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    clock: int,
    hz: float,
    total_samples: int,
    cap: int = 200,
    allow_steps: bool = False,
) -> tuple[int, int]:
    """Turn YM2203/YM2612 f-num trajectories into instrument pitch macros.

    `pitch2` is a 1/128-semitone offset. A macro carries the driver's
    motion without row quantization. `allow_steps` (YM2612) encodes short
    steps as row effects instead. Returns (copies made, row-ramp fallbacks).
    """
    moving = [e for e in events if e.on and e.fm_pitch_pts]
    if not moving:
        return 0, 0
    tick_samples = VGM_RATE / hz if hz else VGM_RATE / 60.0
    track_end = total_samples or max((e.sample for e in events), default=0)
    by_ch: dict[int, list[NoteEvent]] = {}
    for e in events:
        if e.on:
            by_ch.setdefault(e.ch, []).append(e)
    made = fallbacks = 0
    idx_map: dict[tuple, int] = {}
    for notes in by_ch.values():
        for i, e in enumerate(notes):
            if not e.fm_pitch_pts:
                continue
            end = e.end or (notes[i + 1].sample if i + 1 < len(notes) else track_end)
            # A macro holds up to 255 values and can step every 255 ticks, so
            # a note of up to 65025 ticks still fits.  Do not truncate earlier:
            # long held notes can change pitch near their end (Avengers 23).
            ticks = max(1, min(int((end - e.sample) / tick_samples) + 1, 65025))
            pts = e.fm_pitch_pts
            ref = opn_pitch_units(pts[0][1], pts[0][2])
            # A held pitch with a jump or two is a step function. One
            # E5/E8/E9 row effect reproduces it without an instrument copy.
            # Only oscillating motion needs a macro.
            if allow_steps and (len(pts) <= 4 or _steppable_pitch(pts)):
                deltas = tuple(
                    (t, int(round(opn_pitch_units(b, f) - ref)))
                    for t, b, f in pts[1:]
                )
                if deltas and any(d for _t, d in deltas):
                    e.pitch_steps = deltas
                    continue
            steps = [
                (e.sample, 0.0),
                *[(t, opn_pitch_units(b, f) - ref) for t, b, f in pts],
            ]
            series = _tick_values(steps, e.sample, ticks, tick_samples)
            # The OPN platform in this module's linear pitch mode adds the
            # macro to the note+pitch value in 1/128 semitones; the trajectory
            # is in 1/64 semitones, so one step is two macro units.
            vals = [
                max(-32768, min(32767, int(round(2.0 * d))))
                for d in series
            ]
            if len(set(vals)) == 1:
                continue
            if len(patch_order) >= cap:
                # No room for another instrument: the driver's own steps
                # become row ramps.  Keep every register write, not just a
                # decayed end point.
                deltas = tuple(
                    (t, int(round(opn_pitch_units(b, f) - ref)))
                    for t, b, f in pts[1:]
                )
                if deltas:
                    spread = [0] + [d for _t, d in deltas]
                    if max(spread) - min(spread) >= 4:
                        e.sweep = deltas
                        fallbacks += 1
                continue
            macro, speed, loop = _macro_series(vals)
            if not macro:
                continue
            base_key = patch_order[e.ins]
            key = ("pitch", base_key, macro, speed, loop)
            idx = idx_map.get(key)
            if idx is None:
                base = patches[base_key]
                copy = replace(
                    base,
                    pitch_macro=macro,
                    pitch_speed=speed,
                    pitch_loop=loop,
                    uses=1,
                    name=f"{base.name}+P",
                )
                patches[key] = copy
                patch_order.append(key)
                idx = len(patch_order) - 1
                idx_map[key] = idx
            else:
                patches[key].uses += 1
            e.ins = idx
            made += 1
    return made, fallbacks


def _rebase_non_fm_ins(
    events: list[NoteEvent], old_count: int, new_count: int,
) -> None:
    """Move sample/tone instrument indices after FM instruments are appended."""
    delta = new_count - old_count
    if delta <= 0:
        return
    for e in events:
        if (e.dac or e.pcm or e.tone_chip) and e.ins >= old_count:
            e.ins += delta


def _apply_opn_carrier_octave(
    events: list[NoteEvent], fm_patches: list[FMPatch],
    *, skip_channels: set[int] | None = None,
) -> int:
    """Label notes at their loudest carrier's octave.

    A constant pitch macro cancels the label move. The source f-num and
    audio stay unchanged. Mixed-carrier patches keep the channel. Returns
    the number of instruments relabelled.
    """
    by_ins: dict[int, list[NoteEvent]] = {}
    skipped_ins: set[int] = set()
    for e in events:
        if not e.on or e.pcm or e.dac or e.tone_chip:
            continue
        if skip_channels and e.ch in skip_channels:
            if 0 <= e.ins < len(fm_patches):
                skipped_ins.add(e.ins)
            continue
        if 0 <= e.ins < len(fm_patches):
            by_ins.setdefault(e.ins, []).append(e)
    shifted = 0
    for ins, notes in by_ins.items():
        patch = fm_patches[ins]
        octave = opn_carrier_octave(patch)
        if octave == 0:
            continue
        if any(not (0 <= e.note + 12 * octave <= 179) for e in notes):
            continue
        if ins in skipped_ins:
            # The same instrument also plays an Ext channel-3 operator
            # column. Give the relabelled channels their own copy. The
            # operator columns keep the unshifted patch.
            patch = replace(patch, name=f"{patch.name} L")
            ins = len(fm_patches)
            fm_patches.append(patch)
            for e in notes:
                e.ins = ins
        for e in notes:
            e.note += 12 * octave
        shift = -1536 * octave
        if patch.pitch_macro:
            macro = tuple(v + shift for v in patch.pitch_macro)
            speed = patch.pitch_speed
            loop = patch.pitch_loop
        else:
            macro = (shift,)
            speed = 1
            loop = 255
        fm_patches[ins] = replace(
            patch, pitch_macro=macro, pitch_speed=speed, pitch_loop=loop,
        )
        shifted += 1
    return shifted


def analyze(
    vgm: VgmFile, *, speed: int | None = None, pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    condense: float = 1.0,
    grid_cache: dict | None = None,
    c352_quad: bool = False,
    dac_slices: bool = False,
) -> Song:
    """Build the Song for one rendition of `vgm`.

    `grid_cache` memoises the grid estimate, which does not depend on
    `condense`. The caller owns it. `c352_quad` keeps the C352 rear pair on
    its own outputs.
    """
    tick = TARGET_TICK_SAMPLES if tick_samples is None else float(tick_samples)
    row_floor = MIN_ROW_SAMPLES if min_row is None else float(min_row)
    ym = YM2151State()
    patches: dict[tuple, FMPatch] = {}
    patch_order: list[tuple] = []
    events: list[NoteEvent] = []
    keyon_times: list[int] = []
    warnings: list[str] = list(vgm.warnings)
    # One module carries one FM family. A mixed log converts one and warns.
    chips = {w.chip for w in vgm.writes}
    has_ym2203 = "ym2203" in chips or "ym2203_ssg" in chips
    has_ym2151 = "ym2151" in chips
    has_ym2612 = "ym2612" in chips
    has_sn76489 = "sn76489" in chips
    if has_ym2203 and has_ym2151:
        warnings.append(
            "YM2151 and YM2203 writes in one log are not supported together; "
            "only the YM2151 was converted"
            if not has_ym2612 else
            "YM2612, YM2151 and YM2203 writes in one log are not supported "
            "together; only the YM2612 was converted"
        )
    if has_ym2612 and has_ym2151 and not has_ym2203:
        warnings.append(
            "YM2612 and YM2151 writes in one log are not supported together; "
            "only the YM2612 was converted"
        )
    if has_ym2612 and has_ym2203 and not has_ym2151:
        warnings.append(
            "YM2612 and YM2203 writes in one log are not supported together; "
            "only the YM2612 was converted"
        )
    is_opn2 = has_ym2612
    is_opn = has_ym2203 and not has_ym2151 and not is_opn2
    ym_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip in ("ym2203", "ym2203_ssg")})
        if is_opn else []
    )
    ym_clock = vgm.ym2203_clock or YM2203_DEFAULT_CLOCK
    # 0xA8-0xAE means 3-slot mode. Furnace's Ext layout gives each
    # channel-3 operator its own column: FM1, FM2, OP1-4, then PSG1-3.
    ym2203_ext = is_opn and any(
        w.chip == "ym2203" and 0xA8 <= w.reg <= 0xAE for w in vgm.writes
    )
    ym2612_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip == "ym2612"})
        if is_opn2 else []
    )
    ym2612_clock = vgm.ym2612_clock or YM2612_DEFAULT_CLOCK
    # 0x27 bits 6-7 select special mode (01) or CSM (10). Both use the Ext
    # layout. CSM timer key-ons are not synthesized.
    ym2612_modes = {
        (w.val >> 6) & 0x03
        for w in vgm.writes
        if is_opn2 and w.chip == "ym2612" and (w.reg & 0xFF) == 0x27
    }
    ym2612_ext = is_opn2 and bool(ym2612_modes & {1, 2})
    ym2612_csm = is_opn2 and 2 in ym2612_modes
    multi_ext = ym2203_ext or ym2612_ext
    ym_chunk = 9 if multi_ext else 6
    ym_base = {
        cid: ym_chunk * i
        for i, cid in enumerate(ym_ids if is_opn else ym2612_ids)
    }
    ym2203_ssg_count = 0
    fm_end = ym_chunk * len(ym_ids if is_opn else ym2612_ids) if (is_opn or is_opn2) else 8
    # A Genesis log carries the SN76489 beside the YM2612.  The module uses a
    # second (SMS) system for it, so its four channels follow every FM instance.
    psg_ids = [0] if (has_sn76489 and is_opn2) else []
    psg_base = fm_end if psg_ids else None
    psg_clock = vgm.sn76489_clock or PSG_DEFAULT_CLOCK
    if psg_ids:
        fm_end += 4
    pending_off = [None] * 8  # type: list[int | None]
    chip_fx: list[tuple[int, int, int] | tuple[int, int, int, int]] = []
    channel_fx: list[tuple[int, int, int, int]] = []
    last_lfrq = last_wave = -1
    # The OPM resets 0x19 to 0 and Furnace defaults PMD/AMD to 0x7F. Pin both
    # to zero before the first row.
    last_amd = last_pmd = 0
    pms_acc: dict[int, list] = {}
    held_ins = [-1] * 8
    held_kon = [-1] * 8
    held_ramp: list[list[tuple[int, int, int]]] = [[] for _ in range(8)]
    # Mid-note KC/KF writes per held note (percussion sweeps).
    sweep_pts: list[list[tuple[int, int, int]]] = [[] for _ in range(8)]
    sweep_base: list[tuple[int, int]] = [(0, 0)] * 8
    sweep_ev: list[int | None] = [None] * 8
    # Mid-note carrier TL writes of a held note (the driver's software volume
    # envelope).  Only the carrier is tracked: the volume column can express a
    # carrier attenuation shift, not a modulator timbre change.
    tl_pts: list[list[tuple[int, int]]] = [[] for _ in range(8)]
    tl_ev: list[int | None] = [None] * 8
    tl_last = [0x7F] * 8
    # Last KON operator mask per channel: key state is per operator, and an
    # envelope restarts only on a 0->1 transition of its bit.
    op_mask = [0] * 8

    def close_sweep(ch: int) -> None:
        idx = sweep_ev[ch]
        if idx is not None:
            _finalize_sweep(events[idx], sweep_pts[ch], sweep_base[ch][0], sweep_base[ch][1])
            sweep_ev[ch] = None

    def close_tl(ch: int) -> None:
        idx = tl_ev[ch]
        if idx is not None and tl_pts[ch]:
            events[idx].tl_pts = tuple(tl_pts[ch])
        tl_ev[ch] = None

    def intern_patch(ch: int, mask: int = 0xF) -> int:
        snap = ym.snapshot(ch)
        if mask != 0xF:
            # An un-keyed operator never starts its envelope. Maximum
            # attenuation (TL 127) is the closest a static OPM instrument gets.
            snap.ops = tuple(
                OpRegs(
                    dt_mul=op.dt_mul,
                    tl=0x7F if not mask & (1 << i) else op.tl,
                    ks_ar=op.ks_ar,
                    am_d1r=op.am_d1r,
                    dt2_d2r=op.dt2_d2r,
                    d1l_rr=op.d1l_rr,
                )
                for i, op in enumerate(snap.ops)
            )
        key = snap.identity() + (mask,)
        if key not in patches:
            snap.name = f"OPM {len(patch_order):02d}"
            patches[key] = snap
            patch_order.append(key)
        p = patches[key]
        p.uses += 1
        return patch_order.index(key)

    def emit_chip_fx(sample: int, cmd: int, val: int) -> None:
        # A same-value 0x18 write is not redundant: the OPM reloads its LFO
        # counter on every write.
        if chip_fx and chip_fx[-1] == (sample, cmd, val):
            return
        chip_fx.append((sample, cmd, val))

    if has_ym2151:
        emit_chip_fx(0, FX_LFO_AMD, 0)
        emit_chip_fx(0, FX_LFO_PMD, 0)

    last_keyon_sample = [-10**9] * 8
    last_test_pulse = -10**9
    for w in vgm.writes:
        if w.chip != "ym2151":
            continue
        if w.reg != 0x08:
            if w.reg == 0x18:
                last_lfrq = w.val & 0xFF
                emit_chip_fx(w.sample, FX_LFO_SPEED, last_lfrq)
            elif w.reg == 0x01:
                # TEST bit 1 gates the LFO's wave-3 term. Capcom's driver
                # pulses it at key-on to re-seed the LFO. Emit 17 00, then
                # restore the real LFRQ one tick later.
                if w.val & 0x02:
                    last_test_pulse = w.sample
                    emit_chip_fx(w.sample, FX_LFO_SPEED, 0)
                    chip_fx.append(
                        (w.sample, FX_LFO_SPEED, max(0, last_lfrq), 1)
                    )
                elif w.sample - last_test_pulse > 2:
                    emit_chip_fx(w.sample, FX_LFO_SPEED, max(0, last_lfrq))
            elif w.reg == 0x19:
                if w.val & 0x80:
                    pmd = w.val & 0x7F
                    if pmd != last_pmd:
                        last_pmd = pmd
                        emit_chip_fx(w.sample, FX_LFO_PMD, pmd)
                else:
                    amd = w.val & 0x7F
                    if amd != last_amd:
                        last_amd = amd
                        emit_chip_fx(w.sample, FX_LFO_AMD, amd)
            elif w.reg == 0x1B:
                wave = w.val & 3
                if wave != last_wave:
                    last_wave = wave
                    emit_chip_fx(w.sample, FX_LFO_WAVE, wave)
            elif 0x38 <= w.reg <= 0x3F:
                ch = w.reg - 0x38
                ym.write(w.reg, w.val)
                if ym.kon[ch] and held_ins[ch] >= 0:
                    pa = w.val
                    held_ramp[ch].append((w.sample, (pa >> 4) & 7, pa & 3))
                continue
            ym.write(w.reg, w.val)
            if 0x28 <= w.reg <= 0x37:
                ch = w.reg & 7
                # KC/KF writes during the release tail still belong to the
                # sweep. The next key-on closes it.
                if sweep_ev[ch] is not None:
                    sweep_pts[ch].append((w.sample, ym.kc[ch], ym.kf[ch]))
            elif 0x60 <= w.reg <= 0x7F:
                # A carrier TL write while the voice is held is a software
                # volume step. Record only steps that move the carrier, or a
                # modulator rewrite (timbre) fakes a fade.
                ch = w.reg & 7
                if ym.kon[ch] and tl_ev[ch] is not None:
                    cur = ym.carrier_tl(ch)
                    if cur != tl_last[ch]:
                        tl_pts[ch].append((w.sample, cur))
                        tl_last[ch] = cur
            continue
        ch = w.val & 7
        mask = (w.val & 0x78) >> 3
        newly_on = mask & ~op_mask[ch]
        op_mask[ch] = mask
        if mask and newly_on:
            pending_off[ch] = None
            _flush_pms_ramp(pms_acc, held_ins[ch], held_ramp[ch], held_kon[ch])
            held_ramp[ch] = []
            close_sweep(ch)
            close_tl(ch)
            ins = intern_patch(ch, mask)
            note = kc_to_furnace_note(ym.kc[ch])
            events.append(NoteEvent(
                sample=w.sample,
                ch=ch,
                on=True,
                note=note,
                ins=ins,
                vol=-1,
                e5=kf_to_e5(ym.kf[ch]) if ym.kf[ch] else 0x80,
                pan=ym.pan_byte(ch),
            ))
            sweep_base[ch] = (ym.kc[ch], ym.kf[ch])
            sweep_pts[ch] = []
            sweep_ev[ch] = len(events) - 1
            tl_pts[ch] = []
            tl_last[ch] = ym.carrier_tl(ch)
            tl_ev[ch] = len(events) - 1
            keyon_times.append(w.sample)
            last_keyon_sample[ch] = w.sample
            ym.kon[ch] = True
            held_ins[ch] = ins
            held_kon[ch] = w.sample
        elif mask:
            # A redundant write or a per-operator change while the note
            # sounds. Every operator stays as it was, and this is not a note
            # event. Treating it as one retriggers the whole note.
            pass
        elif ym.kon[ch]:
            pending_off[ch] = w.sample
            _flush_pms_ramp(pms_acc, held_ins[ch], held_ramp[ch], held_kon[ch])
            held_ramp[ch] = []
            close_tl(ch)
            ym.kon[ch] = False
            held_ins[ch] = -1

    for ch in range(8):
        _flush_pms_ramp(pms_acc, held_ins[ch], held_ramp[ch], held_kon[ch])
        close_sweep(ch)
        close_tl(ch)

    # Key-offs were not recorded above. Re-walk for offs that sit after the
    # last key-on and before the next one.
    ym2 = YM2151State()
    last_on = [-10**9] * 8
    next_on_at: list[list[int]] = [[] for _ in range(8)]
    on_index: dict[tuple[int, int], int] = {}
    for i, ev in enumerate(events):
        if ev.on and not ev.pcm:
            next_on_at[ev.ch].append(ev.sample)
            on_index[(ev.ch, ev.sample)] = i
    cursor = [0] * 8
    offs: list[NoteEvent] = []
    sounding = [False] * 8
    for w in vgm.writes:
        if w.chip != "ym2151":
            continue
        # A mid-note write that clears RL/FB/CON's output bits (regs 0x20-0x27,
        # RL == 0) cuts the channel instantly even with the voices keyed.
        # Without this the note rang for the rest of the track.
        forced_off = (
            w.reg != 0x08
            and 0x20 <= w.reg <= 0x27
            and ((w.val >> 6) & 3) == 0
        )
        if w.reg != 0x08:
            ym2.write(w.reg, w.val)
            if not forced_off:
                continue
        ch = (w.val & 7) if w.reg == 0x08 else (w.reg & 7)
        if not forced_off and w.val & 0x78:
            last_on[ch] = w.sample
            sounding[ch] = True
            continue
        if forced_off and not sounding[ch]:
            continue
        sounding[ch] = False
        # key off
        nxt = None
        idx = cursor[ch]
        times = next_on_at[ch]
        while idx < len(times) and times[idx] <= w.sample:
            idx += 1
        cursor[ch] = idx
        if idx < len(times):
            nxt = times[idx]
        if nxt is not None and nxt - w.sample <= 2:
            continue  # retrigger
        if w.sample - last_on[ch] <= 2:
            continue
        note_i = on_index.get((ch, last_on[ch]))
        if note_i is not None:
            # Note end: the sweep ramp must stop before the next note, since a
            # note-off does not reset a 01xx/02xx slide by itself.
            events[note_i].end = w.sample
        offs.append(NoteEvent(
            sample=w.sample, ch=ch, on=False, note=180,
            ins=-1, vol=-1, e5=0x80, pan=0,
        ))
    events.extend(offs)

    if is_opn:
        states = {cid: YM2203State() for cid in ym_ids}
        held_idx: dict[tuple[int, int, int], int] = {}
        pitch_pts: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        pitch_base: dict[tuple[int, int, int], tuple[int, int]] = {}
        opn_tl_pts: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        opn_tl_last: dict[tuple[int, int, int], int] = {}
        opn_offs: list[int] = []
        opn_lfo = 0
        opn_special = 0

        def opn_event_ch(cid: int, ch: int, op: int = -1) -> int:
            return ym_base[cid] + ch

        # Furnace's ext columns use frequency order (1,2,0,3), parameter order
        # (0,2,1,3), and key mask bit = column.
        ext_freq_col = {op: c for c, op in enumerate(EXT_FREQ_OP)}

        def intern_opn_patch(cid: int, ch: int, mask: int) -> int:
            snap = states[cid].snapshot(ch, mask)
            key = snap.identity() + (mask,)
            if key not in patches:
                snap.name = f"OPN {len(patch_order):02d}"
                patches[key] = snap
                patch_order.append(key)
            p = patches[key]
            p.uses += 1
            return patch_order.index(key)

        def intern_ext_patch(cid: int) -> int:
            snap = states[cid].snapshot(2, 0x0F)
            cache = ext_patch_cache.setdefault(cid, {})
            key0 = (
                snap.alg & 7,
                snap.fb & 7,
                tuple(op.as_tuple(include_tl=False) for op in snap.ops),
            )
            idx = cache.get(key0)
            if idx is not None:
                patches[patch_order[idx]].uses += 1
                return idx
            key = ("ext", cid, key0)
            snap.name = f"OPN {len(patch_order):02d}"
            snap.uses = 1
            patches[key] = snap
            patch_order.append(key)
            idx = len(patch_order) - 1
            cache[key0] = idx
            return idx

        def close_opn_note(
            key: tuple[int, int, int], sample: int, *, off: bool,
        ) -> None:
            idx = held_idx.pop(key, None)
            if idx is None:
                return
            ev = events[idx]
            pts = pitch_pts.pop(key, [])
            if pts:
                # The first write right after the key-on is the driver's real
                # start pitch (some drivers order the key-on before the fnum
                # write). Otherwise the note stays at its key-on snapshot.
                merged: list[tuple[int, int, int]] = []
                for p in pts:
                    if merged and p[0] - merged[-1][0] <= 16:
                        merged[-1] = p
                    else:
                        merged.append(p)
                if merged and merged[0][0] - ev.sample <= 120 and merged[0][1:] != pitch_base[key]:
                    ev.note, ev.e5 = opn_note_for(
                        merged[0][1], merged[0][2], ym_clock
                    )
                    # The note starts at this write; the pre-key-on snapshot
                    # must not re-apply the same step as a pattern effect.
                    merged = [(ev.sample, *merged[0][1:])] + merged[1:]
                    pitch_base[key] = merged[0][1:]
                distinct: list[tuple[int, int, int]] = []
                for p in merged:
                    if not distinct or p[1:] != distinct[-1][1:]:
                        distinct.append(p)
                if len(distinct) > 1:
                    ev.fm_pitch_pts = tuple(distinct)
            tl = opn_tl_pts.pop(key, [])
            if tl:
                ev.tl_pts = tuple(tl)
            if off:
                ev.end = sample
                events.append(NoteEvent(
                    sample=sample,
                    ch=ev.ch,
                    on=False,
                    note=180,
                    ins=-1,
                    vol=-1,
                    e5=0x80,
                    pan=0,
                ))
                opn_offs.append(len(events) - 1)

        ext_active_freqs: dict[int, tuple] = {}
        ext_patch_cache: dict[int, dict[tuple, int]] = {}
        ext_ready: set[int] = set()

        for w in vgm.writes:
            if w.chip != "ym2203":
                continue  # the SSG stream is read by collect_ay8910
            cid = w.chip_id
            st = states.get(cid)
            if st is None:
                continue
            reg, val = w.reg, w.val
            if reg == 0x22 and (val & 0x08):
                opn_lfo += 1
            if reg in (0xA8, 0xA9, 0xAA, 0xAC, 0xAD, 0xAE):
                opn_special += 1
            if reg == 0x27 and (val & 0x40):
                opn_special += 1
            if reg == 0x28:
                ch = val & 0x03
                if ch >= 3:
                    continue
                mask = (val >> 4) & 0x0F
                if ym2203_ext and ch == 2:
                    prev = st.op_mask[ch]
                    if mask:
                        if cid not in ext_ready:
                            if mask != 0x0F:
                                continue
                            ext_ready.add(cid)
                        freqs = tuple(st.frequency_op(ch, h) for h in range(4))
                        active = ext_active_freqs.get(cid)
                        for c in range(4):
                            bit = 1 << c
                            if (prev & bit) and not (mask & bit):
                                close_opn_note((cid, ch, c), w.sample, off=True)
                        emitted = False
                        for c in range(4):
                            bit = 1 << c
                            if not (mask & bit):
                                continue
                            fresh = not (prev & bit) or active is None
                            changed = (
                                active is not None
                                and freqs[EXT_FREQ_OP[c]] != active[EXT_FREQ_OP[c]]
                            )
                            if not (fresh or changed):
                                continue
                            close_opn_note((cid, ch, c), w.sample, off=False)
                            ins = intern_ext_patch(cid)
                            op = EXT_FREQ_OP[c]
                            block, fnum = freqs[op]
                            note, e5 = opn_note_for(block, fnum, ym_clock)
                            events.append(NoteEvent(
                                sample=w.sample,
                                ch=ym_base[cid] + 2 + c,
                                on=True,
                                note=note,
                                ins=ins,
                                vol=127,
                                e5=e5,
                                pan=0,
                                sweep_div=2.0,
                                e5_lo=0x00,
                                e5_hi=0xFF,
                            ))
                            key = (cid, ch, c)
                            held_idx[key] = len(events) - 1
                            pitch_base[key] = (block, fnum)
                            pitch_pts[key] = [(w.sample, block, fnum)]
                            opn_tl_pts[key] = []
                            opn_tl_last[key] = 0x7F
                            emitted = True
                        ext_active_freqs[cid] = freqs
                        if emitted:
                            keyon_times.append(w.sample)
                        st.kon[ch] = True
                    else:
                        for c in range(4):
                            close_opn_note((cid, ch, c), w.sample, off=True)
                        ext_active_freqs.pop(cid, None)
                        st.kon[ch] = False
                    st.op_mask[ch] = mask
                    continue
                newly_on = mask & ~st.op_mask[ch]
                st.op_mask[ch] = mask
                key = (cid, ch, -1)
                if mask and newly_on:
                    close_opn_note(key, w.sample, off=False)
                    block, fnum = st.frequency(ch)
                    note, e5 = opn_note_for(block, fnum, ym_clock)
                    events.append(NoteEvent(
                        sample=w.sample,
                        ch=opn_event_ch(cid, ch, -1),
                        on=True,
                        note=note,
                        ins=intern_opn_patch(cid, ch, mask),
                        vol=-1,
                        e5=e5,
                        pan=0,
                        sweep_div=2.0,
                        e5_lo=0x00,
                        e5_hi=0xFF,
                    ))
                    held_idx[key] = len(events) - 1
                    pitch_base[key] = (block, fnum)
                    pitch_pts[key] = [(w.sample, block, fnum)]
                    opn_tl_pts[key] = []
                    opn_tl_last[key] = st.carrier_tl(ch)
                    keyon_times.append(w.sample)
                    st.kon[ch] = True
                elif not mask and st.kon[ch]:
                    st.kon[ch] = False
                    close_opn_note(key, w.sample, off=True)
                continue
            st.write(reg, val)
            if 0xA0 <= reg <= 0xA6:
                ch = reg & 0x03
            elif 0xA8 <= reg <= 0xAE:
                ch = 2  # channel-3 per-operator frequency
            elif 0x30 <= reg <= 0x9E:
                ch = reg & 0x03
            elif 0xB0 <= reg <= 0xB2:
                ch = reg - 0xB0
            else:
                continue
            if ch >= 3:
                continue
            if ym2203_ext and ch == 2:
                if 0x40 <= reg <= 0x4E:
                    # TL writes on an operator column would need the column's
                    # own baseline for the volume column; the next key-on
                    # picks the new TL up from the instrument copy instead.
                    continue
                if 0xA8 <= reg <= 0xAE:
                    op = reg & 0x03
                    if op < 3:
                        key = (cid, ch, ext_freq_col[op])
                        if key in held_idx:
                            pitch_pts[key].append(
                                (w.sample, *st.frequency_op(ch, op))
                            )
                    continue
                # A normal channel-3 f-num write moves all four operators
                # until the per-operator registers take over.
                if 0xA0 <= reg <= 0xA6:
                    for c in range(4):
                        key = (cid, ch, c)
                        if key in held_idx:
                            pitch_pts[key].append(
                                (w.sample, *st.frequency_op(ch, EXT_FREQ_OP[c]))
                            )
                continue
            key = (cid, ch, -1)
            if key not in held_idx:
                continue
            if 0xA0 <= reg <= 0xA6:
                if ch == 2 and st.special and any(st.special_set):
                    # In 3-slot mode the per-operator registers, not the
                    # normal f-num, are the sounding pitch.
                    continue
                pitch_pts[key].append((w.sample, st.block[ch], st.fnum[ch]))
            elif 0xA8 <= reg <= 0xAE:
                pitch_pts[key].append((w.sample, *st.frequency(2)))
            elif 0x40 <= reg <= 0x4E:
                cur = st.carrier_tl(ch)
                if cur != opn_tl_last.get(key, 0x7F):
                    opn_tl_pts[key].append((w.sample, cur))
                    opn_tl_last[key] = cur
        for key in list(held_idx):
            close_opn_note(key, vgm.total_samples or 0, off=False)

        # A 0x00 that the driver writes immediately before the next key-on is
        # a retrigger, not a rest: keeping the note-off would cut the new
        # note (the writer orders the note-on first at the same row).
        ons_by_ch: dict[int, list[int]] = {}
        for e in events:
            if e.on:
                ons_by_ch.setdefault(e.ch, []).append(e.sample)
        drop = {
            idx for idx in opn_offs
            if any(abs(t - events[idx].sample) <= 2
                   for t in ons_by_ch.get(events[idx].ch, ()))
        }
        if drop:
            events = [e for i, e in enumerate(events) if i not in drop]
        if opn_lfo:
            warnings.append(
                f"{opn_lfo} YM2203 LFO writes are not converted"
            )
        if opn_special and not ym2203_ext:
            warnings.append(
                f"{opn_special} YM2203 channel-3 special-mode writes; "
                "3-slot chords are approximated by one operator"
            )

    if is_opn2:
        states = {cid: YM2612State() for cid in ym2612_ids}
        held_idx: dict[tuple[int, int, int], int] = {}
        pitch_pts: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        pitch_base: dict[tuple[int, int, int], tuple[int, int]] = {}
        opn_tl_pts: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        opn_tl_last: dict[tuple[int, int, int], int] = {}
        opn_offs: list[int] = []
        ext_active_freqs: dict[int, tuple] = {}
        ext_patch_cache: dict[int, dict[tuple, int]] = {}
        ext_ready: set[int] = set()
        dac_writes = 0
        lfo_last: dict[int, int] = {}

        def opn_event_ch(cid: int, ch: int) -> int:
            return ym_base[cid] + ch

        def normalized_patch(snap):
            """Strip the carrier level into a pattern volume value.

            Furnace scales every KVS=2 carrier by ``TL = clamp(127 - vol +
            op.tl)``, so storing the carriers at relative TLs (loudest = 0)
            and putting the absolute level in the volume column reproduces
            the source exactly.  A driver that rewrites carrier TL every note
            otherwise creates one instrument per note and exhausts the 250
            instrument limit.
            """
            carriers = OPN_CARRIERS.get(snap.alg & 7, (3,))
            ops = list(snap.ops)
            tls = [ops[i].tl & 0x7F for i in carriers]
            floor = min(tls) if tls else 0x7F
            for i in carriers:
                ops[i] = replace(ops[i], tl=(ops[i].tl & 0x7F) - floor)
            return replace(snap, ops=tuple(ops)), 127 - floor

        def intern_opn_patch(cid: int, ch: int, mask: int) -> tuple[int, int]:
            snap, vol = normalized_patch(states[cid].snapshot(ch, mask))
            key = snap.identity() + (mask,)
            if key not in patches:
                snap.name = f"OPN {len(patch_order):02d}"
                patches[key] = snap
                patch_order.append(key)
            p = patches[key]
            p.uses += 1
            return patch_order.index(key), vol

        def intern_ext_patch(cid: int) -> int:
            snap = states[cid].snapshot(2, 0x0F)
            cache = ext_patch_cache.setdefault(cid, {})
            key0 = (
                snap.alg & 7,
                snap.fb & 7,
                tuple(op.as_tuple(include_tl=False) for op in snap.ops),
            )
            idx = cache.get(key0)
            if idx is not None:
                patches[patch_order[idx]].uses += 1
                return idx
            key = ("ext", cid, key0)
            snap.name = f"OPN {len(patch_order):02d}"
            snap.uses = 1
            patches[key] = snap
            patch_order.append(key)
            idx = len(patch_order) - 1
            cache[key0] = idx
            return idx

        def close_opn_note(
            key: tuple[int, int, int], sample: int, *, off: bool,
        ) -> None:
            idx = held_idx.pop(key, None)
            if idx is None:
                return
            ev = events[idx]
            pts = pitch_pts.pop(key, [])
            if pts:
                merged: list[tuple[int, int, int]] = []
                for p in pts:
                    if merged and p[0] - merged[-1][0] <= 16:
                        merged[-1] = p
                    else:
                        merged.append(p)
                if merged and merged[0][0] - ev.sample <= 120:
                    if merged[0][1:] != pitch_base[key]:
                        ev.note, ev.e5 = ym2612_note_for(
                            merged[0][1], merged[0][2], ym2612_clock
                        )
                        # The note now starts at this write.  Earlier points
                        # are the pre-key-on snapshot; keeping them would
                        # re-apply this step as a pattern transpose.
                        merged = [(ev.sample, *merged[0][1:])] + merged[1:]
                        pitch_base[key] = merged[0][1:]
                distinct: list[tuple[int, int, int]] = []
                for p in merged:
                    if not distinct or p[1:] != distinct[-1][1:]:
                        distinct.append(p)
                if len(distinct) > 1:
                    ev.fm_pitch_pts = tuple(distinct)
            tl = opn_tl_pts.pop(key, [])
            if tl:
                ev.tl_pts = tuple(tl)
            if off:
                ev.end = sample
                events.append(NoteEvent(
                    sample=sample,
                    ch=ev.ch,
                    on=False,
                    note=180,
                    ins=-1,
                    vol=-1,
                    e5=0x80,
                    pan=0,
                ))
                opn_offs.append(len(events) - 1)

        def chan_freqs(cid: int, ch: int) -> tuple[tuple[int, int], ...]:
            st = states[cid]
            if ym2612_ext and ch == 2:
                return tuple(st.frequency_op(2, g) for g in range(4))
            return (st.frequency(ch),)

        def kon_last(writes):
            """Apply non-KON writes at a sample before its key-on writes.

            Genesis drivers often set the last patch byte (0xB4 pan/LFO is
            common) right after the 0x28 write in the same VGM sample.  The
            hardware sees the old value for a few microseconds; the note that
            actually sounds uses the final value.  Snapshotting at the KON
            instruction picks up the stale patch, so defer the KONs to the
            end of their sample group.
            """
            ordered = []
            i = 0
            while i < len(writes):
                j = i + 1
                while j < len(writes) and writes[j].sample == writes[i].sample:
                    j += 1
                group = writes[i:j]

                def is_kon(w):
                    return (
                        w.chip == "ym2612"
                        and not (w.reg & 0x100)
                        and (w.reg & 0xFF) == 0x28
                    )

                ordered.extend(w for w in group if not is_kon(w))
                ordered.extend(w for w in group if is_kon(w))
                i = j
            return ordered

        for w in kon_last(vgm.writes):
            if w.chip != "ym2612":
                continue
            cid = w.chip_id
            st = states.get(cid)
            if st is None:
                continue
            reg, val = w.reg, w.val
            r = reg & 0xFF
            bank = bool(reg & 0x100)
            if not bank and r == 0x22:
                st.write(reg, val)
                # Furnace's YM2612 effect 10xy: x = enable, y = speed.  The
                # register itself has enable in bit 3.
                fx = (0x10 if val & 0x08 else 0x00) | (val & 0x07)
                if lfo_last.get(cid) != fx:
                    chip_fx.append((w.sample, 0x10, fx))
                    lfo_last[cid] = fx
                continue
            if not bank and r == 0x27:
                st.write(reg, val)
                continue
            if not bank and r == 0x2A:
                st.write(reg, val)
                dac_writes += 1
                continue
            if not bank and r == 0x2B:
                was = st.dac_enabled
                st.write(reg, val)
                if st.dac_enabled and not was:
                    # The DAC replaces channel 6's FM output.
                    close_opn_note((cid, 5, -1), w.sample, off=True)
                continue
            if r == 0x28 and not bank:
                raw_ch = val & 0x07
                if raw_ch in (3, 7):
                    continue
                # 0-2 are channels 1-3 and 4-6 are channels 4-6: the second
                # group continues the 0-based index after a reserved slot.
                ch = raw_ch if raw_ch < 3 else raw_ch - 1
                mask = (val >> 4) & 0x0F
                if st.dac_enabled and ch == 5:
                    # FM6 is replaced by the DAC: its key-ons are the sample
                    # player's writes, not FM notes.
                    if not mask and st.kon[ch]:
                        st.kon[ch] = False
                        close_opn_note((cid, ch, -1), w.sample, off=True)
                    st.op_mask[ch] = mask
                    continue
                if ym2612_ext and ch == 2:
                    prev = st.op_mask[ch]
                    if mask:
                        if cid not in ext_ready:
                            # Before the first full chord the driver is still
                            # initializing the operators.  The first full
                            # chord is the audible one.
                            if mask != 0x0F:
                                st.op_mask[ch] = mask
                                continue
                            ext_ready.add(cid)
                        freqs = tuple(st.frequency_op(2, g) for g in range(4))
                        active = ext_active_freqs.get(cid)
                        for c in range(4):
                            bit = 1 << c
                            if (prev & bit) and not (mask & bit):
                                close_opn_note((cid, ch, c), w.sample, off=True)
                        emitted = False
                        for c in range(4):
                            bit = 1 << c
                            if not (mask & bit):
                                continue
                            fresh = not (prev & bit) or active is None
                            changed = (
                                active is not None
                                and freqs[EXT_FREQ_OP[c]] != active[EXT_FREQ_OP[c]]
                            )
                            if not (fresh or changed):
                                continue
                            close_opn_note((cid, ch, c), w.sample, off=False)
                            ins = intern_ext_patch(cid)
                            group = EXT_FREQ_OP[c]
                            block, fnum = freqs[group]
                            note, e5 = ym2612_note_for(
                                block, fnum, ym2612_clock
                            )
                            events.append(NoteEvent(
                                sample=w.sample,
                                ch=ym_base[cid] + 2 + c,
                                on=True,
                                note=note,
                                ins=ins,
                                vol=127,
                                e5=e5,
                                pan=st.pan_byte(2),
                                sweep_div=2.0,
                                e5_lo=0x00,
                                e5_hi=0xFF,
                            ))
                            key = (cid, ch, c)
                            held_idx[key] = len(events) - 1
                            pitch_base[key] = (block, fnum)
                            pitch_pts[key] = [(w.sample, block, fnum)]
                            opn_tl_pts[key] = []
                            opn_tl_last[key] = 0x7F
                            emitted = True
                        ext_active_freqs[cid] = freqs
                        if emitted:
                            keyon_times.append(w.sample)
                        st.kon[ch] = True
                    else:
                        for c in range(4):
                            close_opn_note((cid, ch, c), w.sample, off=True)
                        ext_active_freqs.pop(cid, None)
                        st.kon[ch] = False
                    st.op_mask[ch] = mask
                    continue
                prev = st.op_mask[ch]
                newly_on = mask & ~prev
                st.op_mask[ch] = mask
                key = (cid, ch, -1)
                if mask and newly_on:
                    close_opn_note(key, w.sample, off=False)
                    block, fnum = st.frequency(ch)
                    note, e5 = ym2612_note_for(block, fnum, ym2612_clock)
                    ins, vol = intern_opn_patch(cid, ch, mask)
                    events.append(NoteEvent(
                        sample=w.sample,
                        ch=opn_event_ch(cid, ch),
                        on=True,
                        note=note,
                        ins=ins,
                        vol=vol,
                        e5=e5,
                        pan=st.pan_byte(ch),
                        sweep_div=2.0,
                        e5_lo=0x00,
                        e5_hi=0xFF,
                    ))
                    held_idx[key] = len(events) - 1
                    pitch_base[key] = (block, fnum)
                    pitch_pts[key] = [(w.sample, block, fnum)]
                    opn_tl_pts[key] = []
                    opn_tl_last[key] = st.carrier_tl(ch)
                    keyon_times.append(w.sample)
                    st.kon[ch] = True
                elif not mask and st.kon[ch]:
                    st.kon[ch] = False
                    close_opn_note(key, w.sample, off=True)
                continue

            # A passive register write.  The state applies the chip's own
            # latches; the note's trajectory records whatever the write
            # actually committed.
            ch = -1
            valid = True
            if 0x30 <= r <= 0x9E:
                valid = (r & 0x03) != 3
                ch = (r & 0x03) + (3 if bank else 0)
            elif 0xA0 <= r <= 0xA6:
                valid = (r & 0x03) != 3
                ch = (r & 0x03) + (3 if bank else 0)
            elif not bank and 0xA8 <= r <= 0xAE:
                valid = (r & 0x03) != 3
                ch = 2
            elif 0xB0 <= r <= 0xB2:
                ch = (r - 0xB0) + (3 if bank else 0)
            elif 0xB4 <= r <= 0xB6:
                ch = (r - 0xB4) + (3 if bank else 0)
            if not valid or ch < 0 or ch >= 6:
                st.write(reg, val)
                continue

            before = chan_freqs(cid, ch)
            b4 = st.pan_pms_ams[ch]
            st.write(reg, val)
            after = chan_freqs(cid, ch)

            if ym2612_ext and ch == 2:
                for c in range(4):
                    key = (cid, ch, c)
                    group = EXT_FREQ_OP[c]
                    if key in held_idx and after[group] != before[group]:
                        pitch_pts[key].append((w.sample, *after[group]))
                # TL writes on an operator column: the next key-on picks the
                # new level up from the instrument copy (as on the YM2203).
            else:
                key = (cid, ch, -1)
                if key in held_idx and after[0] != before[0]:
                    pitch_pts[key].append((w.sample, *after[0]))
                if 0x40 <= r <= 0x4E and key in held_idx:
                    cur = st.carrier_tl(ch)
                    if cur != opn_tl_last.get(key, 0x7F):
                        opn_tl_pts[key].append((w.sample, cur))
                        opn_tl_last[key] = cur
            if 0xB4 <= r <= 0xB6:
                # Per-channel pan / PMS / AMS: snapshot at key-on, and write
                # a channel effect when the driver moves it during a note.
                new_b4 = st.pan_pms_ams[ch]
                if new_b4 != b4:
                    target = (
                        ym_base[cid] + 2 if (ym2612_ext and ch == 2)
                        else ym_base[cid] + ch
                    )
                    pan = st.pan_byte(ch)
                    if (b4 ^ new_b4) & 0xC0:
                        channel_fx.append((w.sample, target, 0x08, pan))
                    if (b4 ^ new_b4) & 0x30:
                        channel_fx.append(
                            (w.sample, target, 0x63, (new_b4 >> 4) & 0x03)
                        )
                    if (b4 ^ new_b4) & 0x07:
                        channel_fx.append(
                            (w.sample, target, 0x62, new_b4 & 0x07)
                        )

        for key in list(held_idx):
            close_opn_note(key, vgm.total_samples or 0, off=False)

        # A 0x00 written immediately before the next key-on is a retrigger.
        ons_by_ch: dict[int, list[int]] = {}
        for e in events:
            if e.on:
                ons_by_ch.setdefault(e.ch, []).append(e.sample)
        drop = {
            idx for idx in opn_offs
            if any(abs(t - events[idx].sample) <= 2
                   for t in ons_by_ch.get(events[idx].ch, ()))
        }
        if drop:
            events = [e for i, e in enumerate(events) if i not in drop]
        if ym2612_csm:
            warnings.append(
                "YM2612 CSM mode: per-operator frequencies are converted, "
                "but timer-generated key-ons are not synthesized"
            )

    # Collect DAC runs now; group them after the row grid is known.
    dac_samples: list[DacSample] = []
    dac_plays: list[DacPlay] = []
    dac_count = 0
    dac_ch = -1
    dac_stats: dict = {}
    if pcm and is_opn2:
        dac_samples, dac_plays, dac_warn, dac_stats = collect_dac(
            vgm, slices=dac_slices,
        )
        warnings.extend(dac_warn)
        dac_ch = ym_base.get(ym2612_ids[0], 0) + (8 if ym2612_ext else 5)
        if (
            dac_writes and not dac_plays and not dac_warn
            and not dac_stats.get("dac_disabled_bytes")
        ):
            warnings.append(
                f"{dac_writes} YM2612 DAC writes but no playable DAC data"
            )

    # Genesis PSG.  The SN76489 has no key-on, so the collector gates on its
    # 4-bit attenuation and keeps mid-note period/volume writes as macros.
    psg_count = 0
    if psg_base is not None:
        psg_hits, psg_offs, psg_warn = collect_sn76489(vgm, psg_clock)
        warnings.extend(psg_warn)
        psg_events: list[NoteEvent] = []
        for h in psg_hits:
            ch = psg_base + h.voice
            if h.noise:
                spec = h.noise_mode & 3
                # The SMS noise channel's note selects the fixed-rate preset;
                # mode 3 takes its period from PSG channel 3.
                note = 60 + (spec if spec < 3 else 0)
                e5 = 0x80
                fmode = ((h.noise_mode >> 2) & 1) | (0x10 if spec == 3 else 0)
                channel_fx.append((h.sample_time, ch, 0x20, fmode))
                for t, mode in h.mode_pts:
                    mf = ((mode >> 2) & 1) | (0x10 if (mode & 3) == 3 else 0)
                    channel_fx.append((t, ch, 0x20, mf))
                pitch: tuple[tuple[int, int], ...] = ()
            else:
                note, e5 = psg_note(h.period)
                pitch = tuple(h.pitch_pts)
            psg_events.append(NoteEvent(
                sample=h.sample_time,
                ch=ch,
                on=True,
                note=note,
                ins=-1,
                vol=max(0, min(15, h.vol)),
                e5=e5,
                pan=0,
                end=h.off_sample,
                vol_pts=tuple(
                    (t, max(0, min(15, v)), None) for t, v in h.vol_pts
                ),
                tone_pitch_pts=pitch,
                tone_chip=3,
                tone_clock=psg_clock,
                tone_div=PSG_DIVIDER,
                tone_period_offset=0,
            ))
            psg_count += 1
        for off in psg_offs:
            psg_events.append(NoteEvent(
                sample=off.sample_time,
                ch=psg_base + off.voice,
                on=False,
                note=180,
                ins=-1,
                vol=-1,
                e5=0x80,
                pan=0,
            ))
        psg_events.sort(key=lambda e: (e.sample, 0 if not e.on else 1))
        events.extend(psg_events)

    # Row 0 is the start of the VGM, not the first key-on. Loop points in the
    # header are sample offsets from t=0; shifting t0 would move Bxx.
    t0 = 0
    # Chosen below, after every chip has been read.
    grid_note = ""
    row_subdiv = 1

    rom, pcm_samples = build_pcm_bank(vgm)
    # DAC maps are inserted after the row grid; rebase non-FM instruments once.
    n_dac_ins = 0
    pcm_ins_base = len(patch_order) + n_dac_ins
    if pcm:
        clock = vgm.k007232_clock or 3579545
        # Instruments are written only for samples long enough to hear, so the
        # pattern index has to count those, not the raw bank slot.
        audible = [s for s in pcm_samples if s.length >= 8]
        audible_index = {id(s): i for i, s in enumerate(audible)}
        # The register is 8-bit and linear, but a few drivers treat it as a
        # 0-15 preset. Decide from the loudest write in the whole file.
        peak_raw = max(
            (max(h.vol_l, h.vol_r) for s in audible for h in s.hits), default=0
        )
        vol_scale = 255.0 if peak_raw > 16 else 15.0
        for smp in audible:
            c4p = smp.mode_pitch()
            ins = pcm_ins_base + audible_index[id(smp)]
            for h in smp.hits:
                note, e5 = pitch_to_note(h.pitch, c4p, clock)
                vl = vol15(h.vol_l, vol_scale)
                vr = vol15(h.vol_r, vol_scale)
                vol = max(vl, vr)
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=fm_end + h.ch,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=vol if vol else 15,
                    e5=e5,
                    pan=pcm_pan_fx(vl, vr),
                    pcm=True,
                ))
        if not rom and any(smp.length >= 8 and smp.hits for smp in pcm_samples):
            warnings.append("no K007232 ROM block (0x94); PCM notes will be silent until samples are added")
    else:
        pcm_samples = []

    oki_samples: list[OkiSample] = []
    adpcm_samples: list[AdpcmSample] = []
    spcm_audible: list[SegapcmSample] = []
    c140_audible: list[C140Sample] = []
    c352_audible: list[C352Sample] = []
    # Set where the C352 runs and the source drives its rear pair.
    c352_quad_events = False
    if pcm:
        oki_samples, oki_warn = collect_oki(vgm)
        warnings.extend(oki_warn)
        adpcm_samples, adpcm_warn = collect_msm6258(vgm)
        warnings.extend(adpcm_warn)
        # Sample chips sit after the 8 YM2151 channels, and after the two
        # K007232 channels when the bank has audible samples: K007232, then
        # each MSM6295 (4 voices), then each MSM6258.
        sample_base = fm_end + (2 if audible else 0)
        oki_ids: list[int] = []
        for smp in oki_samples:
            if smp.chip_id not in oki_ids:
                oki_ids.append(smp.chip_id)
        oki_base = {cid: sample_base + 4 * i for i, cid in enumerate(oki_ids)}
        ins0 = pcm_ins_base + (len(audible) if pcm else 0)
        for i, smp in enumerate(oki_samples):
            ins = ins0 + i
            dur = len(smp.data) * 2 / smp.rate * VGM_RATE
            for h in smp.hits:
                ch = oki_base[smp.chip_id] + h.voice
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=ch,
                    on=True,
                    note=FURNACE_C4,
                    ins=ins,
                    vol=h.vol,
                    e5=0x80,
                    pan=0,
                    pcm=True,
                    no_pan=True,
                ))
                if h.end and h.end + 64 < h.sample_time + dur:
                    events.append(NoteEvent(
                        sample=h.end,
                        ch=ch,
                        on=False,
                        note=180,
                        ins=-1,
                        vol=-1,
                        e5=0x80,
                        pan=0,
                        pcm=True,
                        no_pan=True,
                    ))
        adpcm_base = sample_base + 4 * len(oki_ids)
        adpcm_ids: list[int] = []
        for smp in adpcm_samples:
            if smp.chip_id not in adpcm_ids:
                adpcm_ids.append(smp.chip_id)
        adpcm_at = {cid: adpcm_base + i for i, cid in enumerate(adpcm_ids)}
        ins1 = ins0 + len(oki_samples)
        div_done: set[int] = set()
        for i, smp in enumerate(adpcm_samples):
            ins = ins1 + i
            ch = adpcm_at[smp.chip_id]
            fx = msm6258_div_fx(smp.divider)
            if fx is not None and smp.chip_id not in div_done:
                div_done.add(smp.chip_id)
                channel_fx.append((0, ch, 0x20, fx))
            for h in smp.hits:
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=ch,
                    on=True,
                    note=FURNACE_C4,
                    ins=ins,
                    vol=8,
                    e5=0x80,
                    pan=0,
                    pcm=True,
                    no_pan=True,
                ))
                if h.end and h.end > h.sample_time:
                    events.append(NoteEvent(
                        sample=h.end,
                        ch=ch,
                        on=False,
                        note=180,
                        ins=-1,
                        vol=-1,
                        e5=0x80,
                        pan=0,
                        pcm=True,
                        no_pan=True,
                    ))

        # SegaPCM: 16 voices, one .fur channel each, rate = clock*freq/32768.
        spcm_clock = vgm.segapcm_clock or 4000000
        _spcm_rom, spcm_samples, spcm_warn = collect_segapcm(vgm)
        warnings.extend(spcm_warn)
        spcm_audible = [s for s in spcm_samples if s.length >= 8]
        spcm_base = adpcm_base + len(adpcm_ids)
        ins2 = ins1 + len(adpcm_samples)
        spcm_peak = max(
            (max(h.vol_l, h.vol_r) for s in spcm_audible for h in s.hits), default=0
        )
        spcm_scale = 127.0 if spcm_peak > 16 else 16.0
        for i, smp in enumerate(spcm_audible):
            ins = ins2 + i
            ref = segapcm_rate(spcm_clock, smp.mode_freq())
            for h in smp.hits:
                note, e5 = rate_to_note(segapcm_rate(spcm_clock, h.freq), ref)
                vl = vol15(h.vol_l, spcm_scale)
                vr = vol15(h.vol_r, spcm_scale)
                # Furnace passes the pattern volume through to the chip, whose
                # register is 7-bit: use the register value's own range.
                vol = max(h.vol_l, h.vol_r) & 0x7F
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=spcm_base + h.voice,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=vol if vol else 15,
                    e5=e5,
                    pan=pcm_pan_fx(vl, vr),
                    pcm=True,
                ))

        # Namco C140: 24 voices per instance, rate = clock*freq/32768 words.
        c140_clock = vgm.c140_clock or 12288000
        _c140_rom, c140_samples, c140_offs, c140_warn = collect_c140(vgm)
        warnings.extend(c140_warn)
        c140_audible = [s for s in c140_samples if s.length >= 8]
        c140_ids: list[int] = []
        for smp in c140_audible:
            for h in smp.hits:
                if h.chip_id not in c140_ids:
                    c140_ids.append(h.chip_id)
        c140_base = spcm_base + (16 if spcm_audible else 0)
        c140_at = {cid: c140_base + 24 * i for i, cid in enumerate(c140_ids)}
        ins3 = ins2 + len(spcm_audible)
        c140_peak = max(
            (max(h.vol_l, h.vol_r) for s in c140_audible for h in s.hits), default=0
        )
        c140_scale = 255.0 if c140_peak > 16 else 16.0
        for i, smp in enumerate(c140_audible):
            ins = ins3 + i
            ref = c140_rate(c140_clock, smp.mode_freq())
            for h in smp.hits:
                base_f = hit_played_freq(h)
                steps = merge_step_writes(h.freq_pts)
                note, e5 = rate_to_note(c140_rate(c140_clock, base_f), ref)
                sweep: tuple[tuple[int, int], ...] = ()
                if steps and base_f:
                    deltas = tuple(
                        (t, int(round(64.0 * 12.0 * math.log2(f / base_f))))
                        for t, f in steps
                        if f
                    )
                    vals = [d for _t, d in deltas]
                    if deltas and max(vals + [0]) - min(vals + [0]) >= 4:
                        sweep = deltas
                vl = vol15(h.vol_l, c140_scale)
                vr = vol15(h.vol_r, c140_scale)
                # C140 volume registers are 8-bit and Furnace passes the column
                # straight through (platform/c140.cpp chVol = outVol*pan/255).
                vol = max(h.vol_l, h.vol_r) & 0xFF
                pts = tuple(
                    (t, max(a, b) & 0xFF,
                     pcm_pan_fx(vol15(a, c140_scale), vol15(b, c140_scale))
                     if max(a, b) else None)
                    for t, a, b in h.vol_pts
                )
                # A hit keyed with both registers at zero is a fade-in or a
                # mute. Start silent on Furnace's default balance. 08 00
                # would mute the voice forever.
                fade_in = vol == 0 and any(v > 0 for _t, v, _p in pts)
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=c140_at.get(h.chip_id, c140_base) + h.voice,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=(0 if fade_in else vol if vol else 15),
                    e5=e5,
                    pan=0 if fade_in else pcm_pan_fx(vl, vr),
                    pcm=True,
                    end=h.off_sample,
                    sweep=sweep,
                    vol_pts=pts,
                    no_pan=fade_in,
                ))
        # Chip key-offs stop the voice; without a note-off a looping sample
        # would ring on until the next note on that voice.
        for off in c140_offs:
            base = c140_at.get(off.chip_id)
            if base is None:
                continue
            events.append(NoteEvent(
                sample=off.sample_time,
                ch=base + off.voice,
                on=False,
                note=180,
                ins=-1,
                vol=-1,
                e5=0x80,
                pan=0,
                pcm=True,
            ))

        # Namco C352: 32 voices per instance. Rate is clock*freq/(288*65536).
        # The pattern stores the target volume registers; the chip slews there.
        c352_clock = vgm.c352_clock or C352_DEFAULT_CLOCK
        _c352_rom, c352_samples, c352_offs, c352_warn = collect_c352(
            vgm, quad=c352_quad,
        )
        warnings.extend(c352_warn)
        c352_audible = [s for s in c352_samples if s.length >= 8]
        # The quad representation only earns its keep where the source drives
        # the rear pair. A track that leaves register 1 silent keeps the plain
        # stereo fold it has always had, byte for byte.
        c352_quad_events = c352_quad and any(
            h.rear_l or h.rear_r or any(pt[3] or pt[4] for pt in h.quad_pts)
            for s in c352_audible for h in s.hits
        )
        c352_ids: list[int] = []
        for smp in c352_audible:
            for h in smp.hits:
                if h.chip_id not in c352_ids:
                    c352_ids.append(h.chip_id)
        c352_base = c140_base + 24 * len(c140_ids)
        c352_at = {cid: c352_base + 32 * i for i, cid in enumerate(c352_ids)}
        ins4 = ins3 + len(c140_audible)
        c352_peak = max(
            (max(h.vol_l, h.vol_r, *(max(a, b) for _t, a, b in h.vol_pts))
             for s in c352_audible for h in s.hits), default=0
        )
        c352_scale = 255.0 if c352_peak > 16 else 16.0
        for i, smp in enumerate(c352_audible):
            ins = ins4 + i
            ref = c352_rate(c352_clock, smp.mode_freq())
            for h in smp.hits:
                base_f = c352_played_freq(h)
                steps = c352_merge_steps(h.freq_pts)
                note, e5 = rate_to_note(c352_rate(c352_clock, base_f), ref)
                sweep: tuple[tuple[int, int], ...] = ()
                if steps and base_f:
                    deltas = tuple(
                        (t, int(round(64.0 * 12.0 * math.log2(f / base_f))))
                        for t, f in steps
                        if f
                    )
                    vals = [d for _t, d in deltas]
                    if deltas and max(vals + [0]) - min(vals + [0]) >= 4:
                        sweep = deltas
                # The driver strobes first and writes the note's volume in
                # the same frame. The first in-frame step is the note's
                # balance. The strobe value is stale.
                init_l, init_r = h.vol_l, h.vol_r
                for _t, a, b in h.vol_pts:
                    if _t - h.sample_time > C352_REFIT_SAMPLES:
                        break
                    init_l, init_r = a, b
                vl = vol15(init_l, c352_scale)
                vr = vol15(init_r, c352_scale)
                vol = max(init_l, init_r) & 0xFF
                pts = tuple(
                    (t, max(a, b) & 0xFF,
                     pcm_pan_fx(vol15(a, c352_scale), vol15(b, c352_scale))
                     if max(a, b) else None)
                    for t, a, b in h.vol_pts
                )
                fade_in = vol == 0 and any(v > 0 for _t, v, _p in pts)
                quad = None
                quad_pts: tuple[tuple[int, int, int, int, int], ...] = ()
                if c352_quad_events:
                    # The same refit, on the four registers instead of the
                    # folded pair: the note's own balance is the first write
                    # inside its frame, not the stale one at the strobe.
                    four = (h.front_l, h.front_r, h.rear_l, h.rear_r)
                    for pt in h.quad_pts:
                        if pt[0] - h.sample_time > C352_REFIT_SAMPLES:
                            break
                        four = pt[1:]
                    quad = four
                    quad_pts = tuple(h.quad_pts)
                    # The level rides on the four registers, so the volume
                    # column is held wide open and every later step is one of
                    # the four pans, not a column change.
                    vol = 255
                    pts = ()
                    fade_in = False
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=c352_at.get(h.chip_id, c352_base) + h.voice,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=(0 if fade_in else vol if vol else 15),
                    e5=e5,
                    pan=0 if fade_in else pcm_pan_fx(vl, vr),
                    pcm=True,
                    end=h.off_sample,
                    sweep=sweep,
                    vol_pts=pts,
                    no_pan=fade_in,
                    quad=quad,
                    quad_pts=quad_pts,
                ))
        for off in c352_offs:
            base = c352_at.get(off.chip_id)
            if base is None:
                continue
            events.append(NoteEvent(
                sample=off.sample_time,
                ch=base + off.voice,
                on=False,
                note=180,
                ins=-1,
                vol=-1,
                e5=0x80,
                pan=0,
                pcm=True,
            ))
    else:
        audible = []

    # SCC and AY are pitched chips and convert even under --melody-only. Their
    # channels follow the FM block and every sample chip.
    tone_base = (
        fm_end + (2 if audible else 0) + 4 * len(oki_samples) + len(adpcm_samples)
        + (16 if spcm_audible else 0) + 24 * len(c140_audible) + 32 * len(c352_audible)
    )
    # Count the final DAC maps once, below; the collector's list is transient.
    tone_ins_base = (
        len(patch_order)
        + (len(audible) if pcm else 0) + len(oki_samples)
        + len(adpcm_samples) + len(spcm_audible) + len(c140_audible) + len(c352_audible)
    )
    scc_ids: list[int] = []
    scc_waves: list[bytes] = []
    scc_ins: list[ToneIns] = []
    ay_ids: list[int] = []
    ay_ins: list[ToneIns] = []
    psg_ins: list[ToneIns] = []
    scc_count = 0
    ay_count = 0
    scc_notes, scc_offs, scc_wave_list, scc_warn = collect_scc(vgm)
    warnings.extend(scc_warn)
    if scc_notes:
        for cid in sorted({n.hit.chip_id for n in scc_notes}):
            scc_ids.append(cid)
        scc_waves = list(scc_wave_list)
        scc_wave_index = {img: i for i, img in enumerate(scc_waves)}
        scc_at = {cid: tone_base + SCC_VOICES * i for i, cid in enumerate(scc_ids)}
        scc_clock = vgm.k051649_clock or SCC_DEFAULT_CLOCK
        for n in scc_notes:
            h = n.hit
            ch = scc_at.get(h.chip_id)
            if ch is None:
                continue
            if h.period <= SCC_MIN_PERIOD or not any(h.wave):
                # The chip halts a voice below period 9, and a waveform that
                # was never written is silence: no note. The key-off list
                # below carries the gate's close.
                continue
            gate_end = h.off_sample or (vgm.total_samples or h.sample_time)
            segs = segment_periods(
                [(h.sample_time, h.period), *h.freq_pts],
                gate_end, scc_clock, 16, 1,
            )
            for i, (st, period, intra) in enumerate(segs):
                nxt = segs[i + 1][0] if i + 1 < len(segs) else 0
                seg_end = nxt or h.off_sample
                wave = _value_at(h.wave_pts, st, h.wave)
                if period <= SCC_MIN_PERIOD or not any(wave):
                    # A rest the driver encoded as a halted period. The note
                    # that was sounding has to stop, or it holds the old pitch
                    # across the hole.
                    events.append(NoteEvent(
                        sample=st, ch=ch + h.voice, on=False, note=180,
                        ins=-1, vol=-1, e5=0x80, pan=0,
                    ))
                    continue
                bound = nxt or gate_end
                vol = _value_at(h.vol_pts, st, h.vol)
                vol_pts = tuple(
                    (t, v, None) for t, v in h.vol_pts if st < t < bound
                )
                steps = _tone_steps(intra, st, SCC_REFIT_SAMPLES if i == 0 else 0)
                wave_steps: list[tuple[int, int]] = []
                for t, img in merge_step_writes(
                    [(t, img) for t, img in h.wave_pts if st < t < bound], gap=64,
                ):
                    idx = scc_wave_index.get(img)
                    if idx is None:
                        idx = len(scc_waves)
                        scc_wave_index[img] = idx
                        scc_waves.append(img)
                    if wave_steps and wave_steps[-1][1] == idx:
                        continue
                    wave_steps.append((t, idx))
                wave_index = scc_wave_index.get(wave)
                if wave_index is None:
                    wave_index = len(scc_waves)
                    scc_wave_index[wave] = wave_index
                    scc_waves.append(wave)
                note, e5 = scc_note(scc_clock, period)
                events.append(NoteEvent(
                    sample=st,
                    ch=ch + h.voice,
                    on=True,
                    note=note,
                    ins=tone_ins_base,
                    vol=vol,
                    e5=e5,
                    pan=0,
                    end=seg_end,
                    vol_pts=vol_pts,
                    tone_pitch_pts=tuple(steps),
                    tone_wave_pts=tuple(wave_steps),
                    tone_wave=wave_index,
                    tone_period_offset=1,
                    tone_chip=1,
                ))
                scc_count += 1
        for off in scc_offs:
            base = scc_at.get(off.chip_id)
            if base is None:
                continue
            events.append(NoteEvent(
                sample=off.sample_time, ch=base + off.voice, on=False, note=180,
                ins=-1, vol=-1, e5=0x80, pan=0,
            ))

    ay_hits, ay_offs, ay_warn = collect_ay8910(vgm)
    warnings.extend(ay_warn)
    if ay_hits:
        for cid in sorted({h.chip_id for h in ay_hits}):
            ay_ids.append(cid)
        ay_at = {cid: tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * i
                 for i, cid in enumerate(ay_ids)}
        ay_clock = vgm.ay8910_clock or AY_DEFAULT_CLOCK
        ay_count = _emit_ay_notes(
            events, channel_fx, ay_hits, ay_offs, ay_at, ay_clock, 8,
            tone_ins_base + len(scc_ins), vgm.total_samples or 0,
        )

    # The YM2203 SSG rides in the same six-channel system: 0-2 FM, 3-5 PSG. Its
    # notes share the AY instrument list.
    ssg_hits, ssg_offs, ssg_warn = collect_ay8910(
        vgm, chip="ym2203_ssg", clock=ym_clock, divider=YM2203_SSG_DIVIDER,
    )
    warnings.extend(ssg_warn)
    if ssg_hits and ym_ids:
        ssg_at = {
            cid: ym_base[cid] + (6 if ym2203_ext else 3) for cid in ym_ids
        }
        ym2203_ssg_count = _emit_ay_notes(
            events, channel_fx, ssg_hits, ssg_offs, ssg_at, ym_clock,
            YM2203_SSG_DIVIDER, tone_ins_base + len(scc_ins),
            vgm.total_samples or 0,
        )
        # The SSG voices share the AY instrument path; count them with the
        # other PSG notes so the placeholder logic and reports see them.
        ay_count += ym2203_ssg_count

    events.sort(key=lambda e: (e.sample, e.ch, 0 if e.on else 1))

    # Prefer the measured lattice: EDxx places events at their true write
    # time. Manual --speed keeps the legacy 60 Hz path. A rip with no FM
    # stream is measured from its sample-chip triggers.
    pcm_onsets: dict[int, list[int]] = {}
    for e in events:
        # Every non-FM note-on counts as a rhythm marker, sample chips and the
        # SCC/AY tone channels alike, so a rip without FM still gets a grid.
        if e.on and (e.pcm or e.ch >= 8):
            pcm_onsets.setdefault(e.ch, []).append(e.sample)
    for ch, times in pcm_onsets.items():
        merged: list[int] = []
        for t in times:
            if merged and t - merged[-1] <= _ONSET_MERGE_SAMPLES:
                continue
            merged.append(t)
        pcm_onsets[ch] = merged

    tone_hilight = False
    if speed and speed >= 1:
        hz = 60.0
        use_speed = speed
    else:
        def _grid(kind: str, **kw):
            """The grid estimate for the track, memoised across factors"""
            key = (kind, tick, row_floor)
            if grid_cache is None or key not in grid_cache:
                found = estimate_grid(vgm, tick_samples=tick, min_row=row_floor, **kw)
                if grid_cache is not None:
                    grid_cache[key] = found
                return found
            return grid_cache[key]

        grid = _grid("fm")
        if grid is None and pcm_onsets:
            grid = _grid("pcm", onsets=pcm_onsets, label="sample-trigger lattice")
        if grid is not None:
            tone_only = (scc_count or ay_count) and not keyon_times and not any(
                e.on and e.pcm for e in events
            )
            if tone_only:
                hz, use_speed, row_subdiv, grid_note = _musical_row(grid)
                hz, use_speed, row_subdiv, grid_note = _fit_tone_subdivision(
                    hz, use_speed, events, grid_note,
                )
                tone_hilight = True
            else:
                hz = grid.hz
                use_speed = grid.speed
                row_subdiv = grid.subdiv
                grid_note = (
                    f"{grid.source}, {grid.confidence * 100:.0f}% within 6%; "
                    f"row = 1/{grid.subdiv} of the measured 16th, "
                    f"{grid.speed} ticks/row"
                )
        else:
            hz = 60.0
            use_speed = suggest_speed(keyon_times, hz)
            grid_note = "60 Hz fallback"

    tick_scale = _tone_pitch_tick_scale(vgm, hz, use_speed)
    if tick_scale > 1:
        # Keep rows (and the BPM) where they are; only the tick gets finer.
        hz *= tick_scale
        use_speed = min(255, use_speed * tick_scale)
        grid_note = (
            f"{grid_note}; " if grid_note else ""
        ) + f"tone pitch tick x{tick_scale}"

    pcm_gaps: list[int] = []
    if spcm_audible:
        by_voice: dict[int, list[int]] = {}
        for smp in spcm_audible:
            for h in smp.hits:
                by_voice.setdefault(h.voice, []).append(h.sample_time)
        for times in by_voice.values():
            times.sort()
            pcm_gaps.extend(b - a for a, b in zip(times, times[1:]) if b > a)
    if c352_audible:
        by_c352: dict[tuple[int, int], list[int]] = {}
        for smp in c352_audible:
            for h in smp.hits:
                by_c352.setdefault((h.chip_id, h.voice), []).append(h.sample_time)
        for times in by_c352.values():
            times.sort()
            pcm_gaps.extend(b - a for a, b in zip(times, times[1:]) if b > a)
    if pcm_gaps:
        extent = vgm.total_samples or max((e.sample for e in events), default=0)
        new_hz, new_speed, new_sub, pcm_note = refine_pcm_grid(
            hz, use_speed, row_subdiv, extent, pcm_gaps, tick_samples=tick,
        )
        if pcm_note:
            hz, use_speed, row_subdiv = new_hz, new_speed, new_sub
            grid_note = f"{grid_note}; {pcm_note}" if grid_note else pcm_note

    # Condensing lengthens rows by `condense`, dropping the row rate and BPM.
    # It runs last, and PCM restarts that no longer fit show up as writer loss.
    if condense > 1:
        hz, use_speed, row_subdiv = condense_row(
            hz, use_speed, row_subdiv, condense, tick,
        )
        grid_note = f"{grid_note}; condensed x{condense:g}" if grid_note else f"condensed x{condense:g}"

    # Group DAC runs after the row grid; insert their maps before tone instruments.
    dac_pan_events: list[tuple[int, int]] = []
    if dac_plays and dac_ch >= 0:
        dac_base = len(patch_order)
        (
            dac_samples, dac_notes, dac_pan_events,
            dac_group_warn, dac_group_stats,
        ) = group_dac_plays(
            dac_samples, dac_plays, hz=hz, speed=use_speed, t0=t0,
            total_samples=vgm.total_samples or 0,
        )
        dac_stats.update(dac_group_stats)
        warnings.extend(dac_group_warn)
        n_dac_ins = (len(dac_samples) + 119) // 120
        if n_dac_ins:
            _rebase_non_fm_ins(events, dac_base, dac_base + n_dac_ins)
            tone_ins_base += n_dac_ins
        for placed, sample_idx, pan in dac_notes:
            events.append(NoteEvent(
                sample=placed,
                ch=dac_ch,
                on=True,
                note=60 + sample_idx % 120,
                ins=dac_base + sample_idx // 120,
                vol=127,
                e5=0x80,
                pan=pan,
                dac=True,
            ))
        for placed, pan in dac_pan_events:
            channel_fx.append((placed, dac_ch, 0x08, pan))
        events.sort(key=lambda e: (e.sample, e.ch, 0 if e.on else 1))
        dac_count = len(dac_notes)

    hilight_a, hilight_b = 4, 16
    if tone_hilight and row_subdiv >= 1:
        hilight_a = min(255, max(1, int(round(4 * row_subdiv))))
        hilight_b = min(255, max(hilight_a, int(round(16 * row_subdiv))))

    # YM2203 FM pitch motion is an instrument macro because it advances once
    # per tick. It adds instruments. Rebind the tone-chip base before the
    # tone events.
    foreign_ins = (
        n_dac_ins
        + (len(audible) if pcm else 0) + len(oki_samples) + len(adpcm_samples)
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
    )
    fm_pitch_clock = ym2612_clock if is_opn2 else ym_clock
    fm_before_pitch = len(patch_order)
    fm_pitch_copies, fm_pitch_lost = _bind_fm_pitch(
        events, patches, patch_order, fm_pitch_clock, hz, vgm.total_samples or 0,
        cap=max(len(patch_order), 250 - foreign_ins - 24),
        allow_steps=is_opn2,
    )
    _rebase_non_fm_ins(events, fm_before_pitch, len(patch_order))
    if fm_pitch_copies:
        # Keep the DAC maps when recomputing after pitch copies.
        tone_ins_base = (
            len(patch_order) + n_dac_ins
            + (len(audible) if pcm else 0) + len(oki_samples)
            + len(adpcm_samples) + len(spcm_audible) + len(c140_audible)
            + len(c352_audible)
        )
    if fm_pitch_lost:
        warnings.append(
            f"{fm_pitch_lost} {'YM2612' if is_opn2 else 'YM2203'} pitch sweeps "
            "encoded as row ramps: instrument limit"
        )

    # Tone-chip instruments, after the row rate is final. A macro advances
    # once per tick, so building it before condense (or before the musical
    # row is chosen) plays the driver's trajectory at the wrong speed.
    _bind_tone_instruments(
        events, scc_ins, ay_ins, psg_ins, tone_ins_base,
        vgm.k051649_clock or SCC_DEFAULT_CLOCK,
        vgm.ay8910_clock or AY_DEFAULT_CLOCK,
        psg_clock,
        hz, vgm.total_samples,
    )

    loop_sample = vgm.loop_sample
    if loop_sample is not None and loop_sample < 0:
        loop_sample = 0

    fm_list = [patches[k] for k in patch_order]
    has_sample_ins = bool(
        oki_samples or adpcm_samples or spcm_audible or c140_audible
        or c352_audible or any(s.length >= 8 for s in pcm_samples)
        or scc_waves or ay_count or dac_samples or psg_count
    )
    if not fm_list:
        if is_opn and ym2203_ssg_count:
            warnings.append("no YM2203 FM key-ons found; converting the SSG only")
        elif is_opn2:
            warnings.append("no YM2612 key-ons found")
        else:
            warnings.append(
                "no YM2203 key-ons found" if is_opn
                else "no YM2151 key-ons found"
            )
        # The placeholder would occupy instrument 0 and shift every sample
        # instrument. Keep it only when the module would otherwise be empty.
        if not has_sample_ins:
            fm_list = [FMPatch(7, 0, 0, 0, tuple(OpRegs(tl=0) for _ in range(4)), name="empty")]  # type: ignore[arg-type]
    else:
        for i, p in enumerate(fm_list):
            fms, ams, mspeed = _pick_lfo_macros(pms_acc.get(i, []), VGM_RATE / hz)
            p.fms_macro = fms
            p.ams_macro = ams
            p.macro_speed = mspeed

    # A power-of-two carrier MULT sounds an octave off the channel f-num. Label
    # the note at the loudest carrier's octave and cancel the move with a
    # constant pitch macro, leaving the source registers untouched.
    fm_before_octave = len(fm_list)
    _apply_opn_carrier_octave(
        events, fm_list,
        skip_channels=(
            {
                ym_base[cid] + 2 + col
                for cid in (ym_ids if is_opn else ym2612_ids)
                for col in range(4)
            }
            if (ym2203_ext or ym2612_ext) else None
        ),
    )
    _rebase_non_fm_ins(events, fm_before_octave, len(fm_list))

    n_total_ins = (
        len(fm_list) + n_dac_ins
        + (len(audible) if pcm else 0) + len(oki_samples) + len(adpcm_samples)
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
        + len(scc_ins) + len(ay_ins) + len(psg_ins)
    )
    if n_total_ins > 250:
        warnings.append(
            f"{n_total_ins} instruments exceed Furnace's 250-slot limit; "
            "the extra instruments cannot be addressed"
        )
    n_total_smp = (
        len(pcm_samples if pcm else []) + len(oki_samples) + len(adpcm_samples)
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
        + len(dac_samples)
    )
    if n_total_smp > 65535:
        warnings.append(
            f"{n_total_smp} samples exceed the 16-bit sample-map index range"
        )

    if not is_opn and not is_opn2:
        transpose_opm_events(events, vgm.ym2151_clock)

    k007232_volume = vgm.chip_volume(0x2A)
    if k007232_volume is None:
        k007232_volume = 0.20

    return Song(
        vgm=vgm,
        t0=t0,
        loop_sample=loop_sample,
        speed=use_speed,
        hz=hz,
        fm_patches=fm_list,
        pcm_samples=pcm_samples if pcm else [],
        events=events,
        warnings=warnings,
        chip_fx=chip_fx,
        grid_note=grid_note,
        row_subdiv=row_subdiv,
        hilight_a=hilight_a,
        hilight_b=hilight_b,
        oki_samples=oki_samples,
        msm6258_samples=adpcm_samples,
        segapcm_samples=spcm_audible,
        c140_samples=c140_audible,
        c352_samples=c352_audible,
        channel_fx=channel_fx,
        k007232_volume=k007232_volume,
        c352_quad=c352_quad_events,
        fm_chip="ym2203" if is_opn else ("ym2612" if is_opn2 else "ym2151"),
        ym2203_ids=ym_ids,
        ym2203_ext=ym2203_ext,
        ym2203_ssg_count=ym2203_ssg_count,
        ym2612_ids=ym2612_ids,
        ym2612_ext=ym2612_ext,
        ym2612_csm=ym2612_csm,
        scc_ids=scc_ids,
        scc_waves=scc_waves,
        scc_ins=scc_ins,
        scc_count=scc_count,
        ay_ids=ay_ids,
        ay_ins=ay_ins,
        ay_count=ay_count,
        psg_ids=psg_ids,
        psg_ins=psg_ins,
        psg_count=psg_count,
        dac_samples=dac_samples,
        dac_count=dac_count,
        dac_stats=dac_stats,
    )


def format_report(song: Song) -> str:
    from vgm2151fur import __version__  # deferred: vgm2151fur/__init__ imports convert

    v = song.vgm
    lines = [
        f"file        {v.path}",
        f"track       {v.name}",
        f"converter   vgm2151fur {__version__}",
        f"game        {v.gd3.get('game', '')}",
        f"author      {v.gd3.get('author', '')}",
        f"length      {v.total_samples / VGM_RATE:.2f}s  ({v.total_samples} samples)",
        f"t0          {song.t0} ({song.t0 / VGM_RATE:.3f}s) pattern origin",
        f"loop        {song.loop_sample} ({(song.loop_sample or 0) / VGM_RATE:.3f}s)"
        if song.loop_sample is not None else "loop        none",
        f"grid        {song.hz:.4g} Hz, speed {song.speed} -> {song.samples_per_row:.1f} samples/row"
        f"  ({song.bpm:.1f} BPM 16ths{'; ' + song.grid_note if song.grid_note else ''})",
        f"FM patches  {len(song.fm_patches)}",
    ]
    if song.fm_chip == "ym2203":
        lines.insert(-1, (
            f"FM chip     YM2203 x{len(song.ym2203_ids)} @ "
            f"{(v.ym2203_clock or YM2203_DEFAULT_CLOCK) / 1e6:.6g} MHz, "
            f"{song.ym2203_ssg_count} SSG notes"
        ))
    elif song.fm_chip == "ym2612":
        layout = " Ext" if song.ym2612_ext else ""
        csm = " + CSM" if song.ym2612_csm else ""
        lines.insert(-1, (
            f"FM chip     YM2612{layout}{csm} x{len(song.ym2612_ids)} @ "
            f"{(v.ym2612_clock or YM2612_DEFAULT_CLOCK) / 1e6:.6g} MHz"
        ))
    for i, p in enumerate(song.fm_patches):
        lfo = ""
        if p.fms_macro:
            lfo = f"  FMS {list(p.fms_macro)} @{p.macro_speed}t"
        lines.append(
            f"  {i:02d}  alg {p.alg} fb {p.fb}  uses {p.uses:4d}  "
            f"carrierTL {p.carrier_tl():02X}  {p.name}{lfo}"
        )
    if song.fm_chip == "ym2612" and song.fm_patches:
        lines.append("  (carrier TL is relative; the key-on level rides the volume column)")
    if song.chip_fx:
        lfo_n = sum(1 for _s, c, _v in song.chip_fx if c in (0x10, 0x17, 0x18, 0x1E, 0x1F))
        if song.fm_chip == "ym2612":
            lines.append(f"LFO fx      {lfo_n} chip writes (10xx)")
        else:
            lines.append(f"LFO fx      {lfo_n} chip writes (17/18/1E/1F)")
    if song.fm_chip == "ym2203":
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and e.ch % 6 < 3
        )
    elif song.fm_chip == "ym2612":
        fm_end = (9 if song.ym2612_ext else 6) * len(song.ym2612_ids)
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and not e.dac and 0 <= e.ch < fm_end
        )
    else:
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and not e.dac and e.ch < 8
        )
    pcm_ons = sum(1 for e in song.events if e.on and e.pcm)
    lines.append(f"FM notes    {fm_ons}")
    if song.psg_ids:
        lines.append(
            f"SN76489     {song.psg_count} notes  ({len(song.psg_ins)} instruments, "
            f"{song.vgm.sn76489_clock or PSG_DEFAULT_CLOCK} Hz)"
        )
    if song.dac_count:
        data = sum(len(s.data) for s in song.dac_samples)
        maps = (len(song.dac_samples) + 119) // 120
        source = song.dac_stats.get("dac_source_bytes", 0)
        mode = song.dac_stats.get("dac_mode", "slices")
        if mode == "chunks":
            shape = f"{len(song.dac_samples)} PCM chunks"
            extra = (
                f", {song.dac_stats.get('dac_pan_chunks', 0)} pan chunks"
                f", {song.dac_stats.get('dac_trimmed_silence_bytes', 0)} "
                "silence bytes skipped"
            )
        elif mode == "packed":
            shape = f"{len(song.dac_samples)} PCM phrases"
            extra = f", {song.dac_stats.get('dac_trimmed_silence_bytes', 0)} silence bytes skipped"
        elif mode == "timeline":
            shape = "1 timeline sample"
            extra = ""
        else:
            shape = f"{len(song.dac_samples)} slices"
            silent = song.dac_stats.get("dac_silent_groups", 0)
            extra = f", {silent} silent rows skipped" if silent else ""
        lines.append(
            f"DAC         {song.dac_count} notes  {shape}  {maps} maps  "
            f"{data} bytes  ({source} source bytes{extra})"
        )
    n_regions = (
        len(song.pcm_samples) + len(song.segapcm_samples) + len(song.c140_samples)
        + len(song.c352_samples)
    )
    lines.append(f"PCM notes   {pcm_ons}  ({n_regions} sample regions)")
    if song.scc_ids:
        lines.append(
            f"SCC notes   {song.scc_count}  ({len(song.scc_ids)} chip"
            f"{'s' if len(song.scc_ids) > 1 else ''}, "
            f"{len(song.scc_waves)} waveforms, {song.vgm.k051649_clock} Hz"
            f"{', SCC+' if song.vgm.k051649_plus else ''})"
        )
    if song.ay_ids:
        lines.append(
            f"AY notes    {song.ay_count}  ({len(song.ay_ids)} chip"
            f"{'s' if len(song.ay_ids) > 1 else ''}, {song.vgm.ay8910_clock} Hz)"
        )
    if song.pcm_samples:
        lines.append(f"PCM level   {song.k007232_volume:.4g}x of the YM2151 (rip mix)")
    for i, s in enumerate(song.pcm_samples):
        if s.length < 8:
            continue
        lines.append(
            f"  {i:02d}  start {s.start:05X}  len {s.length:5d}  "
            f"hits {len(s.hits):3d}  pitch {s.mode_pitch()}  "
            f"C-4 {s.c4_rate(v.k007232_clock or 3579545)} Hz"
        )
    if song.oki_samples:
        hits = sum(len(s.hits) for s in song.oki_samples)
        clock = song.oki_samples[0].clock
        pin = "pin7 high" if song.oki_samples[0].pin7 else "pin7 low"
        lines.append(
            f"OKI phrases {len(song.oki_samples)}  hits {hits}  "
            f"{clock} Hz {pin}  {song.oki_samples[0].rate} Hz"
        )
        for i, s in enumerate(song.oki_samples):
            lines.append(
                f"  {i:02d}  phrase {s.phrase:02X}  len {s.length:5d}  "
                f"hits {len(s.hits):3d}  {s.name}"
            )
    if song.msm6258_samples:
        lines.append(f"MSM6258     {len(song.msm6258_samples)} bursts")
    for chip, smps in (
        ("SegaPCM", song.segapcm_samples),
        ("C140", song.c140_samples),
        ("C352", song.c352_samples),
    ):
        if not smps:
            continue
        hits = sum(len(s.hits) for s in smps)
        lines.append(f"{chip:<11} {len(smps)} regions  hits {hits}")
        for i, s in enumerate(smps):
            if s.loop_start is not None:
                loop = f"loop {s.loop_start}..{s.loop_end}"
                if getattr(s, "loop_mode", 0) == 2:
                    loop += " ping-pong"
            else:
                loop = "one-shot"
            lines.append(
                f"  {i:02d}  start {s.start:05X}  len {s.length:5d}  "
                f"hits {len(s.hits):3d}  {loop}"
            )
    last = song.events[-1].sample if song.events else song.t0
    rows = song.row_of(last) + 1
    lines.append(f"rows        {rows}  (~{math.ceil(rows / 256)} orders of 256)")
    for w in song.warnings:
        lines.append(f"warning     {w}")
    return "\n".join(lines) + "\n"
