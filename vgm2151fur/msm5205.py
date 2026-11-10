"""OKI MSM5205 streamed ADPCM -> Generic PCM DAC bodies.

The MSM5205 has no sample ROM.  A CPU writes one ADPCM nibble at a time
through VGM command 0x32, and the chip clocks the nibbles out at
``clock / prescaler``.  This collector mirrors libvgm's reset, FIFO, prescaler
and ADPCM state machine, renders one PCM track per chip at the 44.1 kHz VGM
timeline, then splits it at attacks and silences exactly like the YM2612 DAC
and 32X PWM paths.  Each piece becomes a 16-bit Generic PCM DAC body.
"""

from __future__ import annotations

import array
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.samplecluster import cluster_fragments16
from vgm2151fur.vgm import VGM_RATE, VgmFile

# The audio track is rendered at the VGM rate, so these segmentation windows
# scale directly from the PWM/YM2612-DAC rules.
MSM5205_CHUNK_FRAMES = 11025       # 0.25 s
MSM5205_ATTACK_SILENCE = 500       # ~11 ms of near-silence splits a hit
MSM5205_ATTACK_MIN_FRAMES = 2756   # ~62 ms
MSM5205_ATTACK_MAX_FRAMES = 44100  # ~1 s
MSM5205_ATTACK_QUIET_DIV = 32
MSM5205_ATTACK_MAX_SAMPLES = 480
MSM5205_ONSET_WINDOW = 16
MSM5205_ONSET_QUIET = 64
MSM5205_ONSET_MIN_GAP = 128
MSM5205_ONSET_LOOKAHEAD = 128
MSM5205_DEFAULT_PRESCALER = 0
MSM5205_DEFAULT_BITS = 4

# libvgm/MAME ADPCM tables.
_INDEX_SHIFT = (-1, -1, -1, -1, 2, 4, 6, 8)
_NBL2BIT = (
    (1, 0, 0, 0), (1, 0, 0, 1), (1, 0, 1, 0), (1, 0, 1, 1),
    (1, 1, 0, 0), (1, 1, 0, 1), (1, 1, 1, 0), (1, 1, 1, 1),
    (-1, 0, 0, 0), (-1, 0, 0, 1), (-1, 0, 1, 0), (-1, 0, 1, 1),
    (-1, 1, 0, 0), (-1, 1, 0, 1), (-1, 1, 1, 0), (-1, 1, 1, 1),
)


def _diff_table() -> list[int]:
    table = [0] * (49 * 16)
    for step in range(49):
        stepval = int(16.0 * (11.0 / 10.0) ** step)
        for nib, bits in enumerate(_NBL2BIT):
            table[step * 16 + nib] = bits[0] * (
                stepval * bits[1]
                + stepval // 2 * bits[2]
                + stepval // 4 * bits[3]
                + stepval // 8
            )
    return table


_DIFF = _diff_table()


def msm5205_prescaler(value: int) -> int:
    """Master-clock divider for the S1/S2 pair."""
    value &= 3
    if value & 1:
        return 1 if value & 2 else 64
    return 48 if value & 2 else 96


@dataclass
class Msm5205Hit:
    time: int


@dataclass
class Msm5205Sample:
    chip_id: int
    data: bytes  # signed 16-bit little-endian at 44100 Hz
    rate: int = VGM_RATE
    hits: list[Msm5205Hit] = field(default_factory=list)
    chunk: int = 0
    first_chunk: bool = True

    @property
    def frames(self) -> int:
        return len(self.data) // 2

    @property
    def name(self) -> str:
        return f"MSM5205 {self.chip_id + 1} {self.chunk:03d}"


class _ChipState:
    def __init__(self, clock: int, flags: int) -> None:
        self.clock = clock
        self.prescaler = flags & 0x03
        self.bitwidth = 4 if flags & 0x04 else 3
        self.reset = 0
        self.signal = -2
        self.step = 0
        self.vclk = 0
        self.data = [0] * 8
        self.pos = 0
        self.accum = 0.0

    @property
    def rate(self) -> float:
        return max(1.0, self.clock / msm5205_prescaler(self.prescaler))

    def write(self, reg: int, value: int) -> None:
        value &= 0xFF
        if reg == 0:
            old = self.reset
            self.reset = value
            if old ^ value:
                self.signal = 0
                self.step = 0
            if self.reset:
                self.pos = 0
        elif reg == 1:
            write_pos = (self.pos >> 4) & 0x07
            read_pos = self.pos & 0x07
            if ((write_pos + 1) & 0x07) != read_pos:
                self.data[write_pos] = value
                self.pos = ((write_pos + 1) << 4) | read_pos
        elif reg == 2:
            old = self.vclk
            self.vclk = value
            if (
                msm5205_prescaler(self.prescaler) == 1
                and ((old ^ value) & 1) and self.vclk
            ):
                self._clock_one()
        elif reg == 4:
            self.prescaler = value & 0x03
        elif reg == 5:
            self.bitwidth = 4 if value else 3

    def _clock_one(self) -> None:
        if self.reset:
            return
        read_pos = self.pos & 0x0F
        write_pos = (self.pos >> 4) & 0x07
        if read_pos == write_pos or msm5205_prescaler(self.prescaler) == 1:
            return
        data = self.data[read_pos]
        if self.bitwidth == 3:
            data <<= 1
        nib = data & 0x0F
        sample = _DIFF[self.step * 16 + nib]
        self.signal = ((sample << 8) + (self.signal * 245)) >> 8
        self.signal = max(-2048, min(2047, self.signal))
        self.step = max(0, min(48, self.step + _INDEX_SHIFT[nib & 7]))
        self.pos = (write_pos << 4) | ((read_pos + 1) & 0x07)

    def tick(self, samples: int = 1) -> int:
        """Advance the chip clock and return the new output level."""
        if self.reset:
            return 0
        step = self.rate / VGM_RATE
        self.accum += step * samples
        while self.accum >= 1.0:
            self._clock_one()
            self.accum -= 1.0
        # Master mode repeats the last converted level between clocks.
        return self.signal

    def level(self) -> int:
        if self.reset:
            return 0
        return max(-32768, min(32767, self.signal << 4))


def _render_chip(vgm: VgmFile, cid: int, clock: int, flags: int, *,
                 total: int) -> bytes:
    state = _ChipState(clock, flags)
    out = array.array("h")
    writes = [w for w in vgm.writes if w.chip == "msm5205" and w.chip_id == cid]
    wi = 0
    for t in range(max(1, total)):
        while wi < len(writes) and writes[wi].sample <= t:
            w = writes[wi]
            state.write(w.reg, w.val)
            wi += 1
        state.tick()
        out.append(state.level())
    # Writes after the header's stream end still matter to the first sample.
    while wi < len(writes):
        state.write(writes[wi].reg, writes[wi].val)
        wi += 1
        out.append(state.level())
    return out.tobytes()


def _pcm_stats(data: bytes) -> tuple[array.array, int, int, int]:
    vals = array.array("h")
    vals.frombytes(data)
    if not vals:
        return vals, 0, 64, 0
    peak = max(abs(v) for v in vals)
    counts = Counter(vals)
    idle, idle_count = counts.most_common(1)[0]
    if idle_count * 100 < len(vals) or abs(idle) > max(64, peak // 4):
        idle = 0
    width = max(64, peak // MSM5205_ATTACK_QUIET_DIV)
    return vals, idle, width, peak


def _audible_pcm(data: bytes, idle: int, width: int) -> bool:
    vals = array.array("h")
    vals.frombytes(data)
    return any(abs(v - idle) > width for v in vals)


def _onset_starts(
    vals: array.array, idle: int, width: int, peak: int,
) -> list[int]:
    if not vals:
        return []
    amp = [abs(v - idle) for v in vals]
    env: list[int] = []
    window: list[int] = []
    for value in amp:
        window.append(value)
        if len(window) > MSM5205_ONSET_WINDOW:
            window.pop(0)
        env.append(max(window))
    low = width
    high = max(width * 4, peak // 8)
    starts: list[int] = []

    def add(pos: int) -> None:
        if not starts or pos - starts[-1] >= MSM5205_ONSET_MIN_GAP:
            starts.append(pos)

    if env[0] >= high:
        add(0)
    low_start: int | None = None
    for i, value in enumerate(env):
        if value <= low:
            if low_start is None:
                low_start = i
            continue
        if low_start is not None and i - low_start >= MSM5205_ONSET_QUIET:
            look = env[i:i + MSM5205_ONSET_LOOKAHEAD]
            if look and max(look) >= high:
                add(i)
        low_start = None
    for i in range(1, len(env)):
        if env[i] >= high and env[i - 1] <= low:
            add(i)
    return sorted(starts)


def _attack_bodies(
    data: bytes, vals: array.array, idle: int, width: int,
) -> list[tuple[int, bytes]]:
    if not vals:
        return []
    peak = max(abs(v - idle) for v in vals)
    starts = _onset_starts(vals, idle, width, peak)
    if len(starts) < 2:
        starts = []

    def quiet(value: int) -> bool:
        return abs(value - idle) <= width

    bounds: list[tuple[int, int]] = []
    if starts:
        for i, a in enumerate(starts):
            b = starts[i + 1] if i + 1 < len(starts) else len(vals)
            while b > a and quiet(vals[b - 1]):
                b -= 1
            if b > a:
                bounds.append((a, b))
    else:
        start = 0
        quiet_start: int | None = None
        for i, value in enumerate(vals):
            if quiet(value):
                if quiet_start is None:
                    quiet_start = i
                continue
            if quiet_start is not None and i - quiet_start >= MSM5205_ATTACK_SILENCE:
                a, b = start, quiet_start
                while a < b and quiet(vals[a]):
                    a += 1
                while b > a and quiet(vals[b - 1]):
                    b -= 1
                if b - a >= MSM5205_ATTACK_MIN_FRAMES:
                    bounds.append((a, b))
                start = i
            quiet_start = None
        a, b = start, len(vals)
        while a < b and quiet(vals[a]):
            a += 1
        while b > a and quiet(vals[b - 1]):
            b -= 1
        if b - a >= MSM5205_ATTACK_MIN_FRAMES:
            bounds.append((a, b))
    merged: list[tuple[int, int]] = []
    i = 0
    while i < len(bounds):
        a, b = bounds[i]
        if b - a < MSM5205_ATTACK_MIN_FRAMES and i + 1 < len(bounds):
            bounds[i + 1] = (a, bounds[i + 1][1])
            i += 1
            continue
        merged.append((a, b))
        i += 1
    if merged and merged[-1][1] - merged[-1][0] < MSM5205_ATTACK_MIN_FRAMES:
        if len(merged) > 1:
            merged[-2] = (merged[-2][0], merged[-1][1])
            merged.pop()
        else:
            merged.pop()
    out: list[tuple[int, bytes]] = []
    for a, b in merged:
        body = data[a * 2:b * 2]
        if not _audible_pcm(body, idle, width):
            continue
        while b - a > MSM5205_ATTACK_MAX_FRAMES:
            cut = a + MSM5205_ATTACK_MAX_FRAMES
            if b - cut < MSM5205_ATTACK_MIN_FRAMES:
                cut = a + (b - a) // 2
            out.append((a, data[a * 2:cut * 2]))
            a = cut
        out.append((a, data[a * 2:b * 2]))
    return out


def collect_msm5205(
    vgm: VgmFile, *, lossy: bool = False,
) -> tuple[list[Msm5205Sample], list[str]]:
    """Render and split every MSM5205 instance into Generic PCM DAC bodies."""
    warnings: list[str] = []
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == "msm5205"})
    if not ids:
        return [], warnings
    total = vgm.total_samples or 0
    if total <= 0:
        total = max(
            (w.sample for w in vgm.writes if w.chip == "msm5205"), default=0
        ) + 1
    clock = vgm.msm5205_clock or 384_000
    flags = vgm.msm5205_flags
    tracks: dict[int, bytes] = {}
    for cid in ids:
        tracks[cid] = _render_chip(vgm, cid, clock, flags, total=total)

    def occurrences(use_attack: bool) -> list[tuple[int, int, bytes]]:
        out: list[tuple[int, int, bytes]] = []
        for cid, data in sorted(tracks.items()):
            frames = len(data) // 2
            if frames <= 0:
                continue
            vals, idle, width, _peak = _pcm_stats(data)
            if use_attack:
                for start, body in _attack_bodies(data, vals, idle, width):
                    out.append((cid, start, body))
            else:
                for start in range(0, frames, MSM5205_CHUNK_FRAMES):
                    end = min(frames, start + MSM5205_CHUNK_FRAMES)
                    body = data[start * 2:end * 2]
                    if _audible_pcm(body, idle, width):
                        out.append((cid, start, body))
        return out

    attack_occ = occurrences(True)
    chunk_occ = occurrences(False)
    attack_unique = len({(cid, body) for cid, _s, body in attack_occ})
    use_attack = (
        len(attack_occ) > 1
        and attack_unique <= MSM5205_ATTACK_MAX_SAMPLES
    )
    chosen = attack_occ if use_attack else chunk_occ

    audible: list[Msm5205Sample] = []
    seen: dict[tuple[int, bytes], Msm5205Sample] = {}
    for cid, start, body in chosen:
        key = (cid, body)
        found = seen.get(key)
        if found is not None:
            found.hits.append(Msm5205Hit(time=start))
            continue
        chunk = Msm5205Sample(
            chip_id=cid,
            data=body,
            hits=[Msm5205Hit(time=start)],
            chunk=len(audible),
            first_chunk=(start == 0),
        )
        seen[key] = chunk
        audible.append(chunk)
    if not audible:
        warnings.append("MSM5205 streams decoded to silence")
    if lossy and len(audible) > 32:
        keep, remap = cluster_fragments16(
            [s.data for s in audible],
            [s.rate for s in audible],
            {i: len(s.hits) for i, s in enumerate(audible)},
        )
        compact = [audible[i] for i in keep]
        for old, smp in enumerate(audible):
            new = remap[old]
            if new < 0 or keep[new] == old:
                continue
            compact[new].hits.extend(smp.hits)
        if len(compact) < len(audible):
            warnings.append(
                f"MSM5205: lossy sample clustering {len(audible)} -> "
                f"{len(compact)} bodies"
            )
        audible = compact
    warnings = list(dict.fromkeys(warnings))
    return audible, warnings
