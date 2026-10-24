"""Measure a converted module and normalise it to a fixed peak headroom.

Furnace applies the song master volume linearly to the whole mix, so the
level is one float and one render tells the truth: `normalize_module`
renders at a quarter volume (below any clipping), reads the peak, and writes
the gain that lands the track at -2.5 dBFS, probing 24 dB lower if the
render still clips.

A build without the module's chips refuses it ("unrecognized system ID"),
which callers report as a skipped fit rather than measure silence.
"""

from __future__ import annotations

import array
import math
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vgm2151fur import furio
from vgm2151fur.furnace import FurnaceError, render_wav

TARGET_PEAK = 10 ** (-2.5 / 20)  # -2.5 dBFS: loudness first, 1.5 dB of headroom
MAX_GAIN = 10 ** (24 / 20)  # quieter than this is a broken render, not a track

# Probe volumes, loudest first; step down when the render sits at the clamp.
_PROBES = (0.25, 0.25 / 16, 0.25 / 256)

_CLIPPED = 0.98  # a peak this close to 1.0 may be a clamp, not the signal


@dataclass
class Normalization:
    data: bytes  # module bytes with the master volume applied
    peak: float  # full-volume peak (linear, 1.0 = full scale)
    gain: float  # the master volume that was written


def wav_peak(path: Path) -> float:
    """Largest absolute sample of a rendered WAV (f32 or s16)."""
    raw = Path(path).read_bytes()
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        if cid == b"fmt ":
            fmt = raw[pos + 8:pos + 8 + size]
        elif cid == b"data":
            data = raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    if fmt is None or data is None or len(fmt) < 16:
        raise ValueError(f"{Path(path).name}: no fmt/data chunk")
    audio_format, _ch, _rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if audio_format == 3 and bits == 32:
        samples = array.array("f")
        samples.frombytes(data[: len(data) - len(data) % 4])
        return max(max(samples, default=0.0), -min(samples, default=0.0))
    if audio_format == 1 and bits == 16:
        samples = array.array("h")
        samples.frombytes(data[: len(data) - len(data) % 2])
        return max(max(samples, default=0), -min(samples, default=0)) / 32768.0
    raise ValueError(f"{Path(path).name}: unsupported WAV format {audio_format}/{bits}")


def _render_raw_peak(fur: Path, wav: Path) -> float:
    """Render `fur` at its own volume and scan the peak, retrying once.

    Concurrent Furnace consoles occasionally lose a race on the shared
    config file and exit without rendering; the retry is for that transient.
    """
    try:
        render_wav(fur, wav, loops=0, outformat="f32")
    except FurnaceError:
        wav.unlink(missing_ok=True)
        render_wav(fur, wav, loops=0, outformat="f32")
    raw = wav_peak(wav)
    wav.unlink()
    return raw


def normalize_module(data: bytes, *, name: str = "module") -> Normalization | None:
    """Render `data` and return it with the master volume set to -2.5 dBFS peak.

    None when the render is silent, so callers keep the module untouched.
    """
    with tempfile.TemporaryDirectory(prefix="vgm2151fur-norm-") as tmp:
        tmpdir = Path(tmp)
        probe_fur = tmpdir / (name + ".fur")
        probe_wav = tmpdir / (name + ".f32.wav")
        peak = 0.0
        for probe in _PROBES:
            probe_fur.write_bytes(furio.set_master_volume(data, probe))
            # -loops 0 renders one pass: intro plus one loop body. A second
            # pass only repeats, and the peak of a repeat is the same peak.
            raw = _render_raw_peak(probe_fur, probe_wav)
            peak = raw / probe
            if raw < _CLIPPED:
                break
    if peak <= 0.0:
        return None
    gain = min(MAX_GAIN, TARGET_PEAK / peak)
    return Normalization(data=furio.set_master_volume(data, gain), peak=peak, gain=gain)


def peak_dbfs(peak: float) -> float:
    return 20 * math.log10(peak) if peak > 0 else float("-inf")
