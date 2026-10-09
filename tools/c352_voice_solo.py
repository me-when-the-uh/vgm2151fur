"""Per-voice isolation: which voice owns a problem window, and who loses it.

    python tools/c352_voice_solo.py <source.vgz|vgm> <t0> <t1> <outdir> <voice> [voice...]
                                    [--probe A B]

For every voice it builds a minimal one-voice VGM covering [t0, t1] (all
strobes plus that voice's registers, plus a tail volume write so the module
covers the window - without an event the module simply ends), renders it
with the reference player, converts it to a .fur and renders that through
Furnace. The printed table is the band energy in the probe window (default:
the last two seconds of the window). A voice where ref has energy and fur
does not (or the reverse) is the one to inspect further.
"""

import argparse
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "vgm2151fur"))
sys.path.insert(0, str(BASE / "vgm2151fur" / "tools"))

from vgm2151fur.vgm import load_vgm, assemble_rom
import renderer
from vgm2151fur.furnace import render_wav

BANDS = {"low 20-120": (20, 120), "mid 120-500": (120, 500), "hi 2k-8k": (2000, 8000)}


def build_solo(vgm, rom: bytes, voice: int, t0: int, t1: int, out: Path) -> None:
    clock = vgm.c352_clock or 24192000
    raw = clock & 0x3FFFFFFF
    if getattr(vgm, "c352_mute_rear", False):
        raw |= 0x80000000
    header = bytearray(0x100)
    header[0:4] = b"Vgm "
    header[8:12] = struct.pack("<I", 0x171)
    header[0x18:0x1C] = struct.pack("<I", t1 - t0)
    header[0x24:0x28] = struct.pack("<I", 44100)
    header[0x34:0x38] = struct.pack("<I", 0x100 - 0x34)
    header[0xD6] = vgm.c352_divider or 72
    header[0xDC:0xE0] = struct.pack("<I", raw)

    # the driver only rewrites some registers per retrigger, so the voice's
    # geometry (bank/start/end/loop) is whatever the file left there: replay
    # the shadow up to t0 and emit it as a snapshot before the window
    shadow = [0] * 8
    for w in vgm.writes:
        if w.chip != "c352" or w.sample > t0:
            continue
        addr = w.reg & 0x7FFF
        if addr < 0x100:
            shadow[addr & 7] = w.val & 0xFFFF

    body = bytearray()
    payload = struct.pack("<II", len(rom), 0) + bytes(rom)
    body += bytes([0x67, 0x66, 0x92]) + struct.pack("<I", len(payload)) + payload
    for reg, val in ((4, shadow[4]), (5, shadow[5]), (6, shadow[6]), (7, shadow[7]),
                     (2, shadow[2]), (0, shadow[0]), (1, shadow[1]),
                     (3, shadow[3] & ~0x6000)):
        body += bytes([0xE1]) + struct.pack(">HH", voice * 8 + reg, val)
    last = 0
    for w in vgm.writes:
        if w.chip != "c352":
            continue
        addr = w.reg & 0x7FFF
        if addr != 0x202 and (addr >> 3) != voice:
            continue
        if not (t0 <= w.sample <= t1):
            continue
        delay = w.sample - t0 - last
        while delay > 0:
            step = min(delay, 65535)
            body += bytes([0x61]) + struct.pack("<H", step)
            delay -= step
        last = w.sample - t0
        port = 0x8000 if w.chip_id else 0
        body += bytes([0xE1]) + struct.pack(">HH", port | addr, w.val & 0xFFFF)
    gap = (t1 - t0) - 4410 - last
    if gap > 0:
        body += bytes([0x61]) + struct.pack("<H", min(gap, 65535))
    body += bytes([0xE1]) + struct.pack(">HH", voice * 8, 0x1111)
    body += b"\x66"
    out.write_bytes(bytes(header) + bytes(body))


def load_wav(path: Path) -> np.ndarray:
    data = path.read_bytes()
    pos = 12
    while pos + 8 <= len(data):
        ident = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if ident == b"data":
            body = data[pos + 8:pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    a = np.frombuffer(body, dtype="<i2").astype(np.float64)
    return a.reshape(-1, 2).mean(axis=1) / 32768.0


def band_energy(x: np.ndarray, lo: int, hi: int) -> float:
    f = np.fft.rfft(x * np.hanning(len(x)))
    p = np.abs(f) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / 44100)
    return float(p[(fr >= lo) & (fr < hi)].sum())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("t0", type=float)
    ap.add_argument("t1", type=float)
    ap.add_argument("outdir")
    ap.add_argument("voices", nargs="+", type=int)
    ap.add_argument("--probe", nargs=2, type=float, default=None,
                    help="probe window seconds (default: last 2s of the slice)")
    ap.add_argument("--keep", action="store_true", help="keep per-voice fixtures")
    args = ap.parse_args()

    t0 = int(args.t0 * 44100)
    t1 = int(args.t1 * 44100)
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    vgm = load_vgm(Path(args.source))
    rom = assemble_rom(vgm, "c352", 0)
    if not rom:
        print("source has no C352 ROM block")
        return 1
    probe = args.probe or (args.t1 - 2.0, args.t1 - 0.2)
    if probe[0] < args.t0:
        probe = (args.t0, probe[1])

    header = f"{'voice':>5s} | {'band':12s} | {'ref':>10s} {'fur':>10s}  (probe {probe[0]}-{probe[1]}s)"
    print(header)
    for voice in args.voices:
        solo = outdir / f"solo{voice}.vgm"
        build_solo(vgm, rom, voice, t0, t1, solo)
        renderer.render(solo, outdir / f"solo{voice}.ref.wav")
        fur = outdir / f"solo{voice}.fur"
        if fur.is_file():
            fur.unlink()
        proc = subprocess.run([sys.executable, "-m", "vgm2151fur", "convert", str(solo),
                               "-o", str(outdir)],
                              cwd=str(BASE / "vgm2151fur"), capture_output=True, text=True)
        if not fur.is_file():
            print(f"{voice:5d} | convert failed: {(proc.stdout or '')[-200:]} "
                  f"{(proc.stderr or '')[-200:]}")
            continue
        render_wav(fur, outdir / f"solo{voice}.fur.wav")
        ref = load_wav(outdir / f"solo{voice}.ref.wav")
        fw = load_wav(outdir / f"solo{voice}.fur.wav")
        pa = max(0, int((probe[0] - args.t0) * 44100))
        pb = max(pa + 1, int((probe[1] - args.t0) * 44100))
        for name, (lo, hi) in BANDS.items():
            rs, fs = ref[pa:min(pb, len(ref))], fw[pa:min(pb, len(fw))]
            r = band_energy(rs, lo, hi) if len(rs) >= 800 else float("nan")
            f = band_energy(fs, lo, hi) if len(fs) >= 800 else float("nan")
            print(f"{voice:5d} | {name:12s} | {r:10.2e} {f:10.2e}")
        if not args.keep:
            for p in (solo, outdir / f"solo{voice}.ref.wav",
                      outdir / f"solo{voice}.fur.wav"):
                if p.is_file():
                    p.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
