"""Turn YM2151, K007232, MSM6295, MSM6258, SegaPCM, C140 and C352 writes into notes and a row grid."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

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
from vgm2151fur.oki import (
    FURNACE_C4,
    AdpcmSample,
    OkiSample,
    collect_msm6258,
    collect_oki,
    msm6258_div_fx,
)
from vgm2151fur.pcm import PcmSample, build_pcm_bank, pitch_to_note, rate_to_note
from vgm2151fur.segapcm import SegapcmSample, collect_segapcm, segapcm_rate
from vgm2151fur.timing import (
    MIN_ROW_SAMPLES,
    TARGET_TICK_SAMPLES,
    condense_row,
    estimate_grid,
    refine_pcm_grid,
)
from vgm2151fur.vgm import VGM_RATE, VgmFile


# YM2151 operator register: bits 0-2 = channel, bits 3-4 = op (0=M1, 1=C1, 2=M2, 3=C2)
# Furnace internal op index matches that: 0=M1, 1=C1, 2=M2, 3=C2
CARRIERS = {
    0: (3,),
    1: (3,),
    2: (3,),
    3: (3,),
    4: (1, 3),
    5: (1, 2, 3),
    6: (1, 2, 3),
    7: (0, 1, 2, 3),
}

# Official YM2151 KC note nibble → semitone from C of this octave.
# Valid codes: C#=0, D=1, D#=2, E=4, F=5, F#=6, G=8, G#=9, A=10, A#=12, B=13,
# C(next octave)=14.  3/7/11/15 are unused and alias 4/8/12/14.
# A linear 0=C# … 12=C map is wrong: Konami writes 0x4E for C-5, not D-5.
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

    The key fraction is the *top* six bits of the register: ymfm's
    ``ch_block_freq = (KC << 6) | (KF >> 2)``.  One KF step is 1/64 semitone
    and Furnace's E5xx is one unit per KF step: E5 0x80+f exports KF = f << 2,
    and values above 0xBF saturate the register's six-bit fraction.
    """
    return max(0, min(255, 0x80 + (kf >> 2)))


def kc_kf_units(kc: int, kf: int) -> int:
    """Absolute pitch code in 1/64 semitone units (fraction = KF >> 2)."""
    return (
        (((kc >> 4) & 7) * 12 + _KC_SEMI[kc & 0x0F]) * 64 + (kf >> 2)
    )


# Furnace's default YM2151 clock.  A chip clock is not a pitch in a .fur:
# Furnace keeps notes at their musical frequency at any clock (baseFreqOff in
# arcade.cpp), so the *registers* a rip's driver wrote sound sharper/lower than
# their plain meaning whenever the board clock differs from this one.
OPM_DEFAULT_CLOCK = 3579545

# One sample-chip trigger is often a key-on plus its own pitch and volume
# writes.  A real drum interval is hundreds of samples, never tens.
_ONSET_MERGE_SAMPLES = 100


def opm_pitch_offset(clock: int | None) -> int:
    """Clock → note transposition in 1/64 semitone units.

    Playback is a ratio: KC/KF registers the driver wrote for a 4 MHz OPM
    (Sega System 16C) sound 12·log2(clock/default) = +1.92 semitones above
    what the same registers mean at 3.579545 MHz.
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
        if ev.on and ev.ch < 8 and 0 <= ev.note < 180:
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
class OpRegs:
    dt_mul: int = 0x01
    tl: int = 0x7F
    ks_ar: int = 0x1F
    am_d1r: int = 0x00
    dt2_d2r: int = 0x00
    d1l_rr: int = 0xF7

    def as_tuple(self, include_tl: bool) -> tuple:
        if include_tl:
            return (self.dt_mul, self.tl, self.ks_ar, self.am_d1r, self.dt2_d2r, self.d1l_rr)
        return (self.dt_mul, self.ks_ar, self.am_d1r, self.dt2_d2r, self.d1l_rr)


@dataclass
class FMPatch:
    alg: int
    fb: int
    ams: int
    pms: int
    ops: tuple[OpRegs, OpRegs, OpRegs, OpRegs]  # M1, C1, M2, C2
    name: str = ""
    uses: int = 0
    fms_macro: tuple[int, ...] = ()
    ams_macro: tuple[int, ...] = ()
    macro_speed: int = 1

    def identity(self) -> tuple:
        """Cluster key: full timbre including carrier TLs.

        Carrier TL is part of the identity: Furnace applies volume on a log
        curve, so a stripped TL cannot be reconstructed from pattern volume.
        Keep TL on the instrument and omit pattern volume.
        """
        op_keys = tuple(self.ops[i].as_tuple(include_tl=True) for i in range(4))
        return (self.alg & 7, self.fb & 7, op_keys)

    def carrier_tl(self) -> int:
        tls = [self.ops[i].tl & 0x7F for i in CARRIERS.get(self.alg & 7, (3,))]
        return min(tls) if tls else 0x7F


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
    # Key-off sample (0 = still playing at the end / unknown).
    end: int = 0
    # Mid-note pitch writes: (sample, 1/64-semitone delta from the note pitch).
    # Non-empty for percussion sweeps some drivers write a step at a time
    # (~88 or ~190 samples per step); encoders turn this into 01xx/02xx pitch
    # ramps. C140 slides use the same shape.
    sweep: tuple[tuple[int, int], ...] = ()
    # Mid-note volume steps (sample, C140 8-bit level, Furnace 08xy balance or
    # None to leave it). Emitted as volume-column (and pan) updates on rows
    # without a note, so software fades survive.
    vol_pts: tuple[tuple[int, int, int | None], ...] = ()
    # Mid-note *carrier* TL steps (sample, absolute YM2151 carrier TL).  Some
    # drivers implement a note's volume envelope by rewriting the carrier TL
    # while the voice is held (Namco System 86 fades a held note out with
    # 0x60+ writes that never retrigger); the key-on snapshot cannot carry
    # them, so the writer maps them onto the Furnace volume column relative to
    # the instrument's own carrier TL.
    tl_pts: tuple[tuple[int, int], ...] = ()
    # PCM pan 0x00 is a real "both off". OKI/MSM6258 are mono and must not
    # write 08xx (that effect is the K007232/YM2151 split pan).
    no_pan: bool = False


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
    # Chip-wide YM2151 LFO: (sample, effect, value). 17xx speed, 18xx wave,
    # 1Exx AMD, 1Fxx PMD. Furnace defaults PMD/AMD to 0x7F and LFO off.
    chip_fx: list[tuple[int, int, int]] = field(default_factory=list)
    # How hz was chosen ("" = default 60 Hz). Shown in --report.
    grid_note: str = ""
    # Rows per measured 16th note (1 = one row per 16th, 8 = 128th-note rows).
    row_subdiv: int = 1
    # K007232 output level relative to the YM2151, from the rip's own mix
    # (extra header) when present. Written to the .fur chip output.
    k007232_volume: float = 0.20
    # Set by loop finding. The row `Bxx` jumps from. None keeps the jump on
    # the last content row, which is the VGM loop length.
    loop_end_sample: int | None = None

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

        Furnace EDxx delays a channel's row by whole ticks (playback.cpp
        processRow/nextTick). At speed 1 there is no sub-row placement, so the
        nearest row is exact enough (the drivers' note unit matches the row
        within ~1%); at higher speeds floor + nearest tick keeps the error
        under half a tick.  `stats["ed_rolled"]` counts offsets too large for
        the row's tick count (a sub-row position the condensed grid cannot hold).
        """
        if self.speed <= 1:
            return self.row_of(sample), 0
        row = max(0, int((sample - self.t0) // self.samples_per_row))
        tick = VGM_RATE / self.hz
        target = self.t0 + row * self.samples_per_row
        d = int(round((sample - target) / tick))
        if d >= self.speed:
            row += 1
            d = 0
            if stats is not None:
                stats["ed_rolled"] = stats.get("ed_rolled", 0) + 1
        return row, max(0, d)

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
            # The reset is the vibrato switching off.  Keep it as the final
            # step so the macro returns to 0; without it the macro holds the
            # last sensitivity for the rest of the song, running a wobble past
            # every later note.  Its length is irrelevant (a macro holds its
            # last value), so reuse the previous step to keep the median step
            # honest.
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

    The Konami drivers increment the sensitivity every ~0.1 s of sustain, so a
    held note starts steady and drifts "drunk" over ~1 s.  The macro holds each
    value for `speed` ticks, so the step duration must be converted with the
    song's real tick rate (44100/hz), not a fixed 60 Hz frame.
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

    Drivers write KC/KF as a pair a few samples apart. Merge those pairs, or the
    write-order transient does not enter the trajectory.  The note pitch comes
    from the first pair when it lands right after the key-on (the drivers set
    the sweep start *after* keying on), otherwise from the pre-key-on registers
    (the note really started at the old pitch and the sweep begins later).
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
    # The note's own pitch counts as a trajectory point: a single mid-note
    # step (one KC pair written and held) has no internal spread at all, and
    # the old range check dropped it whole. The note then stayed at its old
    # pitch until the next key-on.
    vals = [0] + [d for _s, d in deltas]
    if max(vals) - min(vals) >= 4:
        ev.sweep = deltas


def analyze(
    vgm: VgmFile, *, speed: int | None = None, pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    condense: float = 1.0,
) -> Song:
    tick = TARGET_TICK_SAMPLES if tick_samples is None else float(tick_samples)
    row_floor = MIN_ROW_SAMPLES if min_row is None else float(min_row)
    ym = YM2151State()
    patches: dict[tuple, FMPatch] = {}
    patch_order: list[tuple] = []
    events: list[NoteEvent] = []
    keyon_times: list[int] = []
    warnings: list[str] = list(vgm.warnings)
    pending_off = [None] * 8  # type: list[int | None]
    chip_fx: list[tuple[int, int, int]] = []
    last_lfrq = last_wave = -1
    # The OPM resets its LFO depth register (0x19) to 0, but Furnace's arcade
    # core defaults PMD and AMD to 0x7f (arcade.cpp reset).  A driver that
    # never writes 0x19 - Namco System 86 does not - but still loads PMS/AMS
    # into its instruments would otherwise play the maximum vibrato/tremolo
    # the rip never had, because the depth is a chip-wide register the score
    # does not carry.  Send the hardware reset depth explicitly.
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
            # An operator the KON mask leaves un-keyed never starts its
            # envelope; the closest a static OPM instrument gets is maximum
            # attenuation (TL 127, ~-95 dB) on that operator.  A common
            # driver shape for 3-operator percussion and brass patches.
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
        if chip_fx and chip_fx[-1][1] == cmd and chip_fx[-1][2] == val:
            return
        chip_fx.append((sample, cmd, val))

    # See last_amd/last_pmd above: pin the chip-wide depth to the OPM's reset
    # value before the first row (a module without any YM2151 write must not
    # carry YM2151 effects).
    if any(w.chip == "ym2151" for w in vgm.writes):
        emit_chip_fx(0, FX_LFO_AMD, 0)
        emit_chip_fx(0, FX_LFO_PMD, 0)

    last_keyon_sample = [-10**9] * 8
    for w in vgm.writes:
        if w.chip != "ym2151":
            continue
        if w.reg != 0x08:
            if w.reg == 0x18:
                if w.val != last_lfrq:
                    last_lfrq = w.val
                    emit_chip_fx(w.sample, FX_LFO_SPEED, w.val & 0xFF)
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
                if ym.kon[ch] and sweep_ev[ch] is not None:
                    sweep_pts[ch].append((w.sample, ym.kc[ch], ym.kf[ch]))
            elif 0x60 <= w.reg <= 0x7F:
                # A carrier TL write while the voice is held is a software
                # volume step; record only steps that move the carrier, so a
                # modulator rewrite (timbre) does not fake a fade.
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
            # A redundant write or a per-operator change while the note sounds:
            # every operator stays as it was, so this is not a note event.
            # Treating it as one retriggered the whole note and was audible on
            # sustained leads.
            pass
        elif ym.kon[ch]:
            pending_off[ch] = w.sample
            _flush_pms_ramp(pms_acc, held_ins[ch], held_ramp[ch], held_kon[ch])
            held_ramp[ch] = []
            close_sweep(ch)
            close_tl(ch)
            ym.kon[ch] = False
            held_ins[ch] = -1

    for ch in range(8):
        _flush_pms_ramp(pms_acc, held_ins[ch], held_ramp[ch], held_kon[ch])
        close_sweep(ch)
        close_tl(ch)

    # Flush key-offs that were not a same-sample retrigger.
    # We recorded only key-ons above; add offs that sit > 1 sample after last on
    # and before the next on. Re-walk for offs.
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
        # RL == 0) cuts the channel instantly even with the voices still keyed.
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

    # Row 0 is the start of the VGM, not the first key-on. Loop points in the
    # header are sample offsets from t=0; shifting t0 would move Bxx.
    t0 = 0
    # Chosen below, after every chip has been read.
    grid_note = ""
    row_subdiv = 1

    rom, pcm_samples = build_pcm_bank(vgm)
    pcm_ins_base = len(patch_order)
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
                    ch=8 + h.ch,
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
    channel_fx: list[tuple[int, int, int, int]] = []
    if pcm:
        oki_samples, oki_warn = collect_oki(vgm)
        warnings.extend(oki_warn)
        adpcm_samples, adpcm_warn = collect_msm6258(vgm)
        warnings.extend(adpcm_warn)
        # Sample chips sit after the 8 YM2151 channels: K007232, then each
        # MSM6295 (4 voices), then each MSM6258.
        sample_base = 8 + (2 if audible else 0) if pcm else 8
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
                # A frequency write right after key-on is the driver's real
                # note pitch (same refit as the FM sweep); otherwise the
                # key-on register value is the note's pitch.
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
                # A hit keyed with both registers at zero is a driver fade-in
                # (or a mute). When the registers ramp up afterwards the
                # key-on balance is undefined, so start silent on Furnace's
                # default balance: writing 08 00 would mute the voice forever,
                # since the fade steps only move the volume column.
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
        _c352_rom, c352_samples, c352_offs, c352_warn = collect_c352(vgm)
        warnings.extend(c352_warn)
        c352_audible = [s for s in c352_samples if s.length >= 8]
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
                # The driver strobes first and writes the note's own volume
                # inside the same frame. The register at the strobe belongs to
                # the previous note (often 0, since the driver zeroes it when
                # it stops the voice), and the real write lands on the key-on's
                # own row, where no volume step is placed. Take the first
                # in-frame step as the note's own balance or the attack plays
                # at the stale level.
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

    events.sort(key=lambda e: (e.sample, e.ch, 0 if e.on else 1))

    # Prefer the measured key-on lattice: rows subdivide the measured 16th note
    # and carry several ticks, which lets EDxx place every event at its true
    # (60 Hz-frame-quantized) write time.  Manual --speed keeps the legacy 60 Hz
    # behaviour.  A rip with no FM stream to measure (the C352-only boards) is
    # measured from its sample-chip triggers instead.
    pcm_onsets: dict[int, list[int]] = {}
    for e in events:
        if e.pcm and e.on:
            pcm_onsets.setdefault(e.ch, []).append(e.sample)
    for ch, times in pcm_onsets.items():
        merged: list[int] = []
        for t in times:
            if merged and t - merged[-1] <= _ONSET_MERGE_SAMPLES:
                continue
            merged.append(t)
        pcm_onsets[ch] = merged

    if speed and speed >= 1:
        hz = 60.0
        use_speed = speed
    else:
        grid = estimate_grid(vgm, tick_samples=tick, min_row=row_floor)
        if grid is None and pcm_onsets:
            grid = estimate_grid(
                vgm, tick_samples=tick, min_row=row_floor,
                onsets=pcm_onsets, label="sample-trigger lattice",
            )
        if grid is not None:
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

    # Condensing lengthens every row by `condense`, so the row rate (and the
    # tracker BPM, 15 x rows/s) drops by that factor.  It runs last, after the
    # PCM refinement, so PCM restarts that no longer fit their own row show up
    # as loss in the writer rather than being silently re-refined away.
    if condense > 1:
        hz, use_speed, row_subdiv = condense_row(
            hz, use_speed, row_subdiv, condense, tick,
        )
        grid_note = f"{grid_note}; condensed x{condense:g}" if grid_note else f"condensed x{condense:g}"

    loop_sample = vgm.loop_sample
    if loop_sample is not None and loop_sample < 0:
        loop_sample = 0

    fm_list = [patches[k] for k in patch_order]
    has_sample_ins = bool(
        oki_samples or adpcm_samples or spcm_audible or c140_audible
        or c352_audible or any(s.length >= 8 for s in pcm_samples)
    )
    if not fm_list:
        warnings.append("no YM2151 key-ons found")
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
        oki_samples=oki_samples,
        msm6258_samples=adpcm_samples,
        segapcm_samples=spcm_audible,
        c140_samples=c140_audible,
        c352_samples=c352_audible,
        channel_fx=channel_fx,
        k007232_volume=k007232_volume,
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
    for i, p in enumerate(song.fm_patches):
        lfo = ""
        if p.fms_macro:
            lfo = f"  FMS {list(p.fms_macro)} @{p.macro_speed}t"
        lines.append(
            f"  {i:02d}  alg {p.alg} fb {p.fb}  uses {p.uses:4d}  "
            f"carrierTL {p.carrier_tl():02X}  {p.name}{lfo}"
        )
    if song.chip_fx:
        lfo_n = sum(1 for _s, c, _v in song.chip_fx if c in (0x17, 0x18, 0x1E, 0x1F))
        lines.append(f"LFO fx      {lfo_n} chip writes (17/18/1E/1F)")
    fm_ons = sum(1 for e in song.events if e.on and not e.pcm)
    pcm_ons = sum(1 for e in song.events if e.on and e.pcm)
    lines.append(f"FM notes    {fm_ons}")
    n_regions = (
        len(song.pcm_samples) + len(song.segapcm_samples) + len(song.c140_samples)
        + len(song.c352_samples)
    )
    lines.append(f"PCM notes   {pcm_ons}  ({n_regions} sample regions)")
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
