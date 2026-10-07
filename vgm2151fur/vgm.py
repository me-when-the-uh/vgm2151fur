"""VGM / VGZ loader and command walker (YM2151, K007232, MSM6295, MSM6258, SegaPCM, C140, C352)."""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path


VGM_RATE = 44100
CMD_YM2151 = 0x54
CMD_K007232 = 0x41
CMD_MSM6258 = 0xB7
CMD_MSM6295 = 0xB8
CMD_SEGAPCM = 0xC0
CMD_C140 = 0xD4
CMD_C352 = 0xE1
CMD_DATA_BLOCK = 0x67
CMD_END = 0x66
CMD_STREAM_SETUP = 0x90
K007232_ROM_TYPE = 0x94  # 0x80 + 0x14
OKI_ROM_TYPE = 0x8B  # OKIM6295 ROM
SEGAPCM_ROM_TYPE = 0x80  # SegaPCM ROM
C140_ROM_TYPE = 0x8D  # Namco C140 ROM
C352_ROM_TYPE = 0x92  # Namco C352 ROM
K007232_TRIGGER_REG = 0x1F
# Extra-header chip ids (libvgm device index). 0x2A is K007232.
EXTRA_MSM6258 = 0x16
EXTRA_MSM6295 = 0x17
EXTRA_SEGAPCM = 0x04
EXTRA_C140 = 0x1C
EXTRA_C352 = 0x27
EXTRA_K007232 = 0x2A
# Main-header clocks (VGMFile.h layout, spec v1.71). 0x98 is MSM6295, 0x90
# MSM6258, 0x94 its flags. K007232 has no spec field; VGMPlay tools put it at
# 0xE8 (1.71+). C140 is 0xA8 and its type byte 0x96.
SEGAPCM_CLOCK_OFF = 0x38
SEGAPCM_INTF_OFF = 0x3C  # bank shift; mask is the byte at 0x3E (0 → 0x70)
SEGAPCM_MASK_OFF = 0x3E
MSM6258_CLOCK_OFF = 0x90
MSM6295_CLOCK_OFF = 0x98
K007232_EXTRA_CLOCK_OFF = 0xE8
C140_CLOCK_OFF = 0xA8
C140_TYPE_OFF = 0x96
C352_CLOCK_OFF = 0xDC  # VGM 1.71 crystal; bit 31 mutes the rear pair
C352_DIV_OFF = 0xD6  # hardware divider / 4; 72 means a divider of 288
SEGAPCM_CLOCK_X = 4000000  # X board / Hang-On
SEGAPCM_CLOCK_Y = 4026987  # Y board: 32215900/8
C140_DEFAULT_CLOCK = 12288000  # System 2/21 rips with a bogus header field
CLOCK_MIN = 100_000
CLOCK_MAX = 40_000_000
# Sample-chip write commands this tool does not convert. Anything here that
# shows up in a stream means samples are being dropped. Say so.
UNCONVERTED_SAMPLE_CMDS = {
    0xA0: "MultiPCM", 0xB0: "RF5C68", 0xB1: "RF5C164", 0xB2: "PWM",
    0xB6: "uPD7759", 0x40: "NES APU DPCM",
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
    kind: str = "k007232"  # "k007232" | "msm6295" | "segapcm" | "c140" | "c352"


@dataclass
class ChipWrite:
    sample: int
    chip: str  # "ym2151" | "k007232" | "msm6295" | "msm6258" | "segapcm" | "c140" | "c352"
    chip_id: int
    reg: int
    val: int


@dataclass
class VgmFile:
    path: Path
    buf: bytes
    version: int
    total_samples: int
    loop_samples: int
    loop_file_pos: int | None
    loop_sample: int | None
    ym2151_clock: int
    k007232_clock: int
    gd3: dict[str, str]
    roms: list[RomSlice] = field(default_factory=list)
    writes: list[ChipWrite] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # MSM6295 header 0x98 (VGM 1.61+). Bit 31 of the raw field is pin 7,
    # bit 30 marks a second chip (the player halves each instance's volume).
    msm6295_clock: int = 0
    msm6295_pin7: bool = False
    msm6295_dual: bool = False
    # MSM6258 header 0x90 and flag byte 0x94 (divider in bits 0-1).
    msm6258_clock: int = 0
    msm6258_flags: int = 0
    # SegaPCM header 0x38 (VGM 1.51+); C140 header 0xA8 + type byte 0x96
    # (0 = System 2, 1 = System 21, 2 = ASIC219) from 1.61.
    # Bank shift is header byte 0x3C, mask is 0x3E (libvgm). Mask 0 means 0x70.
    segapcm_clock: int = 0
    segapcm_bankshift: int = 0
    segapcm_bankmask: int = 0
    c140_clock: int = 0
    c140_type: int = 0
    # C352 header 0xDC / divider byte 0xD6 (VGM 1.71). 0 means the chip is absent;
    # the analyzer supplies 24192000 only when it is actually converting C352.
    c352_clock: int = 0
    c352_divider: int = 0
    c352_mute_rear: bool = False
    # Extra-header clocks, raw (bit 31 kept). Keyed by libvgm chip id.
    extra_clocks: dict[int, int] = field(default_factory=dict)
    # Extra-header volume mix: chip id -> (flags, raw value, 0x100 = 1.0).
    extra_volumes: dict[int, tuple[int, int]] = field(default_factory=dict)
    stream_setups: int = 0

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

    Names match the converter's chips: ym2151, msm6295, segapcm, c140, c352.
    C219 is the C140-family part the converter does not support. It is reported
    apart from c140, and those packs should not be batch-converted. C352 is
    its own chip (32 voices); it is not folded into c140 or c219.
    """
    if buf[:4] != b"Vgm ":
        return set()
    kinds: set[str] = set()
    version = _header_raw(buf, 8)
    if _header_clock(buf, 0x30):
        kinds.add("ym2151")
    if version >= 0x151 and 100_000 <= _header_clock(buf, SEGAPCM_CLOCK_OFF) <= 40_000_000:
        kinds.add("segapcm")
    ctype = buf[C140_TYPE_OFF] if version >= 0x161 and len(buf) > C140_TYPE_OFF else 0
    c140 = _header_clock(buf, C140_CLOCK_OFF)
    if c140 == 21390 or 100_000 <= c140 <= 40_000_000:
        kinds.add("c140")
    if ctype == 2:
        kinds.discard("c140")
        kinds.add("c219")
    if version >= 0x161 and 100_000 <= _header_clock(buf, MSM6295_CLOCK_OFF) <= 40_000_000:
        kinds.add("msm6295")
    if version >= 0x161 and 100_000 <= _header_clock(buf, MSM6258_CLOCK_OFF) <= 40_000_000:
        kinds.add("msm6258")
    if version >= 0x171 and 100_000 <= _header_clock(buf, K007232_EXTRA_CLOCK_OFF) <= 40_000_000:
        kinds.add("k007232")
    if len(buf) >= 0xC0:
        clocks, _volumes = _extra_chip_lists(buf)
        if clocks.get(EXTRA_SEGAPCM, 0):
            kinds.add("segapcm")
        if clocks.get(EXTRA_C140, 0):
            kinds.add("c140")
        if clocks.get(EXTRA_MSM6295, 0):
            kinds.add("msm6295")
        if clocks.get(EXTRA_MSM6258, 0):
            kinds.add("msm6258")
        if clocks.get(EXTRA_K007232, 0):
            kinds.add("k007232")
        if clocks.get(EXTRA_C352, 0):
            kinds.add("c352")
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
    ym_clock = _header_clock(buf, 0x30) or 3579545
    k_clock = _k007232_clock(buf, clocks, ym_clock, ver)
    oki_raw = _header_raw(buf, 0x98) if ver >= 0x161 else 0
    adpcm_raw = _header_raw(buf, 0x90) if ver >= 0x161 else 0
    adpcm_flags = buf[0x94] if ver >= 0x161 and _field_in_header(buf, 0x94, 1) else 0
    spcm_clock = _clock_or_zero(buf, SEGAPCM_CLOCK_OFF) if ver >= 0x151 else 0
    if not spcm_clock:
        spcm_clock = _sane_clock(clocks.get(EXTRA_SEGAPCM, 0))
    spcm_shift = buf[SEGAPCM_INTF_OFF] if ver >= 0x151 and _field_in_header(buf, SEGAPCM_INTF_OFF, 1) else 0
    spcm_mask = buf[SEGAPCM_MASK_OFF] if ver >= 0x151 and _field_in_header(buf, SEGAPCM_MASK_OFF, 1) else 0
    # Read the field for every version: the legacy System 21 rips carry
    # 21390 there even in 1.51 files, and players resolve it the same way.
    c140_raw = _header_raw(buf, C140_CLOCK_OFF) & 0x3FFFFFFF
    if not c140_raw:
        c140_raw = clocks.get(EXTRA_C140, 0) & 0x3FFFFFFF
    c140_clock = _c140_effective_clock(c140_raw)
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
        msm6295_clock=oki_raw & 0x3FFFFFFF,
        msm6295_pin7=bool(oki_raw & 0x80000000),
        msm6295_dual=bool(oki_raw & 0x40000000),
        msm6258_clock=adpcm_raw & 0x3FFFFFFF,
        msm6258_flags=adpcm_flags,
        segapcm_clock=spcm_clock,
        segapcm_bankshift=spcm_shift,
        segapcm_bankmask=spcm_mask,
        c140_clock=c140_clock,
        c140_type=c140_type,
        c352_clock=c352_clock,
        c352_divider=c352_divider,
        c352_mute_rear=c352_mute_rear,
        extra_clocks=clocks,
        extra_volumes=volumes,
    )

    pos = data_offset(buf)
    n = len(buf)
    sample = 0
    loop_sample: int | None = None
    skipped_sample_cmds: set[int] = set()
    while pos < n:
        if loop_pos is not None and loop_sample is None and pos >= loop_pos:
            loop_sample = sample
        cmd = buf[pos]
        if cmd == CMD_END:
            break
        if cmd == CMD_DATA_BLOCK:
            if pos + 7 > n:
                break
            typ = buf[pos + 2]
            size = int.from_bytes(buf[pos + 3:pos + 7], "little")
            chip_id = (size >> 31) & 1
            size &= 0x7FFFFFFF
            payload = buf[pos + 7:pos + 7 + size]
            kind = {
                K007232_ROM_TYPE: "k007232",
                OKI_ROM_TYPE: "msm6295",
                SEGAPCM_ROM_TYPE: "segapcm",
                C140_ROM_TYPE: "c140",
                C352_ROM_TYPE: "c352",
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
        if cmd == CMD_YM2151 and pos + 3 <= n:
            vgm.writes.append(ChipWrite(sample, "ym2151", 0, buf[pos + 1], buf[pos + 2]))
            pos += 3
            continue
        if cmd == CMD_K007232 and pos + 3 <= n:
            aa = buf[pos + 1]
            vgm.writes.append(ChipWrite(sample, "k007232", (aa >> 7) & 1, aa & 0x7F, buf[pos + 2]))
            pos += 3
            continue
        if cmd in (CMD_MSM6295, CMD_MSM6258) and pos + 3 <= n:
            aa = buf[pos + 1]
            name = "msm6295" if cmd == CMD_MSM6295 else "msm6258"
            vgm.writes.append(ChipWrite(sample, name, (aa >> 7) & 1, aa & 0x7F, buf[pos + 2]))
            pos += 3
            continue
        if cmd == CMD_SEGAPCM and pos + 4 <= n:
            # 0xC0: u16-LE address (register/voice map), then data.
            addr = int.from_bytes(buf[pos + 1:pos + 3], "little")
            vgm.writes.append(ChipWrite(sample, "segapcm", 0, addr & 0x7FF, buf[pos + 3]))
            pos += 4
            continue
        if cmd == CMD_C140 and pos + 4 <= n:
            # 0xD4: 9-bit register address, big-endian, then data. Bits 9+ are
            # the chip select Furnace writes for a second instance.
            addr = int.from_bytes(buf[pos + 1:pos + 3], "big")
            vgm.writes.append(ChipWrite(sample, "c140", addr >> 9, addr & 0x1FF, buf[pos + 3]))
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
        if cmd == CMD_STREAM_SETUP:
            vgm.stream_setups += 1
        if cmd == 0x68:
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
    if skipped_sample_cmds:
        names = sorted({UNCONVERTED_SAMPLE_CMDS[c] for c in skipped_sample_cmds})
        first = min(skipped_sample_cmds)
        vgm.warnings.append(
            f"VGM writes to a sample chip this tool does not convert "
            f"({', '.join(names)}; first command {first:02X}); its samples are missing"
        )
    if (
        vgm.msm6258_clock
        and vgm.stream_setups
        and not any(w.chip == "msm6258" for w in vgm.writes)
    ):
        vgm.warnings.append(
            "MSM6258 clock is set but the log has no 0xB7 data bytes; "
            "DAC streams (0x90-0x95) are not converted"
        )
    if any(w.chip == "c352" for w in vgm.writes) and not vgm.c352_clock:
        vgm.warnings.append("C352 clock is missing or out of range; using 24192000")
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
