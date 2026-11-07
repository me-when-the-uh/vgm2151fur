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
from vgm2151fur.fm import (
    CARRIERS, FMPatch, OpRegs, OPN_CARRIERS, OPM_OP_TO_MASK, OPN_ORDERED_OPS,
    carriers_for, opn_tl_op,
)
from vgm2151fur.k053260 import (
    K053260_REFIT_SAMPLES,
    K053260Sample,
    collect_k053260,
    hit_played_pitch as k053260_played_pitch,
    hit_played_volume as k053260_played_volume,
    k053260_pan_fx,
    k053260_rate,
    merge_step_writes as k053260_merge_steps,
)
from vgm2151fur.oki import (
    ADPCM_MAP_SLOTS,
    FURNACE_C4,
    MSM6258_MAP_NOTE,
    AdpcmSample,
    OkiSample,
    collect_msm6258,
    collect_oki,
    msm6258_div_fx,
    msm6258_ins_count,
)
from vgm2151fur.opl import (
    DRUM_BITS,
    FREQ_BASE_OPL4,
    KIND_OPL,
    KIND_OPL2,
    KIND_OPL3,
    KIND_OPL4,
    KIND_Y8950,
    N as OPL_N,
    OPL2_DEFAULT_CLOCK,
    OPL3_DEFAULT_CLOCK,
    OPL_DEFAULT_CLOCK,
    ORDERED_OPS_L,
    SLOT_ADDR,
    OPLState,
    channel_count,
    drum_channel,
    opl3_like,
    opl_carrier_octave,
    opl_module_clock,
    opl_note_for,
    opll_module_clock,
    opll_note_for,
)
from vgm2151fur.ym2413 import (
    OPLL_CHANNELS,
    OPLL_DRUMS,
    OPLL_DRUMS_CHANNELS,
    OPLL_SETTLE,
    OPLLState,
    opll_rhythm_plan,
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
from vgm2151fur.saa1099 import (
    SAA_DEFAULT_CLOCK,
    SAA_DIVIDER,
    SAA_PITCH_MAX,
    SAA_VOICES,
    collect_saa1099,
    saa_module_clock,
    saa_note,
    saa_period_note,
    saa_period_value,
)
from vgm2151fur.sn76489 import (
    PSG_DEFAULT_CLOCK,
    PSG_DIVIDER,
    PSG_FURNACE_CLOCK,
    PSG_MAX_PERIOD,
    collect_sn76489,
    psg_note,
    t6w28_note,
    t6w28_noise_note,
)
from vgm2151fur.rf5c import (
    RF5C68_DEFAULT_CLOCK,
    RF5C164_DEFAULT_CLOCK,
    RF5C_VOICES,
    Rf5cOff,
    Rf5cSample,
    collect_rf5c,
    rf5c_rate,
)
from vgm2151fur.nes import (
    NES_CHANNELS,
    NES_CPU_DIVIDER,
    NES_DEFAULT_CLOCK,
    NES_FURNACE_CLOCK,
    NES_PCM_MAP_SLOTS,
    NesDpcmHit,
    NesDpcmSample,
    NesPcmHit,
    NesPcmSample,
    _noise_note as nes_noise_note,
    _period_delta as nes_period_delta,
    cluster_nes_pcm,
    collect_nes_dpcm,
    collect_nes_pcm,
    collect_nes_tones,
    nes_note,
)
from vgm2151fur.gb import (
    GB_CHANNELS,
    GB_DEFAULT_CLOCK,
    GB_PITCH_DIVIDER,
    GbHit,
    collect_gb,
    gb_noise_note,
    gb_note,
    gb_period_delta,
)
from vgm2151fur.huc6280 import (
    HUC6280_ATTACK_WINDOW,
    HUC6280_CHANNELS,
    HUC6280_DEFAULT_CLOCK,
    HUC6280_MAX_PERIOD,
    HUC6280_REFIT_SAMPLES,
    NOISE_ENABLE,
    Huc6280PcmHit,
    Huc6280PcmSample,
    collect_huc6280,
    collect_huc6280_pcm,
    huc6280_noise_note,
    huc6280_note,
    pce_effective,
)
from vgm2151fur.segapcm import (
    SegapcmSample, collect_segapcm, hit_played_freq as segapcm_played_freq,
    segapcm_rate,
)
from vgm2151fur.timing import (
    MIN_ROW_SAMPLES,
    TARGET_TICK_SAMPLES,
    Grid,
    condense_row,
    estimate_grid,
    place_tick,
    refine_pcm_grid,
)
from vgm2151fur.vgm import VGM_RATE, VgmFile, assemble_rom
from vgm2151fur.ym2610 import (
    ADPCM_MAP_SLOTS as YM2610_MAP_SLOTS,
    YM2610_DEFAULT_CLOCK,
    YM2610_SSG_DIVIDER,
    YM2610State,
    AdpcmAHit,
    AdpcmASample,
    AdpcmBHit,
    AdpcmBSample,
    collect_ym2610_adpcm,
    ym2610_note_for,
)
from vgm2151fur.ym2608 import (
    YM2608_DEFAULT_CLOCK,
    YM2608_SSG_DIVIDER,
    YM2608State,
    collect_ym2608_delta_t,
    collect_ym2608_rhythm,
)
from vgm2151fur.y8950 import (
    Y8950_DEFAULT_CLOCK,
    collect_y8950_delta_t,
)
from vgm2151fur.ymf278b import (
    OPL4_WAVE_SLOTS,
    Opl4WaveSample,
    collect_opl4_wave,
    load_yrw801,
    opl4_pan_fx,
    opl4_rate,
)
from vgm2151fur.ymz280b import (
    YMZ280B_VOICES,
    YmzOff,
    YmzSample,
    collect_ymz280b,
    hit_played_fnum as ymz_played_fnum,
    ymz280b_rate,
    ymz_pan_fx,
)
from vgm2151fur.vsu import (
    VSU_CHANNELS,
    VSU_DEFAULT_CLOCK,
    VSU_ENVELOPE_CLOCKS,
    VSU_PITCH_DIVIDER,
    VSU_WAVE_BURST,
    collect_vsu,
    vsu_note,
)
from vgm2151fur.ssgpcm import (
    SsgPcmHit,
    SsgPcmSample,
    cluster_ssg_pcm,
    collect_ssg_pcm,
)
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


# A driver may key an OPN voice while its carriers are still muted and write
# the patch's real TL a few samples later. A level write this close to the
# key-on is the note's starting level, not a mid-note step.
OPN_TL_FOLD = 512
# A driver can load a new patch into a still-keyed OPN voice (Time Trax holds
# a drone while swapping patches). The chip changes the operator pitch and
# timbre at once, but a Furnace note keeps its instrument until the next
# key-on. Split the note when a pitch/timbre register changes, after giving
# the patch block time to settle. A key transition inside this window is the
# next note taking the patch, so the split is not emitted then.
OPN_PATCH_SETTLE = 512


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

# A driver that keys on with its carriers parked at TL 7F loads the voice's
# real levels inside the same burst. Measured on the samples that do this,
# the last operator write lands 1-3 samples after the key-on, and one OPM
# register write takes about 2 samples. A burst no longer than a few writes
# is still the same command. Anything wider is a genuine mid-note change.
_OPM_BURST_LOAD_SAMPLES = 16


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


def transpose_opm_events(
    events: list[NoteEvent], clock: int | None, channels: int = 8,
) -> None:
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
        if ev.on and not ev.dac and ev.ch < channels and 0 <= ev.note < 180:
            ev.note, ev.e5 = _shift_note_e5(ev.note, ev.e5, offset)


def _pcm_rate_note(rate: int, reference: int) -> tuple[int, int]:
    """Sample-playback note/E5 for a DAC-stream byte rate.

    A sample body played at note 108 runs at its centre rate, so the ratio
    ``2**((note-108)/12)`` scales that rate.  The driver retunes the HuC6280
    DAC stream constantly (23442 byte-rate writes on the faceball rip), and
    the E5 fine tune carries the remainder at 1/128 semitone, so the sample
    plays at the source's rate instead of one fixed pitch.
    """
    if rate <= 0 or reference <= 0:
        return 108, E5_CENTER
    semis = 12.0 * math.log2(rate / reference)
    base = int(math.floor(semis))
    fine = int(round((semis - base) * 128.0))
    note = 108 + base
    while note < 0:
        note += 1
        fine -= 128
    while note > NOTE_MAX:
        note -= 1
        fine += 128
    return note, max(0, min(255, E5_CENTER + fine))


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
    # OPM PMS/AMS writes while the note is held: (sample, pms, ams). A phase-2
    # pass turns each note's own ramp (including its lead-in) into a macro.
    opm_pms_pts: tuple[tuple[int, int, int], ...] = ()
    # Mid-note volume steps: (sample, chip level, 08xy balance or None). On a
    # slide note the third value is the FA01/FA00 flag instead.
    vol_pts: tuple[tuple[int, int, int | None], ...] = ()
    # Mid-note carrier TL steps: (sample, absolute YM2151 carrier TL). The
    # writer maps them onto the volume column relative to the instrument.
    tl_pts: tuple[tuple[int, int], ...] = ()
    # YM2151 only: mid-note DT1/MULT writes, (sample, operator, raw 0x40
    # register byte). A driver that retunes an operator every frame inside a
    # short staccato note has no row equivalent; a per-operator macro keeps
    # the partial's fast pitch/timbre motion.
    op_dtmul_pts: tuple[tuple[int, int, int], ...] = ()
    # YM2151 only: MULT/DT1/DT2 changes that fit on pattern rows, as
    # (sample, operator, engine value) for 16xy, 53xy and 55xy. Used
    # instead of an operator macro whenever the steps land on rows.
    op_mul_fx: tuple[tuple[int, int, int], ...] = ()
    op_dt_fx: tuple[tuple[int, int, int], ...] = ()
    op_dt2_fx: tuple[tuple[int, int, int], ...] = ()
    # YM2151 only: mid-note DT2 writes, (sample, operator, 0-3).
    op_dt2_pts: tuple[tuple[int, int, int], ...] = ()
    # Volume-column baseline override for `tl_pts` and this note's own level.
    # An Ext channel-3 column scales one operator, not the algorithm's carrier
    # set (`ym2203ext.cpp` DIV_CMD_VOLUME), so the writer cannot take the
    # baseline from carrier_tl(); it is that operator's floor instead. The chip
    # answers `TL = 127 - vol + op.tl`, so the instrument holds the quietest
    # reachable number (the *loudest* level) and every step is `127 - (TL-floor)`.
    vol_tl_base: int | None = None
    # PCM pan 0x00 is a real "both off"; OKI/MSM6258 are mono and must not
    # write 08xx.
    no_pan: bool = False
    # C352 quad output: the four volume registers (front L/R, rear L/R),
    # 0-255. When set, they carry the level and the volume column is pinned.
    quad: tuple[int, int, int, int] | None = None
    # Mid-note changes to those four, as (sample, fl, fr, rl, rr).
    quad_pts: tuple[tuple[int, int, int, int, int], ...] = ()
    # OPL rhythm mode: this note hits one of the kit's percussion operators, so
    # its instrument is a DIV_INS_OPL_DRUMS and the note value only names which
    # operator key to release.
    drum: bool = False
    # SCC/AY only: raw (sample, register) pitch writes. A phase-2 pass turns
    # them into instrument macros once the tick rate is known.
    tone_pitch_pts: tuple[tuple[int, int], ...] = ()
    # SCC only: (sample, 32 raw wave-RAM bytes) whenever the image changed.
    tone_wave_pts: tuple[tuple[int, bytes], ...] = ()
    # SCC only: the wavetable index the note itself starts on (-1 = no wave).
    tone_wave: int = -1
    tone_period_offset: int = 0
    # PCE only: the raw 12-bit period the note starts on. Splitting a long
    # gate at volume peaks needs the pitch at each new note start, which the
    # note/E5 pair encodes only indirectly.
    tone_period: int = 0
    # 0 = not a tone-chip note, 1 = SCC, 2 = AY, 8 = SAA1099. The phase-2
    # pass finds the instrument for these once the tick rate is known.
    tone_chip: int = 0
    # SAA1099 only: (sample, left, right) raw amplitude nibbles after the
    # opening.  The platform multiplies them through the panning macros, so
    # keeping the pair (rather than a volume plus a balance) reproduces soft
    # pan and a volume envelope exactly.
    tone_amp_pts: tuple[tuple[int, int, int], ...] = ()
    # PCE noise-mode note: its "pitch" is the 0-31 noise register, which the
    # platform adds ``std.pitch`` to directly, not a period-table value.
    tone_noise: bool = False
    # The sample where noise ends and the tone tail begins (0 = pure noise).
    # Set only on `tone_noise` notes; the pitch points before it are noise
    # registers, the ones at/after it are tone periods.
    tone_noise_end: int = 0
    # The full noise on/off timeline as (sample, 1|0).  The instrument's noise
    # macro follows it, and the pitch points use a noise register while the
    # mode is 1 and a tone period while it is 0.
    tone_noise_modes: tuple[tuple[int, int], ...] = ()
    # Tone chip clock and divider: SCC 16, AY-8910 8, YM2203 SSG 16.
    tone_clock: int = 0
    tone_div: int = 8
    # VSU channel 4: modulation-table image index for the instrument's FD
    # feature (the wavetable itself is not a module WAVE block).
    tone_mod: int = 0
    # Game Boy wavetable voice: the column carries NR32 levels as row steps,
    # so the binding must not fold them into a volume macro.
    tone_gb_wave: bool = False
    # Game Boy DIV_INS_GB envelope fields (pulse/noise only).
    gb_soft_env: bool = False
    gb_env_vol: int = 0
    gb_env_dir: int = 0
    gb_env_len: int = 0
    gb_sound_len: int = 63
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
    # OPL only: output level writes per operator while the note is held,
    # (operator index, ((sample, $4n low six bits), ...)). The carrier's own
    # levels already ride the volume column through `tl_pts`; a modulator's
    # go to `op_tl_fx` as 12xx-15xx effects instead of an instrument copy.
    opl_op_tl: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = ()
    # OPL only: (sample, operator index, level) on the rows where the
    # modulator's level differs from the instrument baseline.
    op_tl_fx: tuple[tuple[int, int, int], ...] = ()
    # OPLL only: mid-note `$3x` high-nibble changes and repeated key-on writes
    # that arrive faster than one pattern row. A phase-2 pass turns them into
    # the instrument's wave and phase-reset macros.
    opll_preset_pts: tuple[tuple[int, int], ...] = ()
    opll_phase_pts: tuple[int, ...] = ()


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
    # Leading-hold delay for the first pitch value (ticks). Same for volume.
    pitch_delay: int = 0
    waves: tuple[int, ...] = ()
    wave_speed: int = 1
    wave_loop: int = 255
    # Per-tick 0-15 register levels. The platform computes outVol = vol *
    # macro / 15. A column of 15 makes the macro the register value.
    vol: tuple[int, ...] = ()
    vol_speed: int = 1
    vol_loop: int = 255
    vol_delay: int = 0
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
    # SAA1099 only: the raw amplitude nibbles as panning macros.  Furnace's
    # SAA platform writes the amplitude register as
    # ``applyPan(outVol, pan)``, and ``applyPan(15, (left << 4) | right)`` is
    # exactly the source byte, so a column of 15 plus these two macros carries
    # volume and soft pan together without rounding.
    panl: tuple[int, ...] = ()
    panl_speed: int = 1
    panl_loop: int = 255
    panl_delay: int = 0
    panr: tuple[int, ...] = ()
    panr_speed: int = 1
    panr_loop: int = 255
    panr_delay: int = 0
    # SAA1099 envelope control (register 0x18/0x19, the ex1 macro).  Written
    # on the group's output voice; values are the raw control bytes.
    env: tuple[int, ...] = ()
    env_speed: int = 1
    env_loop: int = 255
    env_delay: int = 0
    # Game Boy: the DIV_INS_GB feature. A hardware-envelope note carries
    # env_vol/env_dir/env_len; a software ("zombie") envelope sets soft_env
    # and uses `vol` instead. `gb_wave` marks the wavetable voice, whose
    # instrument selects `wave` through the wave-synth feature.
    gb_soft_env: bool = False
    gb_env_vol: int = 0
    gb_env_dir: int = 0
    gb_env_len: int = 0
    gb_sound_len: int = 63
    gb_wave: bool = False
    # VSU only: which modulation-table image the instrument's FD feature
    # carries (channel 4).
    vsu_mod: int = 0


@dataclass(frozen=True)
class FmSystem:
    """One FM system in module order, for the .fur chip list.

    A module can host chips from more than one family: the Zone 66 logs put a
    PCM-only YM2612 beside the YM3812 that plays the music. `kind` uses the
    same names as `Song.fm_chip`; `fm_channels` is only the FM part (the SSG
    and sample columns are counted separately by the writer), and `clock` is
    the source clock the writer must carry in the chip flag.
    """

    kind: str
    chip_id: int = 0
    fm_channels: int = 0
    ext: bool = False
    csm: bool = False
    drums: bool = False
    clock: int = 0
    # The YM2413 core asked to run as a Konami VRC7 (six channels, its own
    # preset ROM); the writer emits DIV_SYSTEM_VRC7 instead of the OPLL id.
    vrc7: bool = False

    @property
    def channels(self) -> int:
        """Module columns this system occupies, its sample engines included.

        The report and the writer both walk systems in channel order, so the
        span belongs with the system rather than in a second table.
        """
        if self.kind in ("opl", "opl2", "opl3"):
            from vgm2151fur.opl import channel_count
            return channel_count(self.kind, self.drums)
        if self.kind == "y8950":
            # OPL2-shaped FM plus the one delta-T (ADPCM) column.
            from vgm2151fur.opl import channel_count
            return channel_count(self.kind, self.drums) + 1
        if self.kind == "opl4":
            # OPL3-shaped FM plus the 24 wave columns.
            from vgm2151fur.opl import channel_count
            return channel_count(self.kind, self.drums) + 24
        if self.kind == "opll":
            if self.vrc7:
                return 6
            return OPLL_DRUMS_CHANNELS if self.drums else OPLL_CHANNELS
        if self.kind in ("ym2610", "ym2608"):
            # FM, three SSG voices, six kit or ADPCM-A voices and one delta-T.
            return self.fm_channels + 3 + 6 + 1
        if self.kind == "ym2203":
            return self.fm_channels + 3
        return self.fm_channels or 8


def system_channel_ranges(systems) -> list[tuple[FmSystem, int]]:
    """(system, first module channel) in module order."""
    out: list[tuple[FmSystem, int]] = []
    ch = 0
    for system in systems:
        out.append((system, ch))
        ch += system.channels
    return out


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
    segapcm_ids: list[int] = field(default_factory=list)
    rf5c68_samples: list[Rf5cSample] = field(default_factory=list)
    rf5c164_samples: list[Rf5cSample] = field(default_factory=list)
    c140_samples: list[C140Sample] = field(default_factory=list)
    c352_samples: list[C352Sample] = field(default_factory=list)
    k053260_samples: list[K053260Sample] = field(default_factory=list)
    # YMZ280B (PCMD8): one sample body per distinct ROM range/loop/depth.
    # `ymz280b_ids` are the chip instances in channel order.
    ymz280b_samples: list[YmzSample] = field(default_factory=list)
    ymz280b_ids: list[int] = field(default_factory=list)
    ymz280b_count: int = 0
    # (sample, channel, effect, value) placed before notes. MSM6258 divider.
    channel_fx: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Chip-wide YM2151 LFO effects: (sample, effect, value[, tick offset]).
    # 17xx speed, 18xx wave, 1Exx AMD, 1Fxx PMD.
    chip_fx: list[tuple[int, int, int] | tuple[int, int, int, int]] = field(
        default_factory=list
    )
    # How hz was chosen ("" = default 60 Hz). Shown in --report.
    grid_note: str = ""
    # Held one/two-step pitch moves written as E8/E9, and one/two-step
    # trajectories that stayed ramps (a slide, a release tail, or a short
    # hold). Shown in --report.
    legato_steps: int = 0
    legato_ramps: int = 0
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
    # Module systems in channel order. Empty means "derive one system from
    # `fm_chip`" (every pre-combination module). A combined log lists each
    # family's chips here; the YM2612 DAC system comes before the OPL one so
    # its six columns match `ym_base`.
    fm_systems: list[FmSystem] = field(default_factory=list)
    # True when the source log carried an FM chip at all. A PCE/SCC/NES-only
    # rip leaves this false so the writer drops the analyzer's legacy YM2151
    # placeholder instead of emitting an empty OPM device. Defaults to true so
    # hand-built Songs (tests, the older entry points) keep their FM system.
    has_fm_source: bool = True
    # YM2151 instances in channel order.  Each one owns eight FM channels.
    ym2151_ids: list[int] = field(default_factory=list)
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

    # OPL-family instances in channel order. The YMF262 owns both register
    # banks, so it is always a single id; the YM3812 may appear twice in a
    # Sound Blaster Pro log. `opl_drums` marks the rhythm system, whose last
    # melodic columns play the kit.
    opl_ids: list[int] = field(default_factory=list)
    opl_drums: bool = False
    # YM2413 (OPLL) instances in channel order. Each owns nine melodic
    # channels, or eleven when the drum kit is in use (the kit takes over the
    # last three hardware channels as five percussion columns).
    opll_ids: list[int] = field(default_factory=list)
    opll_drums: bool = False
    # The same core asked for as a Konami VRC7 (bit 31 of the YM2413 clock).
    opll_vrc7: bool = False
    # Top of the volume column for this system's FM channels. The OPL platform
    # answers 63 to getVolMax and masks the value to six bits, so writing the
    # 127 an OPN column tops out at would land on zero.
    fm_vol_max: int = 127
    # FM and percussion note counts for the report. `fm_notes` excludes the kit
    # columns, which ride on the same channel list but take a drum voice.
    fm_notes: int = 0
    drum_notes: int = 0

    # YM2610 (OPNB) instances.  Each one owns 14 channels (4 FM + 3 SSG +
    # 6 ADPCM-A + 1 ADPCM-B) or 16 on the B variant (6 FM).
    ym2610_ids: list[int] = field(default_factory=list)
    ym2610_b: bool = False
    ym2610_ssg_count: int = 0
    ym2610_a_samples: list[AdpcmASample] = field(default_factory=list)
    ym2610_b_samples: list[AdpcmBSample] = field(default_factory=list)
    ym2610_a_count: int = 0
    ym2610_b_count: int = 0

    # YM2608 (OPNA) instances. Each one owns 16 channels (6 FM + 3 SSG +
    # 6 rhythm + 1 ADPCM-B), or 19 with channel 3 split into four operator
    # columns. The rhythm voices are the chip's own ROM, so only the delta-T
    # channel carries samples.
    ym2608_ids: list[int] = field(default_factory=list)
    ym2608_ext: bool = False
    ym2608_ssg_count: int = 0
    ym2608_rhythm_count: int = 0
    # Rhythm keys that became a note in the module. The writer needs this to
    # decide whether to emit the kit's instrument, because a muted voice is
    # collected but never written.
    ym2608_rhythm_notes: int = 0
    ym2608_b_samples: list[AdpcmBSample] = field(default_factory=list)
    ym2608_b_count: int = 0
    # The Y8950's one ADPCM (delta-T) voice, using the same sample-map format.
    y8950_b_samples: list[AdpcmBSample] = field(default_factory=list)
    y8950_b_count: int = 0
    # The YMF278B/OPL4 wave slots, extracted from the YRW801/ROM or the RAM.
    opl4_wave_samples: list[Opl4WaveSample] = field(default_factory=list)
    opl4_wave_count: int = 0

    # K007232 output level relative to the YM2151, from the rip's own mix
    # (extra header) when present. Written to the .fur chip output.
    k007232_volume: float = 0.20
    # Set by loop finding. The row `Bxx` jumps from. None keeps the jump on
    # the last content row, which is the VGM loop length.
    loop_end_sample: int | None = None
    # The chosen rows. The writer uses these directly: re-deriving them from
    # the sample points can round to a neighbouring row after the row length
    # is calibrated to the loop.
    loop_start_row: int | None = None
    loop_end_row: int | None = None
    # The unquantized source marker. Events after it belong to the loop body
    # even when the coarser condensed row would put them in the row before
    # the chosen start.
    loop_marker_sample: int | None = None
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
    # SSG/PSG PCM streams: volume-register spamming while the voice's mixer
    # bits are off. Each body becomes a DIV_INS_AY sample instrument played at
    # note 108 (C-4), matching the reference modules. One instance per body;
    # `ssg_pcm_count` notes.
    ssg_pcm_samples: list[SsgPcmSample] = field(default_factory=list)
    ssg_pcm_count: int = 0
    # NES APU: five channels per instance. `nes_ins` holds the one plain tone
    # instrument; DPCM bodies are 1-bit (depth 1) and direct `$4011` bodies are
    # 8-bit PCM, each with one sample instrument.
    nes_ids: list[int] = field(default_factory=list)
    nes_ins: list[ToneIns] = field(default_factory=list)
    nes_dpcm_samples: list[NesDpcmSample] = field(default_factory=list)
    nes_pcm_samples: list[NesPcmSample] = field(default_factory=list)
    nes_count: int = 0
    nes_pcm_count: int = 0
    # HuC6280 (PC Engine): six wavetable voices per instance. `huc6280_waves`
    # is the deduplicated 32x5-bit wavetable list, `huc6280_ins` one instrument
    # per distinct (wave, pitch macro, wave macro), `huc6280_count` notes.
    # Direct-D/A stream bodies become 8-bit sample instruments.
    huc6280_ids: list[int] = field(default_factory=list)
    huc6280_waves: list[bytes] = field(default_factory=list)
    huc6280_ins: list[ToneIns] = field(default_factory=list)
    huc6280_pcm_samples: list[Huc6280PcmSample] = field(default_factory=list)
    huc6280_count: int = 0
    huc6280_pcm_count: int = 0
    # Game Boy DMG: four voices (two pulses, wave, noise). `gb_waves` is the
    # deduplicated 32-nibble wavetable list, `gb_ins` one instrument per
    # distinct (wave, envelope, macros) and `gb_count` the note total.
    gb_ids: list[int] = field(default_factory=list)
    gb_waves: list[bytes] = field(default_factory=list)
    gb_ins: list[ToneIns] = field(default_factory=list)
    gb_count: int = 0
    # Virtual Boy VSU: five wavetable voices and one noise voice.  `vsu_waves`
    # is the deduplicated 32x6-bit image list, `vsu_mod_waves` the 32-byte
    # modulation tables, `vsu_ins` one instrument per distinct volume/pitch
    # shape.  Wave changes ride the pattern as 10xx effects.
    vsu_ids: list[int] = field(default_factory=list)
    vsu_waves: list[bytes] = field(default_factory=list)
    vsu_mod_waves: list[bytes] = field(default_factory=list)
    vsu_ins: list[ToneIns] = field(default_factory=list)
    vsu_count: int = 0
    vsu_dynamic: bool = False
    # Philips SAA1099: six square/noise voices per chip.  A CMS/Game Blaster
    # log normally has two chips.  `saa_ins` holds the pitch, raw-amplitude
    # panning and envelope macros.
    saa_ids: list[int] = field(default_factory=list)
    saa_ins: list[ToneIns] = field(default_factory=list)
    saa_count: int = 0
    # Genesis SN76489: four channels (three tone + noise) after the FM block.
    psg_ids: list[int] = field(default_factory=list)
    psg_ins: list[ToneIns] = field(default_factory=list)
    psg_count: int = 0
    # The source wrote Game Gear stereo masks; the SMS system is the Game Gear
    # variant and the pattern carries 08xy per note.
    psg_stereo: bool = False
    # T6W28: one stereo chip made of the two SN76489 halves.  The module uses
    # Furnace's T6W28 system (four channels) and independent 08xy panning.
    psg_t6w28: bool = False
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


def _saa_tick_interval(vgm: VgmFile) -> float | None:
    """Most common gap between SAA1099 writes, in samples.

    A Furnace export writes its macros once per tick; the CMS rips do the
    same at the driver's frame rate.  Using that gap as one row keeps the
    macros and the pattern grid on the source's own clock instead of guessing
    a 16th from the note onsets.
    """
    times = [w.sample for w in vgm.writes if w.chip == "saa1099"]
    if len(times) < 16:
        return None
    intervals = [
        b - a for a, b in zip(times, times[1:])
        if 64 <= b - a <= 20_000
    ]
    if len(intervals) < 8:
        return None
    counts = Counter(intervals)
    mode, hits = counts.most_common(1)[0]
    if hits < 3:
        return None
    # Fold near neighbours (a driver that writes a register a sample or two
    # after the tick) into the same bucket.
    near = [
        value for value in counts
        if abs(value - mode) <= max(1, mode // 64)
    ]
    total = sum(counts[value] for value in near)
    if total < 3:
        return None
    # Keep the fractional tick: a 60/72 Hz driver alternates 612/613-sample
    # gaps, and rounding to the mode would drift by half a sample per tick.
    return sum(value * counts[value] for value in near) / total


def _tone_pitch_tick_scale(vgm: VgmFile, hz: float, speed: int) -> int:
    """Raise hz and speed until tone pitch macros see every write.

    The row length stays the same and the tick gets finer. A driver that
    writes the period faster than the module tick would otherwise alias.
    """
    groups: dict[tuple[str, int, int], list[int]] = {}
    vsu_env = 0.0
    for w in vgm.writes:
        if w.chip == "vsu":
            reg = w.reg
            if reg < 0xA0:
                # A wave rewrite is a burst; keep one time per settled image.
                key = (w.chip, w.chip_id, reg >> 5)
                times = groups.setdefault(key, [])
                if not times or w.sample - times[-1] > VSU_WAVE_BURST:
                    times.append(w.sample)
            elif 0x100 <= reg < 0x160:
                ch = (reg - 0x100) >> 4
                r = (reg - 0x100) & 0x0F
                if r == 2:
                    groups.setdefault(
                        ("vsu-freq", w.chip_id, ch), [],
                    ).append(w.sample)
                elif r in (4, 5):
                    vsu_env = (
                        VSU_ENVELOPE_CLOCKS * VGM_RATE
                        / float(vgm.vsu_clock or VSU_DEFAULT_CLOCK)
                    )
            continue
        if w.chip in ("ym2203_ssg", "ay8910") and w.reg % 2 == 0 and w.reg < 6:
            key = (w.chip, w.chip_id, w.reg // 2)
        elif w.chip == "saa1099" and 8 <= w.reg <= 13:
            key = (w.chip, w.chip_id, w.reg - 8)
        elif w.chip == "saa1099" and 0x10 <= w.reg <= 0x12:
            key = (w.chip, w.chip_id, w.reg)
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
    if vsu_env:
        # The hardware envelope can step faster than the note-write lattice.
        intervals += [max(1, int(round(vsu_env)))] * 8
    if len(intervals) < 8:
        return 1
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if vsu_env:
        # The envelope step is a hard timing requirement, not just another
        # note lattice: a 769-sample tick would miss a 1.3k-sample decay.
        median = min(median, max(1, int(round(vsu_env))))
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
        # Only a change within the first tick is the note settling. A note
        # that holds its pitch and then moves once (a delayed slide or a
        # portamento to the next note) is real motion: folding it into E5
        # would retune the whole note, which was audible on the NES triangle.
        if i > 1:
            return pitch
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
    *, merge_gap: int = 0,
) -> tuple[tuple[int, ...], int, int]:
    """A per-tick macro, or nothing when the register never moves.

    ``merge_gap`` folds a register burst into one update, keeping the first
    write's time and the last write's value. A driver often puts the real
    noise period or envelope byte a sample or two after the gate; without
    this the first tick would play the stale value from the previous hit,
    which is especially audible on a snare transient.
    """
    if merge_gap:
        merged: list[tuple[int, int]] = []
        for t, v in pts:
            if merged and t - merged[-1][0] <= merge_gap:
                merged[-1] = (merged[-1][0], v)
            else:
                merged.append((t, v))
        pts = merged
    if len(pts) < 2 or all(v == pts[0][1] for _t, v in pts):
        return (), 1, 255
    return _macro_series(_tick_values(pts, start, ticks, tick_samples))


def _vsu_sw_effect(sweep: int, ev1: int) -> tuple[int, int]:
    """Map VSU channel-4 SweepControl/EV1 to a Furnace VBOY effect.

    EV1 bit 4 enables modulation, bit 5 resets its phase, bit 6 enables the
    sweep/mod block.  ``14xy`` takes the modulation period in y and the
    enable/reset flags in x; ``13xy`` writes the raw sweep byte.
    """
    mod = bool(ev1 & 0x10)
    reset = bool(ev1 & 0x20)
    sweep_on = bool(ev1 & 0x40)
    period = (sweep >> 4) & 0x07
    if period == 0:
        return (0x13, 0)
    if mod and sweep_on:
        return (
            0x14,
            ((3 if reset else 1) << 4) | ((sweep >> 4) & 0x0F),
        )
    if sweep_on:
        return (0x13, sweep & 0xFF)
    return (0x13, 0)


def _plain_tone(spec: ToneIns, tone_chip: int) -> ToneIns:
    """The same timbre with no per-note motion, so notes can share it."""
    if tone_chip in (1, 5):
        # SCC and PCE are wavetable chips: the wave is the timbre.
        return ToneIns(wave=spec.wave)
    if tone_chip == 7:
        # Keep the starting waveform and modulation table so a budget
        # fallback is still audible; per-note motion is lost.
        return ToneIns(wave=spec.wave, vsu_mod=spec.vsu_mod)
    if tone_chip == 6:
        # Game Boy: a wavetable voice keeps its waveform; a pulse/noise voice
        # keeps the hardware envelope (without the sample-level macros).
        if getattr(spec, "gb_wave", False):
            return ToneIns(wave=spec.wave, gb_wave=True)
        return ToneIns(
            gb_env_vol=spec.gb_env_vol,
            gb_env_dir=spec.gb_env_dir,
            gb_env_len=spec.gb_env_len,
            gb_sound_len=spec.gb_sound_len,
        )
    return ToneIns()


def _tone_macro_close(
    a: tuple[int, ...], b: tuple[int, ...], tol: int,
) -> bool:
    """True when two per-tick macros stay within `tol` of each other.

    A shorter macro holds its last value after it runs out, so a longer one
    may share it when its extra tail stays near that final value. This is
    what lets envelopes that differ only in how long they hold silence merge.
    """
    if len(a) == len(b):
        return all(abs(x - y) <= tol for x, y in zip(a, b))
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if any(abs(a[i] - b[i]) > tol for i in range(n)):
        return False
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return all(abs(v - shorter[-1]) <= tol for v in longer[n:])


def _tone_close(a: ToneIns, b: ToneIns, tol: int) -> bool:
    """Whether two tone instruments can share one macro set at `tol`.

    Everything that is not the two continuous macros must be identical: a
    different wave, noise mode, duty/envelope shape, register speed or loop
    point is a different instrument, not a near-duplicate.
    """
    exact = (
        "wave", "waves", "wave_speed", "wave_loop",
        "pitch_speed", "pitch_loop",
        "noise", "noise_speed", "noise_loop",
        "vol_speed", "vol_loop",
        "mode", "mode_speed", "mode_loop",
        "env_shape", "env_shape_speed", "env_shape_loop",
        "env_period", "env_period_speed", "env_period_loop",
        "panl_speed", "panl_loop", "panr_speed", "panr_loop",
        "env", "env_speed", "env_loop",
        "gb_soft_env", "gb_env_vol", "gb_env_dir", "gb_env_len",
        "gb_sound_len", "gb_wave", "vsu_mod",
    )
    if any(getattr(a, f) != getattr(b, f) for f in exact):
        return False
    return (
        _tone_macro_close(a.pitch, b.pitch, tol)
        and _tone_macro_close(a.vol, b.vol, tol)
        and _tone_macro_close(a.panl, b.panl, tol)
        and _tone_macro_close(a.panr, b.panr, tol)
    )


def _take_tone(
    spec: ToneIns,
    mapping: dict[ToneIns, int],
    bucket: list[ToneIns],
    others: int,
    cap: int,
    tone_chip: int,
    merge_tol: int = 0,
) -> tuple[int, ToneIns]:
    """Index of `spec` in `bucket`.

    Past the instrument cap, share a plain instrument of the same wave.
    When the budget is tight, `merge_tol` lets a new note reuse an existing
    instrument whose continuous macros are within that many register steps.
    Envelope differences of one 0-15 level are inaudible next to the cost of
    losing the macro entirely.
    """
    found = mapping.get(spec)
    if found is not None:
        return found, spec
    if merge_tol > 0:
        for i, existing in enumerate(bucket):
            if _tone_close(spec, existing, merge_tol):
                mapping[spec] = i
                return i, existing
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
    if not bucket:
        # The budget is already spent on the earlier families. This family
        # still needs one instrument for its notes to point at.
        mapping[plain] = 0
        bucket.append(plain)
        return 0, plain
    return 0, bucket[0]


_TONE_MACRO_FIELDS = (
    "pitch", "waves", "vol", "noise", "mode", "env_shape", "env_period",
    "panl", "panr", "env",
)
_TONE_MACRO_TIMING = (
    ("pitch", "pitch_speed", "pitch_loop"),
    ("waves", "wave_speed", "wave_loop"),
    ("vol", "vol_speed", "vol_loop"),
    ("noise", "noise_speed", "noise_loop"),
    ("mode", "mode_speed", "mode_loop"),
    ("env_shape", "env_shape_speed", "env_shape_loop"),
    ("env_period", "env_period_speed", "env_period_loop"),
    ("panl", "panl_speed", "panl_loop"),
    ("panr", "panr_speed", "panr_loop"),
    ("env", "env_speed", "env_loop"),
)


def _tone_sig(spec: ToneIns) -> tuple:
    """Everything about a tone instrument except its macro values."""
    skip = set(_TONE_MACRO_FIELDS)
    skip.update(("pitch_delay", "vol_delay"))
    for _field, speed, loop in _TONE_MACRO_TIMING:
        skip.add(speed)
        skip.add(loop)
    return tuple(
        (name, getattr(spec, name))
        for name in spec.__dataclass_fields__ if name not in skip
    )


def _tone_macro_extends(a: ToneIns, b: ToneIns) -> bool:
    """True when every macro of `a` extends the matching macro of `b`."""
    for field in _TONE_MACRO_FIELDS:
        va = tuple(getattr(a, field))
        vb = tuple(getattr(b, field))
        if bool(va) != bool(vb) or len(va) < len(vb):
            return False
        if va[:len(vb)] != vb:
            return False
    for field, speed, loop in _TONE_MACRO_TIMING:
        if getattr(a, field) or getattr(b, field):
            if (getattr(a, speed) != getattr(b, speed)
                    or getattr(a, loop) != getattr(b, loop)):
                return False
    return True


def _tone_macro_same_for(a: ToneIns, b: ToneIns, ticks: int) -> bool:
    """Whether `a` and `b` play the same tone macros for `ticks` ticks.

    Each side uses its own speed, loop and delay.  Evaluating the longer
    macro with the shorter macro's timing made a fast looping tail look like
    a slow prefix; notes then inherited a late pitch/LFO tail they never had.
    """
    for field, speed_field, loop_field in _TONE_MACRO_TIMING:
        if (
            _macro_effective(
                tuple(getattr(a, field)),
                getattr(a, speed_field),
                getattr(a, loop_field),
                ticks,
                getattr(a, f"{field}_delay", 0),
            )
            != _macro_effective(
                tuple(getattr(b, field)),
                getattr(b, speed_field),
                getattr(b, loop_field),
                ticks,
                getattr(b, f"{field}_delay", 0),
            )
        ):
            return False
    return True


def _tone_macro_size(spec: ToneIns) -> int:
    return sum(len(getattr(spec, field)) for field in _TONE_MACRO_FIELDS)


def _delay_leading_values(values, speed: int) -> tuple[tuple, int]:
    """Collapse a leading run into one value plus a delay in ticks."""
    values = tuple(values)
    if not values:
        return (), 0
    run = 1
    while run < len(values) and values[run] == values[0]:
        run += 1
    if run <= 1:
        return values, 0
    hold = run * max(1, speed)
    if hold <= 256:
        return (values[0],) + values[run:], hold - 1
    extra = hold - 256
    keep = 1 + (extra + speed - 1) // max(1, speed)
    return (values[0],) * keep + values[run:], 255


def _compress_tone_delays(bucket: list[ToneIns]) -> None:
    """Make every leading-hold macro use the delay field before grouping."""
    for i, spec in enumerate(bucket):
        if not (
            spec.pitch or spec.vol or spec.panl or spec.panr or spec.env
        ):
            continue
        changes: dict[str, object] = {}
        for field, speed, loop in _TONE_MACRO_TIMING:
            values = getattr(spec, field)
            if not values or getattr(spec, loop) != 255:
                continue
            delay_field = f"{field}_delay"
            if not hasattr(spec, delay_field) or getattr(spec, delay_field):
                continue
            short, delay = _delay_leading_values(values, getattr(spec, speed))
            if delay:
                changes[field] = short
                changes[delay_field] = delay
        if changes:
            bucket[i] = replace(spec, **changes)


def _tone_vol_sig(spec: ToneIns) -> tuple:
    """Everything about a tone instrument except its volume macro."""
    skip = {"vol", "vol_speed", "vol_loop", "vol_delay"}
    return tuple(
        (name, getattr(spec, name))
        for name in spec.__dataclass_fields__ if name not in skip
    )


# A macro level is 0-15, so 15 is the whole range. The note column is the
# real floor: a quieter copy is skipped when its column cannot be lowered by
# the full offset without going below 0.
_AY_VOL_SPREAD = 15


def _vol_offset(loud: ToneIns, quiet: ToneIns) -> int | None:
    """Constant 1..15-level difference from `loud` to `quiet`, else None."""
    a = tuple(loud.vol)
    b = tuple(quiet.vol)
    if (
        not a or not b
        or loud.vol_speed != quiet.vol_speed
        or loud.vol_loop != quiet.vol_loop
    ):
        return None
    if loud.vol_loop != 255:
        # A loop's phase is not worth reconciling here; equal macros are
        # handled by the generic prefix merge.
        return None
    delay_a = getattr(loud, "vol_delay", 0)
    delay_b = getattr(quiet, "vol_delay", 0)
    speed = max(1, loud.vol_speed)
    horizon = max(len(a), len(b)) * speed + max(delay_a, delay_b) + 1
    ea = _macro_effective(a, speed, 255, horizon, delay_a)
    eb = _macro_effective(b, speed, 255, horizon, delay_b)
    delta = ea[0] - eb[0]
    if delta < 1 or delta > _AY_VOL_SPREAD:
        return None
    if any(x - y != delta for x, y in zip(ea, eb)):
        return None
    return delta


def _merge_ay_volume_offsets(
    bucket: list[ToneIns], notes: list[tuple[NoteEvent, int]],
) -> dict[int, int]:
    """Share one AY/SSG volume macro when copies differ by a constant offset.

    The AY platform adds the pattern column to the macro
    (``macro + column - 15``), so a quieter macro can ride the same
    instrument by lowering the note's column by the same constant. The
    difference has to be consistent at every macro step; a varying shape is
    a different envelope, not a normalization. There is no spread ceiling
    below the macro's own 0-15 range: the note column is the floor, and a
    copy is left alone when it cannot be lowered by the full offset. Keep
    the loudest macro so every note still reaches the chip's maximum level.
    """
    if len(bucket) < 2:
        return {i: i for i in range(len(bucket))}
    groups: dict[tuple, list[int]] = {}
    for i, spec in enumerate(bucket):
        groups.setdefault(_tone_vol_sig(spec), []).append(i)
    center_of: dict[int, int] = {i: i for i in range(len(bucket))}
    volume_notes: dict[int, list[NoteEvent]] = {}
    for event, idx in notes:
        volume_notes.setdefault(idx, []).append(event)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        # Keep the loudest macro: compensating columns only move down. The
        # other direction would need a column above 15, which the platform
        # cannot reach.
        pending = sorted(
            indexes, key=lambda i: (-sum(bucket[i].vol), i)
        )
        while pending:
            center = pending.pop(0)
            cluster = [center]
            rest: list[int] = []
            for i in pending:
                if _vol_offset(bucket[center], bucket[i]) is not None:
                    cluster.append(i)
                else:
                    rest.append(i)
            pending = rest
            if len(cluster) < 2:
                continue
            for i in cluster[1:]:
                delta = _vol_offset(bucket[center], bucket[i])
                assert delta is not None
                if any(ev.vol < delta for ev in volume_notes.get(i, ())):
                    continue
                for ev in volume_notes.get(i, ()):
                    ev.vol = max(0, min(15, ev.vol - delta))
                center_of[i] = center
    mapping: dict[int, int] = {}
    kept: list[ToneIns] = []
    for i in range(len(bucket)):
        if center_of[i] == i:
            mapping[i] = len(kept)
            kept.append(bucket[i])
    for i in range(len(bucket)):
        mapping[i] = mapping[center_of[i]]
    if len(kept) != len(bucket):
        bucket[:] = kept
    return mapping


def _merge_tone_prefixes(
    bucket: list[ToneIns],
    notes_by_ins: dict[int, list[tuple[int, int]]] | None = None,
    tick_samples: float = 0.0,
    use_speed: int = 1,
) -> dict[int, int]:
    """Share the longest left-to-right-compatible tone macro.

    Volume and pitch envelopes often differ only by how long they continue:
    a short note's copy is the prefix of the sustained one. Keep the longer
    and point every note at it; the next key-on cuts a short note before the
    extra tail matters. Returns the old-index -> new-index map.
    """
    if len(bucket) < 2:
        return {i: i for i in range(len(bucket))}
    groups: dict[tuple, list[int]] = {}
    for i, spec in enumerate(bucket):
        groups.setdefault(_tone_sig(spec), []).append(i)

    def extends(j: int, i: int) -> bool:
        notes = (notes_by_ins or {}).get(i, ())
        if not notes:
            return False
        for start, end in notes:
            if end <= 0:
                return False
            ticks = _macro_ticks(start, end, tick_samples)
            if not _tone_macro_same_for(bucket[j], bucket[i], ticks):
                return False
        return True

    target = {i: i for i in range(len(bucket))}
    for indexes in groups.values():
        if len(indexes) > 1 and any(
            _tone_macro_size(bucket[i]) > 0 for i in indexes
        ):
            target.update(_prefix_targets(
                indexes,
                lambda i: _tone_macro_size(bucket[i]),
                extends,
            ))
    mapping: dict[int, int] = {}
    kept: list[ToneIns] = []
    for i in range(len(bucket)):
        if target[i] == i:
            mapping[i] = len(kept)
            kept.append(bucket[i])
    for i in range(len(bucket)):
        mapping[i] = mapping[target[i]]
    if len(kept) != len(bucket):
        bucket[:] = kept
    return mapping


def _loop_saa_pitch_macros(
    bucket: list[ToneIns], lfo_loop: bool = True,
) -> None:
    """Loop a periodic SAA pitch macro independently of its other macros.

    Prefix grouping can keep a longer copy of a macro whose periodic prefix
    was already a cycle.  Pseudo-duty detunes are pitch-only, so loop
    detection does not depend on pan, envelope or mode macros being present.
    Step-length compression stays off: it would change the duty ratio.
    """
    if not lfo_loop:
        return
    for i, ins in enumerate(bucket):
        if not ins.pitch or ins.pitch_loop != 255:
            continue
        values, speed, loop = _macro_series(
            list(ins.pitch), tail_loop=True,
            initial_speed=ins.pitch_speed, tail_min=16,
        )
        if (
            loop != 255
            and speed == ins.pitch_speed
            and len(values) < len(ins.pitch)
        ):
            bucket[i] = replace(ins, pitch=values, pitch_loop=loop)


def _bind_tone_instruments(
    events: list[NoteEvent],
    scc_ins: list[ToneIns],
    ay_ins: list[ToneIns],
    psg_ins: list[ToneIns],
    nes_ins: list[ToneIns],
    huc6280_ins: list[ToneIns],
    gb_ins: list[ToneIns],
    vsu_ins: list[ToneIns],
    saa_ins: list[ToneIns],
    tone_ins_base: int,
    scc_clock: int,
    ay_clock: int,
    psg_clock: int,
    nes_clock: int,
    huc6280_clock: int,
    gb_clock: int,
    vsu_clock: int,
    saa_clock: int,
    hz: float,
    total_samples: int,
    use_speed: int = 1,
    merge_tol: int = 0,
    lfo_loop: bool = True,
) -> int:
    """Fill `scc_ins` / `ay_ins` and point each tone note at one.

    Pitch, wave and AY register motion that changes during a note become
    macros. AY notes bind first. The stored indices follow the SCC list.
    `merge_tol` > 0 lets near-identical continuous macros share an
    instrument when the module is close to the instrument budget. Returns
    the number of notes that reused an existing instrument this way.
    """
    if not any(e.tone_chip for e in events):
        return 0
    tick_samples = VGM_RATE / hz if hz else VGM_RATE / 60.0
    track_end = total_samples or max((e.sample for e in events), default=0)
    scc_map: dict[ToneIns, int] = {}
    ay_map: dict[ToneIns, int] = {}
    psg_map: dict[ToneIns, int] = {}
    nes_map: dict[ToneIns, int] = {}
    huc6280_map: dict[ToneIns, int] = {}
    gb_map: dict[ToneIns, int] = {}
    vsu_map: dict[ToneIns, int] = {}
    saa_map: dict[ToneIns, int] = {}
    ins_cap = max(1, INS_TARGET - tone_ins_base)
    # GB and VSU bind last. Reserve a share so a large SCC/AY/PCE list cannot
    # spend every slot before them; the surplus collapses to plain
    # per-wave/per-env instruments, the documented "fall back to columns"
    # behavior.
    has_late = any(
        e.tone_chip in (6, 7, 8) and e.on for e in events
    )
    early_cap = ins_cap
    if has_late:
        families = len({e.tone_chip for e in events if e.on and e.tone_chip})
        early_cap = max(1, ins_cap - max(8, ins_cap // max(2, families)))
    bound: list[tuple[NoteEvent, int]] = []
    merged = 0

    def take(spec, mapping, bucket, others, cap, chip):
        nonlocal merged
        idx, kept = _take_tone(
            spec, mapping, bucket, others, cap, chip, merge_tol,
        )
        if merge_tol > 0 and kept != spec:
            merged += 1
        return idx, kept

    tone_order = [e for e in events if e.on and e.tone_chip == 2]
    tone_order += [e for e in events if e.on and e.tone_chip == 1]
    tone_order += [e for e in events if e.on and e.tone_chip == 3]
    tone_order += [e for e in events if e.on and e.tone_chip == 4]
    tone_order += [e for e in events if e.on and e.tone_chip == 5]
    tone_order += [e for e in events if e.on and e.tone_chip == 6]
    tone_order += [e for e in events if e.on and e.tone_chip == 7]
    tone_order += [e for e in events if e.on and e.tone_chip == 8]
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
            # The T6W28 uses divider 16 (tone) or 15 (noise) at 3072000 Hz.
            divider = e.tone_div or PSG_DIVIDER
            clock = e.tone_clock or psg_clock or PSG_DEFAULT_CLOCK
            if divider == PSG_DIVIDER and abs(clock - PSG_FURNACE_CLOCK) <= 1:
                clock = PSG_FURNACE_CLOCK
            maximum = PSG_MAX_PERIOD
        elif e.tone_chip == 4:
            # The NES core runs the CPU at COLOR_NTSC/2 with a divider of 16
            # and an 11-bit timer (never a module clock).
            clock = e.tone_clock or nes_clock or NES_DEFAULT_CLOCK
            if abs(clock - NES_FURNACE_CLOCK) <= 1:
                clock = NES_FURNACE_CLOCK
            divider = NES_CPU_DIVIDER
            maximum = 0x7FF
        elif e.tone_chip == 5:
            # The PCE core's pitch table runs from the doubled PSG clock with
            # a divider of 64, which is the PSG clock with a divider of 32;
            # the period register maps straight through.
            clock = e.tone_clock or huc6280_clock or HUC6280_DEFAULT_CLOCK
            divider = 32
            maximum = HUC6280_MAX_PERIOD
        elif e.tone_chip == 6:
            # Furnace's GB core divides the 4 MHz clock by 16 and writes
            # ``2048 - freq``; the analyzer already handed us that value as
            # the note's raw period, so the table inverts it directly.
            clock = e.tone_clock or gb_clock or GB_DEFAULT_CLOCK
            divider = GB_PITCH_DIVIDER
            maximum = 0x7FF
        elif e.tone_chip == 7:
            # The VBOY platform also has a 16x divider and writes
            # ``2048 - tableValue``.  The analyzer stores the source
            # ``2048 - Frequency`` value, so the table inverts it directly.
            clock = e.tone_clock or vsu_clock or VSU_DEFAULT_CLOCK
            divider = VSU_PITCH_DIVIDER
            maximum = 0x7FF
        elif e.tone_chip == 8:
            # The SAA platform converts the pitch-table value into an 8-bit
            # offset and 3-bit octave; the collector already handed us the
            # midpoint of the bucket that maps back to the source pair.
            clock = e.tone_clock or saa_clock or SAA_DEFAULT_CLOCK
            divider = SAA_DIVIDER
            maximum = SAA_PITCH_MAX
        else:
            # AY and the YM2203's SSG share the instrument path but not the
            # clock or Furnace's divider (8 vs 16).
            clock = e.tone_clock or ay_clock
            divider = e.tone_div or 8
            maximum = 0xFFF
        if e.tone_chip == 8:
            tbl = pitch_table(saa_module_clock(clock), divider, maximum=maximum)
        else:
            tbl = (
                pitch_table(float(clock), divider, maximum=maximum)
                if e.tone_chip in (3, 4, 5, 6, 7)
                else pitch_table(module_clock(clock), divider)
            )
        base_off = (e.note << 7) + (e.e5 - E5_CENTER)
        if e.tone_chip == 5 and e.tone_pitch_pts and not e.tone_noise:
            # A PCE driver that retunes every frame writes a staircase, not a
            # vibrato. When every step is at least 0.75 semitone, put the
            # trajectory on the pattern rows as E8/E9 + E5 instead of hiding
            # it in a per-note pitch macro. The delta is in 1/64-semitone
            # units (the writer's pitch-step unit), hence the divide by two.
            steps = tuple(
                (
                    t,
                    int(round((
                        tbl.offset_for(
                            e.note, per + e.tone_period_offset,
                        ) - base_off
                    ) / 2.0)),
                )
                for t, per in e.tone_pitch_pts
            )
            if steps and all(abs(v) >= 48 for _t, v in steps):
                e.pitch_steps = steps
                e.tone_pitch_pts = ()
                e.sweep_div = 2.0
                e.e5_lo = 0x00
                e.e5_hi = 0xFF
        pitch: tuple[int, ...] = ()
        pitch_speed, pitch_loop = 1, 255
        pitch_delay = 0
        if e.tone_pitch_pts:
            pts = [(e.sample, 0)]
            if e.tone_noise:
                # PCE noise: the register follows note-60 and ``pitch2``
                # directly, so the macro value is the raw register offset.  A
                # note can toggle noise on and off several times; the mode
                # timeline says which kind each pitch point is.
                gate_n = e.note - 60
                modes = list(e.tone_noise_modes)
                mode = modes[0][1] if modes else 1
                mi = 0
                for t, per in e.tone_pitch_pts:
                    while mi + 1 < len(modes) and modes[mi + 1][0] <= t:
                        mi += 1
                        mode = modes[mi][1]
                    if mode:
                        pts.append((t, per - gate_n))
                    else:
                        pts.append((
                            t,
                            tbl.offset_for(e.note, per + e.tone_period_offset)
                            - base_off,
                        ))
            else:
                pts += [
                    (t, tbl.offset_for(e.note, per + e.tone_period_offset)
                     - base_off)
                    for t, per in e.tone_pitch_pts
                ]
            series = _tick_values(pts, e.sample, ticks, tick_samples)
            if any(series):
                if e.tone_noise:
                    # A noise pitch is a register offset, not a tone bend;
                    # leave its leading values uncollapsed.
                    pitch, pitch_speed, pitch_loop = _macro_series(series)
                elif e.tone_chip == 8:
                    # A SAA1099 driver uses fast detunes as a pseudo-duty
                    # cycle; thinning or step-compressing them changes the
                    # duty ratio.  Keep the tick-exact series, but still loop
                    # an exact periodic tail.
                    pitch, pitch_speed, pitch_loop, pitch_delay = (
                        _macro_series_delay(
                            series, tail_loop=lfo_loop, step_compress=False,
                        )
                    )
                    if pitch_delay == 0:
                        pitch = _absorb_detune(e, pitch, pitch_speed)
                else:
                    pitch, pitch_speed, pitch_loop, pitch_delay = (
                        _macro_series_delay(
                            series, tail_loop=lfo_loop, step_compress=True,
                        )
                    )
                    # A delayed first value is held long enough to be a real
                    # step, not an attack settle; folding its tail into E5
                    # would drop the held level. Only absorb a settle when
                    # there is no delay.
                    if pitch_delay == 0:
                        # Folding a constant offset into E5 would be lost on
                        # a noise note: the register reads ``pitch2``.
                        pitch = _absorb_detune(e, pitch, pitch_speed)
            if e.tone_chip == 8 and pitch and len(pitch) <= 8:
                # The short residual bend after a legato split is the
                # transition into the next note, not an instrument identity.
                # Put it on the pattern rows (E8/E9 + E5) so the notes of a
                # descent can share one volume/mode/envelope instrument.
                steps = tuple(
                    (
                        t,
                        int(round((
                            tbl.offset_for(
                                e.note, per + e.tone_period_offset,
                            ) - base_off
                        ) / 2.0)),
                    )
                    for t, per in e.tone_pitch_pts
                )
                steps = tuple((t, v) for t, v in steps if v)
                if steps:
                    e.pitch_steps = steps
                    e.sweep_div = 2.0
                    e.e5_lo = 0x00
                    e.e5_hi = 0xFF
                    pitch = ()
                    pitch_speed, pitch_loop, pitch_delay = 1, 255, 0
        wave_vals: tuple[int, ...] = ()
        wave_speed, wave_loop = 1, 255
        if e.tone_wave_pts:
            pts = [(e.sample, max(0, e.tone_wave))] + list(e.tone_wave_pts)
            wave_vals, wave_speed, wave_loop = _macro_series(
                _tick_values(pts, e.sample, ticks, tick_samples),
            )
        column_vol = e.vol
        pce_peak = 0
        saa_peak = 0
        vol_vals: tuple[int, ...] = ()
        vol_speed, vol_loop = 1, 255
        vol_delay = 0
        series: list[int] = []
        if e.vol_pts and e.tone_chip not in (3,) and not (
                e.tone_chip == 6 and e.tone_gb_wave):
            # PSG volume is the raw 4-bit register and the SMS platform reads
            # it straight from the column; a std.vol macro would scale it.
            # The PCE envelope benefits from the tick-rate macro: its balance
            # writes can move several times inside one pattern row.
            pts = [(e.sample, max(0, e.vol))] + [
                (t, max(0, v)) for t, v, _ in e.vol_pts
            ]
            series = _tick_values(pts, e.sample, ticks, tick_samples)
            if e.tone_chip == 5:
                # Normalize the PCE envelope to its own peak. The note column
                # then carries the peak level and the macro the decay shape,
                # so notes with different starting volumes are not all 31.
                pce_peak = max(1, series[0]) if series else 0
            if e.tone_chip == 8:
                # Same idea for the SAA level: the note column carries the
                # segment's peak and the macro the normalized shape.  Notes
                # of a descent then share one instrument, and only the pan
                # (08xy) differs per note.
                saa_peak = max(1, max(series)) if series else 0
                if saa_peak:
                    series = [
                        max(0, min(15, int(round(v * 15.0 / saa_peak))))
                        for v in series
                    ]
            if e.tone_chip == 6:
                # The GB platform applies a software-envelope volume macro
                # directly; a generic FA volume slide is not implemented there,
                # so never fold the trajectory into a slide. _macro_series
                # drops the trailing hold anyway, keeping decay macros short.
                vol_vals, vol_speed, vol_loop, vol_delay = (
                    _macro_series_delay(series)
                )
            else:
                # PCE keeps the trajectory as a normalized macro: a slide
                # would move the raw column at one unit per tick and lose the
                # peak scaling.
                folded = (
                    None if e.tone_chip in (5, 8) else _fold_saw(series)
                )
                # Past 255 ticks a non-repeating series is stored as every Nth
                # value. A one-level saw is played as a slide instead, so the
                # ramp still moves on every tick.
                thin = (
                    e.tone_chip not in (5, 8)
                    and folded is None
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
                    vol_vals, vol_speed, vol_loop, vol_delay = (
                        _macro_series_delay(series)
                    )
                    if e.tone_chip == 5 and vol_vals and pce_peak:
                        vol_vals = tuple(
                            max(0, min(31, int(round(v * 31 / pce_peak))))
                            for v in vol_vals
                        )
                        if vol_vals:
                            vol_vals = (31,) + vol_vals[1:]
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
        panl = panr = env = ()
        panl_speed = panr_speed = env_speed = 1
        panl_loop = panr_loop = env_loop = 255
        panl_delay = panr_delay = env_delay = 0
        if e.tone_chip == 8:
            # SAA1099: the amplitude register carries volume and soft pan
            # together; put the raw nibbles on the two panning macros and
            # leave the volume column at 15.
            if e.tone_amp_pts:
                pl = [(t, left) for t, left, _right in e.tone_amp_pts]
                pr = [(t, right) for t, _left, right in e.tone_amp_pts]
                pl_series = _tick_values(pl, e.sample, ticks, tick_samples)
                pr_series = _tick_values(pr, e.sample, ticks, tick_samples)
                panl, panl_speed, panl_loop, panl_delay = _macro_series_delay(
                    pl_series,
                )
                panr, panr_speed, panr_loop, panr_delay = _macro_series_delay(
                    pr_series,
                )
                # Both panning macros must be present: the platform starts
                # from pan 0xFF, and a missing panL would leave the left
                # nibble at 15 even when the source side is a constant zero.
                if not panl and pl_series:
                    panl, panl_speed, panl_loop, panl_delay = (
                        (pl_series[0],), 1, 255, 0,
                    )
                if not panr and pr_series:
                    panr, panr_speed, panr_loop, panr_delay = (
                        (pr_series[0],), 1, 255, 0,
                    )
            noise, noise_speed, noise_loop = _macro_if_moving(
                [(t, val) for t, cmd, val in e.tone_fx_pts if cmd == 0x11],
                e.sample, ticks, tick_samples, merge_gap=_AY_MERGE_GAP,
            )
            mode, mode_speed, mode_loop = _macro_if_moving(
                [(t, val & 3) for t, cmd, val in e.tone_fx_pts if cmd == 0x10],
                e.sample, ticks, tick_samples, merge_gap=_AY_MERGE_GAP,
            )
            env, env_speed, env_loop, env_delay = _macro_series_delay(
                _tick_values(
                    [(t, val) for t, cmd, val in e.tone_fx_pts if cmd == 0x12],
                    e.sample, ticks, tick_samples,
                ),
            )
        elif e.tone_fx_pts:
            noise, noise_speed, noise_loop = _macro_if_moving(
                [(t, val) for t, cmd, val in e.tone_fx_pts if cmd == FX_NOISE],
                e.sample, ticks, tick_samples, merge_gap=_AY_MERGE_GAP,
            )
            mode, mode_speed, mode_loop = _macro_if_moving(
                [(t, (val + 1) & 7) for t, cmd, val in e.tone_fx_pts if cmd == FX_MODE],
                e.sample, ticks, tick_samples, merge_gap=_AY_MERGE_GAP,
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
                merge_gap=_AY_MERGE_GAP,
            )
        if e.tone_chip == 5 and not noise:
            # Every PCE instrument carries the noise mode explicitly: 1 for a
            # noise hit, 0 (tone) otherwise. A constant mode lives on the
            # instrument so it does not depend on a row effect that may land
            # off-grid, and a following tone note always clears the previous
            # hit's noise.
            noise, noise_speed, noise_loop = (
                1 if e.tone_noise else 0,
            ), 1, 255
        spec = ToneIns(
            wave=max(0, e.tone_wave),
            pitch=pitch, pitch_speed=pitch_speed, pitch_loop=pitch_loop,
            pitch_delay=pitch_delay,
            waves=wave_vals, wave_speed=wave_speed, wave_loop=wave_loop,
            vol=vol_vals, vol_speed=vol_speed, vol_loop=vol_loop,
            vol_delay=vol_delay,
            noise=noise, noise_speed=noise_speed, noise_loop=noise_loop,
            mode=mode, mode_speed=mode_speed, mode_loop=mode_loop,
            env_shape=env_shape, env_shape_speed=env_shape_speed,
            env_shape_loop=env_shape_loop,
            env_period=env_period, env_period_speed=env_period_speed,
            env_period_loop=env_period_loop,
            panl=panl, panl_speed=panl_speed, panl_loop=panl_loop,
            panl_delay=panl_delay,
            panr=panr, panr_speed=panr_speed, panr_loop=panr_loop,
            panr_delay=panr_delay,
            env=env, env_speed=env_speed, env_loop=env_loop,
            env_delay=env_delay,
            gb_soft_env=bool(getattr(e, "gb_soft_env", False)),
            gb_env_vol=int(getattr(e, "gb_env_vol", 0)),
            gb_env_dir=int(getattr(e, "gb_env_dir", 0)),
            gb_env_len=int(getattr(e, "gb_env_len", 0)),
            gb_sound_len=int(getattr(e, "gb_sound_len", 63)),
            gb_wave=bool(e.tone_gb_wave),
            vsu_mod=int(getattr(e, "tone_mod", 0)),
        )
        if e.tone_chip == 1:
            idx, kept = take(spec, scc_map, scc_ins, len(ay_ins), early_cap, 1)
        elif e.tone_chip == 3:
            idx, kept = take(
                spec, psg_map, psg_ins, len(scc_ins) + len(ay_ins), early_cap, 3,
            )
        elif e.tone_chip == 4:
            idx, kept = take(
                spec, nes_map, nes_ins,
                len(scc_ins) + len(ay_ins) + len(psg_ins), early_cap, 4,
            )
        elif e.tone_chip == 5:
            idx, kept = take(
                spec, huc6280_map, huc6280_ins,
                len(scc_ins) + len(ay_ins) + len(psg_ins) + len(nes_ins),
                early_cap, 5,
            )
        elif e.tone_chip == 6:
            idx, kept = take(
                spec, gb_map, gb_ins,
                len(scc_ins) + len(ay_ins) + len(psg_ins) + len(nes_ins)
                + len(huc6280_ins),
                ins_cap, 6,
            )
        elif e.tone_chip == 7:
            idx, kept = take(
                spec, vsu_map, vsu_ins,
                len(scc_ins) + len(ay_ins) + len(psg_ins) + len(nes_ins)
                + len(huc6280_ins) + len(gb_ins),
                ins_cap, 7,
            )
        elif e.tone_chip == 8:
            # The SAA amplitude nibbles are only four bits wide, so a
            # one-step near-identical merge is an audible level change.
            # Keep the pan/volume macros exact; only the prefix merge below
            # may share them.
            idx, kept = _take_tone(
                spec, saa_map, saa_ins,
                len(scc_ins) + len(ay_ins) + len(psg_ins) + len(nes_ins)
                + len(huc6280_ins) + len(gb_ins) + len(vsu_ins),
                ins_cap, 8, 0,
            )
        else:
            # AY runs first, so the SCC list is still empty and `others` is 0.
            idx, kept = take(spec, ay_map, ay_ins, 0, early_cap, 2)
        bound.append((e, idx))
        if (
            e.tone_chip in (3, 7)
            and not e.tone_noise
            and kept != spec
            and not kept.pitch
            and e.tone_pitch_pts
        ):
            # The tone instrument list is full, so this note fell back to the
            # plain instrument. Put the period trajectory on E5 row steps
            # instead of silently dropping it.
            steps = tuple(
                (
                    t,
                    int(round(
                        tbl.offset_for(
                            e.note, per + e.tone_period_offset,
                        ) - base_off
                    )),
                )
                for t, per in e.tone_pitch_pts
            )
            steps = tuple((t, v) for t, v in steps if v)
            if steps:
                e.pitch_steps = steps
        if e.tone_chip == 4 and kept.pitch:
            # The instrument now carries the pitch trajectory; the row ramp
            # would double it.
            e.sweep = ()
        if e.tone_chip == 5:
            # When the envelope became an instrument macro, the column is
            # the macro's full-scale reference (31). Otherwise the PCE column
            # is the control volume and `vol_pts` carry the balance/volume
            # steps as row effects.
            if kept.vol:
                e.vol_pts = ()
                e.vol = pce_peak if pce_peak else 31
                e.vol_slide = 0
            else:
                e.vol = column_vol
        elif e.tone_chip == 8:
            # The SAA volume macro is normalized to its segment peak, so the
            # note column carries that peak.  The balance is already the
            # note's 08xy state, not part of the instrument.
            e.vol_pts = ()
            e.vol = saa_peak if (kept.vol and saa_peak) else column_vol
            e.vol_slide = 0
        elif e.tone_chip == 3 or (e.tone_chip == 6 and e.tone_gb_wave):
            # The SMS column is the raw 4-bit register; keep vol_pts as
            # pattern steps rather than folding them into a volume macro.
            # The GB wave column is the NR32 level (0/5/10/15), same idea.
            pass
        elif kept.vol:
            # The column that makes the macro the register: SCC vol*macro/15,
            # AY macro + column - 15, PCE vol*macro/31.
            e.vol_pts = ()
            e.vol = 31 if e.tone_chip == 5 else 15
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
    # Re-apply the leading-delay compression to every final tone macro
    # before grouping. A macro that was copied or shared can still carry the
    # literal leading repeats; compressing here keeps the compact form and
    # stops the grouping pass from choosing the longer, uncompressed copy.
    for bucket in (
        scc_ins, ay_ins, psg_ins, nes_ins, huc6280_ins, gb_ins,
        vsu_ins, saa_ins,
    ):
        _compress_tone_delays(bucket)

    # AY/SSG volume envelopes that differ by a constant 1 or 2 can share the
    # loudest instrument; the quieter notes ride the volume column. This runs
    # before the generic prefix merge so the reduced volume list is what gets
    # compared.
    ay_notes = [(ev, idx) for ev, idx in bound if ev.tone_chip == 2]
    ay_vol_map = _merge_ay_volume_offsets(ay_ins, ay_notes)
    if any(old != new for old, new in ay_vol_map.items()):
        for i, (event, idx) in enumerate(bound):
            if event.tone_chip == 2:
                bound[i] = (event, ay_vol_map.get(idx, idx))

    # Merge truncated macro copies before fixing the absolute indices. Each
    # family has its own list, so shrinking one only moves that family's
    # indices; the base offsets are computed from the final lengths below.
    family_notes: dict[int, dict[int, list[tuple[int, int]]]] = {}
    for event, idx in bound:
        family_notes.setdefault(event.tone_chip, {}).setdefault(
            idx, []).append((event.sample, event.end or track_end))
    tone_maps = {
        1: _merge_tone_prefixes(
            scc_ins, family_notes.get(1), tick_samples, use_speed),
        2: _merge_tone_prefixes(
            ay_ins, family_notes.get(2), tick_samples, use_speed),
        3: _merge_tone_prefixes(
            psg_ins, family_notes.get(3), tick_samples, use_speed),
        4: _merge_tone_prefixes(
            nes_ins, family_notes.get(4), tick_samples, use_speed),
        5: _merge_tone_prefixes(
            huc6280_ins, family_notes.get(5), tick_samples, use_speed),
        6: _merge_tone_prefixes(
            gb_ins, family_notes.get(6), tick_samples, use_speed),
        7: _merge_tone_prefixes(
            vsu_ins, family_notes.get(7), tick_samples, use_speed),
        8: _merge_tone_prefixes(
            saa_ins, family_notes.get(8), tick_samples, use_speed),
    }
    for i, (event, idx) in enumerate(bound):
        mapping = tone_maps.get(event.tone_chip)
        if mapping is not None:
            bound[i] = (event, mapping.get(idx, idx))
    _loop_saa_pitch_macros(saa_ins, lfo_loop)
    n_scc = len(scc_ins)
    n_ay = len(ay_ins)
    n_psg = len(psg_ins)
    n_nes = len(nes_ins)
    n_huc = len(huc6280_ins)
    n_gb = len(gb_ins)
    n_vsu = len(vsu_ins)
    for event, idx in bound:
        if event.tone_chip == 1:
            event.ins = tone_ins_base + idx
        elif event.tone_chip == 3:
            event.ins = tone_ins_base + n_scc + n_ay + idx
        elif event.tone_chip == 4:
            event.ins = tone_ins_base + n_scc + n_ay + n_psg + idx
        elif event.tone_chip == 5:
            event.ins = tone_ins_base + n_scc + n_ay + n_psg + n_nes + idx
        elif event.tone_chip == 6:
            event.ins = (
                tone_ins_base + n_scc + n_ay + n_psg + n_nes + n_huc + idx
            )
        elif event.tone_chip == 7:
            event.ins = (
                tone_ins_base + n_scc + n_ay + n_psg + n_nes + n_huc
                + n_gb + idx
            )
        elif event.tone_chip == 8:
            event.ins = (
                tone_ins_base + n_scc + n_ay + n_psg + n_nes + n_huc
                + n_gb + n_vsu + idx
            )
        else:
            event.ins = tone_ins_base + n_scc + idx
    return merged


def _split_pce_volume_peaks(
    events: list[NoteEvent], clock: int, track_end: int,
) -> list[NoteEvent]:
    """Split a PCE tone gate at each volume step (and every five seconds).

    A long balance/control-volume envelope can contain dozens of steps. A
    single instrument volume macro then holds many of them and can run for
    tens of seconds, which is neither a useful instrument nor accurate at the
    row rate. Each step is the driver's next software level - in the
    percussion drivers it is the 16th-note grid, with different steps acting
    as hi-hat and snare accents - so make each one a new note. The note row
    carries that step's level and the macro only the pan/pitch inside it.
    The five-second ceiling keeps any remaining macro bounded.
    """
    cap = 5 * VGM_RATE
    out: list[NoteEvent] = []
    for ev in events:
        if not (ev.on and ev.tone_chip == 5 and ev.vol_pts):
            out.append(ev)
            continue
        end = ev.end or track_end
        if end <= ev.sample:
            out.append(ev)
            continue
        timeline = [(ev.sample, ev.vol, ev.pan)]
        timeline += [(t, v, p) for t, v, p in ev.vol_pts]
        timeline.sort(key=lambda item: item[0])
        pts: list[tuple[int, int, int]] = []
        for t, v, p in timeline:
            if pts and pts[-1][0] == t:
                pts[-1] = (t, v, p)
            else:
                pts.append((t, v, p))
        vol_at = [(t, v) for t, v, _p in pts]
        pan_at = [(t, p) for t, _v, p in pts]
        splits = [
            pts[i][0] for i in range(1, len(pts))
            if pts[i][1] != pts[i - 1][1]
        ]
        t = ev.sample
        while t + cap < end:
            t += cap
            splits.append(t)
        splits = sorted({s for s in splits if ev.sample < s < end})
        if not splits:
            out.append(ev)
            continue
        starts = [ev.sample] + splits
        for i, st in enumerate(starts):
            seg_end = starts[i + 1] if i + 1 < len(starts) else ev.end
            bound = seg_end or track_end
            vol = _value_at(vol_at, st, ev.vol)
            pan = _value_at(pan_at, st, ev.pan)
            if ev.tone_noise:
                base_noise = _value_at(
                    [(ev.sample, ev.note - 60)] + list(ev.tone_pitch_pts),
                    st, ev.note - 60,
                )
                note = huc6280_noise_note(base_noise)
                e5 = 0x80
                base_period = 0
                pitch_pts = tuple(
                    (t, n) for t, n in ev.tone_pitch_pts if st < t < bound
                )
            else:
                base_period = _value_at(
                    [(ev.sample, ev.tone_period)] + list(ev.tone_pitch_pts),
                    st, ev.tone_period,
                )
                note, e5 = huc6280_note(clock, base_period)
                pitch_pts = tuple(
                    (t, p) for t, p in ev.tone_pitch_pts if st < t < bound
                )
            wave = _value_at(
                [(ev.sample, ev.tone_wave)] + list(ev.tone_wave_pts),
                st, ev.tone_wave,
            )
            out.append(replace(
                ev,
                sample=st,
                note=note,
                e5=e5,
                vol=vol,
                pan=pan,
                end=seg_end,
                vol_pts=tuple(
                    (t, v, p) for t, v, p in pts if st < t < bound
                ),
                tone_pitch_pts=pitch_pts,
                tone_wave_pts=tuple(
                    (t, idx) for t, idx in ev.tone_wave_pts
                    if st < t < bound
                ),
                tone_wave=wave,
                tone_period=base_period,
            ))
    return out


def _tone_instrument_demand(
    events: list[NoteEvent],
    scc_clock: int,
    ay_clock: int,
    psg_clock: int,
    nes_clock: int,
    huc6280_clock: int,
    gb_clock: int,
    vsu_clock: int,
    saa_clock: int,
    hz: float,
    total_samples: int,
    use_speed: int = 1,
    lfo_loop: bool = True,
) -> int:
    """How many tone instruments the notes want with an unlimited budget.

    The FM macro passes run before the tone list is built, so an OPL/OPLL
    driver that changes f-num often can spend every free slot on FM copies and
    leave every AY/PSG note on the plain fallback. The caller measures the
    real demand first and reserves that many slots for the tone families.
    """
    tone = [replace(e) for e in events if e.on and e.tone_chip]
    if not tone:
        return 0
    scc, ay, psg, nes, huc, gb, vsu, saa = [], [], [], [], [], [], [], []
    _bind_tone_instruments(
        tone, scc, ay, psg, nes, huc, gb, vsu, saa,
        # A negative base leaves the instrument cap effectively unlimited, so
        # every distinct macro shape is counted.
        -1000,
        scc_clock, ay_clock, psg_clock, nes_clock, huc6280_clock,
        gb_clock, vsu_clock, saa_clock, hz, total_samples, use_speed,
        lfo_loop=lfo_loop,
    )
    return sum(
        len(bucket)
        for bucket in (scc, ay, psg, nes, huc, gb, vsu, saa)
    )


# The format stores a macro length in one byte, so 255 is the longest macro
# that can be declared. 256 wraps to 0 and the reader then never consumes the
# data block, reporting "invalid macro code" on the bytes after it.
MACRO_MAX = 255

# Instrument IDs are one byte, so 0xFF is the highest addressable instrument.
# Furnace itself allows 256 entries, but a 256th would have no valid ID in a
# pattern cell. The allocator targets 250 to keep a little headroom for the
# passes that append copies; INS_HARD_LIMIT is the absolute ceiling.
INS_TARGET = 250
INS_HARD_LIMIT = 0xFF


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


def _tail_cycle(
    values: list[int], limit: int = 128, window: int = 2048,
    min_start: int = 0,
) -> tuple[int, int] | None:
    """Exact (loop start, period) for a periodic tail after a lead-in."""
    offset = max(0, len(values) - window)
    values = values[offset:]
    n = len(values)
    for start in range(max(0, min_start - offset), n - 1):
        tail = values[start:]
        p = _cycle(tail, limit)
        if p is not None and len(set(tail[:p])) > 1:
            return offset + start, p
    return None


def _lfo_tail_cycle(
    values: list[int], limit: int = 128, tol: int = 1,
    window: int = 2048, min_start: int = 0,
) -> tuple[int, int] | None:
    """Find a quantized LFO at the tail by walking its peaks backwards.

    The last local maximum and minimum establish the top and bottom of the
    LFO. Extrema are accepted backwards while they still match those peaks;
    the first mismatch starts the range. Allow a few one-unit mismatches in
    the period, since run lengths of a quantized LFO can vary by a tick.
    """
    offset = max(0, len(values) - window)
    values = values[offset:]
    n = len(values)
    if n < 6:
        return None
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = i + 1
        while j < n and values[j] == values[i]:
            j += 1
        runs.append((i, values[i]))
        i = j
    extrema: list[tuple[int, int, int]] = []
    for k in range(1, len(runs) - 1):
        prev = runs[k - 1][1]
        cur = runs[k][1]
        nxt = runs[k + 1][1]
        if cur > prev and cur > nxt:
            extrema.append((runs[k][0], cur, 1))
        elif cur < prev and cur < nxt:
            extrema.append((runs[k][0], cur, -1))
    if len(extrema) < 3:
        return None
    top = bottom = None
    left = None
    max_count = min_count = 0
    for idx, value, kind in reversed(extrema):
        if kind > 0:
            if top is None:
                top = value
            elif abs(value - top) > tol:
                break
            max_count += 1
        else:
            if bottom is None:
                bottom = value
            elif abs(value - bottom) > tol:
                break
            min_count += 1
        left = idx
    if top is None or bottom is None or top == bottom:
        return None
    if max_count < 2 and min_count < 2:
        return None
    assert left is not None
    # Extend over the peak run itself, but not over unrelated lead-in values.
    while left > 0 and (
        abs(values[left - 1] - top) <= tol
        or abs(values[left - 1] - bottom) <= tol
    ):
        left -= 1
    left = max(left, max(0, min_start - offset))
    region = values[left:]
    best: tuple[float, int, int] | None = None
    for p in range(2, min(limit, len(region) // 2) + 1):
        for shift in range(0, min(p, len(region) - 2 * p + 1)):
            block = region[shift:shift + p]
            if len(set(block)) < 2:
                continue
            directions = [
                1 if b > a else -1
                for a, b in zip(block, block[1:]) if b != a
            ]
            if not any(a != b for a, b in zip(directions, directions[1:])):
                continue
            errs = [
                abs(region[i] - region[i - p])
                for i in range(shift + p, len(region))
            ]
            if not errs:
                continue
            bad = sum(1 for err in errs if err > tol)
            if bad > max(1, len(errs) // 10):
                continue
            score = bad / len(errs) + p * 0.0005 + shift * 0.0001
            if best is None or score < best[0]:
                best = (score, p, shift)
    if best is None:
        return None
    return offset + left + best[2], best[1]


def _step_compress(
    series: list[int], max_speed: int = 32, ratio: float = 0.03,
) -> tuple[list[int], int]:
    """Shorten per-tick repeats by raising the macro's step length.

    Runs of similar length are rounded to one value per step. The timing
    error is bounded to a few percent of the macro's total; the caller keeps
    the leading hold in ticks, so this only affects the moving part.
    """
    if len(series) < 4:
        return series, 1
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(series):
        j = i + 1
        while j < len(series) and series[j] == series[i]:
            j += 1
        runs.append((series[i], j - i))
        i = j
    if len(runs) == len(series):
        return series, 1
    total = len(series)
    allowed = max(1, int(total * ratio))
    best: tuple[int, int, list[int]] | None = None
    for speed in range(2, min(max_speed, total // 2) + 1):
        values: list[int] = []
        error = 0
        for value, length in runs:
            count = max(1, int(round(length / speed)))
            values.extend([value] * count)
            error += abs(length - count * speed)
        if len(values) >= total * 0.75 or len(values) > MACRO_MAX:
            continue
        if error > allowed:
            continue
        if best is None or len(values) < best[0] or (
                len(values) == best[0] and speed > best[1]):
            best = (len(values), speed, values)
    if best is None:
        return series, 1
    return best[2], best[1]


def _macro_series(
    series: list[int], *, tail_loop: bool = False,
    initial_speed: int = 1, loop_min: int = 0, tail_min: int = 16,
) -> tuple[tuple[int, ...], int, int]:
    """(values, speed, loop) for one tone macro from a per-tick series.

    A constant series needs no macro at all; a periodic one becomes a single
    cycle that loops; anything else is stored whole, thinned by whole ticks
    only when it does not fit the format's one-byte length. A macro that does
    not loop drops its trailing run of one value, since the engine holds the
    last value anyway - that is what lets two notes whose morph differs only
    in how long it lasts share one instrument.

    `tail_loop` also keeps the first cycle of an LFO that starts after a
    lead-in, setting the loop start where that cycle begins.
    """
    if not series or len(set(series)) == 1:
        return (), 1, 255
    speed = max(1, initial_speed)
    loop = 255
    if len(series) > MACRO_MAX:
        p = _cycle(series)
        if p is not None and loop_min == 0:
            return tuple(series[:p]), 1, 0
        if tail_loop and len(series) > tail_min:
            cyc = _tail_cycle(series, min_start=loop_min)
            if cyc is None:
                cyc = _lfo_tail_cycle(series, min_start=loop_min)
            if cyc is not None and cyc[0] + cyc[1] <= MACRO_MAX:
                start, p = cyc
                return tuple(series[:start + p]), 1, start
        speed = min(255, (len(series) + MACRO_MAX - 1) // MACRO_MAX)
        series = series[::speed]
    p = _cycle(series)
    if p is not None and p < len(series) and loop_min == 0:
        return tuple(series[:p]), speed, 0
    if tail_loop and len(series) > tail_min:
        cyc = _tail_cycle(series, min_start=loop_min)
        if cyc is None:
            cyc = _lfo_tail_cycle(series, min_start=loop_min)
        if cyc is not None:
            start, p = cyc
            return tuple(series[:start + p]), speed, start
    while len(series) > 1 and series[-1] == series[-2]:
        series = series[:-1]
    return tuple(series), speed, loop


def _macro_series_delay(
    series: list[int], *, tail_loop: bool = False,
    step_compress: bool = False,
) -> tuple[tuple[int, ...], int, int, int]:
    """`_macro_series` plus a leading-hold delay.

    A macro whose first value repeats can keep one copy and use the macro
    delay to hold it for the same number of ticks. That shortens the stored
    list (freeing room before the 255-entry cutoff) without changing the
    played values. `_macro_series` still handles cycles and the trailing hold.
    """
    if not series:
        return (), 1, 255, 0
    first = series[0]
    run = 1
    while run < len(series) and series[run] == first:
        run += 1
    if run <= 1:
        speed = 1
        body = series
        use_tail = tail_loop and len(body) > 16
        if step_compress and len(body) > 16:
            body, speed = _step_compress(series)
        values, out_speed, loop = _macro_series(
            body, tail_loop=use_tail, initial_speed=speed, tail_min=0,
        )
        return values, out_speed, loop, 0
    # The delay holds the first value for delay+1 ticks. Anything beyond that
    # needs copies in the value list; each later copy advances by one macro
    # step (speed ticks). Keep the leading hold exact in ticks while using a
    # larger step length for the moving part.
    speed = 1
    body = list(series[run:])
    use_tail = tail_loop and len(body) > 16
    if step_compress and len(body) > 16:
        body, speed = _step_compress(body)
    if run <= 256:
        short = [first] + body
        delay = run - 1
    else:
        extra = run - 256
        extra_copies = (extra + speed - 1) // max(1, speed)
        short = [first] * (1 + extra_copies) + body
        delay = 255
    values, out_speed, loop = _macro_series(
        short, tail_loop=use_tail, initial_speed=speed, loop_min=1,
        tail_min=0,
    )
    speed = out_speed
    # A loop that starts at the first value repeats the leading run on every
    # cycle; collapsing it would remove that repeat. A loop later in the
    # macro is unaffected by the initial hold, and the shortened value list
    # already has its loop index shifted correctly.
    if not values or loop == 0:
        values, speed, loop = _macro_series(series, tail_loop=tail_loop)
        return values, speed, loop, 0
    return values, speed, loop, delay


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


# A held legato move is at least half a semitone in 1/64 units. Smaller
# changes stay in the ramp model, where the row slide can express them.
_LEGATO_MIN_UNITS = 32


def _classify_legato(
    events: list[NoteEvent], row_samples: float,
    channels: set[int] | None = None,
) -> tuple[int, int]:
    """Turn a held one- or two-step YM2151 move into a quick transposition.

    A driver that retunes a sounding note once and holds it is not writing a
    glide. E8xx/E9xx carries that step exactly, while a row ramp smears it
    over a whole row. A staircase (more steps), a move that reverses, and a
    release-tail retune stay ramps. Returns (steps, single-step ramps).
    """
    steps = ramps = 0
    for ev in events:
        if (
            not ev.on or ev.pcm or ev.tone_chip or ev.dac or not ev.sweep
            or (channels is not None and ev.ch not in channels)
        ):
            continue
        seq: list[tuple[int, int]] = []
        last = 0
        for sample, delta in ev.sweep:
            if delta != last:
                seq.append((sample, delta))
                last = delta
        if not 1 <= len(seq) <= 2:
            continue
        if abs(seq[-1][1]) < _LEGATO_MIN_UNITS:
            ramps += 1
            continue
        # A step that does not hold for a row before the key-off is a slide,
        # not a retune. So is a write in the release tail.
        if ev.end and (
            seq[-1][0] >= ev.end
            or seq[-1][0] + row_samples > ev.end
        ):
            ramps += 1
            continue
        if len(seq) == 2:
            # Two steps in one direction that keep moving. A return is a
            # vibrato half-cycle and stays a ramp.
            if (seq[0][1] > 0) != (seq[1][1] > 0):
                ramps += 1
                continue
            if abs(seq[1][1]) <= abs(seq[0][1]):
                ramps += 1
                continue
        ev.pitch_steps = tuple(seq)
        ev.sweep = ()
        steps += 1
    return steps, ramps


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
        # A hardware envelope is a continuous volume contour. Splitting the
        # hit on a pitch step would trigger a new note and restart that
        # envelope (the phase-reset macro), turning one percussion decay into
        # several attacks. Keep the whole hit as one note and put the pitch
        # motion on its macro instead.
        env_hit = h.env or any(
            cmd == FX_MODE and ((val + 1) & 4) for _t, cmd, val in h.fx_pts
        )
        first_mode = next(
            (val for _t, cmd, val in h.fx_pts if cmd == FX_MODE), None,
        )
        env_open = first_mode is not None and ((first_mode + 1) & 4)
        if (h.opened_tone or h.freq_pts) and not env_hit:
            segs = segment_periods(
                [(h.sample_time, h.period), *h.freq_pts],
                gate_end, clock, divider, 0,
                merge_gap=_AY_MERGE_GAP,
                min_hold=_AY_NOTE_HOLD,
                drop_short=False,
            )
        else:
            intra = list(h.freq_pts) if env_hit else []
            segs = [(h.sample_time, h.period, intra)]
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
            vol = _value_at(h.vol_pts, sst, h.vol)
            if env_open:
                # The envelope generator drives the level; the register's low
                # nibble is ignored. Writing that nibble (usually 0) as the
                # column makes the pattern look like a silent note. The column
                # is 15 whenever a volume macro carries the level.
                vol = 15
            vol_pts = tuple(
                (t, v, None)
                for t, v in h.vol_pts if sst < t < bound
            )
            prev_st = segs[i - 1][0] if i else -1
            latest: dict[int, int] = {}
            for t, fx, val in buckets[i]:
                # A source register burst often lands a sample or two after
                # the gate. It belongs to this note's opening state, not the
                # next tick, or a snare would start on the previous hit's
                # noise period.
                if t > sst + _AY_MERGE_GAP:
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


def _saa_amp_at(h, t: int) -> tuple[int, int]:
    left, right = h.left, h.right
    for pt in h.amp_pts:
        if pt[0] <= t:
            left, right = pt[1], pt[2]
        else:
            break
    return left, right


def _saa_mode_at(h, t: int) -> tuple[bool, bool]:
    tone, noise = h.tone, h.noise
    for pt in h.mode_pts:
        if pt[0] <= t:
            tone, noise = pt[1], pt[2]
        else:
            break
    return tone, noise


def _saa_noise_at(h, t: int) -> int:
    value = h.noise_freq
    for pt in h.noise_pts:
        if pt[0] <= t:
            value = pt[1]
        else:
            break
    return value


def _saa_env_at(h, t: int) -> int:
    value = h.env
    for pt in h.env_pts:
        if pt[0] <= t:
            value = pt[1]
        else:
            break
    return value


def _saa_mode_effect(tone: bool, noise: bool) -> int:
    """Furnace 10xy: high nibble toggles noise, low nibble tone."""
    return (0x10 if noise else 0) | (0x01 if tone else 0)


def _saa_mode_macro(tone: bool, noise: bool) -> int:
    """Platform psgMode: bit 0 tone, bit 1 noise."""
    return (0x01 if tone else 0) | (0x02 if noise else 0)


def _emit_saa_notes(
    events: list[NoteEvent],
    channel_fx: list[tuple[int, int, int, int]],
    hits,
    offs,
    at: dict[int, int],
    clock: int,
    total_samples: int = 0,
) -> int:
    """Append notes and key-offs for one dual-chip SAA1099 stream.

    The amplitude gate is the note delimiter.  Pitch writes inside a gate are
    the source's pseudo-duty detunes, so they stay on the note's pitch macro
    instead of becoming new notes.  The raw left/right amplitude nibbles go to
    the panning macros; the note column is 15 when those macros are kept.
    """
    count = 0
    for h in hits:
        base = at.get(h.chip_id)
        if base is None:
            continue
        ch = base + h.voice
        st = h.sample_time
        gate_end = h.off_sample or (total_samples or st)
        if h.hidden:
            # Silent source notes for an envelope generator.  Split at every
            # period change so the note/E5 carries the source voice's exact
            # frequency; one long macro would be thinned and would alias the
            # envelope rate.  Volume 0 keeps these notes inaudible.
            starts = [(st, h.period)] + list(h.freq_pts)
            for i, (when, period) in enumerate(starts):
                next_t = (
                    starts[i + 1][0] if i + 1 < len(starts) else h.off_sample
                )
                src_note, src_e5 = saa_period_note(clock, period)
                events.append(NoteEvent(
                    sample=when,
                    ch=ch,
                    on=True,
                    note=src_note,
                    ins=-1,
                    vol=0,
                    e5=src_e5,
                    pan=0,
                    end=next_t,
                    tone_amp_pts=(),
                    tone_fx_pts=(),
                    tone_chip=8,
                    tone_clock=clock,
                    tone_div=SAA_DIVIDER,
                    tone_period_offset=0,
                ))
                count += 1
            continue
        # A large pitch step held for more than half a frame is a legato note,
        # not a macro: split it so the instrument is shared across the notes
        # of a descent.  Fast pseudo-duty detunes are small and stay on the
        # pitch macro.  The amplitude/pan, mode, noise and envelope state at
        # each split is carried into that note, the same way volume is.
        segs = segment_periods(
            [(h.sample_time, h.period), *h.freq_pts],
            gate_end, clock, SAA_DIVIDER, 0,
            merge_gap=_AY_MERGE_GAP,
            min_hold=max(1, _NOTE_HOLD_SAMPLES // 2),
            drop_short=False,
        )
        for i, (sst, period, intra) in enumerate(segs):
            nxt = segs[i + 1][0] if i + 1 < len(segs) else 0
            seg_end = nxt or h.off_sample
            bound = nxt or gate_end
            note, e5 = saa_period_note(clock, period)
            steps = _tone_steps(list(intra), sst, 0, _AY_MERGE_GAP)
            left, right = _saa_amp_at(h, sst)
            vol = max(left, right)
            pan = pcm_pan_fx(left, right)
            # Volume is the macro (as on the other PSGs); the left/right
            # balance is note/row state.  Keeping balance out of the
            # instrument lets notes with the same level shape share one.
            amp_pts: list[tuple[int, int, int]] = [(sst, left, right)]
            for pt in h.amp_pts:
                if sst < pt[0] < bound:
                    amp_pts.append(pt)
            vol_pts = tuple(
                (t, max(l, r), None) for t, l, r in amp_pts[1:]
            )
            last_pan = pan
            for t, l, r in amp_pts[1:]:
                next_pan = pcm_pan_fx(l, r)
                if next_pan != last_pan:
                    channel_fx.append((t, ch, 0x08, next_pan))
                    last_pan = next_pan
            tone, noise = _saa_mode_at(h, sst)
            mode_pts: list[tuple[int, bool, bool]] = [(sst, tone, noise)]
            for pt in h.mode_pts:
                if sst < pt[0] < bound:
                    mode_pts.append(pt)
            nf = _saa_noise_at(h, sst)
            noise_pts: list[tuple[int, int]] = [(sst, nf)]
            for pt in h.noise_pts:
                if sst < pt[0] < bound:
                    noise_pts.append(pt)
            env = _saa_env_at(h, sst)
            env_pts: list[tuple[int, int]] = [(sst, env)]
            for pt in h.env_pts:
                if sst < pt[0] < bound:
                    env_pts.append(pt)

            # Row effects for the opening state; the instrument macros carry
            # any later motion.  The initial effects also keep a constant
            # state when the budget falls back to a plain voice.
            channel_fx.append((sst, ch, 0x10, _saa_mode_effect(tone, noise)))
            channel_fx.append((sst, ch, 0x11, nf))
            for t, to, no in mode_pts[1:]:
                channel_fx.append((t, ch, 0x10, _saa_mode_effect(to, no)))
            for t, value in noise_pts[1:]:
                channel_fx.append((t, ch, 0x11, value))
            if h.voice % 3 == 2:
                channel_fx.append((sst, ch, 0x12, env))
                for t, value in env_pts[1:]:
                    channel_fx.append((t, ch, 0x12, value))

            fx: list[tuple[int, int, int]] = [
                (t, 0x10, _saa_mode_macro(to, no))
                for t, to, no in mode_pts
            ]
            fx += [(t, 0x11, value) for t, value in noise_pts]
            if h.voice % 3 == 2:
                fx += [(t, 0x12, value) for t, value in env_pts]

            events.append(NoteEvent(
                sample=sst,
                ch=ch,
                on=True,
                note=note,
                ins=-1,
                vol=vol,
                e5=e5,
                pan=pan,
                end=seg_end,
                vol_pts=vol_pts,
                tone_pitch_pts=tuple(steps),
                tone_fx_pts=tuple(fx),
                tone_chip=8,
                tone_clock=clock,
                tone_div=SAA_DIVIDER,
                tone_period_offset=0,
            ))
            count += 1
    for off in offs:
        base = at.get(off.chip_id)
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
        ))
    return count


def _emit_ssg_pcm_notes(
    events: list[NoteEvent],
    hits: list[SsgPcmHit],
    at: dict[int, int],
    sample_base: int,
) -> tuple[int, list[tuple[NoteEvent, int]]]:
    """Emit one sample-playback note per SSG PCM segment.

    Bodies are grouped 120 to a ``DIV_INS_AY`` sample map.  The pattern note
    is ``60 + slot`` (60..179); the map stores C-4 for every slot, so the body
    plays at its stored ``c4_rate``.  The caller binds the instrument once the
    bases are known.
    """
    count = 0
    bound: list[tuple[NoteEvent, int]] = []
    for h in hits:
        base = at.get(h.chip_id)
        if base is None:
            continue
        idx = sample_base + h.sample_index
        ev = NoteEvent(
            sample=h.sample_time,
            ch=base + h.voice,
            on=True,
            note=60 + (idx % 120),
            ins=-1,
            vol=15,
            e5=0x80,
            pan=0,
            pcm=True,
            end=h.end_time,
            no_pan=True,
        )
        events.append(ev)
        bound.append((ev, idx))
        count += 1
    return count, bound


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


def _row_steppable(
    pts: tuple[tuple[int, int, int], ...],
    start: int,
    tick_samples: float,
    row_ticks: int,
) -> bool:
    """True when the trajectory has at most about one write per pattern row.

    A vibrato written once per driver tick at speed 1 can ride the row's E5/E8
    effects exactly.  A macro would have to thin it to fit 255 entries, which
    turns a 60 Hz wobble into a slow staircase.
    """
    if row_ticks <= 0 or tick_samples <= 0:
        return False
    row_len = tick_samples * row_ticks
    rows = {int((t - start) // row_len) for t, _b, _f in pts}
    return len(rows) * 5 >= len(pts) * 4


def _bind_opll_macros(
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    channel_fx: list[tuple[int, int, int, int]],
    hz: float,
    speed: int,
    t0: int,
    total_samples: int,
    cap: int = 200,
) -> int:
    """Turn sub-row OPLL preset/level/retrigger writes into instrument macros.

    A driver can walk `$3x` at tick rate: a preset change every 108 samples is
    faster than one pattern row, so a row effect keeps only the last value.
    Furnace's OPLL platform reads a wave macro for the high nibble and a volume
    macro for the low nibble, and a phase-reset macro for a repeated keyed
    `$2x` write.  Build those series only when the writes really land faster
    than a row; slower motion keeps the cheaper row effects.
    """
    if not events or hz <= 0:
        return 0
    tick = VGM_RATE / hz
    row_samples = tick * max(1, speed)
    by_ch: dict[int, list[NoteEvent]] = {}
    for ev in events:
        if ev.on:
            by_ch.setdefault(ev.ch, []).append(ev)
    next_on: dict[int, int] = {}
    for notes in by_ch.values():
        for i, ev in enumerate(notes):
            next_on[id(ev)] = (
                notes[i + 1].sample if i + 1 < len(notes)
                else total_samples or ev.sample
            )
    made = 0
    cache: dict[tuple, int] = {}
    for ev in events:
        if not ev.on:
            continue
        if ev.tone_chip or ev.pcm or ev.dac:
            # SCC/AY/PCE/NES/GB tone events also carry `vol_pts`, and their
            # instrument index lives in the same number space as the FM
            # patches. Treating one as an OPLL note replaced its instrument
            # with an FM macro copy and pinned its volume column to 15, so an
            # AY note with a real decay played at full level.
            continue
        if not (ev.opll_preset_pts or ev.opll_phase_pts or ev.vol_pts):
            continue
        if not (0 <= ev.ins < len(patch_order)):
            continue
        base = patches[patch_order[ev.ins]]
        if not base.opll:
            continue
        times = (
            [t for t, _p in ev.opll_preset_pts]
            + list(ev.opll_phase_pts)
            + [t for t, _v, _pan in ev.vol_pts]
        )
        if not times:
            continue
        rows = {
            place_tick(t, t0, row_samples, speed, tick, floor=True)[0]
            for t in times
        }
        note_row = place_tick(
            ev.sample, t0, row_samples, speed, tick, floor=True,
        )[0]
        # A single change in the note's own row is still inaudible as a row
        # effect (the volume-step writer lands it at the row start), so use a
        # macro then too.  Duplicates in any later row mean multiple writes
        # that one row effect cannot carry.
        if len(rows) >= len(times) and note_row not in rows:
            continue
        end = ev.end or next_on.get(id(ev), total_samples or ev.sample)
        if ev.vol_pts:
            # The key-off ends the note but a driver can keep gating the
            # release tail; the volume macro must reach its last write.
            end = max(end, ev.vol_pts[-1][0])
        ticks = max(1, min(
            int((end - ev.sample) / tick) + 1, 65025,
        ))
        # A macro has one byte of entries. Keep the early tick-rate detail
        # exact; a change after the first 255 ticks is slow enough to remain a
        # row effect instead of thinning the early pulse into a staircase.
        macro_ticks = min(ticks, 255)
        macro_end = ev.sample + macro_ticks * tick
        wave = wave_speed = wave_loop = None
        if ev.opll_preset_pts:
            pts = sorted([(ev.sample, base.preset)] + list(ev.opll_preset_pts))
            series = _tick_values(pts, ev.sample, macro_ticks, tick)
            if len(set(series)) > 1:
                wave, wave_speed, wave_loop = _macro_series(series)
        volm = vol_speed = vol_loop = None
        if ev.vol_pts:
            pts = sorted(
                [(ev.sample, max(0, ev.vol))]
                + [(t, max(0, v)) for t, v, _pan in ev.vol_pts]
            )
            series = _tick_values(pts, ev.sample, macro_ticks, tick)
            if len(set(series)) > 1:
                volm, vol_speed, vol_loop = _macro_series(series)
        phase = phase_speed = phase_loop = None
        if ev.opll_phase_pts:
            series = [0] * macro_ticks
            hit = False
            for t in ev.opll_phase_pts:
                k = int(round((t - ev.sample) / tick))
                if 0 < k < macro_ticks:
                    series[k] = 1
                    hit = True
            if hit:
                phase, phase_speed, phase_loop = _macro_series(series)
        if not (wave or volm or phase):
            continue
        if len(patch_order) >= cap:
            continue
        key = (
            "opll-macro", patch_order[ev.ins],
            wave, wave_speed, wave_loop,
            volm, vol_speed, vol_loop,
            phase, phase_speed, phase_loop,
        )
        idx = cache.get(key)
        if idx is None:
            copy = replace(
                base,
                wave_macro=wave or (),
                wave_speed=wave_speed or 1,
                wave_loop=wave_loop if wave_loop is not None else 255,
                vol_macro=volm or (),
                vol_speed=vol_speed or 1,
                vol_loop=vol_loop if vol_loop is not None else 255,
                phase_macro=phase or (),
                phase_speed=phase_speed or 1,
                phase_loop=phase_loop if phase_loop is not None else 255,
                uses=1,
                name=f"{base.name}+T",
                base_ins=ev.ins,
            )
            patches[key] = copy
            patch_order.append(key)
            idx = len(patch_order) - 1
            cache[key] = idx
        ev.ins = idx
        if volm:
            ev.vol = 15
            ev.vol_pts = tuple(
                p for p in ev.vol_pts if p[0] >= macro_end
            )
        if wave:
            channel_fx[:] = [
                fx for fx in channel_fx
                if not (
                    fx[1] == ev.ch and fx[2] == 0x10
                    and ev.sample <= fx[0] < macro_end
                )
            ]
        ev.opll_preset_pts = ()
        ev.opll_phase_pts = ()
        made += 1
    return made


def _bind_fm_pitch(
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    clock: int,
    hz: float,
    total_samples: int,
    cap: int = 200,
    allow_steps: bool = False,
    row_ticks: int = 1,
    quant: int = 0,
    opll: bool = False,
) -> tuple[int, int]:
    """Turn YM2203/YM2612 f-num trajectories into instrument pitch macros.

    `pitch2` is a 1/128-semitone offset. A macro carries the driver's
    motion without row quantization. `allow_steps` (the OPN and OPL
    families) encodes short steps as row effects instead. Returns (copies
    made, row-ramp fallbacks).
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
            next_start = (
                notes[i + 1].sample if i + 1 < len(notes) else track_end
            )
            end = e.end or next_start
            gate_end = end
            pts = e.fm_pitch_pts
            if i + 1 < len(notes):
                # The OPLL collector holds a voice through OPLL_SETTLE before
                # the next key-on, so that note's f-num preparation lands in
                # this note's pitch points. Those writes are often a low
                # placeholder (fnum 1) and turned the previous note into a
                # sub-octave bass if used as its pitch. Drop the trailing prep
                # window; a genuine release-tail gesture is earlier.
                cutoff = next_start - OPLL_SETTLE
                kept = tuple(p for p in pts if p[0] < cutoff)
                if kept:
                    pts = kept
            if e.end and len(pts) > 1:
                # The key-off write can also clear the f-num high bit, which
                # the collector sees as a pitch point on the key-off sample.
                # It is not a release gesture; using it dropped the note by
                # several octaves on its last row.
                pts = (pts[0],) + tuple(
                    p for p in pts[1:] if abs(p[0] - e.end) > 2
                )
            if pts:
                # The key-off can end the note while the driver keeps moving
                # the f-num through the release tail; the macro must reach
                # the last write just like the volume macro.
                end = max(end, pts[-1][0])
            # A macro holds up to 255 values and can step every 255 ticks, so
            # a note of up to 65025 ticks still fits.  Do not truncate earlier:
            # long held notes can change pitch near their end (Avengers 23).
            ticks = max(1, min(int((end - e.sample) / tick_samples) + 1, 65025))
            ref = opn_pitch_units(pts[0][1], pts[0][2])
            if (
                opll and len(pts) <= 2
                and pts[-1][0] < gate_end
            ):
                # One held pitch step *inside* the note (a drum's tuning
                # change) needs no instrument and no row effect: retune the
                # note to it. A write at or after the key-off is the release
                # tail or the next note's f-num preparation; retuning to that
                # turned ordinary notes into sub-octave bass notes.
                e.note, e.e5 = opll_note_for(
                    pts[-1][1], pts[-1][2], clock,
                )
                continue
            row_ok = _row_steppable(
                pts, e.sample, tick_samples, row_ticks,
            )
            # A held pitch with a jump or two is a step function. One
            # E5/E8/E9 row effect reproduces it without an instrument copy.
            # Only oscillating motion needs a macro. OPLL keeps fast sub-row
            # bends in a macro (its row steps do not reach the registers on
            # export), but a trajectory with at most about one write per row
            # rides E5/E8/E9 exactly and must not spend an instrument slot.
            if (
                allow_steps
                and (
                    len(pts) <= 4
                    or _steppable_pitch(pts)
                    or (ticks > MACRO_MAX and row_ok)
                )
            ) or (opll and row_ok):
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
            if quant > 1:
                # OPLL pitch macros are short and numerous. A coarser value
                # grid merges near-identical drum bends without leaving them
                # as row ramps (which do not reach OPLL registers on export).
                vals = [
                    int(round(v / quant)) * quant for v in vals
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
            macro, speed, loop = _macro_series(vals, tail_loop=True)
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
                    base_ins=e.ins,
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


def _bind_op_macros(
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    tick_samples: float,
    cap: int = 250,
) -> tuple[int, int]:
    """Turn a scripted modulator level into row effects.

    On FM the ``$4n`` register is the modulator's output and sets how much of
    the carrier each harmonic gets. The volume column reaches only an
    operator marked KVS, so a modulator move cannot ride it. The baseline
    instrument is already shared by shape (``shape_key`` keeps the loudest
    modulator level), so a quieter level travels as that operator's own
    pattern effect (``12xx``..``15xx``), which the OPL platform stores as an
    absolute level. That avoids one instrument copy per ramp. Carriers still
    ride the volume column through ``tl_pts``. Returns ``(notes, 0)``.
    """
    made = fallbacks = 0
    idx_map: dict[tuple, int] = {}
    if tick_samples <= 0:
        return 0, 0
    for e in events:
        if not e.on or not e.opl_op_tl or getattr(e, "drum", False) or e.pcm:
            continue
        if not (0 <= e.ins < len(patch_order)):
            continue
        base = patches[patch_order[e.ins]]
        carriers = set(base.carriers())
        # A fast ramp has several levels inside one row, and a row holds one
        # 12xx/13xx effect per operator. Those notes keep the operator macro
        # (an instrument copy); a note whose level differs only at its attack
        # (the common per-note case) stays on the shared instrument and uses
        # the row effect.
        if any(oi not in carriers and len(pts) > 2
               for oi, pts in e.opl_op_tl):
            entries: list[tuple[int, tuple[int, ...], int]] = []
            for oi, pts in e.opl_op_tl:
                if oi in carriers or not pts:
                    continue
                last_s = pts[-1][0]
                span_end = e.end if (e.end and e.end > last_s) else last_s
                span_ticks = max(1.0, (span_end - e.sample) / tick_samples)
                speed = max(1, min(254, int(span_ticks // 240) + 1))
                marks: list[tuple[int, int]] = []
                for s, val in pts:
                    slot = max(0, int((s - e.sample) // tick_samples)) // speed
                    if slot > 254:
                        break
                    if marks and marks[-1][0] == slot:
                        marks[-1] = (slot, val)
                    else:
                        marks.append((slot, val))
                if not marks:
                    continue
                start = base.ops[oi].tl & 0x3F
                values: list[int] = []
                mi = 0
                cur = start
                for slot in range(0, marks[-1][0] + 1):
                    while mi < len(marks) and marks[mi][0] <= slot:
                        cur = marks[mi][1]
                        mi += 1
                    values.append(cur)
                if len(set(values)) < 2 and values[0] == start:
                    continue
                entries.append((oi, tuple(values), speed))
            if not entries:
                continue
            key = ("op-tl", patch_order[e.ins], tuple(entries))
            idx = idx_map.get(key)
            if idx is None:
                if len(patch_order) >= cap:
                    fallbacks += 1
                    continue
                copy = replace(base, op_tl_macro=tuple(entries), uses=1,
                               name=f"{base.name}+lvl", base_ins=e.ins)
                patches[key] = copy
                patch_order.append(key)
                idx = len(patch_order) - 1
                idx_map[key] = idx
            else:
                patches[patch_order[idx]].uses += 1
            e.ins = idx
            made += 1
            continue

        fx: list[tuple[int, int, int]] = []
        for oi, pts in e.opl_op_tl:
            if oi in carriers or not pts:
                continue          # the volume column already carries that one
            baseline = base.ops[oi].tl & 0x3F
            for sample, value in pts:
                value &= 0x3F
                if value == baseline:
                    continue
                item = (sample, oi, value)
                # Only one level can sound per row; a later write at the same
                # sample wins.
                if fx and fx[-1][0] == sample and fx[-1][1] == oi:
                    fx[-1] = item
                else:
                    fx.append(item)
        if not fx:
            continue
        e.op_tl_fx = tuple(fx)
        made += 1
    return made, fallbacks


def _bind_pms_macros(
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    hz: float,
    total_samples: int,
    cap: int = 200,
) -> tuple[int, int]:
    """Per-note PMS/AMS macro when the instrument's common ramp does not fit.

    A short note whose source never moves PMS must not inherit the ramp. A
    ramp that starts at a different time needs its own lead-in. Notes that do
    match the common macro keep the shared instrument.
    """
    made = fallbacks = 0
    tick_samples = VGM_RATE / hz if hz else VGM_RATE / 60.0
    track_end = total_samples or max((e.sample for e in events), default=0)
    by_ch: dict[int, list[NoteEvent]] = {}
    for e in events:
        if e.on:
            by_ch.setdefault(e.ch, []).append(e)
    idx_map: dict[tuple, int] = {}
    work = [
        (notes, i, e)
        for notes in by_ch.values()
        for i, e in enumerate(notes)
        if (e.ins >= 0 and not e.pcm and not e.dac and not e.tone_chip
            and e.opm_pms_pts)
    ]
    # Cover the notes with no PMS writes first: they would otherwise inherit
    # a ramp they never had. A late ramp that cannot get a slot still lands
    # on the base instrument, which at least has vibrato.
    work.sort(key=lambda item: len(item[2].opm_pms_pts) > 1)
    for notes, i, e in work:
        pts = list(e.opm_pms_pts)
        if len(pts) <= 1:
            pms_vals = (pts[0][1] if pts else 0,)
            ams_vals = (pts[0][2] if pts else 0,)
            speed = 1
        else:
            end = e.end or (
                notes[i + 1].sample if i + 1 < len(notes) else track_end
            )
            span = max(1, end - e.sample)
            speed = max(1, min(255, math.ceil((span / tick_samples) / 255)))
            while True:
                steps = max(
                    1,
                    min(int((end - e.sample) / (speed * tick_samples)) + 1,
                        65025),
                )
                pms_series: list[int] = []
                ams_series: list[int] = []
                j = 0
                cur_p, cur_a = pts[0][1], pts[0][2]
                for k in range(steps):
                    t = e.sample + k * speed * tick_samples
                    while j < len(pts) and pts[j][0] <= t:
                        cur_p, cur_a = pts[j][1], pts[j][2]
                        j += 1
                    pms_series.append(cur_p)
                    ams_series.append(cur_a)
                while (len(pms_series) > 1
                       and pms_series[-1] == pms_series[-2]
                       and ams_series[-1] == ams_series[-2]):
                    pms_series.pop()
                    ams_series.pop()
                if len(pms_series) <= 255 or speed >= 255:
                    break
                speed = min(255, speed * 2)
            pms_vals = tuple(pms_series)
            ams_vals = tuple(ams_series)
            if len(set(pms_vals)) == 1 and len(set(ams_vals)) == 1:
                pms_vals = pms_vals[:1]
                ams_vals = ams_vals[:1]
                speed = 1
        base_key = patch_order[e.ins]
        base = patches[base_key]
        if not base.fms_macro:
            if (len(pms_vals) == 1 and len(ams_vals) == 1
                    and pms_vals[0] == base.pms
                    and ams_vals[0] == base.ams):
                continue
        elif (pms_vals, ams_vals, speed) == (
            base.fms_macro, base.ams_macro, base.macro_speed
        ):
            continue
        key = ("pms-note", base_key, pms_vals, ams_vals, speed)
        idx = idx_map.get(key)
        if idx is None:
            if len(patch_order) >= cap:
                fallbacks += 1
                continue
            copy = replace(
                base, fms_macro=pms_vals,
                ams_macro=ams_vals, macro_speed=speed,
                uses=1, name=f"{base.name}+pms", base_ins=e.ins,
            )
            patches[key] = copy
            patch_order.append(key)
            idx = len(patch_order) - 1
            idx_map[key] = idx
        else:
            patches[patch_order[idx]].uses += 1
        e.ins = idx
        made += 1
    return made, fallbacks


def _rebase_non_fm_ins(
    events: list[NoteEvent], old_count: int, new_count: int,
) -> None:
    """Move sample/tone instrument indices after FM instruments are appended."""
    delta = new_count - old_count
    if delta == 0:
        return
    for e in events:
        if (e.dac or e.pcm or e.tone_chip) and e.ins >= old_count:
            e.ins += delta


# Macro fields of an FM instrument. Two instruments whose non-macro identity
# matches may share one when every macro agrees from left to right; the longer
# macro wins because a short note only plays the prefix before the next key-on.
_FM_MACRO_FIELDS = (
    "fms_macro", "ams_macro", "pitch_macro", "wave_macro", "vol_macro",
    "phase_macro", "op_tl_macro", "op_dt_macro", "op_dt2_macro",
    "op_mul_macro",
)
_FM_MACRO_SPEEDS = (
    ("fms_macro", "macro_speed"),
    ("pitch_macro", "pitch_speed"),
    ("wave_macro", "wave_speed"),
    ("vol_macro", "vol_speed"),
    ("phase_macro", "phase_speed"),
)
_FM_MACRO_LOOPS = (
    ("pitch_macro", "pitch_loop"),
    ("wave_macro", "wave_loop"),
    ("vol_macro", "vol_loop"),
    ("phase_macro", "phase_loop"),
)


def _macro_extends(a, b) -> bool:
    """True when `a` is `b` plus a (possibly empty) extra tail."""
    a = tuple(a)
    b = tuple(b)
    if bool(a) != bool(b) or len(a) < len(b):
        return False
    return a[:len(b)] == b


def _op_macro_extends(a, b) -> bool:
    """Compare per-operator macros: `a` must extend `b` for every operator."""
    da = {op: (tuple(values), speed) for op, values, speed in a}
    db = {op: (tuple(values), speed) for op, values, speed in b}
    if set(da) != set(db):
        return False
    for op in da:
        values_a, speed_a = da[op]
        values_b, speed_b = db[op]
        if speed_a != speed_b or not _macro_extends(values_a, values_b):
            return False
    return True


def _fm_macro_extends(a: FMPatch, b: FMPatch) -> bool:
    """Whether `a` carries every macro of `b` plus a longer tail."""
    for field in _FM_MACRO_FIELDS:
        va = getattr(a, field)
        vb = getattr(b, field)
        if field.startswith("op_"):
            if not _op_macro_extends(va, vb):
                return False
        elif not _macro_extends(va, vb):
            return False
    for field, speed in _FM_MACRO_SPEEDS:
        if (getattr(a, field) or getattr(b, field)) and (
                getattr(a, speed) != getattr(b, speed)):
            return False
    for field, loop in _FM_MACRO_LOOPS:
        if (getattr(a, field) or getattr(b, field)) and (
                getattr(a, loop) != getattr(b, loop)):
            return False
    return True


def _macro_ticks(start: int, end: int, tick_samples: float) -> int:
    if end <= start:
        return 1
    return max(1, int(math.ceil((end - start) / tick_samples)))


def _macro_effective(
    values: tuple[int, ...], speed: int, loop: int, ticks: int,
    delay: int = 0,
) -> tuple[int, ...]:
    """The value a macro plays on each tick, including delay/hold/loop tails."""
    if not values:
        return ()
    speed = max(1, speed)
    hold = max(0, delay) + 1
    out: list[int] = []
    for t in range(ticks):
        if t < hold:
            out.append(values[0])
            continue
        pos = 1 + (t - hold) // speed
        if pos < len(values):
            out.append(values[pos])
        elif 0 <= loop < len(values):
            span = max(1, len(values) - loop)
            out.append(values[loop + (pos - len(values)) % span])
        else:
            out.append(values[-1])
    return tuple(out)


def _fm_macro_same_for(a: FMPatch, b: FMPatch, ticks: int) -> bool:
    """Whether `a` and `b` play the same macros for `ticks` ticks."""
    speed_of = dict(_FM_MACRO_SPEEDS)
    loop_of = dict(_FM_MACRO_LOOPS)
    for field in _FM_MACRO_FIELDS:
        va = getattr(a, field)
        vb = getattr(b, field)
        if field.startswith("op_"):
            da = {op: (tuple(values), speed) for op, values, speed in va}
            db = {op: (tuple(values), speed) for op, values, speed in vb}
            for op in db:
                if (
                    _macro_effective(da[op][0], da[op][1], 255, ticks)
                    != _macro_effective(db[op][0], db[op][1], 255, ticks)
                ):
                    return False
        else:
            speed = getattr(b, speed_of[field]) if field in speed_of else 1
            loop = getattr(b, loop_of[field]) if field in loop_of else 255
            if (
                _macro_effective(tuple(va), speed, loop, ticks)
                != _macro_effective(tuple(vb), speed, loop, ticks)
            ):
                return False
    return True


def _fm_macro_size(patch: FMPatch) -> int:
    size = 0
    for field in _FM_MACRO_FIELDS:
        value = getattr(patch, field)
        if field.startswith("op_"):
            size += sum(len(values) for _op, values, _speed in value)
        else:
            size += len(value)
    return size


def _prefix_targets(indexes: list[int], size_of, prefix_of) -> dict[int, int]:
    """Map each index to the longest compatible extension in its group.

    Prefix chains are resolved to the final (largest) member. Equal-size
    identical macros pick the lower index so the mapping is stable.
    """
    target: dict[int, int] = {i: i for i in indexes}
    for i in indexes:
        best: int | None = None
        for j in indexes:
            if j == i or not prefix_of(j, i):
                continue
            sj = size_of(j)
            si = size_of(i)
            if sj < si or (sj == si and j > i):
                continue
            if (
                best is None
                or sj > size_of(best)
                or (sj == size_of(best) and j < best)
            ):
                best = j
        if best is not None:
            target[i] = best
    for i in indexes:
        cur = i
        seen: set[int] = set()
        while target[cur] != cur and cur not in seen:
            seen.add(cur)
            cur = target[cur]
        target[i] = cur
    return target


def _merge_fm_prefix_macros(
    events: list[NoteEvent], fm_patches: list[FMPatch],
    tick_samples: float,
) -> int:
    """Share the longest left-to-right-compatible macro between instruments.

    Short notes often produce a truncated copy of the same instrument macro.
    When every macro agrees from the beginning and only one has a longer tail,
    the longer instrument plays the short note identically (the next key-on
    comes first). Keep the longest and drop the shorter copies. This runs just
    before the hard-limit trim, so it can save slots without losing motion.
    """
    old_count = len(fm_patches)
    if old_count < 2:
        return 0
    notes_by_ins: dict[int, list[tuple[int, int]]] = {}
    for e in events:
        if e.on and 0 <= e.ins < old_count:
            notes_by_ins.setdefault(e.ins, []).append((e.sample, e.end))
    groups: dict[tuple, list[int]] = {}
    for i, patch in enumerate(fm_patches):
        groups.setdefault(patch.identity(), []).append(i)

    def extends(j: int, i: int) -> bool:
        if not _fm_macro_extends(fm_patches[j], fm_patches[i]):
            return False
        for start, end in notes_by_ins.get(i, ()):
            if end <= 0:
                return False
            ticks = _macro_ticks(start, end, tick_samples)
            if not _fm_macro_same_for(fm_patches[j], fm_patches[i], ticks):
                return False
        return True

    target = {i: i for i in range(old_count)}
    for indexes in groups.values():
        if len(indexes) > 1 and any(
            _fm_macro_size(fm_patches[i]) > 0 for i in indexes
        ):
            target.update(_prefix_targets(
                indexes,
                lambda i: _fm_macro_size(fm_patches[i]),
                extends,
            ))
    if all(target[i] == i for i in range(old_count)):
        return 0
    new_index: dict[int, int] = {}
    kept: list[FMPatch] = []
    for i in range(old_count):
        if target[i] == i:
            new_index[i] = len(kept)
            kept.append(fm_patches[i])
    for i in range(old_count):
        new_index[i] = new_index[target[i]]
    merged_patches: list[FMPatch] = []
    for idx, patch in enumerate(kept):
        base = (
            new_index.get(patch.base_ins, -1)
            if patch.base_ins >= 0 else -1
        )
        if base == idx:
            base = -1             # the copy's base merged into this slot
        merged_patches.append(replace(patch, base_ins=base))
    fm_patches[:] = merged_patches
    for e in events:
        if 0 <= e.ins < old_count:
            e.ins = new_index.get(e.ins, e.ins)
    _rebase_non_fm_ins(events, old_count, len(fm_patches))
    return old_count - len(fm_patches)


def _trim_fm_macro_copies(
    events: list[NoteEvent], fm_patches: list[FMPatch], limit: int,
) -> int:
    """Fold trailing FM macro copies back onto their base voices.

    The allocation passes reserve their slots, but a future pass or a mixed
    log can still leave the final list over the one-byte instrument range.
    Base voices are the timbre and stay; only copies created by a macro pass
    (`base_ins >= 0`) are dropped, and the notes that used them return to the
    voice they were copied from. Returns how many copies were removed.
    """
    removed: dict[int, int] = {}
    while len(fm_patches) > max(1, limit):
        idx = len(fm_patches) - 1
        patch = fm_patches[idx]
        base = patch.base_ins
        if base < 0 or base >= idx:
            break       # a base voice or a malformed copy: stop, do not lose timbre
        removed[idx] = base
        fm_patches.pop()
    if not removed:
        return 0
    # A copy's base can itself be a copy that was removed later in the loop.
    for idx in list(removed):
        base = removed[idx]
        seen: set[int] = set()
        while base in removed and base not in seen:
            seen.add(base)
            base = removed[base]
        removed[idx] = base
    for e in events:
        if e.ins in removed:
            e.ins = removed[e.ins]
    return len(removed)


def _apply_opn_carrier_octave(
    events: list[NoteEvent], fm_patches: list[FMPatch],
    *, skip_channels: set[int] | None = None,
) -> int:
    """Relabel notes at their loudest carrier's octave, either FM family."""
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
        octave = (
            opl_carrier_octave(patch) if getattr(patch, "opl", False)
            else opn_carrier_octave(patch)
        )
        if octave == 0:
            continue
        if any(not (0 <= e.note + 12 * octave <= 179) for e in notes):
            continue
        if ins in skipped_ins:
            # The same instrument also plays an Ext channel-3 operator
            # column. Give the relabelled channels their own copy. The
            # operator columns keep the unshifted patch.
            patch = replace(patch, name=f"{patch.name} L", base_ins=ins)
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


def _pcm_levels(
    hit: AdpcmAHit | AdpcmBHit,
) -> tuple[tuple[int, int, int], ...]:
    """Late level and pan writes as (sample, volume, pan) points.

    A pan of 0 silences the channel, but Furnace's panning command treats
    `0x00` as center. Carry that silence in the volume column instead and
    restore the level when the pan returns.
    """
    pts = sorted(
        [(t, 0, v) for t, v in hit.vol_pts]
        + [(t, 1, p) for t, p in hit.pan_pts]
    )
    vol, pan = hit.vol, hit.pan
    last = (0 if pan == 0 else vol, pan)
    levels: list[tuple[int, int, int]] = []
    i = 0
    while i < len(pts):
        t = pts[i][0]
        while i < len(pts) and pts[i][0] == t:
            _t, kind, value = pts[i]
            if kind:
                pan = value
            else:
                vol = value
            i += 1
        level = (0 if pan == 0 else vol, pan)
        if level == last:
            continue
        levels.append((t, *level))
        last = level
    return tuple(levels)


def _adpcm_b_motion(hit: AdpcmBHit) -> tuple[
    tuple[tuple[int, int], ...], tuple[tuple[int, int, int], ...]
]:
    """Late B writes as pitch steps (1/64 semitones) and volume/pan points."""
    base = hit.rate & 0xFFFF
    steps: list[tuple[int, int]] = []
    if base:
        for t, rate in hit.rate_pts:
            delta = int(round(
                64.0 * 12.0 * math.log2((rate & 0xFFFF) / base)
            ))
            if delta and (not steps or steps[-1][1] != delta):
                steps.append((t, delta))
    return tuple(steps), _pcm_levels(hit)


def _bind_opm_levels(
    events: list["NoteEvent"], patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
) -> tuple[int, int]:
    """Make a held note's *rising* level reachable, not just its falling one.

    A YM2151 voice that builds up walks its carriers down through the TL range -
    "Purple Twilight" takes op3/op4 from 0x39 to 0x1E while op1/op2 hold 0x1E.
    The converter keeps a carrier's logged TL in the instrument and puts mid-note
    moves on the volume column, but the platform answers
    ``TL = 127 - vol + op.tl``: volume can only attenuate. So a step *louder* than
    the instrument's stored level was unreachable - the note row went out at vol
    127 (the column's maximum) and the chip stayed at 0x39, leaving the entrance
    of a crescendo inaudible.

    Fix: for an instrument whose notes ever ask for a lower TL than it stores,
    open the instrument up to that floor and hang every note off the column
    instead. The carriers keep their relative differences (the driver moves them in
    lockstep), the loudest request now lands at vol 127, and a note that starts
    quietly rides ``127 - (level - floor)``.

    Returns (instruments opened, notes given a baseline).
    """
    # Pass 1: what is the loudest level each instrument is ever asked for?
    floor: dict[int, int] = {}
    tl_keys: set[int] = set()
    for ev in events:
        if not ev.on or ev.pcm or not (0 <= ev.ins < len(patch_order)):
            continue
        if not ev.tl_pts:
            continue                # nothing moved: the instrument is already right
        patch = patches.get(patch_order[ev.ins])
        if patch is None:
            continue
        key = ev.ins
        want = min([tl for _t, tl in ev.tl_pts] + [patch.carrier_tl()])
        prev = floor.get(key)
        floor[key] = want if prev is None else min(prev, want)
        tl_keys.add(key)

    # Pass 2: open each affected instrument to its floor, remembering what it
    # stored before so the notes can be hung beneath it. Every note of an
    # instrument starts at that same level - `identity()` keeps carrier TL in the
    # cluster key, so notes at a different level are separate instruments.
    opened: dict[int, int] = {}        # instrument -> TL it stored before shifting
    for key in sorted(tl_keys):
        patch = patches.get(patch_order[key])
        if patch is None:
            continue
        base = patch.carrier_tl()
        low = floor[key]
        if low >= base:
            continue
        delta = base - low
        carriers = set(patch.carriers())
        ops = list(patch.ops)
        for i, op in enumerate(ops):
            if i in carriers and (op.tl & 0x7F) < 0x7F:
                ops[i] = replace(op, tl=(op.tl & 0x7F) - delta)
        patch.ops = tuple(ops)      # type: ignore[assignment]
        opened[key] = base

    # Pass 3: hang the notes off the column. Every note of an opened instrument
    # states its own starting level now - including short notes that never move,
    # which would otherwise play at the instrument's new, louder floor. The note
    # row carries that level and `vol_tl_base` gives the writer the same baseline
    # for the mid-note steps, where it computes `vol = fm_vol_max - (tl - base)`.
    rebased = 0
    if opened:
        for ev in events:
            if not ev.on or ev.pcm or not (0 <= ev.ins < len(patch_order)):
                continue
            base = opened.get(ev.ins)
            if base is None:
                continue
            low = floor[ev.ins]
            ev.vol_tl_base = low
            ev.vol = max(0, min(127, 127 - (base - low)))
            rebased += 1
    return len(opened), rebased


# YM2151 DT1 index -> Furnace's engine index (same table as the writer).
_YM_DT_TO_FURNACE = (3, 4, 5, 6, 7, 2, 1, 0)


def _bind_opm_dtmul_macros(
    events: list["NoteEvent"], patches: dict[tuple, FMPatch],
    patch_order: list[tuple], tick_samples: float, t0: int, use_speed: int,
    cap: int = 250,
) -> tuple[int, int]:
    """Place a note's mid-note DT1/MULT/DT2 writes.

    A YM2151 driver can retune one operator every frame inside a very short
    staccato note (Magical Chase walks channel 1's third operator MULT
    through 0..4, and later through long staircases). When the changes fit
    on pattern rows, use the operator effects 16xy/53xy/55xy - the pattern
    then shows the movement and no instrument copy is needed. Only motion
    that collides inside one row falls back to a per-operator DT/MULT/DT2
    macro.

    Returns (notes given a copy, notes left on the base voice at the cap).
    """
    made = fallbacks = 0
    idx_map: dict[tuple, int] = {}
    if tick_samples <= 0:
        return 0, 0
    row_samples = tick_samples * max(1, use_speed)

    def expand(marks: list[tuple[int, int]], start: int) -> tuple[int, ...]:
        cur = start
        mi = 0
        values: list[int] = []
        for slot in range(marks[-1][0] + 1):
            while mi < len(marks) and marks[mi][0] <= slot:
                cur = marks[mi][1]
                mi += 1
            values.append(cur)
        return tuple(values)

    def row_marks(
        pts: list[tuple[int, int]], start: int,
    ) -> tuple[list[tuple[int, int]], bool]:
        """(sample, value) points that fit rows, and whether nothing is lost.

        A row can carry one value per operator. If a row contains two
        different values and the first is not what the previous row already
        held, the intermediate transition is lost and the macro path is
        needed instead.
        """
        groups: dict[int, list[tuple[int, int]]] = {}
        for s, value in pts:
            if ev.end and s >= ev.end:
                continue
            row = max(0, int((s - t0) // row_samples))
            groups.setdefault(row, []).append((s, value))
        carried = start
        out: list[tuple[int, int]] = []
        lossless = True
        for row in sorted(groups):
            vals = groups[row]
            distinct: list[int] = []
            for _s, value in vals:
                if not distinct or distinct[-1] != value:
                    distinct.append(value)
            if len(distinct) > 1 and distinct[0] != carried:
                lossless = False
            if distinct[-1] != carried:
                out.append((vals[-1][0], distinct[-1]))
                carried = distinct[-1]
        return out, lossless

    def macro_values(
        pts: list[tuple[int, int]], start: int,
    ) -> tuple[int, ...]:
        last_s = pts[-1][0]
        span_end = ev.end if (ev.end and ev.end > last_s) else last_s
        span_ticks = max(1.0, (span_end - ev.sample) / tick_samples)
        speed = max(1, min(254, int(span_ticks // 240) + 1))
        marks: list[tuple[int, int]] = []
        for s, value in pts:
            slot = max(0, int((s - ev.sample) // tick_samples)) // speed
            if slot > 254:
                break
            if marks and marks[-1][0] == slot:
                marks[-1] = (slot, value)
            else:
                marks.append((slot, value))
        return expand(marks, start) if marks else ()

    for e in events:
        ev = e
        if not ev.on or not (ev.op_dtmul_pts or ev.op_dt2_pts) or ev.pcm:
            continue
        if not (0 <= ev.ins < len(patch_order)):
            continue
        base = patches[patch_order[ev.ins]]
        by_op: dict[int, list[tuple[int, int]]] = {}
        for s, oi, raw in ev.op_dtmul_pts:
            by_op.setdefault(oi, []).append((s, raw))
        dt2_by_op: dict[int, list[tuple[int, int]]] = {}
        for s, oi, dt2 in ev.op_dt2_pts:
            dt2_by_op.setdefault(oi, []).append((s, dt2))
        mul_entries: list[tuple[int, tuple[int, ...], int]] = []
        dt_entries: list[tuple[int, tuple[int, ...], int]] = []
        dt2_entries: list[tuple[int, tuple[int, ...], int]] = []
        for oi in range(4):
            raw_pts = by_op.get(oi, [])
            dt2_pts = dt2_by_op.get(oi, [])
            if not raw_pts and not dt2_pts:
                continue
            start_raw = base.ops[oi].dt_mul
            start_mul = start_raw & 0x0F
            start_dt = _YM_DT_TO_FURNACE[(start_raw >> 4) & 7]
            start_dt2 = (base.ops[oi].dt2_d2r >> 6) & 3
            if raw_pts:
                mul_pts = [(s, raw & 0x0F) for s, raw in raw_pts]
                marks, ok = row_marks(mul_pts, start_mul)
                if ok:
                    ev.op_mul_fx += tuple(
                        (s, oi, value) for s, value in marks
                    )
                else:
                    values = macro_values(mul_pts, start_mul)
                    if len(set(values)) > 1 or values[0] != start_mul:
                        mul_entries.append((oi, values, 1))
                dt_pts = [
                    (s, _YM_DT_TO_FURNACE[(raw >> 4) & 7])
                    for s, raw in raw_pts
                ]
                marks, ok = row_marks(dt_pts, start_dt)
                if ok:
                    ev.op_dt_fx += tuple(
                        (s, oi, value) for s, value in marks
                    )
                else:
                    values = macro_values(dt_pts, start_dt)
                    if len(set(values)) > 1 or values[0] != start_dt:
                        dt_entries.append((oi, values, 1))
            if dt2_pts:
                marks, ok = row_marks(dt2_pts, start_dt2)
                if ok:
                    ev.op_dt2_fx += tuple(
                        (s, oi, value) for s, value in marks
                    )
                else:
                    values = macro_values(dt2_pts, start_dt2)
                    if len(set(values)) > 1 or values[0] != start_dt2:
                        dt2_entries.append((oi, values, 1))
        if not mul_entries and not dt_entries and not dt2_entries:
            continue
        key = (
            "op-dtmul", patch_order[ev.ins],
            tuple(mul_entries), tuple(dt_entries), tuple(dt2_entries),
        )
        idx = idx_map.get(key)
        if idx is None:
            if len(patch_order) >= cap:
                fallbacks += 1
                continue
            changes: dict[str, tuple] = {}
            if mul_entries:
                changes["op_mul_macro"] = tuple(mul_entries)
            if dt_entries:
                changes["op_dt_macro"] = tuple(dt_entries)
            if dt2_entries:
                changes["op_dt2_macro"] = tuple(dt2_entries)
            copy = replace(
                base, uses=1, name=f"{base.name}+mul", base_ins=ev.ins,
                **changes,
            )
            patches[key] = copy
            patch_order.append(key)
            idx = len(patch_order) - 1
            idx_map[key] = idx
        else:
            patches[patch_order[idx]].uses += 1
        ev.ins = idx
        made += 1
    return made, fallbacks


def _bind_ext_levels(
    events: list["NoteEvent"], patches: dict[tuple, FMPatch],
    patch_order: list[tuple], notes: list[tuple[int, int, int, int]],
    floors: dict[int, list[int]],
) -> tuple[int, int]:
    """Give each Ext channel-3 column back the level range it was scripted with.

    An Ext column is its own Furnace channel, and `DIV_CMD_VOLUME` in
    ym2203ext.cpp answers it with `TL = 127 - vol + op.tl` for the one operator
    that column plays (`ops[orderedOps[ch]]`). Volume only attenuates, so the
    instrument carries the *lowest* TL - the loudest level - that any note of
    that operator started on or moved to, and each note rides its own difference.

    `notes` holds (event index, instrument index, operator index, TL at key-on);
    `floors` the running per-operator minimum collected beside them. Without this
    a column that walked `$4A` through 6,7,8...19 played every note at whichever
    level the first interned instrument happened to hold, because the Ext cache
    key ignores TL to keep one shared timbre per voice.

    Returns (notes levelled, notes skipped as unaddressable).
    """
    # The floor of an instrument's operator is shared by every note of that
    # column, so set it once, before any note is rebased onto it.
    for ins_idx, per_op in floors.items():
        if not (0 <= ins_idx < len(patch_order)):
            continue
        patch = patches.get(patch_order[ins_idx])
        if patch is None:
            continue
        ops = list(patch.ops)
        for oi, floor in enumerate(per_op):
            if floor <= 0x7F and ops[oi].tl & 0x7F != floor:
                ops[oi] = replace(ops[oi], tl=floor)
        patch.ops = tuple(ops)  # type: ignore[assignment]

    levelled = skipped = 0
    for ev_i, ins_idx, oi, tl in notes:
        if not (0 <= ev_i < len(events)) or not (0 <= ins_idx < len(patch_order)):
            skipped += 1
            continue
        patch = patches.get(patch_order[ins_idx])
        if patch is None or oi >= len(patch.ops):
            skipped += 1
            continue
        floor = floors.get(ins_idx, [0x7F] * 4)[oi]
        ev = events[ev_i]
        # vol such that 127 - vol + floor == tl. A level under the floor cannot
        # occur here: the floor is the minimum of every level seen for this
        # operator, mid-note moves included.
        ev.vol_tl_base = floor
        ev.vol = max(0, min(127, 127 - (tl - floor)))
        levelled += 1
    return levelled, skipped


def _collect_opl(
    vgm: VgmFile,
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    keyon_times: list[int],
    channel_fx: list[tuple[int, int, int, int]],
    chip_fx: list[tuple],
    *,
    kind: str,
    ids: list[int],
    base: dict[int, int],
    clock: float,
    drums: bool,
    chip_name: str | None = None,
    freq_base: int | None = None,
) -> tuple[dict, list[str]]:
    """Turn an OPL-family register stream into notes and instruments.

    Channels are read through Furnace's own slot and channel tables. A
    converted voice lands on the column that drives the same operators: an
    OPL3 pairs its banks as (0,3), (1,4) and (2,5) per port, and rhythm mode
    takes the last FM channels of bank A for the kit. Key state comes from
    where the chip takes it: bit 5 of the channel's 0xBn register, or one
    latch bit of 0xBD. A driver that keys a channel without touching its
    pitch therefore still starts a note, and a re-pitch with the key already
    down stays a bend inside that note.

    Returns counters for the report and the warnings the caller should show.
    ``freq_base`` is the variant's frequency divider (the OPL4 is not the
    OPL3's ``32768*288``), so the note mapping matches Furnace's own table.
    """
    opl3 = opl3_like(kind)
    states = {cid: OPLState(kind, drums) for cid in ids}
    # Which Furnace channels a register drives. An OPL3 four-operator pair is
    # keyed and tuned on its first channel but takes two operators from each.
    # One frequency register can thus belong to one voice and an operator
    # register to that voice's third or fourth slot.
    freq_owners: dict[int, dict[int, list[int]]] = {}
    op_owners: dict[int, dict[int, list[int]]] = {}
    # Which operator *of the instrument* a slot register tunes. A four-operator
    # voice takes its rows from both channels of the pair, and Furnace walks
    # them through orderedOpsL: the row number is not the operator number there.
    op_index: dict[int, dict[tuple[int, int], int]] = {}
    # The operators one voice owns: (operator index, slot register base). Read at
    # key-on to prime the level curve without building a patch each time.
    chan_ops: dict[int, dict[int, list[tuple[int, int]]]] = {}
    for cid in ids:
        st = states[cid]
        fmap: dict[int, list[int]] = {}
        omap: dict[int, list[int]] = {}
        imap: dict[tuple[int, int], int] = {}
        ops_of: dict[int, list[tuple[int, int]]] = {}
        for fch, cm in enumerate(st.chan_map):
            fmap.setdefault(cm, []).append(fch)
        for row in range(len(st.slots)):
            for fch, slot in enumerate(st.slots[row]):
                if slot == OPL_N:
                    continue
                four = st.is_four_op(fch if not (row > 1 and fch & 1)
                                     else fch - 1)
                # Rows 2/3 are the second bank's operators for the even
                # channels that *can* head a 4-op pair. A two-operator voice
                # does not own them; treating them as its ops proposed
                # modulator effects on operators the platform ignores.
                if row > 1 and not four:
                    continue
                addr = SLOT_ADDR[slot]
                owners = omap.setdefault(addr, [])
                if fch not in owners:
                    owners.append(fch)
                imap.setdefault((addr, fch),
                                ORDERED_OPS_L[row] if four else row)
                # Same pair, grouped the other way: what to prime at key-on.
                ops_of.setdefault(fch, []).append(
                    (ORDERED_OPS_L[row] if four else row, addr))
        freq_owners[cid] = fmap
        op_owners[cid] = omap
        op_index[cid] = imap
        chan_ops[cid] = ops_of

    held: dict[tuple[int, int], int] = {}
    # The pan byte last written for a held channel.  A source that returns
    # from a hard pan to centre must get an explicit 0xFF; omitting it leaves
    # the old pan in force for every later row.
    pan_last: dict[tuple[int, int], int] = {}
    # Row effects that changed a live operator field. Furnace only reloads an
    # instrument when the instrument changes, so a reuse of the same voice
    # would inherit the previous note's MULT/feedback/waveform. Keep the
    # instrument's own value for each changed field and write it back on the
    # next key-on of that same instrument.
    pending_reset: dict[
        tuple[int, int], tuple[int, dict[tuple[int, int], int]]
    ] = {}
    pitch_pts: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    pitch_base: dict[tuple[int, int], tuple[int, int]] = {}
    # Mid-note pitch is compared with what the register holds *now*, not with the
    # value a note started on. A driver that writes vibrato by hand - alternating
    # one channel's f-num between two values every 150 ms, as this rip's
    # slow-attack pad does - returns to the starting pitch on every other write,
    # and matching against the base drops each of those returns: one leg of the
    # oscillation survives and the pitch never comes back. Levels already track
    # their last value (`tl_last`) for exactly this reason.
    pitch_last: dict[tuple[int, int], tuple[int, int]] = {}
    tl_pts: dict[tuple[int, int], list[tuple[int, int]]] = {}
    tl_last: dict[tuple[int, int], int] = {}
    # Level of a kit operator when its note started: the column steps from here.
    tl_open: dict[tuple[int, int], int] = {}
    # Output level per operator of a sounding voice. The volume column only
    # reaches an operator marked KVS. A scripted modulator has to travel as
    # an instrument macro instead, and both are recorded, each on its own path.
    op_tl_pts: dict[tuple[int, int], dict[int, list[tuple[int, int]]]] = {}
    op_tl_last: dict[tuple[int, int, int], int] = {}
    off_idx: list[int] = []
    stats = {
        "notes": 0, "drums": 0, "four_op": 0,
        "mode_midnote": 0, "lost_melodic": 0, "repeat_keyon": 0,
        "ins_reused": 0,
    }
    warnings: list[str] = []

    def op_is_carrier(cid: int, fch: int, oi: int) -> bool:
        """Whether an operator reaches the output, i.e. rides the volume column.

        Furnace scales a level by the channel volume only where `kvs==2` and
        the operator is an output (opl.cpp KVSL). This mirrors that test. A
        kit column is one operator of its own and always counts as one.
        """
        st = states[cid]
        if st.drums and fch > st.melodic:
            return True
        return oi in carriers_for(st.algorithm(fch), False, True,
                                  st.is_four_op(fch))

    def normalized_opl(patch: FMPatch) -> tuple[FMPatch, int]:
        """Carrier level out of the instrument and into the volume column.

        Furnace scales an operator marked KVS by ``63 - clamp(vol - tl, 0, 63)``.
        Putting the loudest carrier at 0 and its attenuation in the column
        reproduces the logged register exactly while letting every note of one
        timbre share an instrument. Sixty-three is the top of an OPL column:
        `getVolMax` answers 63, and the platform masks the value to six bits.
        """
        ops = list(patch.ops)
        carriers = patch.carriers()
        floor = min((ops[i].tl & 0x3F) for i in carriers) if carriers else 0
        for i in carriers:
            ops[i] = replace(ops[i], tl=(ops[i].tl & 0x3F) - floor)
        return replace(patch, ops=tuple(ops)), 63 - min(floor, 63)  # type: ignore[arg-type]

    def opl_base_effect(patch: FMPatch, cmd: int, e: int) -> int | None:
        """The instrument's value for a live operator effect, encoded for the row.

        `e` is the effect operator index (0-3). A four-operator voice maps it
        through the platform's orderedOpsL; a two-operator voice uses it
        directly. Returns None for a field the patch does not have.
        """
        if cmd == 0x11:
            return patch.fb & 7
        op = ORDERED_OPS_L[e] if patch.four else e
        if not (0 <= op < len(patch.ops)):
            return None
        o = patch.ops[op]
        if cmd == 0x16:
            return ((e + 1) << 4) | (o.mult & 15)
        if 0x1A <= cmd <= 0x1D:
            return o.ar & 15
        if 0x57 <= cmd <= 0x5A:
            return o.dr & 15
        if cmd == 0x50:
            return ((e + 1) << 4) | (o.am & 1)
        if cmd == 0x51:
            return ((e + 1) << 4) | (o.sl & 15)
        if cmd == 0x52:
            return ((e + 1) << 4) | (o.rr & 15)
        if cmd == 0x53:
            return ((e + 1) << 4) | (o.vib & 1)
        if cmd == 0x55:
            return ((e + 1) << 4) | (o.sus & 1)
        if cmd == 0x5B:
            return ((e + 1) << 4) | (o.ksr & 1)
        if cmd == 0x2A:
            return ((e + 1) << 4) | (o.ws & 7)
        return None

    def live_op_fx(
        key: tuple[int, int], ch: int, cmd: int, e: int,
        value: int, sample: int,
    ) -> None:
        """Place one live operator effect and remember its base value."""
        channel_fx.append((sample, ch, cmd, value))
        idx = held.get(key)
        if idx is None:
            return
        old_ins = events[idx].ins
        if not (0 <= old_ins < len(patch_order)):
            return
        patch = patches.get(patch_order[old_ins])
        if patch is None:
            return
        base = opl_base_effect(patch, cmd, e)
        if base is None:
            return
        if key not in pending_reset:
            pending_reset[key] = (old_ins, {})
        pending_reset[key][1][(cmd, e)] = base

    # A Furnace module stores instrument indices as one byte, so 256 is the
    # hard ceiling. Leave room for the sample instruments the writer adds.
    ins_cap = 200
    reuse_by_shape: dict[tuple, tuple] = {}

    def shape_key(snap) -> tuple:
        """Timbre with the modulator level folded out.

        A driver that ramps the FM depth per note (MoonDriver does: 22, 23,
        24... on registers ``$40``-``$42``) would otherwise need one
        instrument per step, and a 2-minute track crossed Furnace's one-byte
        instrument index. The loudest depth is the instrument; the carrier
        level still rides the volume column.
        """
        carriers = snap.carriers()
        ops = tuple(
            (i, snap.ops[i].as_tuple(include_tl=(i in carriers)))
            for i in range(4)
        )
        return ("opl-shape", snap.alg & 3, snap.fb & 7, snap.opl, snap.four,
                snap.drum, ops)

    def reuse(snap) -> tuple:
        """An existing patch with the same carrier shape, else the first."""
        shape = (snap.alg & 7, snap.carriers())
        key = reuse_by_shape.get(shape)
        if key is None or key not in patches:
            key = patch_order[0]
        return key

    def loudest(base: FMPatch, snap: FMPatch) -> FMPatch:
        """Keep the deepest modulator level seen for a shared shape.

        A driver that fades the FM depth by raising a modulator's ``$4n``
        writes a different byte on every note. Those levels share one patch
        (``shape_key`` folds them out), so the patch keeps the loudest one
        instead of whichever note happened to arrive first.
        """
        carriers = set(base.carriers())
        ops = list(base.ops)
        changed = False
        for i in range(4):
            if i in carriers:
                continue
            here = snap.ops[i].tl & 0x3F
            if here < (ops[i].tl & 0x3F):
                ops[i] = replace(ops[i], tl=here)
                changed = True
        return replace(base, ops=tuple(ops)) if changed else base

    def intern(cid: int, fch: int) -> tuple[int, int]:
        """Instrument index and volume column for the voice a channel holds."""
        st = states[cid]
        if drums and fch > st.melodic:
            snap = st.drum_snapshot()
            key = ("drum",) + snap.identity()
            if key not in patches:
                if len(patch_order) >= ins_cap:
                    key = reuse(snap)
                    stats["ins_reused"] += 1
                else:
                    snap.name = f"OPL drum {len(patch_order):02d}"
                    patches[key] = snap
                    patch_order.append(key)
            p = patches[key]
            p.uses += 1
            return patch_order.index(key), 63
        snap, vol = normalized_opl(st.snapshot(fch))
        if st.is_four_op(fch):
            stats["four_op"] += 1
        key = shape_key(snap)
        if key not in patches:
            if len(patch_order) >= ins_cap:
                key = reuse(snap)
                stats["ins_reused"] += 1
            else:
                fam = "OPL3 4op" if st.is_four_op(fch) else kind.upper()
                snap.name = f"{fam} {len(patch_order):02d}"
                patches[key] = snap
                patch_order.append(key)
                reuse_by_shape.setdefault(
                    (snap.alg & 7, snap.carriers()), key,
                )
        else:
            patches[key] = loudest(patches[key], snap)
        p = patches[key]
        p.uses += 1
        return patch_order.index(key), vol

    def close_note(cid: int, fch: int, sample: int, *, off: bool) -> None:
        key = (cid, fch)
        idx = held.pop(key, None)
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
            if merged and merged[0][0] - ev.sample <= 120 and merged[0][1:] != pitch_base[key]:
                ev.note, ev.e5 = opl_note_for(
                    merged[0][1], merged[0][2], clock, opl3, freq_base,
                )
                merged = [(ev.sample, *merged[0][1:])] + merged[1:]
                pitch_base[key] = merged[0][1:]
            distinct: list[tuple[int, int, int]] = []
            for p in merged:
                if not distinct or p[1:] != distinct[-1][1:]:
                    distinct.append(p)
            if len(distinct) > 1:
                ev.fm_pitch_pts = tuple(distinct)
        tl = tl_pts.pop(key, [])
        if tl:
            if ev.drum:
                # The kit shares one instrument across its four columns, and the
                # platform writes an operator's logged level plus the column's
                # move from the top. A level step rides the column directly.
                shift = tl_open.pop(key, tl[0][1])
                # A volume point is a triple: sample, column value and pan. The
                # OPL has no per-point pan, and the last slot stays empty.
                ev.vol_pts = tuple(
                    (s, max(0, min(63, 63 + shift - value)), None)
                    for s, value in tl
                )
            else:
                ev.tl_pts = tuple(tl)
        # Hand the level moves on to the macro pass. Keep a single move too: one
        # brightness step inside a long note is exactly what a driver uses to
        # change colour, and `_bind_op_macros` already discards the curve when the
        # value it holds never differs from the instrument's.
        curves = op_tl_pts.pop(key, {})
        ev.opl_op_tl = tuple(
            (oi, tuple(pts)) for oi, pts in sorted(curves.items()) if pts
        )
        for oi in range(4):
            op_tl_last.pop((cid, fch, oi), None)
        tl_open.pop(key, None)
        pan_last.pop(key, None)
        if off:
            ev.end = sample
            events.append(NoteEvent(
                sample=sample, ch=ev.ch, on=False, note=180, ins=-1,
                vol=-1, e5=0x80, pan=0,
            ))
            off_idx.append(len(events) - 1)

    def open_note(cid: int, fch: int, sample: int) -> None:
        st = states[cid]
        key = (cid, fch)
        kit = drums and fch > st.melodic
        close_note(cid, fch, sample, off=False)
        block, fnum = st.frequency(fch)
        note, e5 = opl_note_for(block, fnum, clock, opl3, freq_base)
        ins, vol = intern(cid, fch)
        # A reused instrument is not reloaded by the platform. Restore the
        # fields that the previous note changed with row effects, so this
        # note starts on the instrument's own MULT/feedback/AR/... values.
        resets = pending_reset.pop(key, None)
        if resets is not None:
            old_ins, fields = resets
            if old_ins == ins:
                for (cmd, e), base_val in fields.items():
                    channel_fx.append((
                        sample, base[cid] + fch, cmd, base_val,
                    ))
        ev_pan = 0 if kit else st.pan_byte(fch)
        events.append(NoteEvent(
            sample=sample,
            ch=base[cid] + fch,
            on=True,
            note=note,
            ins=ins,
            vol=vol,
            e5=e5,
            # The kit's own 0xCn bits mean nothing in rhythm mode. A percussion
            # column never gets a pan effect.
            pan=ev_pan,
            sweep_div=2.0,
            e5_lo=0x00,
            e5_hi=0xFF,
            drum=kit,
        ))
        held[key] = len(events) - 1
        if not kit:
            pan_last[key] = ev_pan
        pitch_base[key] = (block, fnum)
        pitch_last[key] = (block, fnum)
        pitch_pts[key] = [(sample, block, fnum)]
        tl_pts[key] = []
        tl_last[key] = st.channel_tl(fch)
        tl_open[key] = tl_last[key]
        # Prime each operator with the level its note starts on: a macro runs
        # from the key-on, and entry zero is the level the note attacks with.
        # Reading the register page rather than a snapshot keeps a held note
        # from allocating a patch, and an operator the voice does not own is
        # dropped with pop() to keep a stale level out of the next note's
        # entry zero.
        op_tl_pts[key] = {}
        owned: set[int] = set()
        for oi, addr in chan_ops[cid].get(fch, ()):
            level = st.regs[0x40 | addr] & 0x3F
            op_tl_last[(cid, fch, oi)] = level
            owned.add(oi)
            if not op_is_carrier(cid, fch, oi):
                # Entry zero of the effect/macro curve: the level the note
                # attacks with, before any later $4n write.
                op_tl_pts[key][oi] = [(sample, level)]
        for stale in [k for k in op_tl_last if k[0] == cid and k[1] == fch
                      and k[2] not in owned]:
            op_tl_last.pop(stale, None)
        keyon_times.append(sample)
        if kit:
            stats["drums"] += 1
        else:
            stats["notes"] += 1

    def release_reinterpreted(cid: int, sample: int) -> None:
        """Close notes on the channels rhythm mode takes away.

        Percussion mode reassigns bank A's last FM channels, so a voice already
        sounding there cannot be held: the chip stops shaping it as an FM note
        at the same write that enables the kit.
        """
        st = states[cid]
        for fch in [k[1] for k in held if k[0] == cid and k[1] > st.melodic]:
            close_note(cid, fch, sample, off=True)
            stats["mode_midnote"] += 1

    last_amd = last_vib = -1

    def keyed_here(w) -> bool:
        """True when this write can start a note rather than load a register."""
        r = w.reg & 0xFF
        if 0xB0 <= r <= 0xB8:
            return bool(w.val & 0x20)
        if r == 0xBD:
            # Rhythm mode on, or any trigger bit set: the kit keys from here.
            return bool(w.val & 0x3F)
        return False

    def ordered(writes):
        """Same-sample key bits last, so the byte beside them is already loaded.

        A driver that sets a connection, level or waveform in the same sample as
        its key bit means it for the voice that is starting: the chip takes the
        bytes in stream order, and so does a Furnace row when it applies an
        instrument and then keys it. Reading the key first would snapshot the
        patch of the note that just ended - which is what the YM2612 path had to
        fix in exactly this way.
        """
        out = []
        i, n = 0, len(writes)
        while i < n:
            j = i + 1
            s = writes[i].sample
            while j < n and writes[j].sample == s:
                j += 1
            group = writes[i:j]
            out.extend(w for w in group if not keyed_here(w))
            out.extend(w for w in group if keyed_here(w))
            i = j
        return out

    source = chip_name or kind
    for w in ordered(vgm.writes):
        if w.chip != source:
            continue
        cid = w.chip_id
        st = states.get(cid)
        if st is None:
            continue
        # The OPL4's port 2 is its wave half, not a third FM bank. Feeding
        # those bytes to the FM image invents key-ons: wave register 0xB5 holds
        # a pitch byte whose bit 5 then reads as an FM key.
        if (w.reg >> 8) > 1:
            continue
        port = (w.reg >> 8) & 1 if opl3 else 0
        r = w.reg & 0xFF
        val = w.val
        addr = (port << 8) | r

        if r == 0xBD and port == 0:
            old = st.regs[addr]
            was_rhythm = bool(old & 0x20)
            old_trig = (old & 0x1F) if was_rhythm else 0
            st.write(port, r, val)
            is_rhythm = bool(val & 0x20)
            trig = (val & 0x1F) if is_rhythm else 0
            # The two depth bits are global, and Furnace gives each its own
            # effect: 10xx amplitude modulation depth, 17xx vibrato depth.
            amd = int(bool(val & 0x80))
            vib = int(bool(val & 0x40))
            if amd != last_amd:
                chip_fx.append((w.sample, 0x10, amd))
                last_amd = amd
            if vib != last_vib:
                chip_fx.append((w.sample, 0x17, vib))
                last_vib = vib
            if is_rhythm and not was_rhythm:
                release_reinterpreted(cid, w.sample)
            for bit, _name in DRUM_BITS:
                if (trig & bit) and not (old_trig & bit):
                    fch = drum_channel(bit, st.melodic)
                    if not is_rhythm:
                        continue
                    open_note(cid, fch, w.sample)
                elif (old_trig & bit) and not (trig & bit):
                    close_note(cid, drum_channel(bit, st.melodic), w.sample, off=True)
            if is_rhythm and old_trig and trig == old_trig:
                # The same latch byte twice in a row is a retrigger on real
                # silicon, which one pattern cell cannot express.
                stats["repeat_keyon"] += 1
            continue

        if 0xB0 <= r <= 0xB8:
            old = st.regs[addr]
            st.write(port, r, val)
            was_on = bool(old & 0x20)
            is_on = bool(val & 0x20)
            # The same byte carries the top two f-num bits. A key-on row can
            # also move the pitch of whatever voice this register tunes.
            chan_off = (port << 8) | (r - 0xB0)
            for fch in freq_owners[cid].get(chan_off, ()):
                key = (cid, fch)
                if drums and fch >= st.melodic:
                    # Rhythm mode keys the kit through 0xBD. This channel's own
                    # key bit is only a pitch write here. A set bit while
                    # rhythm is still off was a melodic voice with no column.
                    if is_on and not was_on and fch > st.melodic:
                        stats["lost_melodic"] += 1
                elif is_on and not was_on:
                    if fch < 12 and (fch & 1) and opl3 and st.is_four_op(fch - 1):
                        pass  # the pair's first channel keys the whole voice
                    else:
                        open_note(cid, fch, w.sample)
                elif was_on and not is_on:
                    close_note(cid, fch, w.sample, off=True)
                if key in held:
                    block, fnum = st.frequency(fch)
                    # This write carries the top two f-num bits as well. A held
                    # voice can move pitch here without any key transition.
                    if (block, fnum) != pitch_last[key]:
                        pitch_pts[key].append((w.sample, block, fnum))
                        pitch_last[key] = (block, fnum)
            continue

        prev_val = st.regs[addr]
        st.write(port, r, val)

        if 0xC0 <= r <= 0xC8:
            # Pan lives in the unused high bits of the feedback/connection
            # byte.  A live change must reach a held note; the note-start byte
            # alone would leave a hard pan latched across the next rows.
            chan_off = (port << 8) | (r - 0xC0)
            for fch in freq_owners[cid].get(chan_off, ()):
                key = (cid, fch)
                if key not in held or (drums and fch > st.melodic):
                    continue
                # Feedback is part of the timbre and a tracker can raise it
                # mid-note. Keep the change as 11xx; the connection bit has no
                # OPL row effect in Furnace and stays with the instrument.
                old_fb = (prev_val >> 1) & 7
                new_fb = (val >> 1) & 7
                if new_fb != old_fb:
                    live_op_fx(
                        (cid, fch), base[cid] + fch, 0x11, 0,
                        new_fb, w.sample,
                    )
                new_pan = st.pan_byte(fch)
                if new_pan and new_pan != pan_last.get(key):
                    channel_fx.append((
                        w.sample, base[cid] + fch, 0x08, new_pan,
                    ))
                    pan_last[key] = new_pan
            continue

        if 0xA0 <= r <= 0xA8:
            chan_off = (port << 8) | (r - 0xA0)
            for fch in freq_owners[cid].get(chan_off, ()):
                key = (cid, fch)
                if key not in held:
                    continue
                block, fnum = st.frequency(fch)
                if (block, fnum) != pitch_last[key]:
                    pitch_pts[key].append((w.sample, block, fnum))
                    pitch_last[key] = (block, fnum)
            continue
        slot = r & 0x1F
        if (r - slot) in (0x20, 0x40, 0x60, 0x80, 0xE0) and slot <= 0x15:
            # Operator page. A slot's registers share their low five bits:
            # 0x23, 0x43 and 0xE3 are one operator's AM/multiplier, level and
            # waveform byte. The level rides the volume column (or an
            # operator macro); every other field is a live voicing change and
            # becomes a row effect, with the instrument's own value restored
            # when the same voice starts the next note.
            owners = op_owners[cid].get((port << 8) | slot, ())
            if not owners:
                continue
            if (r - slot) != 0x40:
                if prev_val == val:
                    continue        # the driver re-wrote the same byte; no change
                page = r - slot
                for fch in owners:
                    if (cid, fch) not in held:
                        continue
                    if drums and fch > st.melodic:
                        # A kit column is one operator and one shared patch;
                        # a live per-operator write there has no unambiguous
                        # effect column, so leave it on the instrument.
                        continue
                    oi = op_index[cid].get(((port << 8) | slot, fch))
                    if oi is None:
                        continue
                    four = st.is_four_op(fch)
                    # The platform walks a four-operator voice through
                    # orderedOpsL; the two-operator row number is the column.
                    fx_op = ORDERED_OPS_L[oi] if four else oi
                    ch = base[cid] + fch
                    if page == 0x20:
                        # AM/VIB/EG/KSR/MULT. These are live voicing changes,
                        # not just instrument state: a tracker may raise the
                        # multiplier or flip a waveform mid-note. Furnace has a
                        # row effect for each field, so keep them.
                        for bit, cmd in (
                            (7, 0x50), (6, 0x53), (5, 0x55), (4, 0x5B),
                        ):
                            if ((prev_val >> bit) & 1) != ((val >> bit) & 1):
                                live_op_fx(
                                    (cid, fch), ch, cmd, fx_op,
                                    ((fx_op + 1) << 4) | ((val >> bit) & 1),
                                    w.sample,
                                )
                        if (prev_val & 15) != (val & 15):
                            live_op_fx(
                                (cid, fch), ch, 0x16, fx_op,
                                ((fx_op + 1) << 4) | (val & 15),
                                w.sample,
                            )
                    elif page == 0x60:
                        # Attack/decay.
                        if ((prev_val >> 4) & 15) != ((val >> 4) & 15):
                            live_op_fx(
                                (cid, fch), ch, 0x1A + fx_op, fx_op,
                                (val >> 4) & 15, w.sample,
                            )
                        if (prev_val & 15) != (val & 15):
                            live_op_fx(
                                (cid, fch), ch, 0x57 + fx_op, fx_op,
                                val & 15, w.sample,
                            )
                    elif page == 0x80:
                        # Sustain/release.
                        if ((prev_val >> 4) & 15) != ((val >> 4) & 15):
                            live_op_fx(
                                (cid, fch), ch, 0x51, fx_op,
                                ((fx_op + 1) << 4) | ((val >> 4) & 15),
                                w.sample,
                            )
                        if (prev_val & 15) != (val & 15):
                            live_op_fx(
                                (cid, fch), ch, 0x52, fx_op,
                                ((fx_op + 1) << 4) | (val & 15),
                                w.sample,
                            )
                    elif page == 0xE0:
                        # Waveform, 3 bits on OPL3 and 2 on OPL2.
                        mask = 7 if st.opl3 else 3
                        if (prev_val & mask) != (val & mask):
                            live_op_fx(
                                (cid, fch), ch, 0x2A, fx_op,
                                ((fx_op + 1) << 4) | (val & mask),
                                w.sample,
                            )
                continue
            for fch in owners:
                key = (cid, fch)
                if key not in held:
                    continue
                # Record this operator's own level. Only the carrier's composite
                # level can ride the volume column. A write that moves just a
                # modulator stops here rather than testing (and overwriting) the
                # channel value: keeping it would latch the modulator's number as
                # the carrier reference and drop every later carrier step.
                oi = op_index[cid].get(((port << 8) | slot, fch))
                if oi is not None:
                    here = val & 0x3F
                    if op_tl_last.get((cid, fch, oi)) != here:
                        ops = op_tl_pts.setdefault(key, {})
                        ops.setdefault(oi, []).append((w.sample, here))
                        op_tl_last[(cid, fch, oi)] = here
                    if not op_is_carrier(cid, fch, oi):
                        # A modulator. The volume column cannot reach it, and
                        # this level only survives as the operator macro built
                        # later.
                        continue
                cur = st.channel_tl(fch)
                if cur != tl_last[key]:
                    tl_pts[key].append((w.sample, cur))
                    tl_last[key] = cur

    for cid, fch in list(held):
        close_note(cid, fch, vgm.total_samples or 0, off=False)

    if drums:
        lost = stats["lost_melodic"]
        cut = stats["mode_midnote"]
        warnings.append(
            "rhythm mode is on, so bank A's last FM channels are the kit"
            + (f"; {lost} melodic key-on(s) there had no column and "
               f"{cut} held note(s) stopped at a mode switch" if (lost or cut) else "")
        )
    if stats["repeat_keyon"]:
        warnings.append(
            f"{stats['repeat_keyon']} repeated drum trigger byte(s) cannot "
            "retrigger a pattern cell; one hit is written"
        )
    # A re-key on the same row (0xBD cleared and set inside one sample group)
    # is a retrigger, not a release followed by a new note.
    ons_by_ch: dict[int, list[int]] = {}
    for e in events:
        if e.on:
            ons_by_ch.setdefault(e.ch, []).append(e.sample)
    drop = {
        idx for idx in off_idx
        if any(abs(t - events[idx].sample) <= 2 for t in ons_by_ch.get(events[idx].ch, ()))
    }
    if drop:
        events[:] = [e for i, e in enumerate(events) if i not in drop]
    return stats, warnings


def _collect_opll(
    vgm: VgmFile,
    events: list[NoteEvent],
    patches: dict[tuple, FMPatch],
    patch_order: list[tuple],
    keyon_times: list[int],
    channel_fx: list[tuple[int, int, int, int]],
    chip_fx: list[tuple],
    *,
    ids: list[int],
    base: dict[int, int],
    clock: float,
    drums: bool,
) -> tuple[dict, list[str]]:
    """Turn a YM2413 register stream into notes and OPLL instruments.

    The chip keys a channel with bit 4 of ``$2x`` and carries the level in the
    low nibble of ``$3x``; there is no separate carrier TL register. In
    rhythm mode ``$0e`` keys five fixed drum voices and the ``$36-$38`` bytes
    hold their levels, so those columns take their note from the shared
    frequency registers and their volume from the nibble the chip reads.
    """
    states = {cid: OPLLState(drums) for cid in ids}
    held: dict[tuple[int, int], int] = {}
    # A key-off does not necessarily end the driver's control of a voice.
    # Percussion drivers (the clap in mahbod-usedtosay) key off the carrier
    # and then keep walking `$3x`, using the level nibble as a burst gate
    # during the release tail.  Keep such a voice "held" so those level
    # points reach its volume macro, but still release it at the key bit.
    released: dict[tuple[int, int], int] = {}
    pitch_pts: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    pitch_base: dict[tuple[int, int], tuple[int, int]] = {}
    pitch_last: dict[tuple[int, int], tuple[int, int]] = {}
    vol_pts: dict[tuple[int, int], list[tuple[int, int]]] = {}
    vol_last: dict[tuple[int, int], int] = {}
    held_preset: dict[tuple[int, int], int] = {}
    preset_pts: dict[tuple[int, int], list[tuple[int, int]]] = {}
    phase_pts: dict[tuple[int, int], list[int]] = {}
    # A key-on is opened after OPLL_SETTLE so the low f-num byte lands.  Keep
    # the patch and level from the key-on instant: a driver that retriggers
    # faster than the settle window must not give the first hit the timbre of
    # the next one.
    pending: dict[tuple[int, int], dict] = {}
    off_idx: list[int] = []
    stats = {
        "notes": 0, "drums": 0, "midnote_inst": 0, "inst_fx": 0,
        "lost_melodic": 0,
    }
    warnings: list[str] = []

    def hw_channels(hw: int) -> tuple[int, ...]:
        """Furnace columns a chip channel's frequency registers drive.

        In rhythm mode the kit shares one hardware voice between two columns:
        channel 8 is snare and hi-hat, channel 9 is tom and top cymbal. A
        pitch write there belongs to both, so the engine's own `drumSlot`
        table (opll.cpp) is read backwards here. Without the kit, one chip
        channel is exactly one column.
        """
        if not drums:
            return (hw,) if hw < 9 else ()
        if hw < 6:
            return (hw,)
        return {6: (6,), 7: (7, 10), 8: (8, 9)}.get(hw, ())

    def column_level(cid: int, fch: int) -> int:
        st = states[cid]
        if drums and fch >= 6:
            for entry in OPLL_DRUMS:
                if entry[0] == fch:
                    return 15 - st.drum_volume(entry[3], entry[4])
            return 15
        return 15 - st.volume(fch)

    def intern_patch(snap: FMPatch, cid: int, fch: int) -> int:
        if drums and fch >= 6:
            key = ("opll-drum",) + snap.identity()
        else:
            key = snap.identity()
        if key not in patches:
            if drums and fch >= 6:
                snap.name = "OPLL drums"
            elif snap.preset:
                snap.name = f"OPLL preset {snap.preset:02d}"
            else:
                snap.name = f"OPLL {len(patch_order):02d}"
            patches[key] = snap
            patch_order.append(key)
        p = patches[key]
        p.uses += 1
        return patch_order.index(key)

    def intern(cid: int, fch: int) -> int:
        st = states[cid]
        snap = st.drum_patch() if drums and fch >= 6 else st.snapshot(fch)
        return intern_patch(snap, cid, fch)

    def close_note(cid: int, fch: int, sample: int, *, off: bool) -> None:
        key = (cid, fch)
        idx = held.pop(key, None)
        if idx is None:
            return
        ev = events[idx]
        rel = released.pop(key, None)
        pts = pitch_pts.pop(key, [])
        if rel is not None:
            # `sample` is the next key-on here; its own pitch writes belong to
            # the new note.
            pts = [p for p in pts if p[0] < sample]
        if pts:
            merged: list[tuple[int, int, int]] = []
            for p in pts:
                if merged and p[0] - merged[-1][0] <= 16:
                    merged[-1] = p
                else:
                    merged.append(p)
            if (merged and merged[0][0] - ev.sample <= 120
                    and merged[0][1:] != pitch_base[key]):
                ev.note, ev.e5 = opll_note_for(
                    merged[0][1], merged[0][2], clock,
                )
                merged = [(ev.sample, *merged[0][1:])] + merged[1:]
                pitch_base[key] = merged[0][1:]
            distinct: list[tuple[int, int, int]] = []
            for p in merged:
                if not distinct or p[1:] != distinct[-1][1:]:
                    distinct.append(p)
            if len(distinct) > 1:
                ev.fm_pitch_pts = tuple(distinct)
        vp = vol_pts.pop(key, [])
        if rel is not None:
            vp = [(s, v) for s, v in vp if s < sample]
        if vp:
            ev.vol_pts = tuple((s, max(0, min(15, v)), None) for s, v in vp)
        pp = preset_pts.pop(key, [])
        if rel is not None:
            pp = [(s, p) for s, p in pp if s < sample]
        if pp:
            ev.opll_preset_pts = tuple(pp)
        ph = phase_pts.pop(key, [])
        if rel is not None:
            ph = [t for t in ph if t < sample]
        if ph:
            ev.opll_phase_pts = tuple(ph)
        for d in (pitch_pts, pitch_base, pitch_last, vol_pts, vol_last,
                  held_preset, preset_pts, phase_pts):
            d.pop(key, None)
        if rel is not None:
            # The key-off must still reach the chip: it starts the release
            # that gives a clap its noise character. The driver's later `$3x`
            # steps stay in the volume macro, which Furnace keeps running
            # through release, so the gesture is not cut short.
            end = rel
            ev.end = end
            events.append(NoteEvent(
                sample=end, ch=ev.ch, on=False, note=180, ins=-1,
                vol=-1, e5=0x80, pan=0,
            ))
            off_idx.append(len(events) - 1)
        elif off:
            ev.end = sample
            events.append(NoteEvent(
                sample=sample, ch=ev.ch, on=False, note=180, ins=-1,
                vol=-1, e5=0x80, pan=0,
            ))
            off_idx.append(len(events) - 1)

    def open_note(
        cid: int, fch: int, sample: int, snap: FMPatch | None = None,
        vol: int | None = None, pend: dict | None = None,
    ) -> None:
        st = states[cid]
        close_note(cid, fch, sample, off=False)
        hw = fch
        if drums and fch >= 6:
            for entry in OPLL_DRUMS:
                if entry[0] == fch:
                    hw = entry[2]
                    break
        if pend is not None and "fnum" in pend:
            block, fnum = pend["fnum"]
        else:
            block, fnum = st.block_fnum(hw)
        note, e5 = opll_note_for(block, fnum, clock)
        if snap is None:
            snap = st.drum_patch() if drums and fch >= 6 else st.snapshot(fch)
        ins = intern_patch(snap, cid, fch)
        if vol is None:
            vol = column_level(cid, fch)
        key = (cid, fch)
        events.append(NoteEvent(
            sample=sample, ch=base[cid] + fch, on=True, note=note, ins=ins,
            vol=vol, e5=e5, pan=0, sweep_div=2.0, e5_lo=0x00,
            e5_hi=0xFF, drum=bool(drums and fch >= 6),
        ))
        held[key] = len(events) - 1
        pitch_base[key] = (block, fnum)
        pitch_last[key] = (block, fnum)
        pitch_pts[key] = [(sample, block, fnum)]
        vol_pts[key] = list(pend["volpts"]) if pend else []
        vol_last[key] = (
            pend["vol_last"] if pend else vol
        )
        held_preset[key] = (
            pend["preset"][-1][1] if pend and pend["preset"]
            else snap.preset if (not drums and hw < 9) else 0
        )
        preset_pts[key] = list(pend["preset"]) if pend else []
        phase_pts[key] = list(pend["phase"]) if pend else []
        keyon_times.append(sample)
        if drums and fch >= 6:
            stats["drums"] += 1
        else:
            stats["notes"] += 1

    def touch_freq(cid: int, hw: int, sample: int) -> None:
        st = states[cid]
        block, fnum = st.block_fnum(hw)
        for fch in hw_channels(hw):
            key = (cid, fch)
            if key in pending:
                pending[key]["fnum"] = (block, fnum)
            if (key not in pending and key in held
                    and (block, fnum) != pitch_last[key]):
                pitch_pts[key].append((sample, block, fnum))
                pitch_last[key] = (block, fnum)

    def update_level(cid: int, fch: int, sample: int) -> None:
        key = (cid, fch)
        # A retrigger can leave the previous (released) note in `held` while
        # the new key-on waits out the settle window. `$3x` writes from that
        # instant belong to the new voice, so the pending note wins.
        item = pending.get(key)
        if item is not None:
            level = column_level(cid, fch)
            if level != item["vol_last"]:
                item["volpts"].append((sample, level))
                item["vol_last"] = level
            return
        if key in held:
            level = column_level(cid, fch)
            if level != vol_last[key]:
                vol_pts[key].append((sample, level))
                vol_last[key] = level
            return

    def keyed_here(w) -> bool:
        r = w.reg & 0xFF
        if 0x20 <= r <= 0x28:
            return bool(w.val & 0x10)
        if r == 0x0E:
            return bool(w.val & 0x1F)
        return False

    def ordered(writes):
        out = []
        i, n = 0, len(writes)
        while i < n:
            j = i + 1
            s = writes[i].sample
            while j < n and writes[j].sample == s:
                j += 1
            group = writes[i:j]
            out.extend(w for w in group if not keyed_here(w))
            out.extend(w for w in group if keyed_here(w))
            i = j
        return out

    def key_on_edge(cid: int, fch: int, sample: int) -> None:
        """Arm a voice the moment its key bit rises.

        The note is not opened there: ``$2x`` carries only the top bit of the
        f-num, and drivers write ``$1x`` a sample or two after raising bit 4.
        Reading the chip at the key therefore opens the voice mid-write - flat
        by anything up to an octave on a YM2413 whose f-num is nine bits. The
        open waits for ``OPLL_SETTLE`` so the voice takes the page the driver
        lands on, which is also what one pattern row can hold.
        """
        flush_one(cid, fch)
        st = states[cid]
        snap = st.drum_patch() if drums and fch >= 6 else st.snapshot(fch)
        vol = column_level(cid, fch)
        hw = fch
        if drums and fch >= 6:
            for entry in OPLL_DRUMS:
                if entry[0] == fch:
                    hw = entry[2]
                    break
        pending[(cid, fch)] = {
            "t": sample, "snap": snap, "vol": vol,
            "volpts": [], "vol_last": vol,
            "preset": [], "phase": [],
            "preset_last": snap.preset if not (drums and fch >= 6) else 0,
            "fnum": st.block_fnum(hw),
        }

    def flush_one(cid: int, fch: int) -> None:
        item = pending.pop((cid, fch), None)
        if item is not None:
            open_note(
                cid, fch, item["t"], item["snap"], item["vol"], item,
            )

    def flush_due(sample: int) -> None:
        for key, item in list(pending.items()):
            if item["t"] + OPLL_SETTLE < sample:
                cid, fch = key
                pending.pop(key, None)
                open_note(
                    cid, fch, item["t"], item["snap"], item["vol"], item,
                )

    def release_note(cid: int, fch: int, sample: int) -> None:
        """Keep collecting `$3x` level automation after a key-off.

        Drivers use the level nibble as a burst gate during the release tail
        (a clap is a chain of these). The note still keys off at `sample`, but
        the voice stays in `held` so the later writes reach its volume macro;
        it is finalised at the next key-on or when the log ends.
        """
        key = (cid, fch)
        if key not in held:
            return
        released[key] = sample

    stream = ordered(vgm.writes)
    for w in stream:
        # Settle first, but only once the window is fully behind this write: a
        # register landing exactly at the window's end belongs to the key.
        flush_due(w.sample)
        if w.chip != "opll":
            continue
        cid = w.chip_id
        st = states.get(cid)
        if st is None:
            continue
        r = w.reg & 0xFF
        val = w.val & 0xFF
        prev = st.regs[r]

        if r == 0x0E:
            st.write(r, val)
            was_rhythm = bool(prev & 0x20)
            is_rhythm = bool(val & 0x20)
            old_trig = (prev & 0x1F) if was_rhythm else 0
            trig = (val & 0x1F) if is_rhythm else 0
            if is_rhythm and not was_rhythm and not drums:
                # The kit takes channels 6-8 away from the melodic section.
                for hw in (6, 7, 8):
                    for fch in hw_channels(hw):
                        flush_one(cid, fch)
                        close_note(cid, fch, w.sample, off=True)
            if drums:
                for entry in OPLL_DRUMS:
                    fch, _name, _hw, _reg, _shift, bit = entry
                    if (trig & bit) and not (old_trig & bit) and is_rhythm:
                        key_on_edge(cid, fch, w.sample)
                    elif (old_trig & bit) and not (trig & bit):
                        flush_one(cid, fch)
                        close_note(cid, fch, w.sample, off=True)
            continue

        if 0x10 <= r <= 0x18:
            st.write(r, val)
            touch_freq(cid, r - 0x10, w.sample)
            continue

        if 0x20 <= r <= 0x28:
            st.write(r, val)
            hw = r - 0x20
            was_on = bool(prev & 0x10)
            is_on = bool(val & 0x10)
            if drums:
                if hw >= 6:
                    if is_on and not was_on:
                        stats["lost_melodic"] += 1
                elif is_on and not was_on:
                    key_on_edge(cid, hw, w.sample)
                elif was_on and not is_on:
                    flush_one(cid, hw)
                    release_note(cid, hw, w.sample)
            else:
                if is_on and not was_on:
                    key_on_edge(cid, hw, w.sample)
                elif was_on and not is_on:
                    flush_one(cid, hw)
                    release_note(cid, hw, w.sample)
            touch_freq(cid, hw, w.sample)
            continue

        if 0x30 <= r <= 0x38:
            st.write(r, val)
            hw = r - 0x30
            if drums and hw >= 6:
                for entry in OPLL_DRUMS:
                    if entry[3] == r:
                        update_level(cid, entry[0], w.sample)
            else:
                key = (cid, hw)
                preset = st.preset(hw)
                if key in pending and preset != pending[key]["preset_last"]:
                    # The settle window is still open. Keep the change on the
                    # pending note so the tick-rate macro sees it too.
                    pending[key]["preset"].append((w.sample, preset))
                    pending[key]["preset_last"] = preset
                if key in held and preset != held_preset[key]:
                    # Furnace reloads a held OPLL voice through the wave
                    # column: effect 10xx is DIV_CMD_WAVE, which writes the
                    # $3x instrument nibble without touching the note.  A
                    # macro pass later replaces this with a tick-rate wave
                    # macro when the writes are faster than a row.
                    ev = events[held[key]]
                    bank = (
                        patches[patch_order[ev.ins]].preset == 0
                        if 0 <= ev.ins < len(patch_order) else False
                    )
                    preset_pts[key].append((w.sample, preset))
                    if preset or bank:
                        # A switch to the user patch is only faithful while
                        # the note already plays one: $00-$07 hold that
                        # instrument and nothing else rewrites them here.
                        channel_fx.append(
                            (w.sample, base[cid] + hw, 0x10, preset)
                        )
                        stats["inst_fx"] += 1
                    elif key not in released:
                        stats["midnote_inst"] += 1
                    held_preset[key] = preset
                update_level(cid, hw, w.sample)
            continue

        # $00-$07 hold the single user instrument. A change while a custom
        # voice is sounding reaches the chip immediately, but one pattern cell
        # cannot reload a held note, so the next key-on takes it.
        st.write(r, val)
        if r <= 0x07:
            for key in held:
                if key in released:
                    continue
                if key[0] == cid and not held_preset.get(key, 0):
                    stats["midnote_inst"] += 1
                    break

    for cid, fch in list(pending):
        flush_one(cid, fch)
    for cid, fch in list(held):
        close_note(cid, fch, vgm.total_samples or 0, off=False)

    if stats["midnote_inst"]:
        warnings.append(
            f"{stats['midnote_inst']} user-patch change(s) landed inside a "
            "held OPLL note; the next key-on picks them up"
        )
    if drums and stats["lost_melodic"]:
        warnings.append(
            f"rhythm mode is on, so OPLL channels 7-9 are the kit; "
            f"{stats['lost_melodic']} melodic key-on(s) there had no column"
        )
    ons_by_ch: dict[int, list[int]] = {}
    for e in events:
        if e.on:
            ons_by_ch.setdefault(e.ch, []).append(e.sample)
    drop = {
        idx for idx in off_idx
        if any(abs(t - events[idx].sample) <= 2
               for t in ons_by_ch.get(events[idx].ch, ()))
    }
    if drop:
        events[:] = [e for i, e in enumerate(events) if i not in drop]
    return stats, warnings


def _opl_rhythm_plan(
    vgm, kind: str, chip_name: str | None = None,
) -> tuple[bool, dict]:
    """Decide which OPL layout a log should become, and count what each costs.

    Rhythm mode is one bit of one register, but it moves bank A's last three FM
    channels into a kit, and the layout is stored in the Furnace system id. A
    track is converted as either nine melodic columns or six (fifteen on an
    OPL3) plus the percussion. Drivers differ, and picking wrongly loses notes:

    - many arcade and sound-card drivers poke ``$BD`` with bit 5 set during chip
      init and clear it in the same sample, never latching a drum. The melodic
      layout keeps every one of that track's notes. The drums layout would take
      its channels 7 to 9 away.
    - a track that really latches the kit gets the drums layout, and a melodic
      voice still played on the rhythm channels is routed onto the kit columns
      that own their operators rather than dropped (see ``_collect_opl``).

    Returns ``(drums, stats)``: kit latches actually triggered while the mode was
    in force, how long rhythm mode stayed on, and the melodic key-ons that sit on
    registers a kit takes over. ``$BD`` is bank 0 on every OPL; the OPL3's
    ``$1BD`` is the second bank's own register, not a rhythm control.
    """
    # The OPL4's FM half is OPL3-shaped.
    st = OPLState(KIND_OPL3 if kind == "opl4" else kind, False)
    kit_regs = {fch for fch, cm in enumerate(st.chan_map)
                if ((cm >> 8) & 1) == 0 and (cm & 0xFF) >= 6}
    chan_of: dict[tuple[int, int], list[int]] = {}
    for fch, cm in enumerate(st.chan_map):
        chan_of.setdefault((((cm >> 8) & 1) if st.opl3 else 0, cm & 0xFF),
                           []).append(fch)

    stats = {"latches": 0, "mode_samples": 0, "melodic_on_kit": 0}
    per: dict[int, list] = {}
    source = chip_name or kind
    for w in vgm.writes:
        if w.chip == source:
            per.setdefault(w.chip_id, []).append(w)
    end = int(vgm.total_samples or 0)
    for ws in per.values():
        mode = False
        since = 0
        trig = 0
        keyed: dict[tuple[int, int], bool] = {}
        for w in ws + [None]:                       # sentinel closes the last mode
            if w is None:
                if mode and end > since:
                    stats["mode_samples"] += end - since
                break
            port = ((w.reg >> 8) & 1) if st.opl3 else 0
            r = w.reg & 0xFF
            if r == 0xBD and port == 0:
                now = bool(w.val & 0x20)
                if now != mode:
                    stats["segments"] = stats.get("segments", 0) + 1
                    if mode:
                        stats["mode_samples"] += w.sample - since
                    mode, since = now, w.sample
                bits = (w.val & 0x1F) if now else 0
                for k in range(5):
                    bit = 1 << k
                    if (bits & bit) and not (trig & bit):
                        stats["latches"] += 1
                trig = bits
                continue
            if 0xB0 <= r <= 0xB8:
                on = bool(w.val & 0x20)
                was = keyed.get((port, r), False)
                keyed[(port, r)] = on
                if on and not was and not mode:
                    stats["melodic_on_kit"] += sum(
                        1 for fch in chan_of.get((port, r - 0xB0), ())
                        if fch in kit_regs)
    # A latch that actually fires is the whole test: with none, rhythm mode was a
    # poke between two writes of the same sample and no kit voice ever sounded.
    return bool(stats["latches"]), stats


def _fm_vol_max(systems, *, is_opll: bool = False,
                is_opl: bool = False) -> int:
    """The volume column's ceiling for a module's main FM family.

    The ceiling is a chip property: 127 on the OPM and OPN family, 63 on an
    OPL, 15 on the YM2413. A combined module takes it from the family whose
    channels come first, and the writer's `_channel_vol_max` gives every column
    its own chip's value on top of that.
    """
    kind = systems[0].kind if systems else (
        "opll" if is_opll else "opl" if is_opl else "ym2151"
    )
    if kind == "opll":
        return 15
    if kind in ("opl", "opl2", "opl3"):
        return 63
    return 127


def _family_keyons(vgm: VgmFile, chips: set[str]) -> int:
    """Rising key-on edges of the named chip families.

    Used to choose the family a mixed log converts. A repeated write while a
    voice is already keyed is not a new note, and a rhythm latch counts each
    newly set kit bit.
    """
    count = 0
    fm_mask: dict[tuple[str, int, int], int] = {}
    opl_on: dict[tuple[int, int, int], bool] = {}
    rhythm: dict[int, int] = {}
    # The YM2413's rhythm mode: which chips have it on right now. Its kit is
    # latched through $0e, so a melodic key on those channels means nothing.
    opll_mode: dict[int, bool] = {}
    for w in vgm.writes:
        if w.chip not in chips:
            continue
        if w.chip == "ym2151":
            # Mirror the collector: the voice is in bits 2-0, the operator mask
            # in bits 6-3, and a key-off write clears the mask. Counting only
            # the non-zero writes would call a whole channel one note.
            if w.reg == 0x08:
                ch = w.val & 7
                mask = (w.val & 0x78) >> 3
                key = ("ym2151", w.chip_id, ch)
                if mask and not fm_mask.get(key):
                    count += 1
                fm_mask[key] = mask
        elif w.chip == "ym2203":
            if (w.reg & 0xFF) == 0x28:
                ch = w.val & 3
                mask = (w.val >> 4) & 0x0F
                key = ("ym2203", w.chip_id, ch)
                if mask and not fm_mask.get(key):
                    count += 1
                fm_mask[key] = mask
        elif w.chip == "ym2612":
            if not (w.reg & 0x100) and (w.reg & 0xFF) == 0x28:
                raw = w.val & 7
                if raw in (3, 7):
                    continue
                ch = raw if raw < 3 else raw - 1
                mask = (w.val >> 4) & 0x0F
                key = ("ym2612", w.chip_id, ch)
                if mask and not fm_mask.get(key):
                    count += 1
                fm_mask[key] = mask
        elif w.chip in ("ym2608", "ym2610"):
            # Both decode $28 like the OPN2; the four-channel YM2610 has no
            # second block, and slot numbers 3/7 are its rhythm latch.
            if not (w.reg & 0x100) and (w.reg & 0xFF) == 0x28:
                raw = w.val & 7
                if raw in (3, 7):
                    continue
                ch = raw if raw < 3 else raw - 1
                if w.chip == "ym2610" and ch > 2:
                    continue
                mask = (w.val >> 4) & 0x0F
                key = (w.chip, w.chip_id, ch)
                if mask and not fm_mask.get(key):
                    count += 1
                fm_mask[key] = mask
        elif w.chip == "opll":
            # Bit 4 of $2x keys a melodic voice; in rhythm mode the last three
            # belong to the chip's own kit and $0e's five low bits latch it.
            # ($3x is the instrument and volume nibble, never a key.)
            r = w.reg & 0xFF
            mode = opll_mode.get(w.chip_id, False)
            if 0x20 <= r <= 0x28:
                key = ("opll", w.chip_id, r)
                on = bool(w.val & 0x10) and not (mode and r >= 0x26)
                if on and not opl_on.get(key, False):
                    count += 1
                opl_on[key] = on
            elif r == 0x0E:
                opll_mode[w.chip_id] = bool(w.val & 0x20)
                sel = (w.val & 0x1F) if opll_mode[w.chip_id] else 0
                prev = rhythm.get(("opll", w.chip_id), 0)
                count += (sel & ~prev).bit_count()
                rhythm[("opll", w.chip_id)] = sel
        elif w.chip in ("opl", "opl2", "opl3", KIND_Y8950, "ymf278b"):
            r = w.reg & 0xFF
            port = (w.reg >> 8) & 1
            if 0xB0 <= r <= 0xB8:
                key = (w.chip_id, port, r)
                on = bool(w.val & 0x20)
                if on and not opl_on.get(key, False):
                    count += 1
                opl_on[key] = on
            elif r == 0xBD and port == 0:
                trig = (w.val & 0x1F) if w.val & 0x20 else 0
                prev = rhythm.get(w.chip_id, 0)
                count += (trig & ~prev).bit_count()
                rhythm[w.chip_id] = trig
    return count


def _family_sounds(vgm: VgmFile, names: set[str]) -> bool:
    """Whether a chip family has anything a module can play.

    A rising key-on counts. So does an SSG voice someone tuned, a rhythm latch
    that names a drum, and an ADPCM-A trigger. Register *setup* does not: the
    merged arcade-plus-homebrew logs often carry a second FM chip that only
    received the driver's init block, and that chip deserves no system.
    """
    if _family_keyons(vgm, names):
        return True
    for w in vgm.writes:
        if w.chip not in names:
            continue
        reg = w.reg & 0xFF
        if w.chip.endswith("_ssg"):
            # $0-$5 are the coarse period bytes: a tuned voice can sound.
            if reg <= 5 and w.val:
                return True
        elif w.chip == "ym2608" and not (w.reg & 0x100):
            # Port 0's $10 latches the six kit triggers. A latch with none set
            # is init or a release, and plays nothing.
            if reg == 0x10 and (w.val & 0x3F):
                return True
        elif w.chip == "ym2610" and (w.reg & 0x100):
            # Port 1 of the YM2610 is ADPCM-A; $10 carries its two trigger bits.
            if reg == 0x10 and (w.val & 0xF0):
                return True
        elif w.chip == KIND_Y8950:
            # $07 bit 7 starts the delta-T voice. An FM-only Y8950 already
            # counted through _family_keyons above.
            if reg == 0x07 and (w.val & 0x80) and not (w.val & 0x01):
                return True
    return False


def analyze(
    vgm: VgmFile, *, speed: int | None = None, pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    condense: float = 1.0,
    grid_cache: dict | None = None,
    c352_quad: bool = False,
    dac_slices: bool = False,
    lfo_loop: bool = True,
    lossy_samples: bool = False,
) -> Song:
    """Build the Song for one rendition of `vgm`.

    `grid_cache` memoises the grid estimate, which does not depend on
    `condense`. The caller owns it. `c352_quad` keeps the C352 rear pair on
    its own outputs. `lfo_loop` enables the LFO/tail loop search for long
    pitch macros; turning it off keeps the exact first-cycle detection but
    skips the expensive tail scan.
    """
    tick = TARGET_TICK_SAMPLES if tick_samples is None else float(tick_samples)
    row_floor = MIN_ROW_SAMPLES if min_row is None else float(min_row)
    ym = YM2151State()
    patches: dict[tuple, FMPatch] = {}
    patch_order: list[tuple] = []
    events: list[NoteEvent] = []
    keyon_times: list[int] = []
    warnings: list[str] = list(vgm.warnings)
    # One module carries one FM family per system. A mixed log converts the
    # families that actually sound; every dropped one is named after the
    # decision below, where the answer is known.
    chips = {w.chip for w in vgm.writes}
    has_ym2203 = "ym2203" in chips or "ym2203_ssg" in chips
    has_ym2151 = "ym2151" in chips
    has_ym2612 = "ym2612" in chips
    has_ym2610 = "ym2610" in chips or "ym2610_ssg" in chips
    has_ym2608 = "ym2608" in chips or "ym2608_ssg" in chips
    has_sn76489 = "sn76489" in chips
    has_opl = bool(chips & {"opl", "opl2", "opl3", KIND_Y8950})
    has_y8950 = KIND_Y8950 in chips
    has_opl4 = "ymf278b" in chips
    has_opll = "opll" in chips
    has_nes = "nes" in chips

    # The PCM a YM2612 streams is music even when the chip keys no FM voice,
    # so its activity counts the DAC as well as the key-ons.
    dac_side = bool(
        vgm.dac_stream_plays or vgm.dac_block_plays or vgm.dac_events
    )
    sounds = {
        "ym2151": _family_sounds(vgm, {"ym2151"}),
        "ym2203": _family_sounds(vgm, {"ym2203", "ym2203_ssg"}),
        "ym2612": _family_sounds(vgm, {"ym2612"}) or dac_side,
        "ym2610": _family_sounds(vgm, {"ym2610", "ym2610_ssg"}),
        "ym2608": _family_sounds(vgm, {"ym2608", "ym2608_ssg"}),
        "opl": _family_sounds(vgm, {"opl"}),
        "opl2": _family_sounds(vgm, {"opl2"}),
        "opl3": _family_sounds(vgm, {"opl3"}),
        "y8950": _family_sounds(vgm, {KIND_Y8950}),
        "opl4": _family_sounds(vgm, {"ymf278b"}),
        "opll": _family_sounds(vgm, {"opll"}),
    }

    # Which family a mixed log converts. A chip with nothing to play is
    # dropped first: these merged logs often open two FM chips and score the
    # music on one of them.
    arcade_present = {
        name for name, present in (
            ("ym2608", has_ym2608), ("ym2610", has_ym2610),
            ("ym2612", has_ym2612), ("ym2203", has_ym2203),
            ("ym2151", has_ym2151),
        ) if present
    }
    arcade_live = {name for name in arcade_present if sounds[name]}
    opl_side = chips & {KIND_OPL, KIND_OPL2, KIND_OPL3, KIND_Y8950}
    if has_opl4:
        opl_side.add("opl4")
    opl_live = {name for name in opl_side if sounds[name]}
    opll_live = has_opll and sounds["opll"]

    # A mixed OPL + arcade log converts one family. Pick the side with more
    # key-ons, which is the one that carries the track, and name the loss.
    prefer_opl = False
    opl_keys = arcade_keys = 0
    if has_opl and arcade_present:
        opl_keys = _family_keyons(vgm, opl_side)
        arcade_keys = _family_keyons(vgm, arcade_present)
        prefer_opl = opl_keys > arcade_keys

    # A YM2612 that only drives the DAC is not a competing FM family: the OPL
    # plays every note while the OPN2 streams PCM, and Furnace hosts both as
    # separate systems (the Zone 66 shape).  Keep both sides only in that
    # case; a YM2612 with notes still needs the one-family routing because
    # the pitch pass binds one chip clock at a time.
    is_combo = bool(
        (opl_live or opll_live) and arcade_present == {"ym2612"}
        and not _family_keyons(vgm, {"ym2612"}) and dac_side
    )
    if is_combo:
        prefer_opl = False

    # The OPNA owns its rhythm and delta-T engines, so it takes precedence over
    # the other arcade families. A YM2203 loses to a YM2151 because the OPM
    # collector owns the module and its SSG voices come from that chip.
    arcade_pick = next(
        (name for name in ("ym2608", "ym2610", "ym2612")
         if name in arcade_live),
        None,
    )
    if arcade_pick is None and "ym2203" in arcade_live:
        # A live YM2151 in the same log owns the module.
        if "ym2151" not in arcade_live:
            arcade_pick = "ym2203"
    if arcade_pick is None and "ym2151" in arcade_live:
        arcade_pick = "ym2151"
    # A leftover init block can make a chip look live without a single note
    # (a YM2608 SSG period left at 1, or its rhythm latch at time zero). If
    # that chip wins only by family precedence while another live family
    # actually keys notes, the one with notes is the music.
    if arcade_pick is not None:
        family_chips = {
            "ym2608": {"ym2608", "ym2608_ssg"},
            "ym2610": {"ym2610", "ym2610_ssg"},
            "ym2612": {"ym2612"},
            "ym2203": {"ym2203", "ym2203_ssg"},
            "ym2151": {"ym2151"},
        }
        keyons = {
            name: _family_keyons(vgm, family_chips[name])
            for name in arcade_live
        }
        if not keyons.get(arcade_pick):
            sounding = {name: n for name, n in keyons.items() if n}
            if sounding:
                arcade_pick = max(
                    sounding, key=lambda name: (sounding[name], name == "ym2203"),
                )
    if arcade_pick is None and not (opl_live or opll_live):
        # Nothing anywhere plays a note. Keep the chip the log declares so an
        # init-only or silent rip still gets its system and its reset effects.
        arcade_pick = next(
            (name for name in ("ym2608", "ym2610", "ym2612", "ym2203", "ym2151")
             if name in arcade_present),
            None,
        )
    # The YM2151 has no sample engine, Ext split or CSM timer to argue with an
    # OPL family: Furnace hosts both, so a log that scores the OPM beside an
    # OPL gets two systems instead of losing one. The YM2203 and YM2612 own no
    # sample engine either (the YM2203's SSG is just three more columns), and
    # the YM2413's kit and user bank own no arcade channel block.
    share_fm = bool(
        arcade_pick in ("ym2151", "ym2612", "ym2203") and (opl_live or opll_live)
    )
    keep_arcade = arcade_pick is not None and (
        not prefer_opl or is_combo or share_fm
    )

    def _picked(name: str) -> bool:
        return keep_arcade and arcade_pick == name

    is_opna = _picked("ym2608")
    is_opnb = _picked("ym2610")
    is_opn2 = _picked("ym2612")
    is_opn = _picked("ym2203")
    # The OPL family is one system per chip: the YM3526 and YM3812 own a flat
    # nine-channel space, the YMF262 two banks of nine. Rhythm mode is a second
    # system in Furnace (it reassigns the last FM channels to the kit), and the
    # log decides which one a module gets. When several OPL kinds write, the
    # one with more key-ons carries the track.
    opl_kinds = [
        k for k in (KIND_OPL, KIND_OPL2, KIND_OPL3, KIND_Y8950) if k in chips
    ]
    if has_opl4:
        opl_kinds.append("opl4")
    opl_kind = None
    if prefer_opl or is_combo or share_fm or arcade_pick is None:
        live_kinds = [k for k in opl_kinds if k in opl_live] or opl_kinds
        if len(live_kinds) > 1:
            opl_kind = max(
                live_kinds, key=lambda k: _family_keyons(vgm, {k}),
            )
        elif live_kinds:
            opl_kind = live_kinds[0]
    is_opl = opl_kind is not None
    # The YM2413 is its own system. It coexists with the SMS PSG and an AY-8910
    # (Furnace ships those pairings), but not with another FM family.
    is_opll = bool(
        opll_live and not is_opl and (arcade_pick is None or share_fm)
    )

    # Every family that writes but gets no system is named here, after the
    # decision. A chip that only received its init block lost nothing.
    def _names(group) -> str:
        return "/".join(sorted(n.upper() for n in group))

    all_fm = arcade_present | opl_side | ({"opll"} if has_opll else set())
    converted = set()
    if is_opl:
        converted.add(opl_kind)
    if is_opll:
        converted.add("opll")
    if keep_arcade and arcade_pick:
        converted.add(arcade_pick)
    silent = {n for n in all_fm if not sounds[n]}
    lost = {n for n in all_fm if sounds[n] and n not in converted}
    if silent:
        warnings.append(
            f"{_names(silent)} write no notes; the chip only received its "
            "init block, so it gets no system"
        )
    if is_opl and len(opl_kinds) > 1:
        dropped = {k for k in opl_kinds if k != opl_kind}
        warnings.append(
            f"more than one OPL family writes in this log; only {opl_kind} "
            f"was converted, the {_names(dropped)} side was dropped"
        )
    if "ym2151" in lost and arcade_pick:
        warnings.append(
            f"YM2151 and {(arcade_pick).upper()} write in one log; only the "
            f"{_names({arcade_pick})} was converted"
        )
    if has_opll and "opll" in lost:
        warnings.append(
            "OPLL and another FM chip write in one log; only the "
            f"{_names(converted) or 'OPL'} side was converted"
        )
    for name in sorted(lost):
        # Anything still unmentioned is named here, so no family disappears
        # from the module without a word about it.
        if not any(name.upper() in w for w in warnings):
            warnings.append(
                f"{name.upper()} writes in this log and gets no system; the "
                f"module keeps {_names(converted)}"
            )
    if is_combo:
        other = _names(arcade_present)
        arcade_writes = sum(1 for w in vgm.writes if w.chip in arcade_present)
        warnings.append(
            f"{other} drives PCM only ({arcade_writes} writes, no FM key-ons) "
            "beside the OPL music; both systems were converted"
        )
    # The grid estimate reads one chip family's writes. With more than one
    # system it takes them all, so a PCM-only OPN2 whose notes land on the
    # stream still measures the OPL's tempo.
    selected_fm_chips = None
    if converted:
        selected_fm_chips = set()
        for name in sorted(converted):
            selected_fm_chips.add(name)
            if f"{name}_ssg" in chips:
                selected_fm_chips.add(f"{name}_ssg")
    if has_opl and arcade_present and lost:
        other = _names(arcade_present)
        opl_writes = sum(1 for w in vgm.writes if w.chip in opl_side)
        arcade_writes = sum(1 for w in vgm.writes if w.chip in arcade_present)
        if prefer_opl:
            warnings.append(
                f"OPL has more key-ons ({opl_keys}) than {other} "
                f"({arcade_keys}); writes OPL {opl_writes}, {other} "
                f"{arcade_writes}; only the OPL side was converted"
            )
        else:
            warnings.append(
                f"{other} has more key-ons ({arcade_keys}) than OPL "
                f"({opl_keys}); writes {other} {arcade_writes}, OPL "
                f"{opl_writes}; only the {other} side was converted"
            )
    if share_fm and not is_combo:
        # Both sides play, so both are kept. Say which is which.
        arcade_writes = sum(1 for w in vgm.writes if w.chip == arcade_pick)
        home = opl_side | ({"opll"} if is_opll else set())
        warnings.append(
            f"{_names({arcade_pick})} ({_family_keyons(vgm, {arcade_pick})} "
            f"key-ons, {arcade_writes} writes) plays beside {_names(home)} "
            f"({_family_keyons(vgm, home)} key-ons); each keeps its own system"
        )
    # The OPL4's registers live under the "ymf278b" chip name, not "opl4".
    opl_source = "ymf278b" if opl_kind == "opl4" else opl_kind
    opl_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip == opl_source})
        if is_opl else []
    )
    # Bit 5 of the rhythm register is the mode switch, but a driver may poke it
    # during chip init and clear it in the same sample without ever sounding a
    # drum. The layout is stored in the Furnace system id. A track gets the
    # drums layout only when the log shows a kit voice actually latching.
    opl_drums, opl_rhythm = (
        _opl_rhythm_plan(
            vgm, opl_kind,
            chip_name="ymf278b" if opl_kind == "opl4" else None,
        ) if is_opl else (False, {}))
    opl_is_opl3 = opl_kind in (KIND_OPL3, "opl4")
    opl_clock = (
        opl_module_clock(
            (vgm.opl3_clock if opl_kind == KIND_OPL3 else
             vgm.opl2_clock if opl_kind == KIND_OPL2 else
             vgm.y8950_clock if opl_kind == KIND_Y8950 else
             vgm.ymf278b_clock if opl_kind == "opl4" else vgm.opl_clock),
            opl_is_opl3,
        ) if is_opl else 0.0
    )
    opll_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip == "opll"})
        if is_opll else []
    )
    # Bit 31 of the YM2413 clock field asks for the Konami VRC7: same OPLL
    # core, six channels and the VRC7 preset ROM. It has no rhythm mode.
    opll_vrc7 = bool(is_opll and getattr(vgm, "ym2413_vrc7", False))
    # Like the OPL kit, the drums layout is a separate Furnace system. It is
    # chosen only when a $0e trigger actually rises while rhythm mode is on.
    opll_drums, opll_rhythm = (
        opll_rhythm_plan(vgm, opll_ids) if is_opll and not opll_vrc7
        else (False, {})
    )
    opll_clock = (
        opll_module_clock(vgm.ym2413_clock or 3579545) if is_opll else 0.0
    )
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
    ym2610_ids = (
        sorted({
            w.chip_id for w in vgm.writes
            if w.chip in ("ym2610", "ym2610_ssg")
        })
        if is_opnb else []
    )
    ym2610_clock = vgm.ym2610_clock or YM2610_DEFAULT_CLOCK
    ym2610_b = bool(vgm.ym2610_b)
    ym2610_fm_count = 6 if ym2610_b else 4
    ym2610_modes = {
        (w.val >> 6) & 0x03
        for w in vgm.writes
        if is_opnb and w.chip == "ym2610" and (w.reg & 0xFF) == 0x27
    }
    if is_opnb and ym2610_modes & {1, 2}:
        warnings.append(
            "YM2610 channel-2 special/CSM mode is converted with the base "
            "14/16-channel layout; per-operator FM3 frequencies are not split"
        )
    # The OPNA's channel order is the YM2610B's with rhythm in place of
    # ADPCM-A: six FM (nine when channel 3 is split), three SSG, six rhythm
    # and one ADPCM-B. Bus A $27 bits 6-7 are the same mode field, and only
    # special mode (01) is in these logs - the OPNA has no CSM.
    ym2608_ids = (
        sorted({
            w.chip_id for w in vgm.writes
            if w.chip in ("ym2608", "ym2608_ssg")
        })
        if is_opna else []
    )
    ym2608_clock = vgm.ym2608_clock or YM2608_DEFAULT_CLOCK
    ym2608_modes = {
        (w.val >> 6) & 0x03
        for w in vgm.writes
        if is_opna and w.chip == "ym2608" and not (w.reg & 0x100)
        and (w.reg & 0xFF) == 0x27
    }
    ym2608_ext = bool(ym2608_modes & {1, 2})
    ym2608_fm_count = 9 if ym2608_ext else 6
    multi_ext = ym2203_ext or ym2612_ext or ym2608_ext
    ym_chunk = 9 if multi_ext else 6
    if is_opnb:
        ym_chunk = ym2610_fm_count + 3 + 6 + 1
        ym_base = {cid: ym_chunk * i for i, cid in enumerate(ym2610_ids)}
        ym_count = len(ym2610_ids)
    elif is_opna:
        ym_chunk = ym2608_fm_count + 3 + 6 + 1
        ym_base = {cid: ym_chunk * i for i, cid in enumerate(ym2608_ids)}
        ym_count = len(ym2608_ids)
    else:
        ym_base = {
            cid: ym_chunk * i
            for i, cid in enumerate(ym_ids if is_opn else ym2612_ids)
        }
        ym_count = len(ym_ids if is_opn else ym2612_ids)
    ym2203_ssg_count = 0

    # Channels per OPL instance, in module order: nine for an OPL/OPL2,
    # eighteen for an OPL3, plus the five kit columns when rhythm is on. The
    # Y8950 adds one ADPCM column after its FM block.
    opl_state_kind = opl_kind
    opl_fm_count = channel_count(opl_state_kind, opl_drums) if is_opl else 0
    opl_chunk = (
        opl_fm_count + (1 if opl_kind == KIND_Y8950 else 0)
        + (OPL4_WAVE_SLOTS if opl_kind == "opl4" else 0)
    ) if is_opl else 0
    # A combined module lists its systems in channel order: the arcade family
    # (or the OPM's eight voices) first, then every OPL one. The bases below
    # are offsets into the module's channel space, not per-chip ones.
    convert_opm = "ym2151" in converted
    opm_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip == "ym2151"})
        if convert_opm else []
    )
    opm_base = {cid: 8 * i for i, cid in enumerate(opm_ids)}
    ym_end = (
        ym_chunk * ym_count if (is_opn or is_opn2 or is_opnb or is_opna)
        else (8 * len(opm_ids) if convert_opm else 0)
    )
    opl_base = {
        cid: ym_end + opl_chunk * i for i, cid in enumerate(opl_ids)
    }
    opl_end = ym_end + opl_chunk * len(opl_ids)
    # The YM2413 owns nine melodic columns, or eleven when its kit is in use.
    # The VRC7 is the same core with six.
    opll_chunk = (
        6 if opll_vrc7 else
        OPLL_DRUMS_CHANNELS if opll_drums else OPLL_CHANNELS
    ) if is_opll else 0
    opll_start = opl_end if is_opl else ym_end
    opll_base = {cid: opll_start + opll_chunk * i for i, cid in enumerate(opll_ids)}
    opll_end = opll_start + opll_chunk * len(opll_ids)

    # The channel every pitched system ends at. Sample chips are numbered after
    # it; a log with no pitched chip at all still owns the OPM's eight columns.
    fm_end = 0
    if is_opnb or is_opna or is_opn or is_opn2:
        fm_end = ym_end
    elif convert_opm:
        fm_end = 8 * len(opm_ids)
    if is_opl:
        fm_end = max(fm_end, opl_end)
    if is_opll:
        fm_end = max(fm_end, opll_end)
    if not (is_opl or is_opll or convert_opm or is_opn or is_opn2
            or is_opnb or is_opna):
        fm_end = 8

    # Systems in module order. Each family lists its chips in channel order.
    fm_systems: list[FmSystem] = []
    if is_opnb:
        for cid in ym2610_ids:
            fm_systems.append(FmSystem(
                "ym2610", cid, ym2610_fm_count, clock=ym2610_clock,
            ))
    elif is_opna:
        for cid in ym2608_ids:
            fm_systems.append(FmSystem(
                "ym2608", cid, ym2608_fm_count, ext=ym2608_ext,
                clock=ym2608_clock,
            ))
    elif is_opn2:
        for cid in ym2612_ids:
            fm_systems.append(FmSystem(
                "ym2612", cid, 9 if ym2612_ext else 6,
                ext=ym2612_ext, csm=ym2612_csm, clock=ym2612_clock,
            ))
    elif is_opn:
        for cid in ym_ids:
            fm_systems.append(FmSystem(
                "ym2203", cid, 6 if ym2203_ext else 3,
                ext=ym2203_ext, clock=ym_clock,
            ))
    elif "ym2151" in converted or not (is_opl or is_opll or arcade_present):
        for cid in (opm_ids or [0]):
            fm_systems.append(FmSystem(
                "ym2151", cid, 8, clock=vgm.ym2151_clock,
            ))
    if is_opl:
        for cid in opl_ids:
            fm_systems.append(FmSystem(
                opl_kind, cid, opl_fm_count, drums=opl_drums,
                clock=int(opl_clock),
            ))
    elif is_opll:
        for cid in opll_ids:
            fm_systems.append(FmSystem(
                "opll", cid,
                (6 if opll_vrc7 else
                 OPLL_DRUMS_CHANNELS if opll_drums else OPLL_CHANNELS),
                drums=opll_drums, clock=int(opll_clock), vrc7=opll_vrc7,
            ))

    # A Genesis log carries the SN76489 beside the YM2612.  The module uses a
    # second (SMS) system for it, so its four channels follow every FM instance.
    # A Genesis log carries the SN76489 beside the YM2612. A Master System or
    # Game Gear rip is the chip on its own, with no FM family at all; it must
    # still get its SMS system (and its four columns).
    psg_ids = [0] if (
        has_sn76489 and (
            is_opn2
            or not (arcade_present or has_opl or has_opll or has_opl4 or has_y8950)
        )
    ) else []
    psg_t6w28 = bool(psg_ids and getattr(vgm, "sn76489_t6w28", False))
    if psg_ids and not psg_t6w28:
        # A dual SN76489 has an independent register file per chip instance.
        present = sorted({w.chip_id for w in vgm.writes if w.chip == "sn76489"})
        if present:
            psg_ids = present
    psg_stereo = bool(psg_ids and getattr(vgm, "sn76489_stereo", False))
    psg_base = fm_end if psg_ids else None
    psg_clock = vgm.sn76489_clock or PSG_DEFAULT_CLOCK
    psg_at = {
        cid: psg_base + 4 * i for i, cid in enumerate(psg_ids)
    } if psg_base is not None else {}
    if psg_ids:
        fm_end += 4 * len(psg_ids)
    pending_off = [None] * 8  # type: list[int | None]
    chip_fx: list[tuple[int, int, int] | tuple[int, int, int, int]] = []
    channel_fx: list[tuple[int, int, int, int]] = []
    last_lfrq = last_wave = -1
    # The OPM resets 0x19 to 0 and Furnace defaults PMD/AMD to 0x7F. Pin both
    # to zero before the first row.
    last_amd = last_pmd = 0
    held_ins = [-1] * 8
    held_kon = [-1] * 8
    pms_ev: list[int | None] = [None] * 8
    held_ramp: list[list[tuple[int, int, int]] | None] = [None] * 8
    held_lfo: dict[int, list] = {}
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
    # Mid-note DT1/MULT writes per held note (fast operator pitch tracking).
    dtmul_pts: list[list[tuple[int, int, int]]] = [[] for _ in range(8)]
    dtmul_last = [[0] * 4 for _ in range(8)]
    dtmul_ev: list[int | None] = [None] * 8
    # Mid-note DT2 changes per held note.
    dt2_pts: list[list[tuple[int, int, int]]] = [[] for _ in range(8)]
    dt2_last = [[0] * 4 for _ in range(8)]
    dt2_ev: list[int | None] = [None] * 8
    # Last KON operator mask per channel: key state is per operator, and an
    # envelope restarts only on a 0->1 transition of its bit.
    op_mask = [0] * 8

    def close_sweep(ch: int) -> None:
        idx = sweep_ev[ch]
        if idx is not None:
            _finalize_sweep(events[idx], sweep_pts[ch], sweep_base[ch][0], sweep_base[ch][1])
            sweep_ev[ch] = None

    def close_pms(ch: int) -> None:
        idx = pms_ev[ch]
        if idx is not None and held_ramp[ch]:
            events[idx].opm_pms_pts = tuple(held_ramp[ch])
            ins = held_ins[ch]
            writes = list(held_ramp[ch])[1:]
            if ins >= 0 and writes:
                _flush_pms_ramp(held_lfo, ins, writes, held_kon[ch])
        pms_ev[ch] = None
        held_ramp[ch] = None

    def close_tl(ch: int) -> None:
        idx = tl_ev[ch]
        if idx is not None and tl_pts[ch]:
            events[idx].tl_pts = tuple(tl_pts[ch])
        tl_ev[ch] = None

    def close_dtmul(ch: int) -> None:
        idx = dtmul_ev[ch]
        if idx is not None and dtmul_pts[ch]:
            events[idx].op_dtmul_pts = tuple(dtmul_pts[ch])
        dtmul_ev[ch] = None

    def close_dt2(ch: int) -> None:
        idx = dt2_ev[ch]
        if idx is not None and dt2_pts[ch]:
            events[idx].op_dt2_pts = tuple(dt2_pts[ch])
        dt2_ev[ch] = None

    def intern_patch(ch: int, mask: int = 0xF) -> int:
        snap = ym.snapshot(ch)
        if mask != 0xF:
            # An un-keyed operator never starts its envelope. Maximum
            # attenuation (TL 127) is the closest a static OPM instrument gets.
            # The mask bit that keys an operator is not the operator number:
            # the $08 SS bits run S1,S3,S2,S4 (see OPM_OP_TO_MASK). Reading
            # the bits in order mutes the wrong operators and can leave a
            # voice whose every carrier is silent.
            snap.ops = tuple(
                OpRegs(
                    dt_mul=op.dt_mul,
                    tl=(0x7F if not mask & (1 << OPM_OP_TO_MASK[i])
                        else op.tl),
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

    # The OPM collector runs only for the family the module keeps. A mixed log
    # that chose the OPL side must not also write YM2151 notes into the same
    # channels. (`convert_opm` was set with the channel bases above.)
    if convert_opm:
        emit_chip_fx(0, FX_LFO_AMD, 0)
        emit_chip_fx(0, FX_LFO_PMD, 0)
        if len(opm_ids) > 1:
            warnings.append(
                f"dual YM2151 log: {len(opm_ids)} chips converted as separate systems"
            )

    for opm_cid in opm_ids:
        ym = YM2151State()
        base_ch = opm_base[opm_cid]
        last_lfrq = last_wave = -1
        last_amd = last_pmd = 0
        last_keyon_sample = [-10**9] * 8
        last_test_pulse = -10**9
        pending_off = [None] * 8  # type: list[int | None]
        held_ins = [-1] * 8
        held_kon = [-1] * 8
        pms_ev = [None] * 8  # type: list[int | None]
        held_ramp = [None] * 8  # type: list[list[tuple[int, int, int]] | None]
        sweep_pts = [[] for _ in range(8)]
        sweep_base = [(0, 0)] * 8
        sweep_ev = [None] * 8  # type: list[int | None]
        tl_pts = [[] for _ in range(8)]
        tl_ev = [None] * 8  # type: list[int | None]
        tl_last = [0x7F] * 8
        op_mask = [0] * 8

        def settle_burst_tl(idx: int, ch: int, sample: int) -> None:
            """Fold a key-on burst's post-key-on operator levels into the snapshot.

            Some drivers park every carrier at TL 7F, key on, and write the voice's
            real levels a few samples later in the same burst. A snapshot taken at
            the key-on then describes an instrument that cannot sound, and the
            later write reaches the writer only as a mid-note fade, which the
            volume column can attenuate but never raise back. The burst belongs
            to this note.
            """
            for j in range(idx + 1, len(vgm.writes)):
                w2 = vgm.writes[j]
                if w2.sample - sample > _OPM_BURST_LOAD_SAMPLES:
                    break
                if (w2.chip != "ym2151" or w2.chip_id != opm_cid
                        or not (0x60 <= w2.reg <= 0x7F)):
                    continue
                if (w2.reg & 7) != ch:
                    continue
                ym.write(w2.reg, w2.val)

        def settle_burst_dtmul(idx: int, ch: int, sample: int) -> None:
            """Fold a key-on burst's DT1/MULT writes into the instrument.

            Drivers commonly send the voice's operator bytes just after the
            key-on. Those describe the attack timbre, not a move inside the
            note. Anything after the short burst is a real mid-note change.
            """
            for j in range(idx + 1, len(vgm.writes)):
                w2 = vgm.writes[j]
                if w2.sample - sample > _OPM_BURST_LOAD_SAMPLES:
                    break
                if (w2.chip != "ym2151" or w2.chip_id != opm_cid
                        or not (0x40 <= w2.reg <= 0x5F
                                or 0xC0 <= w2.reg <= 0xDF)):
                    continue
                if (w2.reg & 7) != ch:
                    continue
                ym.write(w2.reg, w2.val)

        for wi, w in enumerate(vgm.writes):
            if not convert_opm or w.chip != "ym2151" or w.chip_id != opm_cid:
                continue
            if w.reg != 0x08:
                if w.reg == 0x18:
                    last_lfrq = w.val & 0xFF
                    if opm_cid == 0:
                        emit_chip_fx(w.sample, FX_LFO_SPEED, last_lfrq)
                elif w.reg == 0x01:
                    # TEST bit 1 gates the LFO's wave-3 term. Capcom's driver
                    # pulses it at key-on to re-seed the LFO. Emit 17 00, then
                    # restore the real LFRQ one tick later.
                    if w.val & 0x02:
                        last_test_pulse = w.sample
                        if opm_cid == 0:
                            emit_chip_fx(w.sample, FX_LFO_SPEED, 0)
                            chip_fx.append(
                                (w.sample, FX_LFO_SPEED, max(0, last_lfrq), 1)
                            )
                    elif w.sample - last_test_pulse > 2:
                        if opm_cid == 0:
                            emit_chip_fx(
                                w.sample, FX_LFO_SPEED, max(0, last_lfrq)
                            )
                elif w.reg == 0x19:
                    if w.val & 0x80:
                        pmd = w.val & 0x7F
                        if pmd != last_pmd:
                            last_pmd = pmd
                            if opm_cid == 0:
                                emit_chip_fx(w.sample, FX_LFO_PMD, pmd)
                    else:
                        amd = w.val & 0x7F
                        if amd != last_amd:
                            last_amd = amd
                            if opm_cid == 0:
                                emit_chip_fx(w.sample, FX_LFO_AMD, amd)
                elif w.reg == 0x1B:
                    wave = w.val & 3
                    if wave != last_wave:
                        last_wave = wave
                        if opm_cid == 0:
                            emit_chip_fx(w.sample, FX_LFO_WAVE, wave)
                elif 0x38 <= w.reg <= 0x3F:
                    ch = w.reg - 0x38
                    ym.write(w.reg, w.val)
                    if held_ramp[ch] is not None:
                        held_ramp[ch].append(
                            (w.sample, (w.val >> 4) & 7, w.val & 3)
                        )
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
                elif 0x40 <= w.reg <= 0x5F:
                    # DT1/MULT writes while the note sounds: the driver uses
                    # them as a very fast per-operator pitch/timbre slide.
                    ch = w.reg & 7
                    op = (w.reg >> 3) & 3
                    if ym.kon[ch] and dtmul_ev[ch] is not None:
                        cur = ym.ops[ch][op].dt_mul
                        if cur != dtmul_last[ch][op]:
                            dtmul_pts[ch].append((w.sample, op, cur))
                            dtmul_last[ch][op] = cur
                elif 0xC0 <= w.reg <= 0xDF:
                    # DT2 changes while the note sounds.
                    ch = w.reg & 7
                    op = (w.reg >> 3) & 3
                    if ym.kon[ch] and dt2_ev[ch] is not None:
                        cur = (ym.ops[ch][op].dt2_d2r >> 6) & 3
                        if cur != dt2_last[ch][op]:
                            dt2_pts[ch].append((w.sample, op, cur))
                            dt2_last[ch][op] = cur
                continue
            ch = w.val & 7
            mask = (w.val & 0x78) >> 3
            newly_on = mask & ~op_mask[ch]
            op_mask[ch] = mask
            if mask and newly_on:
                pending_off[ch] = None
                close_pms(ch)
                close_sweep(ch)
                close_tl(ch)
                close_dtmul(ch)
                close_dt2(ch)
                if ym.carrier_tl(ch) >= 0x7F:
                    # Nothing in this voice can sound yet: the driver loads the
                    # real levels at the tail of this same burst.
                    settle_burst_tl(wi, ch, w.sample)
                settle_burst_dtmul(wi, ch, w.sample)
                ins = intern_patch(ch, mask)
                note = kc_to_furnace_note(ym.kc[ch])
                events.append(NoteEvent(
                    sample=w.sample,
                    ch=base_ch + ch,
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
                pms_ev[ch] = len(events) - 1
                pa = ym.pms_ams[ch]
                held_ramp[ch] = [(w.sample, (pa >> 4) & 7, pa & 3)]
                tl_pts[ch] = []
                tl_last[ch] = ym.carrier_tl(ch)
                tl_ev[ch] = len(events) - 1
                dtmul_pts[ch] = []
                dtmul_last[ch] = [ym.ops[ch][i].dt_mul for i in range(4)]
                dtmul_ev[ch] = len(events) - 1
                dt2_pts[ch] = []
                dt2_last[ch] = [
                    (ym.ops[ch][i].dt2_d2r >> 6) & 3 for i in range(4)
                ]
                dt2_ev[ch] = len(events) - 1
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
                idx = sweep_ev[ch]
                if (
                    idx is not None
                    and w.sample - events[idx].sample < int(0.05 * VGM_RATE)
                ):
                    close_sweep(ch)
                close_pms(ch)
                close_tl(ch)
                close_dtmul(ch)
                close_dt2(ch)
                ym.kon[ch] = False
                held_ins[ch] = -1

        for ch in range(8):
            close_pms(ch)
            close_sweep(ch)
            close_tl(ch)
            close_dtmul(ch)
            close_dt2(ch)

        # A crescendo walks the carriers *down* through the TL range while the note
        # sounds. The volume column can only attenuate what the instrument stores, so
        # open each affected instrument to its loudest requested level first and let
        # the notes state their own starting volume. Must run after close_tl(), which
        # is what hands every note its mid-note steps.
    if convert_opm and patch_order:
        _bind_opm_levels(events, patches, patch_order)

    # Key-offs were not recorded above. Re-walk for offs that sit after the
    # last key-on and before the next one.
    for opm_cid in opm_ids:
        ym2 = YM2151State()
        base_ch = opm_base[opm_cid]
        last_on = [-10**9] * 8
        next_on_at: list[list[int]] = [[] for _ in range(8)]
        on_index: dict[tuple[int, int], int] = {}
        # The OPM re-walk owns this instance's eight columns. A module that
        # dropped the family must not add key-offs to channels it numbers from
        # zero.
        for i, ev in enumerate(events):
            if (convert_opm and ev.on and not ev.pcm
                    and base_ch <= ev.ch < base_ch + 8):
                lch = ev.ch - base_ch
                next_on_at[lch].append(ev.sample)
                on_index[(lch, ev.sample)] = i
        cursor = [0] * 8
        offs: list[NoteEvent] = []
        sounding = [False] * 8
        for w in vgm.writes:
            if not convert_opm or w.chip != "ym2151" or w.chip_id != opm_cid:
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
                sample=w.sample, ch=base_ch + ch, on=False, note=180,
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
                    # 64 samples (~1.5 ms) joins a partial f-num write:
                    # drivers write the block/high byte and the low byte at
                    # different samples, and the half-updated value would
                    # otherwise become a short, wrong pitch step.
                    if merged and p[0] - merged[-1][0] <= 64:
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
        # Per-column level bookkeeping for the Ext layout. The instrument a
        # column plays is shared and TL-blind, so each note's level has to ride
        # its own volume: `ext_tl_notes` remembers (event, instrument, operator,
        # TL at key-on) and `ext_tl_floors` the lowest TL seen per operator,
        # which `_bind_ext_levels` later writes into the instrument. A level is
        # reachable only upwards in attenuation from that floor, because the
        # platform answers `TL = 127 - vol + op.tl`.
        ext_tl_notes: list[tuple[int, int, int, int]] = []
        ext_tl_floors: dict[int, list[int]] = {}
        # Which (instrument, operator) a held column belongs to, so a mid-note
        # $4X write can widen that operator's floor.
        ext_key_op: dict[tuple[int, int, int], tuple[int, int]] = {}

        def ext_floor(ins: int, oi: int, tl: int) -> None:
            per_op = ext_tl_floors.setdefault(ins, [0x7F] * 4)
            if tl < per_op[oi]:
                per_op[oi] = tl

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
                            # A key-on is a 0->1 transition. A bit that stays
                            # set is a still-held operator: the driver often
                            # rewrites $28 while releasing other operators and
                            # loads the next note's frequency into this one, so
                            # a frequency change alone is not a retrigger.
                            fresh = not (prev & bit) or active is None
                            if not fresh:
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
                            # This column plays ops[orderedOps[c]]. Keep its key-on
                            # level: `_bind_ext_levels` rebases the note's volume
                            # onto the instrument's floor for that operator, and a
                            # mid-note $4X write widens the same floor.
                            tl_op = OPN_ORDERED_OPS[c]
                            tl0 = st.ops[ch][tl_op].tl & 0x7F
                            ext_floor(ins, tl_op, tl0)
                            ext_tl_notes.append(
                                (len(events) - 1, ins, tl_op, tl0))
                            ext_key_op[key] = (ins, tl_op)
                            opn_tl_last[key] = tl0
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
                    # A level write on an operator column IS that column's
                    # volume: the driver scripts it continuously under a held
                    # note (Exit Logo walks $4A and $4E through 6,7,8...19).
                    # Record it against the operator this column plays and widen
                    # that instrument's floor so a louder step stays reachable.
                    tl_op = opn_tl_op(reg)
                    c = OPN_ORDERED_OPS.index(tl_op)
                    key = (cid, ch, c)
                    if key not in held_idx:
                        continue
                    tl_now = st.ops[ch][tl_op].tl & 0x7F
                    ins, _oi = ext_key_op.get(key, (None, tl_op))
                    if ins is not None:
                        ext_floor(ins, tl_op, tl_now)
                    if tl_now != opn_tl_last.get(key, 0x7F):
                        opn_tl_pts[key].append((w.sample, tl_now))
                        opn_tl_last[key] = tl_now
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

        # Now that every note and its levels are in: park each Ext column's
        # loudest level in the instrument and put the rest on that channel's
        # volume. Doing it here is what lets a note that only moves later still
        # reach the quietest value the driver ever asked for.
        if ym2203_ext and ext_tl_notes:
            _bind_ext_levels(
                events, patches, patch_order, ext_tl_notes, ext_tl_floors)

        # A YM2203 note can walk its carriers louder than the level captured
        # at key-on. The volume column can only attenuate, so open each such
        # instrument to the loudest level it is ever asked for and hang every
        # note off the column, exactly as the OPM path does. Without this the
        # sustain stayed at the key-on TL and sounded much quieter than the
        # source (Eternal Dream's first FM note steps $48/$4C from 28/32 to
        # 1E/28 a few hundred samples in).
        _bind_opm_levels(events, patches, patch_order)

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

    # One collector for the chips with two three-channel FM blocks behind two
    # address ports: the YM2612 ($52/$53) and the YM2608 ($56/$57). vgm.py
    # flattens the second port into bit 8 of the register number, so an operator
    # write reads `(port << 8) | (page + 4*operator + slot)` with `slot = reg & 3`
    # naming the channel inside that block, and the channel index is
    # `(reg & 3) + 3 * port`. Keying matches on both chips: one $28 on port 0
    # whose channel field is 0-2 for the first block and 4-6 for the second,
    # bit 2 selecting the block - ymfm's `channel_offset(chnum) =
    # (chnum + (chnum >> 2)) * 4`, which is KON_6 here. The YM2610B decodes
    # identically and keeps its own route only because the pages under $30
    # differ: it puts ADPCM-A on port 1 and delta-T on port 0, where the OPNA
    # puts rhythm on port 0 and delta-T on port 1, and the four-channel YM2610
    # additionally starts its slots at one instead of zero. Notes and
    # instruments come from ym2612_note_for either way, whose frequency base is
    # the same for both chips; what differs per chip is only the clock, the name
    # in the log, and $2A/$2B (a DAC byte on the YM2612, IRQ status on the
    # OPNA).
    if is_opn2 or is_opna:
        block_port_chip = "ym2612" if is_opn2 else "ym2608"
        block_port_ids = ym2612_ids if is_opn2 else ym2608_ids
        block_port_clock = ym2612_clock if is_opn2 else ym2608_clock
        block_port_ext = ym2612_ext if is_opn2 else ym2608_ext
        block_port_csm = ym2612_csm if is_opn2 else False
        # One note formula and one instrument model for every two-port OPN chip:
        # ym2612_note_for, whose default frequency base (9437184) is what
        # DivPlatformYM2608 uses at its /6 prescale too. Only the clock per
        # chip moves a pitch.
        states = {
            cid: (YM2612State() if is_opn2 else YM2608State())
            for cid in block_port_ids
        }
        held_idx: dict[tuple[int, int, int], int] = {}
        pitch_pts: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        pitch_base: dict[tuple[int, int, int], tuple[int, int]] = {}
        opn_tl_pts: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        opn_tl_last: dict[tuple[int, int, int], int] = {}
        opn_offs: list[int] = []
        ext_active_freqs: dict[int, tuple] = {}
        ext_patch_cache: dict[int, dict[tuple, int]] = {}
        # Ext column level bookkeeping, as on the YM2203: each column's volume
        # must carry the levels the driver scripted, relative to the instrument
        # floor `_bind_ext_levels` installs at the end of this collector.
        ext_tl_notes: list[tuple[int, int, int, int]] = []
        ext_tl_floors: dict[int, list[int]] = {}
        ext_key_op: dict[tuple[int, int, int], tuple[int, int]] = {}

        def ext_floor(ins: int, oi: int, tl: int) -> None:
            per_op = ext_tl_floors.setdefault(ins, [0x7F] * 4)
            if tl < per_op[oi]:
                per_op[oi] = tl

        dac_writes = 0
        lfo_last: dict[int, int] = {}

        def opn_event_ch(cid: int, ch: int) -> int:
            # The Ext layout keeps the first block's three voices at 0-2 and
            # splits channel 3 across four operator columns, so the second
            # block continues at 6-8 (genesisext and ym2608ext both answer
            # `c.chan > 5` by handing `chan - 3` to the base system).
            return ym_base[cid] + (ch + 3 if (block_port_ext and ch >= 3) else ch)

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
                    # 64 samples (~1.5 ms) joins a partial f-num write:
                    # drivers write the block/high byte and the low byte at
                    # different samples, and the half-updated value would
                    # otherwise become a short, wrong pitch step.
                    if merged and p[0] - merged[-1][0] <= 64:
                        merged[-1] = p
                    else:
                        merged.append(p)
                if merged and merged[0][0] - ev.sample <= 120:
                    if merged[0][1:] != pitch_base[key]:
                        ev.note, ev.e5 = ym2612_note_for(
                            merged[0][1], merged[0][2], block_port_clock
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
                # A driver keys the voice while its carriers still hold the
                # previous (often muted) level and writes the real TL a few
                # samples later. That first louder write is the note's start
                # level; leaving it as a step put the note at volume 0.
                keyon_tl = 127 - max(0, ev.vol)
                if (0 <= tl[0][0] - ev.sample <= OPN_TL_FOLD
                        and tl[0][1] < keyon_tl):
                    ev.vol = max(0, min(127, 127 - tl[0][1]))
                    tl = tl[1:]
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
            if block_port_ext and ch == 2:
                return tuple(st.frequency_op(2, g) for g in range(4))
            return (st.frequency(ch),)

        # First key transitions per channel. A patch load just before one is
        # preloading the next note, not changing the held voice; the split
        # below must not fire for it.
        key_times: dict[tuple[int, int], list[int]] = {}
        for w in vgm.writes:
            if w.chip != block_port_chip or (w.reg & 0x100):
                continue
            r0 = w.reg & 0xFF
            if r0 != 0x28:
                continue
            rc = w.val & 0x07
            if rc in (3, 7):
                continue
            key_times.setdefault(
                (w.chip_id, rc if rc < 3 else rc - 1), []
            ).append(w.sample)

        patch_pending: dict[tuple[int, int, int], tuple[int, int]] = {}

        def patch_identity_write(r: int) -> bool:
            """A passive write that changes the voice's pitch or timbre."""
            return (
                0x30 <= r <= 0x3E
                or 0x90 <= r <= 0x9E
                or 0xB0 <= r <= 0xB2
            )

        def split_held_note(key, sample: int) -> None:
            """Start a new note for a patch the driver loaded mid-note."""
            idx = held_idx.get(key)
            if idx is None:
                return
            cid, ch, _op = key
            st = states[cid]
            ev = events[idx]
            # Anything the block recorded after the split instant belongs to
            # the new note, which restarts from the settled registers.
            pts = pitch_pts.get(key)
            if pts is not None:
                pitch_pts[key] = [p for p in pts if p[0] < sample]
            ev.end = sample
            close_opn_note(key, sample, off=False)
            block, fnum = st.frequency(ch)
            note, e5 = ym2612_note_for(block, fnum, block_port_clock)
            ins, vol = intern_opn_patch(cid, ch, st.op_mask[ch])
            events.append(NoteEvent(
                sample=sample,
                ch=ev.ch,
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
            pitch_pts[key] = [(sample, block, fnum)]
            opn_tl_pts[key] = []
            opn_tl_last[key] = st.carrier_tl(ch)

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
                        w.chip == block_port_chip
                        and not (w.reg & 0x100)
                        and (w.reg & 0xFF) == 0x28
                    )

                ordered.extend(w for w in group if not is_kon(w))
                ordered.extend(w for w in group if is_kon(w))
                i = j
            return ordered

        for w in kon_last(vgm.writes):
            if w.chip != block_port_chip:
                continue
            cid = w.chip_id
            st = states.get(cid)
            if st is None:
                continue
            # A patch block that has gone quiet is a real mid-note change.
            for pk in list(patch_pending):
                if pk[0] != cid:
                    continue
                first, last = patch_pending[pk]
                if w.sample - last > OPN_PATCH_SETTLE:
                    del patch_pending[pk]
                    split_held_note(pk, first)
            reg, val = w.reg, w.val
            r = reg & 0xFF
            port = bool(reg & 0x100)
            if not port and r == 0x22:
                st.write(reg, val)
                # Furnace's YM2612 effect 10xy: x = enable, y = speed.  The
                # register itself has enable in bit 3.
                fx = (0x10 if val & 0x08 else 0x00) | (val & 0x07)
                if lfo_last.get(cid) != fx:
                    chip_fx.append((w.sample, 0x10, fx))
                    lfo_last[cid] = fx
                continue
            if not port and r == 0x27:
                st.write(reg, val)
                continue
            if not port and r == 0x2A:
                st.write(reg, val)
                if is_opn2:
                    dac_writes += 1
                continue
            if not port and r == 0x2B:
                # The one place the two chips' behaviour differs below $30: on
                # the YM2612 this byte turns the DAC on and takes channel 6's
                # FM output away, while on the YM2608 $2A/$2B are IRQ counter
                # and mask registers. `dac_enabled` is a constant False on
                # YM2608State, so only the YM2612 closes a note here.
                was = st.dac_enabled
                st.write(reg, val)
                if st.dac_enabled and not was:
                    # The DAC replaces channel 6's FM output.
                    close_opn_note((cid, 5, -1), w.sample, off=True)
                continue
            if r == 0x28 and not port:
                raw_ch = val & 0x07
                if raw_ch in (3, 7):
                    continue
                # 0-2 are channels 1-3 and 4-6 are channels 4-6: the second
                # group continues the 0-based index after a reserved slot.
                ch = raw_ch if raw_ch < 3 else raw_ch - 1
                mask = (val >> 4) & 0x0F
                # A key transition takes the loaded patch itself; a pending
                # split here would put two notes on the same row.
                if not (block_port_ext and ch == 2):
                    pkey = (cid, ch, -1)
                    if pkey in patch_pending:
                        first, last = patch_pending.pop(pkey)
                        if w.sample - last > OPN_PATCH_SETTLE:
                            split_held_note(pkey, first)
                if st.dac_enabled and ch == 5:
                    # FM6 is replaced by the DAC: its key-ons are the sample
                    # player's writes, not FM notes.
                    if not mask and st.kon[ch]:
                        st.kon[ch] = False
                        close_opn_note((cid, ch, -1), w.sample, off=True)
                    st.op_mask[ch] = mask
                    continue
                if block_port_ext and ch == 2:
                    prev = st.op_mask[ch]
                    if mask:
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
                            # A key-on is a 0->1 transition. A bit that stays
                            # set is a still-held operator: the driver often
                            # rewrites $28 while releasing other operators and
                            # loads the next note's frequency into this one, so
                            # a frequency change alone is not a retrigger.
                            fresh = not (prev & bit) or active is None
                            if not fresh:
                                continue
                            close_opn_note((cid, ch, c), w.sample, off=False)
                            ins = intern_ext_patch(cid)
                            group = EXT_FREQ_OP[c]
                            block, fnum = freqs[group]
                            note, e5 = ym2612_note_for(
                                block, fnum, block_port_clock
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
                            # This column plays ops[orderedOps[c]] (ym2610ext and
                            # genesisext share the ym2203ext mapping); keep its
                            # key-on level for the floor pass below.
                            tl_op = OPN_ORDERED_OPS[c]
                            tl0 = st.ops[ch][tl_op].tl & 0x7F
                            ext_floor(ins, tl_op, tl0)
                            ext_tl_notes.append(
                                (len(events) - 1, ins, tl_op, tl0))
                            ext_key_op[key] = (ins, tl_op)
                            opn_tl_last[key] = tl0
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
                    note, e5 = ym2612_note_for(block, fnum, block_port_clock)
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
                ch = (r & 0x03) + (3 if port else 0)
            elif 0xA0 <= r <= 0xA6:
                valid = (r & 0x03) != 3
                ch = (r & 0x03) + (3 if port else 0)
            elif not port and 0xA8 <= r <= 0xAE:
                valid = (r & 0x03) != 3
                ch = 2
            elif 0xB0 <= r <= 0xB2:
                ch = (r - 0xB0) + (3 if port else 0)
            elif 0xB4 <= r <= 0xB6:
                ch = (r - 0xB4) + (3 if port else 0)
            if not valid or ch < 0 or ch >= 6:
                st.write(reg, val)
                continue

            key = (cid, ch, -1)
            # A frequency write ends a patch block: split before it so the new
            # note starts at the pitch the block left, then keep this write as
            # the new note's first pitch step.
            if (not (block_port_ext and ch == 2)
                    and key in patch_pending and 0xA0 <= r <= 0xA6):
                first, _last = patch_pending.pop(key)
                split_held_note(key, first)
            before = chan_freqs(cid, ch)
            b4 = st.pan_pms_ams[ch]
            st.write(reg, val)
            after = chan_freqs(cid, ch)
            if (not (block_port_ext and ch == 2)
                    and patch_identity_write(r)
                    and key in held_idx):
                pend = patch_pending.get(key)
                if pend is None:
                    # Do not split when the next key-on/off is about to take
                    # this voice anyway.
                    soon = any(
                        0 <= t - w.sample <= OPN_PATCH_SETTLE
                        for t in key_times.get((cid, ch), ())
                    )
                    if not soon:
                        patch_pending[key] = (w.sample, w.sample)
                else:
                    patch_pending[key] = (pend[0], w.sample)

            if block_port_ext and ch == 2:
                for c in range(4):
                    key = (cid, ch, c)
                    group = EXT_FREQ_OP[c]
                    if key in held_idx and after[group] != before[group]:
                        pitch_pts[key].append((w.sample, *after[group]))
                if (r & 0xF0) == 0x40:
                    # A level write on an operator column IS that column's
                    # volume: the driver scripts it continuously under a held
                    # note, exactly as on the YM2203 Ext. Record it against the
                    # operator this column plays, and widen that instrument's
                    # floor so a louder step is still reachable by volume.
                    tl_op = opn_tl_op(r)
                    c = OPN_ORDERED_OPS.index(tl_op)
                    key = (cid, ch, c)
                    if key in held_idx:
                        tl_now = st.ops[ch][tl_op].tl & 0x7F
                        ins_x, _oi = ext_key_op.get(key, (None, tl_op))
                        if ins_x is not None:
                            ext_floor(ins_x, tl_op, tl_now)
                        if tl_now != opn_tl_last.get(key, 0x7F):
                            opn_tl_pts[key].append((w.sample, tl_now))
                            opn_tl_last[key] = tl_now
            else:
                key = (cid, ch, -1)
                if key in held_idx and after[0] != before[0]:
                    pitch_pts[key].append((w.sample, *after[0]))
                if (0x40 <= r <= 0x4E and key in held_idx
                        and key not in patch_pending):
                    cur = st.carrier_tl(ch)
                    if cur != opn_tl_last.get(key, 0x7F):
                        opn_tl_pts[key].append((w.sample, cur))
                        opn_tl_last[key] = cur
            if (0xB4 <= r <= 0xB6
                    and not (ch == 5 and st.dac_enabled)):
                # Per-channel pan / PMS / AMS: snapshot at key-on, and write
                # a channel effect when the driver moves it during a note.
                # While the DAC owns channel 6, 0xB6 is FM6 channel state; the
                # DAC's own pan (if the log has one) travels with its notes,
                # so a bare register write must not pan the drums.
                new_b4 = st.pan_pms_ams[ch]
                if new_b4 != b4:
                    target = (
                        ym_base[cid] + 2 if (block_port_ext and ch == 2)
                        else opn_event_ch(cid, ch)
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

        if block_port_ext and ext_tl_notes:
            _bind_ext_levels(
                events, patches, patch_order, ext_tl_notes, ext_tl_floors)

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
        if block_port_csm:
            warnings.append(
                "YM2612 CSM mode: per-operator frequencies are converted, "
                "but timer-generated key-ons are not synthesized"
            )

    # Rhythm is per chip: one log may carry two YM3812s and set the mode on
    # either of them. `getVolMax` answers 63 for the FM chips too, and every
    # OPL column tops out there.
    opl_stats: dict = {}
    if is_opl:
        opl_stats, opl_warn = _collect_opl(
            vgm, events, patches, patch_order, keyon_times, channel_fx, chip_fx,
            kind=opl_state_kind, ids=opl_ids, base=opl_base, clock=opl_clock,
            drums=opl_drums,
            chip_name="ymf278b" if opl_kind == KIND_OPL4 else None,
            freq_base=FREQ_BASE_OPL4 if opl_kind == KIND_OPL4 else None,
        )
        warnings.extend(opl_warn)
        if opl_stats.get("four_op"):
            warnings.append(
                f"{opl_stats['four_op']} note-on(s) read as an OPL3 "
                "four-operator voice (0x104 pair bit)"
            )
        if opl_stats.get("ins_reused"):
            warnings.append(
                f"{opl_stats['ins_reused']} note(s) past the 200-instrument "
                "limit share a patch of the same carrier shape; a Furnace "
                "module stores only 256 instruments in total"
            )

    # OPL4 wave part: 24 columns after the FM block. Furnace does not load the
    # YRW801 ROM itself, so the ROM samples are extracted and embedded.
    opl4_wave_samples: list[Opl4WaveSample] = []
    opl4_wave_events: list[tuple[NoteEvent, int]] = []
    opl4_wave_count = 0
    if is_opl and opl_kind == "opl4":
        rom = assemble_rom(vgm, "ymf278b_rom") or load_yrw801()
        ram = assemble_rom(vgm, "ymf278b_ram")
        if pcm:
            opl4_wave_samples, wave_warn = collect_opl4_wave(
                vgm, rom=rom, ram=ram,
            )
            warnings.extend(wave_warn)
        wave_base = {
            cid: opl_base[cid] + opl_fm_count for cid in opl_ids
        }
        for i, smp in enumerate(opl4_wave_samples):
            ref = opl4_rate(opl_clock, smp.mode_step())
            for h in smp.hits:
                opl4_wave_count += 1
                note, e5 = rate_to_note(opl4_rate(opl_clock, h.step), ref)
                wave_pan = opl4_pan_fx(h.pan)
                # TL is attenuation: 0 loudest, 0x7F silent. The platform maps
                # the column as 0x7f - volume.
                vol = max(0, min(127, 0x7F - (h.level & 0x7F)))
                if (h.pan & 0x0F) == 8:
                    vol = 0          # both sides off: a silent slot
                # $5n level and pan moves during the note are row steps.
                # Interleave the two streams so a pan write between two level
                # writes still lands in the right order. Pan 8 ("both sides
                # off") has no 08xy form; it rides the volume column as zero.
                motion: list[tuple[int, int, int]] = [
                    (t, 0, level) for t, level in h.vol_pts
                ] + [
                    (t, 1, pan) for t, pan in h.pan_pts
                ]
                motion.sort(key=lambda p: p[0])
                last_vol = vol
                last_pan = h.pan & 0x0F
                written_vol = vol
                written_pan = last_pan
                vol_pts: list[tuple[int, int, int | None]] = []
                for when, kind, value in motion:
                    if when <= h.sample_time:
                        continue
                    if h.end_time and when >= h.end_time:
                        break
                    if kind == 0:
                        last_vol = max(0, min(127, 0x7F - (value & 0x7F)))
                    else:
                        last_pan = value & 0x0F
                    target_vol = 0 if last_pan == 8 else last_vol
                    if target_vol == written_vol and last_pan == written_pan:
                        continue
                    pan_fx = (
                        opl4_pan_fx(last_pan)
                        if last_pan != 8 and last_pan != written_pan
                        else None
                    )
                    vol_pts.append((when, target_vol, pan_fx))
                    written_vol = target_vol
                    written_pan = last_pan
                # The driver silences a slot by writing TL=0x7F just before
                # its key-off. The key-off row already stops the note, so the
                # trailing zero is not heard in the source; keeping it only
                # adds a volume-00 row (often on a row with no note) and can
                # outlive the following key-on after row rounding. Keep a zero
                # only when a later write raises the level again.
                while vol_pts and vol_pts[-1][1] == 0:
                    vol_pts.pop()
                # Held f-num/octave writes are pitch bends. A sample channel
                # takes them as 1/64-semitone row steps, like ADPCM-B.
                pitch_steps: list[tuple[int, int]] = []
                if h.step:
                    previous = 0
                    for when, step in h.pitch_pts:
                        if when <= h.sample_time:
                            continue
                        if h.end_time and when >= h.end_time:
                            break
                        delta = int(round(
                            64.0 * 12.0 * math.log2(step / h.step)
                        ))
                        if delta != previous:
                            pitch_steps.append((when, delta))
                            previous = delta
                ev = NoteEvent(
                    sample=h.sample_time,
                    ch=wave_base.get(0, 0) + h.slot,
                    on=True,
                    note=note,
                    ins=-1,  # bound below with the wave map
                    vol=vol,
                    e5=e5,
                    pan=wave_pan,
                    pcm=True,
                    end=h.end_time,
                    vol_pts=tuple(vol_pts),
                    pitch_steps=tuple(pitch_steps),
                )
                if pitch_steps:
                    ev.e5_lo = 0x00
                    ev.e5_hi = 0xFF
                events.append(ev)
                opl4_wave_events.append((ev, i))
        for smp in opl4_wave_samples:
            for h in smp.hits:
                if h.end_time and h.end_time > h.sample_time:
                    events.append(NoteEvent(
                        sample=h.end_time,
                        ch=wave_base.get(0, 0) + h.slot,
                        on=False,
                        note=180,
                        ins=-1,
                        vol=-1,
                        e5=0x80,
                        pan=0,
                        pcm=True,
                    ))

    opll_stats: dict = {}
    if is_opll:
        opll_stats, opll_warn = _collect_opll(
            vgm, events, patches, patch_order, keyon_times, channel_fx, chip_fx,
            ids=opll_ids, base=opll_base, clock=opll_clock,
            drums=opll_drums,
        )
        warnings.extend(opll_warn)
        if opll_drums and opll_rhythm.get("melodic_on_kit"):
            # The kit layout was chosen, yet the log also played those chip
            # channels melodically while rhythm mode was off.
            warnings.append(
                f"{opll_rhythm['melodic_on_kit']} melodic key-on(s) on OPLL "
                "channels 7-9 landed while rhythm mode was off"
            )

    ym2610_ssg_count = 0
    ym2608_ssg_count = 0
    ym2610_a_samples: list[AdpcmASample] = []

    ym2610_b_samples: list[AdpcmBSample] = []
    ym2610_a_count = 0
    ym2610_b_count = 0
    # Events whose instrument index depends on the DAC maps, which are only
    # grouped after the row grid is known: (event, 0=A/1=B, sample index).
    ym2610_events: list[tuple[NoteEvent, int, int]] = []
    if is_opnb:
        states = {cid: YM2610State(ym2610_b) for cid in ym2610_ids}
        held_idx: dict[tuple[int, int, int], int] = {}
        pitch_pts: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        pitch_base: dict[tuple[int, int, int], tuple[int, int]] = {}
        opn_tl_pts: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        opn_tl_last: dict[tuple[int, int, int], int] = {}
        opn_offs: list[int] = []
        lfo_last: dict[int, int] = {}

        def opn_event_ch(cid: int, ch: int) -> int:
            return ym_base[cid] + ch

        def normalized_patch(snap):
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
                    # 64 samples (~1.5 ms) joins a partial f-num write:
                    # drivers write the block/high byte and the low byte at
                    # different samples, and the half-updated value would
                    # otherwise become a short, wrong pitch step.
                    if merged and p[0] - merged[-1][0] <= 64:
                        merged[-1] = p
                    else:
                        merged.append(p)
                if merged and merged[0][0] - ev.sample <= 120:
                    if merged[0][1:] != pitch_base[key]:
                        ev.note, ev.e5 = ym2610_note_for(
                            merged[0][1], merged[0][2], ym2610_clock
                        )
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
                # A driver keys the voice while its carriers still hold the
                # previous (often muted) level and writes the real TL a few
                # samples later. That first louder write is the note's start
                # level; leaving it as a step put the note at volume 0.
                keyon_tl = 127 - max(0, ev.vol)
                if (0 <= tl[0][0] - ev.sample <= OPN_TL_FOLD
                        and tl[0][1] < keyon_tl):
                    ev.vol = max(0, min(127, 127 - tl[0][1]))
                    tl = tl[1:]
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

        def kon_last(writes):
            ordered = []
            i = 0
            while i < len(writes):
                j = i + 1
                while j < len(writes) and writes[j].sample == writes[i].sample:
                    j += 1
                group = writes[i:j]

                def is_kon(w):
                    return (
                        w.chip == "ym2610"
                        and not (w.reg & 0x100)
                        and (w.reg & 0xFF) == 0x28
                    )

                ordered.extend(w for w in group if not is_kon(w))
                ordered.extend(w for w in group if is_kon(w))
                i = j
            return ordered

        for w in kon_last(vgm.writes):
            if w.chip != "ym2610":
                continue
            cid = w.chip_id
            st = states.get(cid)
            if st is None:
                continue
            reg, val = w.reg, w.val
            r = reg & 0xFF
            port = bool(reg & 0x100)
            if not port and r == 0x22:
                st.write(reg, val)
                fx = (0x10 if val & 0x08 else 0x00) | (val & 0x07)
                if lfo_last.get(cid) != fx:
                    chip_fx.append((w.sample, 0x10, fx))
                    lfo_last[cid] = fx
                continue
            if not port and r == 0x27:
                st.write(reg, val)
                continue
            if (port and r <= 0x2D) or (not port and 0x10 <= r <= 0x1F):
                # ADPCM-A/B.  The sample collector owns these registers; the
                # FM state keeps a copy only for the report.
                st.write(reg, val)
                continue
            if not port and r == 0x28:
                ch = st.kon_map.get(val & 7)
                if ch is None:
                    st.write(reg, val)
                    continue
                prev = st.op_mask[ch]
                mask = (val >> 4) & 0x0F
                st.write(reg, val)
                newly_on = mask & ~prev
                key = (cid, ch, -1)
                if mask and newly_on:
                    close_opn_note(key, w.sample, off=False)
                    block, fnum = st.frequency(ch)
                    note, e5 = ym2610_note_for(block, fnum, ym2610_clock)
                    ins, vol = intern_opn_patch(cid, ch, mask)
                    events.append(NoteEvent(
                        sample=w.sample,
                        ch=opn_event_ch(cid, ch),
                        on=True,
                        note=note,
                        ins=ins,
                        vol=vol,
                        e5=e5,
                        pan=st.pan_byte_ch(ch),
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

            ch = st.channel_of(reg)
            if ch is None:
                st.write(reg, val)
                continue
            before = st.frequency(ch)
            b4 = st.pan_pms_ams[ch]
            st.write(reg, val)
            after = st.frequency(ch)
            key = (cid, ch, -1)
            if key in held_idx and after != before:
                pitch_pts[key].append((w.sample, *after))
            if 0x40 <= r <= 0x4E and key in held_idx:
                cur = st.carrier_tl(ch)
                if cur != opn_tl_last.get(key, 0x7F):
                    opn_tl_pts[key].append((w.sample, cur))
                    opn_tl_last[key] = cur
            if 0xB4 <= r <= 0xB6:
                new_b4 = st.pan_pms_ams[ch]
                if new_b4 != b4:
                    target = ym_base[cid] + ch
                    pan = st.pan_byte_ch(ch)
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

        rom_a = assemble_rom(vgm, "ym2610_adpcm") if pcm else b""
        rom_b = assemble_rom(vgm, "ym2610_deltat") if pcm else b""
        if pcm:
            if rom_a or rom_b:
                ym2610_a_samples, ym2610_b_samples, ym_warn = (
                    collect_ym2610_adpcm(vgm, rom_a, rom_b or rom_a)
                )
                warnings.extend(ym_warn)
                for i, smp in enumerate(ym2610_a_samples):
                    for h in smp.hits:
                        ym2610_a_count += 1
                        if h.pan == 0 and not any(p for _t, p in h.pan_pts):
                            continue
                        ev = NoteEvent(
                            sample=h.sample_time,
                            ch=ym_base[h.chip_id] + ym2610_fm_count + 3 + h.channel,
                            on=True,
                            note=60 + (i % YM2610_MAP_SLOTS),
                            ins=-1,
                            vol=0 if h.pan == 0 else max(0, min(31, h.vol)),
                            e5=0x80,
                            pan=h.pan,
                            pcm=True,
                            end=h.end_time,
                        )
                        levels = _pcm_levels(h)
                        if levels:
                            ev.vol_pts = levels
                        events.append(ev)
                        ym2610_events.append((ev, 0, i))
                        if h.end_time and h.end_time > h.sample_time:
                            events.append(NoteEvent(
                                sample=h.end_time,
                                ch=ev.ch,
                                on=False,
                                note=180,
                                ins=-1,
                                vol=-1,
                                e5=0x80,
                                pan=0,
                                pcm=True,
                            ))
                for i, smp in enumerate(ym2610_b_samples):
                    for h in smp.hits:
                        ym2610_b_count += 1
                        if h.pan == 0 and not any(p for _t, p in h.pan_pts):
                            continue
                        pitch_steps, vol_pts = _adpcm_b_motion(h)
                        ev = NoteEvent(
                            sample=h.sample_time,
                            ch=ym_base[h.chip_id] + ym2610_fm_count + 9,
                            on=True,
                            note=60 + (i % YM2610_MAP_SLOTS),
                            ins=-1,
                            # ADPCM-B's channel volume is 8-bit (255 max),
                            # wider than the usual 0-127 pattern column.
                            vol=0 if h.pan == 0 else max(0, min(255, h.vol)),
                            e5=0x80,
                            pan=h.pan,
                            pcm=True,
                            end=h.end_time,
                        )
                        if pitch_steps:
                            ev.pitch_steps = pitch_steps
                            ev.e5_lo = 0x00
                            ev.e5_hi = 0xFF
                        if vol_pts:
                            ev.vol_pts = vol_pts
                        events.append(ev)
                        ym2610_events.append((ev, 1, i))
                        if h.end_time and h.end_time > h.sample_time:
                            events.append(NoteEvent(
                                sample=h.end_time,
                                ch=ev.ch,
                                on=False,
                                note=180,
                                ins=-1,
                                vol=-1,
                                e5=0x80,
                                pan=0,
                                pcm=True,
                            ))
            else:
                warnings.append(
                    "YM2610 ADPCM ROM block missing; sample channels skipped"
                )

    # The OPNA's rhythm kit and delta-T channel. A rhythm voice is picked by
    # which bit of the key byte rose, so no sample data travels with it and all
    # six columns share one empty instrument. ADPCM-B reads the 0x81 block
    # through a sample map, exactly as the YM2610's does.
    ym2608_b_samples: list[AdpcmBSample] = []
    ym2608_rhythm_events: list[NoteEvent] = []
    ym2608_b_events: list[tuple[NoteEvent, int]] = []
    ym2608_rhythm_count = 0
    ym2608_b_count = 0
    if is_opna:
        rhythm_hits, rh_warn = collect_ym2608_rhythm(vgm)
        warnings.extend(rh_warn)
        rom_b = assemble_rom(vgm, "ym2608_deltat") if pcm else b""
        if pcm:
            ym2608_b_samples, dt_warn = collect_ym2608_delta_t(vgm, rom_b)
            warnings.extend(dt_warn)

        for h in rhythm_hits:
            ym2608_rhythm_count += 1
            if h.pan == 0 and not any(p for _t, p in h.pan_pts):
                continue
            ev = NoteEvent(
                sample=h.sample_time,
                ch=ym_base[h.chip_id] + ym2608_fm_count + 3 + h.voice,
                on=True,
                # The voice is the channel; the engine reads no pitch here.
                note=60,
                ins=-1,  # bound with the other sample instruments
                vol=max(0, min(31, h.vol)),
                e5=0x80,
                pan=h.pan,
                pcm=True,
                end=h.end_time,
            )
            levels = _pcm_levels(h)
            if levels:
                ev.vol_pts = levels
            events.append(ev)
            ym2608_rhythm_events.append(ev)
            if h.end_time and h.end_time > h.sample_time:
                events.append(NoteEvent(
                    sample=h.end_time,
                    ch=ev.ch,
                    on=False,
                    note=180,
                    ins=-1,
                    vol=-1,
                    e5=0x80,
                    pan=0,
                    pcm=True,
                ))

        # $11 is the kit's global level, and the platform exposes it as an
        # 1Fxx effect. Keeping it on a rhythm column reproduces the driver's
        # mix balance; folding it into each hit would clamp a quiet kit to
        # column 0 and hide the per-voice level.
        if rhythm_hits:
            for w in vgm.writes:
                if (w.chip != "ym2608" or (w.reg & 0x100)
                        or (w.reg & 0xFF) != 0x11):
                    continue
                if w.chip_id not in ym_base:
                    continue
                target = ym_base[w.chip_id] + ym2608_fm_count + 3
                channel_fx.append((w.sample, target, 0x1F, w.val & 0x3F))

        for i, smp in enumerate(ym2608_b_samples):
            for h in smp.hits:
                ym2608_b_count += 1
                if h.pan == 0 and not any(p for _t, p in h.pan_pts):
                    continue
                pitch_steps, vol_pts = _adpcm_b_motion(h)
                ev = NoteEvent(
                    sample=h.sample_time,
                    ch=ym_base[h.chip_id] + ym2608_fm_count + 9,
                    on=True,
                    note=60 + (i % YM2610_MAP_SLOTS),
                    ins=-1,  # bound below with the ADPCM-B maps
                    vol=0 if h.pan == 0 else max(0, min(255, h.vol)),
                    e5=0x80,
                    pan=h.pan,
                    pcm=True,
                    end=h.end_time,
                )
                if pitch_steps:
                    ev.pitch_steps = pitch_steps
                    ev.e5_lo = 0x00
                    ev.e5_hi = 0xFF
                if vol_pts:
                    ev.vol_pts = vol_pts
                events.append(ev)
                ym2608_b_events.append((ev, i))
                if h.end_time and h.end_time > h.sample_time:
                    events.append(NoteEvent(
                        sample=h.end_time,
                        ch=ev.ch,
                        on=False,
                        note=180,
                        ins=-1,
                        vol=-1,
                        e5=0x80,
                        pan=0,
                        pcm=True,
                    ))

    # The Y8950's ADPCM voice is one column after its FM block. It is the
    # same delta-T engine the OPNA carries, so its samples use the ADPCM-B
    # maps and bind with the YM2608's below.
    y8950_b_samples: list[AdpcmBSample] = []
    y8950_b_events: list[tuple[NoteEvent, int]] = []
    y8950_b_count = 0
    if is_opl and opl_kind == KIND_Y8950:
        rom_b = assemble_rom(vgm, "y8950_deltat") if pcm else b""
        if pcm:
            y8950_b_samples, dt_warn = collect_y8950_delta_t(vgm, rom_b)
            warnings.extend(dt_warn)
        for i, smp in enumerate(y8950_b_samples):
            for h in smp.hits:
                y8950_b_count += 1
                if h.pan == 0 and not any(p for _t, p in h.pan_pts):
                    continue
                pitch_steps, vol_pts = _adpcm_b_motion(h)
                ev = NoteEvent(
                    sample=h.sample_time,
                    ch=opl_base.get(h.chip_id, 0) + opl_fm_count,
                    on=True,
                    note=60 + (i % YM2610_MAP_SLOTS),
                    ins=-1,  # bound below with the ADPCM-B maps
                    vol=max(0, min(255, h.vol)),
                    e5=0x80,
                    pan=h.pan,
                    pcm=True,
                    end=h.end_time,
                )
                if pitch_steps:
                    ev.pitch_steps = pitch_steps
                    ev.e5_lo = 0x00
                    ev.e5_hi = 0xFF
                if vol_pts:
                    ev.vol_pts = vol_pts
                events.append(ev)
                y8950_b_events.append((ev, i))
                if h.end_time and h.end_time > h.sample_time:
                    events.append(NoteEvent(
                        sample=h.end_time,
                        ch=ev.ch,
                        on=False,
                        note=180,
                        ins=-1,
                        vol=-1,
                        e5=0x80,
                        pan=0,
                        pcm=True,
                    ))

    # Collect DAC runs after the row grid is known: the source-sample path
    # needs the grid to check how many row bodies the module will hold.
    dac_samples: list[DacSample] = []
    dac_plays: list[DacPlay] = []
    dac_count = 0
    dac_ch = -1
    dac_stats: dict = {}
    if pcm and is_opn2:
        dac_ch = ym_base.get(ym2612_ids[0], 0) + (8 if ym2612_ext else 5)

    # Genesis PSG.  The SN76489 has no key-on, so the collector gates on its
    # 4-bit attenuation and keeps mid-note period/volume writes as macros.
    psg_count = 0
    if psg_base is not None:
        psg_hits, psg_offs, psg_warn = collect_sn76489(vgm, psg_clock)
        warnings.extend(psg_warn)
        psg_events: list[NoteEvent] = []
        for h in psg_hits:
            ch = psg_at.get(h.chip, psg_base) + h.voice
            vol_pts: tuple[tuple[int, int, int | None], ...] = ()
            if psg_t6w28:
                if h.noise:
                    note, e5 = t6w28_noise_note(h.period, psg_clock)
                    pitch = tuple(h.pitch_pts)
                    fmode = 1 if (h.noise_mode & 4) else 0
                    channel_fx.append((h.sample_time, ch, 0x20, fmode))
                    for t, mode in h.mode_pts:
                        channel_fx.append(
                            (t, ch, 0x20, 1 if (mode & 4) else 0)
                        )
                    tone_div = 15
                else:
                    note, e5 = t6w28_note(h.period, psg_clock)
                    pitch = tuple(h.pitch_pts)
                    tone_div = 16
                vol_pts = tuple(
                    (t, max(0, min(15, v)), pan)
                    for t, v, pan in h.mix_pts
                )
            else:
                tone_div = PSG_DIVIDER
                if h.noise:
                    spec = h.noise_mode & 3
                    # The SMS noise channel's note selects the fixed-rate
                    # preset (`note % 12` is 2, 1, 0 in Furnace for hardware
                    # rates 0-2).  Mode 3 takes its period from PSG channel 3,
                    # so its note is that channel's note and its pitch points
                    # follow that period.
                    if spec == 3:
                        note, e5 = psg_note(h.period)
                        pitch = tuple(h.pitch_pts)
                    else:
                        note = 62 - spec
                        e5 = 0x80
                        pitch = ()
                    fmode = ((h.noise_mode >> 2) & 1) | (
                        0x10 if spec == 3 else 0
                    )
                    channel_fx.append((h.sample_time, ch, 0x20, fmode))
                    for t, mode in h.mode_pts:
                        mf = ((mode >> 2) & 1) | (
                            0x10 if (mode & 3) == 3 else 0
                        )
                        channel_fx.append((t, ch, 0x20, mf))
                else:
                    note, e5 = psg_note(h.period)
                    pitch = tuple(h.pitch_pts)
                if psg_stereo:
                    for t, pan in h.pan_pts:
                        if pan:
                            channel_fx.append((t, ch, 0x08, pan))
                vol_pts = tuple(
                    (t, max(0, min(15, v)), None) for t, v in h.vol_pts
                )
            psg_events.append(NoteEvent(
                sample=h.sample_time,
                ch=ch,
                on=True,
                note=note,
                ins=-1,
                vol=max(0, min(15, h.vol)),
                e5=e5,
                pan=h.opened_pan,
                end=h.off_sample,
                vol_pts=vol_pts,
                tone_pitch_pts=pitch,
                tone_chip=3,
                tone_clock=psg_clock,
                tone_div=tone_div,
                tone_period_offset=0,
            ))
            psg_count += 1
        for off in psg_offs:
            psg_events.append(NoteEvent(
                sample=off.sample_time,
                ch=psg_at.get(off.chip, psg_base) + off.voice,
                on=False,
                note=180,
                ins=-1,
                vol=-1,
                e5=0x80,
                pan=0,
            ))
        psg_events.sort(key=lambda e: (e.sample, 0 if not e.on else 1))
        events.extend(psg_events)
        if not psg_count:
            # Chips that only wrote their init (all four attenuations muted)
            # need no system: an empty PSG would only add four dead columns.
            warnings.append(
                "SN76489 writes carry no audible note; the PSG system was "
                "dropped"
            )
            psg_ids = []
            psg_t6w28 = False
            # Give the reserved columns back. Every sample chip's base is
            # read from fm_end below, and a gap here would shift all of them.
            fm_end -= 4 * max(1, len(psg_at))

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
        pcm_ids = sorted({s.chip_id for s in audible})
        pcm_at = {
            cid: fm_end + 2 * i for i, cid in enumerate(pcm_ids)
        }
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
                    ch=pcm_at.get(smp.chip_id, fm_end) + h.ch,
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
        pcm_ids = []

    oki_samples: list[OkiSample] = []
    adpcm_samples: list[AdpcmSample] = []
    n_adpcm_ins = 0
    n_adpcm_ch = 0
    spcm_audible: list[SegapcmSample] = []
    spcm_ids: list[int] = []
    rf5c68_audible: list[Rf5cSample] = []
    rf5c164_audible: list[Rf5cSample] = []
    rf5c68_ids: list[int] = []
    rf5c164_ids: list[int] = []
    c140_audible: list[C140Sample] = []
    c352_audible: list[C352Sample] = []
    k053260_audible: list[K053260Sample] = []
    ymz_audible: list[YmzSample] = []
    ymz_offs: list[YmzOff] = []
    ymz_ids: list[int] = []
    ymz_events: list[tuple[NoteEvent, int]] = []
    ymz_count = 0
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
        sample_base = fm_end + 2 * len(pcm_ids)
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
        n_adpcm_ch = len(adpcm_ids)
        n_adpcm_ins = msm6258_ins_count(len(adpcm_samples))
        # None means "not yet set". Effect 2 is the /512 default, so a later
        # sample that returns to it still has to write 20 02.
        div_now: dict[int, int | None] = {}
        for i, smp in enumerate(adpcm_samples):
            ins = ins1 + i // ADPCM_MAP_SLOTS
            note = MSM6258_MAP_NOTE + (i % ADPCM_MAP_SLOTS)
            ch = adpcm_at[smp.chip_id]
            fx = msm6258_div_fx(smp.divider)
            if smp.chip_id not in div_now:
                div_now[smp.chip_id] = fx
                if fx is not None:
                    channel_fx.append((0, ch, 0x20, fx))
            elif div_now[smp.chip_id] != fx:
                div_now[smp.chip_id] = fx
                when = smp.hits[0].sample_time if smp.hits else 0
                channel_fx.append((when, ch, 0x20, 2 if fx is None else fx))
            for h in smp.hits:
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=ch,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=127,
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
        spcm_ids = sorted({s.chip_id for s in spcm_audible})
        spcm_base = adpcm_base + len(adpcm_ids)
        spcm_at = {
            cid: spcm_base + 16 * i for i, cid in enumerate(spcm_ids)
        }
        ins2 = ins1 + n_adpcm_ins
        spcm_peak = max(
            (max(h.vol_l, h.vol_r) for s in spcm_audible for h in s.hits), default=0
        )
        spcm_scale = 127.0 if spcm_peak > 16 else 16.0
        for i, smp in enumerate(spcm_audible):
            ins = ins2 + i
            ref = segapcm_rate(spcm_clock, smp.mode_freq())
            for h in smp.hits:
                base_f = segapcm_played_freq(h)
                note, e5 = rate_to_note(
                    segapcm_rate(spcm_clock, base_f), ref,
                )
                # Mid-note frequency writes are a bend around the key-on
                # pitch.  Row ramps carry sub-semitone motion; a large step
                # lands through the sweep encoder's E8/E9 as well.
                sweep: tuple[tuple[int, int], ...] = ()
                if h.freq_pts and base_f:
                    deltas = tuple(
                        (t, int(round(64.0 * 12.0 * math.log2(f / base_f))))
                        for t, f in h.freq_pts
                        if f
                    )
                    vals = [d for _t, d in deltas]
                    if deltas and max(vals + [0]) - min(vals + [0]) >= 4:
                        sweep = deltas
                vl = vol15(h.vol_l, spcm_scale)
                vr = vol15(h.vol_r, spcm_scale)
                # Furnace passes the pattern volume through to the chip, whose
                # register is 7-bit: use the register value's own range.
                vol = max(h.vol_l, h.vol_r) & 0x7F
                pts = tuple(
                    (t, max(a, b) & 0x7F,
                     pcm_pan_fx(vol15(a, spcm_scale), vol15(b, spcm_scale))
                     if max(a, b) else None)
                    for t, a, b in h.vol_pts
                )
                # A hit keyed with both registers at zero is a fade-in or a
                # mute. Start silent on Furnace's default balance; 08 00 would
                # mute the voice forever.
                fade_in = vol == 0 and any(v > 0 for _t, v, _p in pts)
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=spcm_at.get(smp.chip_id, spcm_base) + h.voice,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=0 if fade_in else vol,
                    e5=e5,
                    pan=0 if fade_in else pcm_pan_fx(vl, vr),
                    pcm=True,
                    sweep=sweep,
                    vol_pts=pts,
                    no_pan=fade_in,
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
        c140_base = spcm_base + 16 * len(spcm_ids)
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

        # Konami K053260: four voices per instance. The source rate is
        # clock/(4096-pitch), one step a frame; KADPCM frames are nibbles.
        k053260_clock = vgm.k053260_clock or 3579545
        _k053260_rom, k053260_samples, k053260_offs, k053260_warn = (
            collect_k053260(vgm)
        )
        warnings.extend(k053260_warn)
        k053260_audible = [s for s in k053260_samples if s.length >= 8]
        k053260_ids: list[int] = []
        for smp in k053260_audible:
            for h in smp.hits:
                if h.chip_id not in k053260_ids:
                    k053260_ids.append(h.chip_id)
        k053260_base = c352_base + 32 * len(c352_ids)
        k053260_at = {
            cid: k053260_base + 4 * i for i, cid in enumerate(k053260_ids)
        }
        ins5 = ins4 + len(c352_audible)
        for i, smp in enumerate(k053260_audible):
            ins = ins5 + i
            ref = k053260_rate(k053260_clock, smp.mode_pitch())
            for h in smp.hits:
                base_pitch = k053260_played_pitch(h)
                steps = k053260_merge_steps(h.freq_pts)
                note, e5 = rate_to_note(
                    k053260_rate(k053260_clock, base_pitch), ref,
                )
                sweep: tuple[tuple[int, int], ...] = ()
                if steps and base_pitch:
                    base_rate = k053260_rate(k053260_clock, base_pitch)
                    deltas = tuple(
                        (t, int(round(64.0 * 12.0 * math.log2(
                            k053260_rate(k053260_clock, f) / base_rate
                        ))))
                        for t, f in steps if f
                    )
                    vals = [d for _t, d in deltas]
                    if deltas and max(vals + [0]) - min(vals + [0]) >= 4:
                        sweep = deltas
                init_vol, init_pan = k053260_played_volume(h)
                pts: list[tuple[int, int, int | None]] = []
                last_pan_fx = (
                    None if init_pan == 0 else k053260_pan_fx(init_pan)
                )
                for t, vol_now, pan_now in h.state_pts:
                    if t - h.sample_time <= K053260_REFIT_SAMPLES:
                        continue
                    if pan_now == 0:
                        pts.append((t, 0, None))
                        last_pan_fx = None
                        continue
                    fx = k053260_pan_fx(pan_now)
                    pts.append((t, vol_now & 0x7F, fx if fx != last_pan_fx else None))
                    last_pan_fx = fx
                pan_zero = init_pan == 0
                vol = 0 if pan_zero else init_vol & 0x7F
                pan = 0 if pan_zero else k053260_pan_fx(init_pan)
                events.append(NoteEvent(
                    sample=h.sample_time,
                    ch=k053260_at.get(h.chip_id, k053260_base) + h.voice,
                    on=True,
                    note=note,
                    ins=ins,
                    vol=vol,
                    e5=e5,
                    pan=pan,
                    pcm=True,
                    end=h.off_sample,
                    sweep=sweep,
                    vol_pts=tuple(pts),
                    no_pan=pan_zero,
                ))
        for off in k053260_offs:
            base = k053260_at.get(off.chip_id)
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

        # Ricoh RF5C68 (FM Towns) and RF5C164 (Sega CD): eight PCM voices each,
        # one .fur channel per voice. Rate = clock * step / 786432; the sample's
        # usual step is C-4 and every hit's step is its ratio to that.
        rf5c68_clock = vgm.rf5c68_clock or RF5C68_DEFAULT_CLOCK
        rf5c164_clock = vgm.rf5c164_clock or RF5C164_DEFAULT_CLOCK
        rf5c68_samples: list[Rf5cSample] = []
        rf5c68_offs: list[Rf5cOff] = []
        for cid in sorted({
            w.chip_id for w in vgm.writes if w.chip == "rf5c68"
        }):
            _ram, samples, offs, warn = collect_rf5c(
                vgm, "rf5c68", rf5c68_clock, cid,
            )
            rf5c68_samples.extend(samples)
            rf5c68_offs.extend(offs)
            warnings.extend(warn)
        rf5c164_samples: list[Rf5cSample] = []
        rf5c164_offs: list[Rf5cOff] = []
        for cid in sorted({
            w.chip_id for w in vgm.writes if w.chip == "rf5c164"
        }):
            _ram, samples, offs, warn = collect_rf5c(
                vgm, "rf5c164", rf5c164_clock, cid,
            )
            rf5c164_samples.extend(samples)
            rf5c164_offs.extend(offs)
            warnings.extend(warn)
        rf5c68_audible = [s for s in rf5c68_samples if s.length >= 8]
        rf5c164_audible = [s for s in rf5c164_samples if s.length >= 8]
        rf5c68_ids = sorted({s.chip_id for s in rf5c68_audible})
        rf5c164_ids = sorted({s.chip_id for s in rf5c164_audible})
        rf5c_base = k053260_base + 4 * len(k053260_ids)
        ins6 = ins5 + len(k053260_audible)
        rf5c68_at = {
            cid: rf5c_base + RF5C_VOICES * i
            for i, cid in enumerate(rf5c68_ids)
        }
        rf5c164_at = {
            cid: rf5c_base + RF5C_VOICES * (len(rf5c68_ids) + i)
            for i, cid in enumerate(rf5c164_ids)
        }

        def _rf5c_events(samples, clock, ch_at, ins_base, offs) -> None:
            def ch_of(smp, voice: int) -> int:
                if isinstance(ch_at, dict):
                    return ch_at.get(smp.chip_id, 0) + voice
                return ch_at + voice
            for i, smp in enumerate(samples):
                ins = ins_base + i
                ref = rf5c_rate(clock, smp.mode_step())
                for h in smp.hits:
                    note, e5 = rate_to_note(rf5c_rate(clock, h.step), ref)
                    left = h.pan & 0x0F
                    right = (h.pan >> 4) & 0x0F
                    # The core mixes pan nibble * envelope, so the volume
                    # column carries the peak and the pan effect the ratio.
                    peak = max(left, right)
                    vol = int(round(h.env * peak / 15.0)) & 0xFF
                    events.append(NoteEvent(
                        sample=h.sample_time,
                        ch=ch_of(smp, h.voice),
                        on=True,
                        note=note,
                        ins=ins,
                        vol=vol,
                        e5=e5,
                        pan=pcm_pan_fx(left, right),
                        pcm=True,
                    ))
            for off in offs:
                base = (
                    ch_at.get(off.chip_id, 0)
                    if isinstance(ch_at, dict) else ch_at
                )
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

        _rf5c_events(
            rf5c68_audible, rf5c68_clock, rf5c68_at,
            ins6, rf5c68_offs,
        )
        _rf5c_events(
            rf5c164_audible, rf5c164_clock, rf5c164_at,
            ins6 + len(rf5c68_audible), rf5c164_offs,
        )

        # Yamaha YMZ280B (PCMD8): eight sample voices per instance.  Its
        # channels follow K053260 and precede the pitched tone chips.  Bodies
        # are already sliced from the type-0x86 ROM; each distinct body gets
        # one DIV_INS_YMZ280B instrument.
        ymz_samples, ymz_offs, ymz_warn = collect_ymz280b(vgm)
        warnings.extend(ymz_warn)
        ymz_audible = [s for s in ymz_samples if s.length > 0 and s.hits]
        ymz_ids = sorted({s.chip_id for s in ymz_audible})
        ymz_base = (
            fm_end + 2 * len(pcm_ids)
            + 4 * len({s.chip_id for s in oki_samples}) + n_adpcm_ch
            + 16 * len(spcm_ids)
            + RF5C_VOICES * len(rf5c68_ids)
            + RF5C_VOICES * len(rf5c164_ids)
            + 24 * len({h.chip_id for s in c140_audible for h in s.hits})
            + 32 * len({h.chip_id for s in c352_audible for h in s.hits})
            + 4 * len({h.chip_id for s in k053260_audible for h in s.hits})
        )
        ymz_at = {
            cid: ymz_base + YMZ280B_VOICES * i
            for i, cid in enumerate(ymz_ids)
        }
        for i, smp in enumerate(ymz_audible):
            ref = smp.c4_rate()
            for h in smp.hits:
                base_f = ymz_played_fnum(h)
                clock = smp.clock
                note, e5 = rate_to_note(
                    ymz280b_rate(clock, base_f, smp.depth), ref,
                )
                sweep: tuple[tuple[int, int], ...] = ()
                if h.freq_pts and base_f:
                    base_rate = ymz280b_rate(clock, base_f, smp.depth)
                    deltas = tuple(
                        (t, int(round(64.0 * 12.0 * math.log2(
                            ymz280b_rate(clock, f, smp.depth) / base_rate
                        ))))
                        for t, f in h.freq_pts if f
                    )
                    vals = [d for _t, d in deltas]
                    if deltas and max(vals + [0]) - min(vals + [0]) >= 4:
                        sweep = deltas
                pts = tuple(
                    (t, v & 0xFF, None) for t, v in h.vol_pts
                )
                # A looping body ends only at a real key-off.  A one-shot also
                # ends at the ROM stop address, so the pattern releases there
                # instead of holding a silent keyed voice.
                natural_end = h.sample_time + int(round(
                    smp.length / ymz280b_rate(clock, base_f, smp.depth)
                    * VGM_RATE
                ))
                if smp.loop_start is None:
                    end = min(h.off_sample or natural_end, natural_end)
                else:
                    end = h.off_sample
                ev = NoteEvent(
                    sample=h.sample_time,
                    ch=ymz_at.get(h.chip_id, ymz_base) + h.voice,
                    on=True,
                    note=note,
                    ins=-1,  # bound after the sample-instrument base is known
                    vol=h.vol & 0xFF,
                    e5=e5,
                    pan=ymz_pan_fx(h.pan),
                    pcm=True,
                    end=end,
                    sweep=sweep,
                    vol_pts=pts,
                )
                events.append(ev)
                ymz_events.append((ev, i))
                ymz_count += 1
                for t, pan in h.pan_pts:
                    channel_fx.append((
                        t, ev.ch, 0x08, ymz_pan_fx(pan),
                    ))
        for off in ymz_offs:
            base = ymz_at.get(off.chip_id)
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

    # NES APU: two pulses, triangle, noise and the DPCM/PCM channel.  Collect
    # now so the instrument counts are known before the tone list is placed.
    nes_ids = (
        sorted({w.chip_id for w in vgm.writes if w.chip == "nes"})
        if has_nes else []
    )
    nes_clock = vgm.nes_clock or NES_DEFAULT_CLOCK
    if has_nes:
        nes_tones, nes_offs, nes_warn = collect_nes_tones(vgm, nes_clock)
        warnings.extend(nes_warn)
        nes_dpcm_samples, nes_dpcm_hits, nes_warn = collect_nes_dpcm(
            vgm, nes_clock,
        )
        warnings.extend(nes_warn)
        nes_pcm_samples, nes_pcm_hits, nes_warn = collect_nes_pcm(
            vgm, nes_clock,
        )
        warnings.extend(nes_warn)
        if lossy_samples:
            before = len(nes_pcm_samples)
            nes_pcm_samples, nes_pcm_hits = cluster_nes_pcm(
                nes_pcm_samples, nes_pcm_hits,
            )
            if len(nes_pcm_samples) < before:
                warnings.append(
                    f"NES raw PCM: lossy sample clustering {before} -> "
                    f"{len(nes_pcm_samples)} bodies"
                )
    else:
        nes_tones, nes_offs = [], []
        nes_dpcm_samples, nes_dpcm_hits = [], []
        nes_pcm_samples, nes_pcm_hits = [], []
    n_nes_dpcm_ins = len(nes_dpcm_samples)
    n_nes_pcm_ins = (
        (len(nes_pcm_samples) + NES_PCM_MAP_SLOTS - 1) // NES_PCM_MAP_SLOTS
    )

    # SCC and AY are pitched chips and convert even under --melody-only. Their
    # channels follow the FM block and every sample chip.
    tone_base = (
        fm_end + 2 * len(pcm_ids)
        + 4 * len({s.chip_id for s in oki_samples}) + n_adpcm_ch
        + 16 * len(spcm_ids)
        + RF5C_VOICES * len(rf5c68_ids)
        + RF5C_VOICES * len(rf5c164_ids)
        + 24 * len({h.chip_id for s in c140_audible for h in s.hits})
        + 32 * len({h.chip_id for s in c352_audible for h in s.hits})
        + 4 * len({h.chip_id for s in k053260_audible for h in s.hits})
        + YMZ280B_VOICES * len(ymz_ids)
    )
    # Count the final DAC maps once, below; the collector's list is transient.
    n_ym2610_a_ins = (len(ym2610_a_samples) + YM2610_MAP_SLOTS - 1) // YM2610_MAP_SLOTS
    n_ym2610_b_ins = (len(ym2610_b_samples) + YM2610_MAP_SLOTS - 1) // YM2610_MAP_SLOTS
    # The OPNA's delta-T maps follow the YM2610's, and its rhythm kit rides on
    # one empty map instrument: the engine picks the voice by channel, so no
    # note selects a sample and only the macros are read.
    n_ym2608_b_ins = (len(ym2608_b_samples) + YM2610_MAP_SLOTS - 1) // YM2610_MAP_SLOTS
    n_ym2608_rhythm_ins = 1 if ym2608_rhythm_events else 0
    # The Y8950's ADPCM maps are the last sample instruments, after the OPNA's.
    n_y8950_b_ins = (
        (len(y8950_b_samples) + YM2610_MAP_SLOTS - 1) // YM2610_MAP_SLOTS
    )
    # One instrument per wave sample: the note is the pitch, not a slot.
    n_opl4_wave_ins = len(opl4_wave_samples)
    # An OPL4 with wave samples but no FM notes keeps the empty FM
    # placeholder at instrument 0 (the writer emits it below). The wave
    # instruments then start at 1, so instrument 0x01 is wave 017 and
    # 0x19 is wave 385, matching the reference modules' numbering.
    fm_ins_count = len(patch_order)
    if not fm_ins_count and is_opl and opl4_wave_samples:
        fm_ins_count = 1
    tone_ins_base = (
        fm_ins_count
        + (len(audible) if pcm else 0) + len(oki_samples)
        + n_adpcm_ins + len(spcm_audible)
        + len(rf5c68_audible) + len(rf5c164_audible) + len(c140_audible)
        + len(c352_audible) + len(k053260_audible)
        + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins
        + n_y8950_b_ins + n_opl4_wave_ins
    )
    opl4_wave_ins_base = tone_ins_base - n_opl4_wave_ins
    y8950_b_ins_base = opl4_wave_ins_base - n_y8950_b_ins
    ym2610_b_ins_base = (
        y8950_b_ins_base - n_ym2610_b_ins - n_ym2608_rhythm_ins - n_ym2608_b_ins
    )
    ym2610_a_ins_base = ym2610_b_ins_base - n_ym2610_a_ins
    ym2608_rhythm_ins_base = y8950_b_ins_base - n_ym2608_b_ins - n_ym2608_rhythm_ins
    ym2608_b_ins_base = y8950_b_ins_base - n_ym2608_b_ins
    for ev, kind, idx in ym2610_events:
        base = ym2610_a_ins_base if kind == 0 else ym2610_b_ins_base
        ev.ins = base + idx // YM2610_MAP_SLOTS
    for ev in ym2608_rhythm_events:
        ev.ins = ym2608_rhythm_ins_base
    for ev, idx in ym2608_b_events:
        ev.ins = ym2608_b_ins_base + idx // YM2610_MAP_SLOTS
    for ev, idx in y8950_b_events:
        ev.ins = y8950_b_ins_base + idx // YM2610_MAP_SLOTS
    for ev, idx in opl4_wave_events:
        ev.ins = opl4_wave_ins_base + idx
    scc_ids: list[int] = []
    scc_waves: list[bytes] = []
    scc_ins: list[ToneIns] = []
    ay_ids: list[int] = []
    ay_ins: list[ToneIns] = []
    psg_ins: list[ToneIns] = []
    nes_ins: list[ToneIns] = []
    scc_count = 0
    ay_count = 0
    # SSG/PSG PCM bodies and the notes that play them. The instrument indices
    # are bound after the tone instrument list is final.
    ssg_pcm_samples: list[SsgPcmSample] = []
    ssg_pcm_events: list[tuple[NoteEvent, int]] = []
    ssg_pcm_count = 0
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

    # Each SSG stream can carry PCM (volume-register DAC) as well as tones.
    # Collect both. The tone walker ignores DAC-mode writes because the mixer
    # turns the voice off, so the two never claim the same time.
    def _ssg_pcm(chip_name: str):
        if not pcm:
            return [], []
        got, got_hits, got_warn = collect_ssg_pcm(vgm, chip=chip_name)
        warnings.extend(got_warn)
        if lossy_samples:
            before = len(got)
            got, got_hits = cluster_ssg_pcm(got, got_hits)
            if len(got) < before:
                warnings.append(
                    f"{chip_name}: lossy sample clustering {before} -> "
                    f"{len(got)} bodies"
                )
        return got, got_hits

    def _emit_pcm(got, got_hits, at) -> None:
        nonlocal ssg_pcm_count
        if not got_hits:
            return
        base = len(ssg_pcm_samples)
        ssg_pcm_samples.extend(got)
        n, bound = _emit_ssg_pcm_notes(events, got_hits, at, base)
        ssg_pcm_count += n
        ssg_pcm_events.extend(bound)

    ay_pcm, ay_pcm_hits = _ssg_pcm("ay8910")
    ay_hits, ay_offs, ay_warn = collect_ay8910(vgm)
    warnings.extend(ay_warn)
    ay_clock = vgm.ay8910_clock or AY_DEFAULT_CLOCK
    # A Famicom's AY is a Sunsoft 5B with SEL tied low, so it runs at half
    # the logged CPU clock.  The VGM's pin-26 flag (0x79 bit 4) says the same
    # thing on other platforms.  The tracker's pitch table still runs from the
    # logged clock, so label the notes at half speed; the written period is
    # then doubled and the chip sounds at the real /2 rate.  A rip that
    # already logged the halved clock is left alone.
    nes_clk = vgm.nes_clock or NES_DEFAULT_CLOCK
    if nes_ids:
        ay_half = abs(ay_clock - nes_clk) <= 16
    else:
        ay_half = bool(getattr(vgm, "ay8910_flags", 0) & 0x10)
    ay_note_clock = (ay_clock // 2) if ay_half else ay_clock
    ay_cids = sorted(
        {h.chip_id for h in ay_hits} | {h.chip_id for h in ay_pcm_hits}
    )
    if ay_cids:
        for cid in ay_cids:
            ay_ids.append(cid)
        ay_at = {cid: tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * i
                 for i, cid in enumerate(ay_ids)}
        if ay_hits:
            ay_count = _emit_ay_notes(
                events, channel_fx, ay_hits, ay_offs, ay_at, ay_note_clock, 8,
                tone_ins_base + len(scc_ins), vgm.total_samples or 0,
            )
        _emit_pcm(ay_pcm, ay_pcm_hits, ay_at)

    # NES APU columns: two pulses, triangle, noise and DPCM/PCM. They follow
    # the SCC/AY blocks in module order.
    if nes_ids:
        nes_base = (
            tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * len(ay_ids)
        )
        nes_tone_events: list[NoteEvent] = []
        for h in nes_tones:
            ch = nes_base + h.voice
            if h.voice == 3:
                note = nes_noise_note(h.period)
                e5 = 0x80
                sweep: tuple[tuple[int, int], ...] = ()
            else:
                note, e5 = nes_note(nes_clock, h.period)
                deltas = tuple(
                    (t, nes_period_delta(h.period, per))
                    for t, per in h.pitch_pts
                )
                vals = [d for _t, d in deltas]
                sweep = deltas if deltas and max(vals + [0]) - min(vals + [0]) >= 4 else ()
            # Duty/noise mode, envelope mode and the triangle linear counter
            # are row effects.
            if h.voice < 2:
                channel_fx.append((h.sample_time, ch, 0x12, h.duty))
                for t, duty in h.duty_pts:
                    channel_fx.append((t, ch, 0x12, duty))
            elif h.voice == 3:
                channel_fx.append((h.sample_time, ch, 0x12, h.duty))
                for t, duty in h.duty_pts:
                    channel_fx.append((t, ch, 0x12, duty))
                channel_fx.append((h.sample_time, ch, 0x15, h.env_mode))
                for t, mode in h.env_pts:
                    channel_fx.append((t, ch, 0x15, mode))
            else:
                channel_fx.append((h.sample_time, ch, 0x19, h.linear))
                for t, linear in h.linear_pts:
                    channel_fx.append((t, ch, 0x19, linear))
            if h.voice < 2:
                channel_fx.append((h.sample_time, ch, 0x15, h.env_mode))
                for t, mode in h.env_pts:
                    channel_fx.append((t, ch, 0x15, mode))
            ev = NoteEvent(
                sample=h.sample_time,
                ch=ch,
                on=True,
                note=note,
                ins=-1,
                vol=15 if h.voice == 2 else h.vol,
                e5=e5,
                pan=0,
                end=h.off_sample,
                vol_pts=tuple((t, v, None) for t, v in h.vol_pts),
                sweep=sweep,
                sweep_div=2.0,
                e5_lo=0x00,
                e5_hi=0xFF,
                # Tone-chip marker: keeps instrument rebasing in step and lets
                # the writer put volume steps on the note row.  `tone_pitch_pts`
                # lets a per-note instrument carry the driver's pitch motion;
                # `sweep` stays as the all-on-the-columns fallback.
                tone_pitch_pts=tuple(h.pitch_pts) if h.voice != 3 else (),
                tone_clock=nes_clock,
                tone_div=NES_CPU_DIVIDER,
                tone_period_offset=1,
                tone_chip=4,
            )
            events.append(ev)
            nes_tone_events.append(ev)
        for off in nes_offs:
            events.append(NoteEvent(
                sample=off.sample_time, ch=nes_base + off.voice, on=False,
                note=180, ins=-1, vol=-1, e5=0x80, pan=0,
            ))
        # DPCM: the note value selects the DMC rate (60 + rate index); one
        # instrument per sample body. PCM: note 108 plays an 8-bit body at its
        # stored rate.
        nes_dpcm_events: list[tuple[NoteEvent, int]] = []
        for hit in nes_dpcm_hits:
            ch = nes_base + 4
            channel_fx.append((hit.sample_time, ch, 0x18, 1))
            channel_fx.append((hit.sample_time, ch, 0x11, 0))
            ev = NoteEvent(
                sample=hit.sample_time, ch=ch, on=True,
                note=60 + (hit.rate_index & 0x0F), ins=-1, vol=15,
                e5=0x80, pan=0, end=hit.end_time, no_pan=True, pcm=True,
            )
            events.append(ev)
            nes_dpcm_events.append((ev, hit.sample_index))
            if hit.end_time:
                # The driver clears $4015 to stop the sample early (a drum
                # that is cut before its body ends). Without an explicit off
                # the tracker would play the whole body.
                events.append(NoteEvent(
                    sample=hit.end_time, ch=ch, on=False, note=180, ins=-1,
                    vol=-1, e5=0x80, pan=0, pcm=True,
                ))
        nes_pcm_events: list[tuple[NoteEvent, int]] = []
        pcm_mode_set = False
        for hit in nes_pcm_hits:
            ch = nes_base + 4
            if not pcm_mode_set:
                channel_fx.append((hit.sample_time, ch, 0x18, 0))
                pcm_mode_set = True
            ev = NoteEvent(
                sample=hit.sample_time, ch=ch, on=True,
                note=60 + (hit.sample_index % NES_PCM_MAP_SLOTS),
                ins=-1, vol=15, e5=0x80, pan=0, end=hit.end_time,
                no_pan=True, dac=True,
            )
            events.append(ev)
            nes_pcm_events.append((ev, hit.sample_index))
            if hit.end_time:
                events.append(NoteEvent(
                    sample=hit.end_time, ch=ch, on=False, note=180, ins=-1,
                    vol=-1, e5=0x80, pan=0,
                ))
    else:
        nes_base = None
        nes_tone_events = []
        nes_dpcm_events = []
        nes_pcm_events = []

    # HuC6280 (PC Engine) columns: six wavetable voices. They follow the NES
    # block in module order. A note's instrument is its 32x5-bit wavetable;
    # the control volume is the column and the balance register rides as 08xy.
    huc6280_ids: list[int] = []
    huc6280_waves: list[bytes] = []
    huc6280_ins: list[ToneIns] = []
    huc_pcm_samples: list[Huc6280PcmSample] = []
    huc6280_count = 0
    huc6280_pcm_count = 0
    huc6280_pcm_events: list[tuple[NoteEvent, int]] = []
    huc_notes, huc_offs, huc_wave_list, huc_warn = collect_huc6280(vgm)
    warnings.extend(huc_warn)
    if pcm:
        huc_pcm_samples, huc_pcm_hits, huc_warn = collect_huc6280_pcm(vgm)
    else:
        huc_pcm_samples, huc_pcm_hits = [], []
    warnings.extend(huc_warn)
    n_huc6280_pcm_ins = len(huc_pcm_samples)
    huc6280_clock = vgm.huc6280_clock or HUC6280_DEFAULT_CLOCK
    huc_cids = sorted(
        {n.hit.chip_id for n in huc_notes}
        | {hit.chip_id for hit in huc_pcm_hits}
    )
    if huc_cids:
        for cid in huc_cids:
            huc6280_ids.append(cid)
        huc_base = (
            tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * len(ay_ids)
            + (NES_CHANNELS if nes_ids else 0)
        )
        huc_at = {
            cid: huc_base + HUC6280_CHANNELS * i
            for i, cid in enumerate(huc6280_ids)
        }
        huc_clock = huc6280_clock
        huc6280_waves = list(huc_wave_list)
        huc_wave_index = {img: i for i, img in enumerate(huc6280_waves)}

        def _merge_vol_balance(hit):
            # Collapse the control-volume and balance timelines into the
            # effective volume column plus the normalized L/R pan, so the
            # pattern's volume column carries the envelope rather than showing
            # 31 with all of it hidden in the balance register.
            evs = [(t, 0, v) for t, v in hit.vol_pts]
            evs += [(t, 1, b) for t, b in hit.balance_pts]
            evs.sort(key=lambda x: (x[0], x[1]))
            out: list[tuple[int, int, int | None]] = []
            cur_vol, cur_bal = hit.vol, hit.balance
            for t, kind, val in evs:
                if kind == 0:
                    cur_vol = val
                else:
                    cur_bal = val
                v, p = pce_effective(cur_vol, cur_bal)
                out.append((t, v, p))
            return out

        for n in huc_notes:
            h = n.hit
            ch = huc_at.get(h.chip_id)
            if ch is None:
                continue
            voice_ch = ch + h.voice
            gate_end = h.off_sample or (vgm.total_samples or h.sample_time)
            # A noise-capable voice can switch between periodic noise and the
            # loaded waveform several times inside one gate (LOX1 Wave 5 turns
            # noise off for a tone passage and back on).  Split at every
            # transition: the note boundary is placed with EDxx (tick
            # resolution) and the instrument's noise macro sets the mode on
            # the note's first tick, so the switch lands on the tick instead
            # of the nearest row.  A single macro over the whole 35 s gate
            # would be thinned to ~140 ms per step.
            has_noise = h.voice >= 4 and (
                bool(h.noise & NOISE_ENABLE)
                or any(reg & NOISE_ENABLE for _t, reg in h.noise_pts)
            )
            if has_noise:
                mode_pts = [
                    (h.sample_time, 1 if (h.noise & NOISE_ENABLE) else 0)
                ]
                for t, reg in h.noise_pts:
                    if h.sample_time < t < gate_end:
                        m = 1 if (reg & NOISE_ENABLE) else 0
                        if m != mode_pts[-1][1]:
                            mode_pts.append((t, m))
                for i, (st, m) in enumerate(mode_pts):
                    en = (
                        mode_pts[i + 1][0]
                        if i + 1 < len(mode_pts) else gate_end
                    )
                    if en <= st:
                        continue
                    ev_vol, ev_pan = pce_effective(
                        _value_at(h.vol_pts, st, h.vol),
                        _value_at(h.balance_pts, st, h.balance),
                    )
                    ev_vol_pts = tuple(
                        p for p in _merge_vol_balance(h) if st < p[0] < en
                    )
                    if ev_vol_pts and (
                        ev_vol_pts[0][0] - st <= HUC6280_ATTACK_WINDOW
                    ):
                        _t, ev_vol, ev_pan = ev_vol_pts[0]
                        ev_vol_pts = ev_vol_pts[1:]
                    if m:
                        regs = [
                            (st, _value_at(h.noise_pts, st, h.noise) & 0x1F)
                        ]
                        last_noise = regs[0][1]
                        for t, reg in h.noise_pts:
                            if not (st < t < en and (reg & NOISE_ENABLE)):
                                continue
                            value = reg & 0x1F
                            # Compare against the last *accepted* pitch. The
                            # list comprehension compared against the start
                            # value every time, so the alternating return to
                            # the first noise pitch was dropped: the channel
                            # stayed on the snare pitch for the rest of the
                            # gate.
                            if value != last_noise:
                                regs.append((t, value))
                                last_noise = value
                        note = huc6280_noise_note(
                            _value_at(h.noise_pts, st, h.noise)
                        )
                        events.append(NoteEvent(
                            sample=st, ch=voice_ch, on=True, note=note,
                            ins=tone_ins_base, vol=ev_vol, e5=0x80,
                            pan=ev_pan, end=en, vol_pts=ev_vol_pts,
                            tone_pitch_pts=(
                                tuple(regs) if len(regs) > 1 else ()
                            ),
                            tone_noise=True, tone_noise_end=0,
                            tone_wave=n.wave_index, tone_chip=5,
                        ))
                    else:
                        pts = [
                            (st, _value_at(h.pitch_pts, st, h.period))
                        ]
                        pts += [
                            (t, p) for t, p in h.pitch_pts if st < t < en
                        ]
                        # A tone passage can open before the driver writes
                        # the frequency; use the first real period so the
                        # note is not a bogus top-of-range blip.
                        real = [(t, p) for t, p in pts if p > 0]
                        if not real:
                            continue
                        base_t, base_p = real[0]
                        note, e5 = huc6280_note(huc_clock, base_p)
                        tail = [(t, p) for t, p in real if t > base_t]
                        events.append(NoteEvent(
                            sample=st, ch=voice_ch, on=True, note=note,
                            ins=tone_ins_base, vol=ev_vol, e5=e5,
                            pan=ev_pan, end=en, vol_pts=ev_vol_pts,
                            tone_pitch_pts=tuple(tail),
                            tone_wave=n.wave_index, tone_chip=5,
                            tone_period=base_p,
                        ))
                    huc6280_count += 1
                continue
            if not any(h.wave):
                # A tone note needs a waveform; a noise note above does not.
                continue
            if h.voice >= 4:
                channel_fx.append((h.sample_time, voice_ch, 0x11, 0))
            seg_pts = [(h.sample_time, h.period)]
            seg_pts += list(h.pitch_pts)
            # Collapse only the adjacent LO/HI byte pair. A one-frame merge
            # would swallow the driver's per-frame vibrato and leave a single
            # arbitrary pitch value (unstable, too-flat notes).
            segs = segment_periods(
                seg_pts, gate_end, huc_clock, 32, 0,
                merge_gap=_AY_MERGE_GAP, drop_short=False,
            )
            for i, (st, period, intra) in enumerate(segs):
                nxt = segs[i + 1][0] if i + 1 < len(segs) else 0
                seg_end = nxt or h.off_sample
                wave = _value_at(h.wave_pts, st, h.wave)
                if not any(wave):
                    events.append(NoteEvent(
                        sample=st, ch=voice_ch, on=False, note=180,
                        ins=-1, vol=-1, e5=0x80, pan=0,
                    ))
                    continue
                bound = nxt or gate_end
                vol, balance = pce_effective(
                    _value_at(h.vol_pts, st, h.vol),
                    _value_at(h.balance_pts, st, h.balance),
                )
                vol_pts = tuple(
                    p for p in _merge_vol_balance(h) if st < p[0] < bound
                )
                # The PCE driver often writes the note's attack balance a few
                # samples after the gate. That is the note's first level, not
                # a same-row volume step (which the writer would drop): fold it
                # into the note's own volume/pan so the attack is audible.
                if vol_pts and vol_pts[0][0] - st <= HUC6280_ATTACK_WINDOW:
                    _t, vol, balance = vol_pts[0]
                    vol_pts = vol_pts[1:]
                steps = _tone_steps(
                    intra, st, HUC6280_REFIT_SAMPLES if i == 0 else 0,
                    merge_gap=_AY_MERGE_GAP,
                )
                wave_steps: list[tuple[int, int]] = []
                for t, img in merge_step_writes(
                    [(t, img) for t, img in h.wave_pts if st < t < bound],
                    gap=64,
                ):
                    idx = huc_wave_index.get(img)
                    if idx is None:
                        idx = len(huc6280_waves)
                        huc_wave_index[img] = idx
                        huc6280_waves.append(img)
                    if wave_steps and wave_steps[-1][1] == idx:
                        continue
                    wave_steps.append((t, idx))
                wave_index = huc_wave_index.get(wave)
                if wave_index is None:
                    wave_index = len(huc6280_waves)
                    huc_wave_index[wave] = wave_index
                    huc6280_waves.append(wave)
                note, e5 = huc6280_note(huc_clock, period)
                events.append(NoteEvent(
                    sample=st, ch=voice_ch, on=True, note=note,
                    ins=tone_ins_base, vol=vol, e5=e5, pan=balance,
                    end=seg_end, vol_pts=vol_pts,
                    tone_pitch_pts=tuple(steps),
                    tone_wave_pts=tuple(wave_steps),
                    tone_wave=wave_index,
                    tone_period_offset=0,
                    tone_chip=5,
                    tone_period=period,
                ))
                huc6280_count += 1
        events[:] = _split_pce_volume_peaks(
            events, huc_clock, vgm.total_samples or 0,
        )

        # The PCE effective level is control volume + balance. Once the volume
        # envelope moves into the instrument's tick-rate macro, the balance's
        # left/right ratio is the only part left for the pattern. Split it out
        # as 08xy points so the macro can be the register level.
        for ev in events:
            if ev.tone_chip != 5 or not ev.vol_pts:
                continue
            last_pan = ev.pan
            pts: list[tuple[int, int, int | None]] = []
            for t, value, pan in ev.vol_pts:
                if pan is not None and pan != last_pan:
                    channel_fx.append((t, ev.ch, 0x08, pan & 0xFF))
                    last_pan = pan
                pts.append((t, value, None))
            ev.vol_pts = tuple(pts)

        for off in huc_offs:
            base = huc_at.get(off.chip_id)
            if base is None:
                continue
            events.append(NoteEvent(
                sample=off.sample_time, ch=base + off.voice, on=False,
                note=180, ins=-1, vol=-1, e5=0x80, pan=0,
            ))
        # Direct-D/A stream bodies: note 108 plays an 8-bit sample at its
        # stored rate from a DIV_INS_AMIGA instrument.
        for hit in huc_pcm_hits:
            base = huc_at.get(hit.chip_id, huc_base)
            ref = (
                huc_pcm_samples[hit.sample_index].rate
                if 0 <= hit.sample_index < len(huc_pcm_samples) else 0
            )
            note, e5 = _pcm_rate_note(hit.rate or ref, ref)
            ev = NoteEvent(
                sample=hit.sample_time, ch=base + hit.voice, on=True,
                note=note, ins=-1, vol=31, e5=e5, pan=0,
                end=hit.end_time, no_pan=True, pcm=True,
            )
            events.append(ev)
            huc6280_pcm_events.append((ev, hit.sample_index))
        huc6280_pcm_count = len(huc_pcm_hits)

    # Game Boy DMG: two pulse voices, the wavetable voice and noise, in that
    # order after the PCE block. A note's instrument is a DIV_INS_GB that
    # carries the hardware envelope (or, when the driver used the zombie
    # volume bug, a volume macro); the wave voice also selects a 32-nibble
    # wavetable through the wave-synth feature.
    gb_ids: list[int] = []
    gb_waves: list[bytes] = []
    gb_ins: list[ToneIns] = []
    gb_count = 0
    gb_clock = vgm.gb_dmg_clock or GB_DEFAULT_CLOCK
    gb_hits, gb_offs, gb_wave_list, gb_warn = collect_gb(vgm, gb_clock)
    warnings.extend(gb_warn)
    if gb_hits or gb_offs:
        gb_ids.append(0)
        gb_base = (
            tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * len(ay_ids)
            + (NES_CHANNELS if nes_ids else 0)
            + (HUC6280_CHANNELS if huc6280_ids else 0)
        )
        gb_waves = list(gb_wave_list)
        gb_wave_index = {img: i for i, img in enumerate(gb_waves)}
        for h in gb_hits:
            ch = gb_base + h.voice
            if h.voice == 3:
                note = gb_noise_note(h.period)
                e5 = 0x80
                # The binding's noise branch converts these raw note positions
                # to offsets (it subtracts the note's own position), so store
                # the ntPos for each NR43, not the already-subtracted offset.
                offsets: list[tuple[int, int]] = []
                for t, nr43 in h.pitch_pts:
                    pos = gb_noise_note(nr43) - 60
                    if offsets and offsets[-1][1] == pos:
                        continue
                    offsets.append((t, pos))
                pitch_pts: tuple[tuple[int, int], ...] = tuple(offsets)
            else:
                note, e5 = gb_note(gb_clock, h.period)
                # The binding works in pitch-table units; the GB table holds
                # ``2048 - register``.
                pitch_pts = tuple((t, 2048 - p) for t, p in h.pitch_pts)
            # Duty (pulse) / noise width. Both use the 12xx effect.
            if h.voice != 2:
                channel_fx.append((h.sample_time, ch, 0x12, h.duty))
                for t, d in h.duty_pts:
                    channel_fx.append((t, ch, 0x12, d))
            # Ch1 hardware sweep.
            if h.voice == 0 and (h.sweep & 0x7F):
                channel_fx.append((
                    h.sample_time, ch, 0x13,
                    (((h.sweep >> 4) & 7) << 4) | (h.sweep & 7),
                ))
                channel_fx.append((h.sample_time, ch, 0x14, (h.sweep >> 3) & 1))
                for t, sw in h.sweep_pts:
                    channel_fx.append((
                        t, ch, 0x13, (((sw >> 4) & 7) << 4) | (sw & 7),
                    ))
                    channel_fx.append((t, ch, 0x14, (sw >> 3) & 1))
            # Stereo: NR51's per-channel L/R map is Furnace's 08xy.
            ev_pan = h.pan
            if ev_pan == 0:
                # Both off: a real mute, not "leave the previous pan".
                ev_pan = 0
            for t, p in h.pan_pts:
                channel_fx.append((t, ch, 0x08, p))
            wave_steps: list[tuple[int, int]] = []
            if h.voice == 2:
                for t, w in h.wave_pts:
                    idx = gb_wave_index.get(gb_waves[w]) if w < len(gb_waves) else None
                    if idx is None:
                        idx = len(gb_waves)
                        gb_wave_index[gb_waves[w]] = idx
                    if wave_steps and wave_steps[-1][1] == idx:
                        continue
                    wave_steps.append((t, idx))
            vol_pts = tuple((t, v, None) for t, v in h.vol_pts)
            ev = NoteEvent(
                sample=h.sample_time,
                ch=ch,
                on=True,
                note=note,
                ins=-1,
                # The column is the note's maximum. A hardware envelope is
                # restored by the instrument's hardware sequence (see
                # _write_ins_gb), so it starts at the source's initial volume
                # without the column capping it. The wave voice's column is
                # NR32 (0/5/10/15) and soft-envelope notes carry their onset
                # volume in the volume macro.
                vol=(15 if h.voice != 2 and not h.soft_env else h.vol),
                e5=e5,
                pan=ev_pan,
                end=h.off_sample,
                vol_pts=vol_pts,
                tone_pitch_pts=pitch_pts,
                tone_wave_pts=tuple(wave_steps),
                tone_wave=h.wave if h.voice == 2 else -1,
                tone_clock=gb_clock,
                tone_div=GB_PITCH_DIVIDER,
                tone_period_offset=0,
                tone_chip=6,
                tone_gb_wave=h.voice == 2,
                tone_noise=h.voice == 3,
                gb_soft_env=h.soft_env,
                gb_env_vol=h.env_vol,
                gb_env_dir=h.env_dir,
                gb_env_len=h.env_len,
                gb_sound_len=h.sound_len,
            )
            events.append(ev)
            gb_count += 1
        for off in gb_offs:
            events.append(NoteEvent(
                sample=off.sample_time, ch=gb_base + off.voice, on=False,
                note=180, ins=-1, vol=-1, e5=0x80, pan=0,
            ))

    # Virtual Boy VSU: five wavetable voices and one noise voice, after the
    # Game Boy block.  The driver may keep a voice keyed and articulate with
    # EV0, or use key-on/key-off; the collector handles both.  Wave images
    # become 10xx pattern effects (the sentinel 0x1000 is resolved once the
    # module's wave list is known), and channel 4's sweep/modulation rides
    # Furnace's native 13xx/14xy effects plus the instrument FD table.
    vsu_ids: list[int] = []
    vsu_waves: list[bytes] = []
    vsu_mod_waves: list[bytes] = []
    vsu_ins: list[ToneIns] = []
    vsu_count = 0
    vsu_clock = vgm.vsu_clock or VSU_DEFAULT_CLOCK
    vsu_hits, vsu_offs, vsu_wave_list, vsu_mod_list, vsu_dynamic, vsu_warn = (
        collect_vsu(vgm)
    )
    warnings.extend(vsu_warn)
    if vsu_hits or vsu_offs:
        vsu_ids = sorted({h.chip_id for h in vsu_hits})
        vsu_waves = list(vsu_wave_list)
        vsu_mod_waves = list(vsu_mod_list)
        vsu_base = (
            tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * len(ay_ids)
            + (NES_CHANNELS if nes_ids else 0)
            + (HUC6280_CHANNELS if huc6280_ids else 0)
            + (4 * len(gb_ids))
        )
        vsu_at = {
            cid: vsu_base + VSU_CHANNELS * i
            for i, cid in enumerate(vsu_ids)
        }
        for h in vsu_hits:
            ch = vsu_at.get(h.chip_id)
            if ch is None:
                continue
            clock = vgm.vsu_clock or VSU_DEFAULT_CLOCK
            note, e5 = vsu_note(clock, h.period)
            vol_pts = tuple((t, v, None) for t, v in h.vol_pts)
            ev = NoteEvent(
                sample=h.sample_time,
                ch=ch + h.voice,
                on=True,
                note=note,
                ins=-1,
                vol=h.vol,
                e5=e5,
                pan=h.pan,
                end=h.off_sample,
                vol_pts=vol_pts,
                tone_pitch_pts=tuple(h.pitch_pts),
                tone_clock=clock,
                tone_div=VSU_PITCH_DIVIDER,
                tone_period_offset=0,
                tone_chip=7,
                tone_wave=h.wave if h.voice != 5 else -1,
                tone_mod=h.mod if h.voice == 4 else 0,
            )
            events.append(ev)
            vsu_count += 1
            chn = ch + h.voice
            # The starting wave lives on the instrument's wave-synth entry;
            # a 10xx on the note row would be undone by the note-on's wave
            # reset.  Only mid-note changes ride the pattern.
            if h.voice != 5:
                for t, idx in h.wave_pts:
                    channel_fx.append((
                        t, chn, 0x10, 0x1000 | (idx & 0xFF),
                    ))
            # Direct 4-bit left/right levels: 08xy, no centre law.
            for t, p in h.pan_pts:
                channel_fx.append((t, chn, 0x08, p & 0xFF))
            # Channel 5 noise tap (EV1 bits 4-6) is 11xx.
            if h.voice == 5:
                channel_fx.append((h.sample_time, chn, 0x11, h.tap & 7))
                for t, tap in h.tap_pts:
                    channel_fx.append((t, chn, 0x11, tap & 7))
            # Channel 4 sweep/modulation.
            if h.voice == 4:
                effect = _vsu_sw_effect(h.sweep, h.ev1)
                if effect is not None:
                    channel_fx.append((h.sample_time, chn, *effect))
                for t, sweep, ev1 in h.sw_pts:
                    effect = _vsu_sw_effect(sweep, ev1)
                    if effect is not None:
                        channel_fx.append((t, chn, *effect))
        for off in vsu_offs:
            base = vsu_at.get(off.chip_id)
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
            ))

    # Philips SAA1099: six square/noise voices per chip, after the VSU block.
    # A CMS/Game Blaster log normally carries two chips; each becomes its own
    # Furnace 0x97 system, which Furnace names Creative Music System.
    saa_ids: list[int] = []
    saa_ins: list[ToneIns] = []
    saa_count = 0
    saa_clock = vgm.saa1099_clock or SAA_DEFAULT_CLOCK
    saa_hits, saa_offs, saa_warn = collect_saa1099(vgm)
    warnings.extend(saa_warn)
    if saa_hits or saa_offs:
        saa_ids = sorted({
            w.chip_id for w in vgm.writes if w.chip == "saa1099"
        })
        if not saa_ids:
            saa_ids = sorted({h.chip_id for h in saa_hits})
        saa_base = (
            tone_base + SCC_VOICES * len(scc_ids) + AY_VOICES * len(ay_ids)
            + (NES_CHANNELS if nes_ids else 0)
            + (HUC6280_CHANNELS if huc6280_ids else 0)
            + (4 * len(gb_ids))
            + VSU_CHANNELS * len(vsu_ids)
        )
        saa_at = {
            cid: saa_base + SAA_VOICES * i
            for i, cid in enumerate(saa_ids)
        }
        saa_count = _emit_saa_notes(
            events, channel_fx, saa_hits, saa_offs, saa_at, saa_clock,
            vgm.total_samples or 0,
        )

    # The YM2203 SSG rides in the same six-channel system: 0-2 FM, 3-5 PSG. Its
    # notes share the AY instrument list.
    ssg_pcm, ssg_pcm_hits = _ssg_pcm("ym2203_ssg")
    ssg_hits, ssg_offs, ssg_warn = collect_ay8910(
        vgm, chip="ym2203_ssg", clock=ym_clock, divider=YM2203_SSG_DIVIDER,
    )
    warnings.extend(ssg_warn)
    if (ssg_hits or ssg_pcm_hits) and ym_ids:
        ssg_at = {
            cid: ym_base[cid] + (6 if ym2203_ext else 3) for cid in ym_ids
        }
        if ssg_hits:
            ym2203_ssg_count = _emit_ay_notes(
                events, channel_fx, ssg_hits, ssg_offs, ssg_at, ym_clock,
                YM2203_SSG_DIVIDER, tone_ins_base + len(scc_ins),
                vgm.total_samples or 0,
            )
            # The SSG voices share the AY instrument path; count them with the
            # other PSG notes so the placeholder logic and reports see them.
            ay_count += ym2203_ssg_count
        _emit_pcm(ssg_pcm, ssg_pcm_hits, ssg_at)

    # The YM2610's SSG is inside the same system, after the FM block.
    ym2610_pcm, ym2610_pcm_hits = _ssg_pcm("ym2610_ssg")
    ym2610_ssg_hits, ym2610_ssg_offs, ym2610_ssg_warn = collect_ay8910(
        vgm, chip="ym2610_ssg", clock=ym2610_clock,
        divider=YM2610_SSG_DIVIDER,
    )
    warnings.extend(ym2610_ssg_warn)
    if (ym2610_ssg_hits or ym2610_pcm_hits) and ym2610_ids:
        ssg_at = {
            cid: ym_base[cid] + ym2610_fm_count for cid in ym2610_ids
        }
        if ym2610_ssg_hits:
            ym2610_ssg_count = _emit_ay_notes(
                events, channel_fx, ym2610_ssg_hits, ym2610_ssg_offs, ssg_at,
                ym2610_clock, YM2610_SSG_DIVIDER,
                tone_ins_base + len(scc_ins), vgm.total_samples or 0,
            )
            ay_count += ym2610_ssg_count
        _emit_pcm(ym2610_pcm, ym2610_pcm_hits, ssg_at)

    # The OPNA's SSG sits after its FM block and divides the same way.
    ym2608_pcm, ym2608_pcm_hits = _ssg_pcm("ym2608_ssg")
    ym2608_ssg_hits, ym2608_ssg_offs, ym2608_ssg_warn = collect_ay8910(
        vgm, chip="ym2608_ssg", clock=ym2608_clock,
        divider=YM2608_SSG_DIVIDER,
    )
    warnings.extend(ym2608_ssg_warn)
    if (ym2608_ssg_hits or ym2608_pcm_hits) and ym2608_ids:
        ssg_at = {
            cid: ym_base[cid] + ym2608_fm_count for cid in ym2608_ids
        }
        if ym2608_ssg_hits:
            ym2608_ssg_count = _emit_ay_notes(
                events, channel_fx, ym2608_ssg_hits, ym2608_ssg_offs, ssg_at,
                ym2608_clock, YM2608_SSG_DIVIDER,
                tone_ins_base + len(scc_ins), vgm.total_samples or 0,
            )
            ay_count += ym2608_ssg_count
        _emit_pcm(ym2608_pcm, ym2608_pcm_hits, ssg_at)

    # Drop bodies no emitted note references.  A chip whose channel map is
    # absent (or a segment the writer will not place) would otherwise leave an
    # unused SMP2 block and a sample-map slot behind, which is pure file size.
    if ssg_pcm_events:
        used = sorted({idx for _ev, idx in ssg_pcm_events})
        if len(used) != len(ssg_pcm_samples):
            remap = {old: new for new, old in enumerate(used)}
            ssg_pcm_samples = [ssg_pcm_samples[i] for i in used]
            ssg_pcm_events = [(ev, remap[idx]) for ev, idx in ssg_pcm_events]
            for ev, idx in ssg_pcm_events:
                ev.note = 60 + (idx % 120)
    else:
        ssg_pcm_samples = []

    # The writer places the SSG PCM instruments right before the SCC/AY/PSG
    # tone instruments, so the tone bases move up by their count.  One
    # instrument holds up to 120 bodies through its sample map.
    n_ssg_pcm = (len(ssg_pcm_samples) + 119) // 120
    ssg_pcm_ins_base = tone_ins_base
    if n_ssg_pcm:
        tone_ins_base += n_ssg_pcm
    # NES DPCM/PCM sample instruments follow the SSG PCM group, still before
    # the SCC/AY/PSG tone list.
    nes_dpcm_ins_base = tone_ins_base
    tone_ins_base += n_nes_dpcm_ins
    nes_pcm_ins_base = tone_ins_base
    tone_ins_base += n_nes_pcm_ins
    # HuC6280 direct-D/A bodies are plain 8-bit sample instruments after the
    # NES group.
    huc6280_pcm_ins_base = tone_ins_base
    tone_ins_base += n_huc6280_pcm_ins
    # YMZ280B bodies come last among the sample instruments, just before the
    # pitched SCC/AY/NES/PCE/GB tone instruments.
    ymz_ins_base = tone_ins_base
    tone_ins_base += len(ymz_audible)

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
                found = estimate_grid(
                    vgm, tick_samples=tick, min_row=row_floor,
                    chips=selected_fm_chips, **kw,
                )
                if grid_cache is not None:
                    grid_cache[key] = found
                return found
            return grid_cache[key]

        grid = _grid("fm")
        if grid is None and pcm_onsets:
            grid = _grid("pcm", onsets=pcm_onsets, label="sample-trigger lattice")
        if grid is not None:
            tone_only = (
                scc_count or ay_count or vsu_count or saa_count
            ) and not keyon_times and not any(
                e.on and e.pcm for e in events
            )
            if tone_only:
                saa_tick = _saa_tick_interval(vgm) if saa_count else None
                if saa_tick:
                    hz = VGM_RATE / saa_tick
                    use_speed = 1
                    row_subdiv = 1.0
                    grid_note = (
                        f"SAA1099 write lattice ({saa_tick:g} samples/tick), "
                        "row = 1 write tick"
                    )
                    tone_hilight = True
                else:
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
    if k053260_audible:
        by_k053: dict[tuple[int, int], list[int]] = {}
        for smp in k053260_audible:
            for h in smp.hits:
                by_k053.setdefault((h.chip_id, h.voice), []).append(h.sample_time)
        for times in by_k053.values():
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

    # A held retune is a transpose on the OPM. The OPN/OPL paths model their
    # pitch motion with macros and ramps, so in a combined module the pass sees
    # only the eight YM2151 columns.
    legato_steps = legato_ramps = 0
    if convert_opm:
        legato_steps, legato_ramps = _classify_legato(
            events, (VGM_RATE / hz) * use_speed,
            channels=(
                None if not (is_opn or is_opn2 or is_opnb or is_opna or is_opl)
                else set(range(8))
            ),
        )

    # Group DAC runs after the row grid; insert their maps before tone instruments.
    dac_pan_events: list[tuple[int, int]] = []
    if pcm and is_opn2:
        dac_samples, dac_plays, dac_warn, dac_stats = collect_dac(
            vgm, slices=dac_slices, grid=(hz, use_speed, t0),
            lossy_samples=lossy_samples,
        )
        warnings.extend(dac_warn)
        if (
            dac_writes and not dac_plays and not dac_warn
            and not dac_stats.get("dac_disabled_bytes")
        ):
            warnings.append(
                f"{dac_writes} YM2612 DAC writes but no playable DAC data"
            )
    if dac_plays and dac_ch >= 0:
        dac_base = len(patch_order)
        (
            dac_samples, dac_notes, dac_offs, dac_pan_events,
            dac_group_warn, dac_group_stats,
        ) = group_dac_plays(
            dac_samples, dac_plays, hz=hz, speed=use_speed, t0=t0,
            total_samples=vgm.total_samples or 0,
            drop_quiet=not dac_slices,
            cluster_fragments=lossy_samples and not dac_slices,
        )
        dac_stats.update(dac_group_stats)
        warnings.extend(dac_group_warn)
        if lossy_samples and dac_group_stats.get("dac_fragment_clustered"):
            warnings.append(
                "YM2612 DAC: lossy sample clustering folded "
                f"{dac_group_stats['dac_fragment_clustered']} bodies into "
                f"{len(dac_samples)} slots"
            )
        n_dac_ins = (len(dac_samples) + 119) // 120
        if n_dac_ins:
            _rebase_non_fm_ins(events, dac_base, dac_base + n_dac_ins)
            tone_ins_base += n_dac_ins
        for placed, sample_idx, pan in dac_notes:
            no_pan = pan < 0
            events.append(NoteEvent(
                sample=placed,
                ch=dac_ch,
                on=True,
                note=60 + sample_idx % 120,
                ins=dac_base + sample_idx // 120,
                vol=127,
                e5=0x80,
                pan=0 if no_pan else pan,
                no_pan=no_pan,
                dac=True,
            ))
        for placed, _pan in dac_offs:
            events.append(NoteEvent(
                sample=placed,
                ch=dac_ch,
                on=False,
                note=180,
                ins=-1,
                vol=-1,
                e5=0x80,
                pan=0,
                dac=True,
            ))
        # Source samples can outlive a pan write; those points ride the
        # channel as 08xx mid-note instead of cutting the sample.
        for t, pan in dac_stats.get("dac_pan_points", ()):
            if pan >= 0:
                dac_pan_events.append((t, pan))
        seen_pan: set[tuple[int, int]] = set()
        for placed, pan in sorted(dac_pan_events):
            if pan < 0 or (placed, pan) in seen_pan:
                continue
            seen_pan.add((placed, pan))
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
        + (len(audible) if pcm else 0) + len(oki_samples) + n_adpcm_ins
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
        + len(k053260_audible) + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins + len(ymz_audible)
    )
    for ins, ramps in held_lfo.items():
        if not (0 <= ins < len(patch_order)):
            continue
        base_key = patch_order[ins]
        fm_pms, ams, macro_speed = _pick_lfo_macros(ramps, VGM_RATE / hz)
        if fm_pms:
            patches[base_key] = replace(
                patches[base_key], fms_macro=fm_pms, ams_macro=ams,
                macro_speed=macro_speed,
            )

    if is_combo:
        # Only the OPL side has FM notes; the YM2612's DAC events carry no
        # pitch points, so one clock is enough here.
        fm_pitch_clock = opl_clock
    else:
        fm_pitch_clock = (
            ym2610_clock if is_opnb else
            (ym2608_clock if is_opna else
             (ym2612_clock if is_opn2 else (opl_clock if is_opl else ym_clock)))
        )
    
    # The tone families bind after the FM macros. Without a reservation an
    # OPL/OPLL driver that rewrites f-num often spends every free instrument
    # on FM copies and leaves the AY/PSG notes on the plain fallback, so their
    # vibrato and decay vanish. Measure the tone demand now and keep that many
    # slots; a small emergency share stays with FM so its fast sub-row bends,
    # which cannot use row effects, still get their macros.
    tone_groups = (
        n_ssg_pcm + n_nes_dpcm_ins + n_nes_pcm_ins
        + n_huc6280_pcm_ins + n_opl4_wave_ins
    )
    tone_demand = 0
    if (
        scc_ids or ay_ids or psg_ids or nes_ids or huc6280_ids
        or gb_ids or vsu_ids or saa_ids
    ):
        tone_demand = _tone_instrument_demand(
            events, vgm.k051649_clock or SCC_DEFAULT_CLOCK, ay_note_clock,
            psg_clock, nes_clock, huc6280_clock, gb_clock, vsu_clock,
            saa_clock, hz, vgm.total_samples, use_speed,
            lfo_loop=lfo_loop,
        )
    tone_demand_raw = tone_demand
    free_slots = max(
        0, INS_TARGET - foreign_ins - len(patch_order) - tone_groups,
    )
    tone_demand = min(tone_demand, max(0, free_slots - 32))
    tone_reserve = tone_groups + tone_demand
    fm_ins_cap = max(
        len(patch_order), INS_TARGET - foreign_ins - tone_reserve,
    )
    fm_before_pitch = len(patch_order)
    opll_macro_copies = 0
    if is_opll:
        opll_macro_copies = _bind_opll_macros(
            events, patches, patch_order, channel_fx, hz, use_speed, t0,
            vgm.total_samples or 0,
            cap=fm_ins_cap,
        )
    _pms_copies, pms_lost = _bind_pms_macros(
        events, patches, patch_order, hz, vgm.total_samples or 0,
        cap=fm_ins_cap,
    )
    fm_pitch_copies, fm_pitch_lost = _bind_fm_pitch(
        events, patches, patch_order, fm_pitch_clock, hz, vgm.total_samples or 0,
        cap=fm_ins_cap,
        # OPLL keeps pitch motion in a macro. Its platform has no E8/E9
        # quick-legato path the writer can rely on, so row steps played in the
        # module but vanished on export and left a fast drum bend flat.
        allow_steps=(is_opn or is_opn2 or is_opl or is_opnb or is_opna),
        row_ticks=use_speed,
        quant=4 if is_opll else 0,
        opll=is_opll,
    )
    opm_dtmul_copies = opm_dtmul_lost = 0
    if convert_opm:
        opm_dtmul_copies, opm_dtmul_lost = _bind_opm_dtmul_macros(
            events, patches, patch_order, VGM_RATE / hz,
            t0, use_speed, cap=fm_ins_cap,
        )
    # An operator level the driver scripted while its note sounded. The volume
    # column reaches only a KVS operator. The rest travel as instrument macros.
    op_tl_copies = op_tl_lost = 0
    if is_opl:
        op_tl_copies, op_tl_lost = _bind_op_macros(
            events, patches, patch_order, VGM_RATE / hz,
            cap=fm_ins_cap,
        )
    _rebase_non_fm_ins(events, fm_before_pitch, len(patch_order))
    if len(patch_order) > fm_before_pitch:
        # Any FM macro copy (pitch, PMS/AMS, OPLL level/preset) moves the
        # sample and tone bases. Keep the DAC maps and the non-FM sample
        # blocks while recomputing.
        fm_ins_count = len(patch_order)
        if not fm_ins_count and is_opl and opl4_wave_samples:
            fm_ins_count = 1
        tone_ins_base = (
            fm_ins_count + n_dac_ins
            + (len(audible) if pcm else 0) + len(oki_samples)
            + n_adpcm_ins + len(spcm_audible) + len(c140_audible)
            + len(c352_audible) + len(k053260_audible)
            + n_ym2610_a_ins + n_ym2610_b_ins
            + n_ym2608_rhythm_ins + n_ym2608_b_ins
            + n_y8950_b_ins + n_opl4_wave_ins
        )
        ssg_pcm_ins_base = tone_ins_base
        tone_ins_base += n_ssg_pcm
        nes_dpcm_ins_base = tone_ins_base
        tone_ins_base += n_nes_dpcm_ins
        nes_pcm_ins_base = tone_ins_base
        tone_ins_base += n_nes_pcm_ins
        huc6280_pcm_ins_base = tone_ins_base
        tone_ins_base += n_huc6280_pcm_ins
        ymz_ins_base = tone_ins_base
        tone_ins_base += len(ymz_audible)
    if pms_lost:
        warnings.append(
            f"{pms_lost} YM2151 PMS/AMS ramps stayed on the base instrument: "
            "instrument limit"
        )
    if fm_pitch_lost:
        pitch_chip = (
            "YM2610" if is_opnb else
            "YM2608" if is_opna else
            "YM2612" if is_opn2 else
            {"opl": "YM3526", "opl2": "YM3812",
             "opl3": "YMF262"}.get(opl_kind, "YM2203") if is_opl else "YM2203"
        )
        warnings.append(
            f"{fm_pitch_lost} {pitch_chip} pitch sweeps "
            "encoded as row ramps: instrument limit"
        )
    if op_tl_lost:
        warnings.append(
            f"{op_tl_lost} OPL note(s) scripted a modulator level that stayed "
            "flat on the base instrument: operator macro limit"
        )
    if opm_dtmul_lost:
        warnings.append(
            f"{opm_dtmul_lost} YM2151 note(s) retuned an operator inside the "
            "note but stayed on the base instrument: operator macro limit"
        )

    # Tone-chip instruments, after the row rate is final. A macro advances
    # once per tick, so building it before condense (or before the musical
    # row is chosen) plays the driver's trajectory at the wrong speed. When
    # the raw demand would overrun the budget, let near-identical continuous
    # macros share an instrument instead of falling all the way back to a
    # plain voice. This is what keeps a large AY-heavy log inside the limit
    # without losing every decay.
    tone_merge_tol = (
        1 if tone_ins_base + tone_demand_raw > INS_TARGET else 0
    )
    tone_merged = _bind_tone_instruments(
        events, scc_ins, ay_ins, psg_ins, nes_ins, huc6280_ins, gb_ins,
        vsu_ins, saa_ins,
        tone_ins_base,
        vgm.k051649_clock or SCC_DEFAULT_CLOCK,
        ay_note_clock,
        psg_clock,
        nes_clock,
        huc6280_clock,
        gb_clock,
        vsu_clock,
        saa_clock,
        hz, vgm.total_samples, use_speed,
        merge_tol=tone_merge_tol, lfo_loop=lfo_loop,
    )
    if tone_merged:
        warnings.append(
            f"instrument limit: {tone_merged} tone note(s) shared a "
            "near-identical instrument macro (1-step tolerance)"
        )
    # SSG PCM instruments sit immediately before the tone list.
    for ev, idx in ssg_pcm_events:
        ev.ins = ssg_pcm_ins_base + idx // 120
    # NES tone instruments are bound by `_bind_tone_instruments`; the sample
    # instruments follow the SSG PCM group.
    for ev, idx in nes_dpcm_events:
        ev.ins = nes_dpcm_ins_base + idx
    for ev, idx in nes_pcm_events:
        ev.ins = nes_pcm_ins_base + idx // NES_PCM_MAP_SLOTS
    # HuC6280 direct-D/A bodies follow the NES sample group.
    for ev, idx in huc6280_pcm_events:
        ev.ins = huc6280_pcm_ins_base + idx
    # The final FM instrument count may include OPL operator-level or pitch
    # copies added after the tone bases were first computed.  The YMZ events
    # were placeholders until now, so recompute their base against the final
    # patch list rather than using the stale pre-copy value.
    ymz_ins_base = (
        len(patch_order) + n_dac_ins
        + (len(audible) if pcm else 0) + len(oki_samples)
        + n_adpcm_ins + len(spcm_audible) + len(rf5c68_audible)
        + len(rf5c164_audible) + len(c140_audible) + len(c352_audible)
        + len(k053260_audible) + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins + n_y8950_b_ins
        + n_opl4_wave_ins + n_ssg_pcm + n_nes_dpcm_ins
        + n_nes_pcm_ins + n_huc6280_pcm_ins
    )
    for ev, idx in ymz_events:
        ev.ins = ymz_ins_base + idx

    loop_sample = vgm.loop_sample
    if loop_sample is not None and loop_sample < 0:
        loop_sample = 0

    fm_list = [patches[k] for k in patch_order]
    has_sample_ins = bool(
        oki_samples or adpcm_samples or spcm_audible or c140_audible
        or c352_audible or k053260_audible
        or rf5c68_audible or rf5c164_audible
        or ym2610_a_samples or ym2610_b_samples
        or ym2608_b_samples or ym2608_rhythm_events
        or y8950_b_samples or opl4_wave_samples or ssg_pcm_samples
        or nes_ids or nes_dpcm_samples or nes_pcm_samples
        or huc_pcm_samples or ymz_audible
        or any(s.length >= 8 for s in pcm_samples)
        or scc_waves or ay_count or dac_samples or psg_count
        or saa_ids or saa_count
    )
    if not fm_list:
        chip_name = {
            KIND_OPL: "YM3526", KIND_OPL2: "YM3812", KIND_OPL3: "YMF262",
        }.get(opl_kind or "", "YM2151")
        if is_opn and ym2203_ssg_count:
            warnings.append("no YM2203 FM key-ons found; converting the SSG only")
        elif is_opn2:
            warnings.append("no YM2612 key-ons found")
        elif is_opl:
            warnings.append(f"no {chip_name} key-ons found")
        elif is_opnb:
            warnings.append("no YM2610 FM key-ons found")
        elif is_opna:
            warnings.append("no YM2608 FM key-ons found")
        elif not has_sample_ins:
            # A PSG-only (SMS/GG) or AY/SCC/NES module needs no FM key-ons;
            # the legacy YM2151 placeholder is only a channel-numbering base.
            warnings.append(
                "no YM2203 key-ons found" if is_opn
                else "no YM2151 key-ons found"
            )
        # Keep the placeholder when the module would otherwise be empty,
        # and on an OPL4 with wave samples: it keeps the wave instruments
        # 1-based (0x01 = wave 017, 0x19 = wave 385). The sample bases above
        # already reserve its slot.
        if not has_sample_ins or (is_opl and opl4_wave_samples):
            fm_list = [FMPatch(
                0 if is_opl else 7, 0, 0, 0,
                tuple(OpRegs(tl=63 if is_opl else 0) for _ in range(4)),
                name="empty", opl=is_opl,
            )]  # type: ignore[arg-type]
    # A power-of-two carrier MULT sounds an octave off the channel f-num. Label
    # the note at the loudest carrier's octave and cancel the move with a
    # constant pitch macro, leaving the source registers untouched.
    fm_before_octave = len(fm_list)
    _apply_opn_carrier_octave(
        events, fm_list,
        skip_channels=(None if is_opl else (
            {
                ym_base[cid] + 2 + col
                for cid in (ym_ids if is_opn else
                            ym2608_ids if is_opna else ym2612_ids)
                for col in range(4)
            }
            if (ym2203_ext or ym2612_ext or ym2608_ext) else None
        )),
    )
    _rebase_non_fm_ins(events, fm_before_octave, len(fm_list))

    non_fm_ins = (
        n_dac_ins
        + (len(audible) if pcm else 0) + len(oki_samples) + n_adpcm_ins
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
        + len(k053260_audible) + n_ym2610_a_ins + n_ym2610_b_ins
        + len(scc_ins) + len(ay_ins) + len(psg_ins)
        + len(vsu_ins)
        + n_ym2608_rhythm_ins + n_ym2608_b_ins
        + n_ssg_pcm + len(ymz_audible)
    )
    # Before trimming anything for the hard limit, share an instrument whose
    # macros only differ by a longer tail with its shorter copies. A short
    # note keyed off before the extra tail is reached plays exactly the same.
    _merge_fm_prefix_macros(events, fm_list, VGM_RATE / hz)

    # Absolute ceiling. The per-family caps target INS_TARGET, but a mixed
    # log or a late copy pass can still land above the final hard limit.
    # Fold only pass-created FM copies back onto their base voices; base
    # voices carry the timbre and are never dropped here.
    old_fm_count = len(fm_list)
    fm_limit = max(1, INS_HARD_LIMIT - non_fm_ins)
    dropped_copies = _trim_fm_macro_copies(events, fm_list, fm_limit)
    if dropped_copies:
        _rebase_non_fm_ins(events, old_fm_count, len(fm_list))
        warnings.append(
            f"instrument limit: folded {dropped_copies} FM macro copy/copies "
            f"back onto their base voices to stay within {INS_HARD_LIMIT} slots"
        )
    n_total_ins = len(fm_list) + non_fm_ins
    if n_total_ins > INS_HARD_LIMIT:
        warnings.append(
            f"{n_total_ins} instruments exceed the 0xFF instrument limit; "
            "the extra instruments cannot be addressed"
        )
    n_total_smp = (
        len(pcm_samples if pcm else []) + len(oki_samples) + len(adpcm_samples)
        + len(spcm_audible) + len(c140_audible) + len(c352_audible)
        + len(k053260_audible) + len(dac_samples)
        + len(ym2610_a_samples) + len(ym2610_b_samples)
        + len(ym2608_b_samples) + len(ssg_pcm_samples)
        + len(ymz_audible)
    )
    if n_total_smp > 65535:
        warnings.append(
            f"{n_total_smp} samples exceed the 16-bit sample-map index range"
        )

    if convert_opm:
        # Only the OPM collector leaves `sweep` set at this point, so the pass
        # runs whenever a YM2151 shares the module - even beside an OPL family
        # whose own pitch motion travels as macros.
        transpose_opm_events(
            events, vgm.ym2151_clock, channels=8 * max(1, len(opm_ids)),
        )

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
        legato_steps=legato_steps,
        legato_ramps=legato_ramps,
        row_subdiv=row_subdiv,
        hilight_a=hilight_a,
        hilight_b=hilight_b,
        oki_samples=oki_samples,
        msm6258_samples=adpcm_samples,
        segapcm_samples=spcm_audible,
        segapcm_ids=spcm_ids,
        rf5c68_samples=rf5c68_audible,
        rf5c164_samples=rf5c164_audible,
        c140_samples=c140_audible,
        c352_samples=c352_audible,
        k053260_samples=k053260_audible,
        ymz280b_samples=ymz_audible,
        ymz280b_ids=ymz_ids,
        ymz280b_count=ymz_count,
        channel_fx=channel_fx,
        k007232_volume=k007232_volume,
        c352_quad=c352_quad_events,
        fm_chip=(
            # The family whose channels start the module. A combined log names
            # its first system here and lists every one in `fm_systems`.
            fm_systems[0].kind if fm_systems else
            opl_kind if is_opl else
            "opll" if is_opll else
            "ym2610" if is_opnb else
            ("ym2608" if is_opna else
             ("ym2203" if is_opn else ("ym2612" if is_opn2 else "ym2151")))
        ),
        fm_systems=fm_systems,
        # A PCE/SCC/NES-only log has no FM write, so the writer drops the
        # analyzer's legacy YM2151 placeholder instead of adding an empty OPM.
        has_fm_source=(
            # A header-only VGM (no register writes) keeps the legacy YM2151
            # so the worker/batch paths still produce a silent module.
            not vgm.writes
            or any(
                w.chip in (
                    "ym2151", "ym2203", "ym2612", "ym2610", "ym2608",
                    "opll", "opl", "opl2", "opl3", "y8950", "ymf278b",
                )
                for w in vgm.writes
            )
        ),
        ym2151_ids=opm_ids,
        ym2203_ids=ym_ids,
        ym2203_ext=ym2203_ext,
        ym2203_ssg_count=ym2203_ssg_count,
        ym2612_ids=ym2612_ids,
        ym2612_ext=ym2612_ext,
        ym2612_csm=ym2612_csm,
        opl_ids=opl_ids,
        opl_drums=opl_drums,
        opll_ids=opll_ids,
        opll_drums=opll_drums,
        opll_vrc7=opll_vrc7,
        nes_ids=nes_ids,
        nes_ins=nes_ins,
        nes_dpcm_samples=nes_dpcm_samples,
        nes_pcm_samples=nes_pcm_samples,
        nes_count=len(nes_tones) + len(nes_dpcm_hits) + len(nes_pcm_hits),
        nes_pcm_count=len(nes_pcm_hits),
        huc6280_ids=huc6280_ids,
        huc6280_waves=huc6280_waves,
        huc6280_ins=huc6280_ins,
        huc6280_pcm_samples=huc_pcm_samples,
        huc6280_count=huc6280_count,
        huc6280_pcm_count=huc6280_pcm_count,
        gb_ids=gb_ids,
        gb_waves=gb_waves,
        gb_ins=gb_ins,
        gb_count=gb_count,
        vsu_ids=vsu_ids,
        vsu_waves=vsu_waves,
        vsu_mod_waves=vsu_mod_waves,
        vsu_ins=vsu_ins,
        vsu_count=vsu_count,
        vsu_dynamic=vsu_dynamic,
        saa_ids=saa_ids,
        saa_ins=saa_ins,
        saa_count=saa_count,
        fm_vol_max=_fm_vol_max(fm_systems, is_opll=is_opll, is_opl=is_opl),
        fm_notes=(
            opll_stats.get("notes", 0) if is_opll else
            opl_stats.get("notes", 0) if is_opl else 0
        ),
        drum_notes=(
            opll_stats.get("drums", 0) if is_opll else
            opl_stats.get("drums", 0) if is_opl else 0
        ),
        ym2610_ids=ym2610_ids,
        ym2610_b=ym2610_b,
        ym2610_ssg_count=ym2610_ssg_count,
        ym2610_a_samples=ym2610_a_samples,
        ym2610_b_samples=ym2610_b_samples,
        ym2610_a_count=ym2610_a_count,
        ym2610_b_count=ym2610_b_count,
        ym2608_ids=ym2608_ids,
        ym2608_ext=ym2608_ext,
        ym2608_ssg_count=ym2608_ssg_count,
        ym2608_rhythm_count=ym2608_rhythm_count,
        ym2608_rhythm_notes=len(ym2608_rhythm_events),
        ym2608_b_samples=ym2608_b_samples,
        ym2608_b_count=ym2608_b_count,
        y8950_b_samples=y8950_b_samples,
        y8950_b_count=y8950_b_count,
        opl4_wave_samples=opl4_wave_samples,
        opl4_wave_count=opl4_wave_count,
        scc_ids=scc_ids,
        scc_waves=scc_waves,
        scc_ins=scc_ins,
        scc_count=scc_count,
        ay_ids=ay_ids,
        ay_ins=ay_ins,
        ay_count=ay_count,
        ssg_pcm_samples=ssg_pcm_samples,
        ssg_pcm_count=ssg_pcm_count,
        psg_ids=psg_ids,
        psg_ins=psg_ins,
        psg_count=psg_count,
        psg_stereo=psg_stereo,
        psg_t6w28=psg_t6w28,
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
    if song.legato_steps or song.legato_ramps:
        lines.append(
            f"pitch       {song.legato_steps} legato steps, "
            f"{song.legato_ramps} single-step ramps"
        )
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
    elif song.fm_chip in ("opl", "opl2", "opl3"):
        chip_name = {"opl": "YM3526", "opl2": "YM3812",
                     "opl3": "YMF262"}[song.fm_chip]
        default = {"opl": OPL_DEFAULT_CLOCK, "opl2": OPL2_DEFAULT_CLOCK,
                   "opl3": OPL3_DEFAULT_CLOCK}[song.fm_chip]
        clock = v.opl3_clock if song.fm_chip == "opl3" else (
            v.opl2_clock if song.fm_chip == "opl2" else v.opl_clock)
        mode = " + rhythm" if song.opl_drums else ""
        pan = " (hard panned)" if getattr(v, f"{song.fm_chip}_pan", False) else ""
        four = sum(1 for p in song.fm_patches if p.four)
        lines.insert(-1, (
            f"FM chip     {chip_name}{mode} x{len(song.opl_ids)} @ "
            f"{(clock or default) / 1e6:.6g} MHz{pan}"
        ))
        if song.fm_chip == "opl3" or four:
            lines.insert(-1, (
                f"FM voices   {channel_count(song.fm_chip, song.opl_drums)} columns"
                f", {four} four-operator instrument(s)"
            ))
    elif song.fm_chip == "ym2610":
        variant = "YM2610B" if song.ym2610_b else "YM2610"
        lines.insert(-1, (
            f"FM chip     {variant} x{len(song.ym2610_ids)} @ "
            f"{(v.ym2610_clock or YM2610_DEFAULT_CLOCK) / 1e6:.6g} MHz, "
            f"{song.ym2610_ssg_count} SSG notes, "
            f"{song.ym2610_a_count} ADPCM-A, {song.ym2610_b_count} ADPCM-B"
        ))
    elif song.fm_chip == "ym2608":
        layout = " Ext" if song.ym2608_ext else ""
        lines.insert(-1, (
            f"FM chip     YM2608{layout} x{len(song.ym2608_ids)} @ "
            f"{(v.ym2608_clock or YM2608_DEFAULT_CLOCK) / 1e6:.6g} MHz, "
            f"{song.ym2608_ssg_count} SSG notes, "
            f"{song.ym2608_rhythm_count} rhythm, {song.ym2608_b_count} ADPCM-B"
        ))
    elif song.fm_chip == "ym2151":
        # The OPM was the tool's first target and needs no header to explain
        # itself, but a module that also carries an OPL family does.
        if len({s.kind for s in song.fm_systems}) > 1:
            lines.insert(-1, (
                f"FM chip     YM2151 @ "
                f"{(v.ym2151_clock or 3579545) / 1e6:.6g} MHz, eight voices"
            ))
    families = {s.kind for s in song.fm_systems}
    if len(families) > 1:
        # A merged log converts more than one family. Name each with its own
        # clock and note count, so the report says who played what.
        label = {
            "ym2151": "YM2151", "ym2203": "YM2203", "ym2612": "YM2612",
            "ym2610": "YM2610B" if song.ym2610_b else "YM2610",
            "ym2608": "YM2608", "opl": "YM3526", "opl2": "YM3812",
            "opl3": "YMF262", "y8950": "Y8950", "opl4": "YMF278B",
            "opll": "YM2413",
        }
        ranges = system_channel_ranges(song.fm_systems)
        parts: list[str] = []
        for kind in dict.fromkeys(s.kind for s in song.fm_systems):
            group = [(s, base) for s, base in ranges if s.kind == kind]
            first = group[0][0]
            name = label.get(kind, kind)
            if first.ext:
                name += " Ext"
            if first.drums:
                name += " kit"
            notes = 0
            for system, base in group:
                end = base + system.fm_channels
                notes += sum(
                    1 for e in song.events
                    if e.on and not e.pcm and not e.dac and not e.tone_chip
                    and base <= e.ch < end
                )
            clock = first.clock or 0
            parts.append(
                f"{name} x{len(group)} @ {clock / 1e6:.6g} MHz, {notes} notes"
            )
        lines.insert(-1, "FM systems  " + "; ".join(parts))

    for i, p in enumerate(song.fm_patches):
        lfo = ""
        if p.fms_macro:
            lfo = f"  FMS {list(p.fms_macro)} @{p.macro_speed}t"
        # An operator level the driver scripted while notes of this voice sounded.
        ops = ""
        entries = getattr(p, "op_tl_macro", ()) or ()
        if entries:
            ops = "  " + ", ".join(
                f"op{oi + 1} $4n x{len(vals)}@{spd}t"
                for oi, vals, spd in entries)
        lines.append(
            f"  {i:02d}  alg {p.alg} fb {p.fb}  uses {p.uses:4d}  "
            f"carrierTL {p.carrier_tl():02X}  {p.name}{lfo}{ops}"
        )
    if song.fm_chip == "ym2612" and song.fm_patches:
        lines.append("  (carrier TL is relative; the key-on level rides the volume column)")
    elif families & {"opl", "opl2", "opl3", "opll"} and song.fm_patches:
        # In a combined module this says what the OPL columns do; the arcade
        # family beside them keeps its carrier levels relative instead.
        lines.append("  (carrier TL rides the volume column; a modulator level "
                     "the log moved mid-note travels as an operator macro; "
                     "a kit column shares one voice)")
    if song.chip_fx:
        codes = (0x10, 0x17, 0x18, 0x1E, 0x1F)
        lfo_n = sum(1 for entry in song.chip_fx if entry[1] in codes)
        if song.fm_chip in ("ym2612", "ym2610"):
            lines.append(f"LFO fx      {lfo_n} chip writes (10xx)")
        elif song.fm_chip in ("opl", "opl2", "opl3"):
            lines.append(
                f"chip fx     {lfo_n} writes (0xBD depth and rhythm bits)")
        else:
            lines.append(f"LFO fx      {lfo_n} chip writes (17/18/1E/1F)")
    if len(families) > 1:
        # Two families share the module, so count every pitch column at its own
        # base instead of assuming the FM block starts at channel 0. The PSG,
        # SCC and sample-chip notes carry their own markers.
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and not e.dac and not e.tone_chip
        )
    elif song.fm_chip == "ym2203":
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
    elif song.fm_chip in ("opl", "opl2", "opl3"):
        # The analyzer has already told the kit from the voices: rhythm columns
        # share instruments and frequency registers, and they are counted apart.
        fm_ons = song.fm_notes
    elif song.fm_chip == "ym2610":
        fm_end = (6 if song.ym2610_b else 4) * len(song.ym2610_ids)
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and not e.dac and 0 <= e.ch < fm_end
        )
    else:
        fm_end = 8 * max(1, len(getattr(song, "ym2151_ids", ()) or (1,)))
        fm_ons = sum(
            1 for e in song.events
            if e.on and not e.pcm and not e.dac and 0 <= e.ch < fm_end
        )
    pcm_ons = sum(1 for e in song.events if e.on and e.pcm)
    kit = (f"  (+{song.drum_notes} kit hits)"
           if families & {"opl", "opl2", "opl3", "opll"} and song.drum_notes
           else "")
    lines.append(f"FM notes    {fm_ons}{kit}")
    if song.psg_ids:
        label = "T6W28" if getattr(song, "psg_t6w28", False) else "SN76489"
        lines.append(
            f"{label:<12}{song.psg_count} notes  "
            f"({len(song.psg_ins)} instruments, "
            f"{song.vgm.sn76489_clock or PSG_DEFAULT_CLOCK} Hz)"
        )
    if song.dac_count:
        data = sum(len(s.data) for s in song.dac_samples)
        maps = (len(song.dac_samples) + 119) // 120
        source = song.dac_stats.get("dac_source_bytes", 0)
        mode = song.dac_stats.get("dac_mode", "slices")
        if mode == "source":
            shape = f"{len(song.dac_samples)} bank samples"
            extra = (
                f", {song.dac_stats.get('dac_source_runs', 0)} bank runs"
                f", {song.dac_stats.get('dac_source_runs_reused', 0)} reused"
            )
        elif mode == "attacks":
            shape = f"{len(song.dac_samples)} PCM hits"
            extra = (
                f", {song.dac_stats.get('dac_attack_segments', 0)} attacks"
                f", {song.dac_stats.get('dac_attack_prefix_reused', 0)} "
                "prefix-shared"
            )
        elif mode == "chunks":
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
        len(song.pcm_samples) + len(song.segapcm_samples)
        + len(song.rf5c68_samples) + len(song.rf5c164_samples)
        + len(song.c140_samples)
        + len(song.c352_samples) + len(song.k053260_samples)
        + len(song.ym2610_a_samples) + len(song.ym2610_b_samples)
        + len(song.ymz280b_samples)
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
    if song.saa_ids:
        lines.append(
            f"SAA notes   {song.saa_count}  ({len(song.saa_ids)} chip"
            f"{'s' if len(song.saa_ids) > 1 else ''}, "
            f"{song.vgm.saa1099_clock or SAA_DEFAULT_CLOCK} Hz, "
            f"{len(song.saa_ins)} instruments)"
        )
    if song.huc6280_ids or song.huc6280_pcm_samples:
        lines.append(
            f"PCE notes   {song.huc6280_count}  ({len(song.huc6280_ids)} chip"
            f"{'s' if len(song.huc6280_ids) > 1 else ''}, "
            f"{len(song.huc6280_waves)} waveforms, "
            f"{song.vgm.huc6280_clock or 3579545} Hz)"
        )
    if song.huc6280_pcm_samples:
        lines.append(
            f"PCE PCM     {song.huc6280_pcm_count} note(s) in "
            f"{len(song.huc6280_pcm_samples)} 8-bit sample(s)"
        )
    if song.gb_ids:
        sw = sum(1 for i in song.gb_ins if i.gb_soft_env)
        lines.append(
            f"GB notes    {song.gb_count}  ({len(song.gb_ids)} chip"
            f"{'s' if len(song.gb_ids) > 1 else ''}, "
            f"{len(song.gb_waves)} waveforms, {len(song.gb_ins)} instruments"
            f"{f', {sw} soft-envelope' if sw else ''}, "
            f"{song.vgm.gb_dmg_clock or 4194304} Hz)"
        )
    if song.vsu_ids:
        lines.append(
            f"VSU notes   {song.vsu_count}  ({len(song.vsu_ids)} chip"
            f"{'s' if len(song.vsu_ids) > 1 else ''}, "
            f"{len(song.vsu_waves)} waveforms, {len(song.vsu_ins)} instruments"
            f"{', dynamic waves' if song.vsu_dynamic else ''}, "
            f"{song.vgm.vsu_clock or VSU_DEFAULT_CLOCK} Hz)"
        )
    if song.ssg_pcm_samples:
        lines.append(
            f"SSG PCM     {song.ssg_pcm_count} note(s) in "
            f"{len(song.ssg_pcm_samples)} 8-bit sample(s)"
        )
    if song.nes_pcm_samples:
        lines.append(
            f"NES PCM     {song.nes_pcm_count} note(s) in "
            f"{len(song.nes_pcm_samples)} 8-bit sample(s)"
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
        ("RF5C68", song.rf5c68_samples),
        ("RF5C164", song.rf5c164_samples),
        ("C140", song.c140_samples),
        ("C352", song.c352_samples),
        ("K053260", song.k053260_samples),
        ("YMZ280B", song.ymz280b_samples),
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
