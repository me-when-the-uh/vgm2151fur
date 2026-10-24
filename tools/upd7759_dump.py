"""Dump a uPD7759 stand-alone voice ROM: one WAV per phrase + an index.

    python tools/upd7759_dump.py <rom file or MAME zip> [--out DIR]

Reads the ROM from a raw .bin/.rom file, or from a MAME .zip: without
--member the member whose header carries the 5a a5 69 55 magic is picked.
Writes voice_XX[_codeYY].wav for every phrase plus upd7759_index.json.

--code-base is the sound-latch value the driver adds phrase numbers to
(Konami Twin16: 0x70 -> phrase 0, so pass --code-base 0x70). It only shows
up in the file names and the index.

    python tools/upd7759_dump.py ../../mame/roms/gradius2.zip \
        --out ../../output/other/gradius2/upd7759 --code-base 0x70
    python tools/upd7759_dump.py voice_rom.bin --list
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vgm2151fur.upd7759 import (  # noqa: E402
    MAGIC, STANDARD_CLOCK, decode_phrase, dump_rom, phrase_count,
)


def load_rom(path: Path, member: str | None) -> tuple[bytes, str]:
    """ROM bytes and a label; a zip is searched for the magic when needed."""
    if path.suffix.lower() not in (".zip", ".7z"):
        return path.read_bytes(), path.name
    if path.suffix.lower() == ".7z":
        sys.exit("7z archives are not supported; use the plain ROM or a .zip")
    with zipfile.ZipFile(path) as archive:
        if member:
            return archive.read(member), f"{path.name}:{member}"
        matches = [info.filename for info in archive.infolist()
                   if not info.is_dir() and archive.read(info.filename)[1:5] == MAGIC]
        if not matches:
            sys.exit(f"{path.name}: no member starts with the uPD7759 header")
        if len(matches) > 1:
            sys.exit(f"{path.name}: several members match (--member NAME): {', '.join(matches)}")
        return archive.read(matches[0]), f"{path.name}:{matches[0]}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", help="raw uPD7759 ROM or MAME .zip with one in it")
    ap.add_argument("--member", help="zip member to read (default: the one with the magic)")
    ap.add_argument("--out", help="output folder (default: <rom>_upd7759 beside the input)")
    ap.add_argument("--rate", type=int, default=44100, help="WAV sample rate (default 44100)")
    ap.add_argument("--clock", type=lambda s: int(s, 0), default=STANDARD_CLOCK,
                    help=f"chip clock in Hz (default {STANDARD_CLOCK})")
    ap.add_argument("--code-base", type=lambda s: int(s, 0), default=None,
                    help="sound-latch code of phrase 0; names files/codes (e.g. 0x70)")
    ap.add_argument("--peak", type=int, default=None,
                    help="scale the set so its loudest sample sits at this 16-bit peak "
                         "(default: keep full scale headroom, gain 1 or 250/peak)")
    ap.add_argument("--list", action="store_true", help="print the phrase table, write nothing")
    args = ap.parse_args()

    rom_path = Path(args.rom)
    if not rom_path.is_file():
        sys.exit(f"no such file: {rom_path}")
    rom, label = load_rom(rom_path, args.member)
    count = phrase_count(rom)
    print(f"{label}: {len(rom)} bytes, {count} phrases, clock {args.clock}")

    if args.list:
        for i in range(count):
            phrase = decode_phrase(rom, i, args.clock)
            code = "" if args.code_base is None else f"code 0x{args.code_base + i:02X}  "
            end = "running off the ROM end" if phrase.end is None else f"end 0x{phrase.end:05x}"
            print(f"  {i:2d}  {code}start 0x{phrase.start:05x}  {phrase.seconds(args.clock):6.2f}s"
                  f"  {phrase.blocks:3d} blocks  rates {sorted(phrase.rates)}  {end}")
        return 0

    out_dir = Path(args.out) if args.out else rom_path.parent / f"{rom_path.stem}_upd7759"
    index = dump_rom(rom, out_dir, source=label, rate=args.rate, clock=args.clock,
                     code_base=args.code_base, target_peak=args.peak)
    total = sum(item["seconds"] for item in index["items"])
    print(f"wrote {len(index['items'])} WAV(s) to {out_dir}")
    print(f"total {total:.2f}s at {index['rate']} Hz, gain {index['gain']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
