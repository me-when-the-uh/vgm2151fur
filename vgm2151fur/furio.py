"""Read Furnace .fur files: blocks, sample metadata, instruments, patterns.

Works on vgm2151fur format-157 modules and on files Furnace re-saves
(INFO + SMP2 + PATN, optionally zlib-wrapped). INF2 files read too.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from vgm2151fur.furwrite import (
    CHIP_C140, CHIP_C352, CHIP_K007232, CHIP_MSM6258, CHIP_MSM6295, CHIP_SEGAPCM,
    FUR_MAGIC,
)


CHIP_CHANNELS = {
    0x82: 8,  # YM2151
    CHIP_MSM6295: 4,  # MSM6295
    CHIP_MSM6258: 1,  # MSM6258
    CHIP_K007232: 2,  # K007232
    CHIP_SEGAPCM: 16,  # SegaPCM
    CHIP_C140: 24,  # Namco C140
    CHIP_C352: 32,  # Namco C352
    0xBB: 8,  # Namco C30 WSG
    0xCF: 16,  # Namco C219
}

# Chips whose samples carry playable 8-bit bodies (VOX depths stay out).
_PCM_BODY_CHIPS = (CHIP_K007232, CHIP_SEGAPCM, CHIP_C140, CHIP_C352)


def load_fur_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:16] == FUR_MAGIC:
        return raw
    try:
        dec = zlib.decompress(raw)
    except zlib.error as exc:
        raise ValueError(f"{path.name}: not a Furnace module") from exc
    if dec[:16] != FUR_MAGIC:
        raise ValueError(f"{path.name}: not a Furnace module")
    return dec


def iter_furs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = list(path.glob("*.fur")) + list(path.glob("*.FUR"))
    seen: set[str] = set()
    out: list[Path] = []
    for p in sorted(files, key=lambda x: x.name.lower()):
        k = str(p.resolve()).lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out

def _read_strz(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end < 0:
        return data[pos:].decode("utf-8", "replace"), len(data)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def iter_blocks(data: bytes):
    if data[:16] != FUR_MAGIC:
        raise ValueError("not a Furnace module")
    info_ptr = struct.unpack_from("<I", data, 20)[0]
    pos = info_ptr
    while pos + 8 <= len(data):
        mag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if size < 0 or pos + 8 + size > len(data) + 8:
            break
        payload = data[pos + 8:pos + 8 + size]
        yield pos, mag, size, payload
        pos += 8 + size


@dataclass
class FurSample:
    index: int
    name: str
    file_off: int
    block_size: int  # magic + size field + payload
    n_frames: int
    compat_rate: int
    c4_rate: int
    depth: int
    loop_mode: int
    flags: int
    flags2: int
    loop_start: int
    loop_end: int
    presence: bytes
    data: bytes


def parse_smp2_payload(payload: bytes) -> dict:
    name, pos = _read_strz(payload, 0)
    n_frames, compat, c4 = struct.unpack_from("<III", payload, pos)
    pos += 12
    depth, loop_mode, flags, flags2 = payload[pos:pos + 4]
    pos += 4
    loop_start, loop_end = struct.unpack_from("<ii", payload, pos)
    pos += 8
    presence = payload[pos:pos + 16]
    pos += 16
    width = 2 if depth == 16 else 1
    nbytes = n_frames * width
    pcm = payload[pos:pos + nbytes]
    return {
        "name": name,
        "n_frames": n_frames,
        "compat_rate": compat,
        "c4_rate": c4,
        "depth": depth,
        "loop_mode": loop_mode,
        "flags": flags,
        "flags2": flags2,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "presence": presence,
        "data": pcm,
    }


def list_fur_samples(data: bytes) -> list[FurSample]:
    out: list[FurSample] = []
    i = 0
    for off, mag, size, payload in iter_blocks(data):
        if mag != b"SMP2":
            continue
        p = parse_smp2_payload(payload)
        out.append(
            FurSample(
                index=i,
                name=p["name"],
                file_off=off,
                block_size=8 + size,
                n_frames=p["n_frames"],
                compat_rate=p["compat_rate"],
                c4_rate=p["c4_rate"],
                depth=p["depth"],
                loop_mode=p["loop_mode"],
                flags=p["flags"],
                flags2=p["flags2"],
                loop_start=p["loop_start"],
                loop_end=p["loop_end"],
                presence=p["presence"],
                data=p["data"],
            )
        )
        i += 1
    return out

@dataclass
class FurRow:
    note: int = -1
    ins: int = -1
    vol: int = -1
    fx: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class FurModule:
    version: int
    speed: int
    hz: float
    pat_len: int
    n_ord: int
    n_ch: int
    chips: list[int]
    pcm_channels: list[int]
    orders: list[list[int]]
    samples: list[FurSample]
    ins_sample: list[int]
    patterns: dict[tuple[int, int], list[FurRow]]
    name: str = ""
    effect_cols: list[int] = field(default_factory=list)
    master_vol: float = 1.0


def _decode_patn_rows(block: bytes, version: int) -> tuple[int, int, list[FurRow]]:
    pos = 0
    _subsong = block[pos]
    pos += 1
    if version >= 240:
        ch = struct.unpack_from("<H", block, pos)[0]
        pos += 2
    else:
        ch = block[pos]
        pos += 1
    idx = struct.unpack_from("<H", block, pos)[0]
    pos += 2
    _name, pos = _read_strz(block, pos)
    rows: list[FurRow] = []
    while pos < len(block):
        mask = block[pos]
        pos += 1
        if mask == 0xFF:
            break
        if mask == 0:
            rows.append(FurRow())
            continue
        if mask & 0x80:
            skip = (mask & 0x7F) + 2
            rows.extend(FurRow() for _ in range(skip))
            continue
        extra = 0
        if mask & 0x20:
            extra |= block[pos]
            pos += 1
        if mask & 0x40:
            extra |= block[pos] << 8
            pos += 1
        effect_mask = extra
        if mask & 0x08:
            effect_mask |= 1
        if mask & 0x10:
            effect_mask |= 2
        row = FurRow()
        if mask & 1:
            row.note = block[pos]
            pos += 1
        if mask & 2:
            row.ins = block[pos]
            pos += 1
        if mask & 4:
            row.vol = block[pos]
            pos += 1
        fx_bytes: list[int] = []
        for bit in range(16):
            if effect_mask & (1 << bit):
                fx_bytes.append(block[pos])
                pos += 1
        for col in range(0, len(fx_bytes) - 1, 2):
            row.fx.append((fx_bytes[col], fx_bytes[col + 1]))
        rows.append(row)
    return ch, idx, rows


def _parse_ins_sample_index(payload: bytes) -> int:
    """INS2: walk NA/SM/FM/MA features for the SM sample index. -1 if none."""
    if len(payload) < 4:
        return -1
    pos = 4  # version + type
    while pos + 2 <= len(payload):
        feat = payload[pos:pos + 2]
        if feat == b"EN":
            break
        if pos + 4 > len(payload):
            break
        size = struct.unpack_from("<H", payload, pos + 2)[0]
        body = pos + 4
        if feat == b"SM" and size >= 2:
            return struct.unpack_from("<H", payload, body)[0]
        pos = body + size
    return -1


def _n_ch_from_chips(chips: list[int]) -> int:
    n = 0
    for c in chips:
        if not c:
            continue
        n += CHIP_CHANNELS.get(c, 1)
    return n


def _pcm_channels(chips: list[int]) -> list[int]:
    ch = 0
    pcm: list[int] = []
    for c in chips:
        if not c:
            continue
        n = CHIP_CHANNELS.get(c, 1)
        if c in _PCM_BODY_CHIPS:
            pcm.extend(range(ch, ch + n))
        ch += n
    if not pcm and chips:
        # PCM-only files still list K007232; if chips were stripped, last 2.
        n_ch = _n_ch_from_chips(chips)
        pcm = list(range(max(0, n_ch - 2), n_ch))
    return pcm


def parse_fur(data: bytes) -> FurModule:
    ver = struct.unpack_from("<H", data, 16)[0]
    info_ptr = struct.unpack_from("<I", data, 20)[0]
    mag = data[info_ptr:info_ptr + 4]
    if mag not in (b"INFO", b"INF2"):
        raise ValueError("INFO/INF2 missing")
    size = struct.unpack_from("<I", data, info_ptr + 4)[0]
    payload = data[info_ptr + 8:info_ptr + 8 + size]

    samples = list_fur_samples(data)
    ins_sample: list[int] = []
    patterns: dict[tuple[int, int], list[FurRow]] = {}
    for _off, bmag, _sz, block in iter_blocks(data):
        if bmag == b"INS2":
            ins_sample.append(_parse_ins_sample_index(block))
        elif bmag == b"PATN":
            ch, idx, rows = _decode_patn_rows(block, ver)
            patterns[(ch, idx)] = rows

    if mag == b"INF2":
        return _parse_inf2(ver, payload, samples, ins_sample, patterns, data)
    return _parse_info(ver, payload, samples, ins_sample, patterns)


def _info_walk(payload: bytes, ver: int) -> dict:
    """Walk an INFO payload to its channel state and master volume.

    `master_off` is payload-relative (Furnace stores the song volume after
    the comment string); -1 when the version predates it.
    """
    speed = payload[1] or 1
    hz = struct.unpack_from("<f", payload, 4)[0] or 60.0
    pat_len = struct.unpack_from("<H", payload, 8)[0] or 64
    n_ord = struct.unpack_from("<H", payload, 10)[0] or 1
    n_ins = struct.unpack_from("<H", payload, 14)[0]
    n_wav = struct.unpack_from("<H", payload, 16)[0]
    n_smp = struct.unpack_from("<H", payload, 18)[0]
    n_pat = struct.unpack_from("<I", payload, 20)[0]
    chips = [c for c in payload[24:56] if c]
    n_ch = _n_ch_from_chips(chips) or 2
    pos = 24 + 32 + 32 + 32 + 128
    name, pos = _read_strz(payload, pos)
    _author, pos = _read_strz(payload, pos)
    pos += 4  # tuning
    pos += 20  # compat phase 1
    pos += (n_ins + n_wav + n_smp) * 4
    pos += n_pat * 4
    orders: list[list[int]] = []
    for _ch in range(n_ch):
        orders.append(list(payload[pos:pos + n_ord]))
        pos += n_ord
    effect_cols = list(payload[pos:pos + n_ch])
    pos += n_ch
    master_vol, master_off = 1.0, -1
    if ver >= 39:
        pos += 2 * n_ch  # channel show + collapse bytes
        for _ in range(2 * n_ch):  # long and short names
            _s, pos = _read_strz(payload, pos)
        _notes, pos = _read_strz(payload, pos)
        if ver >= 59 and pos + 4 <= len(payload):
            master_vol = struct.unpack_from("<f", payload, pos)[0]
            master_off = pos
    return {
        "speed": speed, "hz": hz, "pat_len": pat_len, "n_ord": n_ord,
        "n_ch": n_ch, "chips": chips, "orders": orders,
        "effect_cols": effect_cols, "name": name,
        "master_vol": master_vol, "master_off": master_off,
    }


def _parse_info(
    ver: int,
    payload: bytes,
    samples: list[FurSample],
    ins_sample: list[int],
    patterns: dict[tuple[int, int], list[FurRow]],
) -> FurModule:
    w = _info_walk(payload, ver)
    return FurModule(
        version=ver,
        speed=max(1, w["speed"]),
        hz=w["hz"],
        pat_len=w["pat_len"],
        n_ord=w["n_ord"],
        n_ch=w["n_ch"],
        chips=w["chips"],
        pcm_channels=_pcm_channels(w["chips"]),
        orders=w["orders"],
        samples=samples,
        ins_sample=ins_sample,
        patterns=patterns,
        name=w["name"],
        effect_cols=w["effect_cols"],
        master_vol=w["master_vol"],
    )


def _parse_inf2(
    ver: int,
    payload: bytes,
    samples: list[FurSample],
    ins_sample: list[int],
    patterns: dict[tuple[int, int], list[FurRow]],
    data: bytes,
) -> FurModule:
    pos = 0
    for _ in range(8):
        _s, pos = _read_strz(payload, pos)
    pos += 4  # tuning
    pos += 1  # auto system name
    master_vol = struct.unpack_from("<f", payload, pos)[0]
    pos += 4  # master vol
    n_ch = struct.unpack_from("<H", payload, pos)[0]
    pos += 2
    n_chips = struct.unpack_from("<H", payload, pos)[0]
    pos += 2
    chips: list[int] = []
    for _ in range(n_chips):
        cid = struct.unpack_from("<H", payload, pos)[0]
        pos += 2
        _cc = struct.unpack_from("<H", payload, pos)[0]
        pos += 2
        pos += 12  # vol, pan, rear
        chips.append(cid)
    n_conn = struct.unpack_from("<I", payload, pos)[0]
    pos += 4 + 4 * n_conn
    pos += 1  # auto patchbay
    sng2_off = None
    while pos < len(payload):
        etype = payload[pos]
        pos += 1
        if etype == 0:
            break
        n_el = struct.unpack_from("<I", payload, pos)[0]
        pos += 4
        ptrs = [struct.unpack_from("<I", payload, pos + 4 * i)[0] for i in range(n_el)]
        pos += 4 * n_el
        if etype == 1 and ptrs:
            sng2_off = ptrs[0]
    speed, hz, pat_len, n_ord, orders = 6, 60.0, 64, 1, []
    name = ""
    effect_cols: list[int] = []
    if sng2_off is not None and data[sng2_off:sng2_off + 4] == b"SNG2":
        sz = struct.unpack_from("<I", data, sng2_off + 4)[0]
        sng = data[sng2_off + 8:sng2_off + 8 + sz]
        hz = struct.unpack_from("<f", sng, 0)[0] or 60.0
        pat_len = struct.unpack_from("<H", sng, 6)[0] or 64
        n_ord = struct.unpack_from("<H", sng, 8)[0] or 1
        sp_len = sng[16]
        # speed pattern starts at 17, 16 unsigned shorts
        speed = struct.unpack_from("<H", sng, 17)[0] or 1
        p = 17 + 32
        name, p = _read_strz(sng, p)
        _cmt, p = _read_strz(sng, p)
        for _ch in range(n_ch):
            orders.append(list(sng[p:p + n_ord]))
            p += n_ord
        effect_cols = list(sng[p:p + n_ch])
        _ = sp_len
    if not orders:
        raise ValueError("INF2 module has no SNG2 orders (cannot render)")
    return FurModule(
        version=ver,
        speed=max(1, speed),
        hz=hz,
        pat_len=pat_len,
        n_ord=n_ord,
        n_ch=n_ch or _n_ch_from_chips(chips),
        chips=chips,
        pcm_channels=_pcm_channels(chips) or list(range(max(0, n_ch - 2), n_ch)),
        orders=orders,
        samples=samples,
        ins_sample=ins_sample,
        patterns=patterns,
        name=name,
        effect_cols=effect_cols,
        master_vol=master_vol,
    )


def _block(data: bytes) -> tuple[bytes, bytes, int]:
    """(magic, payload, payload offset) of the INFO or INF2 block."""
    info_ptr = struct.unpack_from("<I", data, 20)[0]
    mag = data[info_ptr:info_ptr + 4]
    if mag not in (b"INFO", b"INF2"):
        raise ValueError("INFO/INF2 missing")
    size = struct.unpack_from("<I", data, info_ptr + 4)[0]
    return mag, data[info_ptr + 8:info_ptr + 8 + size], info_ptr + 8


def _inf2_master_off(payload: bytes) -> int:
    pos = 0
    for _ in range(8):
        _s, pos = _read_strz(payload, pos)
    return pos + 5  # tuning f32 + auto system name byte


def master_volume(data: bytes) -> float:
    """The song master volume (what Furnace shows as the module volume)."""
    ver = struct.unpack_from("<H", data, 16)[0]
    mag, payload, _off = _block(data)
    if mag == b"INF2":
        return struct.unpack_from("<f", payload, _inf2_master_off(payload))[0]
    return _info_walk(payload, ver)["master_vol"]


def set_master_volume(data: bytes, volume: float) -> bytes:
    """Patch the master volume in place. Both formats keep it as one f32."""
    ver = struct.unpack_from("<H", data, 16)[0]
    mag, payload, off = _block(data)
    if mag == b"INF2":
        off += _inf2_master_off(payload)
    else:
        master_off = _info_walk(payload, ver)["master_off"]
        if master_off < 0:
            raise ValueError("INFO block does not carry a master volume")
        off += master_off
    buf = bytearray(data)
    struct.pack_into("<f", buf, off, float(volume))
    return bytes(buf)


def fx_pan_amps(val: int) -> tuple[float, float]:
    """08xy to (left, right). x and y are independent levels, 0-F.

    Furnace 0.6.8 effect 0x08. 0xFF is both full, 0xF0 is left, 0x0F is
    right, 0x80 is left at 8/15 and silent on the right.
    """
    v = val & 0xFF
    return ((v >> 4) & 15) / 15.0, (v & 15) / 15.0

