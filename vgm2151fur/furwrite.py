"""Write a Furnace .fur (format 157): INFO + ADIR + INS2 + SMP2 + PATN.

Note 0 = C-(-5), 108 = C-4. The writer returns a raw module. `compress_fur`
and `write_fur(..., compress=True)` add the zlib container Furnace accepts.
"""

from __future__ import annotations

import math
import struct
import zlib
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace

from vgm2151fur.analyze import Song, ToneIns
from vgm2151fur.fm import CARRIERS, OPN_CARRIERS
from vgm2151fur.c140 import c140_furnace_clock
from vgm2151fur.c352 import C352_DEFAULT_CLOCK
from vgm2151fur.oki import msm6258_flag_text, oki_flag_text, oki_needs_banked
from vgm2151fur.pcm import pcm7_to_s8, scale_pcm7
from vgm2151fur.pitch import FURNACE_CLOCK_DEFAULT, module_clock

# Furnace arcade.cpp: rWrite(DT, dtTable[op.dt]<<4). Inverse of
# dtTable = (7, 6, 5, 0, 1, 2, 3, 4) so YM2151 register DT survives the
# second mapping. Raw DT in the file is heard as the wrong detune.
YM_DT_TO_FURNACE = (3, 4, 5, 6, 7, 2, 1, 0)
# OPN DT1 has the same positive 0..3 / negative -1..-3 encoding, but 4 is
# "negative zero" and must behave as 0.  Furnace's dt index 7 maps to raw 4,
# which is positive one step, so use a separate table for OPN.
OPN_DT_TO_FURNACE = (3, 4, 5, 6, 3, 2, 1, 0)


def ym_dt_to_furnace(ym_dt: int) -> int:
    return YM_DT_TO_FURNACE[ym_dt & 7]


def opn_dt_to_furnace(ym_dt: int) -> int:
    return OPN_DT_TO_FURNACE[ym_dt & 7]


def pack_dt_mul(dt_mul: int, opn: bool = False) -> int:
    """YM2151/YM2612 0x40 or 0x30 register → Furnace DT/MULT byte."""
    dt = (dt_mul >> 4) & 7
    index = opn_dt_to_furnace(dt) if opn else ym_dt_to_furnace(dt)
    return (index << 4) | (dt_mul & 0x0F)


FUR_MAGIC = b"-Furnace module-"
FUR_VERSION = 157  # PATN notes; INFO still the old block (not INF2)
CHIP_YM2151 = 0x82
# Furnace's YM2203 (OPN): three FM channels followed by the AY-compatible
# SSG's three voices, six channels per instance.
CHIP_YM2203 = 0x8D
# Extended channel 3: FM1, FM2, the four operators of FM3, then PSG1-3.
CHIP_YM2203_EXT = 0xB6
# Furnace's YM2612 (OPN2): six FM channels per instance; channel 6 can be a
# sample player.  The Ext layout splits channel 3 into four operator columns
# (9 channels).  The CSM layout adds a timer channel (10 channels).
CHIP_YM2612 = 0x83
CHIP_YM2612_EXT = 0xA0
CHIP_YM2612_CSM = 0xC1
# Furnace's YM2610 (4 FM + 3 SSG + 6 ADPCM-A + 1 ADPCM-B = 14 channels) and
# YM2610B (6 FM, 16 channels).
CHIP_YM2610 = 0xA5
CHIP_YM2610B = 0x9E
# TI SN76489 (the SMS platform): three square voices then noise.  A Genesis
# module carries it as a second system after the YM2612.
CHIP_SMS = 0x03
CHIP_MSM6295 = 0xAA
CHIP_MSM6258 = 0xAB
CHIP_K007232 = 0xC6
CHIP_SEGAPCM = 0x9B
CHIP_C140 = 0xCE
CHIP_K053260 = 0xCC
# Furnace format.md reserves 0xD0 for Namco C352, 32 channels, and marks it
# unavailable. 0.6.8.3 has no C352 core and no C352 instrument type.
CHIP_C352 = 0xD0
# Konami SCC (K051649, voices 3/4 share wave RAM) and SCC+ (K052539,
# independent voice 4 waveform). AY-8910 is the three-channel PSG.
CHIP_K051649 = 0xA1
CHIP_K052539 = 0xB4
CHIP_AY8910 = 0x80
INS_OPM = 33
# DIV_INS_FM: the generic OPN instrument used by the YM2203.  Furnace reads
# its 0x70/0x90 fields from the same INS2 "FM" feature.
INS_FM = 1
INS_MSM6258 = 35
INS_MSM6295 = 36
INS_ADPCMA = 37
INS_ADPCMB = 38
INS_SEGAPCM = 39
INS_K007232 = 45
INS_K053260 = 50
INS_C140 = 53
# DIV_INS_AMIGA. C140 channels already accept this sample type, so a future
# C352 core copied from c140.cpp can play the bodies. There is no DIV_INS_C352.
INS_C352 = 4
# DIV_INS_AY (volume/noise/envelope macros) and DIV_INS_SCC (32-byte wavetable
# through the wave synth).
INS_AY = 6
INS_STD = 0
INS_SCC = 18
SMP_DEPTH_ADPCM_K = 7
SMP_DEPTH_ADPCM_A = 5
SMP_DEPTH_ADPCM_B = 6
SMP_DEPTH_8BIT = 8
SMP_DEPTH_16BIT = 16
SMP_DEPTH_VOX = 10
# Furnace's DAC rate is calcFreq(C-4) * sampleRate / getCenterRate(); the
# engine's C-4 reference is a hair sharp, so a 22050 Hz sample exports as
# 22073.  Compensate at write time so the DAC clock matches the source.
DAC_RATE_COMPENSATION = 22050.0 / 22073.0
# Furnace keeps oldCenterRate=true (8363 Hz), while the linear-pitch C-4
# reference is 440 * 2^(4.25) = 8372.  ADPCM-B's rate is derived through
# calcFreq, so its stored centerRate needs the 8372/8363 correction.
ADPCM_B_RATE_COMPENSATION = 8363.0 / 8372.0
# Furnace system defaults used to decide when to send customClock.
SEGAPCM_CLOCK_X = 4000000
SEGAPCM_CLOCK_Y = 4026987
C140_FURNACE_DEFAULT_CLOCK = 8192000
NOTE_OFF = 180
EMPTY = 0xFFFF
# Furnace's pattern length ceiling (engine `defines.h` DIV_MAX_ROWS) and the
# empty rows a pattern keeps past its last content row.
PAT_LEN_MAX = 256
PAT_TAIL_ROWS = 10
# Chip output levels follow libvgm's VGMPlayer: 8.8 fixed-point defaults with
# 0x100 = YM2151 unity, plus extra-header mix entries (bit 15 = relative).
# SN76489 defaults to 0.500. Absolute extra-header entries override.
_DEV_DEFAULT_VOLUME = {
    0x00: 0x80,
    0x03: 0x100, 0x04: 0x180, 0x08: 0x100, 0x12: 0x100, 0x16: 0x11E,
    0x18: 0x100, 0x19: 0xA0, 0x1C: 0x100, 0x1D: 0x100, 0x27: 0x40,
}
_DEV_CORE_PATCH = {
    0x19: lambda v: v * 8 // 5,  # K051649 SCC core scale
    0x1C: lambda v: (v * 2 + 1) // 3,  # C140/C219 core scale
}
# Furnace core output levels relative to the player's, from render comparisons
# (measured on two-chip modules: the player's overall-volume normalizer scales
# a lone chip, and Furnace has no such step).
_FURNACE_VOLUME_SCALE = {
    0x00: 1.0, 0x03: 1.0, 0x04: 1.0, 0x12: 1.46, 0x16: 0.61, 0x18: 1.0,
    0x1C: 0.7, 0x19: 1.04,
    # MAME shifts the voice product by 6 bits, the Furnace core by 7, so the
    # module needs twice the player's system volume to reach the same level.
    0x1D: 2.0,
}
# MSM6258's player default is 0x11E; its Furnace core is 1.64x louder, so
# 0.61 lands the sample at the source level. The MSM6295 (0x18) is unity.
_CHIP_DEV = {CHIP_YM2151: 0x03, CHIP_SEGAPCM: 0x04, CHIP_MSM6295: 0x18,
             CHIP_MSM6258: 0x16, CHIP_C140: 0x1C, CHIP_C352: 0x27,
             CHIP_K053260: 0x1D,
             CHIP_SMS: 0x00, CHIP_K051649: 0x19, CHIP_K052539: 0x19,
             CHIP_AY8910: 0x12,
             CHIP_YM2610: 0x08, CHIP_YM2610B: 0x08}


def _player_chip_volume(vgm, dev: int, instances: int) -> float:
    """The player's mix level for one chip, the YM2151 default being 1.0."""
    vol = _DEV_DEFAULT_VOLUME[dev]
    if instances > 1:
        vol //= instances
    entry = vgm.extra_volumes.get(dev)
    if entry is not None:
        _flags, value = entry
        if value & 0x8000:
            vol = vol * (value & 0x7FFF) // 0x100
        else:
            vol = value
    patch = _DEV_CORE_PATCH.get(dev)
    if patch is not None:
        vol = patch(vol)
    return vol / 256.0


def _sample_chip_volume(song: "Song", chip: int, instances: int) -> float:
    """Output volume for a .fur chip, calibrated to the reference player."""
    dev = _CHIP_DEV.get(chip)
    if dev is None:
        return 1.0
    if chip == CHIP_MSM6295 and song.vgm.msm6295_dual:
        # The player halves every instance when the header carries two,
        # even on a track that only writes to one of them.
        instances = max(instances, 2)
    scale = _FURNACE_VOLUME_SCALE.get(dev, 1.0)
    return scale * _player_chip_volume(song.vgm, dev, instances)


def _version() -> str:
    """Package version, stamped into every .fur comment (stale-file check)."""
    from vgm2151fur import __version__

    return __version__


class Buf:
    def __init__(self) -> None:
        self.b = bytearray()

    def __len__(self) -> int:
        return len(self.b)

    def raw(self, data: bytes) -> None:
        self.b.extend(data)

    def u8(self, v: int) -> None:
        self.b.append(v & 0xFF)

    def u16(self, v: int) -> None:
        self.b.extend(int(v & 0xFFFF).to_bytes(2, "little"))

    def s16(self, v: int) -> None:
        self.b.extend(int(v).to_bytes(2, "little", signed=True))

    def u32(self, v: int) -> None:
        self.b.extend(int(v & 0xFFFFFFFF).to_bytes(4, "little"))

    def s32(self, v: int) -> None:
        self.b.extend(int(v).to_bytes(4, "little", signed=True))

    def f32(self, v: float) -> None:
        self.b.extend(struct.pack("<f", float(v)))

    def strz(self, s: str) -> None:
        self.b.extend((s or "").encode("utf-8", "replace") + b"\x00")

    def pad(self, n: int, val: int = 0) -> None:
        self.b.extend(bytes([val]) * n)

    def patch_u32(self, off: int, v: int) -> None:
        self.b[off:off + 4] = int(v & 0xFFFFFFFF).to_bytes(4, "little")

    def begin_block(self, magic: bytes) -> tuple[int, int]:
        self.raw(magic[:4])
        size_off = len(self.b)
        self.u32(0)
        return size_off, len(self.b)

    def end_block(self, size_off: int, start: int) -> None:
        self.patch_u32(size_off, len(self.b) - start)


def _write_fm_feature(b: Buf, patch) -> None:
    """INS2 'FM' feature.

    Operator order matches YM2151/YM2203 register order and Furnace's
    internal order.
    """
    b.raw(b"FM")
    size_off = len(b.b)
    b.u16(0)
    start = len(b.b)
    flags = 4 | 0x10 | 0x20 | 0x40 | 0x80
    b.u8(flags)
    b.u8(((patch.alg & 7) << 4) | (patch.fb & 7))
    b.u8(((patch.ams & 3) << 3) | (patch.pms & 7))
    b.u8(0x20)  # 4-op bit; version < 224 has no Block byte
    opn = bool(getattr(patch, "opn", False))
    carriers = set((OPN_CARRIERS if opn else CARRIERS).get(patch.alg & 7, (3,)))
    for idx in range(4):
        op = patch.ops[idx]
        # Keep the logged TL. At vol 127 Furnace uses TL as written. OPN sets
        # it on every operator like the reference converter.
        tl = op.tl & 0x7F
        kvs = 2 if (opn or idx in carriers) else 0
        b.u8(pack_dt_mul(op.dt_mul, opn=opn))
        b.u8(tl)
        b.u8(op.ks_ar & 0xDF)
        b.u8(op.am_d1r)
        if opn:
            # 0x70 is the 5-bit sustain rate (SR) and 0x90 is SSG-EG.
            b.u8(((kvs & 3) << 5) | (op.sr & 0x1F))
            b.u8(op.d1l_rr)
            b.u8(op.ssg_eg & 0x0F)
            b.u8(0)
        else:
            b.u8(((kvs & 3) << 5) | (op.dt2_d2r & 0x1F))
            b.u8(op.d1l_rr)
            b.u8(0)
            dt2 = (op.dt2_d2r >> 6) & 3
            b.u8((dt2 & 3) << 3)
    b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")


def _write_macro(
    b: Buf,
    code: int,
    values: tuple[int, ...] | list[int],
    *,
    speed: int = 1,
    loop: int = 255,
    release: int = 255,
    delay: int = 0,
    width: int = 0,
) -> None:
    """One macro inside a ``MA`` feature. `width` picks the value encoding the
    format calls wordSize: 0 = 8-bit unsigned, 1 = 8-bit signed, 2 = 16-bit
    signed, 3 = 32-bit."""
    if not values:
        return
    values = values[:255]  # the length field is one byte; more would desync
    b.u8(code)
    b.u8(len(values) & 0xFF)
    b.u8(loop & 0xFF)
    b.u8(release & 0xFF)
    b.u8(0)  # mode: sequence
    b.u8(1 | (width << 6))  # open, 8-bit unsigned
    b.u8(delay & 0xFF)
    b.u8(max(1, speed) & 0xFF)
    if width == 0:
        for v in values:
            b.u8(v & 0xFF)
    elif width == 1:
        for v in values:
            b.u8(v & 0xFF)
    elif width == 2:
        for v in values:
            b.u16(v & 0xFFFF)
    else:
        for v in values:
            b.u32(v & 0xFFFFFFFF)


def _write_ma(b: Buf, patch=None) -> None:
    b.raw(b"MA")
    size_off = len(b.b)
    b.u16(0)
    start = len(b.b)
    b.u16(8)  # macro header length (Furnace always writes 8)
    if patch is not None:
        speed = getattr(patch, "macro_speed", 1) or 1
        fms = getattr(patch, "fms_macro", ()) or ()
        ams = getattr(patch, "ams_macro", ()) or ()
        # 10 = FMS/PMS, 11 = AMS. Loop 255 = no loop (hold last).
        _write_macro(b, 10, fms, speed=speed)
        _write_macro(b, 11, ams, speed=speed)
        # 4 = pitch, added to the note in 1/128-semitone units before the OPN
        # f-num conversion (see docs/ym2203.md).
        pitch = getattr(patch, "pitch_macro", ()) or ()
        if pitch:
            _write_macro(
                b, 4, pitch,
                speed=getattr(patch, "pitch_speed", 1) or 1,
                loop=getattr(patch, "pitch_loop", 255),
                width=2,
            )
    b.u8(255)  # stop
    b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")


def _write_ins_opm(b: Buf, name: str, patch) -> None:
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_FM if getattr(patch, "opn", False) else INS_OPM)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_fm_feature(b, patch)
    _write_ma(b, patch)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_sample_map(
    b: Buf, name: str, first_sample: int, maps: list[int],
    ins_type: int = INS_C352,
) -> None:
    """120 note slots, one sample per slot.

    Each slot stores freq 48, which the reader adds to note 60. Every slice
    plays at C-4 and uses the sample's own rate. `first_sample` stays 0:
    Furnace 0.6.8.3 reads a nonzero base as an embedded sample count.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(ins_type)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    b.raw(b"SM")
    sm_size = len(b.b)
    b.u16(0)
    sm_start = len(b.b)
    b.u16(first_sample)
    b.u8(1)   # use the note map
    b.u8(31)  # wave length field (unused for samples)
    for slot in range(120):
        b.u16(48)  # note 60 + 48 = C-4 in the pre-246 encoding
        b.u16(maps[slot] if slot < len(maps) else -1)
    b.b[sm_size:sm_size + 2] = int(len(b.b) - sm_start).to_bytes(2, "little")
    _write_ma(b)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_pcm(b: Buf, name: str, sample_index: int, ins_type: int = INS_K007232) -> None:
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(ins_type)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    b.raw(b"SM")
    sm_size = len(b.b)
    b.u16(0)
    sm_start = len(b.b)
    b.u16(sample_index)
    b.u8(0x02)  # use sample
    b.u8(0)
    b.b[sm_size:sm_size + 2] = int(len(b.b) - sm_start).to_bytes(2, "little")
    _write_ma(b)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _wave_label(wave: bytes) -> str:
    """Short stable id for an SCC waveform, for instrument names."""
    h = 0
    for val in wave:
        h = (h * 31 + val) & 0xFFFF
    return f"{h:04x}"


def _write_ins_scc(b: Buf, name: str, ins) -> None:
    """DIV_INS_SCC: the note starts on wavetable `ins.wave`.

    `ins.waves` and `ins.pitch` carry the driver's per-tick wave and pitch
    motion. The platform reads them as `std.wave` and `std.pitch`.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_SCC)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    b.raw(b"WS")
    ws_size = len(b.b)
    b.u16(0)
    ws_start = len(b.b)
    b.u32(ins.wave)  # wave1
    b.u32(ins.wave)  # wave2, only used by the dual-wave effects
    b.u8(1)  # rateDivider
    b.u8(0)  # effect: none
    b.u8(1)  # enabled
    b.u8(0)  # global: restart the wave on every note
    b.u8(0)  # speed
    b.u8(0)  # param1
    b.u8(0)  # param2
    b.u8(0)  # param3
    b.u8(0)  # param4
    b.b[ws_size:ws_size + 2] = int(len(b.b) - ws_start).to_bytes(2, "little")
    _write_tone_macros(b, ins, wave=True)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_tone_macros(b: Buf, ins, *, wave: bool, reset: bool = False) -> None:
    """MA feature for an SCC/AY instrument.

    File codes: 0 volume, 2 duty, 3 wave, 4 pitch, 6 ex2, 14 phase reset,
    16 ex5. Volume is the register level. The AY column is 15.
    """
    moving = (
        ins.vol or ins.pitch or (wave and ins.waves) or ins.noise or ins.mode
        or ins.env_shape or ins.env_period or reset
    )
    if not moving:
        _write_ma(b)
        return
    b.raw(b"MA")
    ma_size = len(b.b)
    b.u16(0)
    ma_start = len(b.b)
    b.u16(8)  # macro header length (Furnace always writes 8)
    if ins.vol:
        _write_macro(
            b, 0, ins.vol, speed=ins.vol_speed, loop=ins.vol_loop,
        )
    if ins.noise:
        # ay.cpp writes register 6 as ``31 - duty``. The values are already
        # in that effect encoding (the same numbers 21xx uses).
        _write_macro(
            b, 2, ins.noise, speed=ins.noise_speed, loop=ins.noise_loop,
        )
    if wave and ins.waves:
        _write_macro(
            b, 3, ins.waves, speed=ins.wave_speed, loop=ins.wave_loop,
            width=2 if max(ins.waves) > 127 else 1,
        )
    if ins.mode:
        # Raw mode bits (tone 1, noise 2, envelope 4). The pattern effect
        # 20xx stores the same bits minus one; the macro does not.
        _write_macro(
            b, 3, ins.mode, speed=ins.mode_speed, loop=ins.mode_loop,
        )
    if ins.pitch:
        _write_macro(
            b, 4, ins.pitch, speed=ins.pitch_speed, loop=ins.pitch_loop, width=2,
        )
    if ins.env_shape:
        _write_macro(
            b, 6, ins.env_shape, speed=ins.env_shape_speed, loop=ins.env_shape_loop,
        )
    if ins.env_period:
        width = 2 if max(ins.env_period) <= 32767 else 3
        _write_macro(
            b, 16, ins.env_period, speed=ins.env_period_speed,
            loop=ins.env_period_loop, width=width,
        )
    if reset:
        # The AY restarts its envelope only on a shape write, and Furnace skips
        # same-value writes. The phase reset clears that cache.
        _write_macro(b, 14, [1, 0], speed=1)
    b.u8(255)  # stop
    b.b[ma_size:ma_size + 2] = int(len(b.b) - ma_start).to_bytes(2, "little")


def _write_ins_psg(b: Buf, name: str, ins=None) -> None:
    """DIV_INS_STD for the SMS/SN76489: only the pitch macro is needed.

    The 4-bit volume is the pattern column, and noise mode rides on the 20xy
    effect, so a no-motion note gets a plain instrument.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_STD)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_tone_macros(b, ins or ToneIns(), wave=False)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_ay(b: Buf, name: str, ins=None) -> None:
    """DIV_INS_AY with a phase-reset macro and an optional pitch macro.

    Channel mode, noise and envelope are pattern effects, and one instrument
    covers every voice. The phase reset clears the platform's write cache,
    and the next 22xy restarts the envelope.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_AY)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    if ins is None:
        ins = ToneIns()
    _write_tone_macros(b, ins, wave=False, reset=True)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_wave(b: Buf, name: str, data: bytes) -> int:
    """DivWavetable::putWaveData: the 32-byte waveform an SCC instrument plays."""
    ptr = len(b)
    size_off, start = b.begin_block(b"WAVE")
    b.strz(name)
    b.u32(len(data))
    b.u32(0)  # min
    b.u32(255)  # max
    for val in data:
        b.u32(val)
    b.end_block(size_off, start)
    return ptr


def _write_adir_list(b: Buf, ptrs: list[int]) -> int:
    """An ADIR asset directory over the given block pointers (0 = empty)."""
    ptr = len(b)
    size_off, start = b.begin_block(b"ADIR")
    b.u32(len(ptrs))
    for p in ptrs:
        b.u32(p)
    b.end_block(size_off, start)
    return ptr


def _pcm_label(smp, index: int) -> str:
    return f"smp_{index:02d}_{smp.start:05x}"


def _write_sample(b: Buf, name: str, pcm7: bytes, c4_rate: int, volume_scale: float = 1.0) -> None:
    data = pcm7_to_s8(scale_pcm7(pcm7, volume_scale))
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(len(data))
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(SMP_DEPTH_8BIT)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    b.s32(-1)
    b.s32(-1)
    # 4 banks × 32 chip bits. Zero means "do not upload to this chip": Furnace
    # then never copies the sample into K007232 RAM. Song playback stays silent
    # even though the sample editor can still preview the raw PCM.
    for _ in range(4):
        b.u32(0xFFFFFFFF)
    b.raw(data)
    b.end_block(size_off, start)


def _write_vox(
    b: Buf, name: str, data: bytes, c4_rate: int, *,
    nibble_count: bool = False, swap_nibbles: bool = False,
) -> None:
    """VOX ADPCM. MSM6295 stores a byte count; MSM6258 stores a nibble count.

    Furnace sets lengthVOX to (samples+1)/2 and plays that many bytes, and
    its MSM6258 core swaps nibbles again before okim6258::data_w (low nibble
    first). A nibble count plus one swap makes the chip see the VGM byte.
    MSM6295 copies dataVOX into ROM with no swap, so it keeps the byte count.
    """
    stored = data
    if swap_nibbles:
        stored = bytes((((x & 0x0F) << 4) | (x >> 4)) & 0xFF for x in data)
    count = len(stored) * 2 if nibble_count else len(stored)
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(count)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(SMP_DEPTH_VOX)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    b.s32(-1)
    b.s32(-1)
    for _ in range(4):
        b.u32(0xFFFFFFFF)
    b.raw(stored)
    b.end_block(size_off, start)


def _write_sample_s8(
    b: Buf,
    name: str,
    data: bytes,
    c4_rate: int,
    loop: tuple[int | None, int | None] = (None, None),
    depth: int = SMP_DEPTH_8BIT,
    loop_mode: int = 0,
) -> None:
    """Signed PCM with optional loop points. Depth 16 is int16 LE (mulaw).

    loop_mode is the SMP2 byte: 0 forward, 1 backward, 2 ping-pong.
    """
    width = 2 if depth == SMP_DEPTH_16BIT else 1
    n_frames = len(data) // width
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(n_frames)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(depth)
    b.u8(loop_mode & 0xFF)
    b.u8(0)
    b.u8(0)
    if (
        loop[0] is not None and loop[1] is not None
        and 0 <= loop[0] < loop[1] <= n_frames
    ):
        b.s32(loop[0])
        b.s32(loop[1])
    else:
        b.s32(-1)
        b.s32(-1)
    for _ in range(4):
        b.u32(0xFFFFFFFF)
    b.raw(data[:n_frames * width])
    b.end_block(size_off, start)


def _write_sample_k(
    b: Buf, name: str, data: bytes, c4_rate: int, loop: bool,
) -> None:
    """K053260 KADPCM. SMP2 stores a nibble count; two nibbles per byte."""
    nibbles = len(data) * 2
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(nibbles)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(SMP_DEPTH_ADPCM_K)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    b.s32(0 if loop else -1)
    b.s32(nibbles if loop else -1)
    for _ in range(4):
        b.u32(0xFFFFFFFF)
    b.raw(data)
    b.end_block(size_off, start)


def _write_sample_adpcm(
    b: Buf,
    name: str,
    data: bytes,
    c4_rate: int,
    depth: int,
    render_masks: tuple[int, int, int, int],
    loop: bool = False,
) -> None:
    """YM2610 ADPCM-A/B body.

    The file's sample count is nibbles; the loader allocates
    ``((count+1)/2 + 255) & ~0xff`` bytes, so the body is already padded to
    the 256-byte boundary the chip addresses.  render_masks selects the
    sample memory bank: ADPCM-A is bank 0, ADPCM-B bank 1.
    """
    samples = len(data) * 2
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(samples)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(depth)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    if loop:
        b.s32(0)
        b.s32(samples)
    else:
        b.s32(-1)
        b.s32(-1)
    for mask in render_masks:
        b.u32(mask)
    b.raw(data)
    b.end_block(size_off, start)


def segapcm_flag_text(clock: int, rom_size: int) -> str:
    """SegaPCM system flags: clock select / custom clock and ROM size."""
    parts: list[str] = []
    clk = clock or SEGAPCM_CLOCK_X
    if abs(clk - SEGAPCM_CLOCK_X) <= 4000:
        parts.append("clockSel=0")
    elif abs(clk - SEGAPCM_CLOCK_Y) <= 4000:
        parts.append("clockSel=1")
    else:
        parts.append(f"customClock={clk}")
    # 0 = 2 MB (Y board), 1 = 512 KB (the rest). Furnace addresses the ROM in
    # 8-bit units and clips to this size.
    parts.append(f"memSize={0 if rom_size > 0x80000 else 1}")
    return "\n".join(parts)


def c140_flag_text(clock: int, c140_type: int) -> str:
    """C140 system flags: bank type and Furnace-notation clock.

    12288000 becomes the 8192000 default and needs no custom clock.
    """
    parts = [f"bankType={1 if c140_type == 1 else 0}"]
    clk = c140_furnace_clock(clock)
    if clk != C140_FURNACE_DEFAULT_CLOCK:
        parts.append(f"customClock={clk}")
    return "\n".join(parts)


def ay8910_flag_text(clock: int, chip_type: int) -> str:
    """AY-8910 system flags: chip type and clock.

    YM2149 family types 0x10/0x11 select Furnace's YM2149 core. Anything
    but COLOR_NTSC/2 is written as a custom clock.
    """
    parts = [f"chipType={1 if chip_type in (0x10, 0x11) else 0}"]
    if module_clock(clock) != FURNACE_CLOCK_DEFAULT:
        parts.append(f"customClock={clock}")
    return "\n".join(parts)


def k051649_flag_text(clock: int) -> str:
    """SCC custom clock, or "" for Furnace's COLOR_NTSC/2 default."""
    if module_clock(clock) != FURNACE_CLOCK_DEFAULT:
        return f"customClock={clock}"
    return ""


def k053260_flag_text(clock: int) -> str:
    """K053260 clock select: 3.579545 MHz or 4 MHz, otherwise a custom clock."""
    clk = clock or 3579545
    if abs(clk - 3579545) <= 2:
        return "clockSel=0"
    if clk == 4000000:
        return "clockSel=1"
    return f"customClock={clk}"


def c352_flag_text(clock: int, quad: bool = False) -> str:
    """Crystal for a C352 core that divides by 288.

    The value is the real crystal. `quad` enables the four-output mode,
    which sends the rear pair to its own outputs.
    """
    parts = [f"customClock={clock or C352_DEFAULT_CLOCK}"]
    if quad:
        parts.append("quadOutput=true")
    return "\n".join(parts)


def _write_flag(b: Buf, text: str) -> int:
    ptr = len(b)
    size_off, start = b.begin_block(b"FLAG")
    b.strz(text)
    b.end_block(size_off, start)
    return ptr


def _write_empty_adir(b: Buf) -> int:
    ptr = len(b)
    size_off, start = b.begin_block(b"ADIR")
    b.u32(0)
    b.end_block(size_off, start)
    return ptr


@dataclass
class Row:
    note: int = -1  # furnace 0-179, 180=off, -1 empty
    ins: int = EMPTY
    vol: int = EMPTY
    # None until the row holds an effect. Blank rows are the overwhelming
    # majority of a pattern, so the slot list is made on the first effect.
    fx: list[tuple[int, int]] | None = None

    def is_empty(self) -> bool:
        if self.note >= 0 or self.ins != EMPTY or self.vol != EMPTY:
            return False
        if self.fx is None:
            return True
        return all(c == EMPTY for c, _ in self.fx)


def _blank_row() -> Row:
    return Row()


def _bump(stats: dict | None, key: str, n: int = 1) -> None:
    """Count a structural loss event (dropped/overwritten/retriggered content)."""
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


# Every counter the writer raises when content cannot be placed as written.
# `loss_total` is the gate the condense modes compare against the 1x baseline.
LOSS_KEYS = (
    "note_collision",      # a second note landed on a cell that already had one
    "off_swallowed",       # a note-off collided with a note
    "pcm_evicted",         # _assign_pcm_rows dropped an earlier trigger
    "pcm_unplaced",        # a PCM trigger got no row at all
    "fx_dropped",          # more effects on a row than it has columns
    "sweep_clipped",       # a pitch ramp was cut short by a later note
    "vol_step_dropped",    # a fade/TL step lost to a note owning the row
    "e5_skipped",          # a fine-tune step lost to a foreign note row
    "legato_skipped",      # a quick-legato transpose lost to a foreign row
    "ed_stripped_loop",    # an ED delay removed so Bxx could survive
    "orders_truncated",    # >256 orders needed
)


def loss_total(stats: dict) -> int:
    return sum(stats.get(k, 0) for k in LOSS_KEYS)


def keyon_loss(stats: dict | None) -> int:
    """Source key-ons the writer could not place on a pattern cell."""
    if not stats:
        return 0
    return (
        stats.get("note_collision", 0)
        + stats.get("pcm_unplaced", 0)
        + stats.get("pcm_evicted", 0)
        + stats.get("orders_truncated", 0)
    )


def dev_max(stats: dict) -> int:
    """Worst per-row pitch deviation from the driver's steps, in 1/64 st.

    The fidelity number the condense pick compares; it grows as the grid
    coarsens because a row can only render a straight-line slide.
    """
    return int(stats.get("sweep_dev_max", 0))


def _slide_level(level: int) -> int:
    """Column value that lands on ``level`` after the same-tick FA01 step.

    The row sets the column, then the slide runs. 15 is the top of the
    column, so a tooth that peaks there plays one level low.
    """
    if level >= 15:
        return 15
    return level + 1


def _add_fx(row: Row, cmd: int, val: int, fx_cols: int, stats: dict | None = None) -> None:
    fx = row.fx
    if fx is None:
        fx = row.fx = [(EMPTY, EMPTY)] * fx_cols
    for i, (c, _) in enumerate(fx):
        if c == cmd:
            fx[i] = (cmd, val)
            return
    for i, (c, _) in enumerate(fx):
        if c == EMPTY:
            fx[i] = (cmd, val)
            return
    if fx:
        fx[-1] = (cmd, val)
        _bump(stats, "fx_dropped")
    elif fx_cols:
        fx.append((cmd, val))


# C352 quad pan: front pair 0x81/0x82, rear pair 0x89/0x8A. The platform
# scales the volume into all four, and a quad module carries the level here.
QUAD_FX = (0x81, 0x82, 0x89, 0x8A)


def _add_quad_fx(
    row: Row,
    quad: tuple[int, int, int, int],
    fx_cols: int,
    stats: dict | None = None,
    prev: tuple[int, int, int, int] | None = None,
) -> None:
    """Write the four C352 pan slots, skipping any already in force."""
    for i, (cmd, val) in enumerate(zip(QUAD_FX, quad)):
        if prev is not None and prev[i] == val:
            continue
        _add_fx(row, cmd, val & 0xFF, fx_cols, stats)


def _encode_patn_row(row: Row, fx_cols: int) -> bytes:
    """One PATN row. Empty → 0x00 (skip 1)."""
    if row.is_empty():
        return b"\x00"
    fx = [(c, v) for c, v in (row.fx or ())[:fx_cols] if c != EMPTY]
    mask = 0
    extra = 0
    payload = bytearray()
    # extra is the 16-bit effect presence mask (type/value pairs).
    if row.note >= 0:
        mask |= 0x01
        payload.append(row.note & 0xFF)
    if row.ins != EMPTY:
        mask |= 0x02
        payload.append(row.ins & 0xFF)
    if row.vol != EMPTY:
        mask |= 0x04
        payload.append(row.vol & 0xFF)
    if fx:
        # bit3/4: effect 0 + value 0 in the first mask
        mask |= 0x08 | 0x10
        if len(fx) > 1:
            mask |= 0x20  # extra byte for effects 0-3
            if len(fx) > 4:
                mask |= 0x40
            extra = 0
            bit = 1
            for _c, _v in fx[:8]:
                extra |= bit | (bit << 1)
                bit <<= 2
        for c, v in fx[:8]:
            payload.append(c & 0xFF)
            payload.append(v & 0xFF)
    out = bytearray()
    out.append(mask & 0xFF)
    if mask & 0x20:
        out.append(extra & 0xFF)
    if mask & 0x40:
        out.append((extra >> 8) & 0xFF)
    out.extend(payload)
    return bytes(out)


def _write_patn(b: Buf, channel: int, index: int, rows: list[Row | None], fx_cols: int, name: str = "") -> None:
    size_off, start = b.begin_block(b"PATN")
    b.u8(0)  # subsong
    b.u8(channel)
    b.u16(index)
    b.strz(name)
    # Empty rows encode as 0x00; a pattern is mostly empty ones, so they are
    # counted and written as runs instead of one call per row.
    empty = 0
    for row in rows:
        if row is None or (row.note < 0 and row.ins == EMPTY and row.vol == EMPTY and not row.fx):
            empty += 1
            continue
        if empty:
            b.raw(b"\x00" * empty)
            empty = 0
        b.raw(_encode_patn_row(row, fx_cols))
    if empty:
        b.raw(b"\x00" * empty)
    b.u8(0xFF)
    b.end_block(size_off, start)


def _compat_phase1(b: Buf) -> None:
    """20 bytes, version >= 69. Modern (non-broken) defaults; linear pitch = full."""
    b.u8(0)  # limit slides
    b.u8(2)  # linear pitch: full linear (<237)
    b.u8(0)  # loop modality
    b.u8(1)  # proper noise layout
    b.u8(0)  # wave duty is volume
    b.u8(0)  # reset macro on porta
    b.u8(0)  # legacy volume slides
    b.u8(0)  # compatible arpeggio
    b.u8(1)  # note off resets slides
    b.u8(1)  # target resets slides
    b.u8(0)  # arp inhibits porta
    b.u8(0)  # wack algorithm macro
    b.u8(0)  # broken shortcut slides
    b.u8(1)  # ignore duplicate slides
    b.u8(0)  # stop porta on note off
    b.u8(0)  # continuous vibrato
    b.u8(0)  # broken DAC mode
    b.u8(0)  # one tick cut
    b.u8(1)  # ins change allowed during porta
    b.u8(1)  # reset note base on arp stop


def _compat_phase2(b: Buf) -> None:
    """28 bytes, version >= 130."""
    b.u8(0)  # broken speed selection
    b.u8(0)  # no slides on first tick
    b.u8(0)  # next row reset arp pos
    b.u8(0)  # ignore jump at end
    b.u8(0)  # buggy porta after slide
    b.u8(1)  # new ins affects GB env
    b.u8(1)  # shared extch state
    b.u8(0)  # ignore DAC mode change outside
    b.u8(1)  # E1/E2 take priority
    b.u8(1)  # new Sega PCM
    b.u8(0)  # weird fnum pitch slides
    b.u8(0)  # SN duty macro resets phase
    b.u8(1)  # pitch macro is linear
    b.u8(1)  # pitch slide speed in full linear (see _emit_sweep: the written rate
             # is calibrated by measurement, not by the 1/128 figure this flag names)
    b.u8(0)  # old octave boundary
    b.u8(0)  # disable OPN2 DAC volume
    b.u8(1)  # new volume scaling
    b.u8(1)  # volume macro lingers
    b.u8(0)  # broken outVol
    b.u8(1)  # E1/E2 stop on same note
    b.u8(0)  # broken porta after arp
    b.u8(0)  # SN periods under 8 are 1
    b.u8(0)  # cut/delay policy (normal)
    b.u8(0)  # jump treatment (normal)
    b.u8(1)  # automatic system name
    b.u8(0)  # disable sample macro
    b.u8(0)  # broken outVol 2
    b.u8(0)  # old arp strategy


def _traj_at(pts: list[tuple[int, int]], t: int) -> int:
    """Pitch offset at `t`. Each step holds until the next write."""
    v = 0
    for s, d in pts:
        if s > t:
            break
        v = d
    return v


def _emit_steps(
    grid: list[dict[int, Row]],
    song: Song,
    ev,
    fx_cols: int,
    total_rows: int,
    e5_writes: dict[tuple[int, int], int],
    chan: int | None = None,
    stats: dict | None = None,
) -> None:
    """Encode a short held-step pitch trajectory as row effects.

    An octave switch or percussion bend needs no instrument macro: E8xx/E9xx
    carry whole semitones, E5xx the remainder.
    """
    if chan is not None and chan != ev.ch:
        ev = replace(ev, ch=chan)
    ch = ev.ch
    if ch >= len(grid):
        return
    div = 2.0 if (ev.pcm or ev.sweep_div >= 2.0) else 1.0
    note_row = song.place(ev.sample, stats)[0]
    # One row can carry one transpose and one fine-tune step.  If the driver
    # wrote several f-nums inside one row, the last one is the row's pitch;
    # the intermediate values are shorter than a row and cannot be heard.
    by_row: dict[int, int] = {}
    for t, delta in ev.pitch_steps:
        row = song.place(t, stats)[0]
        if row <= note_row:
            row = note_row + 1
        if 0 <= row < total_rows:
            by_row[row] = delta
    e5w = ev.e5
    xpose = 0

    def free_row(r: int) -> int | None:
        while r < total_rows:
            cell = grid[ch].get(r)
            if cell is None or not (0 <= cell.note < 180):
                return r
            r += 1
        return None

    def cell_at(r: int) -> Row:
        cell = grid[ch].get(r)
        if cell is None:
            cell = _blank_row()
            grid[ch][r] = cell
        return cell

    targets = sorted(by_row)
    e8_rows: set[int] = set()
    # A pitch step must not spill past its own note. The next note resets its
    # pitch from its own key-on.
    limit_row = total_rows
    if ev.end:
        limit_row = min(
            total_rows, max(note_row + 1, song.place(ev.end, stats)[0] + 1),
        )
    for idx, row in enumerate(targets):
        if row >= limit_row:
            break
        delta = by_row[row]
        next_row = targets[idx + 1] if idx + 1 < len(targets) else total_rows
        next_row = min(next_row, limit_row)
        cell = grid[ch].get(row)
        if cell is not None and 0 <= cell.note < 180:
            # A later note owns this row and carries its own pitch.
            continue
        # E8xx/E9xx reach +-15 semitones, and a larger jump is split over
        # rows. Never borrow a row a later step owns. Its effect must land
        # there.
        cur_row = row
        while cur_row < next_row:
            need = delta - (xpose + (e5w - ev.e5) / div)
            if abs(need) <= 1024:
                break
            y = 15 if need > 0 else -15
            _add_fx(cell_at(cur_row), 0xE8 if y > 0 else 0xE9, 15, fx_cols, stats)
            e8_rows.add(cur_row)
            xpose += 64 * y
            nxt = free_row(cur_row + 1)
            if nxt is None or nxt >= next_row:
                cur_row = next_row
                break
            cur_row = nxt
        if cur_row >= next_row:
            # No room before the next step; that step's target will carry the
            # remainder using its own row.
            continue
        need = delta - (xpose + (e5w - ev.e5) / div)
        if abs(need) < 1e-6:
            continue
        # Keep E5 inside its range. Bring the residual back to +-1 st with
        # E8xx/E9xx, and a slow glide moves every row instead of stepping once
        # E5 saturates.
        desired = delta - xpose
        hi_units = (ev.e5_hi - ev.e5) / div
        lo_units = (ev.e5_lo - ev.e5) / div
        if desired > hi_units:
            semis = int(math.ceil((desired - hi_units) / 64.0))
        elif desired < lo_units:
            semis = int(math.floor((desired - lo_units) / 64.0))
        else:
            semis = 0
        if semis:
            if cur_row in e8_rows:
                # This row already carries a chunk; a second E8 would
                # replace it.  Carry the rest to the next free row.
                nxt = free_row(cur_row + 1)
                if nxt is None or nxt >= next_row:
                    continue
                cur_row = nxt
            y = max(-15, min(15, semis))
            _add_fx(cell_at(cur_row), 0xE8 if y > 0 else 0xE9, abs(y), fx_cols, stats)
            e8_rows.add(cur_row)
            xpose += 64 * y
        desired = delta - xpose
        new_e5 = max(
            ev.e5_lo,
            min(ev.e5_hi, int(round(ev.e5 + desired * div))),
        )
        if new_e5 != e5w:
            e5_writes[(ch, cur_row)] = new_e5
            e5w = new_e5


def _emit_sweep(
    grid: list[dict[int, Row]],
    song: Song,
    ev,
    fx_cols: int,
    total_rows: int,
    e5_writes: dict[tuple[int, int], int],
    note_rows: dict[int, list[int]],
    chan: int | None = None,
    stats: dict | None = None,
) -> None:
    """Encode a key-on pitch trajectory as per-row ramps and E5 steps.

    Rows are sampled at their boundaries: jumps become E5 steps, drift
    becomes the row rate, and the final row is rated against the held value.
    """
    if chan is not None and chan != ev.ch:
        ev = replace(ev, ch=chan)
    pts = list(ev.sweep)
    if not pts or ev.ch >= len(grid):
        return
    step_times = [s for s, _d in pts]
    row_len = song.samples_per_row
    tick = row_len / max(1, song.speed)
    start = ev.sample
    # The release tail keeps gliding. The trajectory runs to its last KC/KF
    # write. A later note's row clips it below.
    end = pts[-1][0]
    if end - start < tick:
        # Shorter than a tick: no room to ramp. The driver's jump still has to
        # land, and an instant transpose on the next row is the closest the
        # engine can get.
        final = float(_traj_at(pts, int(end)))
        y = max(-15, min(15, int(round(final / 64.0))))
        if abs(y) >= 1:
            row0, _d = song.place(start, stats)
            row1 = row0 + 1
            if row1 < total_rows:
                cell = grid[ev.ch].get(row1)
                if cell is None or not (0 <= cell.note < 180):
                    if cell is None:
                        cell = _blank_row()
                        grid[ev.ch][row1] = cell
                    _add_fx(cell, 0xE8 if y > 0 else 0xE9, abs(y), fx_cols, stats)
        return
    row0, _d0 = song.place(start, stats)
    cell0 = grid[ev.ch].get(row0)
    if cell0 is None:
        return
    base = song.t0 + row0 * row_len
    # A later note owns its own row: clip the sweep before it, or the tail
    # rate (and any E5) would collide with the new note's own row. The
    # crossing is usually a tick or two.
    crossed = False
    after = note_rows.get(ev.ch, ())
    i = bisect_right(after, row0)
    if i < len(after):
        limit = int(song.t0 + after[i] * row_len)
        if limit < end:
            end = limit
            crossed = True
            _bump(stats, "sweep_clipped")

    # E5 and 01xx/02xx move the platform's pitch value, 1/128 semitone in
    # linear mode. The trajectory is in 1/64 semitones. OPN and PCM events
    # (sweep_div 2) need two engine units per trajectory unit. OPM keeps its
    # historical one-unit mapping.
    div = 2.0 if (ev.pcm or ev.sweep_div >= 2.0) else 1.0

    def e5_byte(offset: float) -> int:
        # E5xx is the note's fine tune; fine-tune bytes and slide units
        # advance the same pitch value.
        return max(ev.e5_lo, min(ev.e5_hi, int(round(ev.e5 + offset * div))))

    e5w = ev.e5
    slide = 0.0  # ramp accumulation in trajectory units
    xpose = 0.0  # quick-legato transpose applied via E8xx/E9xx (units)
    last_rate = 0
    last_k = 0
    k = 0
    # The row walk samples the trajectory in time order. A moving index beats
    # restarting at the first point for every row.
    main_i = 0

    def traj(t: int) -> int:
        nonlocal main_i
        while main_i < len(pts) and pts[main_i][0] <= t:
            main_i += 1
        return pts[main_i - 1][1] if main_i else 0

    dev_i = 0
    while True:
        rs = base + k * row_len
        if rs >= end:
            break
        re_full = base + (k + 1) * row_len
        re = min(re_full, end)
        # A row carries one slide rate. Steps inside it collapse into it.
        _bump(stats, "sweep_rate_merged",
              max(0, bisect_left(step_times, re_full) - bisect_left(step_times, rs) - 1))
        t_eff = start if k == 0 else rs
        if k == 0:
            # The first driver step lands inside this row and is covered by
            # its ramp.
            want = 0.0
        else:
            # A jump just past the boundary still snaps onto this row.
            want = float(traj(int(rs + tick)))
        cur = (e5w - ev.e5) / div + slide + xpose
        cell_k = grid[ev.ch].get(row0 + k)
        if abs(want - cur) >= 6:
            if cell_k is None or not (0 <= cell_k.note < 180):
                # A later note's E5 sets its own attack and wins its row.
                e5w = e5_byte(want - slide - xpose)
                e5_writes[(ev.ch, row0 + k)] = e5w
                cur = (e5w - ev.e5) / div + slide + xpose
            else:
                _bump(stats, "e5_skipped")
        # The engine slides for the whole row. The last row is rated over its
        # full span against the held final value.
        is_last = re_full > end
        span = (re_full - t_eff) / tick if is_last else (re - t_eff) / tick
        if k == 0 and ev.pcm:
            # A PCM key-on write would apply a slice of the row's travel.
            want_end = 0.0
        else:
            want_end = float(traj(int(re_full if is_last else re)))
        rate = 0
        if span > 0:
            # FM advances one kc_kf unit per tick per parameter unit. PCM
            # moves 1/128 st per unit, and its written parameter is doubled.
            limit = 255.0 / div
            # Whatever the slide cannot deliver lands as a quick-legato
            # transpose (E8xx/E9xx, held until the next key-on).
            full = limit * span
            need = want_end - cur
            if abs(need) > full and k > 0:
                cell = grid[ev.ch].get(row0 + k)
                if cell is None or not (0 <= cell.note < 180):
                    if ev.pcm:
                        shift = int(round(xpose / 64.0))
                        y = int(round(want_end / 64.0)) - shift
                        y = max(-15, min(15, y))
                        if y:
                            if cell is None:
                                cell = _blank_row()
                                grid[ev.ch][row0 + k] = cell
                            _add_fx(cell, 0xE8 if y > 0 else 0xE9, abs(y), fx_cols, stats)
                            xpose += 64.0 * y
                            slide = 0.0
                            cur = (e5w - ev.e5) / 2.0 + xpose
                    else:
                        # FM adds the transpose to the note until the next one.
                        short = need - (full if need > 0 else -full)
                        y = max(-15, min(15, int(round(short / 64.0))))
                        if y:
                            if cell is None:
                                cell = _blank_row()
                                grid[ev.ch][row0 + k] = cell
                            _add_fx(cell, 0xE8 if y > 0 else 0xE9, abs(y), fx_cols, stats)
                            xpose += 64.0 * y
                            cur += 64.0 * y
                else:
                    _bump(stats, "legato_skipped")
            rate = max(-limit, min(limit, (want_end - cur) / span))
        param = int(round(rate * div))
        param = max(-255, min(255, param))
        if param != last_rate or param != 0:
            # The effect must be written on every row or the glide freezes.
            cell = grid[ev.ch].get(row0 + k)
            if cell is None:
                cell = _blank_row()
                grid[ev.ch][row0 + k] = cell
            # Drop the opposite-direction slide first, or both survive and
            # the net slide depends on column order.
            cell.fx = [(c, v) for c, v in (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                       if c not in (0x01, 0x02)]
            if param:
                _add_fx(cell, 0x01 if param > 0 else 0x02, min(255, abs(param)), fx_cols, stats)
            else:
                _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0, fx_cols, stats)
            last_rate = param
        # Accrue the movement the written parameter actually makes. Rounding
        # error compounds over long glides.
        engine_rate = param / div
        # Fidelity: compare the row's straight line with the driver's steps.
        if stats is not None and span > 0:
            while dev_i < len(pts) and pts[dev_i][0] <= rs:
                dev_i += 1
            bad_here = False
            for j in range(4):
                t = int(rs + (re_full - rs) * (2 * j + 1) / 8.0)
                if t <= start:
                    continue
                while dev_i < len(pts) and pts[dev_i][0] <= t:
                    dev_i += 1
                actual = pts[dev_i - 1][1] if dev_i else 0
                model = cur + engine_rate * span * (2 * j + 1) / 8.0
                dev = abs(model - actual)
                if dev > 6:
                    bad_here = True
                    stats["sweep_dev_bad"] = stats.get("sweep_dev_bad", 0) + 1
                if dev > stats.get("sweep_dev_max", 0):
                    stats["sweep_dev_max"] = int(round(dev))
            if bad_here:
                stats["sweep_dev_rows"] = stats.get("sweep_dev_rows", 0) + 1
        slide += engine_rate * span
        last_k = k
        k += 1

    # Stop the ramp on the row after the last one and pin the final pitch
    # with E5. The final row was rated over its full span. This corrects
    # rounding residue only.
    final = float(_traj_at(pts, int(end)))
    stop_row = row0 + last_k + 1
    if crossed:
        # The later note reads its own pitch. The stop may sit on its row.
        cell = grid[ev.ch].get(stop_row)
        if cell is None:
            cell = _blank_row()
            grid[ev.ch][stop_row] = cell
        if last_rate and not any(c in (0x01, 0x02) for c, _v in (cell.fx or ())):
            _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0, fx_cols, stats)
        return
    if stop_row >= total_rows:
        return
    cell = grid[ev.ch].get(stop_row)
    foreign = cell is not None and 0 <= cell.note < 180
    pin = e5_byte(final - slide - xpose)
    if not last_rate and (foreign or pin == e5w):
        return
    if cell is None:
        cell = _blank_row()
        grid[ev.ch][stop_row] = cell
    if last_rate:
        cell.fx = [(c, v) for c, v in (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                   if c not in (0x01, 0x02)]
        _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0, fx_cols, stats)
    if not foreign and pin != e5w:
        e5_writes[(ev.ch, stop_row)] = pin


def _assign_pcm_rows(
    song: Song, total_rows: int, last_sample: int, stats: dict | None = None,
) -> dict[int, tuple[int, int]]:
    """Row and delay for each playable PCM note-on and its note-off.

    A free row is taken. A taken row pushes the trigger forward until the
    next trigger, or keeps the later write. A note-off moves after its
    trigger and before the next trigger. When a later trigger comes first,
    its key-on stops the previous sample and the off is redundant.
    """
    by_ch: dict[int, list] = {}
    for ev in song.events:
        if ev.pcm and ev.ch >= 0:
            by_ch.setdefault(ev.ch, []).append(ev)
    limit = min(total_rows, song.place(last_sample, stats)[0] + 1)
    out: dict[int, tuple[int, int]] = {}
    for evs in by_ch.values():
        evs.sort(key=lambda e: (e.sample, 0 if e.on else 1))
        ons = [e for e in evs if e.on]
        ideals = [song.place(e.sample, stats) for e in ons]
        placed: dict[int, int] = {}
        for i, ev in enumerate(ons):
            row, delay = ideals[i]
            if row < 0 or row >= total_rows:
                continue
            if row in placed:
                prev = ons[placed[row]]
                if prev.sample == ev.sample or (prev.ins, prev.note) == (ev.ins, ev.note):
                    # Two writes at one source sample, or one sample retriggered
                    # within a row. The later start wins and the first is under
                    # a row away, so it cannot be heard as a separate note.
                    out.pop(id(prev), None)
                    _bump(stats, "pcm_merged")
                    placed[row] = i
                    out[id(ev)] = (row, delay)
                    continue
                dest = row + 1
                while dest < total_rows and dest in placed:
                    dest += 1
                if dest < total_rows:
                    placed[dest] = i
                    out[id(ev)] = (dest, 0)
                    _bump(stats, "pcm_shifted")
                    continue
                out.pop(id(prev), None)
                _bump(stats, "pcm_evicted")
                placed[row] = i
                out[id(ev)] = (row, delay)
                continue
            if row >= limit:
                continue
            placed[row] = i
            out[id(ev)] = (row, delay)
        played = [e for e in ons if id(e) in out]
        for off in (e for e in evs if not e.on):
            # A trigger at the same sample retriggers the voice. The off
            # belongs to the note before it and is redundant.
            before = [e for e in played if e.sample < off.sample]
            if not before:
                continue
            on = before[-1]
            on_row = out[id(on)][0]
            nxt = next((e for e in played if e.sample > on.sample), None)
            next_row = out[id(nxt)][0] if nxt is not None else total_rows
            off_row, off_delay = song.place(off.sample, stats)
            if off_row >= next_row:
                continue
            if off_row <= on_row:
                off_row, off_delay = on_row + 1, 0
            if off_row >= next_row or off_row >= total_rows:
                continue
            if off_row in placed:
                continue
            out[id(off)] = (off_row, off_delay)
    return out


def _pattern_len(last_row: int, cap: int = PAT_LEN_MAX) -> int:
    """The last content row plus a ten-row tail, capped at 256.

    Furnace holds one pattern length for the whole module. `last_row` is -1
    for an empty module.
    """
    return max(1, min(cap, last_row + 1 + PAT_TAIL_ROWS))


def _cell_ed(cell: Row | None) -> int:
    """The row's ED delay, 0 when it carries none."""
    for c, v in (cell.fx if cell is not None else ()) or ():
        if c == 0xED:
            return v
    return 0


def _place_chip_fx(
    grid: list[dict[int, Row]],
    song: Song,
    entries,
    fx_cols: int,
    total_rows: int,
    content_cut: int | None = None,
    stats: dict | None = None,
    max_ch: int = 8,
) -> None:
    """Place chip-wide effects.

    0x17 rides the key-on tick it belongs to. PMD, AMD and wave writes stay
    on channel 0 at the row of the write.
    """
    if not entries:
        return
    max_ch = min(max_ch, len(grid))
    row_len = song.samples_per_row
    has_speed = any(entry[1] == 0x17 for entry in entries)
    by_tick: dict[tuple[int, int], list[int]] = {}
    notes: list[tuple[float, int, int, int]] = []
    times: list[float] = []
    if has_speed:
        tick = row_len / max(1, song.speed)
        for ch in range(max_ch):
            for row, cell in grid[ch].items():
                if 0 <= cell.note < 180:
                    by_tick.setdefault((row, _cell_ed(cell)), []).append(ch)
        notes = sorted(
            (song.t0 + row * row_len + delay * tick, ch, row, delay)
            for (row, delay), chans in by_tick.items() for ch in chans
        )
        times = [t for t, _ch, _r, _d in notes]

    def has_cmd(cell: Row | None, cmd_i: int) -> bool:
        return any(
            c == cmd_i for c, _v in (cell.fx if cell is not None else ()) or ()
        )

    def has_fx(cell: Row | None, cmd_i: int, val_i: int) -> bool:
        return any(
            (c, v) == (cmd_i, val_i)
            for c, v in (cell.fx if cell is not None else ()) or ()
        )

    for entry in entries:
        sample, cmd, val = entry[0], entry[1], entry[2]
        tick_off = entry[3] if len(entry) > 3 else 0
        if content_cut is not None and sample >= content_cut:
            continue
        if cmd != 0x17:
            row = song.row_of(sample)
            if row < 0 or row >= total_rows:
                continue
            cell = grid[0].get(row)
            if cell is None:
                cell = _blank_row()
                grid[0][row] = cell
            _add_fx(cell, cmd, val, fx_cols, stats)
            continue
        target = None
        i = bisect_left(times, sample)
        for j in (i - 1, i):
            if not 0 <= j < len(notes):
                continue
            cand = notes[j]
            if abs(cand[0] - sample) > row_len:
                continue
            if target is None or abs(cand[0] - sample) < abs(target[0] - sample):
                target = cand

        chosen: tuple[int, int] | None = None
        delay = 0
        if target is not None:
            _t, _note_ch, row, delay = target
        else:
            row = song.row_of(sample)
            if row < 0 or row >= total_rows:
                continue
        if tick_off:
            speed = max(1, song.speed)
            delay += tick_off
            row += delay // speed
            delay %= speed
            if row < 0 or row >= total_rows:
                continue
        if target is not None:
            for cand_ch in by_tick.get((row, delay), ()):
                if not has_cmd(grid[cand_ch].get(row), cmd):
                    chosen = (cand_ch, row)
                    break
            if chosen is None:
                for cand_ch in range(max_ch):
                    cell = grid[cand_ch].get(row)
                    if _cell_ed(cell) == delay and not has_cmd(cell, cmd):
                        chosen = (cand_ch, row)
                        break
        if chosen is None:
            for cand_ch in range(max_ch):
                cell = grid[cand_ch].get(row)
                free = cell is None or not (0 <= cell.note < 180)
                if (free and _cell_ed(cell) in (0, delay)
                        and not has_cmd(cell, cmd)):
                    chosen = (cand_ch, row)
                    break
        if chosen is None:
            chosen = (0, row)
        ch, row = chosen
        cell = grid[ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ch][row] = cell
        if has_fx(cell, cmd, val):
            continue
        _add_fx(cell, cmd, val, fx_cols, stats)
        if delay and _cell_ed(cell) == 0:
            _add_fx(cell, 0xED, delay, fx_cols, stats)


def _build_patterns(song: Song, n_ch: int, pat_len_max: int, fx_cols: int,
                    ch_offset: int = 0, stats: dict | None = None) -> tuple[
    list[list[int]], list[tuple[int, int, list[Row | None]]], int, int
]:
    """Return (orders[ch][ord], patterns as (ch, idx, rows), loop_order, pat_len).

    Untouched rows stay `None`. `pat_len` is the window the orders were
    walked with and every pattern is written at.
    """
    last_sample = song.events[-1].sample if song.events else 0
    # A final note with no key-off (a sustained pad or drone) holds to the
    # end of the log.  The pattern otherwise stops at the key-on and the
    # export loops while the source is still sounding.
    if any(
        ev.on and not ev.pcm and not ev.dac and not ev.tone_chip and not ev.end
        for ev in song.events
    ):
        last_sample = max(last_sample, song.vgm.total_samples)
    # a fade or chip write can run past the last note; the module has to carry
    # it or a voice keyed at zero (a fade-in) never gets its volume
    for ev in song.events:
        if ev.vol_pts:
            last_sample = max(last_sample, ev.vol_pts[-1][0])
        if ev.quad_pts:
            last_sample = max(last_sample, ev.quad_pts[-1][0])
        if ev.on and ev.pcm and ev.end:
            last_sample = max(last_sample, ev.end)
    for entry in (getattr(song, "channel_fx", ()) or ()):
        last_sample = max(last_sample, entry[0])
    for entry in (getattr(song, "chip_fx", ()) or ()):
        last_sample = max(last_sample, entry[0])
    # An adjusted loop carries crafted sample points, read with the writer's
    # own cell placement so every row rate opens on the same row. A raw VGM
    # marker keeps the nearest-row reading.
    adjusted = song.loop_sample is not None and song.loop_end_sample is not None
    if song.loop_sample is None:
        loop_row = 0
    elif adjusted:
        loop_row = song.place(song.loop_sample)[0]
    else:
        loop_row = song.row_of(song.loop_sample)
    if loop_row < 0:
        loop_row = 0
    # The loop finder trims the loop end to a bar, but the source continues to
    # the VGM total. Keep that tail: it can hold post-bar A/B gates, and the
    # module loops at the source total. The order walk below reuses the name
    # end_row for each order's bound, so this one keeps its own name.
    content_end = None
    content_cut = None
    if adjusted:
        content_end = song.place(
            max(song.loop_end_sample, song.vgm.total_samples)
        )[0]
        if content_end < loop_row:
            content_end = None
    if content_end is not None:
        total_rows = content_end + 1
    else:
        content_end = song.place(last_sample, stats)[0]
        total_rows = max(1, content_end + 2)

    grid: list[dict[int, Row]] = [dict() for _ in range(n_ch)]
    if stats is not None:
        stats["rows"] = max(stats.get("rows", 0), total_rows)

    # Chip-wide effects are placed after the notes: the LFO speed/pulse has
    # to ride the tick of the key-on it belongs to (see _place_chip_fx).
    for sample, ch, cmd, val in getattr(song, "channel_fx", ()) or ():
        ch += ch_offset
        if ch < 0 or ch >= n_ch:
            continue
        if content_cut is not None and sample >= content_cut:
            continue
        # Same placement as the note on this sample, including its ED delay.
        # row_of() rounds to the nearest row and would land an effect on a
        # different row from the note whenever speed is more than 1.
        row, delay = song.place(sample, stats)
        if row < 0 or row >= total_rows:
            continue
        cell = grid[ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ch][row] = cell
        _add_fx(cell, cmd, val, fx_cols, stats)
        if delay:
            _add_fx(cell, 0xED, delay, fx_cols, stats)

    # E5 is channel state. Collect it per row and replay in row order, back
    # to the neutral 0x80. One note's fine tune cannot leak into the next.
    note_e5: dict[tuple[int, int], int] = {}
    # Mid-note carrier-TL fades ride the volume column as (127 - vol) against
    # the instrument TL. Every FM note-on re-asserts 127 when any note fades.
    fm_tl = any(
        ev.on and not ev.pcm and ev.tl_pts and 0 <= ev.ch + ch_offset < n_ch
        for ev in song.events
    )
    # One cell holds one PCM note. A later trigger moves to the next free row
    # when that row is still before the following trigger. When it is not,
    # the later trigger keeps the row: it is the cursor the channel plays.
    pcm_pos = _assign_pcm_rows(song, total_rows, last_sample, stats)
    for ev in song.events:
        ev_ch = ev.ch + ch_offset
        if ev_ch < 0 or ev_ch >= n_ch:
            continue
        if content_cut is not None and ev.sample >= content_cut:
            continue
        if ev.on:
            _bump(stats, "keyons")
        if ev.pcm:
            assigned = pcm_pos.get(id(ev))
            if assigned is None:
                # An off with no assigned row is the back-to-back retrigger
                # case: the next key-on stops the sample. Writing a NOTE_OFF
                # here can land after that key-on and cut the new note.
                _bump(stats, "pcm_unplaced" if ev.on else "pcm_off_skipped")
                continue
            row, delay = assigned
        else:
            row, delay = song.place(ev.sample, stats)
        if row < 0 or row >= total_rows:
            continue
        cell = grid[ev_ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ev_ch][row] = cell
        if not ev.on:
            if ev.pcm:
                if cell.note >= 0 and cell.note != NOTE_OFF:
                    _bump(stats, "pcm_off_collision")
                    continue
                cell.note = NOTE_OFF
                if delay:
                    _add_fx(cell, 0xED, delay, fx_cols, stats)
                continue
            # A note-off on the note's own row is a cut (ECxx). An off at or
            # before that row is the previous note releasing, and the new
            # note wins.
            started = 0
            for cmd, val in cell.fx or ():
                if cmd == 0xED:
                    started = val
                    break
            if cell.note < 0 or cell.note == NOTE_OFF:
                cell.note = NOTE_OFF
                if delay:
                    _add_fx(cell, 0xED, delay, fx_cols, stats)
            elif delay > started:
                _add_fx(cell, 0xEC, delay, fx_cols, stats)
            else:
                _bump(stats, "off_swallowed")
            continue
        if 0 <= cell.note < 180:
            _bump(stats, "note_collision")
        cell.note = ev.note if ev.note != 180 else NOTE_OFF
        if ev.ins >= 0:
            cell.ins = ev.ins
        if ev.vol >= 0:
            if ev.vol_slide:
                # FA01 steps on the tick it is written, so the column is one
                # higher than the level the chip should play. FA00 holds.
                if ev.vol_slide == 1:
                    cell.vol = _slide_level(ev.vol)
                    _add_fx(cell, 0xFA, 0x01, fx_cols, stats)
                else:
                    cell.vol = ev.vol
                    _add_fx(cell, 0xFA, 0x00, fx_cols, stats)
            else:
                cell.vol = ev.vol
        elif fm_tl and cell.vol == EMPTY:
            cell.vol = 127
        if delay:
            _add_fx(cell, 0xED, delay, fx_cols, stats)
        note_e5[(ev_ch, row)] = ev.e5
        # PCM pan 0x00 is both channels off. Omitting it leaves Furnace's
        # default (both full). FM pan 0 still means "no 08xx on this row".
        if ev.quad is not None:
            _add_quad_fx(cell, ev.quad, fx_cols, stats)
        elif (ev.pcm or ev.pan) and not ev.no_pan:
            _add_fx(cell, 0x08, ev.pan & 0xFF, fx_cols, stats)

    # A PCM note still sounding at the loop cut or the last content row gets
    # an explicit off. The source stop write can be in the skipped tail.
    for ch in range(n_ch):
        last = None
        for ev in song.events:
            if (ev.on and ev.pcm and ev.ch + ch_offset == ch
                    and id(ev) in pcm_pos):
                if content_cut is not None and ev.sample >= content_cut:
                    continue
                last = ev
        if last is None:
            continue
        on_row = pcm_pos[id(last)][0]
        has_off = any(
            not ev.on and ev.pcm and ev.ch + ch_offset == ch
            and ev.sample > last.sample
            and (content_cut is None or ev.sample < content_cut)
            and id(ev) in pcm_pos and pcm_pos[id(ev)][0] > on_row
            for ev in song.events
        )
        if has_off:
            continue
        target = content_end if content_end is not None else total_rows - 1
        if last.end:
            target = min(target, song.place(last.end, stats)[0])
        if target <= on_row:
            target = on_row + 1
        if target >= total_rows:
            continue
        cell = grid[ch].get(target)
        if cell is None:
            cell = _blank_row()
            grid[ch][target] = cell
        if cell.note >= 0 and cell.note != NOTE_OFF:
            continue
        cell.note = NOTE_OFF

    _place_chip_fx(
        grid, song, getattr(song, "chip_fx", ()) or (), fx_cols,
        total_rows, content_cut, stats,
    )

    # A held note's carrier fade steps land on their own rows (only when no
    # note owns the row, so a following note's own volume wins).
    if fm_tl:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not ev.on or ev.pcm or not ev.tl_pts or not (0 <= ev_ch < n_ch):
                continue
            if 0 <= ev.ins < len(song.fm_patches):
                t0 = song.fm_patches[ev.ins].carrier_tl()
            else:
                t0 = 0x7F
            for sample, tl in ev.tl_pts:
                if content_cut is not None and sample >= content_cut:
                    continue
                row, _delay = song.place(sample, stats)
                if row < 0 or row >= total_rows:
                    continue
                cell = grid[ev_ch].get(row)
                if cell is None:
                    cell = _blank_row()
                    grid[ev_ch][row] = cell
                if cell.note >= 0:
                    _bump(stats, "vol_step_dropped")
                    continue
                cell.vol = max(0, min(127, 127 - (tl - t0)))

    # Pitch sweeps run after every event is placed, so the ramps can share
    # the note-off rows and their ED delays. Note rows are known by now, so
    # each channel's list is built once for the crossing checks.
    note_rows: dict[int, list[int]] = {
        ch: sorted(r for r, cell in grid[ch].items() if 0 <= cell.note < 180)
        for ch in range(n_ch)
    }
    sweep_e5: dict[tuple[int, int], int] = {}
    if song.events:
        for ev in song.events:
            if (
                ev.on
                and ev.pitch_steps
                and 0 <= ev.ch + ch_offset < n_ch
                and (content_cut is None or ev.sample < content_cut)
            ):
                if ch_offset:
                    _emit_steps(
                        grid, song, ev, fx_cols, total_rows, sweep_e5,
                        chan=ev.ch + ch_offset, stats=stats,
                    )
                else:
                    _emit_steps(
                        grid, song, ev, fx_cols, total_rows, sweep_e5,
                        stats=stats,
                    )
    if song.events:
        for ev in song.events:
            if (
                ev.on
                and ev.sweep
                and 0 <= ev.ch + ch_offset < n_ch
                and (content_cut is None or ev.sample < content_cut)
            ):
                if ch_offset:
                    _emit_sweep(grid, song, ev, fx_cols, total_rows, sweep_e5,
                                note_rows, ev.ch + ch_offset, stats)
                else:
                    _emit_sweep(grid, song, ev, fx_cols, total_rows, sweep_e5,
                                note_rows, stats=stats)

    # C140 volume steps: the column feeds the chip gain live
    # (platform/c140.cpp chVol = outVol*pan/255), balance rides as 08xx.
    # Only note-free cells are written.
    if song.events:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not (ev.on and ev.vol_pts) or ev_ch < 0 or ev_ch >= n_ch:
                continue
            last_pan = None if ev.no_pan else ev.pan
            note_vol_done = False
            for sample, value, pan in ev.vol_pts:
                if content_cut is not None and sample >= content_cut:
                    continue
                row, delay = song.place(sample, stats)
                if not ev.vol_slide:
                    delay = 0
                if row < 0 or row >= total_rows:
                    continue
                cell = grid[ev_ch].get(row)
                if cell is None:
                    cell = _blank_row()
                    grid[ev_ch][row] = cell
                first_at_note = (
                    cell.note >= 0
                    and ev.tone_chip
                    and row in note_rows.get(ev_ch, ())
                    and not note_vol_done
                )
                if first_at_note:
                    # A loud gate and its quiet follow-up share the note row;
                    # let the quiet step set the note instead of dropping it.
                    note_vol_done = True
                    if ev.vol_slide:
                        cont = bool(pan)
                        level = _slide_level(value) if cont else value
                        cell.vol = level
                        _add_fx(cell, 0xFA, 0x01 if cont else 0x00, fx_cols, stats)
                        if delay:
                            _add_fx(cell, 0xED, delay, fx_cols, stats)
                    else:
                        cell.vol = value
                        if pan is not None and (last_pan is None or pan != last_pan):
                            _add_fx(cell, 0x08, pan & 0xFF, fx_cols, stats)
                            last_pan = pan
                    continue
                if cell.note >= 0:
                    _bump(stats, "vol_step_dropped")
                    continue
                if ev.vol_slide:
                    # The third element is the slide flag, not a pan value.
                    cont = bool(pan)
                    level = _slide_level(value) if cont else value
                    if cell.vol != EMPTY and cell.vol != level:
                        _bump(stats, "vol_step_dropped")
                    cell.vol = level
                    _add_fx(cell, 0xFA, 0x01 if cont else 0x00, fx_cols, stats)
                    if delay:
                        _add_fx(cell, 0xED, delay, fx_cols, stats)
                    continue
                cell.vol = value
                if pan is not None and (last_pan is None or pan != last_pan):
                    _add_fx(cell, 0x08, pan & 0xFF, fx_cols, stats)
                    last_pan = pan

    # A quad C352 voice carries its level on the four pan slots, so a mid-note
    # step writes those effects. Note cells keep their own values.
    if song.events:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not (ev.on and ev.quad_pts) or ev_ch < 0 or ev_ch >= n_ch:
                continue
            prev = ev.quad
            for sample, fl, fr, rl, rr in ev.quad_pts:
                if content_cut is not None and sample >= content_cut:
                    continue
                row, _delay = song.place(sample, stats)
                if row < 0 or row >= total_rows:
                    continue
                cell = grid[ev_ch].get(row)
                if cell is None:
                    cell = _blank_row()
                    grid[ev_ch][row] = cell
                if cell.note >= 0:
                    _bump(stats, "vol_step_dropped")
                    continue
                four = (fl, fr, rl, rr)
                _add_quad_fx(cell, four, fx_cols, stats, prev)
                prev = four

    for ch in range(n_ch):
        cur_e5 = 0x80
        rows = sorted(
            {r for c, r in note_e5 if c == ch}
            | {r for c, r in sweep_e5 if c == ch}
        )
        for row in rows:
            val = note_e5.get((ch, row), sweep_e5.get((ch, row)))
            if val == cur_e5:
                continue
            cell = grid[ch].get(row)
            if cell is None:
                cell = _blank_row()
                grid[ch][row] = cell
            _add_fx(cell, 0xE5, val, fx_cols, stats)
            cur_e5 = val

    # The pattern window: last content row plus a ten-row tail, capped at
    # Furnace's 256. One length serves every order.
    last_content = max(
        (row for ch in range(n_ch) for row in grid[ch]), default=-1
    )
    pat_len = _pattern_len(last_content, pat_len_max)

    # Walk pat_len-row chunks, but force an order boundary at the VGM loop so
    # Bxx can jump there. A long intro must not become one giant order; only
    # `pat_len` rows would survive.
    bounds = [0]
    anchor = loop_row if 0 < loop_row < total_rows else None
    pos = 0
    while True:
        nxt = pos + pat_len
        if anchor is not None and pos < anchor < nxt:
            bounds.append(anchor)
            pos = anchor
            continue
        if nxt >= total_rows:
            break
        bounds.append(nxt)
        pos = nxt
    if bounds[-1] < total_rows:
        bounds.append(total_rows)
    raw_n = len(bounds) - 1
    n_ord = min(256, raw_n)
    bounds = bounds[: n_ord + 1]
    if raw_n > 256:
        _bump(stats, "orders_truncated")
        song.warnings.append(
            f"truncated to 256 orders ({raw_n} needed); raise --speed or split the VGM"
        )

    loop_order = 0
    for oi in range(n_ord):
        if bounds[oi] == loop_row:
            loop_order = oi
            break

    orders: list[list[int]] = [[] for _ in range(n_ch)]
    patterns: list[tuple[int, int, list[Row | None]]] = []
    for ch in range(n_ch):
        for oi in range(n_ord):
            start_row = bounds[oi]
            end_row = bounds[oi + 1]
            rows: list[Row | None] = [None] * pat_len
            span = min(pat_len, end_row - start_row)
            for i in range(span):
                src = grid[ch].get(start_row + i)
                if src is not None:
                    rows[i] = src
            # Short non-last orders: D00 so we don't play trailing empty rows
            # before the next order (used for the intro/loop split).
            if oi != n_ord - 1 and 0 < span < pat_len:
                cell = rows[span - 1]
                if cell is None:
                    cell = rows[span - 1] = _blank_row()
                _add_fx(cell, 0x0D, 0, fx_cols, stats)
            # Bxx goes on the last row that carries content, not the padded
            # order end, or every loop restart inserts silence. Jingles with
            # no VGM loop must not Bxx.
            if oi == n_ord - 1 and song.loop_sample is not None:
                # EDxx is a pre-effect and returns before later effects on the
                # row are read, so a delay on the jump row swallows Bxx and
                # that note never sounds.
                jump_row = min(pat_len, max(0, content_end - start_row + 1)) - 1
                if jump_row < 0:
                    jump_row = 0
                cell = rows[jump_row]
                if cell is None:
                    cell = rows[jump_row] = _blank_row()
                if any(c == 0xED for c, _v in (cell.fx or ())):
                    _bump(stats, "ed_stripped_loop")
                cell.fx = [(c, v) for c, v in (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                           if c != 0xED]
                _add_fx(cell, 0x0B, loop_order, fx_cols, stats)
            orders[ch].append(oi)
            patterns.append((ch, oi, rows))

    return orders, patterns, loop_order, pat_len


def _fit_fx_cols(patterns: list[tuple[int, int, list[Row | None]]], n_ch: int) -> list[int]:
    """Effect columns per channel: one per effect the busiest row uses, 1..8.

    Furnace sizes a pattern column from this count, so it is the
    fit-to-content knob (docs/fur-writer.md).
    """
    cols = [1] * n_ch
    for ch, _idx, rows in patterns:
        if not 0 <= ch < n_ch:
            continue
        used = max(
            (sum(c != EMPTY for c, _v in (row.fx or ())) for row in rows if row is not None),
            default=0,
        )
        if used > cols[ch]:
            cols[ch] = used
    return [min(8, c) for c in cols]


def _grouped(samples):
    """Chip ids in first-seen order, with the samples that belong to each."""
    ids: list[int] = []
    groups: dict[int, list] = {}
    for smp in samples:
        if smp.chip_id not in groups:
            ids.append(smp.chip_id)
            groups[smp.chip_id] = []
        groups[smp.chip_id].append(smp)
    return ids, groups


def compress_fur(data: bytes, level: int = 5) -> bytes:
    """Wrap a raw .fur in the zlib container Furnace's loader expects."""
    return zlib.compress(data, level)


def decompress_fur(data: bytes) -> bytes:
    """Undo `compress_fur`, or return an already-raw module unchanged."""
    if data[:1] != b"x":
        return data
    try:
        return zlib.decompress(data)
    except zlib.error:
        return data


def write_fur(
    song: Song, *, include_pcm: bool = True, include_fm: bool = True,
    stats: dict | None = None, compress: bool = False,
) -> bytes:
    gd3 = song.vgm.gd3
    fm_family = getattr(song, "fm_chip", "ym2151")
    ym2203 = fm_family == "ym2203"
    ym2612 = fm_family == "ym2612"
    ym2610 = fm_family == "ym2610"
    ym_chip = ym2203 or ym2612 or ym2610
    ym_ext = bool(
        (getattr(song, "ym2203_ext", False) if ym2203 else
         getattr(song, "ym2612_ext", False))
    ) if (ym2203 or ym2612) else False
    if ym2203:
        ym_ids = list(getattr(song, "ym2203_ids", ()) or ())
    elif ym2612:
        ym_ids = list(getattr(song, "ym2612_ids", ()) or ())
    elif ym2610:
        ym_ids = list(getattr(song, "ym2610_ids", ()) or ())
    else:
        ym_ids = []
    # A YM2612 system stays when the log had the chip, even with no FM notes.
    # A YM2203 system likewise owns its three SSG voices.  A YM2610 system
    # owns FM, SSG and the two ADPCM engines, so either half keeps it.
    if ym2610:
        fm_present = (include_fm or include_pcm) and bool(ym_ids)
    else:
        fm_present = include_fm and (bool(ym_ids) if ym_chip else True)
    fm_ins = song.fm_patches if (fm_present and include_fm) else []
    pcm_smps = [s for s in song.pcm_samples if s.length >= 8] if include_pcm else []
    oki_smps = list(song.oki_samples) if include_pcm else []
    adpcm_smps = list(song.msm6258_samples) if include_pcm else []
    n_adpcm_ins = (len(adpcm_smps) + 119) // 120
    spcm_smps = [s for s in song.segapcm_samples if s.length >= 8] if include_pcm else []
    c140_smps = [s for s in song.c140_samples if s.length >= 8] if include_pcm else []
    c352_smps = [s for s in song.c352_samples if s.length >= 8] if include_pcm else []
    k053260_smps = (
        [s for s in song.k053260_samples if s.length >= 8] if include_pcm else []
    )
    dac_smps = list(getattr(song, "dac_samples", ()) or ()) if include_pcm else []
    n_dac_ins = (len(dac_smps) + 119) // 120
    dac_smp_base = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(c140_smps) + len(c352_smps)
        + len(k053260_smps)
    )
    ym2610_a_smps = (
        list(getattr(song, "ym2610_a_samples", ()) or ()) if include_pcm else []
    )
    ym2610_b_smps = (
        list(getattr(song, "ym2610_b_samples", ()) or ()) if include_pcm else []
    )
    n_ym2610_a_ins = (len(ym2610_a_smps) + 119) // 120
    n_ym2610_b_ins = (len(ym2610_b_smps) + 119) // 120
    ym2610_a_smp_base = dac_smp_base + len(dac_smps)
    ym2610_b_smp_base = ym2610_a_smp_base + len(ym2610_a_smps)
    oki_ids, oki_groups = _grouped(oki_smps)
    adpcm_ids, adpcm_groups = _grouped(adpcm_smps)
    c140_ids: list[int] = []
    for smp in c140_smps:
        for h in smp.hits:
            if h.chip_id not in c140_ids:
                c140_ids.append(h.chip_id)
    c352_ids: list[int] = []
    for smp in c352_smps:
        for h in smp.hits:
            if h.chip_id not in c352_ids:
                c352_ids.append(h.chip_id)
    k053260_ids: list[int] = []
    for smp in k053260_smps:
        for h in smp.hits:
            if h.chip_id not in k053260_ids:
                k053260_ids.append(h.chip_id)
    # Pitched (non-sample) chips: SCC voices and the AY-8910. Their events
    # carry the channels and instruments, so an empty list means the chip is
    # absent from the source.
    scc_ids = list(getattr(song, "scc_ids", ()) or ())
    scc_waves = list(getattr(song, "scc_waves", ()) or ())
    scc_specs = list(getattr(song, "scc_ins", ()) or ())
    ay_ids = list(getattr(song, "ay_ids", ()) or ())
    ay_specs = list(getattr(song, "ay_ins", ()) or ())
    psg_ids = list(getattr(song, "psg_ids", ()) or ())
    psg_specs = list(getattr(song, "psg_ins", ()) or ())
    scc_chip = CHIP_K052539 if song.vgm.k051649_plus else CHIP_K051649
    n_scc = 5 * len(scc_ids)
    n_ay = 3 * len(ay_ids)
    n_psg = 4 * len(psg_ids)
    # K007232 appears only when the source carried usable samples for it, and
    # never beside another sample chip.
    want_k007 = bool(pcm_smps)
    if fm_present:
        if ym2610:
            n_fm = (6 if song.ym2610_b else 4) * len(ym_ids)
        elif ym2612:
            n_fm = (9 if ym_ext else 6) * len(ym_ids)
        elif ym2203:
            n_fm = (6 if ym_ext else 3) * len(ym_ids)
        else:
            n_fm = 8
    else:
        n_fm = 0
    # The SSG is part of each YM2203/YM2610 system, not a separate AY chip.
    n_ssg = 3 * len(ym_ids) if ((ym2203 or ym2610) and fm_present) else 0
    n_adpcm_a = 6 * len(ym_ids) if (ym2610 and fm_present) else 0
    n_adpcm_b = len(ym_ids) if (ym2610 and fm_present) else 0
    n_k007 = 2 if want_k007 else 0
    n_ch = (
        n_fm + n_ssg + n_adpcm_a + n_adpcm_b + n_psg + n_k007
        + 4 * len(oki_ids) + len(adpcm_ids)
        + (16 if spcm_smps else 0) + 24 * len(c140_ids) + 32 * len(c352_ids)
        + 4 * len(k053260_ids) + n_scc + n_ay
    )
    if n_ch == 0:
        raise ValueError("write_fur: need include_fm and/or include_pcm")
    fx_cols = 8  # room for LFO 17/18/1E/1F plus pan/E5/ED/Bxx
    k_clock = song.vgm.k007232_clock or 3579545
    spcm_clock = song.vgm.segapcm_clock or SEGAPCM_CLOCK_X
    c140_clock = song.vgm.c140_clock or 12288000
    c352_clock = song.vgm.c352_clock or C352_DEFAULT_CLOCK
    k053260_clock = song.vgm.k053260_clock or 3579545

    n_ins = (
        len(fm_ins) + n_dac_ins + len(pcm_smps) + len(oki_smps) + n_adpcm_ins
        + len(spcm_smps) + len(c140_smps) + len(c352_smps) + len(k053260_smps)
        + n_ym2610_a_ins + n_ym2610_b_ins
        + len(scc_specs) + len(ay_specs) + len(psg_specs)
    )
    n_smp = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(c140_smps) + len(c352_smps)
        + len(k053260_smps) + len(dac_smps)
        + len(ym2610_a_smps) + len(ym2610_b_smps)
    )
    n_wav = len(scc_waves)

    # Analysis numbers sample-chip channels after the FM block. With no FM
    # they shift down to stay inside n_ch.
    if ym2610:
        fm_analysis_block = (6 if song.ym2610_b else 4) + 3 + 6 + 1
    else:
        fm_analysis_block = ((9 if ym_ext else 6) * len(ym_ids)) if ym_chip else 8
    orders, patterns, _loop_ord, pat_len = _build_patterns(
        song, n_ch, PAT_LEN_MAX, fx_cols,
        0 if fm_present else -fm_analysis_block,
        stats=stats,
    )
    effect_cols = _fit_fx_cols(patterns, n_ch)
    n_ord = len(orders[0]) if orders else 1
    n_pat = len(patterns)

    names_long: list[str] = []
    names_short: list[str] = []
    if ym2610 and fm_present:
        fm_count = 6 if song.ym2610_b else 4
        for cid in range(len(ym_ids)):
            suffix = f" ({cid + 1})" if len(ym_ids) > 1 else ""
            for voice in range(fm_count):
                names_long.append(f"FM {voice + 1}{suffix}")
                names_short.append(f"F{cid + 1}-{voice + 1}")
            for voice in range(3):
                names_long.append(f"PSG {voice + 1}{suffix}")
                names_short.append(f"P{cid + 1}-{voice + 1}")
            for voice in range(6):
                names_long.append(f"ADPCM-A {voice + 1}{suffix}")
                names_short.append(f"A{cid + 1}-{voice + 1}")
            names_long.append(f"ADPCM-B{suffix}")
            names_short.append(f"B{cid + 1}")
    elif ym2612 and fm_present:
        for cid in range(len(ym_ids)):
            suffix = f" ({cid + 1})" if len(ym_ids) > 1 else ""
            if ym_ext:
                for voice, label in enumerate(("FM 1", "FM 2")):
                    names_long.append(f"{label}{suffix}")
                    names_short.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(4):
                    # Column c carries operator (1,2,0,3)[c]'s frequency.
                    op_label = (1, 2, 0, 3)[voice] + 1
                    label = f"FM 3 OP{op_label}"
                    names_long.append(f"{label}{suffix}")
                    names_short.append(f"O{cid + 1}-{op_label}")
                for voice in range(3):
                    label = f"FM {voice + 4}"
                    names_long.append(f"{label}{suffix}")
                    names_short.append(f"F{cid + 1}-{voice + 4}")
            else:
                for voice in range(6):
                    names_long.append(f"FM {voice + 1}{suffix}")
                    names_short.append(f"F{cid + 1}-{voice + 1}")
    elif ym_chip and fm_present:
        for cid in range(len(ym_ids)):
            if ym_ext:
                for voice, label in enumerate(("FM 1", "FM 2")):
                    names_long.append(f"{label} ({cid + 1})" if len(ym_ids) > 1 else label)
                    names_short.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(4):
                    # Column c carries operator (1,2,0,3)[c]'s frequency
                    # (Furnace's ext frequency-register order).
                    op_label = (1, 2, 0, 3)[voice] + 1
                    label = f"FM 3 OP{op_label}"
                    names_long.append(f"{label} ({cid + 1})" if len(ym_ids) > 1 else label)
                    names_short.append(f"O{cid + 1}-{op_label}")
                for voice in range(3):
                    label = f"PSG {voice + 1}"
                    names_long.append(f"{label} ({cid + 1})" if len(ym_ids) > 1 else label)
                    names_short.append(f"P{cid + 1}-{voice + 1}")
                continue
            for voice in range(3):
                names_long.append(f"FM {cid + 1}-{voice + 1}")
                names_short.append(f"F{cid + 1}-{voice + 1}")
            for voice in range(3):
                names_long.append(f"PSG {cid + 1}-{voice + 1}")
                names_short.append(f"P{cid + 1}-{voice + 1}")
    elif n_fm:
        names_long += [f"FM {i + 1}" for i in range(n_fm)]
        names_short += [f"F{i + 1}" for i in range(n_fm)]
    if psg_ids:
        for cid in range(len(psg_ids)):
            suffix = f" ({cid + 1})" if len(psg_ids) > 1 else ""
            for voice in range(3):
                names_long.append(f"PSG {voice + 1}{suffix}")
                names_short.append(f"S{cid + 1}-{voice + 1}")
            names_long.append(f"PSG Noise{suffix}")
            names_short.append(f"S{cid + 1}-N")
    if n_k007:
        names_long += [f"PCM {i + 1}" for i in range(n_k007)]
        names_short += [f"P{i + 1}" for i in range(n_k007)]
    for cid in oki_ids:
        for voice in range(4):
            names_long.append(f"OKI{cid + 1} {voice + 1}")
            names_short.append(f"O{cid + 1}{voice + 1}")
    for cid in adpcm_ids:
        if len(adpcm_ids) == 1 and cid == 0:
            names_long.append("ADPCM")
            names_short.append("AD")
        else:
            names_long.append(f"ADPCM {cid + 1}")
            names_short.append(f"A{cid + 1}")
    if spcm_smps:
        for voice in range(16):
            names_long.append(f"SEGA {voice + 1}")
            names_short.append(f"S{voice + 1}")
    for cid in c140_ids:
        for voice in range(24):
            names_long.append(f"C140 {cid + 1}-{voice + 1}")
            names_short.append(f"C{cid + 1}-{voice + 1}")
    for cid in c352_ids:
        for voice in range(32):
            names_long.append(f"C352 {cid + 1}-{voice + 1}")
            names_short.append(f"Z{cid + 1}-{voice + 1}")
    for cid in k053260_ids:
        for voice in range(4):
            names_long.append(f"K053260 {cid + 1}-{voice + 1}")
            names_short.append(f"Q{cid + 1}-{voice + 1}")
    for cid in scc_ids:
        for voice in range(5):
            names_long.append(f"SCC {cid + 1}-{voice + 1}")
            names_short.append(f"K{cid + 1}-{voice + 1}")
    for cid in ay_ids:
        for voice in range(3):
            names_long.append(f"PSG {cid + 1}-{voice + 1}")
            names_short.append(f"P{cid + 1}-{voice + 1}")

    label_parts: list[str] = []
    if fm_present:
        if ym2610:
            base = "YM2610B" if song.ym2610_b else "YM2610"
            label_parts.append(f"{base} x{len(ym_ids)}" if len(ym_ids) > 1 else base)
        elif ym2612:
            base = "YM2612" + (" Ext" if ym_ext else "")
            label_parts.append(f"{base} x{len(ym_ids)}" if len(ym_ids) > 1 else base)
        elif ym2203:
            label_parts.append(
                f"YM2203 x{len(ym_ids)}" if len(ym_ids) > 1 else "YM2203"
            )
        else:
            label_parts.append("YM2151")
    if psg_ids:
        label_parts.append("SN76489")
    if n_k007:
        label_parts.append("K007232")
    if oki_ids:
        label_parts.append("MSM6295")
    if adpcm_ids:
        label_parts.append("MSM6258")
    if spcm_smps:
        label_parts.append("SegaPCM")
    if c140_ids:
        label_parts.append("C140")
    if c352_ids:
        label_parts.append("C352")
    if k053260_ids:
        label_parts.append("K053260")
    if scc_ids:
        label_parts.append("SCC+" if song.vgm.k051649_plus else "SCC")
    if ay_ids:
        label_parts.append("AY-8910")
    chip_label = " + ".join(label_parts) or "empty"

    comment = (
        f"Transcribed from VGM by vgm2151fur {_version()} ({chip_label}).\n"
        "Check volumes, sample C-4 rates, and the loop jump.\n"
        "Reconvert with the current vgm2151fur when this version is older.\n"
        f"source: {song.vgm.path.name}\n"
        f"grid: {song.hz:.0f} Hz × speed {song.speed}\n"
        f"{song.vgm.gd3.get('notes', '')}"
    )
    if c352_ids:
        comment += (
            "\nNamco C352 is chip 0xD0. Furnace 0.6.8.3 does not emulate it, "
            "so these channels stay silent in that build.\n"
        )
    if k053260_ids:
        comment += (
            f"\nKonami K053260 at {k053260_clock} Hz; KADPCM samples are "
            "stored as sample type 7 and loop the whole body.\n"
        )
    if scc_ids:
        comment += (
            f"\nKonami {'SCC+ (K052539)' if song.vgm.k051649_plus else 'SCC (K051649)'}"
            f" at {song.vgm.k051649_clock or 1789772} Hz; each SCC instrument "
            "carries one 32-byte waveform.\n"
        )
    if ay_ids:
        comment += (
            f"\nAY-8910 at {song.vgm.ay8910_clock or 1789772} Hz; noise, "
            "channel mode and the hardware envelope are pattern effects "
            "(20xx/21xx/22xy/23xx/24xx).\n"
        )
    if ym2612:
        comment += (
            f"\nYM2612 at {song.vgm.ym2612_clock or 7670453} Hz; each system "
            "has six FM voices"
            + (" and channel 3 split into four operator columns" if ym_ext else "")
            + ". LFO enable/speed is effect 10xy and per-channel PMS/AMS are "
            "62xx/63xx.\n"
        )
    if psg_ids:
        comment += (
            f"\nSN76489 at {song.vgm.sn76489_clock or 3579545} Hz; three "
            "square voices then noise. The volume column is the 4-bit "
            "attenuation (15 = loudest) and 20xy selects the noise mode.\n"
        )
    elif ym2610:
        comment += (
            f"\nYM2610{'B' if song.ym2610_b else ''} at "
            f"{song.vgm.ym2610_clock or 8000000} Hz; each system has the FM "
            "voices, three SSG voices, six ADPCM-A channels and one ADPCM-B "
            "channel. ADPCM-A plays at clock/432; ADPCM-B uses the source "
            "rate register.\n"
        )
    elif ym_chip:
        comment += (
            f"\nYM2203 at {song.vgm.ym2203_clock or 3579545} Hz; each system "
            "has three FM voices then three SSG voices. The SSG register "
            "effects (20xx/21xx/22xy/23xx/24xx) work on the last three "
            "channels of each six-channel block.\n"
        )

    b = Buf()
    b.raw(FUR_MAGIC)
    b.u16(FUR_VERSION)
    b.u16(0)
    b.u32(32)
    b.pad(8)

    chips: list[int] = []
    flag_texts: list[str] = []
    ym2610_sys: dict[int, int] = {}
    if fm_present:
        for cid in (ym_ids or [0]):
            if ym2612:
                chips.append(CHIP_YM2612_EXT if ym_ext else CHIP_YM2612)
            elif ym2203:
                chips.append(CHIP_YM2203_EXT if ym_ext else CHIP_YM2203)
            elif ym2610:
                chips.append(CHIP_YM2610B if song.ym2610_b else CHIP_YM2610)
                ym2610_sys[cid] = len(chips) - 1
            else:
                chips.append(CHIP_YM2151)
            if ym2612:
                fm_clock = song.vgm.ym2612_clock
                # Furnace's Genesis default is COLOR_NTSC*15/7 truncated to
                # 7670454, while the VGM header normally says 7670453.  Write
                # the source clock so the exported pitch register matches it.
                fm_default = 7670454
            elif ym2203:
                fm_clock = song.vgm.ym2203_clock
                fm_default = 3579545
            elif ym2610:
                fm_clock = song.vgm.ym2610_clock
                fm_default = 8000000
            else:
                fm_clock = song.vgm.ym2151_clock
                fm_default = 3579545
            # The cores default to their console clocks.  At any other clock
            # the notes play at the wrong pitch unless the flag carries it.
            flag_texts.append(
                f"customClock={fm_clock}"
                if fm_clock and fm_clock != fm_default else ""
            )
    for _cid in psg_ids:
        chips.append(CHIP_SMS)
        psg_clock = song.vgm.sn76489_clock or 3579545
        if abs(psg_clock - 3579545) <= 2:
            flag_texts.append("clockSel=0")
        else:
            flag_texts.append(f"customClock={psg_clock}")
    if n_k007:
        chips.append(CHIP_K007232)
        flags = "stereo=true"
        pcm_clock = song.vgm.k007232_clock
        if pcm_clock and pcm_clock != 3579545:
            flags += f"\ncustomClock={pcm_clock}"
        flag_texts.append(flags)
    for cid in oki_ids:
        chips.append(CHIP_MSM6295)
        group = oki_groups[cid]
        flag_texts.append(oki_flag_text(
            group[0].clock, group[0].pin7, banked=oki_needs_banked(group),
        ))
    for cid in adpcm_ids:
        chips.append(CHIP_MSM6258)
        flag_texts.append(msm6258_flag_text(adpcm_groups[cid][0].clock))
    if spcm_smps:
        chips.append(CHIP_SEGAPCM)
        rom_size = max(
            (s.rom_size for s in song.vgm.roms if s.kind == "segapcm"), default=0
        )
        flag_texts.append(segapcm_flag_text(song.vgm.segapcm_clock, rom_size))
    for cid in c140_ids:
        chips.append(CHIP_C140)
        flag_texts.append(c140_flag_text(song.vgm.c140_clock, song.vgm.c140_type))
    for cid in c352_ids:
        chips.append(CHIP_C352)
        flag_texts.append(
            c352_flag_text(song.vgm.c352_clock, getattr(song, "c352_quad", False))
        )
    for cid in k053260_ids:
        chips.append(CHIP_K053260)
        flag_texts.append(k053260_flag_text(song.vgm.k053260_clock))
    for cid in scc_ids:
        chips.append(scc_chip)
        flag_texts.append(k051649_flag_text(song.vgm.k051649_clock))
    for cid in ay_ids:
        chips.append(CHIP_AY8910)
        flag_texts.append(ay8910_flag_text(
            song.vgm.ay8910_clock, song.vgm.ay8910_type,
        ))
    n_chips = len(chips)

    info_size_off, info_start = b.begin_block(b"INFO")
    b.u8(0)  # timebase stored as value-1; 0 → timebase 1
    b.u8(song.speed)
    b.u8(song.speed)
    b.u8(1)
    b.f32(float(song.hz))
    b.u16(pat_len)
    b.u16(n_ord)
    b.u8(max(1, min(255, song.hilight_a)))
    b.u8(max(1, min(255, song.hilight_b)))
    b.u16(n_ins)
    b.u16(n_wav)
    b.u16(n_smp)
    b.u32(n_pat)

    b.raw(bytes(chips) + bytes(32 - n_chips))
    vols = bytearray([64] * 32)
    chip_vols: list[float] = []
    k_vol = min(1.0, max(0.0, song.k007232_volume))
    chip_counts = Counter(chips)
    for i, chip in enumerate(chips):
        # Twin16 K007232 sits under the OPM; the rip's mix says how far. Every
        # other sample chip uses the player's mix (defaults + extra-header
        # entries) through _sample_chip_volume.
        if chip == CHIP_K007232 and include_fm:
            cv = k_vol
        elif chip in _CHIP_DEV:
            cv = _sample_chip_volume(song, chip, chip_counts[chip])
        else:
            cv = 1.0
        chip_vols.append(cv)
    # A chip volume above 1.0 (the MSM6258 player default is 0x11E) does not
    # fit Furnace's 0-64 field. Raise the song master instead and scale every
    # chip down by the same factor so the source mix is preserved exactly.
    master_gain = max([1.0] + chip_vols)
    for i, cv in enumerate(chip_vols):
        vols[i] = max(0, min(64, int(round(cv / master_gain * 64))))
    b.raw(bytes(vols))
    b.pad(32)
    flag_ptr_off = len(b)
    b.pad(128)

    b.strz(gd3.get("track") or song.vgm.path.stem)
    b.strz(gd3.get("author") or "")
    b.f32(440.0)
    _compat_phase1(b)

    ins_ptr_off = len(b)
    for _ in range(n_ins):
        b.u32(0)
    wave_ptr_off = len(b)
    for _ in range(n_wav):
        b.u32(0)
    smp_ptr_off = len(b)
    for _ in range(n_smp):
        b.u32(0)
    pat_ptr_off = len(b)
    for _ in range(n_pat):
        b.u32(0)

    for ch in range(n_ch):
        for oi in range(n_ord):
            b.u8(orders[ch][oi])
    for cols in effect_cols:
        b.u8(cols)
    for _ in range(n_ch):
        b.u8(1)
    for _ in range(n_ch):
        b.u8(0)
    for name in names_long:
        b.strz(name)
    for name in names_short:
        b.strz(name)
    b.strz(comment)
    b.f32(master_gain)
    _compat_phase2(b)
    b.u16(150)
    b.u16(150)
    b.strz(gd3.get("track") or "")
    b.strz("")
    b.u8(0)
    b.pad(3)
    b.strz(gd3.get("system") or f"Arcade ({chip_label})")
    b.strz(gd3.get("game") or "")
    b.strz(gd3.get("track_jp") or "")
    b.strz(gd3.get("author_jp") or "")
    b.strz(gd3.get("system_jp") or "")
    b.strz(gd3.get("game_jp") or "")

    # extra chip output (>=135), one (vol, panL, panR) triple per chip. The
    # player multiplies this by the master, exactly like the byte field, so a
    # boosted chip stores its share of the raised master.
    for cv in chip_vols:
        b.f32(cv / master_gain)
        b.f32(0.0)
        b.f32(0.0)
    b.u32(0)  # patchbay connections
    b.u8(1)  # automatic patchbay (>=136), required or the song is silent
    # 8 more compat flags (>=138); byte 1 is brokenFMOff (>=155)
    b.pad(8)
    # speed pattern (>=139) overrides speed 1/2; multiplied by (timeBase+1)=1
    b.u8(2)
    speed_pat = bytes([song.speed & 0xFF, song.speed & 0xFF]) + bytes(14)
    b.raw(speed_pat)
    b.u8(0)  # groove count
    adir_off = len(b)
    b.u32(0)
    b.u32(0)
    b.u32(0)
    b.end_block(info_size_off, info_start)

    adir_ptrs = [_write_empty_adir(b) for _ in range(3)]
    for i, p in enumerate(adir_ptrs):
        b.patch_u32(adir_off + 4 * i, p)

    for i, text in enumerate(flag_texts):
        if text:
            b.patch_u32(flag_ptr_off + 4 * i, _write_flag(b, text))

    ins_ptrs: list[int] = []
    for i, patch in enumerate(fm_ins):
        ins_ptrs.append(len(b))
        fam = "OPN" if getattr(patch, "opn", False) else "OPM"
        _write_ins_opm(b, patch.name or f"{fam} {i:02d}", patch)

    for group in range(n_dac_ins):
        ins_ptrs.append(len(b))
        first = dac_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(dac_smps) - group * 120))
        ]
        # 0.6.8.3 reads a nonzero base as an embedded sample count.
        _write_ins_sample_map(b, f"DAC {group + 1}", 0, maps)

    for i, smp in enumerate(pcm_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(b, f"PCM {_pcm_label(smp, i)}", i)
    for i, smp in enumerate(oki_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(b, smp.name, len(pcm_smps) + i, INS_MSM6295)
    adpcm_smp_base = len(pcm_smps) + len(oki_smps)
    for group in range(n_adpcm_ins):
        ins_ptrs.append(len(b))
        start = group * 120
        maps = [
            adpcm_smp_base + start + slot
            for slot in range(min(120, len(adpcm_smps) - start))
        ]
        _write_ins_sample_map(
            b, f"ADPCM {group + 1}", 0, maps, INS_MSM6258,
        )
    spcm_ins_base = len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
    for i, smp in enumerate(spcm_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"SEGA {smp.start:05x}", spcm_ins_base + i, INS_SEGAPCM,
        )
    c140_ins_base = spcm_ins_base + len(spcm_smps)
    for i, smp in enumerate(c140_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"C140 {smp.start:04x}", c140_ins_base + i, INS_C140,
        )
    c352_ins_base = c140_ins_base + len(c140_smps)
    for i, smp in enumerate(c352_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"C352 {smp.start:06x}", c352_ins_base + i, INS_C352,
        )
    k053260_ins_base = c352_ins_base + len(c352_smps)
    for i, smp in enumerate(k053260_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"K053260 {smp.start:06x}", k053260_ins_base + i,
            INS_K053260,
        )
    for group in range(n_ym2610_a_ins):
        ins_ptrs.append(len(b))
        first = ym2610_a_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(ym2610_a_smps) - group * 120))
        ]
        _write_ins_sample_map(
            b, f"ADPCM-A {group + 1}", 0, maps, INS_ADPCMA,
        )
    for group in range(n_ym2610_b_ins):
        ins_ptrs.append(len(b))
        first = ym2610_b_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(ym2610_b_smps) - group * 120))
        ]
        _write_ins_sample_map(
            b, f"ADPCM-B {group + 1}", 0, maps, INS_ADPCMB,
        )
    for i, spec in enumerate(scc_specs):
        ins_ptrs.append(len(b))
        wave = scc_waves[spec.wave] if spec.wave < len(scc_waves) else b""
        name = f"SCC {i:02d} {_wave_label(wave)}"
        if spec.waves:
            name += "+morph"
        if spec.pitch:
            name += "+pitch"
        _write_ins_scc(b, name, spec)
    for spec in ay_specs:
        ins_ptrs.append(len(b))
        _write_ins_ay(b, "PSG" + ("+pitch" if spec.pitch else ""), spec)
    for spec in psg_specs:
        ins_ptrs.append(len(b))
        _write_ins_psg(b, "SN" + ("+pitch" if spec.pitch else ""), spec)

    smp_ptrs: list[int] = []
    for i, smp in enumerate(pcm_smps):
        smp_ptrs.append(len(b))
        _write_sample(
            b,
            _pcm_label(smp, i),
            smp.pcm7,
            smp.c4_rate(k_clock),
            getattr(smp, "volume_scale", 1.0),
        )
    for smp in oki_smps:
        smp_ptrs.append(len(b))
        _write_vox(b, smp.name, smp.data, smp.rate)
    for smp in adpcm_smps:
        smp_ptrs.append(len(b))
        _write_vox(
            b, smp.name, smp.data, smp.rate,
            nibble_count=True, swap_nibbles=True,
        )
    for smp in spcm_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"spcm_{smp.start:05x}", smp.data, smp.c4_rate(spcm_clock),
            loop=(smp.loop_start, smp.loop_end),
        )
    for smp in c140_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"c140_{smp.start:04x}", smp.data, smp.c4_rate(c140_clock),
            loop=(smp.loop_start, smp.loop_end),
            depth=smp.depth,
        )
    for smp in c352_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"c352_{smp.start:06x}", smp.data, smp.c4_rate(c352_clock),
            loop=(smp.loop_start, smp.loop_end),
            depth=smp.depth,
            loop_mode=smp.loop_mode,
        )
    for smp in k053260_smps:
        smp_ptrs.append(len(b))
        name = f"k053260_{smp.start:06x}"
        if smp.depth == SMP_DEPTH_ADPCM_K:
            _write_sample_k(
                b, name, smp.data, smp.c4_rate(k053260_clock),
                loop=smp.loop_start is not None,
            )
        else:
            _write_sample_s8(
                b, name, smp.data, smp.c4_rate(k053260_clock),
                loop=(smp.loop_start, smp.loop_end),
            )
    for i, smp in enumerate(dac_smps):
        smp_ptrs.append(len(b))
        rate = max(1, int(round(smp.c4_rate * DAC_RATE_COMPENSATION)))
        _write_sample_s8(b, f"dac_{i:03d}", smp.data, rate)
    ym2610_clock = song.vgm.ym2610_clock or 8000000
    ym2610_a_rate = max(1, int(round(ym2610_clock / 432)))
    for smp in ym2610_a_smps:
        sys = ym2610_sys.get(smp.chip_id, 0)
        smp_ptrs.append(len(b))
        _write_sample_adpcm(
            b, f"adpcmA_{smp.start:06x}", smp.data, ym2610_a_rate,
            SMP_DEPTH_ADPCM_A, (1 << sys, 0, 0, 0),
        )
    for smp in ym2610_b_smps:
        sys = ym2610_sys.get(smp.chip_id, 0)
        rate = max(
            1, int(round(
                ym2610_clock / 144 * (smp.rate & 0xFFFF) / 65535
                * ADPCM_B_RATE_COMPENSATION
            )),
        )
        loop = any(h.loop for h in smp.hits)
        smp_ptrs.append(len(b))
        _write_sample_adpcm(
            b, f"adpcmB_{smp.start:06x}_{smp.rate:04x}", smp.data, rate,
            SMP_DEPTH_ADPCM_B, (0, 1 << sys, 0, 0), loop=loop,
        )

    pat_ptrs: list[int] = []
    for ch, idx, rows in patterns:
        pat_ptrs.append(len(b))
        _write_patn(b, ch, idx, rows, fx_cols)

    wave_ptrs: list[int] = []
    for wave in scc_waves:
        # The chip reads signed bytes; Furnace's wavetable is unsigned with
        # 128 as the center, exactly the byte + 128 the SCC platform subtracts.
        data = bytes((val + 128) & 0xFF for val in wave)
        wave_ptrs.append(_write_wave(b, f"scc_wave_{_wave_label(wave)}", data))

    for i, p in enumerate(ins_ptrs):
        b.patch_u32(ins_ptr_off + 4 * i, p)
    for i, p in enumerate(wave_ptrs):
        b.patch_u32(wave_ptr_off + 4 * i, p)
    for i, p in enumerate(smp_ptrs):
        b.patch_u32(smp_ptr_off + 4 * i, p)
    for i, p in enumerate(pat_ptrs):
        b.patch_u32(pat_ptr_off + 4 * i, p)

    data = bytes(b.b)
    return compress_fur(data) if compress else data


def parse_fur_skeleton(data: bytes) -> dict:
    """Walk a .fur we wrote and return header facts (for tests / sanity)."""
    data = decompress_fur(data)
    if data[:16] != FUR_MAGIC:
        raise ValueError("not a Furnace module")
    ver = struct.unpack_from("<H", data, 16)[0]
    info_ptr = struct.unpack_from("<I", data, 20)[0]
    if data[info_ptr:info_ptr + 4] != b"INFO":
        raise ValueError("INFO missing")
    info_size = struct.unpack_from("<I", data, info_ptr + 4)[0]
    info_end = info_ptr + 8 + info_size
    magics = []
    pos = info_end
    notes = 0
    offs = 0
    bxx = 0
    d00 = 0
    fx_cmds: dict[int, int] = {}
    while pos + 8 <= len(data):
        mag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        magics.append(mag.decode("ascii", "replace"))
        block = data[pos + 8:pos + 8 + size]
        if mag == b"PATN" and len(block) >= 4:
            i = 4
            while i < len(block) and block[i] != 0:
                i += 1
            i += 1
            row = 0
            while i < len(block) and block[i] != 0xFF and row < 256:
                mask = block[i]
                i += 1
                if mask == 0:
                    row += 1
                    continue
                effect_mask = 0
                if mask & 0x20:
                    effect_mask |= block[i]
                    i += 1
                if mask & 0x40:
                    effect_mask |= block[i] << 8
                    i += 1
                if mask & 0x08:
                    effect_mask |= 1
                if mask & 0x10:
                    effect_mask |= 2
                if mask & 1:
                    n = block[i]
                    i += 1
                    if n == NOTE_OFF:
                        offs += 1
                    elif n < 180:
                        notes += 1
                if mask & 2:
                    i += 1
                if mask & 4:
                    i += 1
                fx_bytes = []
                for bit in range(16):
                    if effect_mask & (1 << bit):
                        fx_bytes.append(block[i])
                        i += 1
                for col in range(0, len(fx_bytes) - 1, 2):
                    cmd = fx_bytes[col]
                    fx_cmds[cmd] = fx_cmds.get(cmd, 0) + 1
                    if cmd == 0x0B:
                        bxx += 1
                    elif cmd == 0x0D:
                        d00 += 1
                row += 1
        pos += 8 + size
        if pos > len(data):
            break
    # INFO payload: 24 bytes of song params, then 32 chip IDs
    chips = list(data[info_ptr + 8 + 24:info_ptr + 8 + 24 + 32])
    chips = [c for c in chips if c]
    return {
        "version": ver,
        "bytes": len(data),
        "info_end": info_end,
        "first_block": data[info_end:info_end + 4],
        "magics": magics,
        "chips": chips,
        "notes": notes,
        "offs": offs,
        "bxx": bxx,
        "d00": d00,
        "fx_cmds": fx_cmds,
    }
