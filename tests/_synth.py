"""Synthetic songs for the tests: a keyed YM2151 note among timing key-ons.

The analyzer measures the row grid from key-on spacing, so a test song needs
a heartbeat of key-ons. `lattice` supplies one. Every probe of the session
(legato semantics, LFO mapping, the sweep model) used this shape.

`inject_rows` wraps the sweep emitter to place extra effect columns on chosen
rows, which is how those probes reached effects the converter itself does not
emit.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from vgm2151fur.vgm import ChipWrite, VgmFile

PATCH = [
    (0x20, 0xC7), (0x28, 0x3E), (0x30, 0x00), (0x38, 0x00),
    (0x60, 0x00), (0x68, 0x00), (0x70, 0x00), (0x78, 0x00),
    (0x80, 0x1F), (0x88, 0x1F), (0x90, 0x1F), (0x98, 0x1F),
    (0xA0, 0x00), (0xA8, 0x00), (0xB0, 0x00), (0xB8, 0x00),
    (0xC0, 0x00), (0xC8, 0x00), (0xD0, 0x00), (0xD8, 0x00),
    (0xE0, 0x0F), (0xE8, 0x0F), (0xF0, 0x0F), (0xF8, 0x0F),
]


def lattice(n: int = 60, spacing: int = 5000) -> list[ChipWrite]:
    return [
        ChipWrite(sample=k * spacing, chip="ym2151", chip_id=0, reg=0x08, val=0x78)
        for k in range(n)
    ]


def vgm(writes, **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=[], writes=list(writes), **kw,
    )


def raw_vgm(cmds: bytes, *, ym_clock: int = 3579545) -> bytes:
    """A loadable VGM file: header, the given command bytes, end-of-data."""
    buf = bytearray(0x40)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = ym_clock.to_bytes(4, "little")
    buf[0x34:0x38] = (0x40 - 0x34).to_bytes(4, "little")
    body = bytearray(cmds) + b"\x66"
    raw = bytes(buf) + bytes(body)
    return raw[:4] + (len(raw) - 4).to_bytes(4, "little") + raw[8:]


def note_song(key_on: int = 60000, key_off: int = 160000, *,
              kc: int = 0x00, kf: int = 0x00, pitch_writes=None) -> VgmFile:
    """A heartbeat plus a ch0 note, optionally with mid-note KC/KF writes."""
    writes = lattice(int(key_off / 5000) + 6)
    for reg, val in PATCH:
        writes.append(ChipWrite(key_on - 1000, "ym2151", 0, reg, val))
    writes.append(ChipWrite(key_on, "ym2151", 0, 0x28, kc))
    writes.append(ChipWrite(key_on, "ym2151", 0, 0x30, kf))
    writes.append(ChipWrite(key_on, "ym2151", 0, 0x08, 0x78))
    for entry in pitch_writes or []:
        sample, reg, val = entry
        writes.append(ChipWrite(sample, "ym2151", 0, reg, val))
    writes.append(ChipWrite(key_off, "ym2151", 0, 0x08, 0x00))
    writes.sort(key=lambda w: w.sample)
    return vgm(writes)


def staircase(start: int, steps: int = 20, *, step_samples: int = 500,
              kc: int = 0x42, kf: int = 0x14) -> list[tuple[int, int, int]]:
    """KC/KF writes of one 0.25 st step each."""
    out: list[tuple[int, int, int]] = []
    kc_v, kf_v = kc, kf
    for i in range(steps):
        kf_v += 0x40
        if kf_v > 0xFF:
            kf_v -= 0x100
            kc_v += 1
        t = start + i * step_samples
        out.append((t, 0x28, kc_v))
        out.append((t + 10, 0x30, kf_v))
    return out


@contextlib.contextmanager
def inject_rows(song, plan: list[tuple[int, int, int]], key_on: int):
    """Add (row offset, effect, value) columns to the note's rows.

    `plan` offsets are relative to the row the note key-on lands on. A note
    without a pitch sweep only reaches the emitter when it carries one, so a
    stub trajectory of no movement is attached when needed.
    """
    from vgm2151fur import furwrite as fw

    for ev in song.events:
        if ev.on and ev.sample == key_on and not getattr(ev, "sweep", None):
            ev.sweep = ((key_on + 500, 0),)
    row0, _delay = song.place(key_on)
    original = fw._emit_sweep

    def hook(grid, song_arg, ev, fx_cols, total_rows, sweep_e5, note_rows, chan=None, stats=None):
        original(grid, song_arg, ev, fx_cols, total_rows, sweep_e5, note_rows, chan, stats)
        if not ev.on or ev.ch >= 8:
            return
        for k, cmd, val in plan:
            cell = grid[ev.ch].get(row0 + k)
            if cell is None:
                cell = fw._blank_row()
                grid[ev.ch][row0 + k] = cell
            fw._add_fx(cell, cmd, val, fx_cols)

    fw._emit_sweep = hook
    try:
        yield row0
    finally:
        fw._emit_sweep = original
