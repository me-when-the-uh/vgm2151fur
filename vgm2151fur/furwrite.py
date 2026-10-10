"""Write a Furnace .fur (format 157): INFO + ADIR + INS2 + SMP2 + PATN.

Format 157 is the first version with PATN, where note 0 = C-(-5) and
108 = C-4 (no DefleMask leftover octave split). Uncompressed. Modern
Furnace still loads this; version only controls which INFO fields are
read, not which chips exist (YM2151 0x82, MSM6295 0xAA, MSM6258 0xAB,
K007232 0xC6, SegaPCM 0x9B, C140 0xCE, C352 0xD0).
"""

from __future__ import annotations

import struct
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace

from vgm2151fur.analyze import CARRIERS, Song
from vgm2151fur.c140 import c140_furnace_clock
from vgm2151fur.c352 import C352_DEFAULT_CLOCK
from vgm2151fur.oki import msm6258_flag_text, oki_flag_text, oki_needs_banked
from vgm2151fur.pcm import pcm7_to_s8, scale_pcm7

# Furnace arcade.cpp: rWrite(DT, dtTable[op.dt]<<4). Inverse of
# dtTable = (7, 6, 5, 0, 1, 2, 3, 4) so YM2151 register DT survives the
# second mapping. Raw DT in the file is heard as the wrong detune.
YM_DT_TO_FURNACE = (3, 4, 5, 6, 7, 2, 1, 0)


def ym_dt_to_furnace(ym_dt: int) -> int:
    return YM_DT_TO_FURNACE[ym_dt & 7]


def pack_dt_mul(dt_mul: int) -> int:
    """YM2151 0x40 register → Furnace FM-feature DT/MULT byte."""
    return (ym_dt_to_furnace((dt_mul >> 4) & 7) << 4) | (dt_mul & 0x0F)


FUR_MAGIC = b"-Furnace module-"
FUR_VERSION = 157  # PATN notes; INFO still the old block (not INF2)
CHIP_YM2151 = 0x82
CHIP_MSM6295 = 0xAA
CHIP_MSM6258 = 0xAB
CHIP_K007232 = 0xC6
CHIP_SEGAPCM = 0x9B
CHIP_C140 = 0xCE
# Furnace format.md reserves 0xD0 for Namco C352, 32 channels, and marks it
# unavailable. 0.6.8.3 has no C352 core and no C352 instrument type.
CHIP_C352 = 0xD0
INS_OPM = 33
INS_MSM6258 = 35
INS_MSM6295 = 36
INS_SEGAPCM = 39
INS_K007232 = 45
INS_C140 = 53
# DIV_INS_AMIGA. C140 channels already accept this sample type, so a future
# C352 core copied from c140.cpp can play the bodies. There is no DIV_INS_C352.
INS_C352 = 4
SMP_DEPTH_8BIT = 8
SMP_DEPTH_16BIT = 16
SMP_DEPTH_VOX = 10
# Furnace system defaults used to decide when to send customClock.
SEGAPCM_CLOCK_X = 4000000
SEGAPCM_CLOCK_Y = 4026987
C140_FURNACE_DEFAULT_CLOCK = 8192000
NOTE_OFF = 180
EMPTY = 0xFFFF
# Chip output levels, modelled on the reference player (libvgm VGMPlayer):
# _CHIP_VOLUME defaults in 8.8 fixed point with 0x100 = the YM2151's unity,
# the per-chip core patches, and the file's extra-header mix entries. An entry
# selects its instance in bit 0 of the flags byte; bit 15 of the value means
# "relative" (default * value), otherwise the value replaces the default.
# Multiple instances of a chip each carry default / instance count.
_DEV_DEFAULT_VOLUME = {
    0x03: 0x100, 0x04: 0x180, 0x18: 0x100, 0x19: 0x1C0, 0x1C: 0x100, 0x27: 0x40,
}
_DEV_CORE_PATCH = {0x1C: lambda v: (v * 2 + 1) // 3}  # C140/C219 core scale
# Furnace core output levels relative to the player's, from render comparisons.
_FURNACE_VOLUME_SCALE = {0x03: 1.0, 0x04: 1.0, 0x18: 1.0, 0x1C: 0.7}
# MSM6258 (0x19, the player default is 1.75) keeps unity; its Furnace core is
# uncalibrated.
_CHIP_DEV = {CHIP_YM2151: 0x03, CHIP_SEGAPCM: 0x04, CHIP_MSM6295: 0x18,
             CHIP_C140: 0x1C, CHIP_C352: 0x27}


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
    """Output volume for a .fur sample chip, calibrated to the player."""
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

    Operators are stored in YM2151 register order (0x40, 0x48, 0x50, 0x58).
    That is also Furnace's internal order: readFeatureFM reads the four ops
    with a straight loop and arcade.cpp writes op[j] to ADDR + opOffs[j] with
    opOffs = {0x00, 0x08, 0x10, 0x18}. (arcade.cpp's orderedOps = {0,2,1,3}
    is only for per-operator effect dispatch, not for the instrument/file.)
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
    carriers = set(CARRIERS.get(patch.alg & 7, (3,)))
    for idx in range(4):
        op = patch.ops[idx]
        # Keep the logged TL. KVS=2 still lets the volume column attenuate
        # carriers, but at vol 127 (Furnace default) TL is used as written.
        tl = op.tl & 0x7F
        kvs = 2 if idx in carriers else 0
        b.u8(pack_dt_mul(op.dt_mul))
        b.u8(tl)
        b.u8(op.ks_ar & 0xDF)
        b.u8(op.am_d1r)
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
) -> None:
    if not values:
        return
    b.u8(code)
    b.u8(len(values) & 0xFF)
    b.u8(loop & 0xFF)
    b.u8(release & 0xFF)
    b.u8(0)  # mode: sequence
    b.u8(1)  # open, 8-bit unsigned
    b.u8(delay & 0xFF)
    b.u8(max(1, speed) & 0xFF)
    for v in values:
        b.u8(v & 0xFF)


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
    b.u8(255)  # stop
    b.b[size_off:size_off + 2] = int(len(b.b) - start).to_bytes(2, "little")


def _write_ins_opm(b: Buf, name: str, patch) -> None:
    size_off, start = b.begin_block(b"INS2")
    b.u16(FUR_VERSION)
    b.u16(INS_OPM)
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


def _write_vox(b: Buf, name: str, data: bytes, c4_rate: int) -> None:
    """OKI ADPCM bytes. Furnace copies `length` bytes straight into the phrase ROM."""
    size_off, start = b.begin_block(b"SMP2")
    b.strz(name)
    b.u32(len(data))
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
    b.raw(data)
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
    """C140 system flags: bank type (System 2 vs System 21) and clock.

    The clock goes in Furnace's notation (2/3 of the chip crystal): the
    platform's pitch table and VGM export are both built around it, so
    12288000 becomes the 8192000 default and no custom clock is written.
    """
    parts = [f"bankType={1 if c140_type == 1 else 0}"]
    clk = c140_furnace_clock(clock)
    if clk != C140_FURNACE_DEFAULT_CLOCK:
        parts.append(f"customClock={clk}")
    return "\n".join(parts)


def c352_flag_text(clock: int) -> str:
    """Crystal for a C352 core that divides by 288.

    Furnace has no C352 default, so the flag is always written. A port copied
    from c140.cpp ticks at clock/192 and would need the C140 2/3 scale; the
    value here is the real crystal, not that scaled clock.
    """
    return f"customClock={clock or C352_DEFAULT_CLOCK}"


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


def dev_max(stats: dict) -> int:
    """Worst per-row pitch deviation from the driver's steps, in 1/64 st.

    A row can only render a straight-line slide, so this grows as the grid
    coarsens.  It is the fidelity number the condense pick compares (the
    `sweep_dev_bad` count is dominated by sample count, not severity).
    """
    return int(stats.get("sweep_dev_max", 0))


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
    """Pitch offset of the step-function trajectory at time ``t``.

    ``pts`` are the source's merged register writes in ``kc_kf_units`` (1/64
    semitone) relative to the note's own pitch; the driver holds each value
    until its next write, and the note sits at its own pitch before the first
    one.  Sampling this step function at row boundaries tracks the driver's
    coarse steps exactly. Fitting a window across them smears the attack.
    """
    v = 0
    for s, d in pts:
        if s > t:
            break
        v = d
    return v


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
    """Encode a key-on pitch trajectory as per-row 01xx/02xx ramps + E5 steps.

    Furnace can only ramp pitch at a fixed rate per tick and step fine tune
    (E5) at a row start, while the source writes coarse KC/KF pairs (a jump and
    then a slowing drift, typically).  Each row is matched by sampling the
    trajectory at its boundaries:

    - a jump that lands on the row is absorbed by an E5 step (one fine-tune
      unit per 1/64 semitone, within +-64 of the note);
    - the residual drift becomes the row's ramp rate, computed from the row's
      own boundary values so a fast step is not averaged away and a flat
      lead-in does not smear into the next row.

    The ramp physically runs past the trajectory's end to the last row
    boundary, so that row is rated over its full span against the held final
    value, and the stop lands on the following row, pinning the final pitch
    with E5.  A stop on a later note's own row is safe (its key-on still
    reads its own pitch), and when the trajectory runs into that note's row
    the stop must go there, since the row still belongs to this ramp until
    the later note's own k=0 rate takes it over.  The sweep never writes a
    *rate* or E5 on a later note's row (two 01xx/02xx codes on one row make
    the net slide column-order luck).

    E5 steps are recorded in ``e5_writes`` so the caller can replay all E5
    changes for the channel in row order.
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
    end = pts[-1][0]
    if ev.end and ev.end < end:
        end = ev.end
    if end - start < tick:
        # Shorter than a tick: no room to ramp, but a jump inside it still has
        # to land - the driver changes the register (usually within a few ms of
        # the key-on), so an instant transpose on the next row is the closest
        # the engine can get.
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
    # A later note owns its own row: clip the sweep before it, or this sweep's
    # tail rate (and any E5) cannot collide with the new note's own row's
    # rate/fine tune.  The crossing is usually a tick or two. The frozen
    # pitch loses almost nothing.
    crossed = False
    after = note_rows.get(ev.ch, ())
    i = bisect_right(after, row0)
    if i < len(after):
        limit = int(song.t0 + after[i] * row_len)
        if limit < end:
            end = limit
            crossed = True
            _bump(stats, "sweep_clipped")

    def e5_byte(offset: float) -> int:
        # E5xx is the note's fine tune.  One byte step is one KF step = 1/64 st
        # on the FM platforms, half that on PCM (E5 0x40 sits exactly -0.5 st
        # below 0x80), so trajectory offsets convert with a factor of two
        # there.  Values above 0xBF saturate.
        scale = 2.0 if ev.pcm else 1.0
        return max(0x40, min(0xBF, int(round(ev.e5 + offset * scale))))

    e5w = ev.e5
    slide = 0.0  # ramp accumulation in trajectory units
    xpose = 0.0  # quick-legato transpose applied via E8xx/E9xx (units)
    last_rate = 0
    last_k = 0
    k = 0
    while True:
        rs = base + k * row_len
        if rs >= end:
            break
        re_full = base + (k + 1) * row_len
        re = min(re_full, end)
        # A row carries one slide rate, so the driver's pitch steps that fall in
        # the same row collapse into it.  More merges per row = coarser grid.
        _bump(stats, "sweep_rate_merged",
              max(0, bisect_left(step_times, re_full) - bisect_left(step_times, rs) - 1))
        t_eff = start if k == 0 else rs
        if k == 0:
            # The note starts at its own pitch; the driver's first sweep step
            # (written ~2 ticks later) is covered by this row's ramp.  Snapping
            # it with E5 instead trades an early blip for the ramp's late lag;
            # dropping it keeps the attack exactly on the note's pitch.
            want = 0.0
        else:
            # Look one tick past the boundary for the step check: a driver jump
            # that lands just after the row start should still be snapped onto
            # this row, not smeared by the row's ramp.
            want = float(_traj_at(pts, int(rs + tick)))
        cur = (e5w - ev.e5) / (2.0 if ev.pcm else 1.0) + slide + xpose
        cell_k = grid[ev.ch].get(row0 + k)
        if abs(want - cur) >= 6:
            if cell_k is None or not (0 <= cell_k.note < 180):
                # A step is only recorded for rows this sweep owns: on a later
                # note's row the note's own E5 has to win (it sets the attack).
                e5w = e5_byte(want - slide - xpose)
                e5_writes[(ev.ch, row0 + k)] = e5w
                cur = (e5w - ev.e5) / (2.0 if ev.pcm else 1.0) + slide + xpose
            else:
                _bump(stats, "e5_skipped")
        # The physical rate runs the whole row even when the trajectory ends
        # inside it, so the last row must be rated over its full span against
        # the trajectory's held final value: rating it over the clipped span
        # made the pitch overshoot by several times the remaining travel, and
        # the E5 pin cannot fix more than +-1 semitone of that.
        is_last = re_full > end
        span = (re_full - t_eff) / tick if is_last else (re - t_eff) / tick
        if k == 0 and ev.pcm:
            # PCM note rows hold their pitch: Furnace applies a slice of the
            # row's 01xx/02xx travel inside the key-on write, and the attack
            # lands ~rate/128 semitones off.  The ramp starts on the next row
            # instead; the final E5 pin still absorbs the residue.
            want_end = 0.0
        else:
            want_end = float(_traj_at(pts, int(re_full if is_last else re)))
        rate = 0
        if span > 0:
            # FM: 01xx/02xx run 1 kc_kf unit per tick per unit of the
            # parameter, linearly, all the way to 255.  The old +-96 clamp
            # silently halved the steepest driver slides: a 2000-unit row
            # jump came out as a slow crawl.
            #
            # PCM: one parameter unit moves 1/128 st per tick, half the FM
            # rate.  The written parameter is doubled, and the ideal rate is
            # capped at 127.5 units/tick = the engine's 255 maximum, so
            # ``slide`` accumulates the movement the engine actually makes.
            limit = 127.5 if ev.pcm else 255.0
            # A move bigger than the slide can deliver inside this row lands
            # instantly as a quick-legato transpose (E8xx/E9xx, y semitones,
            # persistent until the next note; the next key-on resets it).  This
            # is what turns register jumps and dive attacks from a rate-limited
            # slur into the hardware's instant step; the slide then covers the
            # sub-semitone remainder.
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
                        # The FM platform adds the value to the note and stays
                        # until the next note, so the overflow is a relative
                        # transpose.
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
        param = int(round(rate * 2)) if ev.pcm else int(round(rate))
        param = max(-255, min(255, param))
        if param != last_rate or param != 0:
            # A slide only runs on rows that carry the effect: write the rate on
            # every row the ramp is active, not only when it changes, or the
            # pitch freezes after the first row of a constant-rate glide.
            cell = grid[ev.ch].get(row0 + k)
            if cell is None:
                cell = _blank_row()
                grid[ev.ch][row0 + k] = cell
            # Never leave a slide of the opposite direction next to the new
            # one: 01xx and 02xx are separate effect codes. Both would
            # survive on the row and the net slide becomes column-order luck.
            # The filter keeps the row's effect slots, or the next effect
            # would overwrite a live one instead of filling a free slot.
            cell.fx = [(c, v) for c, v in (cell.fx or [(EMPTY, EMPTY)] * fx_cols)
                       if c not in (0x01, 0x02)]
            if param:
                _add_fx(cell, 0x01 if param > 0 else 0x02, min(255, abs(param)), fx_cols, stats)
            else:
                _add_fx(cell, 0x01 if last_rate > 0 else 0x02, 0, fx_cols, stats)
            last_rate = param
        # Accrue the movement the engine actually makes - from the written,
        # rounded and clamped parameter - not the ideal rate.  A glide that
        # wants 2.66 units/tick is written as 01 03, so the engine runs ~13%
        # faster than the model believes and the error compounds over long
        # climbs.  Every FM/C140 slide carries the same fraction-of-a-unit
        # residue.
        engine_rate = (param / 2.0) if ev.pcm else float(param)
        # Fidelity: each row can only be a straight line, so sample the model
        # against the driver's own steps inside the row.  The deviation is the
        # audible error of condensing - a coarser grid packs more steps into one
        # row and bends them into one line.  Counted in 1/64 semitones; 6 units
        # is 0.1 st, the rough inaudibility floor.
        if stats is not None and span > 0:
            bad_here = False
            for j in range(4):
                t = int(rs + (re_full - rs) * (2 * j + 1) / 8.0)
                if t <= start:
                    continue
                model = cur + engine_rate * span * (2 * j + 1) / 8.0
                dev = abs(model - _traj_at(pts, t))
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

    # The rate keeps running past `end` to the last row's boundary; stop it on
    # the following row and pin the trajectory's final pitch with E5.
    #
    # The final row above was rated over its *full* span (is_last), so `slide`
    # already carries the ramp all the way to the row end: the pin only has to
    # correct the rounding residue.  Adding the tail's travel again (the old
    # `last_rate * (row_end - end) / tick` term) pulled the pitch down by that
    # travel a second time, leaving whole notes flat until the next key-on.
    #
    # A stop on a later note's own row is safe (the note's key-on still reads
    # its own pitch; the stop only kills the ramp) and it is the row that must
    # carry it when the driver's sweep runs into that note's row, or the ramp
    # keeps sliding under the new note.
    final = float(_traj_at(pts, int(end)))
    stop_row = row0 + last_k + 1
    if crossed:
        # A later note's row follows before the trajectory ends.  That note's
        # key-on reads its own pitch, so the running ramp may stay alive
        # through the last row this sweep owns; the stop goes on the later
        # note's row (a later sweep's own k=0 rate clears it again when it
        # takes that row over).  Freezing on the last row instead loses a
        # whole row of the tail's travel.
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
    """Row and delay for each PCM note-on that the pattern can play.

    A trigger whose row is free takes it. A later trigger on a taken row
    moves forward while the destination is still before the next trigger.
    When the next trigger already wants that row, the later of the two
    writes keeps the cell.
    """
    by_ch: dict[int, list] = {}
    for ev in song.events:
        if ev.on and ev.pcm and ev.ch >= 0:
            by_ch.setdefault(ev.ch, []).append(ev)
    limit = min(total_rows, song.row_of(last_sample) + 1)
    out: dict[int, tuple[int, int]] = {}
    for evs in by_ch.values():
        evs.sort(key=lambda e: e.sample)
        ideals = [song.place(e.sample, stats) for e in evs]
        placed: dict[int, int] = {}
        for i, ev in enumerate(evs):
            row, delay = ideals[i]
            if row < 0 or row >= total_rows:
                continue
            if row in placed and evs[placed[row]].sample == ev.sample:
                out.pop(id(evs[placed[row]]), None)
                _bump(stats, "pcm_evicted")
                placed[row] = i
                out[id(ev)] = (row, delay)
                continue
            next_row = ideals[i + 1][0] if i + 1 < len(evs) else limit
            if next_row <= row:
                next_row = row
            ceiling = min(limit, next_row)
            if row not in placed and row < limit:
                placed[row] = i
                out[id(ev)] = (row, delay)
                continue
            dest = row + 1
            found = False
            while dest < ceiling and dest < total_rows:
                if dest not in placed:
                    placed[dest] = i
                    out[id(ev)] = (dest, 0)
                    found = True
                    break
                dest += 1
            if found:
                continue
            if row >= total_rows:
                continue
            victim = placed.get(row)
            if victim is not None:
                out.pop(id(evs[victim]), None)
                _bump(stats, "pcm_evicted")
            placed[row] = i
            out[id(ev)] = (row, delay)
    return out


def _build_patterns(song: Song, n_ch: int, pat_len: int, fx_cols: int, ch_offset: int = 0,
                    stats: dict | None = None) -> tuple[
    list[list[int]], list[tuple[int, int, list[Row | None]]], int
]:
    """Return (orders[ch][ord], patterns as (ch, idx, rows), loop_order).

    Untouched rows stay `None`: most of a pattern is empty and the writers
    skip those without materializing a row object per slot.
    """
    if not song.events:
        orders = [[0] for _ in range(n_ch)]
        pats = [(ch, 0, [None] * pat_len) for ch in range(n_ch)]
        return orders, pats, 0

    last_sample = song.events[-1].sample if song.events else 0
    # a fade or chip write can run past the last note; the module has to carry
    # it or a voice keyed at zero (a fade-in) never gets its volume
    for ev in song.events:
        if ev.vol_pts:
            last_sample = max(last_sample, ev.vol_pts[-1][0])
    for entry in (getattr(song, "channel_fx", ()) or ()):
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
    # An adjusted loop ends on loop_end_sample. Content past that row is the
    # old tail; Bxx would skip it, so it is not written. `content_end` is the
    # absolute row Bxx sits on. The order walk below reuses the name end_row
    # for each order's bound, so the jump must keep this one.
    content_end = None
    content_cut = None
    if adjusted:
        content_end = song.place(song.loop_end_sample)[0]
        if content_end < loop_row:
            content_end = None
        else:
            # Notes at or past the wrap sample belong to the copy of the head
            # that follows the loop in the recording, however a condensed
            # grid would fold them back onto the wrap row.
            content_cut = song.loop_cut_sample
    if content_end is not None:
        total_rows = content_end + 1
    else:
        total_rows = max(1, song.row_of(last_sample) + 2)
        content_end = song.row_of(last_sample)

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

    grid: list[dict[int, Row]] = [dict() for _ in range(n_ch)]
    if stats is not None:
        stats["rows"] = max(stats.get("rows", 0), total_rows)

    for sample, cmd, val in getattr(song, "chip_fx", ()) or ():
        if content_cut is not None and sample >= content_cut:
            continue
        row = song.row_of(sample)
        if row < 0 or row >= total_rows:
            continue
        cell = grid[0].get(row)
        if cell is None:
            cell = _blank_row()
            grid[0][row] = cell
        _add_fx(cell, cmd, val, fx_cols, stats)

    for sample, ch, cmd, val in getattr(song, "channel_fx", ()) or ():
        ch += ch_offset
        if ch < 0 or ch >= n_ch:
            continue
        if content_cut is not None and sample >= content_cut:
            continue
        row = song.row_of(sample)
        if row < 0 or row >= total_rows:
            continue
        cell = grid[ch].get(row)
        if cell is None:
            cell = _blank_row()
            grid[ch][row] = cell
        _add_fx(cell, cmd, val, fx_cols, stats)

    # E5 is a channel state and both notes and sweep steps change it, so the
    # desired value is collected per row first and replayed in row order.
    # Writing each value only when it changes, including back to the neutral
    # 0x80, keeps a note's fine tune from leaking into the next one.
    note_e5: dict[tuple[int, int], int] = {}
    # Mid-note carrier-TL fades (the driver's software volume envelope).  The
    # OPM volume column only attenuates a carrier the log curve already scales
    # (VOL_SCALE_LOG: written TL = op.tl + (127 - vol)), so a fade-out survives
    # as a volume value relative to the instrument's own carrier TL.  The
    # column is channel state, so every FM note-on re-asserts 127 (the
    # instrument's TL) whenever any note in the track carries a fade.
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
        if ev.on and ev.pcm:
            assigned = pcm_pos.get(id(ev))
            if assigned is None:
                _bump(stats, "pcm_unplaced")
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
            if cell.note < 0:
                cell.note = NOTE_OFF
            else:
                _bump(stats, "off_swallowed")
            if delay:
                _add_fx(cell, 0xED, delay, fx_cols, stats)
            continue
        if 0 <= cell.note < 180:
            _bump(stats, "note_collision")
        cell.note = ev.note if ev.note != 180 else NOTE_OFF
        if ev.ins >= 0:
            cell.ins = ev.ins
        if ev.vol >= 0:
            cell.vol = ev.vol
        elif fm_tl and cell.vol == EMPTY:
            cell.vol = 127
        if delay:
            _add_fx(cell, 0xED, delay, fx_cols, stats)
        note_e5[(ev_ch, row)] = ev.e5
        # PCM pan 0x00 is both channels off. Omitting it leaves Furnace's
        # default (both full). FM pan 0 still means "no 08xx on this row".
        if (ev.pcm or ev.pan) and not ev.no_pan:
            _add_fx(cell, 0x08, ev.pan & 0xFF, fx_cols, stats)

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

    # Pitch sweeps (percussion): after every event is placed so the ramps can
    # share the note-off rows (and their ED delays).  The sweep may step E5 on
    # its own rows (jump correction), recorded alongside the note values.
    # Every note row is known before this pass and sweeps only add effects,
    # so each channel's note rows are listed once for the crossing checks.
    note_rows: dict[int, list[int]] = {
        ch: sorted(r for r, cell in grid[ch].items() if 0 <= cell.note < 180)
        for ch in range(n_ch)
    }
    sweep_e5: dict[tuple[int, int], int] = {}
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

    # C140 volume trajectories (fades): the driver steps the voice's volume
    # registers and Furnace feeds the column into the chip gain live
    # (platform/c140.cpp chVol = outVol*pan/255).  Balance steps ride along as
    # 08xx, written only when the pair changes.  Only cells without a note are
    # written, so a note's own volume always wins on its row.
    if song.events:
        for ev in song.events:
            ev_ch = ev.ch + ch_offset
            if not (ev.on and ev.vol_pts) or ev_ch < 0 or ev_ch >= n_ch:
                continue
            last_pan = None if ev.no_pan else ev.pan
            for sample, value, pan in ev.vol_pts:
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
                cell.vol = value
                if pan is not None and (last_pan is None or pan != last_pan):
                    _add_fx(cell, 0x08, pan & 0xFF, fx_cols, stats)
                    last_pan = pan

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
            # Loop jump on the last row that carries content, not on the last
            # row of the padded order: the final order is only as long as the
            # music, and a Bxx on the padded end would insert silence before
            # every loop restart.
            # Jingles with no VGM loop must not Bxx, or they restart forever.
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

    return orders, patterns, loop_order


def _fit_fx_cols(patterns: list[tuple[int, int, list[Row | None]]], n_ch: int) -> list[int]:
    """Effect columns per channel: one per effect the busiest row uses, 1..8.

    Furnace sizes a pattern column from its effect column count, so this is
    the fit-to-content knob (see docs/fur-writer.md). Everything a channel
    plays stays visible and the column is never wider than its densest row.
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


def write_fur(
    song: Song, *, include_pcm: bool = True, include_fm: bool = True,
    stats: dict | None = None,
) -> bytes:
    gd3 = song.vgm.gd3
    fm_ins = song.fm_patches if include_fm else []
    pcm_smps = [s for s in song.pcm_samples if s.length >= 8] if include_pcm else []
    oki_smps = list(song.oki_samples) if include_pcm else []
    adpcm_smps = list(song.msm6258_samples) if include_pcm else []
    spcm_smps = [s for s in song.segapcm_samples if s.length >= 8] if include_pcm else []
    c140_smps = [s for s in song.c140_samples if s.length >= 8] if include_pcm else []
    c352_smps = [s for s in song.c352_samples if s.length >= 8] if include_pcm else []
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
    # A PCM module with no sample chip of its own keeps the K007232 pair, so
    # events already written on channels 8-9 still have a chip under them.
    want_k007 = bool(pcm_smps) or (
        include_pcm and not oki_ids and not adpcm_ids
        and not spcm_smps and not c140_ids and not c352_ids
    )
    n_fm = 8 if include_fm else 0
    n_k007 = 2 if want_k007 else 0
    n_ch = (
        n_fm + n_k007 + 4 * len(oki_ids) + len(adpcm_ids)
        + (16 if spcm_smps else 0) + 24 * len(c140_ids) + 32 * len(c352_ids)
    )
    if n_ch == 0:
        raise ValueError("write_fur: need include_fm and/or include_pcm")
    pat_len = 256
    fx_cols = 8  # room for LFO 17/18/1E/1F plus pan/E5/ED/Bxx
    k_clock = song.vgm.k007232_clock or 3579545
    spcm_clock = song.vgm.segapcm_clock or SEGAPCM_CLOCK_X
    c140_clock = song.vgm.c140_clock or 12288000
    c352_clock = song.vgm.c352_clock or C352_DEFAULT_CLOCK

    n_ins = (
        len(fm_ins) + len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(c140_smps) + len(c352_smps)
    )
    n_smp = (
        len(pcm_smps) + len(oki_smps) + len(adpcm_smps)
        + len(spcm_smps) + len(c140_smps) + len(c352_smps)
    )

    # Analysis numbers sample-chip channels after the 8-channel FM block. With
    # no FM those channels shift down so they still land inside n_ch.
    orders, patterns, _loop_ord = _build_patterns(
        song, n_ch, pat_len, fx_cols, 0 if include_fm else -8, stats=stats
    )
    effect_cols = _fit_fx_cols(patterns, n_ch)
    n_ord = len(orders[0]) if orders else 1
    n_pat = len(patterns)

    names_long: list[str] = []
    names_short: list[str] = []
    if n_fm:
        names_long += [f"FM {i + 1}" for i in range(n_fm)]
        names_short += [f"F{i + 1}" for i in range(n_fm)]
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

    label_parts: list[str] = []
    if include_fm:
        label_parts.append("YM2151")
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

    b = Buf()
    b.raw(FUR_MAGIC)
    b.u16(FUR_VERSION)
    b.u16(0)
    b.u32(32)
    b.pad(8)

    chips: list[int] = []
    flag_texts: list[str] = []
    if include_fm:
        chips.append(CHIP_YM2151)
        fm_clock = song.vgm.ym2151_clock
        # Furnace's YM2151 default is 3.579545 MHz. An OPM at another clock
        # plays flat unless the flag carries customClock.
        flag_texts.append(
            f"customClock={fm_clock}" if fm_clock and fm_clock != 3579545 else ""
        )
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
        flag_texts.append(c352_flag_text(song.vgm.c352_clock))
    n_chips = len(chips)

    info_size_off, info_start = b.begin_block(b"INFO")
    b.u8(0)  # timebase stored as value-1; 0 → timebase 1
    b.u8(song.speed)
    b.u8(song.speed)
    b.u8(1)
    b.f32(float(song.hz))
    b.u16(pat_len)
    b.u16(n_ord)
    b.u8(4)
    b.u8(16)
    b.u16(n_ins)
    b.u16(0)
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
        # entries) through _sample_chip_volume; FM keeps the reference unity.
        if chip == CHIP_K007232 and include_fm:
            cv = k_vol
        elif chip in _CHIP_DEV:
            cv = _sample_chip_volume(song, chip, chip_counts[chip])
        else:
            cv = 1.0
        vols[i] = max(0, min(64, int(round(cv * 64))))
        chip_vols.append(cv)
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
    b.f32(1.0)
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

    # extra chip output (>=135), one (vol, panL, panR) triple per chip
    for cv in chip_vols:
        b.f32(cv)
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
        _write_ins_opm(b, patch.name or f"OPM {i:02d}", patch)

    for i, smp in enumerate(pcm_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(b, f"PCM {_pcm_label(smp, i)}", i)
    for i, smp in enumerate(oki_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(b, smp.name, len(pcm_smps) + i, INS_MSM6295)
    for i, smp in enumerate(adpcm_smps):
        ins_ptrs.append(len(b))
        _write_ins_pcm(
            b, smp.name, len(pcm_smps) + len(oki_smps) + i, INS_MSM6258,
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
        _write_vox(b, smp.name, smp.data, smp.rate)
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

    pat_ptrs: list[int] = []
    for ch, idx, rows in patterns:
        pat_ptrs.append(len(b))
        _write_patn(b, ch, idx, rows, fx_cols)

    for i, p in enumerate(ins_ptrs):
        b.patch_u32(ins_ptr_off + 4 * i, p)
    for i, p in enumerate(smp_ptrs):
        b.patch_u32(smp_ptr_off + 4 * i, p)
    for i, p in enumerate(pat_ptrs):
        b.patch_u32(pat_ptr_off + 4 * i, p)

    return bytes(b.b)


def parse_fur_skeleton(data: bytes) -> dict:
    """Walk a .fur we wrote and return header facts (for tests / sanity)."""
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
