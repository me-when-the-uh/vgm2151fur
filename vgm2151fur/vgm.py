"""VGM / VGZ loader and command walker.

Chips: YM2151, YM2203, YM2612, YM2610, YM3526 (OPL), YM3812 (OPL2),
YMF262 (OPL3), RF5C68, RF5C164, K007232, MSM6295, MSM6258, SegaPCM, C140,
C352, K053260, YMZ280B, Nintendo Virtual Boy (VSU), AY-3-8910,
K051649/K052539 (SCC), SN76489 and its stereo T6W28 variant.
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

VGM_RATE = 44100
CMD_YM2151 = 0x54
# The OPL family. 0x5B is the YM3526 and 0x5A the YM3812; both address one
# flat register space. The YMF262 has two banks of nine channels, and the VGM
# spec gives each bank its own command: 0x5E is port 0, 0x5F port 1. Every
# per-channel and per-operator register exists in both banks and only the port
# tells them apart. The bank rides in bit 8 of ChipWrite.reg exactly like the
# YM2612's second register bank does.
CMD_YM3526 = 0x5B
CMD_YM3812 = 0x5A
# The Y8950 is an OPL1 with the delta-T (ADPCM-B) engine bolted on. Register
# writes arrive on 0x5C; the ADPCM registers are 0x07 (control), 0x08/0x09
# (start), 0x0A/0x0B (end), 0x10/0x11 (pitch) and 0x12 (level).
CMD_Y8950 = 0x5C
# 0x5D is the YMZ280B (PCMD8): register, value.  A dual-chip log uses the
# second instance at 0xAD, the usual "+0x50" YM-family rule.
CMD_YMZ280B = 0x5D
CMD_YMZ280B_2 = 0xAD
# A dual-OPL2 log (the Sound Blaster Pro layout) keeps the second chip's
# register stream in its own command. Confirmed on the Knights of Xentar rips:
# header bit 30 says "Dual YM3812" and 0xAA carries chip 1's writes while
# 0x5A stays on chip 0. There is no spec'd companion for the YM3526. A second
# OPL instance is reported instead of guessed at.
CMD_YM3812_2 = 0xAA
CMD_OPL3_P0 = 0x5E
CMD_OPL3_P1 = 0x5F
# 0x52/0x53 are the first YM2612's two register banks; 0xA2/0xA3 are the
# second instance in a dual-chip log.  The chip's 9-bit register address is
# stored flat as (bank << 8) | register, matching ymfm's index.
CMD_YM2612_P0 = 0x52
CMD_YM2612_P1 = 0x53
CMD_YM2612_B_P0 = 0xA2
CMD_YM2612_B_P1 = 0xA3
# 0x58/0x59 are the YM2610's two register ports; 0xA8/0xA9 are the second
# instance.  Port 0 holds SSG, ADPCM-B and the first two FM channels; port 1
# holds ADPCM-A and the remaining FM channels.
CMD_YM2610_P0 = 0x58
CMD_YM2610_P1 = 0x59
CMD_YM2610_B_P0 = 0xA8
CMD_YM2610_B_P1 = 0xA9
# 0x56/0x57 are the YM2608's two register buses; 0xA6/0xA7 the second chip
# instance.  Bus A holds SSG (<0x10), rhythm (0x10-0x1F) and FM block 1;
# bus B holds ADPCM-B (<=0x0F) and FM block 2.  Bus B rides in bit 8 of reg
# like the YM2612's bank, so every OPN decode can treat one flat address space.
CMD_YM2608_P0 = 0x56
CMD_YM2608_P1 = 0x57
CMD_YM2608_B_P0 = 0xA6
CMD_YM2608_B_P1 = 0xA7
# 0x51 is the YM2413; its dual instance uses 0xA1 (YM family: +0x50). The chip
# addresses one flat space; the banked user-instrument registers are < 0x10 and
# rhythm control is $0E.  Bit 31 of the clock field flags a Konami VRC7, which
# is the same core with six channels and a different preset ROM.
CMD_YM2413 = 0x51
CMD_YM2413_2 = 0xA1
CMD_SN76489 = 0x50
# 0x30 is the second SN76489 in a dual/T6W28 log; 0x3F/0x4F are the second/
# first Game Gear stereo masks (Furnace's vgmOps uses 0x3f for the second).
CMD_SN76489_2 = 0x30
CMD_GGSTEREO = 0x4F
CMD_GGSTEREO_2 = 0x3F
# 0xA4 is the second YM2151 (YM family: +0x50).
CMD_YM2151_2 = 0xA4
# The OPL family's second-instance commands. 0xAB/0xAC are the YM3526 and
# Y8950 pairs, 0xAE/0xAF the YMF262's two ports.
CMD_YM3526_2 = 0xAB
CMD_Y8950_2 = 0xAC
CMD_OPL3_B_P0 = 0xAE
CMD_OPL3_B_P1 = 0xAF
# 0xB3 is the Game Boy DMG (LR35902) APU: register, data.  Register 0x00 maps
# FF10 (NR10), so 0x00-0x16 are the sound registers and 0x20-0x2F the wave RAM.
# The DMG has a single APU, so there is no instance bit.
CMD_GB = 0xB3
# 0xB4 is the NES APU (2A03): register, data.  Reg 0x00-0x17 map $4000-$4017;
# bit 7 of the register selects the second APU instance.
CMD_NES = 0xB4
# 0xB9 is the HuC6280 PSG (PC Engine/TurboGrafx-16): register, data.  The
# register is a channel-select latch at 0x00 followed by per-channel writes;
# bit 7 of the register selects the second chip instance.
CMD_HUC6280 = 0xB9
CMD_DAC_WRITE = 0x2A
CMD_DAC_ENABLE = 0x2B
# 0x55 is the first YM2203's register port; 0xA5 is the second instance in a
# dual-chip log.  Registers 0x00-0x0F are the built-in SSG, everything above
# is FM.
CMD_YM2203 = 0x55
CMD_YM2203_2 = 0xA5
CMD_K007232 = 0x41
CMD_AY8910 = 0xA0
CMD_MSM6258 = 0xB7
CMD_MSM6295 = 0xB8
CMD_SEGAPCM = 0xC0
CMD_RF5C68 = 0xB0
CMD_RF5C164 = 0xB1
CMD_RF5C68_MEM = 0xC1
CMD_RF5C164_MEM = 0xC2
# YMF278B/OPL4: port 0/1 are the OPL3-compatible FM banks, port 2 the 24
# wave slots (10 registers each, 0x08-0xF7).
CMD_YMF278B = 0xD0
CMD_SCC = 0xD2
CMD_C140 = 0xD4
CMD_C352 = 0xE1
CMD_K053260 = 0xBA
# 0xC7 mmll dd is the Virtual Boy VSU: the register is a 16-bit big-endian
# *word index* (byte address / 4).  Furnace writes it as writeS_BE(addr>>2),
# and the reference rips use the same form.  Bit 15 selects a second VSU in a
# dual-chip log (Furnace's baseAddr2S rule).
CMD_VSU = 0xC7
VSU_DUAL_BIT = 0x8000
CMD_DATA_BLOCK = 0x67
CMD_END = 0x66
CMD_STREAM_SETUP = 0x90
# Stream-control chip type (0x90 tt), not the extra-header id. OKIM6258 is 0x17
# and the HuC6280 is 0x1B (libvgm's device index).
STREAM_OKIM6258 = 0x17
STREAM_C6280 = 0x1B
# The YM2612 DAC stream driver, the other type this tool reconstructs. Its data
# bank is the type-0x00 PCM database and its register is $2A.
DAC_STREAM_YM2612 = 0x02
K007232_ROM_TYPE = 0x94  # 0x80 + 0x14
OKI_ROM_TYPE = 0x8B  # OKIM6295 ROM
SEGAPCM_ROM_TYPE = 0x80  # SegaPCM ROM
# NES APU DPCM sample RAM (data-block type 0xC2): 16-bit start then bytes.
# It shares the 0xC2 number with the RF5C164 memory command, but this is the
# data-block type byte, not a stream command.
NES_DPCM_RAM_TYPE = 0xC2
# Type 0x07 is the NES APU DPCM *database*: a flat bank the 0x68 RAM-write
# command copies into $C000/$D000 while the song plays.
NES_DPCM_BANK_TYPE = 0x07
# Type 0x05 is the HuC6280 PCM database.  A DAC stream (0x91 bank id 5) plays
# 8-bit DDA levels out of the concatenation of these blocks.
HUC6280_PCM_TYPE = 0x05
RF5C68_RAM_TYPE = 0xC0  # RF5C68 sample-RAM write (16-bit start + data)
RF5C164_RAM_TYPE = 0xC1  # RF5C164 sample-RAM write (16-bit start + data)
Y8950_DELTAT_ROM_TYPE = 0x88  # Y8950 delta-T (ADPCM-B) ROM
YMF278B_ROM_TYPE = 0x84  # YMF278B (OPL4) ROM image
YMF278B_RAM_TYPE = 0x87  # YMF278B (OPL4) RAM image
YMZ280B_ROM_TYPE = 0x86  # YMZ280B external sample ROM
C140_ROM_TYPE = 0x8D  # Namco C140 ROM
C352_ROM_TYPE = 0x92  # Namco C352 ROM
YM2610_ADPCM_ROM_TYPE = 0x82  # YM2610 ADPCM-A/B ROM
YM2610_DELTAT_ROM_TYPE = 0x83  # YM2610 delta-T ROM
YM2608_DELTAT_ROM_TYPE = 0x81  # YM2608 ADPCM-B (delta-T) ROM
K053260_ROM_TYPE = 0x8E  # Konami K053260 ROM
K007232_TRIGGER_REG = 0x1F
# Extra-header chip ids (libvgm device index). 0x02 is YM2612, 0x2A K007232.
EXTRA_MSM6258 = 0x16
EXTRA_MSM6295 = 0x17
EXTRA_YM2612 = 0x02
EXTRA_YM2203 = 0x06
EXTRA_YM2610 = 0x08
EXTRA_SEGAPCM = 0x04
EXTRA_AY8910 = 0x12
EXTRA_K051649 = 0x19
EXTRA_HUC6280 = 0x1B
EXTRA_C140 = 0x1C
EXTRA_K053260 = 0x1D
EXTRA_C352 = 0x27
EXTRA_K007232 = 0x2A
EXTRA_VSU = 0x22  # Virtual Boy VSU (VGM chip-volume id 34)
EXTRA_YMZ280B = 0x0F
# Main-header clocks (VGMFile.h layout, spec v1.71). 0x98 is MSM6295, 0x90
# MSM6258, 0x94 its flags. K007232 has no spec field; VGMPlay tools put it at
# 0xE8 (1.71+). C140 is 0xA8 and its type byte 0x96. AY-8910 is 0x74 with its
# type byte at 0x78 and flags at 0x79 (1.51+); K051649 is 0x9C (1.61+), where
# bit 31 selects the K052539 (SCC-I / SCC+) variant.
SEGAPCM_CLOCK_OFF = 0x38
SEGAPCM_INTF_OFF = 0x3C  # bank shift; mask is the byte at 0x3E (0 → 0x70)
SEGAPCM_MASK_OFF = 0x3E
# RF5C68 header 0x40 and RF5C164 header 0x6C (both VGM 1.51+). The two chips
# share one register interface: $B0/$B1 write (reg, value) and $C1/$C2 write
# (offset, value) into sample RAM. Furnace hosts both as one system with a
# chipType flag, so the converter reads the clock to tell them apart.
RF5C68_CLOCK_OFF = 0x40
RF5C164_CLOCK_OFF = 0x6C
RF5C68_DEFAULT_CLOCK = 8000000
RF5C164_DEFAULT_CLOCK = 12500000
# Y8950 header clock 0x58 (VGM 1.51+). The FM half is an OPL1 and the ADPCM
# half the same delta-T engine the YM2608 carries; its ROM block is 0x88.
Y8950_CLOCK_OFF = 0x58
Y8950_DEFAULT_CLOCK = 3579545
# YMF278B/OPL4 header clock 0x60 (VGM 1.51+), usual value 33868800. The FM
# half is OPL3-shaped and the wave half has 24 sample slots fed by the 0x84
# ROM and 0x87 RAM blocks.
YMF278B_CLOCK_OFF = 0x60
YMF278B_DEFAULT_CLOCK = 33868800
# YMZ280B header 0x68 (VGM 1.51+).  Bit 30 marks a second chip; its writes
# arrive on command 0xAD and a data block's size bit 31 selects its ROM.
YMZ280B_CLOCK_OFF = 0x68
YMZ280B_DEFAULT_CLOCK = 16934400
YMZ280B_DUAL_BIT = 0x40000000
# Virtual Boy VSU clock at 0x68? No: VGM 1.71 puts it at 0xC4.  Bit 30 marks
# a second chip, whose writes use register bit 15.  The usual clock is 5 MHz.
VSU_CLOCK_OFF = 0xC4
VSU_DEFAULT_CLOCK = 5_000_000
VSU_HEADER_DUAL_BIT = 0x40000000
# YM2612 clock at 0x2C.  Bit 30 is the dual-instance flag; 0xA2/0xA3 then
# answer for the second chip.  The field predates the 1.50 data-offset header.
YM2612_CLOCK_OFF = 0x2C
YM2612_DUAL_BIT = 0x40000000
YM2612_DEFAULT_CLOCK = 7670453  # NTSC Genesis master clock (COLOR_NTSC * 15/7)
# VGM 0x0C is the SN76489 clock.  The Genesis feeds it COLOR_NTSC/1 = 3579545;
# Furnace's SMS core runs the same clock and derives note periods with
# toneDivider 64.
SN76489_CLOCK_OFF = 0x0C
SN76489_DEFAULT_CLOCK = 3579545
# The clock field's high bits are the usual VGM flags: bit 30 marks a second
# SN76489 instance, and bit 31 selects the Toshiba T6W28 variant, whose two
# halves are the same silicon wired as a stereo pair.  vgm2txt labels a
# field T6W28 only when both bits are set; the second half then answers on
# command 0x30.
SN76489_DUAL_BIT = 0x40000000
SN76489_T6W28_BIT = 0x80000000
EXTRA_SN76489 = 0x00  # extra-header clock id for the first/only SN76489
# YM2203 clock at 0x44.  Bit 30 is the "cheat mode" flag for a second
# instance, which then answers on command 0xA5; the clock itself is the same.
YM2203_CLOCK_OFF = 0x44
YM2203_DUAL_BIT = 0x40000000
YM2203_DEFAULT_CLOCK = 3579545
# YM2610 clock at 0x4C.  Bit 31 selects the YM2610B (six FM channels), bit 30
# marks the dual-instance cheat.  The B bit is part of the same field.
YM2610_CLOCK_OFF = 0x4C
YM2610_B_BIT = 0x80000000
YM2610_DUAL_BIT = 0x40000000
# YM2608 joined the header in 1.51 at 0x48; bit 30 is its second instance
# ($A6/$A7). The rhythm/DTA stream additions are 1.61, but the clock field
# itself is 1.51 and rips sometimes carry older versions with it set anyway,
# so version-gate the field, not the feature. YM2413 shares the grid at 0x10;
# for VGM <= 1.4 that dword held the YM2612 clock (spec note on 0x2C).
YM2608_CLOCK_OFF = 0x48
YM2608_DUAL_BIT = 0x40000000
YM2413_CLOCK_OFF = 0x10
# Bit 31 of the YM2413 clock field flags a Konami VRC7 (same OPLL core, six
# channels, its own preset ROM); the low 30 bits stay the clock.
YM2413_VRC7_BIT = 0x80000000
YM2610_DEFAULT_CLOCK = 8000000
# Game Boy DMG header 0x80 (VGM 1.51+).  The clock is the DMG CPU clock, always
# 4194304 Hz on real hardware.  It sits just before the NES APU field.
GB_DMG_CLOCK_OFF = 0x80
GB_DMG_DEFAULT_CLOCK = 4194304
EXTRA_GB = 0x13            # extra-header clock-list chip id (libvgm DMG)
# NES APU header 0x84 (VGM 1.51+).  0x80 is the Game Boy DMG clock, inserted
# ahead of the NES when the header grew; the NES field moved to 0x84.  Bit 30
# marks a second APU, bit 31 an FDS channel.  The clock is the CPU clock:
# COLOR_NTSC / 2 on an NTSC console.  (This was 0x80 and worked only because
# the NTSC default fallback happened to match the files.)
NES_CLOCK_OFF = 0x84
NES_DUAL_BIT = 0x40000000
NES_FDS_BIT = 0x80000000
NES_DEFAULT_CLOCK = 1789773  # COLOR_NTSC / 2
EXTRA_NES = 0x14             # extra-header clock-list chip id
# HuC6280 (PC Engine) header 0xA4 (VGM 1.61+).  The clock is the PSG clock
# (COLOR_NTSC on NTSC hardware); the CPU/PSG core divides it internally.
HUC6280_CLOCK_OFF = 0xA4
HUC6280_DEFAULT_CLOCK = 3579545  # COLOR_NTSC, the PSG clock
# The OPL family joined the header in 1.51: YM3812 at 0x50, YM3526 at 0x54 and
# YMF262 at 0x5C. Bit 30 of the YM3812 field is the dual-chip flag that lets
# command 0xAA address the second one (the Sound Blaster Pro pair); rips also
# set bit 31, which the players use for their own chip-count bookkeeping. The
# spec defines no second-instance command for the YM3526. A dual OPL flag is
# reported rather than guessed at.
OPL2_CLOCK_OFF = 0x50
OPL_CLOCK_OFF = 0x54
OPL3_CLOCK_OFF = 0x5C
OPL_DUAL_BIT = 0x40000000
OPL_DEFAULT_CLOCK = 3579545    # COLOR_NTSC, the AdLib and arcade standard
OPL2_DEFAULT_CLOCK = 3579545   # YM3812; the same crystal as the YM3526
OPL3_DEFAULT_CLOCK = 14318180  # COLOR_NTSC * 4, the OPL3 board standard
AY8910_CLOCK_OFF = 0x74
AY8910_TYPE_OFF = 0x78
AY8910_FLAGS_OFF = 0x79
MSM6258_CLOCK_OFF = 0x90
MSM6295_CLOCK_OFF = 0x98
K051649_CLOCK_OFF = 0x9C
K007232_EXTRA_CLOCK_OFF = 0xE8
C140_CLOCK_OFF = 0xA8
C140_TYPE_OFF = 0x96
K053260_CLOCK_OFF = 0xAC  # VGM 1.61+, one clock for both instances
K053260_DEFAULT_CLOCK = 3579545
C352_CLOCK_OFF = 0xDC  # VGM 1.71 crystal; bit 31 mutes the rear pair
C352_DIV_OFF = 0xD6  # hardware divider / 4; 72 means a divider of 288
SEGAPCM_CLOCK_X = 4000000  # X board / Hang-On
SEGAPCM_CLOCK_Y = 4026987  # Y board: 32215900/8
C140_DEFAULT_CLOCK = 12288000  # System 2/21 rips with a bogus header field
AY8910_DEFAULT_CLOCK = 1789772  # COLOR_NTSC/2, the MSX/ZX PSG clock
K051649_DEFAULT_CLOCK = 1789772  # MSX SCC (half the 3.579545 MHz slot clock)
CLOCK_MIN = 100_000
CLOCK_MAX = 40_000_000
# Sample-chip write commands this tool does not convert. Anything here that
# shows up in a stream means samples are being dropped. Say so.
UNCONVERTED_SAMPLE_CMDS = {
    0xB2: "PWM",
    0xB5: "MultiPCM", 0xB6: "uPD7759", 0x40: "NES APU DPCM",
}

# Commands defined by the VGM spec. 0x60/0x64/0x65/0x69-0x6F are undefined;
# 0x00-0x2F are reserved. Anything outside this set stops parsing.
KNOWN_CMDS = (
    {0x61, 0x62, 0x63, 0x66, 0x67, 0x68}
    | set(range(0x30, 0x60))
    | set(range(0x70, 0x96))
    | set(range(0xA0, 0x100))
)


def read_vgm_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def data_offset(buf: bytes) -> int:
    if len(buf) < 0x40 or buf[:4] != b"Vgm ":
        raise ValueError(f"not a VGM: {buf[:4]!r}")
    ver = int.from_bytes(buf[8:12], "little")
    if ver < 0x150:
        return 0x40
    rel = int.from_bytes(buf[0x34:0x38], "little")
    return 0x34 + rel if rel else 0x40


def cmd_size(cmd: int) -> int:
    if cmd in (0x62, 0x63, 0x66) or 0x70 <= cmd <= 0x8F:
        return 1
    if cmd in (0x4F, 0x50, 0x94) or 0x30 <= cmd <= 0x3F:
        return 2
    if cmd == 0x61 or 0x40 <= cmd <= 0x4E or 0x51 <= cmd <= 0x5F or 0xA0 <= cmd <= 0xBF:
        return 3
    if 0xC0 <= cmd <= 0xDF:
        return 4
    if cmd in (0x90, 0x91, 0x95) or 0xE0 <= cmd <= 0xFF:
        return 5
    if cmd == 0x92:
        return 6
    if cmd == 0x93:
        return 11
    if cmd == 0x68:
        return 12
    return 2


def parse_gd3(buf: bytes) -> dict[str, str]:
    keys = (
        "track", "track_jp", "game", "game_jp", "system", "system_jp",
        "author", "author_jp", "date", "converter", "notes",
    )
    out = {k: "" for k in keys}
    if len(buf) < 0x18:
        return out
    rel = int.from_bytes(buf[0x14:0x18], "little")
    if not rel:
        return out
    pos = 0x14 + rel
    if buf[pos:pos + 4] != b"Gd3 ":
        return out
    pos += 12
    for key in keys:
        end = pos
        while end + 1 < len(buf) and buf[end:end + 2] != b"\x00\x00":
            end += 2
        out[key] = buf[pos:end].decode("utf-16-le", "replace")
        pos = end + 2
        if pos >= len(buf):
            break
    return out


@dataclass
class RomSlice:
    chip: int
    rom_size: int
    start: int
    data: bytes
    kind: str = "k007232"  # "k007232" | "msm6295" | "segapcm" | "c140" |
    #                      "c352" | "k053260"
    # Stream position of a RAM-image dump (0xC0-0xDF). A driver can reload the
    # same NES DPCM window several times while the song plays, so the samples
    # are only valid from this point on. ROM dumps leave it at 0.
    time: int = 0


@dataclass
class Msm6258Play:
    """One OKIM6258 DAC-stream start (0x95, or 0x93).

    `block` indexes `VgmFile.adpcm_blocks`. `length_mode` 3 plays that whole
    block; 1 and 2 play `nbytes` commands or milliseconds from `offset` in the
    concatenated type-0x04 bank (`block` is then ignored). `rate` is the 0x92
    byte rate. `end` is the matching 0x94 sample, or 0 when the stream is not
    stopped.
    """

    sample: int
    chip_id: int
    block: int
    rate: int
    loop: bool = False
    reverse: bool = False
    offset: int = 0
    nbytes: int = 0
    end: int = 0
    step: int = 1
    step_base: int = 0
    length_mode: int = 3


@dataclass
class PcmRamWrite:
    """One 0x68 PCM RAM write (a chunk copied from a data-block bank).

    NES DPCM drivers bank their sample RAM by copying 4 KB windows out of a
    type-0x07 database into ``$C000``/``$D000`` as the song plays, so the DPCM
    collector has to replay these in time order before reading a sample.
    """

    sample: int
    type: int
    chip_id: int
    src: int
    dest: int
    length: int


@dataclass
class DacStreamPlay:
    """One generic DAC-stream start (0x93) for a chip other than the OKIM6258.

    `offset` and `length` address the bank the stream's 0x91 selected.  For the
    YM2612 that bank is the concatenated type-0x00 PCM database (`VgmFile.
    dac_data`), so the pair is a byte range of the sample ROM.  `mode` is the
    raw 0x93 byte: bits 0-2 choose the length unit (1 = commands/bytes, 2 =
    milliseconds, 3 = the rest of the bank), bit 7 loops and bit 4 reverses.
    `rate` is the 0x92 byte rate (bytes per second), and `step`/`step_base`
    are the 0x91 values.
    """

    sample: int
    chip_type: int
    chip_id: int
    stream: int
    port: int
    reg: int
    offset: int
    length: int
    mode: int
    rate: int = 0
    loop: bool = False
    reverse: bool = False
    step: int = 1
    step_base: int = 0
    bank: int = 0
    # 0x95 plays a whole data block by id; -1 for a 0x93 offset play.
    block: int = -1
    # The 0x94 stop (or the next 0x93 start) that ends this play.  0 = the
    # stream ran to its declared length.
    stop_sample: int = 0

    @property
    def length_mode(self) -> int:
        return self.mode & 0x07


@dataclass
class ChipWrite:
    sample: int
    chip: str  # "ym2151" | "ym2203" | "ym2612" | "ym2610" | "opl" | "opl2" |
    #            "opl3" | "k007232" | "msm6295" | "msm6258" | "segapcm" |
    #            "c140" | "c352" | "k053260" | "ay8910" | "k051649" |
    #            "ymz280b" | "vsu" | "sn76489" (chip_id 1 = T6W28 right half)
    chip_id: int
    reg: int
    val: int


@dataclass
class VgmFile:
    path: Path
    buf: bytes
    version: int
    # Corrected to the 0x66 sample when the header disagrees: several logs
    # declare a length far past their last command, and patterns, grid
    # budgets and source-vs-export checks need the timeline the log covers.
    total_samples: int
    loop_samples: int
    loop_file_pos: int | None
    loop_sample: int | None
    ym2151_clock: int
    k007232_clock: int
    gd3: dict[str, str]
    ym2151_dual: bool = False
    # VGM header 0x7C: output volume of the first chip (0x100 = unity).
    # 0.0 means the field is absent; the player default then applies.
    main_volume: float = 0.0
    roms: list[RomSlice] = field(default_factory=list)
    writes: list[ChipWrite] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # YM2612 header 0x2C.  A dual-chip log carries bit 30; 0x52/0x53 target
    # the first instance and 0xA2/0xA3 the second.  Register addresses are
    # kept flat as bank << 8 | register.
    ym2612_clock: int = 0
    ym2612_dual: bool = False
    # YM2610 header 0x4C.  Bit 31 selects the B variant, bit 30 the second
    # instance.  Port 1 registers are stored as 0x100 | register.
    ym2610_clock: int = 0
    ym2610_dual: bool = False
    ym2610_b: bool = False
    # YM2608 header 0x48 (VGM 1.51+). Bit 30 marks a second chip; its writes
    # arrive on $A6/$A7. Both the dual bit and the rhythm/ADPCM-B blocks are
    # new in 1.61, so version-gate them the way the header is gated elsewhere.
    ym2608_clock: int = 0
    ym2608_dual: bool = False
    # YM2413 header 0x10 (the first chip-clock field the format ever had).
    # The dual instance writes via 0xA1 with bit 30 set here.  Bit 31 asks for
    # a Konami VRC7: same OPLL register stream, different preset ROM.
    ym2413_clock: int = 0
    ym2413_dual: bool = False
    ym2413_vrc7: bool = False
    # Game Boy DMG header 0x80 (VGM 1.61+).  Writes arrive on command 0xB3;
    # register 0x00 maps FF10.  Bit 30 of the clock field is a second APU, and
    # the second instance uses bit 7 of the register byte (Dual Chip Support #2).
    gb_dmg_clock: int = 0
    gb_dmg_dual: bool = False
    # NES APU (2A03) header 0x84 (VGM 1.51+).  Writes arrive on command 0xB4;
    # bit 30 of the clock field is a second APU and bit 31 an FDS channel.
    nes_clock: int = 0
    nes_dual: bool = False
    nes_fds: bool = False
    # DPCM sample data.  The type-0x07 database is concatenated here and the
    # 0x68 commands copy windows of it into the $C000/$D000 RAM as they play.
    nes_dpcm_bank: bytearray = field(default_factory=bytearray)
    pcm_ram_writes: list[PcmRamWrite] = field(default_factory=list)
    # HuC6280 (PC Engine) header 0xA4 (VGM 1.61+).  Writes arrive on command
    # 0xB9; the clock is the PSG clock.  Bit 7 of the register selects a
    # second chip instance.
    huc6280_clock: int = 0
    huc6280_dual: bool = False
    # HuC6280 PCM database (data-block type 0x05), concatenated in file order.
    # A DAC stream with bank id 5 plays 8-bit DDA levels out of this bank;
    # 0x95 block ids index the individual blocks in `huc6280_pcm_blocks`.
    huc6280_pcm: bytearray = field(default_factory=bytearray)
    huc6280_pcm_blocks: list[bytes] = field(default_factory=list)
    # Genesis SN76489 header 0x0C.  The data bytes arrive as 0x50 writes;
    # they are a serial register stream, not address/data pairs.
    sn76489_clock: int = 0
    sn76489_clock_2: int = 0
    # Bit 30 of the clock field means a second SN76489; its serial bytes
    # arrive on command 0x30.  Bit 31 (with bit 30) selects the Toshiba
    # T6W28: one stereo chip whose halves are the two register streams.
    sn76489_dual: bool = False
    sn76489_t6w28: bool = False
    # A Game Gear rip writes command 0x4F (stereo mask) besides the serial
    # data bytes. A mask other than 0xFF means the left/right enables are in
    # use, so the module needs the Game Gear variant.
    sn76489_stereo: bool = False
    # VGM 1.50 YM2612 PCM: data block type 0x00 is the sample bank, and
    # 0xE0 sets the read pointer.  0x80-0x8F play one byte (plus a 0-15
    # sample wait).  0x2A is a direct DAC byte.  Events keep stream order:
    # (sample, 0=set pointer, 1=play byte, 2=direct DAC write, value).
    dac_data: bytearray = field(default_factory=bytearray)
    dac_events: list[tuple[int, int, int]] = field(default_factory=list)
    # Modern DAC stream control (0x90-0x95).  Furnace exports a module as
    # 0x90/0x91 once, then 0x92 (rate) + 0x95 (one type-0 block) per sample.
    dac_blocks: list[bytes] = field(default_factory=list)
    dac_block_plays: list[tuple[int, int, int]] = field(default_factory=list)
    # 0x93 starts a stream at an offset/length inside a data bank.  Furnace
    # exports a long PCM play this way, so the YM2612 path slices dac_data with
    # these instead of replaying a whole block.
    dac_stream_plays: list[DacStreamPlay] = field(default_factory=list)
    dac_stream_rates: dict[int, int] = field(default_factory=dict)
    # YM2203 header 0x44.  A dual-chip log carries bit 30 in the clock field;
    # both instances share the clock.  0x55 targets the first chip, 0xA5 the
    # second.  Registers 0x00-0x0F are the SSG.
    ym2203_clock: int = 0
    ym2203_dual: bool = False
    # The OPL family (header 1.51+): YM3812 at 0x50, YM3526 at 0x54, YMF262 at
    # 0x5C.  Bit 30 of a clock field flags a second chip and bit 31 pans the
    # pair hard left/right, which is how a Sound Blaster Pro wires its two
    # YM3812s.  Only the YM3812 pair has a stream this tool reads (command 0xAA
    # carries chip 1); a flagged second OPL or OPL3 is reported, not guessed at.
    opl_clock: int = 0
    opl_dual: bool = False
    opl2_clock: int = 0
    opl2_dual: bool = False
    opl2_pan: bool = False
    opl3_clock: int = 0
    opl3_dual: bool = False
    # MSM6295 header 0x98 (VGM 1.61+). Bit 31 of the raw field is pin 7,
    # bit 30 marks a second chip (the player halves each instance's volume).
    msm6295_clock: int = 0
    msm6295_pin7: bool = False
    msm6295_dual: bool = False
    # MSM6258 header 0x90 and flag byte 0x94 (divider in bits 0-1).
    # Bit 30 marks a second chip (register bit 7 selects it).
    msm6258_clock: int = 0
    msm6258_flags: int = 0
    msm6258_dual: bool = False
    # SegaPCM header 0x38 (VGM 1.51+); C140 header 0xA8 + type byte 0x96
    # (0 = System 2, 1 = System 21, 2 = ASIC219) from 1.61.
    # Bank shift is header byte 0x3C, mask is 0x3E (libvgm). Mask 0 means 0x70.
    segapcm_clock: int = 0
    segapcm_bankshift: int = 0
    segapcm_bankmask: int = 0
    segapcm_dual: bool = False
    c140_clock: int = 0
    c140_type: int = 0
    c140_dual: bool = False
    # RF5C68 header 0x40 and RF5C164 header 0x6C (VGM 1.51+). One register
    # interface, two clocks: FM Towns runs the 68 at 8 MHz, Sega CD the 164 at
    # 12.5 MHz. Furnace hosts both as the RF5C68 system with a chipType flag.
    rf5c68_clock: int = 0
    rf5c164_clock: int = 0
    rf5c68_dual: bool = False
    rf5c164_dual: bool = False
    # Y8950 header 0x58 (VGM 1.51+): OPL1 FM plus one delta-T ADPCM voice.
    y8950_clock: int = 0
    y8950_dual: bool = False
    # YMF278B (OPL4) header 0x60 (VGM 1.51+): OPL3-shaped FM plus 24 wave slots.
    ymf278b_clock: int = 0
    ymf278b_dual: bool = False
    # YMZ280B header 0x68 (VGM 1.51+): eight sample voices.  Bit 30 marks a
    # second chip, whose writes arrive on 0xAD and whose ROM is the type-0x86
    # data block with the size high bit set.
    ymz280b_clock: int = 0
    ymz280b_clock_2: int = 0
    ymz280b_dual: bool = False
    # Virtual Boy VSU header 0xC4 (VGM 1.71+).  Six voices: five 32-entry
    # 6-bit wavetable channels and one noise channel.  Register writes arrive
    # on 0xC7 as a 16-bit big-endian word index; bit 15 selects a second chip.
    vsu_clock: int = 0
    vsu_clock_2: int = 0
    vsu_dual: bool = False
    # K053260 header 0xAC (VGM 1.61+); bit 7 of a 0xBA register selects the
    # second instance, which shares this clock.
    k053260_clock: int = 0
    k053260_dual: bool = False
    # C352 header 0xDC / divider byte 0xD6 (VGM 1.71). 0 means the chip is absent;
    # the analyzer supplies 24192000 only when it is actually converting C352.
    c352_clock: int = 0
    c352_divider: int = 0
    c352_mute_rear: bool = False
    # AY-8910 header 0x74 with type byte 0x78 (0 = AY-8910, 0x10 = YM2149)
    # and flags 0x79 (VGM 1.51+). Second instance clock comes from the extra
    # header (chip id 0x12).
    ay8910_clock: int = 0
    ay8910_type: int = 0
    ay8910_flags: int = 0
    ay8910_dual: bool = False
    # K051649 / K052539 (SCC) header 0x9C (VGM 1.61+), bit 31 set for the
    # K052539 variant. The clock is the value both libvgm and Furnace run the
    # chip at: pitch = clock / (16 * (period + 1)).
    k051649_clock: int = 0
    k051649_plus: bool = False
    k051649_dual: bool = False
    # K007232 header 0xE8 (VGM 1.71+); register bit 7 selects a second chip.
    k007232_dual: bool = False
    # Extra-header clocks, raw (bit 31 kept). Keyed by libvgm chip id.
    extra_clocks: dict[int, int] = field(default_factory=dict)
    # Extra-header volume mix: chip id -> (flags, raw value, 0x100 = 1.0).
    extra_volumes: dict[int, tuple[int, int]] = field(default_factory=dict)
    stream_setups: int = 0
    # OKIM6258 ADPCM bank. Data-block type 0x04, in file order. 0x95 block ids
    # index this list. Plays come from streams whose 0x90 chip type is 0x17;
    # other streams stay on dac_block_plays.
    adpcm_blocks: list[bytes] = field(default_factory=list)
    msm6258_plays: list[Msm6258Play] = field(default_factory=list)

    def chip_volume(self, chip: int) -> float | None:
        """Relative output volume of a chip from the extra header, or None.

        The rip pipeline writes the mix here (K007232 is normally 0.2-0.3 of
        the YM2151). Only plain entries (flags 0) are used; anything else is
        reported as absent so the caller keeps its own default.
        """
        entry = self.extra_volumes.get(chip)
        if entry is None or entry[0] != 0:
            return None
        return entry[1] / 256.0

    @property
    def name(self) -> str:
        return self.gd3.get("track") or self.path.stem


def _field_in_header(buf: bytes, off: int, n: int) -> bool:
    """True when off lies in the header, before the command stream."""
    if off < 0 or len(buf) < off + n:
        return False
    if len(buf) < 0x40 or buf[:4] != b"Vgm ":
        return True
    return off + n <= data_offset(buf)


def _header_raw(buf: bytes, off: int) -> int:
    if not _field_in_header(buf, off, 4):
        return 0
    return int.from_bytes(buf[off:off + 4], "little")


def _header_clock(buf: bytes, off: int) -> int:
    return _header_raw(buf, off) & 0x3FFFFFFF


def _sane_clock(raw: int) -> int:
    """Keep only values that can be a chip clock.

    1.51 files have command-stream bytes at 0xA8 and can read as anything;
    anything outside the sane range falls back to zero (chip default applies).
    """
    raw &= 0x3FFFFFFF
    return raw if CLOCK_MIN <= raw <= CLOCK_MAX else 0


def _clock_or_zero(buf: bytes, off: int) -> int:
    return _sane_clock(_header_clock(buf, off))


def _c140_effective_clock(raw: int) -> int:
    """Resolve a C140 header clock the way the players do.

    21390 is the legacy 8MHz/374 rate the System 21 rips carry (even in 1.51
    files, which predate the field); players map it to the 12.288 MHz default,
    which is the clock all of these conversions are built around. Other
    sub-1MHz values are sample-rate-style legacy logs: players multiply them
    by 576 to recover the clock, and so do we. A real clock is used as-is;
    anything else falls back to the default. See docs/segapcm-c140.md.
    """
    raw &= 0x3FFFFFFF
    if raw == 21390:
        return C140_DEFAULT_CLOCK
    if CLOCK_MIN <= raw < 1_000_000:
        raw *= 576
    if CLOCK_MIN <= raw <= CLOCK_MAX:
        return raw
    return C140_DEFAULT_CLOCK


def c352_effective_clock(raw: int, divider: int) -> int:
    """Crystal a /288 C352 core should run at.

    The header clock is the input crystal (typical 24192000). The 0xD6 byte is
    the hardware divider divided by 4 (typical 72, meaning 288). libvgm feeds
    the core ``header_clock * 72 / byte``, which equals the crystal when the
    byte is 72. A missing divider with a crystal-sized clock keeps the crystal.
    A sub-megahertz clock is the legacy pre-scaled form and is multiplied by 72
    (336000 with divider 1 is 24192000). Bit 31 is stripped by the caller.
    """
    raw &= 0x3FFFFFFF
    if raw == 0:
        return 0
    if divider:
        scaled = raw * 72 // divider
    elif raw >= 1_000_000:
        scaled = raw
    else:
        scaled = raw * 72
    return scaled if CLOCK_MIN <= scaled <= CLOCK_MAX else 0


def _parse_clock_list(buf: bytes, pos: int) -> dict[int, int]:
    """Clock list: count byte, then (u8 chip, u32 clock) entries."""
    out: dict[int, int] = {}
    if pos >= len(buf):
        return out
    count = buf[pos]
    if not 0 < count <= 64:
        return out
    pos += 1
    for _ in range(count):
        if pos + 5 > len(buf):
            return {}
        chip = buf[pos]
        clock = int.from_bytes(buf[pos + 1:pos + 5], "little") & 0x3FFFFFFF
        if chip > 0x7F or not 1000 <= clock <= 80_000_000:
            return {}
        out[chip] = clock
        pos += 5
    return out


def _parse_volume_list(buf: bytes, pos: int) -> dict[int, tuple[int, int]]:
    """Volume list: count byte, then (u8 chip, u8 flags, u16 value) entries."""
    out: dict[int, tuple[int, int]] = {}
    if pos >= len(buf):
        return out
    count = buf[pos]
    if not 0 < count <= 64:
        return out
    pos += 1
    for _ in range(count):
        if pos + 4 > len(buf):
            return {}
        chip, flags = buf[pos], buf[pos + 1]
        value = int.from_bytes(buf[pos + 2:pos + 4], "little")
        if chip > 0x7F or value > 0x400:
            return {}
        out[chip] = (flags, value)
        pos += 4
    return out


def _extra_chip_lists(buf: bytes) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """VGM extra header (1.70+): the clock and volume lists.

    Both lists are located by an offset field next to the 4-byte size; which
    field holds which list varies between writers. Each candidate is parsed
    with validation and an unreadable list is reported as empty rather than as
    garbage. Values below 100 kHz are never accepted as clocks (the volume
    list is easy to mistake for one).
    """
    clocks: dict[int, int] = {}
    volumes: dict[int, tuple[int, int]] = {}
    if len(buf) < 0xC0:
        return clocks, volumes
    rel = int.from_bytes(buf[0xBC:0xC0], "little")
    if not rel:
        return clocks, volumes
    extra = 0xBC + rel
    if extra + 8 > len(buf):
        return clocks, volumes
    size = int.from_bytes(buf[extra:extra + 4], "little")
    if size < 8:
        return clocks, volumes
    off1 = int.from_bytes(buf[extra + 4:extra + 8], "little")
    off2 = int.from_bytes(buf[extra + 8:extra + 12], "little") if size >= 12 else 0
    for base, rel_off in ((extra + 8, off2), (extra + 4, off1), (extra + 4, off2), (extra + 8, off1)):
        if not rel_off:
            continue
        if not volumes:
            volumes = _parse_volume_list(buf, base + rel_off)
        if not clocks:
            clocks = _parse_clock_list(buf, base + rel_off)
    return clocks, volumes


def head_bytes(path: Path, want: int) -> bytes:
    """The first `want` bytes of a VGM/VGZ. .vgz is inflated as far as needed.

    The pack browser and the chip census read headers this way, without
    paying for a command stream they do not look at.
    """
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw[: want * 4], want)
        except zlib.error:
            return b""
    return raw[:want]


def chips_in_head(buf: bytes) -> set[str]:
    """Chip kinds visible in a VGM header, no command stream read.

    Names match the converter's chips: ym2151, ym2610, msm6295, segapcm, c140,
    c352, k053260, ay8910, k051649 (plus "scc_plus" for the K052539 variant),
    k007232, ymz280b and vsu (plus "vsu_dual").
    C219 is the C140-family part the converter does not support. It is reported
    apart from c140, and those packs should not be batch-converted. C352 is
    its own chip (32 voices); it is not folded into c140 or c219.
    """
    if buf[:4] != b"Vgm ":
        return set()
    kinds: set[str] = set()
    version = _header_raw(buf, 8)
    ym2151_head = _header_raw(buf, 0x30)
    if ym2151_head & 0x3FFFFFFF:
        kinds.add("ym2151")
        if ym2151_head & 0x40000000:
            kinds.add("ym2151_dual")
    if _field_in_header(buf, YM2612_CLOCK_OFF, 4):
        ym2612_raw = _header_raw(buf, YM2612_CLOCK_OFF)
        if CLOCK_MIN <= (ym2612_raw & 0x3FFFFFFF) <= CLOCK_MAX:
            kinds.add("ym2612")
            if ym2612_raw & YM2612_DUAL_BIT:
                kinds.add("ym2612_dual")
    sn_head = _header_raw(buf, SN76489_CLOCK_OFF)
    if 100_000 <= (sn_head & 0x3FFFFFFF) <= 40_000_000:
        if (sn_head & SN76489_DUAL_BIT) and (sn_head & SN76489_T6W28_BIT):
            kinds.add("t6w28")
        else:
            kinds.add("sn76489")
            if sn_head & SN76489_DUAL_BIT:
                kinds.add("sn76489_dual")
    if version >= 0x131 and _field_in_header(buf, YM2203_CLOCK_OFF, 4):
        ym2203_raw = _header_raw(buf, YM2203_CLOCK_OFF)
        if 100_000 <= (ym2203_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ym2203")
            if ym2203_raw & YM2203_DUAL_BIT:
                kinds.add("ym2203_dual")
    if version >= 0x151 and _field_in_header(buf, YM2610_CLOCK_OFF, 4):
        ym2610_raw = _header_raw(buf, YM2610_CLOCK_OFF)
        if 100_000 <= (ym2610_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ym2610")
            if ym2610_raw & YM2610_B_BIT:
                kinds.add("ym2610_b")
            if ym2610_raw & YM2610_DUAL_BIT:
                kinds.add("ym2610_dual")
    # YM2608 (header 0x48, VGM 1.51+). Detected apart from its SSG part; the
    # AY of an OPNA is intrinsic, not a second entry in the chip set.
    if version >= 0x151 and _field_in_header(buf, YM2608_CLOCK_OFF, 4):
        ym2608_raw = _header_raw(buf, YM2608_CLOCK_OFF)
        if 100_000 <= (ym2608_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ym2608")
            if ym2608_raw & YM2608_DUAL_BIT:
                kinds.add("ym2608_dual")
    # YM2413 (header 0x10). Under VGM 1.50 this dword meant YM2612; only
    # report it as OPLL when a 0x51/$A1 stream actually exists, which
    # chips_in_head callers cross-check against the parsed writes.
    if version >= 0x150 and _field_in_header(buf, YM2413_CLOCK_OFF, 4):
        if 100_000 <= _header_clock(buf, YM2413_CLOCK_OFF) <= 40_000_000:
            kinds.add("vrc7" if (_header_raw(buf, YM2413_CLOCK_OFF) & YM2413_VRC7_BIT) else "opll")
    # Game Boy DMG (header 0x80, 1.61+).  The extra-header clock list is not in
    # this prefix; the census then relies on the parsed writes instead.
    if version >= 0x161 and _field_in_header(buf, GB_DMG_CLOCK_OFF, 4):
        if 100_000 <= _header_clock(buf, GB_DMG_CLOCK_OFF) <= 40_000_000:
            kinds.add("gb")
    # NES APU (header 0x80, 1.51+). The extra-header clock list is not in this
    # prefix, so a file that carries it only there still converts; the census
    # just will not list it.
    if version >= 0x151 and _field_in_header(buf, NES_CLOCK_OFF, 4):
        if 100_000 <= _header_clock(buf, NES_CLOCK_OFF) <= 40_000_000:
            kinds.add("nes")
    if version >= 0x151 and _field_in_header(buf, SEGAPCM_CLOCK_OFF, 4):
        raw = _header_raw(buf, SEGAPCM_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("segapcm")
            if raw & 0x40000000:
                kinds.add("segapcm_dual")
    if version >= 0x151 and _field_in_header(buf, RF5C68_CLOCK_OFF, 4):
        raw = _header_raw(buf, RF5C68_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("rf5c68")
            if raw & 0x40000000:
                kinds.add("rf5c68_dual")
    if version >= 0x151 and _field_in_header(buf, RF5C164_CLOCK_OFF, 4):
        raw = _header_raw(buf, RF5C164_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("rf5c164")
            if raw & 0x40000000:
                kinds.add("rf5c164_dual")
    if version >= 0x151 and 100_000 <= _header_clock(buf, Y8950_CLOCK_OFF) <= 40_000_000:
        kinds.add("y8950")
    if version >= 0x151 and 100_000 <= _header_clock(buf, YMF278B_CLOCK_OFF) <= 40_000_000:
        kinds.add("ymf278b")
    if version >= 0x151 and _field_in_header(buf, YMZ280B_CLOCK_OFF, 4):
        ymz_raw = _header_raw(buf, YMZ280B_CLOCK_OFF)
        if 100_000 <= (ymz_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ymz280b")
            if ymz_raw & YMZ280B_DUAL_BIT:
                kinds.add("ymz280b_dual")
    if version >= 0x171 and _field_in_header(buf, VSU_CLOCK_OFF, 4):
        vsu_raw = _header_raw(buf, VSU_CLOCK_OFF)
        if 100_000 <= (vsu_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("vsu")
            if vsu_raw & VSU_HEADER_DUAL_BIT:
                kinds.add("vsu_dual")
    if version >= 0x151 and _field_in_header(buf, AY8910_CLOCK_OFF, 4):
        raw = _header_raw(buf, AY8910_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ay8910")
            if raw & 0x40000000:
                kinds.add("ay8910_dual")
    # The OPL family joined the header in 1.51. A YM3812 field with bit 30 set
    # carries a second chip on command 0xAA. Bit 31 is the Sound Blaster Pro
    # hard-pan of that pair, and it is reported apart from the chip itself.
    if version >= 0x151 and _field_in_header(buf, OPL2_CLOCK_OFF, 4):
        opl2_raw = _header_raw(buf, OPL2_CLOCK_OFF)
        if 100_000 <= (opl2_raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("opl2")
            if opl2_raw & OPL_DUAL_BIT:
                kinds.add("opl2_dual")
    if version >= 0x151 and _field_in_header(buf, OPL_CLOCK_OFF, 4):
        raw = _header_raw(buf, OPL_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("opl")
            if raw & OPL_DUAL_BIT:
                kinds.add("opl_dual")
    if version >= 0x151 and _field_in_header(buf, OPL3_CLOCK_OFF, 4):
        raw = _header_raw(buf, OPL3_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("opl3")
            if raw & OPL_DUAL_BIT:
                kinds.add("opl3_dual")
    if version >= 0x151 and _field_in_header(buf, Y8950_CLOCK_OFF, 4):
        raw = _header_raw(buf, Y8950_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("y8950")
            if raw & 0x40000000:
                kinds.add("y8950_dual")
    if version >= 0x151 and _field_in_header(buf, YMF278B_CLOCK_OFF, 4):
        raw = _header_raw(buf, YMF278B_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("ymf278b")
            if raw & 0x40000000:
                kinds.add("ymf278b_dual")
    k051 = _header_raw(buf, K051649_CLOCK_OFF)
    if version >= 0x161 and _field_in_header(buf, K051649_CLOCK_OFF, 4):
        if 100_000 <= (k051 & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("k051649")
            if k051 & 0x80000000:
                kinds.add("scc_plus")
            if k051 & 0x40000000:
                kinds.add("k051649_dual")
    ctype = buf[C140_TYPE_OFF] if version >= 0x161 and len(buf) > C140_TYPE_OFF else 0
    c140_raw = _header_raw(buf, C140_CLOCK_OFF)
    c140 = c140_raw & 0x3FFFFFFF
    if c140 == 21390 or 100_000 <= c140 <= 40_000_000:
        kinds.add("c140")
        if c140_raw & 0x40000000:
            kinds.add("c140_dual")
    if ctype == 2:
        kinds.discard("c140")
        kinds.add("c219")
    if version >= 0x161 and _field_in_header(buf, K053260_CLOCK_OFF, 4):
        raw = _header_raw(buf, K053260_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("k053260")
            if raw & 0x40000000:
                kinds.add("k053260_dual")
    if version >= 0x161 and _field_in_header(buf, MSM6295_CLOCK_OFF, 4):
        raw = _header_raw(buf, MSM6295_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("msm6295")
            if raw & 0x40000000:
                kinds.add("msm6295_dual")
    if version >= 0x161 and _field_in_header(buf, MSM6258_CLOCK_OFF, 4):
        raw = _header_raw(buf, MSM6258_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("msm6258")
            if raw & 0x40000000:
                kinds.add("msm6258_dual")
    if version >= 0x171 and _field_in_header(buf, K007232_EXTRA_CLOCK_OFF, 4):
        raw = _header_raw(buf, K007232_EXTRA_CLOCK_OFF)
        if 100_000 <= (raw & 0x3FFFFFFF) <= 40_000_000:
            kinds.add("k007232")
            if raw & 0x40000000:
                kinds.add("k007232_dual")
    if len(buf) >= 0xC0:
        clocks, _volumes = _extra_chip_lists(buf)
        if clocks.get(EXTRA_YM2612, 0):
            kinds.add("ym2612")
        if clocks.get(EXTRA_YM2203, 0):
            kinds.add("ym2203")
        if clocks.get(EXTRA_YM2610, 0):
            kinds.add("ym2610")
        if clocks.get(EXTRA_SEGAPCM, 0):
            kinds.add("segapcm")
        if clocks.get(EXTRA_AY8910, 0):
            kinds.add("ay8910")
        if clocks.get(EXTRA_K051649, 0):
            kinds.add("k051649")
        if clocks.get(EXTRA_C140, 0):
            kinds.add("c140")
        if clocks.get(EXTRA_K053260, 0):
            kinds.add("k053260")
        if clocks.get(EXTRA_MSM6295, 0):
            kinds.add("msm6295")
        if clocks.get(EXTRA_MSM6258, 0):
            kinds.add("msm6258")
        if clocks.get(EXTRA_K007232, 0):
            kinds.add("k007232")
        if clocks.get(EXTRA_C352, 0):
            kinds.add("c352")
        if clocks.get(EXTRA_YMZ280B, 0):
            kinds.add("ymz280b")
    if version >= 0x171 and _field_in_header(buf, C352_CLOCK_OFF, 4):
        raw = _header_raw(buf, C352_CLOCK_OFF) & 0x3FFFFFFF
        if 1000 <= raw <= 50_000_000:
            kinds.add("c352")
    return kinds


def _k007232_clock(buf: bytes, extra: dict[int, int], ym_clock: int, version: int) -> int:
    """Primary K007232 clock: header 0xE8 (VGM 1.71+), never extra-header volume."""
    clk = _header_clock(buf, K007232_EXTRA_CLOCK_OFF) if version >= 0x171 else 0
    if clk >= 100_000:
        return clk
    extra_clk = extra.get(EXTRA_K007232, 0) & 0x3FFFFFFF
    if extra_clk >= 100_000:
        return extra_clk
    return ym_clock or 3579545


def load_vgm(path: Path | str) -> VgmFile:
    path = Path(path)
    buf = read_vgm_bytes(path)
    if buf[:4] != b"Vgm ":
        raise ValueError(f"{path}: not a VGM")
    ver = int.from_bytes(buf[8:12], "little")
    total = int.from_bytes(buf[0x18:0x1C], "little")
    loop_rel = int.from_bytes(buf[0x1C:0x20], "little")
    loop_samples = int.from_bytes(buf[0x20:0x24], "little")
    loop_pos = (0x1C + loop_rel) if loop_rel else None
    extra = _extra_chip_lists(buf)
    clocks, volumes = extra
    ym2151_raw = _header_raw(buf, 0x30)
    ym2151_dual = bool(ym2151_raw & 0x40000000)
    ym_clock = (ym2151_raw & 0x3FFFFFFF) or 3579545
    # YM2612: the clock field also carries the dual-instance flag in bit 30.
    ym2612_raw = _header_raw(buf, YM2612_CLOCK_OFF)
    ym2612_dual = bool(ym2612_raw & YM2612_DUAL_BIT)
    ym2612_clock = _sane_clock(ym2612_raw)
    if not ym2612_clock:
        ym2612_clock = _sane_clock(clocks.get(EXTRA_YM2612, 0))
    # YM2203: the clock field also carries the dual-instance flag in bit 30.
    ym2203_raw = _header_raw(buf, YM2203_CLOCK_OFF)
    ym2203_dual = bool(ym2203_raw & YM2203_DUAL_BIT)
    ym2203_clock = _sane_clock(ym2203_raw)
    if not ym2203_clock:
        ym2203_clock = _sane_clock(clocks.get(EXTRA_YM2203, 0))
    # YM2610: bit 31 selects the B variant, bit 30 the second instance.
    ym2610_raw = _header_raw(buf, YM2610_CLOCK_OFF)
    ym2610_b = bool(ym2610_raw & YM2610_B_BIT)
    ym2610_dual = bool(ym2610_raw & YM2610_DUAL_BIT)
    ym2610_clock = _sane_clock(ym2610_raw)
    if not ym2610_clock:
        ym2610_clock = _sane_clock(clocks.get(EXTRA_YM2610, 0))
    # YM2608: bit 30 of clock 0x48 marks the second chip ($A6/$A7). The
    # rhythm DAC and delta-T share the single clock with both FM buses.
    ym2608_raw = _header_raw(buf, YM2608_CLOCK_OFF) if ver >= 0x151 else 0
    ym2608_dual = bool(ym2608_raw & YM2608_DUAL_BIT)
    ym2608_clock = _sane_clock(ym2608_raw)
    # YM2413: the header's first chip clock, 0x10.  Bit 31 asks for a Konami
    # VRC7 (six channels, its own preset ROM) on the same OPLL register stream.
    ym2413_raw = _header_raw(buf, YM2413_CLOCK_OFF) if ver >= 0x150 else 0
    ym2413_dual = bool(ym2413_raw & YM2608_DUAL_BIT)
    ym2413_vrc7 = bool(ym2413_raw & YM2413_VRC7_BIT)
    ym2413_clock = _sane_clock(ym2413_raw)
    # Game Boy DMG clock 0x80 (1.61+), falling back to extra-header id 0x13.
    # Bit 30 marks a second APU (Dual Chip Support #2).
    gb_raw = _header_raw(buf, GB_DMG_CLOCK_OFF) if ver >= 0x161 else 0
    gb_dmg_clock = _sane_clock(gb_raw)
    if not gb_dmg_clock:
        gb_dmg_clock = _sane_clock(clocks.get(EXTRA_GB, 0))
    gb_dmg_dual = bool(gb_raw & 0x40000000)
    # NES APU clock 0x84 (1.51+), falling back to extra-header id 0x14.
    nes_raw = _header_raw(buf, NES_CLOCK_OFF) if ver >= 0x151 else 0
    nes_clock = _sane_clock(nes_raw)
    if not nes_clock:
        nes_clock = _sane_clock(clocks.get(EXTRA_NES, 0))
    nes_dual = bool(nes_raw & NES_DUAL_BIT)
    nes_fds = bool(nes_raw & NES_FDS_BIT)
    # HuC6280 (PC Engine) clock 0xA4 (1.61+).  The field carries the PSG clock
    # directly.  The extra-header id is libvgm's device index, 0x1B.
    huc_raw = _header_raw(buf, HUC6280_CLOCK_OFF) if ver >= 0x161 else 0
    huc6280_clock = _sane_clock(huc_raw)
    if not huc6280_clock:
        huc6280_clock = _sane_clock(clocks.get(EXTRA_HUC6280, 0))
    huc6280_dual = bool(huc_raw & 0x40000000)
    # OPL family. The three chips share the header's 1.51 block and each of
    # their clock fields carries a second-instance bit; only the YM3812 has a
    # command for the pair (0xAA), and bit 31 there pans them hard.
    opl2_raw = _header_raw(buf, OPL2_CLOCK_OFF) if ver >= 0x151 else 0
    opl2_clock = _sane_clock(opl2_raw)
    opl2_dual = bool(opl2_raw & OPL_DUAL_BIT)
    opl2_pan = bool(opl2_raw & 0x80000000)
    opl_raw = _header_raw(buf, OPL_CLOCK_OFF) if ver >= 0x151 else 0
    opl_clock = _sane_clock(opl_raw)
    opl_dual = bool(opl_raw & OPL_DUAL_BIT)
    opl3_raw = _header_raw(buf, OPL3_CLOCK_OFF) if ver >= 0x151 else 0
    opl3_clock = _sane_clock(opl3_raw)
    opl3_dual = bool(opl3_raw & OPL_DUAL_BIT)
    # SN76489/T6W28: bit 30 is the second instance, bit 31 T6W28 mode.
    sn76489_raw = _header_raw(buf, SN76489_CLOCK_OFF)
    sn76489_clock = _sane_clock(sn76489_raw)
    sn76489_dual = bool(sn76489_raw & SN76489_DUAL_BIT)
    sn76489_t6w28 = bool(
        sn76489_raw & SN76489_T6W28_BIT
        and sn76489_raw & SN76489_DUAL_BIT
    )
    sn76489_clock_2 = 0
    if sn76489_dual:
        sn76489_clock_2 = _sane_clock(clocks.get(EXTRA_SN76489, 0))
    if not sn76489_clock:
        sn76489_clock = _sane_clock(clocks.get(EXTRA_SN76489, 0))
    k_clock = _k007232_clock(buf, clocks, ym_clock, ver)
    oki_raw = _header_raw(buf, 0x98) if ver >= 0x161 else 0
    adpcm_raw = _header_raw(buf, 0x90) if ver >= 0x161 else 0
    adpcm_flags = buf[0x94] if ver >= 0x161 and _field_in_header(buf, 0x94, 1) else 0
    spcm_raw = _header_raw(buf, SEGAPCM_CLOCK_OFF) if ver >= 0x151 else 0
    spcm_clock = _sane_clock(spcm_raw)
    segapcm_dual = bool(spcm_raw & 0x40000000)
    if not spcm_clock:
        spcm_clock = _sane_clock(clocks.get(EXTRA_SEGAPCM, 0))
    spcm_shift = buf[SEGAPCM_INTF_OFF] if ver >= 0x151 and _field_in_header(buf, SEGAPCM_INTF_OFF, 1) else 0
    spcm_mask = buf[SEGAPCM_MASK_OFF] if ver >= 0x151 and _field_in_header(buf, SEGAPCM_MASK_OFF, 1) else 0
    # RF5C68 and RF5C164 share the register set but ride different header
    # clocks. The extra-header list (chip id 0x05 = RF5C68) is the fallback.
    rf5c68_raw = _header_raw(buf, RF5C68_CLOCK_OFF) if ver >= 0x151 else 0
    rf5c68_clock = _sane_clock(rf5c68_raw)
    rf5c68_dual = bool(rf5c68_raw & 0x40000000)
    if not rf5c68_clock:
        rf5c68_clock = _sane_clock(clocks.get(0x05, 0))
    rf5c164_raw = _header_raw(buf, RF5C164_CLOCK_OFF) if ver >= 0x151 else 0
    rf5c164_clock = _sane_clock(rf5c164_raw)
    rf5c164_dual = bool(rf5c164_raw & 0x40000000)
    y8950_raw = _header_raw(buf, Y8950_CLOCK_OFF) if ver >= 0x151 else 0
    y8950_clock = _sane_clock(y8950_raw)
    y8950_dual = bool(y8950_raw & 0x40000000)
    ymf278b_raw = _header_raw(buf, YMF278B_CLOCK_OFF) if ver >= 0x151 else 0
    ymf278b_clock = _sane_clock(ymf278b_raw)
    ymf278b_dual = bool(ymf278b_raw & 0x40000000)
    # YMZ280B clock 0x68 (1.51+), bit 30 selecting a second chip.  The
    # extra-header clock list supplies the second instance's clock when the
    # writer put the pair on different crystals.
    ymz_raw = _header_raw(buf, YMZ280B_CLOCK_OFF) if ver >= 0x151 else 0
    ymz280b_dual = bool(ymz_raw & YMZ280B_DUAL_BIT)
    ymz280b_clock = _sane_clock(ymz_raw)
    ymz280b_clock_2 = 0
    if ymz280b_dual:
        ymz280b_clock_2 = _sane_clock(clocks.get(EXTRA_YMZ280B, 0))
    if not ymz280b_clock:
        ymz280b_clock = _sane_clock(clocks.get(EXTRA_YMZ280B, 0))
    # Virtual Boy VSU clock 0xC4 (VGM 1.71+).  Furnace uses bit 30 for a
    # second chip; the second chip's writes set bit 15 of the register word.
    vsu_raw = _header_raw(buf, VSU_CLOCK_OFF) if ver >= 0x171 else 0
    vsu_dual = bool(vsu_raw & VSU_HEADER_DUAL_BIT)
    vsu_clock = _sane_clock(vsu_raw)
    vsu_clock_2 = 0
    if vsu_dual:
        vsu_clock_2 = _sane_clock(clocks.get(EXTRA_VSU, 0))
    if not vsu_clock:
        vsu_clock = _sane_clock(clocks.get(EXTRA_VSU, 0))
    # Read the field for every version: the legacy System 21 rips carry
    # 21390 there even in 1.51 files, and players resolve it the same way.
    c140_header = _header_raw(buf, C140_CLOCK_OFF)
    c140_dual = bool(c140_header & 0x40000000)
    c140_raw = c140_header & 0x3FFFFFFF
    if not c140_raw:
        c140_raw = clocks.get(EXTRA_C140, 0) & 0x3FFFFFFF
    c140_clock = _c140_effective_clock(c140_raw)
    k053260_raw = _header_raw(buf, K053260_CLOCK_OFF) if ver >= 0x161 else 0
    k053260_clock = _sane_clock(k053260_raw)
    k053260_dual = bool(k053260_raw & 0x40000000)
    if not k053260_clock:
        k053260_clock = _sane_clock(clocks.get(EXTRA_K053260, 0))
    c140_type = 0
    if ver >= 0x161 and _field_in_header(buf, C140_TYPE_OFF, 1):
        c140_type = buf[C140_TYPE_OFF] & 0x03
    elif c140_raw == 21390:
        # Pre-1.61 System 21 rips log the legacy 8MHz/374 rate instead of a
        # clock; their register stream still needs the System 21 mapping.
        c140_type = 1
    c352_clock = 0
    c352_divider = 0
    c352_mute_rear = False
    if ver >= 0x171 and _field_in_header(buf, C352_CLOCK_OFF, 4):
        c352_raw = _header_raw(buf, C352_CLOCK_OFF)
        c352_mute_rear = bool(c352_raw & 0x80000000)
        if _field_in_header(buf, C352_DIV_OFF, 1):
            c352_divider = buf[C352_DIV_OFF]
        c352_clock = c352_effective_clock(c352_raw, c352_divider)
    if not c352_clock:
        # Extra-header id 0x27 is already a crystal. Applying the divider
        # byte again would rescale a clock that was stored with divider 1.
        c352_clock = _sane_clock(clocks.get(EXTRA_C352, 0))
    ay_raw = _header_raw(buf, AY8910_CLOCK_OFF) if ver >= 0x151 else 0
    ay_clock = _sane_clock(ay_raw)
    ay8910_dual = bool(ay_raw & 0x40000000)
    if not ay_clock:
        ay_clock = _sane_clock(clocks.get(EXTRA_AY8910, 0))
    ay_type = (
        buf[AY8910_TYPE_OFF]
        if ver >= 0x151 and _field_in_header(buf, AY8910_TYPE_OFF, 1) else 0
    )
    ay_flags = (
        buf[AY8910_FLAGS_OFF]
        if ver >= 0x151 and _field_in_header(buf, AY8910_FLAGS_OFF, 1) else 0
    )
    k051_clock = 0
    k051_plus = False
    k051649_dual = False
    if ver >= 0x161 and _field_in_header(buf, K051649_CLOCK_OFF, 4):
        raw = _header_raw(buf, K051649_CLOCK_OFF)
        k051_plus = bool(raw & 0x80000000)
        k051649_dual = bool(raw & 0x40000000)
        k051_clock = _sane_clock(raw & 0x3FFFFFFF)
    if not k051_clock:
        k051_clock = _sane_clock(clocks.get(EXTRA_K051649, 0))
    k007232_header = _header_raw(buf, K007232_EXTRA_CLOCK_OFF) if ver >= 0x171 else 0
    k007232_dual = bool(k007232_header & 0x40000000)

    main_volume = 0.0
    # Bytes past the declared command offset are not header fields. Synthetic
    # test logs and old 0x40-byte headers leave this area as commands.
    if ver >= 0x150 and data_offset(buf) >= 0x7E and len(buf) >= 0x7E:
        raw_volume = int.from_bytes(buf[0x7C:0x7E], "little")
        if 0 < raw_volume <= 0x800:
            main_volume = raw_volume / 0x100
    vgm = VgmFile(
        path=path,
        buf=buf,
        version=ver,
        total_samples=total,
        loop_samples=loop_samples,
        loop_file_pos=loop_pos,
        loop_sample=None,
        ym2151_clock=ym_clock,
        k007232_clock=k_clock,
        gd3=parse_gd3(buf),
        main_volume=main_volume,
        ym2151_dual=ym2151_dual,
        ym2612_clock=ym2612_clock,
        ym2612_dual=ym2612_dual,
        sn76489_clock=sn76489_clock,
        sn76489_clock_2=sn76489_clock_2,
        sn76489_dual=sn76489_dual,
        sn76489_t6w28=sn76489_t6w28,
        ym2203_clock=ym2203_clock,
        ym2203_dual=ym2203_dual,
        ym2610_clock=ym2610_clock,
        ym2610_dual=ym2610_dual,
        ym2610_b=ym2610_b,
        ym2608_clock=ym2608_clock,
        ym2608_dual=ym2608_dual,
        ym2413_clock=ym2413_clock,
        ym2413_dual=ym2413_dual,
        ym2413_vrc7=ym2413_vrc7,
        nes_clock=nes_clock,
        nes_dual=nes_dual,
        nes_fds=nes_fds,
        gb_dmg_clock=gb_dmg_clock,
        gb_dmg_dual=gb_dmg_dual,
        huc6280_clock=huc6280_clock,
        huc6280_dual=huc6280_dual,
        opl_clock=opl_clock,
        opl_dual=opl_dual,
        opl2_clock=opl2_clock,
        opl2_dual=opl2_dual,
        opl2_pan=opl2_pan,
        opl3_clock=opl3_clock,
        opl3_dual=opl3_dual,
        msm6295_clock=oki_raw & 0x3FFFFFFF,
        msm6295_pin7=bool(oki_raw & 0x80000000),
        msm6295_dual=bool(oki_raw & 0x40000000),
        msm6258_clock=adpcm_raw & 0x3FFFFFFF,
        msm6258_flags=adpcm_flags,
        msm6258_dual=bool(adpcm_raw & 0x40000000),
        segapcm_clock=spcm_clock,
        segapcm_bankshift=spcm_shift,
        segapcm_bankmask=spcm_mask,
        segapcm_dual=segapcm_dual,
        rf5c68_clock=rf5c68_clock,
        rf5c164_clock=rf5c164_clock,
        rf5c68_dual=rf5c68_dual,
        rf5c164_dual=rf5c164_dual,
        y8950_clock=y8950_clock,
        y8950_dual=y8950_dual,
        ymf278b_clock=ymf278b_clock,
        ymf278b_dual=ymf278b_dual,
        ymz280b_clock=ymz280b_clock,
        ymz280b_clock_2=ymz280b_clock_2,
        ymz280b_dual=ymz280b_dual,
        vsu_clock=vsu_clock,
        vsu_clock_2=vsu_clock_2,
        vsu_dual=vsu_dual,
        c140_clock=c140_clock,
        c140_type=c140_type,
        c140_dual=c140_dual,
        k053260_clock=k053260_clock,
        k053260_dual=k053260_dual,
        c352_clock=c352_clock,
        c352_divider=c352_divider,
        c352_mute_rear=c352_mute_rear,
        ay8910_clock=ay_clock,
        ay8910_type=ay_type,
        ay8910_flags=ay_flags,
        ay8910_dual=ay8910_dual,
        k051649_clock=k051_clock,
        k051649_plus=k051_plus,
        k051649_dual=k051649_dual,
        k007232_dual=k007232_dual,
        extra_clocks=clocks,
        extra_volumes=volumes,
    )

    pos = data_offset(buf)
    n = len(buf)
    sample = 0
    loop_sample: int | None = None
    skipped_sample_cmds: set[int] = set()
    stray_ym2203_second = False
    stray_ym2612_second = False
    stray_ym2610_second = False
    stray_ym2608_second = False
    stray_opll_second = False
    stray_ym2151_second = False
    stray_opl_second = False
    stray_opl2_second = False
    stray_opl3_second = False
    stray_y8950_second = False
    stray_opl4_second = False
    stray_sn_second = False
    stray_rf5c_second = False
    stray_segapcm_second = False
    stray_k007232_second = False
    stray_c140_second = False
    stray_k053260_second = False
    stray_ay_second = False
    stray_scc_second = False
    stray_ymz_second = False
    stray_vsu_second = False
    stray_msm6258_second = False
    stray_msm6295_second = False
    saw_end = False
    saw_adpcm_compressed = False
    # stream id -> setup recorded by 0x90/0x91/0x92
    streams: dict[int, dict] = {}
    active_adpcm: dict[int, Msm6258Play] = {}
    # Generic 0x93 plays that a 0x94 (or the next 0x93) ends. The stream's
    # declared length is usually the same span, but a driver may stop early.
    active_stream: dict[int, DacStreamPlay] = {}
    # The RF5C pair bank their sample RAM: register 0x07 (bit 6 clear) selects
    # a 4 KB write bank, and 0xC1/0xC2 then address a 16-bit offset inside it.
    # The bank is folded into the memory-write address here so the collector
    # sees one flat 64 KB window, exactly like Furnace's core.
    rf5c_wbank = {"rf5c68": 0, "rf5c164": 0}

    def close_adpcm(sid: int, end_t: int) -> None:
        play = active_adpcm.pop(sid, None)
        if play is not None and play.end == 0 and end_t > play.sample:
            play.end = end_t

    while pos < n:
        if loop_pos is not None and loop_sample is None and pos >= loop_pos:
            loop_sample = sample
        cmd = buf[pos]
        if cmd == CMD_END:
            saw_end = True
            break
        if cmd == CMD_DATA_BLOCK:
            if pos + 7 > n:
                break
            typ = buf[pos + 2]
            size = int.from_bytes(buf[pos + 3:pos + 7], "little")
            chip_id = (size >> 31) & 1
            size &= 0x7FFFFFFF
            payload = buf[pos + 7:pos + 7 + size]
            if typ == 0x00:
                # YM2612 PCM sample bank. The payload is unsigned 8-bit PCM;
                # the old stream reads it through 0xE0/0x80-0x8F, the modern
                # one through 0x95 block ids.
                vgm.dac_blocks.append(payload)
                vgm.dac_data.extend(payload)
            elif typ == NES_DPCM_BANK_TYPE:
                # NES DPCM database. 0x68 commands copy windows of this flat
                # bank into the $C000/$D000 sample RAM as the driver switches
                # banks, so keep the concatenation in file order.
                vgm.nes_dpcm_bank.extend(payload)
            elif typ == 0x04:
                # OKIM6258 ADPCM bank. 0x95 block ids index these in order.
                vgm.adpcm_blocks.append(bytes(payload))
            elif typ == HUC6280_PCM_TYPE:
                # HuC6280 PCM database.  A DAC stream (bank 5) plays 8-bit
                # DDA levels out of the concatenation; 0x95 block ids index
                # the individual blocks.
                vgm.huc6280_pcm.extend(payload)
                vgm.huc6280_pcm_blocks.append(bytes(payload))
            elif typ == 0x44:
                saw_adpcm_compressed = True
            elif typ in (RF5C68_RAM_TYPE, RF5C164_RAM_TYPE, NES_DPCM_RAM_TYPE):
                # C0-DF RAM writes: a 16-bit start offset then the bytes. The
                # two RF5C chips share one register file but each has its own
                # sample RAM, so the kind keeps them apart for the collector.
                # 0xC2 is the NES APU's DPCM RAM (16 KB at $C000).
                if len(payload) >= 2:
                    start = int.from_bytes(payload[0:2], "little")
                    kind = (
                        "rf5c68" if typ == RF5C68_RAM_TYPE else
                        "rf5c164" if typ == RF5C164_RAM_TYPE else "nes_dpcm"
                    )
                    vgm.roms.append(RomSlice(
                        chip_id, 0, start, payload[2:], kind, time=sample,
                    ))
            else:
                kind = {
                    K007232_ROM_TYPE: "k007232",
                    OKI_ROM_TYPE: "msm6295",
                    SEGAPCM_ROM_TYPE: "segapcm",
                    C140_ROM_TYPE: "c140",
                    C352_ROM_TYPE: "c352",
                    K053260_ROM_TYPE: "k053260",
                    YM2610_ADPCM_ROM_TYPE: "ym2610_adpcm",
                    YM2610_DELTAT_ROM_TYPE: "ym2610_deltat",
                    YM2608_DELTAT_ROM_TYPE: "ym2608_deltat",
                    Y8950_DELTAT_ROM_TYPE: "y8950_deltat",
                    YMF278B_ROM_TYPE: "ymf278b_rom",
                    YMF278B_RAM_TYPE: "ymf278b_ram",
                    YMZ280B_ROM_TYPE: "ymz280b",
                }.get(typ) if len(payload) >= 8 else None
                if kind:
                    rom_size = int.from_bytes(payload[0:4], "little")
                    start = int.from_bytes(payload[4:8], "little")
                    vgm.roms.append(RomSlice(chip_id, rom_size, start, payload[8:], kind))
            pos += 7 + size
            continue
        if cmd == 0x61 and pos + 3 <= n:
            sample += int.from_bytes(buf[pos + 1:pos + 3], "little")
            pos += 3
            continue
        if cmd == 0x62:
            sample += 735
            pos += 1
            continue
        if cmd == 0x63:
            sample += 882
            pos += 1
            continue
        if 0x70 <= cmd <= 0x7F:
            sample += (cmd & 0x0F) + 1
            pos += 1
            continue
        if 0x80 <= cmd <= 0x8F:
            # VGM 1.50 YM2612 PCM stream: play one byte from the data block
            # pointer, then wait 0-15 samples.  The timing is the same as a
            # generic wait, but it also advances the DAC read cursor.
            wait = cmd & 0x0F
            vgm.dac_events.append((sample, 1, wait))
            sample += wait
            pos += 1
            continue
        if cmd == 0xE0 and pos + 5 <= n:
            ptr = int.from_bytes(buf[pos + 1:pos + 5], "little")
            vgm.dac_events.append((sample, 0, ptr))
            pos += 5
            continue
        if cmd == 0x92 and pos + 6 <= n:
            stream = buf[pos + 1]
            rate = int.from_bytes(buf[pos + 2:pos + 6], "little")
            vgm.dac_stream_rates[stream] = rate
            st = streams.get(stream)
            if st is not None:
                st["rate"] = rate
            pos += 6
            continue
        if cmd == 0x95 and pos + 5 <= n:
            stream = buf[pos + 1]
            block = int.from_bytes(buf[pos + 2:pos + 4], "little")
            flags = buf[pos + 4]
            st = streams.get(stream)
            # A stream that was never set up stays on the YM2612 DAC path.
            # Logs that emit 0x95 without 0x90 depend on that.
            if st is not None and st["type"] == STREAM_OKIM6258:
                close_adpcm(stream, sample)
                play = Msm6258Play(
                    sample=sample,
                    chip_id=st["chip_id"],
                    block=block,
                    rate=st["rate"] or vgm.dac_stream_rates.get(stream, 0),
                    loop=bool(flags & 0x01),
                    reverse=bool(flags & 0x10),
                    step=st["step"],
                    step_base=st["base"],
                    length_mode=3,
                )
                vgm.msm6258_plays.append(play)
                active_adpcm[stream] = play
            elif st is not None and st["type"] == STREAM_C6280:
                # 0x95 on a HuC6280 stream plays one whole type-0x05 block.
                prev = active_stream.pop(stream, None)
                if prev is not None:
                    prev.stop_sample = sample
                play = DacStreamPlay(
                    sample=sample,
                    chip_type=st["type"],
                    chip_id=st["chip_id"],
                    stream=stream,
                    port=st["port"],
                    reg=st["reg"],
                    offset=0,
                    length=0,
                    mode=3,
                    rate=st["rate"] or vgm.dac_stream_rates.get(stream, 0),
                    loop=bool(flags & 0x80),
                    reverse=bool(flags & 0x10),
                    step=st["step"],
                    step_base=st["base"],
                    bank=st["bank"],
                    block=block,
                )
                vgm.dac_stream_plays.append(play)
                active_stream[stream] = play
            else:
                rate = vgm.dac_stream_rates.get(stream, VGM_RATE)
                vgm.dac_block_plays.append((sample, block, rate))
            pos += 5
            continue
        if cmd == 0x90 and pos + 5 <= n:
            # ss tt pp cc. Bit 7 of tt selects the second chip.
            sid = buf[pos + 1]
            tt = buf[pos + 2]
            streams[sid] = {
                "type": tt & 0x7F,
                "chip_id": (tt >> 7) & 1,
                "port": buf[pos + 3],
                "reg": buf[pos + 4],
                "bank": 0,
                "step": 1,
                "base": 0,
                "pos": 0,
                "rate": vgm.dac_stream_rates.get(sid, 0),
            }
            vgm.stream_setups += 1
            pos += 5
            continue
        if cmd == 0x91 and pos + 5 <= n:
            # ss dd ll bb: bank id, step size, step base.
            sid = buf[pos + 1]
            st = streams.get(sid)
            if st is not None:
                st["bank"] = buf[pos + 2]
                st["step"] = buf[pos + 3] or 1
                st["base"] = buf[pos + 4]
            pos += 5
            continue
        if cmd == 0x93 and pos + 11 <= n:
            # ss offset mode length. Mode 0 only moves the cursor.
            sid = buf[pos + 1]
            offset = int.from_bytes(buf[pos + 2:pos + 6], "little")
            mode = buf[pos + 6]
            length = int.from_bytes(buf[pos + 7:pos + 11], "little")
            st = streams.get(sid)
            kind = mode & 0x07
            if st is not None and offset != 0xFFFFFFFF:
                st["pos"] = offset
            if st is not None and st["type"] == STREAM_OKIM6258 and kind != 0:
                close_adpcm(sid, sample)
                play = Msm6258Play(
                    sample=sample,
                    chip_id=st["chip_id"],
                    block=-1,
                    rate=st["rate"] or vgm.dac_stream_rates.get(sid, 0),
                    loop=bool(mode & 0x80),
                    reverse=bool(mode & 0x10),
                    offset=0 if offset == 0xFFFFFFFF else offset,
                    nbytes=length,
                    step=st["step"],
                    step_base=st["base"],
                    length_mode=kind,
                )
                vgm.msm6258_plays.append(play)
                active_adpcm[sid] = play
            elif st is not None and kind != 0:
                # Any other chip: the stream writes its data bank to the
                # register the 0x90 named, at the 0x92 rate.  The YM2612 DAC
                # is the only one this tool reconstructs, but the record is
                # chip-agnostic.
                # A new start on the same stream ends the previous play.
                prev = active_stream.pop(sid, None)
                if prev is not None:
                    prev.stop_sample = sample
                play = DacStreamPlay(
                    sample=sample,
                    chip_type=st["type"],
                    chip_id=st["chip_id"],
                    stream=sid,
                    port=st["port"],
                    reg=st["reg"],
                    offset=(
                        st.get("pos", 0) if offset == 0xFFFFFFFF else offset
                    ),
                    length=length,
                    mode=mode,
                    rate=st["rate"] or vgm.dac_stream_rates.get(sid, 0),
                    loop=bool(mode & 0x80),
                    reverse=bool(mode & 0x10),
                    step=st["step"],
                    step_base=st["base"],
                    bank=st["bank"],
                )
                vgm.dac_stream_plays.append(play)
                active_stream[sid] = play
            pos += 11
            continue
        if cmd == 0x94 and pos + 2 <= n:
            sid = buf[pos + 1]
            if sid == 0xFF:
                for key in list(active_adpcm):
                    close_adpcm(key, sample)
                for key in list(active_stream):
                    active_stream.pop(key).stop_sample = sample
            else:
                close_adpcm(sid, sample)
                play = active_stream.pop(sid, None)
                if play is not None:
                    play.stop_sample = sample
            pos += 2
            continue
        if cmd == CMD_DAC_WRITE and pos + 2 <= n:
            vgm.dac_events.append((sample, 2, buf[pos + 1]))
            pos += 2
            continue
        if cmd == CMD_DAC_ENABLE and pos + 2 <= n:
            # Some logs use the standalone DAC enable command instead of
            # writing YM2612 register 0x2B through 0x52.
            vgm.writes.append(ChipWrite(sample, "ym2612", 0, 0x2B, buf[pos + 1]))
            pos += 2
            continue
        if cmd in (CMD_GGSTEREO, CMD_GGSTEREO_2) and pos + 2 <= n:
            # Game Gear stereo: four left and four right enable bits.  Keep it
            # on the same chip stream with reg 1 so the collector sees it in
            # order with the tone/volume bytes.  0x3F is the second chip.
            value = buf[pos + 1]
            cid = 0 if cmd == CMD_GGSTEREO else 1
            if cid and not sn76489_dual:
                stray_sn_second = True
            vgm.writes.append(ChipWrite(sample, "sn76489", cid, 1, value))
            if value != 0xFF:
                vgm.sn76489_stereo = True
            pos += 2
            continue
        if cmd in (CMD_SN76489, CMD_SN76489_2) and pos + 2 <= n:
            # One SN76489 serial data byte.  The latch/register/channel is
            # encoded inside the byte; reg 0.  0x30 is the second half of a
            # dual SN76489 or the right half of a T6W28.
            cid = 0 if cmd == CMD_SN76489 else 1
            if cid and not sn76489_dual:
                stray_sn_second = True
            vgm.writes.append(ChipWrite(sample, "sn76489", cid, 0, buf[pos + 1]))
            pos += 2
            continue
        if cmd in (CMD_YM2151, CMD_YM2151_2) and pos + 3 <= n:
            cid = 0
            if cmd == CMD_YM2151_2:
                if ym2151_dual:
                    cid = 1
                else:
                    stray_ym2151_second = True
            if cmd != CMD_YM2151_2 or ym2151_dual:
                vgm.writes.append(ChipWrite(
                    sample, "ym2151", cid, buf[pos + 1], buf[pos + 2],
                ))
            pos += 3
            continue
        if (
            cmd in (
                CMD_YM3526, CMD_YM3526_2, CMD_YM3812, CMD_YM3812_2,
                CMD_OPL3_P0, CMD_OPL3_P1, CMD_OPL3_B_P0, CMD_OPL3_B_P1,
                CMD_Y8950, CMD_Y8950_2,
            )
            and pos + 3 <= n
        ):
            # The OPL chips take (register, value) in one flat space. The YMF262
            # has two banks of nine channels and the command *is* the bank. The
            # port is folded into bit 8 of reg exactly like the YM2612's
            # second register bank; every per-channel register then names one
            # hardware channel unambiguously.
            if cmd in (CMD_YM3526, CMD_YM3526_2):
                cid = 0
                if cmd == CMD_YM3526_2:
                    if opl_dual:
                        cid = 1
                    else:
                        stray_opl_second = True
                if cmd != CMD_YM3526_2 or opl_dual:
                    vgm.writes.append(ChipWrite(
                        sample, "opl", cid, buf[pos + 1], buf[pos + 2],
                    ))
            elif cmd in (CMD_YM3812, CMD_YM3812_2):
                cid = 0
                if cmd == CMD_YM3812_2:
                    if opl2_dual:
                        cid = 1
                    else:
                        stray_opl2_second = True
                if cmd != CMD_YM3812_2 or opl2_dual:
                    vgm.writes.append(ChipWrite(
                        sample, "opl2", cid, buf[pos + 1], buf[pos + 2],
                    ))
            elif cmd in (CMD_Y8950, CMD_Y8950_2):
                cid = 0
                if cmd == CMD_Y8950_2:
                    if y8950_dual:
                        cid = 1
                    else:
                        stray_y8950_second = True
                if cmd != CMD_Y8950_2 or y8950_dual:
                    vgm.writes.append(ChipWrite(
                        sample, "y8950", cid, buf[pos + 1], buf[pos + 2],
                    ))
            else:
                cid = 0
                if cmd in (CMD_OPL3_B_P0, CMD_OPL3_B_P1):
                    if opl3_dual:
                        cid = 1
                    else:
                        stray_opl3_second = True
                if cmd not in (CMD_OPL3_B_P0, CMD_OPL3_B_P1) or opl3_dual:
                    port = 1 if cmd in (CMD_OPL3_P1, CMD_OPL3_B_P1) else 0
                    vgm.writes.append(ChipWrite(
                        sample, "opl3", cid,
                        (port << 8) | buf[pos + 1], buf[pos + 2],
                    ))
            pos += 3
            continue
        if cmd == CMD_YMF278B and pos + 4 <= n:
            # 0xD0 pp aa dd. Ports 0/1 are the OPL3 FM banks and port 2 the 24
            # wave slots; the port rides in bit 8 (or 9) of reg so one flat
            # stream keeps its order.  Bit 7 of the port selects the second
            # chip (Furnace exports it that way).
            pp = buf[pos + 1]
            cid = (pp >> 7) & 1
            if cid and not ymf278b_dual:
                stray_opl4_second = True
            port = pp & 0x7F
            vgm.writes.append(ChipWrite(
                sample, "ymf278b", cid,
                (port << 8) | buf[pos + 2], buf[pos + 3],
            ))
            pos += 4
            continue
        if cmd == CMD_VSU and pos + 4 <= n:
            # 0xC7 mmll dd: 16-bit big-endian register *word* index.  Bit 15
            # marks the second chip.  Wave RAM, the mod table and the channel
            # registers all live in one flat index space.
            reg = int.from_bytes(buf[pos + 1:pos + 3], "big")
            cid = 1 if (reg & VSU_DUAL_BIT) else 0
            if cid and not vsu_dual:
                stray_vsu_second = True
            vgm.writes.append(ChipWrite(
                sample, "vsu", cid, reg & 0x7FFF, buf[pos + 3],
            ))
            pos += 4
            continue
        if cmd in (CMD_YMZ280B, CMD_YMZ280B_2) and pos + 3 <= n:
            # 0x5D is the first YMZ280B, 0xAD the second (YM-family +0x50).
            cid = 0 if cmd == CMD_YMZ280B else 1
            if cid and not ymz280b_dual:
                stray_ymz_second = True
            vgm.writes.append(ChipWrite(
                sample, "ymz280b", cid, buf[pos + 1], buf[pos + 2],
            ))
            pos += 3
            continue
        if (
            cmd in (CMD_YM2612_P0, CMD_YM2612_P1, CMD_YM2612_B_P0, CMD_YM2612_B_P1)
            and pos + 3 <= n
        ):
            # 0x52/0x53 are the first YM2612's two banks, 0xA2/0xA3 the
            # second chip's.  The register address is kept flat so bank 1
            # reads as 0x100+reg (ymfm's index), which is how channels 4-6
            # are selected.
            cid = 0
            if cmd in (CMD_YM2612_B_P0, CMD_YM2612_B_P1):
                if ym2612_dual:
                    cid = 1
                else:
                    stray_ym2612_second = True
            bank = 0x100 if cmd in (CMD_YM2612_P1, CMD_YM2612_B_P1) else 0
            vgm.writes.append(ChipWrite(
                sample, "ym2612", cid, bank | buf[pos + 1], buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd in (CMD_YM2203, CMD_YM2203_2) and pos + 3 <= n:
            # 0x55 is the first instance; 0xA5 is the second, only valid when
            # the header carries the dual bit.  Registers 0x00-0x0F belong to
            # the built-in SSG, the rest are FM.
            cid = 0
            if cmd == CMD_YM2203_2:
                if ym2203_dual:
                    cid = 1
                else:
                    stray_ym2203_second = True
            reg = buf[pos + 1]
            val = buf[pos + 2]
            name = "ym2203_ssg" if reg < 0x10 else "ym2203"
            vgm.writes.append(ChipWrite(sample, name, cid, reg, val))
            pos += 3
            continue
        if (
            cmd in (CMD_YM2610_P0, CMD_YM2610_P1, CMD_YM2610_B_P0, CMD_YM2610_B_P1)
            and pos + 3 <= n
        ):
            # 0x58/0x59 are the first chip's two ports, 0xA8/0xA9 the second.
            # Port 1 is stored as 0x100 | register.  Port 0 registers 0x00-0x0F
            # are the built-in SSG; everything else is FM, ADPCM or global.
            cid = 0
            if cmd in (CMD_YM2610_B_P0, CMD_YM2610_B_P1):
                if ym2610_dual:
                    cid = 1
                else:
                    stray_ym2610_second = True
            port1 = cmd in (CMD_YM2610_P1, CMD_YM2610_B_P1)
            reg = buf[pos + 1]
            name = "ym2610_ssg" if (not port1 and reg < 0x10) else "ym2610"
            vgm.writes.append(ChipWrite(
                sample, name, cid, (0x100 if port1 else 0) | reg, buf[pos + 2],
            ))
            pos += 3
            continue
        if (
            cmd in (CMD_YM2608_P0, CMD_YM2608_P1,
                    CMD_YM2608_B_P0, CMD_YM2608_B_P1)
            and pos + 3 <= n
        ):
            # 0x56/0x57 are the first chip's buses A/B; 0xA6/0xA7 the second.
            # Bus B rides in bit 8 exactly like a YM2612 bank, so one flat
            # address serves both FM blocks; bus A below 0x10 is the SSG and
            # 0x10-0x1F its rhythm section. docs/opna-opll.md has the table.
            cid = 0
            if cmd in (CMD_YM2608_B_P0, CMD_YM2608_B_P1):
                if ym2608_dual:
                    cid = 1
                else:
                    stray_ym2608_second = True
            port1 = cmd in (CMD_YM2608_P1, CMD_YM2608_B_P1)
            reg = buf[pos + 1]
            name = "ym2608_ssg" if (not port1 and reg < 0x10) else "ym2608"
            vgm.writes.append(ChipWrite(
                sample, name, cid, (0x100 if port1 else 0) | reg, buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd in (CMD_YM2413, CMD_YM2413_2) and pos + 3 <= n:
            # One flat register space, like the other OPLs but with banked
            # user-instrument bytes below 0x10 and rhythm control at $0E.
            # 0xA1 is the second chip's stream, gated on clock-0x10 bit 30.
            cid = 0
            if cmd == CMD_YM2413_2:
                if ym2413_dual:
                    cid = 1
                else:
                    stray_opll_second = True
            vgm.writes.append(ChipWrite(sample, "opll", cid, buf[pos + 1],
                                        buf[pos + 2]))
            pos += 3
            continue
        if cmd == CMD_K007232 and pos + 3 <= n:
            aa = buf[pos + 1]
            cid = (aa >> 7) & 1
            if cid:
                stray_k007232_second = True
            vgm.writes.append(ChipWrite(sample, "k007232", cid, aa & 0x7F, buf[pos + 2]))
            pos += 3
            continue
        if cmd == CMD_AY8910 and pos + 3 <= n:
            # 0xA0: register, then data. Bit 7 of the register selects the
            # second chip instance (libvgm reads it the same way).
            rr = buf[pos + 1]
            cid = (rr >> 7) & 1
            if cid and not ay8910_dual:
                stray_ay_second = True
            vgm.writes.append(ChipWrite(sample, "ay8910", cid, rr & 0x7F, buf[pos + 2]))
            pos += 3
            continue
        if cmd in (CMD_MSM6295, CMD_MSM6258) and pos + 3 <= n:
            aa = buf[pos + 1]
            name = "msm6295" if cmd == CMD_MSM6295 else "msm6258"
            cid = (aa >> 7) & 1
            dual = msm6295_dual if name == "msm6295" else bool(adpcm_raw & 0x40000000)
            if cid and not dual:
                if name == "msm6295":
                    stray_msm6295_second = True
                else:
                    stray_msm6258_second = True
            vgm.writes.append(ChipWrite(sample, name, cid, aa & 0x7F, buf[pos + 2]))
            pos += 3
            continue
        if cmd == CMD_SEGAPCM and pos + 4 <= n:
            # 0xC0: u16-LE address (register/voice map), then data.  Bit 15
            # selects the second instance, matching Furnace's export.
            addr = int.from_bytes(buf[pos + 1:pos + 3], "little")
            cid = (addr >> 15) & 1
            if cid:
                stray_segapcm_second = True
            vgm.writes.append(ChipWrite(
                sample, "segapcm", cid, addr & 0x7FF, buf[pos + 3],
            ))
            pos += 4
            continue
        if cmd == CMD_GB and pos + 3 <= n:
            # 0xB3: register, data.  Register 0x00 is FF10 (NR10); 0x20-0x2F
            # is the wave RAM.  Bit 7 of the register selects a second APU
            # (Dual Chip Support #2), so mask it off like the NES does.
            rr = buf[pos + 1]
            gb_cid = (rr >> 7) & 1
            if gb_cid and not gb_dmg_dual:
                vgm.warnings.append(
                    "0xB3 Game Boy writes with register bit 7 set but no "
                    "dual-instance header bit; they were treated as the second APU"
                )
            vgm.writes.append(ChipWrite(
                sample, "gb", gb_cid, rr & 0x7F, buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd == CMD_NES and pos + 3 <= n:
            # 0xB4: register, data. Bit 7 of the register selects the second
            # APU instance (0x00-0x17 = $4000-$4017).
            rr = buf[pos + 1]
            nes_cid = (rr >> 7) & 1
            if nes_cid and not nes_dual:
                vgm.warnings.append(
                    "0xB4 NES writes with register bit 7 set but no "
                    "dual-instance header bit; they were treated as the second APU"
                )
            vgm.writes.append(ChipWrite(
                sample, "nes", nes_cid, rr & 0x7F, buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd == CMD_HUC6280 and pos + 3 <= n:
            # 0xB9: register, data.  Bit 7 of the register selects a second
            # chip instance.  The register file is a channel-select latch at
            # 0x00 followed by per-channel writes.
            rr = buf[pos + 1]
            huc_cid = (rr >> 7) & 1
            if huc_cid and not huc6280_dual:
                vgm.warnings.append(
                    "0xB9 HuC6280 writes with register bit 7 set but no "
                    "dual-instance header bit; they were treated as the second chip"
                )
            vgm.writes.append(ChipWrite(
                sample, "huc6280", huc_cid, rr & 0x7F, buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd in (CMD_RF5C68, CMD_RF5C164) and pos + 3 <= n:
            # 0xB0/0xB1: (register, data). One shared register file; the clock
            # tells the two chips apart. Register 0x07 selects the RAM bank;
            # bit 7 selects a second chip (Furnace's RF5C is not dual-able,
            # but the stream still names it).
            name = "rf5c68" if cmd == CMD_RF5C68 else "rf5c164"
            rr = buf[pos + 1]
            cid = (rr >> 7) & 1
            reg = rr & 0x7F
            val = buf[pos + 2]
            if cid:
                stray_rf5c_second = True
            if reg == 0x07 and not (val & 0x40):
                rf5c_wbank[(name, cid)] = (val & 0x0F) << 12
            vgm.writes.append(ChipWrite(sample, name, cid, reg, val))
            pos += 3
            continue
        if cmd in (CMD_RF5C68_MEM, CMD_RF5C164_MEM) and pos + 4 <= n:
            # 0xC1/0xC2: 16-bit LE offset, then data. The offset rides on the
            # bank register 0x07 set earlier; a "mem" chip carries the resolved
            # flat address so the collector can just fill bytes in order.
            name = "rf5c68" if cmd == CMD_RF5C68_MEM else "rf5c164"
            offset = int.from_bytes(buf[pos + 1:pos + 3], "little")
            addr = (rf5c_wbank.get((name, 0), 0) | offset) & 0xFFFF
            vgm.writes.append(ChipWrite(sample, f"{name}_mem", 0, addr, buf[pos + 3]))
            pos += 4
            continue
        if cmd == CMD_SCC and pos + 4 <= n:
            # 0xD2 pp aa dd: SCC "port" (register bank), register, data. Bit 7
            # of pp selects the second chip instance. The register space is
            # kept flat as (port << 8) | addr; scc.py decodes the banks.
            pp = buf[pos + 1]
            reg = ((pp & 0x7F) << 8) | buf[pos + 2]
            cid = (pp >> 7) & 1
            if cid and not k051649_dual:
                stray_scc_second = True
            vgm.writes.append(ChipWrite(sample, "k051649", cid, reg, buf[pos + 3]))
            pos += 4
            continue
        if cmd == CMD_C140 and pos + 4 <= n:
            # 0xD4: 16-bit big-endian address, then data.  Bit 15 is the chip
            # select Furnace writes for a second instance; the register is the
            # low nine bits.
            addr = int.from_bytes(buf[pos + 1:pos + 3], "big")
            cid = (addr >> 15) & 1
            if cid and not c140_dual:
                stray_c140_second = True
            vgm.writes.append(ChipWrite(
                sample, "c140", cid, addr & 0x1FF, buf[pos + 3],
            ))
            pos += 4
            continue
        if cmd == CMD_C352 and pos + 5 <= n:
            # 0xE1: uint16 BE address, uint16 BE data. Bit 15 of the address
            # is the chip id. 0x202 strobes the latched key on/off bits.
            addr = int.from_bytes(buf[pos + 1:pos + 3], "big")
            data = int.from_bytes(buf[pos + 3:pos + 5], "big")
            vgm.writes.append(ChipWrite(
                sample, "c352", (addr >> 15) & 1, addr & 0x7FFF, data,
            ))
            pos += 5
            continue
        if cmd == CMD_K053260 and pos + 3 <= n:
            # 0xBA: register, data. Bit 7 of the register selects the second
            # instance; the register space is six bits.
            aa = buf[pos + 1]
            cid = (aa >> 7) & 1
            if cid and not k053260_dual:
                stray_k053260_second = True
            vgm.writes.append(ChipWrite(
                sample, "k053260", cid, aa & 0x3F, buf[pos + 2],
            ))
            pos += 3
            continue
        if cmd == CMD_STREAM_SETUP:
            vgm.stream_setups += 1
        if cmd == 0x68 and pos + 12 <= n:
            # 68 tt dd dd dd aa aa aa ll ll ll: copy a data-block window into
            # chip RAM.  The NES DPCM bank uses this to switch 4 KB windows.
            typ = buf[pos + 2]
            src = int.from_bytes(buf[pos + 3:pos + 6], "little")
            dest = int.from_bytes(buf[pos + 6:pos + 9], "little")
            length = int.from_bytes(buf[pos + 9:pos + 12], "little")
            vgm.pcm_ram_writes.append(PcmRamWrite(
                sample=sample, type=typ & 0x7F, chip_id=(typ >> 7) & 1,
                src=src, dest=dest, length=length or 0x1000000,
            ))
            pos += 12
            continue
        if cmd not in KNOWN_CMDS:
            # never skip a guessed number of operands and desync the stream
            vgm.warnings.append(f"unsupported VGM command {cmd:02X} at 0x{pos:X}; stopped parsing")
            break
        if cmd in UNCONVERTED_SAMPLE_CMDS:
            skipped_sample_cmds.add(cmd)
        pos += cmd_size(cmd)

    vgm.loop_sample = loop_sample
    # The 0x66 command's sample is the timeline the file really covers.  A
    # header total far past it truncates the converted module against a
    # source that still holds its last note, and a header total of 0 loses
    # the grid/order budget entirely. The stream length wins.
    if saw_end:
        if total != sample:
            if total and abs(total - sample) > VGM_RATE // 10:
                vgm.warnings.append(
                    f"header length {total} samples, stream ends at {sample}; "
                    "using the stream length"
                )
            vgm.total_samples = sample
    elif not total:
        vgm.total_samples = sample
    if skipped_sample_cmds:
        names = sorted({UNCONVERTED_SAMPLE_CMDS[c] for c in skipped_sample_cmds})
        first = min(skipped_sample_cmds)
        vgm.warnings.append(
            f"VGM writes to a sample chip this tool does not convert "
            f"({', '.join(names)}; first command {first:02X}); its samples are missing"
        )
    if saw_adpcm_compressed:
        vgm.warnings.append(
            "compressed OKIM6258 data block (type 44) is not converted"
        )
    if any(w.chip == "c352" for w in vgm.writes) and not vgm.c352_clock:
        vgm.warnings.append("C352 clock is missing or out of range; using 24192000")
    if any(w.chip == "ay8910" for w in vgm.writes) and not vgm.ay8910_clock:
        vgm.warnings.append("AY-3-8910 clock is missing or out of range; using 1789772")
    if any(w.chip == "k051649" for w in vgm.writes) and not vgm.k051649_clock:
        vgm.warnings.append("K051649 clock is missing or out of range; using 1789772")
    if any(w.chip == "huc6280" for w in vgm.writes) and not vgm.huc6280_clock:
        vgm.warnings.append(
            f"HuC6280 clock is missing or out of range; using {HUC6280_DEFAULT_CLOCK}"
        )
    if any(w.chip == "k053260" for w in vgm.writes) and not vgm.k053260_clock:
        vgm.warnings.append(
            "K053260 clock is missing or out of range; using 3579545"
        )
    if any(w.chip == "rf5c68" for w in vgm.writes) and not vgm.rf5c68_clock:
        vgm.warnings.append(
            f"RF5C68 clock is missing or out of range; using {RF5C68_DEFAULT_CLOCK}"
        )
    if any(w.chip == "rf5c164" for w in vgm.writes) and not vgm.rf5c164_clock:
        vgm.warnings.append(
            f"RF5C164 clock is missing or out of range; using {RF5C164_DEFAULT_CLOCK}"
        )
    if any(w.chip == "y8950" for w in vgm.writes) and not vgm.y8950_clock:
        vgm.warnings.append(
            f"Y8950 clock is missing or out of range; using {Y8950_DEFAULT_CLOCK}"
        )
    if any(w.chip == "ymf278b" for w in vgm.writes) \
            and not vgm.ymf278b_clock:
        vgm.warnings.append(
            "YMF278B clock is missing or out of range; using "
            f"{YMF278B_DEFAULT_CLOCK}"
        )
    if any(w.chip in ("ym2203", "ym2203_ssg") for w in vgm.writes) and not vgm.ym2203_clock:
        vgm.warnings.append("YM2203 clock is missing or out of range; using 3579545")
    if any(w.chip in ("ym2610", "ym2610_ssg") for w in vgm.writes) and not vgm.ym2610_clock:
        vgm.warnings.append(
            f"YM2610 clock is missing or out of range; using {YM2610_DEFAULT_CLOCK}"
        )
    ym2610_adpcm_a = any(
        w.chip == "ym2610" and (w.reg & 0x100) and (w.reg & 0xFF) <= 0x2D
        for w in vgm.writes
    )
    ym2610_adpcm_b = any(
        w.chip == "ym2610" and not (w.reg & 0x100) and 0x10 <= (w.reg & 0xFF) <= 0x1B
        for w in vgm.writes
    )
    if (ym2610_adpcm_a or ym2610_adpcm_b) and not any(
        r.kind == "ym2610_adpcm" for r in vgm.roms
    ):
        vgm.warnings.append(
            "YM2610 ADPCM writes but no ADPCM ROM block (0x82); "
            "the sample channels will be silent"
        )
    if stray_ym2610_second:
        vgm.warnings.append(
            "0xA8/0xA9 YM2610 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_ym2608_second:
        vgm.warnings.append(
            "0xA6/0xA7 YM2608 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_opll_second:
        vgm.warnings.append(
            "0xA1 YM2413 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if (any(w.chip in ("ym2608", "ym2608_ssg") for w in vgm.writes)
            and not vgm.ym2608_clock):
        vgm.warnings.append(
            "YM2608 clock is missing or out of range; using the FM Towns clock"
        )
    if any(w.chip == "opll" for w in vgm.writes) and not vgm.ym2413_clock:
        vgm.warnings.append(
            "YM2413 clock is missing or out of range; using the standard clock"
        )
    if any(w.chip == "gb" for w in vgm.writes) and not vgm.gb_dmg_clock:
        vgm.warnings.append(
            f"Game Boy DMG clock is missing or out of range; using {GB_DMG_DEFAULT_CLOCK}"
        )
    if any(w.chip == "nes" for w in vgm.writes) and not vgm.nes_clock:
        vgm.warnings.append(
            f"NES APU clock is missing or out of range; using {NES_DEFAULT_CLOCK}"
        )
    if any(w.chip == "ym2612" for w in vgm.writes) and not vgm.ym2612_clock:
        vgm.warnings.append(
            f"YM2612 clock is missing or out of range; using {YM2612_DEFAULT_CLOCK}"
        )
    # OPL family. The three clock fields all carry the second-instance bit, but
    # only the YM3812 pair has a distinct command ($AA). A flagged second OPL
    # or OPL3 is reported instead of folded into the first chip's registers.
    if any(w.chip == "opl" for w in vgm.writes) and not vgm.opl_clock:
        vgm.warnings.append(
            f"YM3526 clock is missing or out of range; using {OPL_DEFAULT_CLOCK}"
        )
    if any(w.chip == "opl2" for w in vgm.writes) and not vgm.opl2_clock:
        vgm.warnings.append(
            f"YM3812 clock is missing or out of range; using {OPL2_DEFAULT_CLOCK}"
        )
    if any(w.chip == "opl3" for w in vgm.writes) and not vgm.opl3_clock:
        vgm.warnings.append(
            f"YMF262 clock is missing or out of range; using {OPL3_DEFAULT_CLOCK}"
        )
    for off, name in ((OPL_CLOCK_OFF, "YM3526"), (OPL3_CLOCK_OFF, "YMF262")):
        raw = _header_raw(buf, off) if ver >= 0x151 else 0
        if raw & OPL_DUAL_BIT:
            vgm.warnings.append(
                f"header declares a second {name}; only the first chip's "
                "register stream is converted"
            )
    if any(w.chip == "sn76489" for w in vgm.writes) and not vgm.sn76489_clock:
        if vgm.sn76489_t6w28:
            vgm.sn76489_clock = 3072000
            vgm.warnings.append(
                "T6W28 clock is missing or out of range; using 3072000"
            )
        else:
            vgm.warnings.append(
                f"SN76489 clock is missing or out of range; using {SN76489_DEFAULT_CLOCK}"
            )
    if (
        vgm.dac_data and not vgm.dac_events and not vgm.dac_block_plays
        and not vgm.dac_stream_plays
    ):
        vgm.warnings.append(
            "YM2612 PCM data block present but no DAC play stream; "
            "the DAC data is unused"
        )
    if vgm.dac_events and not vgm.dac_data and any(kind == 1 for _t, kind, _v in vgm.dac_events):
        vgm.warnings.append(
            "YM2612 DAC play commands present but no PCM data block; "
            "the DAC stream cannot be reconstructed"
        )
    if (
        not vgm.dac_data
        and any(kind == 2 for _t, kind, _v in vgm.dac_events)
    ):
        vgm.warnings.append(
            "YM2612 direct 0x2A DAC writes are not converted (no PCM bank)"
        )
    if vgm.dac_block_plays and not vgm.dac_events:
        vgm.warnings.append(
            "modern 0x95 DAC stream control is parsed for the audit but not "
            "converted as source PCM"
        )
    if (
        any(p.chip_type != STREAM_C6280 for p in vgm.dac_stream_plays)
        and not vgm.dac_data
    ):
        vgm.warnings.append(
            "DAC stream start commands present but no PCM data block; "
            "the stream cannot be reconstructed"
        )
    if stray_ym2612_second:
        vgm.warnings.append(
            "0xA2/0xA3 YM2612 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_ym2203_second:
        vgm.warnings.append(
            "0xA5 YM2203 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_opl2_second:
        vgm.warnings.append(
            "0xAA YM3812 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_ym2151_second:
        vgm.warnings.append(
            "0xA4 YM2151 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_opl_second:
        vgm.warnings.append(
            "0xAB YM3526 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_opl3_second:
        vgm.warnings.append(
            "0xAE/0xAF YMF262 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_y8950_second:
        vgm.warnings.append(
            "0xAC Y8950 writes without the dual-instance header bit; "
            "they were treated as the first chip"
        )
    if stray_opl4_second:
        vgm.warnings.append(
            "0xD0 YMF278B writes with port bit 7 set but no dual-instance "
            "header bit; they were treated as the second chip"
        )
    if stray_sn_second:
        vgm.warnings.append(
            "0x30/0x3F SN76489 writes without the dual-instance header bit; "
            "they were treated as the second chip"
        )
    if vgm.sn76489_dual and not vgm.sn76489_t6w28:
        vgm.warnings.append(
            "dual SN76489 log: both chips are converted as separate systems"
        )
    if (sn76489_raw & SN76489_T6W28_BIT) and not vgm.sn76489_dual:
        vgm.warnings.append(
            "T6W28 flag without the dual-instance header bit; the SN76489 "
            "stream was converted as mono"
        )
    if stray_c140_second:
        vgm.warnings.append(
            "0xD4 C140 address bit 15 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_k053260_second:
        vgm.warnings.append(
            "0xBA K053260 register bit 7 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_ay_second:
        vgm.warnings.append(
            "0xA0 AY-8910 register bit 7 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_scc_second:
        vgm.warnings.append(
            "0xD2 K051649 port bit 7 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_msm6295_second:
        vgm.warnings.append(
            "0xB8 MSM6295 register bit 7 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_msm6258_second:
        vgm.warnings.append(
            "0xB7 MSM6258 register bit 7 selects a second chip without the "
            "dual-instance header bit; it was still treated as chip 1"
        )
    if stray_ymz_second:
        vgm.warnings.append(
            "0xAD YMZ280B writes without the dual-instance header bit; "
            "they were treated as the second chip"
        )
    if (any(w.chip == "ymz280b" for w in vgm.writes)
            and not vgm.ymz280b_clock):
        vgm.warnings.append(
            "YMZ280B writes without a header clock; using 16934400 Hz"
        )
        vgm.ymz280b_clock = YMZ280B_DEFAULT_CLOCK
    if stray_vsu_second:
        vgm.warnings.append(
            "0xC7 VSU writes with register bit 15 set but no dual-instance "
            "header bit; they were treated as the second chip"
        )
    if any(w.chip == "vsu" for w in vgm.writes) and not vgm.vsu_clock:
        vgm.warnings.append(
            "VSU writes without a header clock; using 5000000 Hz"
        )
        vgm.vsu_clock = VSU_DEFAULT_CLOCK
    return vgm


def assemble_rom(vgm: VgmFile, kind: str, chip_id: int = 0) -> bytes:
    """Stitch every ROM slice of a kind into one buffer."""
    slices = [s for s in vgm.roms if s.chip == chip_id and s.kind == kind]
    if not slices:
        return b""
    rom_size = max(s.rom_size for s in slices) or 0
    end = max((s.start + len(s.data)) for s in slices)
    size = max(rom_size, end)
    rom = bytearray(size)
    for s in slices:
        rom[s.start:s.start + len(s.data)] = s.data
    return bytes(rom)


def assemble_k007232_rom(vgm: VgmFile, chip_id: int = 0) -> bytes:
    return assemble_rom(vgm, "k007232", chip_id)
