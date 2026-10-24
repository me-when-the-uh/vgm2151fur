"""uPD7759 stand-alone ROM -> ADPCM phrases -> WAV + JSON.

The uPD7759 in stand-alone mode (the mode Konami Twin16 wires it in) plays
phrases out of its own data ROM:

* a 5-byte header: last phrase number, then ``5a a5 69 55``
* one big-endian 16-bit vector per phrase; the ROM address is ``value * 2``
* the command stream starts one byte after that address

The stream is decoded the way MAME does (src/devices/sound/upd7759.cpp): a
4-bit ADPCM state indexes a step table, block headers set the nibble rate
(``clock / (4 * (rate + 1))``, i.e. 5-17.8 kHz at the standard 640 kHz
clock), ``00dddddd`` blocks are silence, ``0xC0|r`` repeats the block that
follows ``r + 1`` times, and a ``0x00`` after a valid block ends the phrase.

The level changes once per nibble, so a phrase is rendered by interpolating
between the event points (one point per nibble at its own rate). The nibble
rate is always below the output rate, so this is an upsample, not a
decimation - the same model a WAV render of MAME's stream would give.

Timing matches MAME's state machine, including the 36-clock cost it charges
for a block header (and the extra 36 for the ``10`` block's count byte).
Those add up to ~0.2%, which is audible when a decoded phrase is compared
against a recording of the chip - the phases drift apart over a long phrase.
"""

from __future__ import annotations

import json
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

STANDARD_CLOCK = 640_000
MAGIC = b"\x5a\xa5\x69\x55"

# MAME charges 36 clocks to the block header state (and 36 more to the count
# byte state of a ``10`` block). The output holds the current level meanwhile.
HEADER_CLOCKS = 36

# MAME's upd775x_step / upd775x_state_table (upd7759.cpp).
STEP = (
    (0, 0, 1, 2, 3, 5, 7, 10, 0, 0, -1, -2, -3, -5, -7, -10),
    (0, 1, 2, 3, 4, 6, 8, 13, 0, -1, -2, -3, -4, -6, -8, -13),
    (0, 1, 2, 4, 5, 7, 10, 15, 0, -1, -2, -4, -5, -7, -10, -15),
    (0, 1, 3, 4, 6, 9, 13, 19, 0, -1, -3, -4, -6, -9, -13, -19),
    (0, 2, 3, 5, 8, 11, 15, 23, 0, -2, -3, -5, -8, -11, -15, -23),
    (0, 2, 4, 7, 10, 14, 19, 29, 0, -2, -4, -7, -10, -14, -19, -29),
    (0, 3, 5, 8, 12, 16, 22, 33, 0, -3, -5, -8, -12, -16, -22, -33),
    (1, 4, 7, 10, 15, 20, 29, 43, -1, -4, -7, -10, -15, -20, -29, -43),
    (1, 4, 8, 13, 18, 25, 35, 53, -1, -4, -8, -13, -18, -25, -35, -53),
    (1, 6, 10, 16, 22, 31, 43, 64, -1, -6, -10, -16, -22, -31, -43, -64),
    (2, 7, 12, 19, 27, 37, 51, 76, -2, -7, -12, -19, -27, -37, -51, -76),
    (2, 9, 16, 24, 34, 46, 64, 96, -2, -9, -16, -24, -34, -46, -64, -96),
    (3, 11, 19, 29, 41, 57, 79, 117, -3, -11, -19, -29, -41, -57, -79, -117),
    (4, 13, 24, 36, 50, 69, 96, 143, -4, -13, -24, -36, -50, -69, -96, -143),
    (4, 16, 29, 44, 62, 85, 118, 175, -4, -16, -29, -44, -62, -85, -118, -175),
    (6, 20, 36, 54, 76, 104, 144, 214, -6, -20, -36, -54, -76, -104, -144, -214),
)

STATE_TABLE = (-1, -1, 0, 0, 1, 2, 2, 3, -1, -1, 0, 0, 1, 2, 2, 3)


def phrase_count(rom: bytes) -> int:
    """Phrases in the ROM (header byte 0 is the last phrase number)."""
    if len(rom) < 5 or rom[1:5] != MAGIC:
        raise ValueError("not a uPD7759 stand-alone ROM (no 5a a5 69 55 header)")
    return rom[0] + 1


def phrase_address(rom: bytes, index: int) -> int:
    """First command byte of a phrase: vector value * 2, plus the +1 MAME adds."""
    if not 0 <= index < phrase_count(rom):
        raise IndexError(f"phrase {index} out of range 0..{rom[0]}")
    value = (rom[5 + 2 * index] << 8) | rom[6 + 2 * index]
    return value * 2 + 1


@dataclass
class Phrase:
    """One decoded phrase: level/duration events in the chip's clock domain.

    ``events`` holds ``(level, clocks)`` pairs in playback order; ``level`` is
    the ADPCM accumulator MAME mixes as ``level / 256`` (256.0 = full scale).
    """

    index: int
    start: int
    end: int | None
    events: list[tuple[int, int]] = field(default_factory=list)
    blocks: int = 0
    rates: set[int] = field(default_factory=set)

    @property
    def clocks(self) -> int:
        return sum(clocks for _level, clocks in self.events)

    def seconds(self, clock: int = STANDARD_CLOCK) -> float:
        return self.clocks / clock

    @property
    def peak(self) -> int:
        return max((abs(level) for level, _clocks in self.events), default=0)


def _decode_nibbles(rom: bytes, offset: int, count: int, rate: int, phrase: Phrase,
                    level: int, state: int) -> tuple[int, int, int]:
    """Feed `count` nibbles from the stream into the ADPCM state machine.

    Odd counts drop the low nibble of the last byte (hardware behavior).
    Returns (new offset, level, state).
    """
    left = count
    while left > 0:
        byte = rom[offset]
        offset += 1
        for nibble in (byte >> 4, byte & 0x0F):
            if left == 0:
                break
            level += STEP[state][nibble]
            state += STATE_TABLE[nibble]
            state = 0 if state < 0 else 15 if state > 15 else state
            phrase.events.append((level, rate * 4))
            left -= 1
    return offset, level, state


def decode_phrase(rom: bytes, index: int, clock: int = STANDARD_CLOCK) -> Phrase:
    """Walk one phrase's command stream the way MAME's state machine does."""
    addr = phrase_address(rom, index)
    phrase = Phrase(index=index, start=addr, end=None)
    level = 0
    state = 0
    repeat_count = 0
    repeat_offset = addr
    offset = addr
    first_valid = False

    while True:
        if repeat_count:
            repeat_count -= 1
            offset = repeat_offset
        if offset >= len(rom):
            break
        header = rom[offset]
        offset += 1
        kind = header & 0xC0

        if kind == 0x00:  # silence; 0x00 after a valid block is the end
            phrase.events.append((0, 1024 * ((header & 0x3F) + 1)))
            phrase.blocks += 1
            level = 0
            state = 0
            if header == 0 and first_valid:
                phrase.end = offset
                break
        elif kind == 0x40:  # 256 nibbles
            rate = (header & 0x3F) + 1
            phrase.rates.add(rate)
            phrase.blocks += 1
            phrase.events.append((level, HEADER_CLOCKS))
            offset, level, state = _decode_nibbles(rom, offset, 256, rate, phrase, level, state)
        elif kind == 0x80:  # n nibbles
            rate = (header & 0x3F) + 1
            phrase.rates.add(rate)
            phrase.blocks += 1
            phrase.events.append((level, HEADER_CLOCKS))
            count = rom[offset] + 1
            offset += 1
            phrase.events.append((level, HEADER_CLOCKS))
            offset, level, state = _decode_nibbles(rom, offset, count, rate, phrase, level, state)
        else:  # 0xC0 repeat: the block after this one plays r + 1 times
            repeat_count = (header & 0x07) + 1
            repeat_offset = offset
            phrase.blocks += 1
            phrase.events.append((level, HEADER_CLOCKS))

        if header:
            first_valid = True

    return phrase


def _event_points(phrase: Phrase, clock: int) -> tuple[list[float], list[int]]:
    """Level sample points: (time in clocks at clock/4, level) per event."""
    times: list[float] = []
    levels: list[int] = []
    acc = 0.0
    for level, clocks in phrase.events:
        frames = clocks / 4.0
        times.append(acc + frames / 2.0)
        levels.append(level)
        acc += frames
    return times, levels


def render_levels(phrase: Phrase, rate: int, clock: int = STANDARD_CLOCK) -> list[float]:
    """Phrase -> output levels at `rate` Hz (256.0 = full scale, MAME's scale)."""
    times, levels = _event_points(phrase, clock)
    if not levels:
        return []
    duration_frames = sum(clocks for _l, clocks in phrase.events) / 4.0
    out_n = max(1, int(round(duration_frames * rate / (clock / 4.0))))
    step = (clock / 4.0) / rate
    out: list[float] = []
    i = 0
    t = 0.0
    for _ in range(out_n):
        while i + 1 < len(times) and t > times[i + 1]:
            i += 1
        if i + 1 < len(times):
            t0, t1 = times[i], times[i + 1]
            frac = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
            value = levels[i] + (levels[i + 1] - levels[i]) * frac
        else:
            value = float(levels[i])
        out.append(value)
        t += step
    return out


def render_wav_bytes(phrase: Phrase, rate: int, clock: int = STANDARD_CLOCK,
                     gain: float = 1.0) -> bytes:
    """Render to 16-bit mono PCM at `rate`. gain scales the level/256 scale."""
    values = render_levels(phrase, rate, clock)
    scale = (32767.0 / 256.0) * gain
    out = bytearray(len(values) * 2)
    for i, value in enumerate(values):
        struct.pack_into("<h", out, i * 2, max(-32768, min(32767, int(round(value * scale)))))
    return bytes(out)


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(max(1000, int(rate)))
        w.writeframes(pcm)


def dump_rom(rom: bytes, out_dir: Path, *, source: str = "", rate: int = 44100,
             clock: int = STANDARD_CLOCK, code_base: int | None = None,
             target_peak: int | None = None) -> dict:
    """Write one WAV per phrase plus `upd7759_index.json`.

    A single gain is picked for the whole set so the loudest phrase stays
    inside 16-bit while the relative levels between phrases are kept.
    code_base names the files (and the index) the way a driver's sound latch
    reaches the phrases: phrase i is code ``code_base + i``. target_peak
    scales the set so its loudest sample sits at that 16-bit peak.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    count = phrase_count(rom)
    phrases = [decode_phrase(rom, i, clock) for i in range(count)]
    peak = max((p.peak for p in phrases), default=0)
    if target_peak and peak:
        gain = target_peak / (peak * (32767.0 / 256.0))
    else:
        gain = 1.0 if peak <= 250 else 250.0 / peak
    index: dict = {
        "source": source,
        "clock": clock,
        "rom_size": len(rom),
        "phrases": count,
        "rate": rate,
        "gain": round(gain, 6),
        "target_peak": target_peak,
        "items": [],
    }
    for phrase in phrases:
        code = None if code_base is None else code_base + phrase.index
        name = f"voice_{phrase.index:02d}" + (f"_code{code:02X}" if code is not None else "")
        file = out_dir / f"{name}.wav"
        pcm = render_wav_bytes(phrase, rate, clock, gain)
        write_wav(file, pcm, rate)
        index["items"].append({
            "index": phrase.index,
            "code": None if code is None else f"0x{code:02X}",
            "file": file.name,
            "start": phrase.start,
            "end": phrase.end,
            "blocks": phrase.blocks,
            "rates": sorted(phrase.rates),
            "events": len(phrase.events),
            "seconds": round(phrase.seconds(clock), 4),
            "peak_raw": phrase.peak,
        })
    (out_dir / "upd7759_index.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    return index
