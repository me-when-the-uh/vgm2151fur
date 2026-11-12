"""Konami K005289 (Bubble System WSG) -> notes -> Furnace.

The chip is two wavetable voices.  The VGM command is ``0x42`` with a 4-bit
register offset and a 12-bit value:

    0/1  control: volume (bits 0-3) and waveform bank (bits 5-7)
    2/3  latch the 12-bit pitch (LD)
    4/5  trigger the latched pitch (TG)

Each voice reads a 32-byte waveform from the 512-byte 4-bit PROM:
``prom[voice * 0x100 + waveform * 0x20 + address]``.  The PROM arrives as a
0xC3 RAM data block (a 2-byte address and then the bytes).

The counter runs at the chip clock, so a voice sounds at
``clock / (32 * (freq + 1))`` with ``freq = 0xFFF - register``.  Furnace's
WSG platform runs the same pitch table with a divider of 32 and writes
``0x1000 - value``, so the source register round-trips exactly with a
``period_offset`` of 1.

Volume is the only gate: the chip has no key-on bit, so a note starts when
the control volume leaves zero and ends when it returns.  A TG that changes
the pitch splits the interval, exactly like the SCC's period writes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note, module_clock
from vgm2151fur.vgm import VgmFile

K005289_VOICES = 2
K005289_WAVE_LEN = 32
K005289_WAVE_BANKS = 8
K005289_PROM_SIZE = 0x200
K005289_DEFAULT_CLOCK = 3579545
K005289_DIVIDER = 32
# A driver writes pitch and the control level within one VGM frame; a write
# this soon after the gate belongs to the note itself.
K005289_REFIT_SAMPLES = 735
# A driver that morphs the waveform rewrites several banks in one burst;
# writes this close together settle on one image.
WAVE_BURST_SAMPLES = 64
WAVE_PTS_MAX = 4096
# Gates shorter than this are driver housekeeping, not music.
K005289_MIN_NOTE = 100


def k005289_rate(clock: int, period: int) -> float:
    """Voice frequency for a 12-bit period value (``0xFFF - register``)."""
    return (clock or K005289_DEFAULT_CLOCK) / (
        K005289_DIVIDER * (period + 1)
    )


def k005289_note(clock: int, period: int) -> tuple[int, int]:
    """Period value -> (Furnace note, E5 fine tune).

    Furnace's WSG platform writes ``0x1000 - pitchTableValue``, and the chip
    subtracts that from 0xFFF before counting, so the table has to produce
    ``period + 1`` for the source's ``period``.
    """
    return furnace_period_note(
        clock=module_clock(clock),
        divider=K005289_DIVIDER,
        value=period,
        period_offset=1,
    )


@dataclass
class K005289Hit:
    sample_time: int
    chip_id: int
    voice: int
    period: int  # 0xFFF - pitch register at the note start
    vol: int  # 4-bit volume register
    wave: bytes  # 32 unsigned PROM bytes the voice plays
    off_sample: int = 0  # gate-off sample (0 = still sounding at the end)
    # Mid-note register writes while the voice sounds.
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    # Every (sample, image) change of the 32 PROM bytes the voice plays after
    # the gate, one entry per settled rewrite burst.
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)


@dataclass
class K005289Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class K005289Note:
    """A volume-gate interval of one voice, with everything it needs."""

    hit: K005289Hit
    end_sample: int  # 0 = still sounding
    wave_index: int = 0


def _blank_chip() -> dict:
    return {
        "latch": [0] * K005289_VOICES,
        "period": [0] * K005289_VOICES,
        "vol": [0] * K005289_VOICES,
        "waveform": [0] * K005289_VOICES,
        "active": [None] * K005289_VOICES,
        "gate_time": [0] * K005289_VOICES,
        # samples until which a pitch/wave rewrite counts as the note's own
        "refit_until": [0] * K005289_VOICES,
    }


def _prom_image(vgm: VgmFile) -> bytearray:
    """The 512-byte PROM, stitched from its RAM-write slices."""
    prom = bytearray(K005289_PROM_SIZE)
    for rom in vgm.roms:
        if rom.kind != "k005289" or not rom.data:
            continue
        start = max(0, rom.start)
        if start >= K005289_PROM_SIZE:
            continue
        data = bytes(rom.data[: K005289_PROM_SIZE - start])
        prom[start:start + len(data)] = data
    return prom


def collect_k005289(
    vgm: VgmFile,
) -> tuple[list[K005289Note], list[K005289Off], list[bytes], list[str]]:
    """Walk the K005289 register stream into notes.

    Returns (notes, key-offs, distinct waveforms, warnings).  Waveforms are
    deduplicated so several channels or notes sharing one image share one
    Furnace instrument.
    """
    warnings: list[str] = []
    notes: list[K005289Note] = []
    offs: list[K005289Off] = []
    waves: dict[bytes, int] = {}
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "k005289"})
    if not chips:
        return [], [], [], warnings
    prom = _prom_image(vgm)
    if not any(prom):
        # The parser already warns about the missing data block; there is no
        # waveform to play, so there is nothing to collect.
        return [], [], [], warnings
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    acc: Counter = Counter()

    def wave_index(wave: bytes) -> int:
        idx = waves.get(wave)
        if idx is None:
            idx = len(waves)
            waves[wave] = idx
        return idx

    def image(voice: int, bank: int) -> bytes:
        off = (
            voice * 0x100
            + (bank & (K005289_WAVE_BANKS - 1)) * K005289_WAVE_LEN
        )
        return bytes(prom[off:off + K005289_WAVE_LEN])

    def close(st: dict, cid: int, voice: int, t: int) -> None:
        """End the voice's interval at t (the control volume reached zero)."""
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= K005289_MIN_NOTE:
            offs.append(K005289Off(t, cid, voice))
            notes.append(K005289Note(
                hit=hit, end_sample=t, wave_index=wave_index(hit.wave),
            ))
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def open_gate(
        st: dict, cid: int, voice: int, t: int, vol: int, wave: bytes,
    ) -> None:
        hit = K005289Hit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=st["period"][voice],
            vol=vol,
            wave=wave,
        )
        st["active"][voice] = hit
        st["gate_time"][voice] = t
        st["refit_until"][voice] = t + K005289_REFIT_SAMPLES

    def record_wave(hit: K005289Hit, t: int, image_bytes: bytes) -> None:
        if not any(image_bytes):
            return
        if hit.wave_pts:
            last_t, last_img = hit.wave_pts[-1]
            if t - last_t <= WAVE_BURST_SAMPLES:
                # A whole-image rewrite is a burst of byte writes; only the
                # state it settles on is a step of the morph.
                if image_bytes != last_img:
                    hit.wave_pts[-1] = (last_t, image_bytes)
                return
            if image_bytes == last_img:
                return
        elif image_bytes == hit.wave:
            return
        if len(hit.wave_pts) >= WAVE_PTS_MAX:
            acc["wave_mid"] += 1
            return
        hit.wave_pts.append((t, image_bytes))

    for w in vgm.writes:
        if w.chip != "k005289":
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        off = w.reg & 0x0F
        val = w.val & 0x0FFF
        voice = off & 1
        if off in (0, 1):
            new_vol = val & 0x0F
            new_wave = (val >> 5) & (K005289_WAVE_BANKS - 1)
            img = image(voice, new_wave)
            st["waveform"][voice] = new_wave
            hit = st["active"][voice]
            if new_vol == 0:
                if hit is not None:
                    close(st, cid, voice, w.sample)
                st["vol"][voice] = 0
                continue
            if hit is None:
                open_gate(st, cid, voice, w.sample, new_vol, img)
            elif w.sample <= st["refit_until"][voice]:
                # The opening control write (or a write within the same frame)
                # is the note's own level and waveform, not a macro step.
                hit.vol = new_vol
                hit.wave = img
            else:
                if new_vol != st["vol"][voice]:
                    hit.vol_pts.append((w.sample, new_vol))
                record_wave(hit, w.sample, img)
            st["vol"][voice] = new_vol
        elif off in (2, 3):
            st["latch"][voice] = val & 0xFFF
        elif off in (4, 5):
            period = 0xFFF - (st["latch"][voice] & 0xFFF)
            st["period"][voice] = period
            hit = st["active"][voice]
            if hit is None:
                continue
            if w.sample <= st["refit_until"][voice]:
                hit.period = period
                continue
            cur = hit.freq_pts[-1][1] if hit.freq_pts else hit.period
            if cur != period:
                hit.freq_pts.append((w.sample, period))

    # Close whatever is still sounding so the writer sees an open end.
    for cid, st in state.items():
        for voice in range(K005289_VOICES):
            hit = st["active"][voice]
            if hit is not None:
                notes.append(K005289Note(
                    hit=hit, end_sample=0, wave_index=wave_index(hit.wave),
                ))
                st["active"][voice] = None

    if acc.get("glitch"):
        warnings.append(
            f"{acc['glitch']} K005289 volume gate(s) shorter than "
            f"{K005289_MIN_NOTE} samples were dropped"
        )
    if acc.get("wave_mid"):
        warnings.append(
            f"{acc['wave_mid']} K005289 waveform changes past the "
            f"{WAVE_PTS_MAX}-entry cap were dropped from a note's morph"
        )

    wave_list = [b""] * len(waves)
    for wave, idx in waves.items():
        wave_list[idx] = wave
    return notes, offs, wave_list, warnings
