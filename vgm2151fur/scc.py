"""Konami K051649 / K052539 (SCC / SCC-I) -> notes -> Furnace.

The register model follows libvgm ``k051649.c`` (the core VGM rips are played
with) and Furnace's ``vgsound_emu`` SCC core, which implement the same chip.

The VGM command is ``0xD2 pp aa dd``: ``pp`` is a register *bank* and ``aa`` an
8-bit register inside it.  This module keeps the bank and register packed as
``reg = (pp << 8) | aa``:

    0x000-0x0ff  waveform RAM: 32 bytes per voice, voices 0-3 (0x060-0x07f is
                 shared by voices 3 and 4 on the K051649)
    0x100-0x109  frequency: two bytes per voice, low then high (12-bit period)
    0x200-0x204  volume: 4-bit per voice
    0x300        voice enable (gate) bits, one per voice
    0x400-0x49f  waveform RAM for voice 4 (K052539 / SCC+)
    0x500-0x50f  test register (rotate and description bits)

A voice sounds at ``clock / (16 * (period + 1))`` and is silent while
``period <= 8``.  Furnace's SCC platform runs the pitch table with the same
clock and divider and writes ``value - 1``, so a period register round-trips
exactly through the table in ``_pitch.py``.

Gates: the chip has no key-on register, only the enable bits, and drivers use
either the enable bit, the volume register (0 = off), or both as the gate.  A
note starts when either goes from off to on and ends when either goes off;
the pitch (and waveform) is the one in the registers, with the same one-frame
refit the C140 path uses for drivers that write the note's own registers a few
milliseconds after the gate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note, module_clock
from vgm2151fur.vgm import VgmFile

SCC_VOICES = 5
SCC_WAVE_LEN = 32
SCC_DEFAULT_CLOCK = 1789772  # MSX SCC: half the 3.579545 MHz slot clock
SCC_MIN_PERIOD = 9  # the chip's counter halts a voice at period <= 8
SCC_REFIT_SAMPLES = 735  # one 60 Hz VGM frame
# A driver that morphs a note's waveform rewrites the 32 bytes in one burst;
# writes this close together are the same rewrite, and this caps how many
# morph steps one note keeps.
WAVE_BURST_SAMPLES = 64
WAVE_PTS_MAX = 4096
# Gates shorter than this are driver housekeeping (init sequences toggle the
# enable mask for a few samples), not music.
SCC_MIN_NOTE = 100

TEST_FREQ_4BIT = 0x01
TEST_FREQ_8BIT = 0x02
TEST_ROTATE = 0x40

# Wave RAM bytes the two shared voices share on the K051649.
_SHARED_WAVE_LO = 0x60


def scc_rate(clock: int, period: int) -> float:
    """Voice frequency for a 12-bit period register, in Hz."""
    return (clock or SCC_DEFAULT_CLOCK) / (16.0 * (period + 1))


def scc_note(clock: int, period: int) -> tuple[int, int]:
    """Period register -> (Furnace note, E5 fine tune) for the given clock.

    Furnace's SCC platform writes ``pitch value - 1``, so the pitch table has
    to produce ``period + 1`` for the source's ``period`` (the ``period_offset``
    argument).
    """
    return furnace_period_note(
        clock=module_clock(clock),
        divider=16,
        value=period,
        period_offset=1,
    )


@dataclass
class SccHit:
    sample_time: int
    chip_id: int
    voice: int
    period: int  # 12-bit frequency register at the gate
    vol: int  # 4-bit volume register at the gate
    wave: bytes  # 32 signed bytes the voice plays
    off_sample: int = 0  # gate-off sample (0 = still sounding at the end)
    # Mid-note register writes while the voice sounds.
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    # Every (sample, image) change of the 32 wave-RAM bytes the voice plays
    # after the gate, one entry per rewrite burst. The analyzer merges the
    # burst into the instrument's wave macro.
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)


@dataclass
class SccOff:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class SccNote:
    """A gate-to-gate interval of one voice, with everything it needs."""

    hit: SccHit
    end_sample: int  # 0 = still sounding
    wave_index: int = 0


def _blank_chip() -> dict:
    return {
        "wave": [bytearray(SCC_WAVE_LEN) for _ in range(SCC_VOICES)],
        "freq": [0] * SCC_VOICES,
        "vol": [0] * SCC_VOICES,
        # live gate state: enable bit and non-zero volume
        "en": [False] * SCC_VOICES,
        "active": [None] * SCC_VOICES,
        "test": 0,
        "gate_time": [0] * SCC_VOICES,
        # samples until which a pitch/volume/wave rewrite counts as the note's
        # own (drivers write them a few milliseconds after the gate)
        "refit_until": [0] * SCC_VOICES,
    }


def _wave_bytes(raw: bytes | bytearray) -> bytes:
    """Wave RAM image, kept as the chip's raw (unsigned) bytes.

    The chip reads each byte as signed 8-bit; Furnace's wavetable holds
    ``signed + 128``, so the writer converts with ``(b + 128) & 0xFF``.  An
    all-zero image is silence (signed zero).
    """
    return bytes(raw)


def collect_scc(
    vgm: VgmFile,
) -> tuple[list[SccNote], list[SccOff], list[bytes], list[str]]:
    """Walk the SCC register stream into notes.

    Returns (notes, key-offs, distinct waveforms, warnings).  Waveforms are
    deduplicated so several channels or notes sharing one image share one
    Furnace instrument.
    """
    warnings: list[str] = []
    notes: list[SccNote] = []
    offs: list[SccOff] = []
    waves: dict[bytes, int] = {}
    plus = bool(getattr(vgm, "k051649_plus", False))
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "k051649"})
    if not chips:
        return [], [], [], warnings
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    acc: Counter = Counter()

    def wave_index(wave: bytes) -> int:
        idx = waves.get(wave)
        if idx is None:
            idx = len(waves)
            waves[wave] = idx
        return idx

    def close(st: dict, cid: int, voice: int, t: int, *, emit_off: bool = True) -> None:
        """End the voice's interval at t (a gate went off)."""
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= SCC_MIN_NOTE:
            if emit_off:
                offs.append(SccOff(t, cid, voice))
            notes.append(SccNote(hit=hit, end_sample=t, wave_index=wave_index(hit.wave)))
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def open_gate(st: dict, cid: int, voice: int, t: int) -> None:
        """Start the voice's interval at t (a gate went on)."""
        hit = SccHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=st["freq"][voice],
            vol=st["vol"][voice],
            wave=_wave_bytes(st["wave"][voice]),
        )
        st["active"][voice] = hit
        st["gate_time"][voice] = t
        st["refit_until"][voice] = t + SCC_REFIT_SAMPLES

    def update_gate(st: dict, cid: int, voice: int, t: int) -> None:
        """Recompute the voice's gate from the enable bit and the volume."""
        on = st["en"][voice] and st["vol"][voice] > 0
        if on and st["active"][voice] is None:
            open_gate(st, cid, voice, t)
        elif not on:
            close(st, cid, voice, t)

    def hit(st: dict, voice: int):
        return st["active"][voice]

    for w in vgm.writes:
        if w.chip != "k051649":
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        bank = w.reg >> 8
        addr = w.reg & 0xFF
        val = w.val & 0xFF
        if bank in (0x0, 0x4):
            if st["test"] & (TEST_ROTATE if bank == 0x0 else 0):
                acc["wave_locked"] += 1
                continue
            if bank == 0x4 and not plus:
                # On the K051649 the port-4 bank is not wired; libvgm treats
                # it as another write to the same wave RAM.
                addr = 0x60 | (addr & 0x1F)
            elif plus and addr >= 0xA0:
                # Past voice 4's wave the K052539 window repeats the register
                # page. Frequency, volume and enable are taken from banks 1-3.
                continue
            if plus and 0x80 <= addr <= 0x9F:
                # SCC+ gives voice 4 its own 32 bytes. The K051649 does not:
                # there, voices 3 and 4 share 0x60-0x7F.
                voices = (4,)
            elif not plus and addr >= _SHARED_WAVE_LO:
                voices = (3, 4)
            else:
                voices = (addr >> 5,)
            for voice in voices:
                st["wave"][voice][addr & 0x1F] = val
            active = [v for v in voices if st["active"][v] is not None]
            for voice in active:
                h = st["active"][voice]
                if w.sample <= st["refit_until"][voice]:
                    # The driver rewrote the note's own waveform right after
                    # the gate; that image is what the voice plays.
                    h.wave = _wave_bytes(st["wave"][voice])
                    continue
                img = _wave_bytes(st["wave"][voice])
                if not any(img):
                    continue
                last = h.wave_pts[-1] if h.wave_pts else None
                if last is not None and w.sample - last[0] <= WAVE_BURST_SAMPLES:
                    # A whole-image rewrite is a burst of byte writes; only the
                    # state it settles on is a step of the morph.
                    h.wave_pts[-1] = (last[0], img)
                    continue
                if last is not None and img == last[1]:
                    continue
                if len(h.wave_pts) >= WAVE_PTS_MAX:
                    acc["wave_mid"] += 1
                    continue
                h.wave_pts.append((w.sample, img))
        elif bank == 0x1:
            voice = (addr >> 1) % SCC_VOICES
            if addr & 1:
                st["freq"][voice] = (st["freq"][voice] & 0x0FF) | ((val & 0x0F) << 8)
            else:
                st["freq"][voice] = (st["freq"][voice] & 0xF00) | val
            h = hit(st, voice)
            if h is not None:
                # Compare with what the voice was last seen playing, so a step
                # back to the note's own pitch is recorded too.
                cur = h.freq_pts[-1][1] if h.freq_pts else h.period
                if cur != st["freq"][voice]:
                    if w.sample <= st["refit_until"][voice]:
                        # A stale key-on value replaced inside the frame is the
                        # note's own pitch (same rule as C140/C352).
                        h.period = st["freq"][voice]
                    else:
                        h.freq_pts.append((w.sample, st["freq"][voice]))
        elif bank == 0x2:
            voice = addr % SCC_VOICES
            new_vol = val & 0x0F
            if new_vol != st["vol"][voice]:
                st["vol"][voice] = new_vol
                h = hit(st, voice)
                if h is not None and new_vol > 0:
                    # SCC drivers set the level before the gate and start the
                    # decay right after it, so a write inside the frame is a
                    # step of that decay, not the note's own level.
                    h.vol_pts.append((w.sample, new_vol))
                update_gate(st, cid, voice, w.sample)
        elif bank == 0x3:
            for voice in range(SCC_VOICES):
                en = bool(val & (1 << voice))
                if en == st["en"][voice]:
                    continue
                st["en"][voice] = en
                update_gate(st, cid, voice, w.sample)
        elif bank == 0x5:
            if val:
                st["test"] = val
                if val & (TEST_FREQ_4BIT | TEST_FREQ_8BIT):
                    acc["test_freq"] += 1

    # Close whatever is still sounding so the writer sees an open end.
    for cid, st in state.items():
        for voice in range(SCC_VOICES):
            h = st["active"][voice]
            if h is not None:
                notes.append(SccNote(hit=h, end_sample=0, wave_index=wave_index(h.wave)))
                st["active"][voice] = None

    if acc.get("wave_locked"):
        warnings.append(
            f"{acc['wave_locked']} SCC waveform writes were ignored while the "
            "test register froze the wave RAM"
        )
    if acc.get("wave_mid"):
        warnings.append(
            f"{acc['wave_mid']} SCC waveform changes past the {WAVE_PTS_MAX}-entry "
            "cap were dropped from a note's morph"
        )
    if acc.get("test_freq"):
        warnings.append(
            f"{acc['test_freq']} SCC test-register writes change the frequency "
            "word width; the 12-bit interpretation is kept"
        )
    wave_list = [b""] * len(waves)
    for wave, idx in waves.items():
        wave_list[idx] = wave
    return notes, offs, wave_list, warnings
