"""Edit .fur chip (system) volumes in place.

Furnace stores one volume per chip, not per channel. An old INFO block keeps
the value twice: a byte per chip (64 = unity) that old readers use, and a
float triple behind the sample data (vol, panL, panR) that 0.6 reads. INF2
keeps only the floats. Both copies are patched together. Every reader
agrees.

Volume above unity cannot ride in the byte field. INFO files cap at 1.0.
INF2 accepts up to 4.0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from vgm2151fur.furwrite import (
    CHIP_C140, CHIP_C352, CHIP_K007232, CHIP_MSM6258, CHIP_MSM6295, CHIP_SEGAPCM,
    FUR_MAGIC,
)

CHIP_NAMES = {
    0x82: "YM2151",
    CHIP_MSM6295: "MSM6295",
    CHIP_MSM6258: "MSM6258",
    CHIP_K007232: "K007232",
    CHIP_SEGAPCM: "SegaPCM",
    CHIP_C140: "C140",
    CHIP_C352: "C352",
}

# Old readers take the byte field as a signed char. It stays <= 64.
MAX_INFO_VOLUME = 1.0
MAX_INF2_VOLUME = 4.0


class FurEditError(Exception):
    """The file carries neither an INFO nor an INF2 block."""


@dataclass
class Chip:
    index: int
    id: int
    volume: float
    pan: float = 0.0
    pan_fr: float = 0.0

    @property
    def name(self) -> str:
        return CHIP_NAMES.get(self.id, f"0x{self.id:02x}")


def _cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def _block(data: bytes, magic: bytes) -> tuple[int, int]:
    """(payload offset, payload size) of the INFO or INF2 block."""
    if len(data) < len(FUR_MAGIC) + 12:
        raise FurEditError("not a Furnace module")
    ptr = struct.unpack_from("<I", data, len(FUR_MAGIC) + 4)[0]
    if data[ptr:ptr + 4] != magic:
        ptr = data.find(magic)
        if ptr < 0:
            raise FurEditError(f"no {magic.decode()} block")
    return ptr + 8, struct.unpack_from("<I", data, ptr + 4)[0]


def _info_triples(data: bytes, payload: int, payload_end: int, n_chips: int) -> int | None:
    """Offset of the float triples, or None when the tail does not check out.

    The payload tail is [triples][patchbay 4][auto 1][pad 8][speed 17][groove 1]
    followed by three asset-directory pointers. Anchor on those: the first has
    to resolve to an ADIR block.
    """
    for off in range(payload_end - 12, max(payload, payload_end - 4096) - 1, -4):
        ptr = struct.unpack_from("<I", data, off)[0]
        if 0 < ptr <= len(data) - 4 and data[ptr:ptr + 4] == b"ADIR":
            triples = off - 31 - 12 * n_chips
            if triples >= payload:
                return triples
            return None
    return None


def _read_info(data: bytes) -> tuple[list[Chip], int, int]:
    """Chips plus (byte-volume offset, triples offset) for an old INFO block."""
    payload, size = _block(data, b"INFO")
    payload_end = payload + size
    n_chips = 0
    for i in range(32):
        if data[payload + 24 + i]:
            n_chips = i + 1
    if n_chips < 1:
        raise FurEditError("INFO block declares no chips")
    vol_off = payload + 56
    triples = _info_triples(data, payload, payload_end, n_chips)
    chips = []
    for i in range(n_chips):
        chip_id = data[payload + 24 + i]
        legacy = data[vol_off + i]
        if triples is not None:
            volume, pan, pan_fr = struct.unpack_from("<fff", data, triples + 12 * i)
        else:
            volume, pan, pan_fr = legacy / 64.0, 0.0, 0.0
        chips.append(Chip(i, chip_id, volume, pan, pan_fr))
    return chips, vol_off, triples if triples is not None else -1


def _read_inf2(data: bytes) -> tuple[list[Chip], int]:
    """Chips from an INF2 header plus the offset of the first volume float."""
    payload, _size = _block(data, b"INF2")
    pos = payload
    for _ in range(8):
        _s, pos = _cstring(data, pos)
    pos += 4 + 1 + 4 + 2 + 2  # tuning, autoSystem, master vol, chans, systems
    chips = []
    first = None
    for i in range(32):
        if pos + 16 > len(data):
            break
        chip_id = struct.unpack_from("<H", data, pos)[0]
        if chip_id == 0:
            break
        if first is None:
            first = pos + 4
        volume, pan, pan_fr = struct.unpack_from("<fff", data, pos + 4)
        chips.append(Chip(i, chip_id, volume, pan, pan_fr))
        pos += 16
    if not chips or first is None:
        raise FurEditError("INF2 block declares no chips")
    return chips, first


def read_chips(data: bytes) -> list[Chip]:
    if b"INF2" in data[:0x2000]:
        try:
            return _read_inf2(data)[0]
        except (FurEditError, struct.error, ValueError):
            pass
    return _read_info(data)[0]


def read_chips_file(path: Path) -> list[Chip]:
    return read_chips(Path(path).read_bytes())


def set_chip_volume(
    data: bytes,
    *,
    factor: float | None = None,
    value: float | None = None,
    targets: list[int] | None = None,
) -> tuple[bytes, list[Chip]]:
    """Scale or set chip volumes. Returns the patched bytes and the new chips.

    `targets` holds chip indexes as listed by read_chips. None means every
    chip. Give exactly one of `factor` and `value`.
    """
    if (factor is None) == (value is None):
        raise ValueError("give exactly one of factor and value")
    if b"INF2" in data[:0x2000]:
        chips, first = _read_inf2(data)
        limit = MAX_INF2_VOLUME
        vol_off = None
    else:
        chips, vol_off, triples = _read_info(data)
        if triples < 0:
            raise FurEditError("INFO tail not recognised, refusing to patch volumes")
        first = triples
        limit = MAX_INFO_VOLUME
    buf = bytearray(data)
    chosen = set(range(len(chips))) if targets is None else set(targets)
    for chip in chips:
        if chip.index not in chosen:
            continue
        new = value if value is not None else chip.volume * factor
        new = max(0.0, min(limit, new))
        struct.pack_into("<f", buf, first + 12 * chip.index, new)
        if vol_off is not None:
            buf[vol_off + chip.index] = max(0, min(64, int(round(new * 64))))
    return bytes(buf), read_chips(bytes(buf))


def set_chip_volume_file(
    path: Path,
    *,
    factor: float | None = None,
    value: float | None = None,
    targets: list[int] | None = None,
    dry_run: bool = False,
) -> list[Chip]:
    path = Path(path)
    data = path.read_bytes()
    patched, chips = set_chip_volume(data, factor=factor, value=value, targets=targets)
    if not dry_run and patched != data:
        path.write_bytes(patched)
    return chips
