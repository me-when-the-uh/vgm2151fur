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
from vgm2151fur.c140 import c140_furnace_clock
from vgm2151fur.c352 import C352_DEFAULT_CLOCK
from vgm2151fur.fm import CARRIERS, OPN_CARRIERS
from vgm2151fur.nes import nes_dpcm_rate
from vgm2151fur.oki import msm6258_flag_text, oki_flag_text, oki_needs_banked
from vgm2151fur.ymz280b import ymz280b_clock
from vgm2151fur.opl import (
    FURNACE_CLOCK, FURNACE_CLOCK_OPL3, opl_module_clock, opll_clock_flag,
    opll_module_clock,
)
from vgm2151fur.pcm import pcm7_to_s8, scale_pcm7
from vgm2151fur.pitch import FURNACE_CLOCK_DEFAULT, module_clock
from vgm2151fur.saa1099 import (
    SAA_CLOCK_8M, SAA_CLOCK_NTSC, SAA_CLOCK_PAL, SAA_DEFAULT_CLOCK,
)
from vgm2151fur.ym2608 import (
    RHYTHM_VOICES,
    YM2608_DEFAULT_CLOCK,
    deltat_store_body,
)

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
# Furnace's YM2608 (OPNA): the YM2610B's channel order with rhythm in place of
# ADPCM-A - 6 FM + 3 SSG + 6 rhythm + 1 ADPCM-B = 16 channels, or 19 with
# channel 3 split into four operator columns.
CHIP_YM2608 = 0x8E
CHIP_YM2608_EXT = 0xB7
# TI SN76489 (the SMS platform): three square voices then noise.  A Genesis
# module carries it as a second system after the YM2612.
CHIP_SMS = 0x03
# DIV_SYSTEM_T6W28: the Neo Geo Pocket Color's stereo SN76489 variant.
# Four channels (three square + noise), independent panL/panR.
CHIP_T6W28 = 0xBF
T6W28_DEFAULT_CLOCK = 3_072_000
CHIP_MSM6295 = 0xAA
CHIP_MSM6258 = 0xAB
CHIP_K007232 = 0xC6
CHIP_SEGAPCM = 0x9B
# Furnace hosts the RF5C68 (FM Towns) and RF5C164 (Sega CD) as one system.
# The chipType flag picks the variant: 0 = RF5C68, 1 = RF5C164.
CHIP_RF5C68 = 0x95
CHIP_C140 = 0xCE
CHIP_K053260 = 0xCC
# DIV_SYSTEM_YMZ280B: eight PCM/ADPCM sample voices.
CHIP_YMZ280B = 0xB8
# DIV_SYSTEM_VBOY: five wavetable voices and one noise voice.
CHIP_VBOY = 0x9C
# Furnace format.md reserves 0xD0 for Namco C352, 32 channels, and marks it
# unavailable. 0.6.8.3 has no C352 core and no C352 instrument type.
CHIP_C352 = 0xD0
# Konami SCC (K051649, voices 3/4 share wave RAM) and SCC+ (K052539,
# independent voice 4 waveform). AY-8910 is the three-channel PSG.
CHIP_K051649 = 0xA1
CHIP_K052539 = 0xB4
CHIP_AY8910 = 0x80
# DIV_SYSTEM_SAA1099: six square/noise voices, no software timers.  A pair of
# these is the Creative Music System / Game Blaster.
CHIP_SAA1099 = 0x97
# The OPL family. Furnace gives the rhythm mode its own system: the drums
# layout turns the last melodic channels into kick, snare, tom, cymbal and
# hi-hat columns (9 -> 11 channels on OPL/OPL2, 18 -> 20 on OPL3).
CHIP_OPL = 0x8F
CHIP_OPL2 = 0x90
CHIP_OPL3 = 0x91
CHIP_OPL_DRUMS = 0xA2
CHIP_OPL2_DRUMS = 0xA3
CHIP_OPL3_DRUMS = 0xA4
# Channels per OPL system and how many of them are melodic. The rhythm systems
# give the kick the first former FM channel and snare, tom, cymbal and hi-hat
# the next four (sysDef.cpp); the kick keeps its two operators, so it plays a
# normal OPL voice.
# The Y8950 is an OPL2 with one ADPCM column after the FM block. The OPL4 is
# an OPL3 with 24 wave columns after it.
CHIP_Y8950 = 0xB2
CHIP_Y8950_DRUMS = 0xB3
CHIP_OPL4 = 0xAE
CHIP_OPL4_DRUMS = 0xAF
OPL_CHIP = {("opl", False): CHIP_OPL, ("opl2", False): CHIP_OPL2,
            ("opl3", False): CHIP_OPL3, ("opl", True): CHIP_OPL_DRUMS,
            ("opl2", True): CHIP_OPL2_DRUMS, ("opl3", True): CHIP_OPL3_DRUMS,
            ("y8950", False): CHIP_Y8950, ("y8950", True): CHIP_Y8950_DRUMS,
            ("opl4", False): CHIP_OPL4, ("opl4", True): CHIP_OPL4_DRUMS}
# The YM2413 (OPLL) is its own core and instrument type. Rhythm mode is a
# separate system whose last three hardware channels become five kit columns.
CHIP_OPLL = 0x89
CHIP_OPLL_DRUMS = 0xA7
# DIV_SYSTEM_VRC7: the same OPLL core with six channels and the VRC7 preset
# ROM.  DIV_SYSTEM_NES: five channels (two pulses, triangle, noise, DPCM).
CHIP_VRC7 = 0x9D
CHIP_NES = 0x06
# DIV_SYSTEM_GB: four channels (two pulses, wavetable, noise). The chip's
# chipType flag selects the emulated model (0 DMG, 2 CGB-E); the converter
# writes CGB-E so the zombie-envelope rule matches.
CHIP_GB = 0x04
# DIV_SYSTEM_PCE: six wavetable voices (two pulses, triangle, noise, DPCM on
# the NES sit at 0x06).
CHIP_PCE = 0x05
OPLL_CHANNELS = 9
OPLL_DRUMS_CHANNELS = 11
OPLL_DRUMS_MELODIC = 6
OPLL_DRUM_LABELS = (
    ("Kick", "BD"), ("Snare", "SD"), ("Tom", "TM"), ("Top", "TP"),
    ("HiHat", "HH"),
)
# The OPL4's FM half is OPL3-shaped; its 24 wave columns are counted by
# `_system_span` on top of these.
OPL_CHANNELS = {("opl", False): 9, ("opl2", False): 9, ("opl3", False): 18,
                ("opl", True): 11, ("opl2", True): 11, ("opl3", True): 20,
                ("y8950", False): 9, ("y8950", True): 11,
                ("opl4", False): 18, ("opl4", True): 20}
OPL_MELODIC = {("opl", False): 9, ("opl2", False): 9, ("opl3", False): 18,
               ("opl", True): 6, ("opl2", True): 6, ("opl3", True): 15,
               ("y8950", False): 9, ("y8950", True): 6,
               ("opl4", False): 18, ("opl4", True): 15}
# Furnace's channel labels. An OPL3 pair whose first channel is even can head a
# four-operator voice, and the system names it that way whether or not a note
# uses it.
OPL_DRUM_LABELS = (
    ("Kick/FM 7", "BD"), ("Snare", "SD"), ("Tom", "TM"), ("Top", "TP"),
    ("HiHat", "HH"),
)


def opl_channel_names(kind: str, drums: bool) -> tuple[list[str], list[str]]:
    """The long and short channel names of one OPL system (sysDef.cpp).

    The kick is the FM channel rhythm mode takes over, so its label keeps both
    names. An OPL3 shortens every channel to its number and marks the pairs
    that can head a four-operator voice.
    """
    total = OPL_CHANNELS[(kind, drums)]
    melodic = OPL_MELODIC[(kind, drums)]
    long_names: list[str] = []
    short_names: list[str] = []
    for ch in range(total):
        if drums and ch >= melodic:
            where = min(ch - melodic, len(OPL_DRUM_LABELS) - 1)
            long_name = (
                f"Kick/FM {melodic + 1}" if where == 0 else OPL_DRUM_LABELS[where][0]
            )
            short_name = OPL_DRUM_LABELS[where][1]
        elif kind == "opl3":
            long_name = (
                f"4OP {ch + 1}" if ch < 12 and not (ch & 1) else f"FM {ch + 1}"
            )
            short_name = str(ch + 1)
        else:
            long_name, short_name = f"FM {ch + 1}", f"F{ch + 1}"
        long_names.append(long_name)
        short_names.append(short_name)
    return long_names, short_names


def opll_channel_names(
    drums: bool, vrc7: bool = False,
) -> tuple[list[str], list[str]]:
    """The long and short channel names of the OPLL, VRC7 and drums sibling."""
    total = 6 if vrc7 else (OPLL_DRUMS_CHANNELS if drums else OPLL_CHANNELS)
    long_names: list[str] = []
    short_names: list[str] = []
    for ch in range(total):
        if drums and ch >= OPLL_DRUMS_MELODIC:
            where = ch - OPLL_DRUMS_MELODIC
            long_names.append(OPLL_DRUM_LABELS[where][0])
            short_names.append(OPLL_DRUM_LABELS[where][1])
        else:
            long_names.append(f"FM {ch + 1}")
            short_names.append(f"F{ch + 1}")
    return long_names, short_names

INS_OPM = 33
# DIV_INS_FM: the generic OPN instrument used by the YM2203.  Furnace reads
# its 0x70/0x90 fields from the same INS2 "FM" feature.
INS_FM = 1
# DIV_INS_OPL and its drums sibling. The OPL editor shows two operators per
# voice, and the drums instrument carries the snare/tom/cymbal/hi-hat
# operators in its operator slots.
INS_OPL = 14
INS_OPL_DRUMS = 32
# DIV_INS_OPLL: the YM2413's own instrument type, two operators and a preset.
INS_OPLL = 13
INS_MSM6258 = 35
INS_MSM6295 = 36
INS_ADPCMA = 37
INS_ADPCMB = 38
INS_SEGAPCM = 39
# DIV_INS_YMZ280B: one sample body per instrument (or a 120-slot note map
# when the module's instrument budget requires it).
INS_YMZ280B = 41
INS_RF5C68 = 42
# DIV_INS_MULTIPCM: the OPL4 wave slot's sample-map instrument.
INS_MULTIPCM = 28
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
# DIV_INS_SAA1099: standard macros, with the raw amplitude carried by the
# pan-left/pan-right macros and the envelope by ex1.
INS_SAA = 9
# DIV_INS_PCE: the HuC6280's 32x5-bit wavetable, through the same wave synth
# as the SCC. Its direct-D/A bodies use DIV_INS_AMIGA (INS_C352 above).
INS_PCE = 5
# DIV_INS_T6W28: the T6W28's plain square/noise instrument.  It shares the
# standard macro layout; only the platform system differs.
INS_T6W28 = 44
# DIV_INS_NES: the 2A03 pulse/triangle/noise and DPCM instrument.  The DPCM
# sample rides the same `amiga` sample feature (1-bit DPCM or 8-bit PCM).
INS_NES = 34
# DIV_INS_GB: the Game Boy's hardware envelope plus an optional wave-synth
# feature for the wavetable voice.
INS_GB = 2
# DIV_INS_VBOY: a wavetable instrument with the FD modulation table and the
# wave-synth feature.  VSU wave changes ride the pattern as 10xx effects.
INS_VBOY = 16
SMP_DEPTH_DPCM = 1
SMP_DEPTH_ADPCM_K = 7
SMP_DEPTH_ADPCM_A = 5
SMP_DEPTH_ADPCM_B = 6
SMP_DEPTH_YMZ_ADPCM = 3
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
    0x0F: 0x100, 0x22: 0x100,  # VSU
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
    # YMZ280B's Furnace core is 1.27x quieter than VGMPlay's at the same
    # system volume (measured on the two-chip Stage 4 module).
    0x0F: 1.27,
    # MAME shifts the voice product by 6 bits, the Furnace core by 7, so the
    # module needs twice the player's system volume to reach the same level.
    0x1D: 2.0,
    # VBOY's core has a 6x post-amp.  Measured against VGMPlay on Galactic
    # Pinball and Jack Bros: the module is about 2.2x quieter at unity.
    0x22: 2.2,
}
# MSM6258's player default is 0x11E; its Furnace core is 1.64x louder, so
# 0.61 lands the sample at the source level. The MSM6295 (0x18) is unity.
_CHIP_DEV = {CHIP_YM2151: 0x03, CHIP_SEGAPCM: 0x04, CHIP_MSM6295: 0x18,
             CHIP_MSM6258: 0x16, CHIP_C140: 0x1C, CHIP_C352: 0x27,
             CHIP_K053260: 0x1D, CHIP_YMZ280B: 0x0F, CHIP_VBOY: 0x22,
             CHIP_SMS: 0x00, CHIP_T6W28: 0x00,
             CHIP_K051649: 0x19, CHIP_K052539: 0x19,
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
    if chip == CHIP_MSM6295 and getattr(song.vgm, "msm6295_dual", False):
        # The player halves every instance when the header carries two,
        # even on a track that only writes to one of them.
        instances = max(instances, 2)
    scale = _FURNACE_VOLUME_SCALE.get(dev, 1.0)
    if chip == CHIP_YMZ280B:
        # VGMPlay does not halve a YMZ280B pair: both Stage 4 instances run
        # at full output. Furnace also mixes the two systems at full level,
        # so keep the per-chip value independent of the instance count.
        instances = 1
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
    internal order. An OPL patch takes the same feature with its own bit
    layout: no detune, a 6-bit level, and AM/vibrato/sustain/key-scale-rate on
    the operator instead of PMS/AMS on the channel.
    """
    b.raw(b"FM")
    size_off = len(b.b)
    b.u16(0)
    start = len(b.b)
    if getattr(patch, "opll", False):
        _write_opll_fm_feature_body(b, patch)
        b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")
        return
    if getattr(patch, "opl", False):
        _write_opl_fm_feature_body(b, patch)
        b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")
        return
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


def _write_opl_fm_feature_body(b: Buf, patch) -> None:
    """The 'FM' feature payload of a DIV_INS_OPL or DIV_INS_OPL_DRUMS voice."""
    four = bool(getattr(patch, "four", False)) or getattr(patch, "drum", False)
    # Four operator entries always: the reader takes its count from opCount, and
    # a drums instrument needs all four (snare, tom, cymbal, hi-hat).
    b.u8(4 | 0x10 | 0x20 | (0x40 | 0x80 if four else 0))
    b.u8(((patch.alg & 3) << 4) | (patch.fb & 7))
    # No channel LFO depth: the OPL sets AM and vibrato per operator, so the
    # tremolo/AMS/FMS byte stays 0. The 4-op instrument flag is the only other
    # bit the platform reads here, and it selects the pair layout on OPL3.
    b.u8(0)
    b.u8(32 if getattr(patch, "four", False) else 0)
    carriers = set(patch.carriers())
    for idx in range(4):
        op = patch.ops[idx]
        kvs = 2 if idx in carriers else 0
        b.u8(((op.ksr & 1) << 7) | (op.mult & 15))
        b.u8(((op.sus & 1) << 7) | (op.tl & 63))
        b.u8(((op.vib & 1) << 5) | (op.ar & 31))
        b.u8(((op.am & 1) << 7) | ((op.ksl & 3) << 5) | (op.dr & 31))
        b.u8(kvs << 5)
        b.u8(((op.sl & 15) << 4) | (op.rr & 15))
        b.u8(0)  # deep vibrato and SSG-EG are not OPL fields
        b.u8(op.ws & 7)


def _write_opll_fm_feature_body(b: Buf, patch) -> None:
    """The 'FM' feature payload of a DIV_INS_OPLL voice.

    The OPLL has two operators and no channel-level connection; ``alg`` in
    this feature is the per-operator sustain (hold) bit. Byte 3 carries the
    ROM preset nibble: zero selects the single user patch in ``$00-$07``,
    1-15 a factory voice. The carrier's TL stays 0 because the chip takes the
    level from the ``$3x`` volume nibble; the pattern volume column mirrors
    that nibble, so a note keeps the source level.
    """
    b.u8(2 | 0x10 | 0x20)
    b.u8(((patch.alg & 1) << 4) | (patch.fb & 7))
    # The chip's $03 bits 4 and 3 are the vibrato and tremolo depth.
    b.u8(((patch.ams & 3) << 3) | (patch.pms & 7))
    b.u8(patch.preset & 31)
    for idx in range(2):
        op = patch.ops[idx]
        b.u8(((op.ksr & 1) << 7) | (op.mult & 15))
        b.u8(((op.sus & 1) << 7) | (op.tl & 63))
        b.u8(((op.vib & 1) << 5) | (op.ar & 15))
        b.u8(((op.am & 1) << 7) | ((op.ksl & 3) << 5) | (op.dr & 15))
        b.u8(0)
        b.u8(((op.sl & 15) << 4) | (op.rr & 15))
        # Bit 3 of ssgEnv maps back to $00/$01 bit 5 (ET).
        b.u8(op.ssg_eg & 0x0F)
        b.u8(0)


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
        # OPLL tick-rate automation: volume (code 0), preset wave (code 3) and
        # repeated key-on retrigger (code 14).
        vol = getattr(patch, "vol_macro", ()) or ()
        if vol:
            _write_macro(
                b, 0, vol,
                speed=getattr(patch, "vol_speed", 1) or 1,
                loop=getattr(patch, "vol_loop", 255),
            )
        wave = getattr(patch, "wave_macro", ()) or ()
        if wave:
            _write_macro(
                b, 3, wave,
                speed=getattr(patch, "wave_speed", 1) or 1,
                loop=getattr(patch, "wave_loop", 255),
            )
        phase = getattr(patch, "phase_macro", ()) or ()
        if phase:
            _write_macro(
                b, 14, phase,
                speed=getattr(patch, "phase_speed", 1) or 1,
                loop=getattr(patch, "phase_loop", 255),
            )
    b.u8(255)  # stop
    b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")


def _write_op_group(
    b: Buf, op_index: int,
    entries: list[tuple[int, tuple[int, ...], int, bool, int]],
) -> None:
    """An ``O1``..``O4`` feature: the macro list of one operator.

    Furnace reads these in ``DivInstrument::readFeatureOx``. Each entry is
    (macro code, values, speed, invert_to_level, value mask). Code 6 is
    ``DIV_MACRO_OP_TL``, code 3 MULT and code 9 DT1. An operator macro has
    no delay: it runs from the key-on, and entry 0 is the value the note
    attacks with.

    For TL the stored byte is a level in Furnace's seven-bit domain, not the
    attenuation: the chip receives ``$4n = 63 - (S & 63)`` and an editor
    shows ``127 - S``. The model speaks ``$4n``. The writer therefore stores
    ``127 - $4n``, which puts the register and the editor both in range.
    Storing ``63 - $4n`` instead plays the same only because bit 6 is
    discarded, and the editor then shows 90-103 for a level of 26-37.
    Measured polarity, masks and hold behaviour are in "Operator macros" in
    ``docs/opl.md``.
    """
    entries = [e for e in entries if e[1]]
    if not entries:
        return
    b.raw(f"O{op_index + 1}".encode("ascii"))
    size_off = len(b.b)
    b.u16(0)
    start = len(b.b)
    # if you update the macro header, update this length as well (Furnace: 8)
    b.u16(8)
    for code, values, speed, to_level, mask in entries:
        stored = (
            [127 - (v & mask) for v in values]
            if to_level else [v & mask for v in values]
        )
        _write_macro(b, code, stored, speed=speed)
    b.u8(255)  # stop
    b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")


def _write_ins_opm(b: Buf, name: str, patch) -> None:
    """INS2 for an FM instrument of any family.

    Only the type differs: DIV_INS_OPL and its drums sibling read the same
    feature, whose payload `_write_fm_feature` picks from the patch.
    """
    opl = bool(getattr(patch, "opl", False))
    ins_type = (
        INS_OPLL if getattr(patch, "opll", False) else
        INS_OPL_DRUMS if getattr(patch, "drum", False) else INS_OPL
    ) if opl else (INS_FM if getattr(patch, "opn", False) else INS_OPM)
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(ins_type)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_fm_feature(b, patch)
    _write_ma(b, patch)
    # One O1..O4 feature per operator carries every macro that operator
    # needs. Separate features for TL and MULT would both target the same
    # operator and make the file order-dependent.
    op_entries: dict[
        int, list[tuple[int, tuple[int, ...], int, bool, int]]
    ] = {}
    for oi, values, speed in getattr(patch, "op_tl_macro", ()) or ():
        op_entries.setdefault(oi, []).append(
            (6, tuple(values), speed, True, 0x3F))
    for oi, values, speed in getattr(patch, "op_mul_macro", ()) or ():
        op_entries.setdefault(oi, []).append(
            (3, tuple(values), speed, False, 0x0F))
    for oi, values, speed in getattr(patch, "op_dt_macro", ()) or ():
        op_entries.setdefault(oi, []).append(
            (9, tuple(values), speed, False, 0x07))
    for oi, values, speed in getattr(patch, "op_dt2_macro", ()) or ():
        op_entries.setdefault(oi, []).append(
            (7, tuple(values), speed, False, 0x03))
    for oi in sorted(op_entries):
        _write_op_group(b, oi, op_entries[oi])
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_sample_map(
    b: Buf, name: str, first_sample: int, maps: list[int],
    ins_type: int = INS_C352, flags: int = 0x01,
) -> None:
    """120 note slots, one sample per slot.

    Each slot stores freq 48, which the reader adds to note 60. Every slice
    plays at C-4 and uses the sample's own rate. `first_sample` stays 0:
    Furnace 0.6.8.3 reads a nonzero base as an embedded sample count.

    `flags` selects the SM feature bits: 1 = useNoteMap, 2 = useSample,
    3 = both.  The AY sample path needs useSample too, so SSG PCM passes 3.
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
    b.u8(flags & 0xFF)
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


def _write_ins_pce(b: Buf, name: str, ins) -> None:
    """DIV_INS_PCE: the note starts on wavetable `ins.wave`.

    The wave-synth feature is identical to the SCC's; only the instrument type
    byte differs. `ins.waves`/`ins.pitch` carry per-tick wave and pitch motion
    as `std.wave`/`std.pitch`.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_PCE)
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


def _write_tone_macros(
    b: Buf, ins, *, wave: bool, reset: bool = False,
    ay_compat: bool = True,
) -> None:
    """MA feature for an SCC/AY/SAA instrument.

    File codes: 0 volume, 2 duty, 3 wave, 4 pitch, 5 ex1, 6 ex2, 12 panL,
    13 panR, 14 phase reset, 16 ex5.  The SAA1099 uses the panning macros for
    its raw amplitude nibbles and ex1 for the envelope control byte.
    """
    moving = (
        ins.vol or ins.pitch or (wave and ins.waves) or ins.noise or ins.mode
        or ins.env_shape or ins.env_period or ins.panl or ins.panr or ins.env
        or reset
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
            delay=getattr(ins, "vol_delay", 0),
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
        # AY mode bits (tone 1, noise 2, envelope 4). Before file version
        # 193 Furnace bumps every DIV_INS_AY wave-macro value by one while
        # loading (instrument.cpp), the same way 20xx carries the bits minus
        # one. Store the effect encoding for that format or the reloaded mode
        # is one step off: tone+env (5) would become noise+env (6) and a drum
        # body would play as noise.
        mode = ins.mode
        if ay_compat and FUR_VERSION < 193:
            mode = tuple((value - 1) & 0xFF for value in mode)
        _write_macro(
            b, 3, mode, speed=ins.mode_speed, loop=ins.mode_loop,
        )
    if ins.panl:
        _write_macro(
            b, 12, ins.panl, speed=ins.panl_speed, loop=ins.panl_loop,
            delay=getattr(ins, "panl_delay", 0),
        )
    if ins.panr:
        _write_macro(
            b, 13, ins.panr, speed=ins.panr_speed, loop=ins.panr_loop,
            delay=getattr(ins, "panr_delay", 0),
        )
    if getattr(ins, "env", ()):
        _write_macro(
            b, 5, ins.env, speed=ins.env_speed, loop=ins.env_loop,
            delay=getattr(ins, "env_delay", 0),
        )
    if ins.pitch:
        _write_macro(
            b, 4, ins.pitch, speed=ins.pitch_speed, loop=ins.pitch_loop,
            delay=getattr(ins, "pitch_delay", 0), width=2,
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


def _write_ins_psg(
    b: Buf, name: str, ins=None, ins_type: int = INS_STD,
) -> None:
    """Plain SN76489/T6W28 instrument: only the pitch macro is needed.

    The 4-bit volume is the pattern column, and noise mode rides on the 20xy
    effect, so a no-motion note gets a plain instrument.
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


def _write_ins_saa(b: Buf, name: str, ins=None) -> None:
    """DIV_INS_SAA1099: standard macros plus the raw pan pair and ex1.

    The volume column is 15 while the panning macros are present, so
    ``applyPan(15, (left << 4) | right)`` reproduces the source amplitude
    register exactly.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_SAA)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_tone_macros(b, ins or ToneIns(), wave=False, ay_compat=False)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_gb(b: Buf, name: str, ins) -> None:
    """DIV_INS_GB: hardware envelope plus an optional wavetable (voice 3).

    The GB feature packs NRx2's initial volume/direction/period, the length
    field and the soft-envelope flag.  A wavetable voice adds the same wave
    synth the SCC uses; its ``wave1`` picks the 32-nibble waveform.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_GB)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_tone_macros(b, ins, wave=bool(getattr(ins, "gb_wave", False)))
    b.raw(b"GB")
    gb_size = len(b.b)
    b.u16(0)
    gb_start = len(b.b)
    b.u8(
        ((int(getattr(ins, "gb_env_len", 0)) & 7) << 5)
        | ((int(getattr(ins, "gb_env_dir", 0)) & 1) << 4)
        | (int(getattr(ins, "gb_env_vol", 0)) & 15)
    )
    b.u8(int(getattr(ins, "gb_sound_len", 63)) & 0xFF)
    soft = bool(getattr(ins, "gb_soft_env", False))
    wave = bool(getattr(ins, "gb_wave", False))
    b.u8(1 if soft else 0)
    if soft or wave:
        b.u8(0)  # no hardware envelope sequence
    else:
        # The pattern volume column is the maximum, so the note starts at 15.
        # This sequence rewrites the hardware envelope to the source's initial
        # volume (and length/direction) before the key-on write, so the column
        # does not raise every drum to full level.
        b.u8(1)
        env_data = (
            ((int(getattr(ins, "gb_sound_len", 63)) & 0xFF) << 8)
            | ((int(getattr(ins, "gb_env_vol", 0)) & 15) << 4)
            | ((int(getattr(ins, "gb_env_dir", 0)) & 1) << 3)
            | (int(getattr(ins, "gb_env_len", 0)) & 7)
        )
        b.u8(0)  # DIV_GB_HWCMD_ENVELOPE
        b.u16(env_data)
    b.b[gb_size:gb_size + 2] = int(len(b.b) - gb_start).to_bytes(2, "little")
    if getattr(ins, "gb_wave", False):
        b.raw(b"WS")
        ws_size = len(b.b)
        b.u16(0)
        ws_start = len(b.b)
        b.u32(max(0, int(getattr(ins, "wave", 0))))  # wave1
        b.u32(max(0, int(getattr(ins, "wave", 0))))  # wave2
        b.u8(1)  # rateDivider
        b.u8(0)  # effect: none
        b.u8(1)  # enabled
        b.u8(0)  # global: restart on every note
        b.u8(0)  # speed
        b.u8(0)  # param1
        b.u8(0)  # param2
        b.u8(0)  # param3
        b.u8(0)  # param4
        b.b[ws_size:ws_size + 2] = int(len(b.b) - ws_start).to_bytes(2, "little")
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_vsu(
    b: Buf, name: str, ins, mod_table: bytes, wave_index: int = 0,
) -> None:
    """DIV_INS_VBOY: wavetable instrument plus the FD modulation table.

    The five VSU wavetables are selected by ``10xx`` pattern effects, so the
    instrument's wave-synth entry points at wave 0.  ``initModTableWithFirst
    Wave`` makes the VBOY platform load the FD table when channel 4 starts a
    note; Furnace's own VSU core then runs the sweep/modulation.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_VBOY)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    b.raw(b"FD")
    fd_size = len(b.b)
    b.u16(0)
    fd_start = len(b.b)
    b.u32(0)  # modSpeed
    b.u32(0)  # modDepth
    b.u8(1)   # initModTableWithFirstWave: load on channel 4 note-on
    table = bytes((mod_table + bytes(32))[:32])
    b.raw(table)
    b.b[fd_size:fd_size + 2] = int(len(b.b) - fd_start).to_bytes(2, "little")
    b.raw(b"WS")
    ws_size = len(b.b)
    b.u16(0)
    ws_start = len(b.b)
    b.u32(max(0, int(wave_index)))  # wave1
    b.u32(max(0, int(wave_index)))  # wave2
    b.u8(1)   # rateDivider
    b.u8(0)   # effect: none
    b.u8(1)   # enabled
    b.u8(0)   # global
    b.u8(0)   # speed
    b.u8(0)   # param1
    b.u8(0)   # param2
    b.u8(0)   # param3
    b.u8(0)   # param4
    b.b[ws_size:ws_size + 2] = int(len(b.b) - ws_start).to_bytes(2, "little")
    _write_tone_macros(b, ins, wave=False)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_ins_nes(b: Buf, name: str, ins=None) -> None:
    """DIV_INS_NES: a plain tone instrument (duty/volume ride the pattern).

    Duty/noise mode, envelope mode and the triangle linear counter are row
    effects, so the instrument only needs a name and (optionally) the pitch
    macro the shared tone writer already knows how to emit.
    """
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_NES)
    b.raw(b"NA")
    na_size = len(b.b)
    b.u16(0)
    na_start = len(b.b)
    b.strz(name)
    b.b[na_size:na_size + 2] = int(len(b.b) - na_start).to_bytes(2, "little")
    _write_tone_macros(b, ins or ToneIns(), wave=False)
    b.raw(b"EN")
    b.end_block(size_off, start)


def _write_sample_dpcm(
    b: Buf, name: str, data: bytes, c4_rate: int, loop: bool = False,
) -> None:
    """NES 1-bit DPCM body.

    Furnace's ``DivSample::init`` pads a DPCM sample to
    ``1 + ((bytes + 14) & ~15)``; a source DMC length is always ``16k+1``
    bytes, which lands on that same value, so the panel's length register
    round-trips.  The bit order is LSB-first, the same as the APU RAM.
    """
    bits = len(data) * 8
    length_dpcm = 1 + ((len(data) + 14) & ~15)
    body = bytes(data) + b"\xAA" * max(0, length_dpcm - len(data))
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(bits)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(SMP_DEPTH_DPCM)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    if loop:
        b.s32(0)
        b.s32(bits)
    else:
        b.s32(-1)
        b.s32(-1)
    for _ in range(4):
        b.u32(0xFFFFFFFF)
    b.raw(body)
    b.end_block(size_off, start)


def _write_wave(b: Buf, name: str, data: bytes, maximum: int = 255) -> int:
    """DivWavetable::putWaveData: the waveform an SCC/PCE instrument plays.

    ``maximum`` is the wavetable's height: 255 for the SCC's 8-bit wave, 31 for
    the PCE's 5-bit one.
    """
    ptr = len(b)
    size_off, start = b.begin_block(b"WAVE")
    b.strz(name)
    b.u32(len(data))
    b.u32(0)  # min
    b.u32(maximum)  # max
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
    swap_nibbles: bool = False,
) -> None:
    """VOX ADPCM. The stored length is a *nibble* count, never a byte count.

    `DivSample::initInternal` (sample.cpp) treats DIV_SAMPLE_DEPTH_VOX the same
    for every chip: `lengthVOX = (count + 1) / 2` bytes are allocated and read.
    A byte count leaves the back half of the sample unread. A 15,149-byte
    banked MSM6295 voice once came back as 7,575 and stopped mid-word.
    `swap_nibbles` pre-swaps the payload for the MSM6258 core, whose reader
    (`okim6258::data_w`) takes the low nibble first. MSM6295 copies dataVOX to
    ROM as it stands.
    """
    stored = data
    if swap_nibbles:
        stored = bytes((((x & 0x0F) << 4) | (x >> 4)) & 0xFF for x in data)
    count = len(stored) * 2
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
    render_masks: tuple[int, int, int, int] = (
        0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF,
    ),
) -> None:
    """Signed PCM with optional loop points. Depth 16 is int16 LE (mulaw).

    loop_mode is the SMP2 byte: 0 forward, 1 backward, 2 ping-pong.
    render_masks selects the systems whose sample memory receives the body.
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
    for mask in render_masks:
        b.u32(mask)
    b.raw(data[:n_frames * width])
    b.end_block(size_off, start)


def _write_sample_ymz_adpcm(
    b: Buf,
    name: str,
    data: bytes,
    c4_rate: int,
    loop: tuple[int | None, int | None] = (None, None),
    render_masks: tuple[int, int, int, int] = (
        0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF,
    ),
) -> None:
    """YMZ280B 4-bit ADPCM body.

    SMP2 stores a nibble count; the loader allocates ``(count+1)/2`` bytes and
    reads the payload as packed high-nibble-first samples.  Loop points are in
    nibbles, which is what the YMZ platform divides by two for its byte
    addresses.
    """
    samples = len(data) * 2
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(samples)
    b.u32(c4_rate)
    b.u32(c4_rate)
    b.u8(SMP_DEPTH_YMZ_ADPCM)
    b.u8(0)
    b.u8(0)
    b.u8(0)
    if loop[0] is not None and loop[1] is not None:
        b.s32(loop[0])
        b.s32(loop[1])
    else:
        b.s32(-1)
        b.s32(-1)
    for mask in render_masks:
        b.u32(mask)
    b.raw(data)
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


def ymz280b_flag_text(clock: int) -> str:
    """Furnace YMZ280B system flags: named clock select or custom clock."""
    clk = int(clock or 16934400)
    named = {
        14318182: 1,              # COLOR_NTSC * 4
        14187580: 2,              # COLOR_PAL * 16 / 5
        16000000: 3,
        16666667: 4,              # 50 MHz / 3
        14000000: 5,
    }
    if clk == 16934400:
        return ""
    sel = named.get(clk)
    if sel is not None:
        return f"clockSel={sel}"
    return f"customClock={clk}"


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


def rf5c68_flag_text(clock: int, chip_type: int) -> str:
    """RF5C68 system flags: chip type and clock.

    `chipType` 0 is the RF5C68 (FM Towns) and 1 the RF5C164 (Sega CD), the
    same split `DivPlatformRF5C68::setFlags` reads. Furnace's named clocks are
    the FM Towns 8 MHz and the Sega CD 12.5 MHz, so the usual rips need no
    custom clock.
    """
    parts = [f"chipType={1 if chip_type else 0}"]
    clk = clock or 8000000
    if clk == 8000000:
        parts.append("clockSel=0")
    elif clk == 10000000:
        parts.append("clockSel=1")
    elif clk == 12500000:
        parts.append("clockSel=2")
    else:
        parts.append(f"customClock={clk}")
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


def ay8910_flag_text(
    clock: int, chip_type: int, *, sunsoft5b: bool = False,
) -> str:
    """AY-8910 system flags: chip type and clock.

    A Famicom's AY is a Sunsoft 5B (SEL tied low), so the module uses the 5B
    type and keeps the logged CPU clock.  The analyzer already labelled its
    notes at half speed because the tracker's pitch table ignores that pin.
    Other files use YM2149 for types 0x10/0x11, else AY-8910.
    """
    if sunsoft5b:
        parts = ["chipType=2", f"customClock={clock}"]
        return "\n".join(parts)
    parts = [f"chipType={1 if chip_type in (0x10, 0x11) else 0}"]
    if module_clock(clock) != FURNACE_CLOCK_DEFAULT:
        parts.append(f"customClock={clock}")
    return "\n".join(parts)


def saa1099_flag_text(clock: int) -> str:
    """SAA1099 system flags: one of Furnace's three clock presets or a clock.

    clockSel 0 = 8 MHz (the Game Blaster/typical value), 1 = COLOR_NTSC*2
    and 2 = COLOR_PAL*8/5 (SAM Coupe).  Anything else rides as customClock.
    """
    clk = float(clock or SAA_DEFAULT_CLOCK)
    for sel, preset in enumerate(
        (SAA_CLOCK_8M, SAA_CLOCK_NTSC, SAA_CLOCK_PAL)
    ):
        if abs(clk - preset) <= 2:
            return f"clockSel={sel}"
    return f"customClock={int(round(clk))}"


def k051649_flag_text(clock: int) -> str:
    """SCC custom clock, or "" for Furnace's COLOR_NTSC/2 default."""
    if module_clock(clock) != FURNACE_CLOCK_DEFAULT:
        return f"customClock={clock}"
    return ""


def pce_flag_text(clock: int) -> str:
    """PC Engine clock, or "" for Furnace's COLOR_NTSC*2 default.

    The VGM header stores the PSG clock (COLOR_NTSC).  Furnace's PCE runs its
    pitch table from twice that with a divider of 64, so the module clock is
    the doubled value; the analyzer labels notes at the PSG clock with a
    divider of 32, which is the same table.
    """
    chip_clock = 2 * (clock or 3579545)
    default = 2 * (315_000_000.0 / 88.0)
    if abs(chip_clock - default) <= 1:
        return ""
    return f"customClock={chip_clock}"


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


# Furnace row effect codes used by the writer.
FX_PORTA_UP = 0x01
FX_PORTA_DOWN = 0x02


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
    # Keep channel state across the loop. Reset-on-loop cuts every held voice
    # and sample at the seam, which is not what the source VGM does.
    b.u8(2)  # loop modality: do nothing
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


def _emit_op_fx(
    grid: list[dict[int, Row]],
    song: Song,
    ev,
    points: tuple[tuple[int, int, int], ...],
    code: int,
    fx_cols: int,
    total_rows: int,
    chan: int | None = None,
    stats: dict | None = None,
    note_row: int | None = None,
) -> None:
    """Place an operator MULT/DT/DT2 change as a pattern effect.

    ``points`` are (sample, operator, value) already in the effect's own
    value domain. One row can carry one value per operator; a later write at
    the same row wins. The effect is allowed on the note's own row so the
    attack value is not delayed by a row.
    """
    if chan is not None and chan != ev.ch:
        ev = replace(ev, ch=chan)
    ch = ev.ch
    if ch >= len(grid) or not points:
        return
    if note_row is None:
        note_row = song.place(ev.sample, stats)[0]
    limit_row = total_rows
    if ev.end:
        limit_row = min(
            total_rows, max(note_row + 1, song.place(ev.end, stats)[0] + 1),
        )
    by_row: dict[tuple[int, int], tuple[int, int]] = {}
    for sample, op, value in points:
        row, delay = song.place(sample, stats)
        if row < note_row:
            row = note_row
        if not (0 <= row < limit_row):
            continue
        by_row[(row, op)] = (delay, value)
    # The OPM platform walks the effect's operator nibble through
    # orderedOps = {0,2,1,3} (fmsharedbase.h), while the instrument and the
    # logged registers use plain register order. Convert the register index
    # back to the effect's operator number, or 16xx/53xx/55xx hit the wrong
    # slot (op2's $50 becomes op1's $48).
    effect_ops = (0, 2, 1, 3)
    for (row, op), (delay, value) in sorted(by_row.items()):
        cell = grid[ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ch][row] = cell
        if 0 <= cell.note < 180 and row != note_row:
            continue          # a later note's key-on reloads the operator
        if row == note_row:
            delay = 0          # never delay the note that owns this row
        if delay > 0 and not any(c == 0xED for c, _v in (cell.fx or ())):
            _add_fx(cell, 0xED, delay, fx_cols, stats)
        x = effect_ops[op & 3] if code in (0x16, 0x53, 0x55) else op
        _add_fx(
            cell, code, (((x + 1) & 0x0F) << 4) | (value & 0x0F),
            fx_cols, stats,
        )


def _emit_steps(
    grid: list[dict[int, Row]],
    song: Song,
    ev,
    fx_cols: int,
    total_rows: int,
    e5_writes: dict[tuple[int, int], int],
    chan: int | None = None,
    stats: dict | None = None,
    note_row: int | None = None,
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
    if note_row is None:
        note_row = song.place(ev.sample, stats)[0]
    # One row can carry one transpose and one fine-tune step.  If the driver
    # wrote several f-nums inside one row, the last one is the row's pitch;
    # the intermediate values are shorter than a row and cannot be heard.
    by_row: dict[int, tuple[int, int]] = {}
    for t, delta in ev.pitch_steps:
        row, delay = song.place(t, stats)
        if row <= note_row:
            row = note_row + 1
        if 0 <= row < total_rows:
            by_row[row] = (delta, delay)
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
        delta, delay = by_row[row]
        next_row = targets[idx + 1] if idx + 1 < len(targets) else total_rows
        next_row = min(next_row, limit_row)
        cell = grid[ch].get(row)
        if cell is not None and 0 <= cell.note < 180:
            # A later note owns this row and carries its own pitch.
            continue
        # A pitch step written inside a row must land on the driver's tick,
        # not at the row start: at fast tempos the row is several
        # milliseconds long and the difference is an audible early bend.
        if delay > 0 and not any(c == 0xED for c, _v in (cell_at(row).fx or ())):
            _add_fx(cell_at(row), 0xED, delay, fx_cols, stats)
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
        # The range correction above is the minimum that fits. It parks E5 on
        # its edge, where the byte has no room left to carry a later step or
        # to correct a ramp's rounding. A residual of whole semitones
        # therefore belongs in the transpose.
        rest = desired - 64 * semis
        if rest and rest % 64 == 0:
            extra = int(rest / 64)
            if abs(semis + extra) <= 15:
                semis += extra
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


def _fx_keep(cell: Row, drop: tuple[int, ...] = ()) -> None:
    """Rebuild a row's effect list without the `drop` codes, keeping order."""
    if cell.fx:
        cell.fx = [(c, v) for c, v in cell.fx if c not in drop]


# A step of this many 1/64-semitone units between two held KC/KF values is a
# hard pitch change (a legato note), not the start of a slide. Real slides and
# hand-written vibrato move in small steps per row and keep their 01xx/02xx
# ramps; only jumps of about a semitone and more become legato.
_SWEEP_JUMP_UNITS = 56


def _emit_jump_row(
    grid: list[dict[int, Row]], ch: int, fx_cols: int, total_rows: int,
    row: int, note_row: int, target_abs: float, div: float,
    e5_note: int, chunks: dict[int, int],
    e5_lo: int, e5_hi: int,
    e5_writes: dict[tuple[int, int], int],
    note_rows: list[int] | None = None,
    stats: dict | None = None, last_rate: int = 0,
    max_rows: int | None = None,
) -> tuple[float, int] | None:
    """Land a hard pitch jump (a legato note change) on `row`.

    Whole semitones go through E8xx/E9xx (quick legato, cumulative until the
    next note), and the fine remainder through E5xx. `target_abs` is the
    absolute pitch offset the row should reach, and `chunks` maps the rows
    this note's earlier jumps used to the semitones they carry.

    E8xx resets the engine's accumulated slide displacement. Measured: a jump
    after a glide lands on note + transpose + fine, not on what the glide had
    reached. The target is therefore measured against the transpose alone,
    and a running 01xx/02xx is stopped on the same row or it would resume
    under the jump.

    A jump bigger than +-15 semitones needs several rows. `max_rows` is how
    many the trajectory leaves it before the next step owns one, and a
    transpose that cannot land in time is not placed at all. When a later
    jump lands inside the span an earlier one still occupies, its chunks are
    picked up again from that row. What the transpose delivered before `row`
    stays, and the rest is re-planned, which keeps a quick reversal from
    stranding the voice on the first jump's target.

    The note's own key-on row is always allowed; a later note's row is not.
    Returns (the transposition this note carries once every chunk has landed,
    in units, and the new E5 byte) when the jump was placed, or None when no
    usable row was found (nothing is written in that case).
    """
    if row < 0 or row >= total_rows:
        return None
    # The semitones the transpose has already delivered before this row.
    held = sum(v for r, v in chunks.items() if r < row)
    # What E5 has to carry once E8xx/E9xx have done the whole semitones. The
    # E5 byte is absolute (e5_note is the note's own fine tune). Its range
    # does not depend on any earlier E5 write.
    off = target_abs - 64.0 * held
    w = int(round(off / 64.0))
    lo_u = (e5_lo - e5_note) / div
    hi_u = (e5_hi - e5_note) / div
    fine = off - 64.0 * w
    # Move a whole semitone from E5 into the transpose when the rest does not
    # fit the E5 byte. Raising w lowers fine, and vice versa.
    if fine > hi_u:
        w += int(math.ceil((fine - hi_u) / 64.0))
    elif fine < lo_u:
        w -= int(math.ceil((lo_u - fine) / 64.0))
    fine = off - 64.0 * w
    # The E8 writes move at most +-15 semitones each and may need several
    # rows. Check every row before writing anything.
    if note_rows:
        limit = next((r for r in sorted(note_rows) if r > row), total_rows)
    else:
        limit = total_rows
    plan: list[tuple[int, int]] = []
    cur_row = row
    rem = w
    while True:
        step = max(-15, min(15, rem)) if rem else 0
        plan.append((cur_row, step))
        rem -= step
        if not rem:
            break
        cur_row += 1
    for jrow, _step in plan:
        if jrow < 0 or jrow >= total_rows or jrow >= limit:
            return None
        if max_rows is not None and jrow - row + 1 > max_rows:
            # The next step owns a row this transpose would still need, and a
            # half-landed jump is worse than leaving the row to that step.
            return None
        cell = grid[ch].get(jrow)
        if jrow != note_row and cell is not None and 0 <= cell.note < 180:
            return None
    # Drop the chunks this jump replaces, both from the grid and from the
    # bookkeeping. A reversal would otherwise leave the old transpose in
    # force.
    for jrow in [r for r in chunks if r >= row]:
        del chunks[jrow]
        cell = grid[ch].get(jrow)
        if cell is not None and cell.fx:
            cell.fx = [(c, v) for c, v in cell.fx if c not in (0xE8, 0xE9)]
    new_e5 = max(e5_lo, min(e5_hi, int(round(e5_note + fine * div))))
    for idx, (jrow, step) in enumerate(plan):
        cell = grid[ch].get(jrow)
        if cell is None:
            cell = _blank_row()
            grid[ch][jrow] = cell
        if idx == 0 and last_rate:
            # The glide dies here: a rate left running would move the pitch
            # after the jump.
            _fx_keep(cell, (0x01, 0x02))
            _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0,
                    fx_cols, stats)
        if step:
            chunks[jrow] = step
            _add_fx(cell, 0xE8 if step > 0 else 0xE9, abs(step),
                    fx_cols, stats)
    # Record even when the byte equals the note's own. The replay walks a
    # running value and deduplicates an unchanged request there.
    e5_writes[(ch, row)] = new_e5
    return 64.0 * sum(chunks.values()), new_e5


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
    """Encode a key-on pitch trajectory into row effects.

    The source's KC/KF stream mixes two kinds of motion, read one row at a time:
    a row renders a single straight line, so only its last write can sound. A run
    of small steps is a real slide or hand-written vibrato and keeps its per-row
    01xx/02xx ramp, and so does a chain of big steps that carries on row after
    row. A step of about a semitone or more with still rows around it is the next
    note under legato: an instant jump on its own row, E8xx/E9xx for the whole
    semitones and E5xx for the remainder. Jump and slide are segmented, and
    either may follow the other inside one note.
    """
    if chan is not None and chan != ev.ch:
        ev = replace(ev, ch=chan)
    pts = list(ev.sweep)
    if not pts or ev.ch >= len(grid):
        return
    row_len = song.samples_per_row
    tick = row_len / max(1, song.speed)
    start = ev.sample
    # The release tail keeps gliding. The trajectory runs to its last KC/KF
    # write. A later note's row clips it below.
    end = pts[-1][0]
    # E5 and 01xx/02xx move the platform's pitch value, 1/128 semitone in
    # linear mode. The trajectory is in 1/64 semitones. OPN and PCM events
    # (sweep_div 2) need two engine units per trajectory unit. OPM keeps its
    # historical one-unit mapping.
    div = 2.0 if (ev.pcm or ev.sweep_div >= 2.0) else 1.0

    def e5_byte(offset: float) -> int:
        # E5xx is the note's fine tune; fine-tune bytes and slide units
        # advance the same pitch value.
        return max(ev.e5_lo, min(ev.e5_hi, int(round(ev.e5 + offset * div))))

    row0, _d0 = song.place(start, stats)
    if end - start < tick:
        # Shorter than a tick: no room to ramp. The driver's jump still has to
        # land, and an instant transpose on the next row is the closest the
        # engine can get.
        final = float(_traj_at(pts, int(end)))
        if _emit_jump_row(
            grid, ev.ch, fx_cols, total_rows, row0 + 1, row0,
            final, div, ev.e5, {}, ev.e5_lo, ev.e5_hi,
            e5_writes, note_rows.get(ev.ch), stats,
        ) is None:
            _bump(stats, "legato_skipped")
        return
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

    # Row-level view of the trajectory. Two writes inside one row cannot be
    # told apart by the engine (a row renders a single straight line). Each
    # row keeps its last value. Classifying on rows also collapses a one-row
    # glitch, a hard step reversed before the row ends, into no movement.
    per_row: list[list[int]] = []  # [row, first pts index, last pts index]
    for idx, (s, _d) in enumerate(pts):
        r = song.place(s, stats)[0]
        if per_row and per_row[-1][0] == r:
            per_row[-1][2] = idx
        else:
            per_row.append([r, idx, idx])

    # Classify each row's value against the one held before it. A step of about a
    # semitone or more is a note change under legato when the rows around it stay
    # still; when the row next door travels the same way and as far, the driver is
    # gliding. Hand-written vibrato moves in smaller steps than either. Jumps are
    # their own one-row segment; the other rows share a slide run anchored at the
    # pitch held before the run.
    def val(e: int) -> float:
        """The pitch one row's last write left; 0 outside the trajectory."""
        return float(pts[per_row[e][2]][1]) if 0 <= e < len(per_row) else 0.0

    # Segment: kind, first row index into per_row, last one, and the pitch held
    # before the segment starts.
    segs: list[tuple[str, int, int, float]] = []
    prev_held = 0.0
    run_first = -1
    anchor = 0.0
    for e, entry in enumerate(per_row):
        row = entry[0]
        step = val(e) - val(e - 1)
        # A neighbour continues this row's motion only when it moves the very
        # next row: separate notes leave rows of silence between their steps.
        ahead = (
            val(e + 1) - val(e)
            if e + 1 < len(per_row) and per_row[e + 1][0] == row + 1 else 0.0
        )
        behind = (
            val(e - 1) - val(e - 2)
            if e >= 1 and per_row[e - 1][0] == row - 1 else 0.0
        )
        big = abs(step) >= _SWEEP_JUMP_UNITS
        chain = (abs(ahead) >= _SWEEP_JUMP_UNITS and ahead * step > 0
                 or abs(behind) >= _SWEEP_JUMP_UNITS and behind * step > 0)
        hard = big and not chain
        d = val(e)
        if hard:
            if run_first >= 0:
                segs.append(("slide", run_first, e - 1, anchor))
                run_first = -1
            segs.append(("jump", e, e, prev_held))
        elif run_first < 0:
            run_first = e
            anchor = prev_held
        prev_held = d
    if run_first >= 0:
        segs.append(("slide", run_first, len(per_row) - 1, anchor))

    # The engine's pitch value is (E5 - note fine tune) / div + xpose. slide
    # accrues what the written 01xx/02xx parameter actually delivers; a jump
    # resets it to zero (E8xx relearns the note's own pitch).
    e5w = ev.e5
    xpose = 0.0
    slide = 0.0
    last_rate = 0
    # The rows this note's jumps put a transpose on, with the semitones each
    # carries. A jump landing inside an earlier jump's span re-plans from its
    # own row. A quick reversal therefore cannot strand the voice on the
    # first target.
    chunks: dict[int, int] = {}

    def pitch_now() -> float:
        return (e5w - ev.e5) / div + xpose + slide

    # A multi-row jump leaves its later E8 chunks on following rows; a slide
    # must not start until they have all landed.
    jump_last_row = row0 - 1

    for seg_i, (kind, a, b, anchor) in enumerate(segs):
        if kind == "jump":
            jrow = per_row[a][0]
            s = pts[per_row[a][1]][0]
            d = float(pts[per_row[a][2]][1])
            if crossed and s >= end:
                continue
            if jrow <= row0:
                # The step is the note's own key-on write, or a stray write in
                # front of it: that row already sounds the note's pitch.
                continue
            if jrow >= total_rows:
                _bump(stats, "legato_skipped")
                continue
            c = grid[ev.ch].get(jrow)
            if c is not None and 0 <= c.note < 180:
                # Rounding put this step on a later note's row. That note
                # re-keys with its own pitch, and the step is inaudible.
                continue
            # Rows this jump may spend before the next segment owns one. A
            # transpose of more than +-15 semitones per row has no room here,
            # and half a jump is worse than none.
            nxt = (per_row[segs[seg_i + 1][1]][0]
                   if seg_i + 1 < len(segs) else total_rows)
            res = _emit_jump_row(
                grid, ev.ch, fx_cols, total_rows, jrow, row0,
                float(d), div, ev.e5, chunks,
                ev.e5_lo, ev.e5_hi, e5_writes,
                note_rows.get(ev.ch), stats,
                last_rate=last_rate, max_rows=max(1, nxt - jrow),
            )
            if res is None:
                _bump(stats, "legato_skipped")
                continue
            xpose, e5w = res
            # E8xx resets the engine's accumulated slide displacement.
            slide = 0.0
            last_rate = 0
            jump_last_row = max([jump_last_row, jrow] + sorted(chunks))
            continue

        # A slide run: ramp from the held anchor through this run's points.
        i_from = per_row[a][1]
        i_to = per_row[b][2]
        first_s = pts[i_from][0]
        if crossed and first_s >= end:
            continue
        run_pts = [
            (pts[j][0], float(pts[j][1])) for j in range(i_from, i_to + 1)
        ]
        step_times = [s for s, _d in run_pts]

        def traj_run(t: int) -> float:
            v = anchor
            for s, d in run_pts:
                if s > t:
                    break
                v = d
            return v

        k = per_row[a][0] - row0
        k = max(k, jump_last_row - row0 + 1, 0)
        k_last = per_row[b][0] - row0
        if b + 1 < len(per_row):
            # Never run under the next segment; its jump owns that row.
            k_last = min(k_last, per_row[b + 1][0] - row0 - 1)
        if k_last < k:
            continue
        run_last: int | None = None
        while k <= k_last:
            rs = base + k * row_len
            if rs >= end:
                break
            re_full = base + (k + 1) * row_len
            re = min(re_full, end)
            _bump(stats, "sweep_rate_merged",
                  max(0, bisect_left(step_times, re_full)
                      - bisect_left(step_times, rs) - 1))
            first_row = seg_i == 0 and k == 0
            cell_k = grid[ev.ch].get(row0 + k)
            # A row runs its effects from the tick its ED delay names. With no
            # slide already carrying the voice into this row, the engine
            # applies the rate once per tick from the delay to the end of the
            # row. The row is therefore worth (speed - ed) ticks however far
            # inside it the key-on sits. Rating it by the wall clock between
            # the key-on and the row end under-delivers a fast bend, and the
            # voice then holds flat for its whole note. A row a running rate
            # enters keeps its full span. Those first ticks move at the old
            # rate, and the row still travels.
            ed = _cell_ed(cell_k) if last_rate == 0 else 0
            t_eff = start if first_row else rs
            want = 0.0 if first_row else traj_run(int(rs + tick))
            cur = pitch_now()
            if abs(want - cur) >= 6:
                if cell_k is None or not (0 <= cell_k.note < 180):
                    # A later note's E5 sets its own attack and wins its row.
                    e5w = e5_byte(want - xpose - slide)
                    e5_writes[(ev.ch, row0 + k)] = e5w
                    cur = pitch_now()
                else:
                    _bump(stats, "e5_skipped")
            # The engine slides for the whole row. The last row of the run is
            # rated over its full span against the held value.
            is_last = k == k_last
            span = (re_full - t_eff) / tick if is_last else (re - t_eff) / tick
            if ed:
                # The ticks the row actually sounds, not the ones its wall-clock
                # length suggests.
                span = float(song.speed - ed)
            want_end = (
                0.0 if (first_row and ev.pcm)
                else traj_run(int(re_full if is_last else re))
            )
            rate = 0.0
            if span > 0:
                # FM advances one kc_kf unit per tick per parameter unit. PCM
                # moves 1/128 st per unit, and its written parameter is
                # doubled.
                limit = 255.0 / div
                rate = max(-limit, min(limit, (want_end - cur) / span))
            param = max(-255, min(255, int(round(rate * div))))
            if param != last_rate or param != 0:
                # The effect must be written on every row or the glide freezes.
                cell = grid[ev.ch].get(row0 + k)
                if cell is None:
                    cell = _blank_row()
                    grid[ev.ch][row0 + k] = cell
                # Drop the opposite-direction slide first, or both survive and
                # the net slide depends on column order.
                cell.fx = [(c, v) for c, v in
                           (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                           if c not in (0x01, 0x02)]
                if param:
                    _add_fx(cell, 0x01 if param > 0 else 0x02,
                            min(255, abs(param)), fx_cols, stats)
                else:
                    _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0,
                            fx_cols, stats)
                last_rate = param
            # Accrue the movement the written parameter actually makes.
            # Rounding error compounds over long glides.
            engine_rate = param / div
            # Fidelity: compare the row's straight line with the driver's steps.
            if stats is not None and span > 0:
                bad_here = False
                for j in range(4):
                    t = int(rs + (re_full - rs) * (2 * j + 1) / 8.0)
                    if t <= start:
                        continue
                    actual = traj_run(t)
                    model = cur + engine_rate * span * (2 * j + 1) / 8.0
                    dev = abs(model - actual)
                    if dev > 6:
                        bad_here = True
                        _bump(stats, "sweep_dev_bad")
                    if dev > stats.get("sweep_dev_max", 0):
                        stats["sweep_dev_max"] = int(round(dev))
                if bad_here:
                    _bump(stats, "sweep_dev_rows")
            slide += engine_rate * span
            run_last = k
            k += 1

        if run_last is None:
            continue
        # Freeze the ramp once the run is over, or it would keep gliding
        # under the next jump or through the hold that follows.
        stop_row = row0 + run_last + 1
        cell = None
        foreign = False
        if 0 <= stop_row < total_rows:
            cell = grid[ev.ch].get(stop_row)
            foreign = cell is not None and 0 <= cell.note < 180
        if last_rate and 0 <= stop_row < total_rows:
            if cell is None:
                cell = _blank_row()
                grid[ev.ch][stop_row] = cell
            if not any(c in (0x01, 0x02) for c, _v in (cell.fx or ())):
                _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0,
                        fx_cols, stats)
            last_rate = 0
        if seg_i != len(segs) - 1:
            continue
        # The final run pins the last held value. Ramp rounding can leave a
        # few units of error, and the E5 byte corrects it.
        if foreign or not (0 <= stop_row < total_rows):
            continue
        final = float(pts[per_row[b][2]][1])
        pin = e5_byte(final - xpose - slide)
        if pin == e5w:
            continue
        if cell is None:
            cell = _blank_row()
            grid[ev.ch][stop_row] = cell
        e5_writes[(ev.ch, stop_row)] = pin
        e5w = pin


def _assign_pcm_rows(
    song: Song, total_rows: int, last_sample: int, stats: dict | None = None,
    clip_row: int | None = None, clip_cut: int | None = None,
) -> dict[int, tuple[int, int]]:
    """Row and delay for each playable PCM note-on and its note-off.

    A free row is taken. A taken row pushes the trigger forward until the
    next trigger, or keeps the later write. A note-off moves after its
    trigger and before the next trigger. When a later trigger comes first,
    its key-on stops the previous sample and the off is redundant. A trigger
    just before the loop wrap that rolls past the last row moves back onto
    it, the same way a note event does.
    """
    by_ch: dict[int, list] = {}
    for ev in song.events:
        if ev.pcm and ev.ch >= 0:
            by_ch.setdefault(ev.ch, []).append(ev)
    limit = min(total_rows, song.place(last_sample, stats)[0] + 1)
    out: dict[int, tuple[int, int]] = {}

    def wrapped(row: int, delay: int, sample: int) -> tuple[int, int]:
        if (
            clip_row is not None and clip_cut is not None
            and row >= total_rows and row <= clip_row + 1
            and sample < clip_cut
        ):
            return clip_row, 0
        return row, delay

    for evs in by_ch.values():
        evs.sort(key=lambda e: (e.sample, 0 if e.on else 1))
        ons = [e for e in evs if e.on]
        ideals = [song.place(e.sample, stats) for e in ons]
        placed: dict[int, int] = {}
        for i, ev in enumerate(ons):
            row, delay = ideals[i]
            row, delay = wrapped(row, delay, ev.sample)
            if row < 0 or row >= total_rows:
                continue
            if row in placed:
                prev = ons[placed[row]]
                if prev.sample == ev.sample:
                    # Two writes at one source sample. The later start wins;
                    # a separate row cannot reproduce the same instant.
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
            off_row, off_delay = wrapped(off_row, off_delay, off.sample)
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


def _phrase_pattern_len(song, last_row: int, total_rows: int) -> int:
    """A pattern length that is a whole musical phrase when the budget allows.

    Eight quarter notes is 32 pulses. On a grid of `pulse` rows per 16th
    that is `32 * pulse` rows. The golden covers use one 8/4 or 16/4
    phrase per pattern. An order boundary is then also a musical boundary.
    Longer tracks step up to 16/4 and beyond to stay inside 256 orders.
    """
    pulse = max(1.0, float(getattr(song, "row_subdiv", 1) or 1))
    base = min(PAT_LEN_MAX, int(round(32 * pulse)))
    if total_rows <= 0:
        return _pattern_len(last_row, PAT_LEN_MAX)
    # Leave room for the loop split and a Bxx-only order. The hard limit is
    # 256 orders.
    for mult in range(1, 9):
        cand = min(PAT_LEN_MAX, base * mult)
        orders = (total_rows + cand - 1) // cand
        if orders <= 250:
            return _pattern_len(last_row, cand)
        if cand == PAT_LEN_MAX:
            break
    return _pattern_len(last_row, PAT_LEN_MAX)


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
                    ch_offset: int = 0, stats: dict | None = None,
                    vsu_wave_base: int = 0) -> tuple[
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
        if ev.on and (ev.pcm or ev.dac) and ev.end:
            last_sample = max(last_sample, ev.end)
    for entry in (getattr(song, "channel_fx", ()) or ()):
        last_sample = max(last_sample, entry[0])
    for entry in (getattr(song, "chip_fx", ()) or ()):
        last_sample = max(last_sample, entry[0])
    # An adjusted loop carries the chosen rows. A raw VGM marker is read with
    # the writer's own cell placement. The row fields win when present.
    adjusted = song.loop_sample is not None and song.loop_end_sample is not None
    if song.loop_start_row is not None:
        loop_row = song.loop_start_row
    elif song.loop_sample is None:
        loop_row = 0
    elif adjusted:
        loop_row = song.place(song.loop_sample)[0]
    else:
        loop_row = song.row_of(song.loop_sample)
    if loop_row < 0:
        loop_row = 0
    # The adjusted loop end is the last row the module plays. Rows past it
    # belong to the next iteration, and keeping the source tail would play
    # them before the jump and strike the head twice at the seam.
    content_end = None
    content_cut = None
    if adjusted:
        content_end = song.loop_end_row
        if content_end is None:
            content_end = song.place(song.loop_end_sample)[0]
        if content_end < loop_row:
            content_end = None
        else:
            content_cut = song.loop_cut_sample
    if content_end is not None:
        total_rows = content_end + 1
    else:
        content_end = song.place(last_sample, stats)[0]
        total_rows = max(1, content_end + 2)

    grid: list[dict[int, Row]] = [dict() for _ in range(n_ch)]
    if stats is not None:
        stats["rows"] = max(stats.get("rows", 0), total_rows)

    def loop_clip(row: int, delay: int, sample: int) -> tuple[int, int]:
        """Keep a pre-wrap event on the last loop row when its ED would roll.

        The row after the loop end is the next iteration's first row. The
        events just before the wrap belong to the end of this one. Playing
        them a fraction of a row early keeps them in the loop instead of
        dropping them for good.
        """
        if (
            content_cut is not None and content_end is not None
            and sample < content_cut and row > content_end
            and row <= content_end + 1
        ):
            speed = max(1, song.speed)
            tick = song.samples_per_row / speed
            offset = (sample - song.t0) / tick - content_end * speed
            return content_end, min(speed - 1, max(0, int(round(offset))))
        return row, delay

    def loop_start(row: int, delay: int, sample: int) -> tuple[int, int]:
        """Keep a post-marker event on the loop's first row.

        The source marker can fall inside the row before the chosen start
        when the grid is coarse. That row belongs to the intro. Leaving the
        event there drops it after the first iteration. Moving it to the
        loop start keeps it in every iteration.
        """
        marker = song.loop_marker_sample
        if (
            content_cut is not None and marker is not None
            and sample >= marker and row < loop_row
            and row >= loop_row - 1
        ):
            return loop_row, 0
        return row, delay

    row_samples = float(getattr(song, "samples_per_row", 0) or 0)
    # A condensed row can catch two tone notes at once. Pin the earlier one to
    # the end of the previous row instead of letting the later note overwrite
    # it. Only a note that ends before the next one starts and has no row
    # volume steps of its own moves; its opening effects move with it. The
    # previous row's note-off is redundant because the moved key-on cuts that
    # note, so the off is skipped below.
    note_row_shift: dict[int, tuple[int, int]] = {}
    fx_row_shift: dict[tuple[int, int], tuple[int, int]] = {}
    shifted_rows: set[tuple[int, int]] = set()
    tone_rows: dict[int, dict[int, list[tuple[object, int]]]] = {}
    for ev in song.events:
        if not (ev.on and ev.tone_chip):
            continue
        ev_ch = ev.ch + ch_offset
        if ev_ch < 0 or ev_ch >= n_ch:
            continue
        if content_cut is not None and ev.sample >= content_cut:
            continue
        row, delay = loop_clip(*song.place(ev.sample, stats), ev.sample)
        row, delay = loop_start(row, delay, ev.sample)
        if 0 <= row < total_rows:
            tone_rows.setdefault(ev_ch, {}).setdefault(row, []).append((ev, delay))
    used_rows = {
        (ch, row) for ch, rows in tone_rows.items() for row in rows
    }
    shift_delay = min(255, max(1, song.speed - 1))
    for ev_ch, rows in tone_rows.items():
        for row in sorted(rows):
            group = rows[row]
            if len(group) < 2:
                continue
            group.sort(key=lambda item: item[0].sample)
            for (prev, _pdelay), (ev, _edelay) in zip(group, group[1:]):
                if (
                    getattr(prev, "tone_chip", 0) not in (1, 2, 3, 4, 5, 8)
                    or prev.vol_pts
                    or not prev.end
                    or prev.end > ev.sample
                    or (row_samples and prev.end - prev.sample >= row_samples)
                ):
                    continue
                dest = row - 1
                if dest >= 0 and (ev_ch, dest) not in used_rows:
                    note_row_shift[id(prev)] = (dest, shift_delay)
                    fx_row_shift[(ev_ch, prev.sample)] = (dest, shift_delay)
                    shifted_rows.add((ev_ch, dest))
                    used_rows.add((ev_ch, dest))
                    _bump(stats, "note_shifted")
                    continue
                if len(group) != 2:
                    continue
                # The row before holds the previous note. Move the later note
                # to the next row instead so both still play; the earlier note
                # is short enough to finish before it.
                dest = row + 1
                if dest >= total_rows or (ev_ch, dest) in used_rows:
                    continue
                note_row_shift[id(ev)] = (dest, 0)
                fx_row_shift[(ev_ch, ev.sample)] = (dest, 0)
                used_rows.add((ev_ch, dest))
                _bump(stats, "note_shifted")

    # Chip-wide effects are placed after the notes: the LFO speed/pulse has
    # to ride the tick of the key-on it belongs to (see _place_chip_fx).
    # The tick delay a tone note on a row will use. A row's ED applies to
    # every event on it, so an effect that lands later in the row than the
    # note cannot stay there without pushing the note late.
    note_delay: dict[tuple[int, int], int] = {}
    for ev_ch, rows in tone_rows.items():
        for row, group in rows.items():
            for ev, delay in group:
                shifted = note_row_shift.get(id(ev))
                dest, dest_delay = shifted if shifted is not None else (row, delay)
                key = (ev_ch, dest)
                prev = note_delay.get(key)
                note_delay[key] = dest_delay if prev is None else min(prev, dest_delay)
    for sample, ch, cmd, val in getattr(song, "channel_fx", ()) or ():
        # VSU wave changes carry a sentinel index into the source wave list;
        # resolve it against the module-wide block order now that the other
        # families' waves are ahead of it.
        if cmd == 0x10 and val >= 0x1000:
            idx = vsu_wave_base + (val & 0x0FFF)
            if idx > 0xFF:
                _bump(stats, "vsu_wave_dropped")
                continue
            val = idx
        ch += ch_offset
        if ch < 0 or ch >= n_ch:
            continue
        if content_cut is not None and sample >= content_cut:
            continue
        # Same placement as the note on this sample, including its ED delay.
        # row_of() rounds to the nearest row and would land an effect on a
        # different row from the note whenever speed is more than 1.
        shifted = fx_row_shift.get((ch, sample))
        if shifted is not None:
            row, delay = shifted
        else:
            row, delay = loop_clip(*song.place(sample, stats), sample)
            row, delay = loop_start(row, delay, sample)
        if row < 0 or row >= total_rows:
            continue
        # The note on this row starts at its own tick; this effect happens
        # after it, so put it at the next row rather than delay the note.
        note_tick = note_delay.get((ch, row))
        if note_tick is not None and delay > note_tick and row + 1 < total_rows:
            row, delay = row + 1, 0
        cell = grid[ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ch][row] = cell
        # Two different values of one effect inside a row (a duty change
        # shortly after the note opened) cannot share the cell: keep the
        # first and move the later one to the next row.
        if any(
            c == cmd and v != val and c != EMPTY
            for c, v in (cell.fx or ())
        ):
            nrow = row + 1
            if nrow >= total_rows:
                continue
            row, delay = nrow, 0
            cell = grid[ch].get(row)
            if cell is None:
                cell = _blank_row()
                grid[ch][row] = cell
        _add_fx(cell, cmd, val, fx_cols, stats)
        if delay and _cell_ed(cell) == 0:
            _add_fx(cell, 0xED, delay, fx_cols, stats)

    # E5 is channel state. Collect it per row and replay in row order, back
    # to the neutral 0x80. One note's fine tune cannot leak into the next.
    note_e5: dict[tuple[int, int], int] = {}
    # Mid-note carrier-TL fades ride the volume column as (max - vol) against
    # the instrument TL. Every FM note-on re-asserts the top of the column when
    # any note fades. The ceiling is the chip's: 127 on the OPM and OPN family,
    # 63 on an OPL (GET_VOLMAX), 15 on the YM2413. One module-wide value serves
    # a single family; a combined one needs the maximum of each channel.
    fm_vol_max = max(1, int(getattr(song, "fm_vol_max", 127)))
    vol_max = _channel_vol_max(song, n_ch, fm_vol_max, ch_offset)
    fm_tl = any(
        ev.on and not ev.pcm and ev.tl_pts and 0 <= ev.ch + ch_offset < n_ch
        for ev in song.events
    )
    # One cell holds one PCM note. A later trigger moves to the next free row
    # when that row is still before the following trigger. When it is not,
    # the later trigger keeps the row: it is the cursor the channel plays.
    pcm_pos = _assign_pcm_rows(
        song, total_rows, last_sample, stats, content_end, content_cut,
    )
    # The tone note that currently owns each (channel, row), so a short
    # percussive attack can be kept when the following hit rounds onto the
    # same row.
    note_ev: dict[tuple[int, int], object] = {}
    # The next key-on per channel, in source order. A cut that would reach
    # that key-on must not be written: it would cut the following note. The
    # next key-on already ends the previous note.
    next_on: dict[int, list[tuple[int, object]]] = {}
    for ev in song.events:
        if ev.on and not ev.pcm:
            ev_ch = ev.ch + ch_offset
            if 0 <= ev_ch < n_ch:
                next_on.setdefault(ev_ch, []).append((ev.sample, ev))
    for entries in next_on.values():
        entries.sort(key=lambda item: item[0])
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
        row, delay = loop_clip(row, delay, ev.sample)
        row, delay = loop_start(row, delay, ev.sample)
        if ev.on:
            shifted = note_row_shift.get(id(ev))
            if shifted is not None:
                row, delay = shifted
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
            if (ev_ch, row) in shifted_rows and cell.note >= 0 and cell.note != NOTE_OFF:
                # The note that moved into this row already cuts the previous
                # note at its key-on; this off would cut the moved note.
                _bump(stats, "off_swallowed")
                continue
            # A note-off on the note's own row is a cut (ECxx). An off at or
            # before that row is the previous note releasing, and the new
            # note wins.
            started = 0
            for cmd, val in cell.fx or ():
                if cmd == 0xED:
                    started = val
                    break
            started_ev = note_ev.get((ev_ch, row))
            if cell.note < 0 or cell.note == NOTE_OFF:
                cell.note = NOTE_OFF
                if delay:
                    _add_fx(cell, 0xED, delay, fx_cols, stats)
            elif (
                started_ev is not None
                and ev.sample > getattr(started_ev, "sample", -1)
            ):
                # This off belongs to the note on this row, even when both
                # round onto the same tick (a gate shorter than one tick).
                # The SN76489/T6W28 short-gate path needs ECxx > EDxx:
                # EDxx greater than ECxx cut the note before it started
                # (T6W28 premature-cut bug). Other platforms count ECxx
                # from the delayed key-on, so the gate is ECxx ticks.
                if getattr(started_ev, "tone_chip", 0) == 3:
                    ec = max(started + 1, delay)
                else:
                    ec = max(1, delay - started)
                # A cut that reaches the following key-on would cut the new
                # note instead. The next key-on already ends this one, so
                # leave the row without an ECxx (the Neo South Island Act 2
                # case: ED03 EC05 with the next note at the same tick).
                speed_now = max(1, song.speed)
                nxt = next(
                    (n for n in next_on.get(ev_ch, ()) if n[0] >= ev.sample),
                    None,
                )
                if nxt is not None:
                    nrow, ndelay = song.place(nxt[0], stats)
                    cut_tick = row * speed_now + started + ec
                    next_tick = nrow * speed_now + ndelay
                    if cut_tick >= next_tick:
                        _bump(stats, "off_swallowed")
                        continue
                _add_fx(cell, 0xEC, ec, fx_cols, stats)
            elif delay > started:
                # A release from the previous note that lands after this
                # note's delayed start would cut the new note; keep the new
                # note and let its own key-on own the row.
                _bump(stats, "off_swallowed")
            continue
        if 0 <= cell.note < 180:
            prev = note_ev.get((ev_ch, row))
            # A 5 ms noise attack and the tonal body that follows it can round
            # onto the same row. Dropping the attack changes the drum's
            # timbre, so move the short note one row back when that row is
            # free; the body keeps its own timing and cuts the attack.
            if (
                prev is not None
                and getattr(prev, "tone_chip", 0) in (2, 8)  # PSG percussion
                and not prev.vol_pts
                and prev.end
                and prev.end <= ev.sample
                and (not row_samples or prev.end - prev.sample < row_samples)
                and row > 0
                and row - 1 not in grid[ev_ch]
            ):
                grid[ev_ch][row - 1] = cell
                note_ev[(ev_ch, row - 1)] = prev
                note_ev.pop((ev_ch, row), None)
                if (ev_ch, row) in note_e5:
                    note_e5[(ev_ch, row - 1)] = note_e5.pop((ev_ch, row))
                cell = _blank_row()
                grid[ev_ch][row] = cell
                _bump(stats, "note_shifted")
            else:
                _bump(stats, "note_collision")
        cell.note = ev.note if ev.note != 180 else NOTE_OFF
        if ev.ins >= 0:
            cell.ins = ev.ins
        if ev.vol >= 0:
            if ev.vol_slide:
                # FA01 steps on the tick it is written. The column is
                # therefore one higher than the level the chip should play.
                # FA00 holds.
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
        note_ev[(ev_ch, row)] = ev
        # PCM pan 0x00 is both channels off. Omitting it leaves Furnace's
        # default (both full). FM pan 0 still means "no 08xx on this row".
        if ev.quad is not None:
            _add_quad_fx(cell, ev.quad, fx_cols, stats)
        elif (ev.pcm or ev.pan or ev.tone_chip == 7) and not ev.no_pan:
            # VSU LRV 0 is "both sides off", not "leave the previous pan".
            _add_fx(cell, 0x08, ev.pan & 0xFF, fx_cols, stats)

    # A PCM note still sounding at the last content row gets an explicit off.
    # At a loop seam a note whose source stop lies past the wrap keeps
    # sounding into the next iteration. No off is written.
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
        if content_cut is not None and (
            not last.end or last.end >= content_cut
        ):
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

    # A held note's carrier fade steps land on their own rows. They are
    # placed only where no note owns the row, and a following note's own
    # volume wins.
    if fm_tl:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not ev.on or ev.pcm or not ev.tl_pts or not (0 <= ev_ch < n_ch):
                continue
            # An Ext channel-3 column scales one operator, not the algorithm's
            # carrier set, so its baseline is that operator's floor - recorded on
            # the note when the levels were bound (see _bind_ext_levels). Using
            # carrier_tl() there would measure the fade from another operator's
            # level and move the wrong register.
            t0 = getattr(ev, "vol_tl_base", None)
            if t0 is None:
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
                # The column's ceiling is the chip that owns this channel.
                top = vol_max[ev_ch] if ev_ch < len(vol_max) else fm_vol_max
                cell.vol = max(0, min(top, top - (tl - t0)))

    # Pitch sweeps run after every event is placed. The ramps can then share
    # the note-off rows and their ED delays. Note rows are known by now, and
    # each channel's list is built once for the crossing checks.
    note_rows: dict[int, list[int]] = {
        ch: sorted(r for r, cell in grid[ch].items() if 0 <= cell.note < 180)
        for ch in range(n_ch)
    }
    def placed_note_row(ev) -> int:
        shifted = note_row_shift.get(id(ev))
        if shifted is not None:
            return shifted[0]
        if ev.pcm:
            assigned = pcm_pos.get(id(ev))
            if assigned is None:
                return -1
            row, delay = assigned
        else:
            row, delay = song.place(ev.sample, stats)
        row, delay = loop_clip(row, delay, ev.sample)
        row, delay = loop_start(row, delay, ev.sample)
        return row

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
                        note_row=placed_note_row(ev),
                    )
                else:
                    _emit_steps(
                        grid, song, ev, fx_cols, total_rows, sweep_e5,
                        stats=stats, note_row=placed_note_row(ev),
                    )
    if song.events:
        for ev in song.events:
            if not (
                ev.on
                and 0 <= ev.ch + ch_offset < n_ch
                and (ev.op_mul_fx or ev.op_dt_fx or ev.op_dt2_fx)
                and (content_cut is None or ev.sample < content_cut)
            ):
                continue
            note_row = placed_note_row(ev)
            if note_row < 0:
                continue
            chan = ev.ch + ch_offset if ch_offset else None
            for points, code in (
                (ev.op_mul_fx, 0x16),
                (ev.op_dt_fx, 0x53),
                (ev.op_dt2_fx, 0x55),
            ):
                if content_cut is not None:
                    points = tuple(
                        p for p in points if p[0] < content_cut
                    )
                if points:
                    _emit_op_fx(
                        grid, song, ev, points, code, fx_cols, total_rows,
                        chan=chan, stats=stats, note_row=note_row,
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
            own_row = placed_note_row(ev)
            if own_row < 0:
                continue
            # A step may not spill into the next note on this channel. The
            # source's final TL=0x7F (silence) lands just before its key-off,
            # and row rounding used to push it past the following key-on, where
            # it muted a note that had just started.
            next_row = next(
                (r for r in note_rows.get(ev_ch, ()) if r > own_row),
                total_rows,
            )
            last_pan = None if ev.no_pan else ev.pan
            note_vol_done = False
            for sample, value, pan in ev.vol_pts:
                if content_cut is not None and sample >= content_cut:
                    continue
                row, delay = song.place(sample, stats)
                if not ev.vol_slide:
                    delay = 0
                if row < 0 or row >= total_rows or row >= next_row:
                    continue
                if ev.pcm and row <= own_row:
                    # The note row already carries the attack level (or the
                    # first real level after a muted key-on). A later step
                    # rounded onto the same row cannot be heard separately.
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
                    elif value == 0:
                        # A later mute must not erase the note's own attack:
                        # the next key-on already ends this note, and a zero
                        # column here would make an audible blip silent.
                        continue
                    elif value > cell.vol:
                        # The step gets louder: keep the quieter attack and
                        # put the raise on the following free row instead.
                        target = row + 1
                        if target < next_row:
                            tcell = grid[ev_ch].get(target)
                            if tcell is None:
                                tcell = _blank_row()
                                grid[ev_ch][target] = tcell
                            if not (0 <= tcell.note < 180):
                                tcell.vol = value
                                if (pan is not None
                                        and (last_pan is None
                                             or pan != last_pan)):
                                    _add_fx(
                                        tcell, 0x08, pan & 0xFF, fx_cols, stats,
                                    )
                                    last_pan = pan
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

    # A quad C352 voice carries its level on the four pan slots. A mid-note
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

    # An OPL modulator level that differs from the shared instrument baseline
    # (12xx..15xx: operator 1-4 TL, 0 highest, 3F lowest). The note row can
    # carry the attack level alongside its note; later rows carry the moves.
    if song.events:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not (ev.on and ev.op_tl_fx) or ev_ch < 0 or ev_ch >= n_ch:
                continue
            for sample, op, value in ev.op_tl_fx:
                if content_cut is not None and sample >= content_cut:
                    continue
                row, delay = song.place(sample, stats)
                if row < 0 or row >= total_rows:
                    continue
                cell = grid[ev_ch].get(row)
                if cell is None:
                    cell = _blank_row()
                    grid[ev_ch][row] = cell
                _add_fx(cell, 0x12 + op, value & 0x3F, fx_cols, stats)
                if delay:
                    _add_fx(cell, 0xED, delay, fx_cols, stats)

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

    # On the SN76489/T6W28 path ECxx must land after EDxx. When ECxx <= EDxx
    # Furnace cut the note before it started (the T6W28 short-gate bug), so
    # drop that cut and let the next key-on end the note. Other platforms
    # count ECxx from the delayed key-on, where ECxx < EDxx is the short gate
    # we actually want (a one-tick VSU noise hit, for example).
    for ch in range(n_ch):
        for row, cell in grid[ch].items():
            if not cell.fx:
                continue
            note = note_ev.get((ch, row))
            if getattr(note, "tone_chip", 0) != 3:
                continue
            ed = next((v for c, v in cell.fx if c == 0xED), None)
            if ed is None:
                continue
            ec = next((v for c, v in cell.fx if c == 0xEC), None)
            if ec is not None and ec <= ed:
                cell.fx = [(c, v) for c, v in cell.fx if c != 0xEC]

    # The pattern window: a whole musical phrase when the order budget
    # allows, otherwise the largest phrase that fits. One length serves
    # every order.
    last_content = max(
        (row for ch in range(n_ch) for row in grid[ch]), default=-1
    )

    # An OPL operator with a zero attack rate never attacks: it keeps whatever
    # envelope level it holds when the key goes down. If the previous note on
    # the channel left that operator at RR=0, the level never fell to silence
    # either. On hardware the source usually writes the next patch's RR during
    # the silent gap, which releases the envelope before the key-on; a module
    # applies the patch at the key-on itself, so the stale level survives and
    # the operator stays audible. Furnace's hard envelope reset (0x30) performs
    # that release: at the key-on the platform drives every operator through
    # RR=15 and then restores the logged value.
    patches = getattr(song, "fm_patches", ()) or ()

    def _opl_reset_patch(ins: int):
        if not 0 <= ins < len(patches):
            return None
        p = patches[ins]
        if not getattr(p, "opl", False) or getattr(p, "drum", False):
            return None
        return p

    if any(
        getattr(p, "opl", False) and not getattr(p, "drum", False)
        for p in patches
    ):
        for ch in range(n_ch):
            cells = grid[ch]
            if not cells:
                continue
            carried = -1
            prev_ins = -1
            reset_rows: set[int] = set()
            for row in sorted(cells):
                cell = cells[row]
                if cell.ins >= 0:
                    carried = cell.ins
                if not (0 <= cell.note < 180):
                    continue
                cur = _opl_reset_patch(carried)
                prev = _opl_reset_patch(prev_ins)
                if cur is not None and prev is not None:
                    cur_ops = 4 if cur.four else 2
                    prev_ops = 4 if prev.four else 2
                    if any(
                        cur.ops[i].ar == 0 and prev.ops[i].rr == 0
                        for i in range(min(cur_ops, prev_ops))
                    ):
                        reset_rows.add(row)
                prev_ins = carried
            for row in reset_rows:
                cell = cells.get(row)
                if cell is None:
                    continue
                _add_fx(cell, 0x30, 1, fx_cols, stats)
                # Disarm on the next row so later notes key normally. A
                # neighbouring note that needs the same reset keeps it armed.
                nrow = row + 1
                if nrow in reset_rows or nrow > last_content:
                    continue
                ncell = cells.get(nrow)
                if ncell is None:
                    ncell = _blank_row()
                    cells[nrow] = ncell
                _add_fx(ncell, 0x30, 0, fx_cols, stats)

    pat_len = min(pat_len_max, _phrase_pattern_len(song, last_content, total_rows))

    # Walk pat_len-row chunks, but force an order boundary at the VGM loop so
    # Bxx can jump there. A long intro must not become one giant order; only
    # `pat_len` rows would survive.
    # A short intro order ends at the loop row with 0D00. If the row before it
    # carries a note, that jump runs before the note's ED delay and the note
    # never starts. Move the boundary one row later until the last row is
    # note-free.
    if loop_row > 0:
        while loop_row < total_rows and any(
            0 <= cell.note < 180
            for ch in range(n_ch)
            if (cell := grid[ch].get(loop_row - 1)) is not None
        ):
            loop_row += 1
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

    # A delay on the jump row is a pre-effect and returns before Bxx is read.
    # The jump therefore rides a channel whose row has no ED. The delayed
    # notes on the others still land just before the wrap.
    jump_ed: set[int] = set()
    if content_end is not None and song.loop_sample is not None:
        for ch in range(n_ch):
            cell = grid[ch].get(content_end)
            if cell is not None and any(
                c == 0xED for c, _v in (cell.fx or ())
            ):
                jump_ed.add(ch)
    jump_carrier: int | None = next(
        (ch for ch in range(n_ch) if ch not in jump_ed), None
    )

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
                jump_row = min(pat_len, max(0, content_end - start_row + 1)) - 1
                if jump_row < 0:
                    jump_row = 0
                # One carrier is enough. With a free channel present only
                # that one jumps. When every row is delayed, channel 0 gives
                # up its delay and the rest keep theirs.
                carry = (ch == 0) if jump_carrier is None else (ch == jump_carrier)
                if carry:
                    cell = rows[jump_row]
                    if cell is None:
                        cell = rows[jump_row] = _blank_row()
                    if any(c == 0xED for c, _v in (cell.fx or ())):
                        # Every channel is delayed on the jump row: one of
                        # them has to give up its delay to carry the jump.
                        _bump(stats, "ed_stripped_loop")
                        cell.fx = [
                            (c, v) for c, v in
                            (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                            if c != 0xED
                        ]
                    _add_fx(cell, 0x0B, loop_order, fx_cols, stats)
            orders[ch].append(oi)
            patterns.append((ch, oi, rows))

    return orders, patterns, loop_order, pat_len


def _fit_fx_cols(patterns: list[tuple[int, int, list[Row | None]]], n_ch: int) -> list[int]:
    """Effect columns per channel: one per effect the busiest row uses, 1..8.

    Furnace sizes a pattern column from this count. It is the fit-to-content
    knob (docs/fur-writer.md).
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


def _system_span(system) -> int:
    """Module columns one FM system occupies, sample engines included."""
    return system.channels


def _channel_vol_max(
    song: Song, n_ch: int, default: int, ch_offset: int = 0,
) -> list[int]:
    """The volume column's ceiling for every module channel.

    A single-family module keeps the one value analysis chose. Two families in
    one module differ: an OPN or OPM fade tops out at 127 while an OPL reaches
    only 63 and the YM2413 just 15, so each system's own columns carry their
    chip's maximum.
    """
    table = [default] * n_ch
    systems = list(getattr(song, "fm_systems", ()) or ())
    # With no FM block in the module the sample chips own these indices, and
    # their fades use their own column scale.
    if ch_offset or len({s.kind for s in systems}) < 2:
        return table
    ch = 0
    for system in systems:
        top = (
            15 if system.kind == "opll" else
            63 if system.kind in ("opl", "opl2", "opl3", "y8950", "opl4") else 127
        )
        for _ in range(system.fm_channels):
            if ch < n_ch:
                table[ch] = top
            ch += 1
        # SSG and sample columns ride past that family's FM block.
        ch += _system_span(system) - system.fm_channels
    return table


def _legacy_system(song: Song):
    """One system for a Song built before `fm_systems` existed."""
    from vgm2151fur.analyze import FmSystem

    kind = getattr(song, "fm_chip", "ym2151")
    if kind in ("opl", "opl2", "opl3"):
        drums = bool(getattr(song, "opl_drums", False))
        ids = list(getattr(song, "opl_ids", ()) or ()) or [0]
        return [
            FmSystem(kind, cid, OPL_CHANNELS[(kind, drums)], drums=drums)
            for cid in ids
        ]
    if kind == "opll":
        drums = bool(getattr(song, "opll_drums", False))
        vrc7 = bool(getattr(song, "opll_vrc7", False))
        ids = list(getattr(song, "opll_ids", ()) or ()) or [0]
        channels = 6 if vrc7 else (
            OPLL_DRUMS_CHANNELS if drums else OPLL_CHANNELS
        )
        return [
            FmSystem("opll", cid, channels, drums=drums, vrc7=vrc7)
            for cid in ids
        ]
    ids = list(getattr(song, f"{kind}_ids", ()) or ()) or [0]
    if kind == "ym2610":
        fm = 6 if getattr(song, "ym2610_b", False) else 4
        return [FmSystem(kind, cid, fm) for cid in ids]
    if kind == "ym2608":
        ext = bool(getattr(song, "ym2608_ext", False))
        return [
            FmSystem(kind, cid, 9 if ext else 6, ext=ext) for cid in ids
        ]
    if kind == "ym2612":
        ext = bool(getattr(song, "ym2612_ext", False))
        csm = bool(getattr(song, "ym2612_csm", False))
        return [
            FmSystem(kind, cid, 9 if ext else 6, ext=ext, csm=csm)
            for cid in ids
        ]
    if kind == "ym2203":
        ext = bool(getattr(song, "ym2203_ext", False))
        return [
            FmSystem(kind, cid, 6 if ext else 3, ext=ext) for cid in ids
        ]
    return [FmSystem("ym2151", 0, 8)]


def _append_system_names(
    song: Song, systems, names_long: list[str], names_short: list[str],
) -> None:
    """Channel labels for every FM system, in module order.

    The tags are the column handle a player sees, so two systems in one module
    must not answer to the same letters. An OPL family that shares a module
    numbers its voices by their channel here; on its own it keeps the plain
    FM 1..9 tags Furnace uses.
    """
    counts = Counter(s.kind for s in systems)
    mixed = len(counts) > 1
    seen: Counter = Counter()
    ch0 = 0
    for system in systems:
        kind = system.kind
        seen[kind] += 1
        instance = seen[kind]
        cid = system.chip_id
        span = _system_span(system)
        # An instance suffix only appears when the family has more than one
        # chip, matching the long names the older writer produced.
        suffix = f" ({instance})" if counts[kind] > 1 else ""
        longs: list[str] = []
        shorts: list[str] = []
        if kind == "ym2610":
            fm = system.fm_channels or (
                6 if getattr(song, "ym2610_b", False) else 4
            )
            for voice in range(fm):
                longs.append(f"FM {voice + 1}{suffix}")
                shorts.append(f"F{cid + 1}-{voice + 1}")
            for voice in range(3):
                longs.append(f"PSG {voice + 1}{suffix}")
                shorts.append(f"P{cid + 1}-{voice + 1}")
            for voice in range(6):
                longs.append(f"ADPCM-A {voice + 1}{suffix}")
                shorts.append(f"A{cid + 1}-{voice + 1}")
            longs.append(f"ADPCM-B{suffix}")
            shorts.append(f"B{cid + 1}")
        elif kind == "ym2608":
            # Furnace names the OPNA's kit columns Kick, Snare, Top, HiHat,
            # Tom and Rim (sysDef.cpp:1365-1370); delta-T is simply ADPCM.
            if system.ext:
                for voice, label in enumerate(("FM 1", "FM 2")):
                    longs.append(f"{label}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(4):
                    # Column c carries operator (1,2,0,3)[c]'s frequency.
                    op_label = (1, 2, 0, 3)[voice] + 1
                    longs.append(f"FM 3 OP{op_label}{suffix}")
                    shorts.append(f"O{cid + 1}-{op_label}")
                for voice in range(3):
                    longs.append(f"FM {voice + 4}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 4}")
            else:
                for voice in range(6):
                    longs.append(f"FM {voice + 1}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
            for voice in range(3):
                longs.append(f"PSG {voice + 1}{suffix}")
                shorts.append(f"P{cid + 1}-{voice + 1}")
            for tag, long_name in RHYTHM_VOICES:
                longs.append(f"{long_name}{suffix}")
                shorts.append(f"{tag}{cid + 1}")
            longs.append(f"ADPCM{suffix}")
            shorts.append(f"D{cid + 1}")
        elif kind == "ym2612":
            if system.ext:
                for voice, label in enumerate(("FM 1", "FM 2")):
                    longs.append(f"{label}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(4):
                    op_label = (1, 2, 0, 3)[voice] + 1
                    longs.append(f"FM 3 OP{op_label}{suffix}")
                    shorts.append(f"O{cid + 1}-{op_label}")
                for voice in range(3):
                    longs.append(f"FM {voice + 4}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 4}")
            else:
                for voice in range(6):
                    longs.append(f"FM {voice + 1}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
        elif kind == "ym2203":
            if system.ext:
                for voice, label in enumerate(("FM 1", "FM 2")):
                    longs.append(f"{label}{suffix}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(4):
                    op_label = (1, 2, 0, 3)[voice] + 1
                    longs.append(f"FM 3 OP{op_label}{suffix}")
                    shorts.append(f"O{cid + 1}-{op_label}")
                for voice in range(3):
                    longs.append(f"PSG {voice + 1}{suffix}")
                    shorts.append(f"P{cid + 1}-{voice + 1}")
            else:
                for voice in range(3):
                    longs.append(f"FM {cid + 1}-{voice + 1}")
                    shorts.append(f"F{cid + 1}-{voice + 1}")
                for voice in range(3):
                    longs.append(f"PSG {cid + 1}-{voice + 1}")
                    shorts.append(f"P{cid + 1}-{voice + 1}")
        elif kind in ("opl", "opl2", "opl3", "y8950", "opl4"):
            melodic = OPL_MELODIC[(kind, bool(system.drums))]
            for i, (long_name, short_name) in enumerate(zip(*opl_channel_names(
                    kind, bool(system.drums)))):
                longs.append(f"{long_name}{suffix}")
                if mixed and i < melodic:
                    # Numbered by module channel, so the OPN family beside it
                    # never answers to the same tag.
                    shorts.append(f"F{ch0 + i + 1}")
                elif counts[kind] > 1:
                    # F2-3 is chip 2's FM 3; a kit column keeps the instance.
                    shorts.append(
                        f"F{cid + 1}-{short_name[1:]}"
                        if short_name.startswith("F") and short_name[1:].isdigit()
                        else f"{short_name}{instance}"
                    )
                else:
                    shorts.append(short_name)
            if kind == "y8950":
                # The ADPCM voice is one column after the FM block.
                longs.append(f"ADPCM{suffix}")
                shorts.append(f"P{cid + 1}" if counts[kind] > 1 else "ADPCM")
            if kind == "opl4":
                # The 24 wave slots follow the FM block.
                for i in range(24):
                    longs.append(f"PCM {i + 1}{suffix}")
                    shorts.append(
                        f"P{cid + 1}-{i + 1}" if counts[kind] > 1 else f"P{i + 1}"
                    )
        elif kind == "opll":
            for i, (long_name, short_name) in enumerate(
                    zip(*opll_channel_names(
                        bool(system.drums), bool(getattr(system, "vrc7", False)),
                    ))):
                longs.append(f"{long_name}{suffix}")
                if mixed and i < (
                    OPLL_DRUMS_MELODIC if system.drums else OPLL_CHANNELS
                ):
                    shorts.append(f"F{ch0 + i + 1}")
                elif counts[kind] > 1:
                    shorts.append(
                        f"F{cid + 1}-{short_name[1:]}"
                        if short_name.startswith("F") and short_name[1:].isdigit()
                        else f"{short_name}{instance}"
                    )
                else:
                    shorts.append(short_name)
        else:
            fm = system.fm_channels or 8
            longs = [f"FM {i + 1}" for i in range(fm)]
            shorts = [f"F{i + 1}" for i in range(fm)]
        names_long.extend(longs)
        names_short.extend(shorts)
        ch0 += span


# The chip names an FM write arrives under. A log whose stream only touches a
# wavetable/PSG chip must not gain the analyzer's legacy YM2151 placeholder.
_FM_CHIP_NAMES = {
    "ym2151", "ym2203", "ym2612", "ym2610", "ym2608",
    "opll", "opl", "opl2", "opl3", "y8950", "ymf278b",
}


def _compact_sample_map_notes(
    patterns: list[tuple[int, int, list[Row | None]]],
    ins_base: int,
    ins_count: int,
    samples: list,
) -> tuple[list, int]:
    """Drop sample-map bodies no placed note selects.

    A note selects a slot as ``60 + slot`` and its instrument picks the map
    group.  A sample can survive grouping and still never be played (a silent
    chunk, a collapsed run, or a note lost to a row collision); remove it and
    renumber both the map notes and the sample list.
    """
    if ins_count <= 0 or not samples:
        return samples, ins_count
    used: set[int] = set()
    for _ch, _idx, rows in patterns:
        for row in rows:
            if row is None:
                continue
            ins, note = row.ins, row.note
            if ins_base <= ins < ins_base + ins_count and 60 <= note < 180:
                used.add((ins - ins_base) * 120 + (note - 60))
    if not used:
        return [], 0
    if max(used) >= len(samples):
        return samples, ins_count
    ordered = sorted(used)
    remap = {old: new for new, old in enumerate(ordered)}
    for _ch, _idx, rows in patterns:
        for row in rows:
            if row is None:
                continue
            ins, note = row.ins, row.note
            if ins_base <= ins < ins_base + ins_count and 60 <= note < 180:
                old = (ins - ins_base) * 120 + (note - 60)
                new = remap[old]
                row.ins = ins_base + new // 120
                row.note = 60 + new % 120
    return [samples[i] for i in ordered], (len(ordered) + 119) // 120


def _shift_sample_map_instruments(
    patterns: list[tuple[int, int, list[Row | None]]],
    start: int,
    delta: int,
) -> None:
    """Move later instrument ids when a sample-map group is removed."""
    if not delta:
        return
    for _ch, _idx, rows in patterns:
        for row in rows:
            if row is None:
                continue
            ins = row.ins
            if ins != EMPTY and ins >= start:
                row.ins = ins + delta


def write_fur(
    song: Song, *, include_pcm: bool = True, include_fm: bool = True,
    stats: dict | None = None, compress: bool = False,
) -> bytes:
    gd3 = song.vgm.gd3
    # Every pitched FM system in module order. A log normally contributes one;
    # the combined Zone 66 shape carries a PCM-only YM2612 beside the OPL that
    # plays the music, and both need their own chip entry and channel block.
    systems = list(getattr(song, "fm_systems", ()) or ())
    if not systems:
        systems = _legacy_system(song)
    kinds = {s.kind for s in systems}

    def _ids_of(kind: str | None) -> list[int]:
        return [s.chip_id for s in systems if kind and s.kind == kind]

    ym2203 = "ym2203" in kinds
    ym2612 = "ym2612" in kinds
    ym2610 = "ym2610" in kinds
    ym2608 = "ym2608" in kinds
    # The YM2413 is its own system (0x89) with a separate drums layout (0xa7).
    opll = "opll" in kinds
    opll_drums = any(s.drums for s in systems if s.kind == "opll")
    opll_ids = _ids_of("opll")
    # The OPL family is one module system per chip: an OPL3 owns both register
    # banks and all eighteen channels, while a dual YM3812 log adds a second
    # OPL2 system the way a dual YM2612 log adds a second OPN2.
    opl_kind = next((k for k in ("opl3", "opl2", "opl") if k in kinds), None)
    opl = opl_kind is not None
    opl_drums = any(s.drums for s in systems if s.kind == opl_kind)
    opl_ids = _ids_of(opl_kind)
    ym_chip = ym2203 or ym2612 or ym2610 or ym2608
    # The sample engines only ever come from one arcade family per module, so
    # the ADPCM counters follow that chip's instances.
    ym_kind = (
        "ym2608" if ym2608 else "ym2610" if ym2610 else
        "ym2612" if ym2612 else "ym2203" if ym2203 else None
    )
    ym_ids = _ids_of(ym_kind)
    ym_ext = any(s.ext for s in systems if s.kind == ym_kind)
    # A YM2612 system stays when the log had the chip, even with no FM notes.
    # A YM2203 system likewise owns its three SSG voices.  A YM2610 system
    # owns FM, SSG and the two ADPCM engines, so either half keeps it.
    # `systems` still carries the analyzer's legacy YM2151 placeholder so the
    # channel layout (tone chips start at 8) and `ch_offset` stay in step; an
    # SPC/PSG-only log must not actually write that empty OPM device, though -
    # it rescales the whole mix in VGM players and leaves eight silent columns.
    fm_source = getattr(song, "has_fm_source", True)
    # A wavetable/PSG chip (PCE, SCC, AY, NES) with no FM write owns the
    # melody, so the empty YM2151 placeholder must not be written.  A
    # sample-chip-only log keeps the legacy YM2151 as its base system (the
    # writer's `ch_offset` would otherwise renumber every sample column).
    has_tone_chip = bool(
        getattr(song, "scc_ids", ()) or getattr(song, "ay_ids", ())
        or getattr(song, "nes_ids", ()) or getattr(song, "huc6280_ids", ())
        or getattr(song, "gb_ids", ())
        or getattr(song, "vsu_ids", ())
        or getattr(song, "saa_ids", ())
        or getattr(song, "psg_ids", ())
        # YMZ280B is a sample engine with real module columns; a YMZ-only log
        # must not gain an empty YM2151 placeholder above it.
        or getattr(song, "ymz280b_samples", ())
    )
    if ym2610 or ym2608:
        fm_present = (include_fm or include_pcm) and bool(ym_ids)
    elif opl:
        fm_present = include_fm and (bool(opl_ids) or bool(ym_ids))
    elif opll:
        fm_present = include_fm and bool(opll_ids)
    elif ym_chip:
        fm_present = include_fm and bool(ym_ids)
    else:
        # The Y8950 owns a delta-T column. Its samples require the chip
        # entry even when the caller excluded FM notes.
        has_y8950_pcm = bool(
            include_pcm and (getattr(song, "y8950_b_samples", ()) or ())
        )
        fm_present = (include_fm or has_y8950_pcm) and (
            fm_source or not has_tone_chip
        )
    fm_ins = song.fm_patches if (fm_present and include_fm) else []
    
    pcm_smps = [s for s in song.pcm_samples if s.length >= 8] if include_pcm else []
    pcm_ids = sorted({s.chip_id for s in pcm_smps})
    oki_smps = list(song.oki_samples) if include_pcm else []
    adpcm_smps = list(song.msm6258_samples) if include_pcm else []
    n_adpcm_ins = (len(adpcm_smps) + 119) // 120
    spcm_smps = [s for s in song.segapcm_samples if s.length >= 8] if include_pcm else []
    spcm_ids = sorted({s.chip_id for s in spcm_smps})
    rf5c68_smps = (
        [s for s in song.rf5c68_samples if s.length >= 8] if include_pcm else []
    )
    rf5c164_smps = (
        [s for s in song.rf5c164_samples if s.length >= 8] if include_pcm else []
    )
    c140_smps = [s for s in song.c140_samples if s.length >= 8] if include_pcm else []
    c352_smps = [s for s in song.c352_samples if s.length >= 8] if include_pcm else []
    k053260_smps = (
        [s for s in song.k053260_samples if s.length >= 8] if include_pcm else []
    )
    ymz_smps = (
        list(getattr(song, "ymz280b_samples", ()) or ())
        if include_pcm else []
    )
    ymz_ids = sorted({s.chip_id for s in ymz_smps})
    dac_smps = list(getattr(song, "dac_samples", ()) or ()) if include_pcm else []
    n_dac_ins = (len(dac_smps) + 119) // 120
    dac_smp_base = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps) + len(k053260_smps)
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
    # The OPNA's delta-T samples are laid out like the YM2610's B maps. Its
    # rhythm kit needs no samples at all, just one instrument to hang the
    # column's macros on.
    ym2608_b_smps = (
        list(getattr(song, "ym2608_b_samples", ()) or ()) if include_pcm else []
    )
    n_ym2608_b_ins = (len(ym2608_b_smps) + 119) // 120
    n_ym2608_rhythm_ins = (
        1 if (include_pcm and getattr(song, "ym2608_rhythm_notes", 0)) else 0
    )
    ym2608_b_smp_base = ym2610_b_smp_base + len(ym2610_b_smps)
    # The Y8950's one ADPCM voice uses the same ADPCM-B sample format.
    y8950_b_smps = (
        list(getattr(song, "y8950_b_samples", ()) or ()) if include_pcm else []
    )
    n_y8950_b_ins = (len(y8950_b_smps) + 119) // 120
    y8950_b_smp_base = ym2608_b_smp_base + len(ym2608_b_smps)
    # The OPL4 wave slots use DIV_INS_MULTIPCM sample maps.
    opl4_wave_smps = (
        list(getattr(song, "opl4_wave_samples", ()) or ()) if include_pcm else []
    )
    n_opl4_wave_ins = len(opl4_wave_smps)
    opl4_wave_smp_base = y8950_b_smp_base + len(y8950_b_smps)
    # SSG/PSG PCM: one DIV_INS_AY sample instrument per 8-bit body. The writer
    # places these instruments just before the SCC/AY/PSG tone instruments and
    # the sample blocks after the OPL4 wave bodies.
    ssg_pcm_smps = (
        list(getattr(song, "ssg_pcm_samples", ()) or ()) if include_pcm else []
    )
    # 120 bodies per AY sample-map instrument.
    n_ssg_pcm_ins = (len(ssg_pcm_smps) + 119) // 120
    ssg_pcm_smp_base = opl4_wave_smp_base + len(opl4_wave_smps)
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
    nes_ids = list(getattr(song, "nes_ids", ()) or ())
    nes_specs = list(getattr(song, "nes_ins", ()) or ())
    nes_dpcm_smps = list(getattr(song, "nes_dpcm_samples", ()) or ())
    nes_pcm_smps = list(getattr(song, "nes_pcm_samples", ()) or ())
    huc6280_ids = list(getattr(song, "huc6280_ids", ()) or ())
    huc6280_waves = list(getattr(song, "huc6280_waves", ()) or ())
    huc6280_specs = list(getattr(song, "huc6280_ins", ()) or ())
    huc6280_pcm_smps = list(getattr(song, "huc6280_pcm_samples", ()) or ())
    gb_ids = list(getattr(song, "gb_ids", ()) or ())
    gb_waves = list(getattr(song, "gb_waves", ()) or ())
    gb_specs = list(getattr(song, "gb_ins", ()) or ())
    vsu_ids = list(getattr(song, "vsu_ids", ()) or ())
    vsu_waves = list(getattr(song, "vsu_waves", ()) or ())
    vsu_mod_waves = list(getattr(song, "vsu_mod_waves", ()) or ())
    vsu_specs = list(getattr(song, "vsu_ins", ()) or ())
    saa_ids = list(getattr(song, "saa_ids", ()) or ())
    saa_specs = list(getattr(song, "saa_ins", ()) or ())
    scc_chip = CHIP_K052539 if song.vgm.k051649_plus else CHIP_K051649
    n_scc = 5 * len(scc_ids)
    n_ay = 3 * len(ay_ids)
    n_psg = 4 * len(psg_ids)
    n_nes = 5 * len(nes_ids)
    n_huc6280 = 6 * len(huc6280_ids)
    n_gb = 4 * len(gb_ids)
    n_vsu = 6 * len(vsu_ids)
    n_saa = 6 * len(saa_ids)
    n_nes_dpcm_ins = len(nes_dpcm_smps)
    n_nes_pcm_ins = (len(nes_pcm_smps) + 119) // 120
    n_huc6280_pcm_ins = len(huc6280_pcm_smps)
    # NES DPCM (1-bit) and direct-DAC (8-bit) bodies follow the SSG PCM group.
    nes_dpcm_smp_base = ssg_pcm_smp_base + len(ssg_pcm_smps)
    nes_pcm_smp_base = nes_dpcm_smp_base + len(nes_dpcm_smps)
    # HuC6280 direct-D/A bodies follow the NES sample group.
    huc6280_pcm_smp_base = nes_pcm_smp_base + len(nes_pcm_smps)
    # K007232 appears only when the source carried usable samples for it, and
    # never beside another sample chip.
    want_k007 = bool(pcm_ids)

    # Columns per system, summed. A single-family module gets the same totals
    # the old branch chain produced; a combined one adds the families together.
    n_fm = n_ssg = n_adpcm_a = n_adpcm_b = 0
    if fm_present:
        for system in systems:
            kind = system.kind
            if kind in ("opl", "opl2", "opl3", "opll", "y8950", "opl4"):
                n_fm += _system_span(system)
                continue
            if kind == "ym2610" or kind == "ym2608":
                n_fm += system.fm_channels
                n_ssg += 3
                # On the OPNA the six columns after the SSG are the rhythm kit;
                # Furnace gives them the same span as the YM2610's ADPCM-A.
                n_adpcm_a += 6
                n_adpcm_b += 1
            elif kind == "ym2203":
                # The SSG is part of each YM2203 system, not a separate chip.
                n_fm += system.fm_channels
                n_ssg += 3
            else:
                n_fm += system.fm_channels or 8
    n_k007 = 2 * len(pcm_ids) if want_k007 else 0
    rf5c68_ids = sorted({s.chip_id for s in rf5c68_smps})
    rf5c164_ids = sorted({s.chip_id for s in rf5c164_smps})
    n_rf5c = 8 * (len(rf5c68_ids) + len(rf5c164_ids))
    n_ch = (
        n_fm + n_ssg + n_adpcm_a + n_adpcm_b + n_psg + n_k007
        + 4 * len(oki_ids) + len(adpcm_ids)
        + 16 * len(spcm_ids) + n_rf5c
        + 24 * len(c140_ids) + 32 * len(c352_ids)
        + 4 * len(k053260_ids) + 8 * len(ymz_ids)
        + n_scc + n_ay + n_nes + n_huc6280 + n_gb + n_vsu + n_saa
    )
    if n_ch == 0:
        raise ValueError("write_fur: need include_fm and/or include_pcm")
    fx_cols = 8  # room for LFO 17/18/1E/1F plus pan/E5/ED/Bxx
    k_clock = song.vgm.k007232_clock or 3579545
    spcm_clock = song.vgm.segapcm_clock or SEGAPCM_CLOCK_X
    rf5c68_clock = song.vgm.rf5c68_clock or 8000000
    rf5c164_clock = song.vgm.rf5c164_clock or 12500000
    c140_clock = song.vgm.c140_clock or 12288000
    c352_clock = song.vgm.c352_clock or C352_DEFAULT_CLOCK
    k053260_clock = song.vgm.k053260_clock or 3579545

    n_ins = (
        len(fm_ins) + n_dac_ins + len(pcm_smps) + len(oki_smps) + n_adpcm_ins
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps) + len(k053260_smps)
        + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins
        + n_y8950_b_ins + n_opl4_wave_ins + n_ssg_pcm_ins
        + n_nes_dpcm_ins + n_nes_pcm_ins + n_huc6280_pcm_ins
        + len(ymz_smps)
        + len(scc_specs) + len(ay_specs) + len(psg_specs) + len(nes_specs)
        + len(huc6280_specs) + len(gb_specs) + len(vsu_specs)
        + len(saa_specs)
    )
    n_smp = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps)
        + len(k053260_smps) + len(dac_smps)
        + len(ym2610_a_smps) + len(ym2610_b_smps) + len(ym2608_b_smps)
        + len(y8950_b_smps) + len(opl4_wave_smps) + len(ssg_pcm_smps)
        + len(nes_dpcm_smps) + len(nes_pcm_smps) + len(huc6280_pcm_smps)
        + len(ymz_smps)
    )
    n_wav = (
        len(scc_waves) + len(huc6280_waves) + len(gb_waves) + len(vsu_waves)
    )
    # The VSU's 10xx effects name a wavetable in the module-wide list.  Keep
    # the block order in step with the index the patterns use.
    vsu_wave_base = len(scc_waves) + len(huc6280_waves) + len(gb_waves)

    # Analysis numbers sample-chip channels after the FM block. With no FM
    # they shift down to stay inside n_ch.
    fm_analysis_block = sum(_system_span(s) for s in systems)

    orders, patterns, _loop_ord, pat_len = _build_patterns(
        song, n_ch, PAT_LEN_MAX, fx_cols,
        0 if fm_present else -fm_analysis_block,
        stats=stats, vsu_wave_base=vsu_wave_base,
    )
    effect_cols = _fit_fx_cols(patterns, n_ch)

    # The note-placement pass is final, so now drop sample-map slots that no
    # placed note ever selects.  Removing a whole 120-slot group also shifts
    # every later instrument id, so the pattern cells are renumbered before
    # instrument and sample-map writing.
    dac_ins_base = len(fm_ins)
    if include_pcm and dac_smps:
        old_dac_ins = n_dac_ins
        old_dac_len = len(dac_smps)
        dac_smps, n_dac_ins = _compact_sample_map_notes(
            patterns, dac_ins_base, n_dac_ins, dac_smps,
        )
        if n_dac_ins != old_dac_ins:
            _shift_sample_map_instruments(
                patterns, dac_ins_base + old_dac_ins,
                n_dac_ins - old_dac_ins,
            )
        if stats is not None:
            stats["dac_samples_unused"] = old_dac_len - len(dac_smps)

    # Sample bases after the DAC group depend on its final length.
    dac_smp_base = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps) + len(k053260_smps)
    )
    ym2610_a_smp_base = dac_smp_base + len(dac_smps)
    ym2610_b_smp_base = ym2610_a_smp_base + len(ym2610_a_smps)
    ym2608_b_smp_base = ym2610_b_smp_base + len(ym2610_b_smps)
    y8950_b_smp_base = ym2608_b_smp_base + len(ym2608_b_smps)
    opl4_wave_smp_base = y8950_b_smp_base + len(y8950_b_smps)
    ssg_pcm_ins_base = (
        len(fm_ins) + n_dac_ins + len(pcm_smps) + len(oki_smps)
        + n_adpcm_ins + len(spcm_smps) + len(rf5c68_smps)
        + len(rf5c164_smps) + len(c140_smps) + len(c352_smps)
        + len(k053260_smps) + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins + n_y8950_b_ins
        + n_opl4_wave_ins
    )
    if include_pcm and ssg_pcm_smps:
        old_ssg_ins = n_ssg_pcm_ins
        old_ssg_len = len(ssg_pcm_smps)
        ssg_pcm_smps, n_ssg_pcm_ins = _compact_sample_map_notes(
            patterns, ssg_pcm_ins_base, n_ssg_pcm_ins, ssg_pcm_smps,
        )
        if n_ssg_pcm_ins != old_ssg_ins:
            _shift_sample_map_instruments(
                patterns, ssg_pcm_ins_base + old_ssg_ins,
                n_ssg_pcm_ins - old_ssg_ins,
            )
        if stats is not None:
            stats["ssg_pcm_samples_unused"] = (
                old_ssg_len - len(ssg_pcm_smps)
            )

    ssg_pcm_smp_base = opl4_wave_smp_base + len(opl4_wave_smps)
    nes_dpcm_smp_base = ssg_pcm_smp_base + len(ssg_pcm_smps)
    nes_pcm_smp_base = nes_dpcm_smp_base + len(nes_dpcm_smps)
    huc6280_pcm_smp_base = nes_pcm_smp_base + len(nes_pcm_smps)
    # YMZ280B bodies and instruments come last among the sample engines.
    ymz_smp_base = huc6280_pcm_smp_base + len(huc6280_pcm_smps)
    n_ins = (
        len(fm_ins) + n_dac_ins + len(pcm_smps) + len(oki_smps) + n_adpcm_ins
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps) + len(k053260_smps)
        + n_ym2610_a_ins + n_ym2610_b_ins
        + n_ym2608_rhythm_ins + n_ym2608_b_ins
        + n_y8950_b_ins + n_opl4_wave_ins + n_ssg_pcm_ins
        + n_nes_dpcm_ins + n_nes_pcm_ins + n_huc6280_pcm_ins
        + len(ymz_smps)
        + len(scc_specs) + len(ay_specs) + len(psg_specs) + len(nes_specs)
        + len(huc6280_specs) + len(gb_specs) + len(vsu_specs)
        + len(saa_specs)
    )
    n_smp = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(rf5c68_smps) + len(rf5c164_smps)
        + len(c140_smps) + len(c352_smps)
        + len(k053260_smps) + len(dac_smps)
        + len(ym2610_a_smps) + len(ym2610_b_smps) + len(ym2608_b_smps)
        + len(y8950_b_smps) + len(opl4_wave_smps) + len(ssg_pcm_smps)
        + len(nes_dpcm_smps) + len(nes_pcm_smps) + len(huc6280_pcm_smps)
        + len(ymz_smps)
    )

    n_ord = len(orders[0]) if orders else 1
    n_pat = len(patterns)

    names_long: list[str] = []
    names_short: list[str] = []
    if fm_present:
        _append_system_names(
            song, systems, names_long, names_short,
        )
    if psg_ids:
        if getattr(song, "psg_t6w28", False):
            names_long += ["Square 1", "Square 2", "Square 3", "Noise"]
            names_short += ["S1", "S2", "S3", "NO"]
        else:
            for cid in range(len(psg_ids)):
                suffix = f" ({cid + 1})" if len(psg_ids) > 1 else ""
                for voice in range(3):
                    names_long.append(f"PSG {voice + 1}{suffix}")
                    names_short.append(f"S{cid + 1}-{voice + 1}")
                names_long.append(f"PSG Noise{suffix}")
                names_short.append(f"S{cid + 1}-N")
    for cid in pcm_ids:
        suffix = f" ({cid + 1})" if len(pcm_ids) > 1 else ""
        for voice in range(2):
            names_long.append(f"PCM {voice + 1}{suffix}")
            names_short.append(f"P{cid + 1}-{voice + 1}")
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
    for cid in spcm_ids:
        suffix = f" ({cid + 1})" if len(spcm_ids) > 1 else ""
        for voice in range(16):
            names_long.append(f"SEGA {voice + 1}{suffix}")
            names_short.append(f"S{cid + 1}-{voice + 1}")
    # The RF5C pair are separate chips to Furnace (one system each), but share
    # a label family: "PCM 1".."PCM 8" for the 68, "PCM 9".."PCM 16" for a 164
    # in the same module, so a combined log never repeats a column name.
    rf5c_n = 0
    for smps in (rf5c68_smps, rf5c164_smps):
        ids = sorted({s.chip_id for s in smps})
        for cid in ids:
            suffix = f" ({cid + 1})" if len(ids) > 1 else ""
            for voice in range(8):
                names_long.append(f"PCM {rf5c_n + voice + 1}{suffix}")
                names_short.append(f"P{rf5c_n + voice + 1}")
            rf5c_n += 8
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
    for cid in ymz_ids:
        for voice in range(8):
            names_long.append(f"YMZ280B {cid + 1}-{voice + 1}")
            names_short.append(f"Y{cid + 1}-{voice + 1}")
    for cid in scc_ids:
        for voice in range(5):
            names_long.append(f"SCC {cid + 1}-{voice + 1}")
            names_short.append(f"K{cid + 1}-{voice + 1}")
    for cid in ay_ids:
        for voice in range(3):
            names_long.append(f"PSG {cid + 1}-{voice + 1}")
            names_short.append(f"P{cid + 1}-{voice + 1}")
    nes_labels = ("Pulse 1", "Pulse 2", "Triangle", "Noise", "DPCM")
    for cid in nes_ids:
        for voice, label in enumerate(nes_labels):
            names_long.append(f"{label} ({cid + 1})")
            names_short.append(f"N{cid + 1}-{voice + 1}")
    pce_labels = ("Wave 1", "Wave 2", "Wave 3", "Wave 4", "Wave 5", "Wave 6")
    for cid in huc6280_ids:
        for voice, label in enumerate(pce_labels):
            names_long.append(f"{label} ({cid + 1})")
            names_short.append(f"P{cid + 1}-{voice + 1}")
    gb_labels = ("Pulse 1", "Pulse 2", "Wavetable", "Noise")
    gb_short = ("S1", "S2", "WA", "NO")
    for cid in gb_ids:
        for voice, (label, short) in enumerate(zip(gb_labels, gb_short)):
            names_long.append(f"{label} ({cid + 1})")
            names_short.append(f"{short}{cid + 1}" if cid else short)
    vsu_labels = ("Voice 1", "Voice 2", "Voice 3", "Voice 4", "Voice 5", "Noise")
    vsu_short = ("W1", "W2", "W3", "W4", "W5", "NO")
    for cid in vsu_ids:
        for label, short in zip(vsu_labels, vsu_short):
            names_long.append(f"VB {label} ({cid + 1})")
            names_short.append(f"{short}{cid + 1}" if cid else short)
    for cid in saa_ids:
        for voice in range(6):
            names_long.append(f"SAA1099 {cid + 1}-{voice + 1}")
            names_short.append(f"A{cid + 1}-{voice + 1}")

    label_parts: list[str] = []
    if fm_present:
        # One entry per family, in module order. A combined log names the OPN2
        # first and the OPL second, matching `systems`.
        listed: list[str] = []
        for system in systems:
            kind = system.kind
            if kind in listed:
                continue
            listed.append(kind)
            n = sum(1 for s in systems if s.kind == kind)
            if kind in ("opl", "opl2", "opl3"):
                base = {"opl": "OPL", "opl2": "OPL2", "opl3": "OPL3"}[kind]
                if system.drums:
                    base += " drums"
            elif kind == "opll":
                base = "OPLL" + (" drums" if system.drums else "")
            elif kind == "ym2610":
                base = "YM2610B" if system.fm_channels > 4 else "YM2610"
            elif kind == "ym2608":
                base = "YM2608" + (" Ext" if system.ext else "")
            elif kind == "ym2612":
                base = "YM2612" + (" Ext" if system.ext else "")
            elif kind == "ym2203":
                base = "YM2203"
            else:
                base = "YM2151"
            label_parts.append(f"{base} x{n}" if n > 1 else base)
    if psg_ids:
        label_parts.append(
            "T6W28" if getattr(song, "psg_t6w28", False)
            else f"SN76489 x{len(psg_ids)}" if len(psg_ids) > 1
            else "SN76489"
        )
    if pcm_ids:
        label_parts.append(
            f"K007232 x{len(pcm_ids)}" if len(pcm_ids) > 1 else "K007232"
        )
    if oki_ids:
        label_parts.append("MSM6295")
    if adpcm_ids:
        label_parts.append("MSM6258")
    if spcm_ids:
        label_parts.append(
            f"SegaPCM x{len(spcm_ids)}" if len(spcm_ids) > 1 else "SegaPCM"
        )
    if rf5c68_smps:
        label_parts.append("RF5C68")
    if rf5c164_smps:
        label_parts.append("RF5C164")
    if fm_present and any(s.kind == "y8950" for s in systems):
        label_parts.append("Y8950")
    if fm_present and any(s.kind == "opl4" for s in systems):
        label_parts.append("OPL4")
    if c140_ids:
        label_parts.append("C140")
    if c352_ids:
        label_parts.append("C352")
    if k053260_ids:
        label_parts.append("K053260")
    if ymz_ids:
        label_parts.append("YMZ280B")
    if scc_ids:
        label_parts.append("SCC+" if song.vgm.k051649_plus else "SCC")
    if ay_ids:
        label_parts.append("AY-8910")
    if nes_ids:
        label_parts.append("NES APU")
    if huc6280_ids:
        label_parts.append("PC Engine")
    if gb_ids:
        label_parts.append("Game Boy")
    if vsu_ids:
        label_parts.append("Virtual Boy")
    if saa_ids:
        label_parts.append(
            "SAA1099 x2" if len(saa_ids) > 1 else "SAA1099"
        )
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
    if saa_ids:
        dual = (
            " (Creative Music System / Game Blaster)"
            if len(saa_ids) > 1 else ""
        )
        comment += (
            f"\nPhilips SAA1099 at "
            f"{song.vgm.saa1099_clock or SAA_DEFAULT_CLOCK} Hz{dual}; each "
            "chip has six square/noise voices and two 3-channel noise "
            "generators. 10xy selects noise/tone, 11xx the noise generator "
            "and 12xx the envelope. The raw amplitude nibbles ride the "
            "instrument's pan-left/pan-right macros with a volume column of "
            "15, so soft pan and the volume envelope stay exact.\n"
        )
    if ym2612:
        comment += (
            f"\nYM2612 at {song.vgm.ym2612_clock or 7670453} Hz; each system "
            "has six FM voices"
            + (" and channel 3 split into four operator columns" if ym_ext else "")
            + ". LFO enable/speed is effect 10xy and per-channel PMS/AMS are "
            "62xx/63xx.\n"
        )
        if dac_smps and opl:
            # The combined shape: the OPN2 never keys an FM voice, it only
            # feeds its DAC. Say so, or the empty FM columns look like a bug.
            dac_channel = next(
                (e.ch for e in song.events if e.dac and e.on), -1
            )
            comment += (
                f"\nThis log's YM2612 has no FM notes: channel "
                f"{dac_channel + 1} is the DAC, and the OPL2 beside it plays "
                f"the music. The stream's PCM is cut into {len(dac_smps)} "
                "rate-exact chunks; one sample-map note starts each of them.\n"
            )
    if psg_ids:
        if getattr(song, "psg_t6w28", False):
            comment += (
                f"\nT6W28 at {song.vgm.sn76489_clock or T6W28_DEFAULT_CLOCK} "
                "Hz; the two SN76489 halves are one stereo chip. The volume "
                "column is the louder side's 4-bit attenuation (15 = "
                "loudest), 08xy splits the two sides, and 20xy selects the "
                "noise mode.\n"
            )
        else:
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
    elif ym2608:
        comment += (
            f"\nYM2608 at {song.vgm.ym2608_clock or YM2608_DEFAULT_CLOCK} Hz; "
            "each system has the FM voices"
            + (", channel 3 split into four operator columns," if ym_ext else ",")
            + " three SSG voices, the chip's six rhythm voices and one ADPCM-B "
            "channel. A rhythm column IS the drum - kick, snare, top cymbal, "
            "hi-hat, tom and rim from the ROM Furnace compiles in - so its note "
            "carries no sample, and the source's global $11 level is folded into "
            "each hit's volume.\n"
        )
    elif ym_chip:
        comment += (
            f"\nYM2203 at {song.vgm.ym2203_clock or 3579545} Hz; each system "
            "has three FM voices then three SSG voices. The SSG register "
            "effects (20xx/21xx/22xy/23xx/24xx) work on the last three "
            "channels of each six-channel block.\n"
        )
    if opll:
        comment += (
            "\nYM2413: instrument 0 is the user patch in $00-$07; every other "
            "voice picks a ROM preset (the wave column, 1-15). The volume "
            "column is the $3x level nibble (0 loudest)."
            + (" Rhythm mode takes channels 7-9 as BD/SD/TM/TP/HH.\n"
               if opll_drums else "\n")
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
    ym2608_sys: dict[int, int] = {}
    opl_sys: dict[int, int] = {}
    if fm_present:
        for system in systems:
            # One module system per chip, in the order the channels were laid
            # out. Each kind carries its own clock flag.
            kind = system.kind
            cid = system.chip_id
            if kind in ("opl", "opl2", "opl3", "y8950", "opl4"):
                chips.append(OPL_CHIP[(kind, bool(system.drums))])
                opl_sys[cid] = len(chips) - 1
                # An OPL core defaults to COLOR_NTSC and an OPL3/OPL4 to four
                # times it. Outside that tolerance the clock decides every
                # f-num, so it has to travel with the module.
                fm_clock = (
                    song.vgm.opl3_clock if kind == "opl3" else
                    song.vgm.opl2_clock if kind == "opl2" else
                    song.vgm.y8950_clock if kind == "y8950" else
                    song.vgm.ymf278b_clock if kind == "opl4" else
                    song.vgm.opl_clock
                )
                is_opl3_clock = kind in ("opl3", "opl4")
                default = FURNACE_CLOCK_OPL3 if is_opl3_clock else FURNACE_CLOCK
                module = opl_module_clock(fm_clock, is_opl3_clock)
                flag_texts.append(
                    "" if module == default else f"customClock={fm_clock}"
                )
                continue
            if kind == "opll":
                chips.append(
                    CHIP_VRC7 if getattr(system, "vrc7", False) else
                    CHIP_OPLL_DRUMS if system.drums else CHIP_OPLL
                )
                # DivPlatformOPLL's named clock selections cover the usual MSX
                # and arcade clocks exactly; use those before a custom clock.
                flag_texts.append(opll_clock_flag(song.vgm.ym2413_clock))
                continue
            if kind == "ym2612":
                chips.append(CHIP_YM2612_EXT if system.ext else CHIP_YM2612)
                fm_clock = song.vgm.ym2612_clock
                # Furnace's Genesis clock list has a named 8 MHz FM Towns
                # entry (clockSel=2).  A source from that machine reports
                # exactly 8 MHz, so ask for the stock setting rather than a
                # custom clock.
                if fm_clock == 8_000_000:
                    flag_texts.append("clockSel=2")
                    continue
                # Furnace's Genesis default is COLOR_NTSC*15/7 truncated to
                # 7670454, while the VGM header normally says 7670453.  Write
                # the source clock so the exported pitch register matches it.
                fm_default = 7670454
            elif kind == "ym2203":
                chips.append(CHIP_YM2203_EXT if system.ext else CHIP_YM2203)
                fm_clock = song.vgm.ym2203_clock
                fm_default = 3579545
            elif kind == "ym2610":
                chips.append(CHIP_YM2610B if system.fm_channels > 4 else CHIP_YM2610)
                ym2610_sys[cid] = len(chips) - 1
                fm_clock = song.vgm.ym2610_clock
                fm_default = 8000000
            elif kind == "ym2608":
                chips.append(CHIP_YM2608_EXT if system.ext else CHIP_YM2608)
                ym2608_sys[cid] = len(chips) - 1
                fm_clock = song.vgm.ym2608_clock or YM2608_DEFAULT_CLOCK
                # Furnace's OPNA clock list has exactly one named entry beyond
                # the 8 MHz default: 38400*13*16, the FM Towns oscillator a
                # real rip reports. Ask for that instead of a custom clock so
                # the module reads as a stock configuration.
                if fm_clock == 38400 * 13 * 16:
                    flag_texts.append("clockSel=1")
                    continue
                fm_default = 8000000
            else:
                chips.append(CHIP_YM2151)
                fm_clock = song.vgm.ym2151_clock
                fm_default = 3579545
            # The cores default to their console clocks.  At any other clock
            # the notes play at the wrong pitch unless the flag carries it.
            flag_texts.append(
                f"customClock={fm_clock}"
                if fm_clock and fm_clock != fm_default else ""
            )
    # The dual-YM3812 balance marks the two OPL chips of that system. In a
    # combined module they are not the first entries, so remember where they
    # start.
    opl_first = (
        len(chips) - sum(1 for s in systems if s.kind == opl_kind)
        if (fm_present and opl) else 0
    )
    t6w28 = bool(psg_ids and getattr(song, "psg_t6w28", False))
    for _cid in psg_ids:
        if t6w28:
            chips.append(CHIP_T6W28)
            psg_clock = song.vgm.sn76489_clock or T6W28_DEFAULT_CLOCK
            if abs(psg_clock - T6W28_DEFAULT_CLOCK) <= 2:
                flag_texts.append("")
            else:
                flag_texts.append(f"customClock={psg_clock}")
            continue
        chips.append(CHIP_SMS)
        psg_clock = song.vgm.sn76489_clock or 3579545
        if _cid == 1 and getattr(song.vgm, "sn76489_clock_2", 0):
            psg_clock = song.vgm.sn76489_clock_2
        # Game Gear selects Furnace's stereo SMPSG core (chipType 3). The
        # plain SMS core is mono, so its 08xy panning would be ignored.
        flags = "chipType=3" if getattr(song, "psg_stereo", False) else ""
        if abs(psg_clock - 3579545) <= 2:
            clock_flag = "clockSel=0"
        else:
            clock_flag = f"customClock={psg_clock}"
        flag_texts.append(flags + ("\n" if flags else "") + clock_flag)
    for _cid in pcm_ids:
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
    for cid in spcm_ids:
        chips.append(CHIP_SEGAPCM)
        rom_size = max(
            (s.rom_size for s in song.vgm.roms
             if s.kind == "segapcm" and s.chip == cid),
            default=0,
        )
        flag_texts.append(segapcm_flag_text(song.vgm.segapcm_clock, rom_size))
    for _cid in rf5c68_ids:
        chips.append(CHIP_RF5C68)
        flag_texts.append(rf5c68_flag_text(song.vgm.rf5c68_clock, 0))
    for _cid in rf5c164_ids:
        chips.append(CHIP_RF5C68)
        flag_texts.append(rf5c68_flag_text(song.vgm.rf5c164_clock, 1))
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
    ymz_sys: dict[int, int] = {}
    for cid in ymz_ids:
        ymz_sys[cid] = len(chips)
        chips.append(CHIP_YMZ280B)
        flag_texts.append(ymz280b_flag_text(ymz280b_clock(song.vgm, cid)))
    for cid in scc_ids:
        chips.append(scc_chip)
        flag_texts.append(k051649_flag_text(song.vgm.k051649_clock))
    # A Famicom's AY is always a Sunsoft 5B (SEL tied low).  Its module type
    # carries the /2, and the analyzer labelled the notes at half speed so the
    # tracker's pitch table writes the doubled period.
    ay_sunsoft5b = bool(nes_ids)
    for cid in ay_ids:
        chips.append(CHIP_AY8910)
        flag_texts.append(ay8910_flag_text(
            song.vgm.ay8910_clock, song.vgm.ay8910_type,
            sunsoft5b=ay_sunsoft5b,
        ))
    for cid in nes_ids:
        chips.append(CHIP_NES)
        # NTSC vs PAL selects the CPU clock; a dump based on the FDS/NSF mapper
        # still runs the same 2A03.
        clock = song.vgm.nes_clock or 1789773
        flag_texts.append(
            "" if abs(clock - 1789773) <= 2 else f"customClock={clock}"
        )
    for cid in huc6280_ids:
        chips.append(CHIP_PCE)
        flag_texts.append(pce_flag_text(song.vgm.huc6280_clock))
    for cid in gb_ids:
        chips.append(CHIP_GB)
        # chipType 2 is the CGB-E core: its single-pass NRx2 rule is what the
        # converter's software-envelope replay follows. A non-standard clock
        # still rides as customClock.
        clock = song.vgm.gb_dmg_clock or 4194304
        flags = "chipType=2"
        if abs(clock - 4194304) > 2:
            flags += f"\ncustomClock={clock}"
        flag_texts.append(flags)
    for cid in vsu_ids:
        chips.append(CHIP_VBOY)
        clock = song.vgm.vsu_clock or 5000000
        flags = "" if clock == 5000000 else f"customClock={clock}"
        if getattr(song, "vsu_dynamic", False):
            # The source rewrites wave RAM while voices are keyed.  Furnace's
            # default anti-click pass disables every voice around a wave
            # upload; the log did not, so keep its intended behaviour.
            flags = (flags + "\n" if flags else "") + "screwThis=true"
        flag_texts.append(flags)
    for _cid in saa_ids:
        chips.append(CHIP_SAA1099)
        flag_texts.append(saa1099_flag_text(song.vgm.saa1099_clock))
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
    # Per-chip balance. Only a Sound Blaster Pro YM3812 pair sets it: the board
    # wires one chip to each speaker, and the log says so in header bit 31.
    chip_pans: list[float] = []
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
        pan = 0.0
        if (opl and opl_kind == "opl2" and len(opl_ids) > 1
                and getattr(song.vgm, "opl2_pan", False)):
            pan = -1.0 if i == opl_first else 1.0
        chip_pans.append(pan)
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
    # boosted chip stores its share of the raised master. All three floats must
    # be written: Furnace reads vol, pan and front/rear per chip in order, and a
    # short array leaves every later offset out of step (its loader then fails on
    # a directory pointer and reports "couldn't read wavetable directory").
    for i, cv in enumerate(chip_vols):
        b.f32(cv / master_gain)
        b.f32(chip_pans[i] if i < len(chip_pans) else 0.0)
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
        if getattr(patch, "opll", False):
            fam = "OPLL"
        elif getattr(patch, "opl", False):
            fam = "OPL3" if getattr(patch, "four", False) else "OPL"
        else:
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
    rf5c68_ins_base = spcm_ins_base + len(spcm_smps)
    for i, smp in enumerate(rf5c68_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"RF5C68 {smp.start:04x}", rf5c68_ins_base + i, INS_RF5C68,
        )
    rf5c164_ins_base = rf5c68_ins_base + len(rf5c68_smps)
    for i, smp in enumerate(rf5c164_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"RF5C164 {smp.start:04x}", rf5c164_ins_base + i, INS_RF5C68,
        )
    c140_ins_base = rf5c164_ins_base + len(rf5c164_smps)
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
    if n_ym2608_rhythm_ins:
        # One empty map serves all six kit columns. ymfm fixes each voice to a
        # range of the rhythm ROM, so a note names nothing: the column is the
        # drum. The instrument exists to give the channel a macro pool and the
        # 0-31 volume scale the platform reports.
        ins_ptrs.append(len(b))
        _write_ins_sample_map(b, "Rhythm", 0, [], INS_ADPCMA)
    for group in range(n_ym2608_b_ins):
        ins_ptrs.append(len(b))
        first = ym2608_b_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(ym2608_b_smps) - group * 120))
        ]
        _write_ins_sample_map(
            b, f"ADPCM-B {group + 1}", 0, maps, INS_ADPCMB,
        )
    for group in range(n_y8950_b_ins):
        ins_ptrs.append(len(b))
        first = y8950_b_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(y8950_b_smps) - group * 120))
        ]
        _write_ins_sample_map(
            b, f"Y8950 ADPCM {group + 1}", 0, maps, INS_ADPCMB,
        )
    for i, smp in enumerate(opl4_wave_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"OPL4 WAVE {smp.wave:03d}", opl4_wave_smp_base + i,
            INS_MULTIPCM,
        )
    # The AY sample path allows 120 bodies per instrument through a note map;
    # notes 60-179 select a slot and every slot plays at C-4.  This keeps a
    # drum-heavy track (hundreds of distinct bodies) inside the 255-instrument
    # module limit.
    for group in range(n_ssg_pcm_ins):
        ins_ptrs.append(len(b))
        first = group * 120
        maps = [
            ssg_pcm_smp_base + first + slot
            for slot in range(min(120, len(ssg_pcm_smps) - first))
        ]
        _write_ins_sample_map(
            b, f"PSG PCM {group + 1}", 0, maps, INS_AY, flags=0x03,
        )
    for i, smp in enumerate(nes_dpcm_smps):
        ins_ptrs.append(len(b))
        # Note 60+rate selects the DMC rate; this instrument names the body.
        _write_ins_pcm(
            b, f"NES DPCM {i:02d}", nes_dpcm_smp_base + i, INS_NES,
        )
    for group in range(n_nes_pcm_ins):
        ins_ptrs.append(len(b))
        first = nes_pcm_smp_base + group * 120
        maps = [
            first + slot
            for slot in range(min(120, len(nes_pcm_smps) - group * 120))
        ]
        # The NES PCM channel takes the same 120-slot sample map the YM2612
        # DAC uses; note 60+slot selects the chunk and it plays at its rate.
        # The sample path also needs useSample, like SSG PCM.
        _write_ins_sample_map(
            b, f"NES PCM {group + 1}", 0, maps, INS_C352, flags=0x03,
        )
    for i, smp in enumerate(huc6280_pcm_smps):
        ins_ptrs.append(len(b))
        # Note 108 (C-4) plays the 8-bit DDA body at its stored rate.
        _write_ins_pcm(
            b, f"HuC6280 PCM {i:02d}", huc6280_pcm_smp_base + i, INS_C352,
        )
    for i, smp in enumerate(ymz_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, f"YMZ{smp.chip_id + 1} {smp.start:06x}",
            ymz_smp_base + i, INS_YMZ280B,
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
    psg_ins_type = (
        INS_T6W28 if getattr(song, "psg_t6w28", False) else INS_STD
    )
    psg_label = "T6W28" if getattr(song, "psg_t6w28", False) else "SN"
    for spec in psg_specs:
        ins_ptrs.append(len(b))
        _write_ins_psg(
            b, psg_label + ("+pitch" if spec.pitch else ""),
            spec, ins_type=psg_ins_type,
        )
    for spec in nes_specs:
        ins_ptrs.append(len(b))
        _write_ins_nes(b, "NES", spec)
    for i, spec in enumerate(huc6280_specs):
        ins_ptrs.append(len(b))
        wave = (
            huc6280_waves[spec.wave]
            if spec.wave < len(huc6280_waves) else b""
        )
        name = f"PCE {i:02d} {_wave_label(wave)}"
        if spec.waves:
            name += "+morph"
        if spec.pitch:
            name += "+pitch"
        _write_ins_pce(b, name, spec)
    for i, spec in enumerate(gb_specs):
        ins_ptrs.append(len(b))
        if getattr(spec, "gb_wave", False):
            wave = (
                gb_waves[spec.wave] if spec.wave < len(gb_waves) else b""
            )
            name = f"GB Wave {i:02d} {_wave_label(wave)}"
            if spec.waves:
                name += "+morph"
        elif getattr(spec, "gb_soft_env", False):
            name = "GB Soft" + ("+pitch" if spec.pitch else "")
        else:
            name = (
                f"GB Env {spec.gb_env_vol:x}{spec.gb_env_dir}"
                f"{spec.gb_env_len}"
                + ("+pitch" if spec.pitch else "")
            )
        _write_ins_gb(b, name, spec)
    for i, spec in enumerate(vsu_specs):
        ins_ptrs.append(len(b))
        mod = (
            vsu_mod_waves[spec.vsu_mod]
            if 0 <= spec.vsu_mod < len(vsu_mod_waves) else bytes(32)
        )
        name = f"VSU {i:02d}"
        if spec.pitch:
            name += "+pitch"
        if spec.vol:
            name += "+vol"
        _write_ins_vsu(
            b, name, spec, mod,
            vsu_wave_base + max(0, int(getattr(spec, "wave", 0))),
        )
    for i, spec in enumerate(saa_specs):
        ins_ptrs.append(len(b))
        name = f"SAA {i:02d}"
        if spec.pitch:
            name += "+pitch"
        if spec.panl or spec.panr:
            name += "+pan"
        if spec.env:
            name += "+env"
        if spec.mode:
            name += "+mode"
        if spec.noise:
            name += "+noise"
        _write_ins_saa(b, name, spec)

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
            b, smp.name, smp.data, smp.rate, swap_nibbles=True,
        )
    for smp in spcm_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"spcm_{smp.start:05x}", smp.data, smp.c4_rate(spcm_clock),
            loop=(smp.loop_start, smp.loop_end),
        )
    for smp in rf5c68_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"rf5c68_{smp.start:04x}", smp.data,
            smp.c4_rate(rf5c68_clock),
            loop=(smp.loop_start, smp.loop_end),
        )
    for smp in rf5c164_smps:
        smp_ptrs.append(len(b))
        _write_sample_s8(
            b, f"rf5c164_{smp.start:04x}", smp.data,
            smp.c4_rate(rf5c164_clock),
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
    ym2608_clock = song.vgm.ym2608_clock or YM2608_DEFAULT_CLOCK
    for smp in ym2608_b_smps:
        sys = ym2608_sys.get(smp.chip_id, 0)
        # The delta-T engine steps its accumulator once per chipClock/144 and
        # the platform turns a sample's rate back into that step value, so this
        # is the register the source wrote.
        rate = max(
            1, int(round(
                ym2608_clock / 144 * (smp.rate & 0xFFFF) / 65535
                * ADPCM_B_RATE_COMPENSATION
            )),
        )
        loop = any(h.loop for h in smp.hits)
        smp_ptrs.append(len(b))
        # The OPNA has one sample bank and reads renderOn[0] (ym2608.cpp:2032),
        # unlike the YM2610, whose delta-T memory is bank 1. The body carries an
        # idle tail out to Furnace's 256-byte sample quantum: the platform plays
        # `lengthB` bytes whether or not the source range was that long, and a
        # short body would read the next sample off its heels.
        _write_sample_adpcm(
            b, f"deltaT_{smp.start:06x}_{smp.rate:04x}",
            deltat_store_body(smp.data), rate,
            SMP_DEPTH_ADPCM_B, (1 << sys, 0, 0, 0), loop=loop,
        )
    y8950_clock = song.vgm.y8950_clock or 3579545
    for smp in y8950_b_smps:
        sys = opl_sys.get(smp.chip_id, 0)
        rate = max(
            1, int(round(
                y8950_clock / 144 * (smp.rate & 0xFFFF) / 65535
                * ADPCM_B_RATE_COMPENSATION
            )),
        )
        loop = any(h.loop for h in smp.hits)
        smp_ptrs.append(len(b))
        # The Y8950 has one ADPCM bank, read from renderOn[0].
        _write_sample_adpcm(
            b, f"y8950_{smp.start:06x}_{smp.rate:04x}",
            deltat_store_body(smp.data), rate,
            SMP_DEPTH_ADPCM_B, (1 << sys, 0, 0, 0), loop=loop,
        )
    opl4_clock = song.vgm.ymf278b_clock or 33868800
    for smp in opl4_wave_smps:
        smp_ptrs.append(len(b))
        # The wave bodies are already signed 16-bit LE.
        _write_sample_s8(
            b, f"opl4_{smp.wave:03d}", smp.data,
            smp.c4_rate(opl4_clock),
            loop=(smp.loop_start, smp.loop_end),
            depth=SMP_DEPTH_16BIT,
        )
    for smp in ssg_pcm_smps:
        smp_ptrs.append(len(b))
        # The bodies are already 8-bit levels encoded for the AY DAC table;
        # `rate` is the source write rate and note 108 plays it 1:1.
        _write_sample_s8(b, smp.name, smp.data, smp.rate)
    nes_clock = song.vgm.nes_clock or 1789773
    for smp in nes_dpcm_smps:
        smp_ptrs.append(len(b))
        _write_sample_dpcm(
            b, f"nes_dpcm_{smp.start:04x}", smp.data,
            nes_dpcm_rate(nes_clock, smp.rate_index), loop=smp.loop,
        )
    for smp in nes_pcm_smps:
        smp_ptrs.append(len(b))
        # 8-bit signed bodies; the sample engine's C-4 reference is a hair
        # sharp, so compensate exactly like the YM2612 DAC bodies.
        rate = max(1, int(round(smp.rate * DAC_RATE_COMPENSATION)))
        _write_sample_s8(b, smp.name, smp.data, rate)
    for smp in huc6280_pcm_smps:
        smp_ptrs.append(len(b))
        # 8-bit bodies converted from the 5-bit DDA stream; note 108 plays
        # them at their byte rate.
        loop = (0, len(smp.data)) if smp.loop else (None, None)
        _write_sample_s8(b, smp.name, smp.data, smp.rate, loop=loop)
    for smp in ymz_smps:
        smp_ptrs.append(len(b))
        sys = ymz_sys.get(smp.chip_id, 0)
        # Only the owning YMZ system uploads this body; a dual log's two
        # chips otherwise both receive the union of their ROMs.
        masks = ((1 << sys) if sys < 32 else 0, 0, 0, 0)
        loop = (
            (smp.loop_start, smp.loop_end)
            if smp.loop_start is not None and smp.loop_end is not None
            else (None, None)
        )
        name = f"ymz{smp.chip_id + 1}_{smp.start:06x}"
        if smp.depth == SMP_DEPTH_YMZ_ADPCM:
            _write_sample_ymz_adpcm(
                b, name, smp.data, smp.c4_rate(), loop=loop,
                render_masks=masks,
            )
        else:
            _write_sample_s8(
                b, name, smp.data, smp.c4_rate(), loop=loop,
                depth=smp.depth, render_masks=masks,
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
    for wave in huc6280_waves:
        # 5-bit samples (0-31); the PCE wavetable height is 31.
        wave_ptrs.append(_write_wave(
            b, f"pce_wave_{_wave_label(wave)}", bytes(wave), maximum=31,
        ))
    for wave in gb_waves:
        # The DMG wave RAM is 32 4-bit samples; height 15. Furnace's GB core
        # XORs each nibble with 15 when it loads the table (the DAC is
        # inverted), so store the complement to make the written wave RAM
        # match the source bytes.
        data = bytes(15 - (v & 0x0F) for v in wave)
        wave_ptrs.append(_write_wave(
            b, f"gb_wave_{_wave_label(wave)}", data, maximum=15,
        ))
    for wave in vsu_waves:
        # The VSU wave RAM is 32 6-bit samples; the platform uploads them
        # unchanged, so the wavetable height is 63.
        wave_ptrs.append(_write_wave(
            b, f"vsu_wave_{_wave_label(wave)}", bytes(wave), maximum=63,
        ))

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
