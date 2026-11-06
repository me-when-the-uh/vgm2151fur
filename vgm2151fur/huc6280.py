"""HuC6280 (PC Engine / TurboGrafx-16) PSG -> notes, wavetables and PCM.

The HuC6280 PSG is a six-voice wavetable chip.  VGM writes arrive on command
``0xB9`` as a register/value pair; the register file starts with a channel
select latch at 0x00 followed by per-channel writes:

    0x00  channel select (bits 0-2, six voices)
    0x01  global balance (left/right nibbles)
    0x02  frequency low byte
    0x03  frequency high nibble (12-bit period)
    0x04  control: bit 7 enable, bit 6 direct-D/A, bits 0-4 volume (0-31)
    0x05  channel balance (left/right nibbles)
    0x06  waveform data (5-bit) / direct-D/A sample
    0x07  noise (bit 7 enable on voices 4/5, bits 0-4 period)
    0x08  LFO frequency
    0x09  LFO control

A voice sounds at ``clock / (32 * period)`` and its waveform is 32 five-bit
samples.  There is no key-on register: the enable bit plus a non-zero control
volume is the gate, exactly like the SN76489 collector.

Furnace's PC Engine platform writes the pitch-table value straight into the
period register (no -1), so a period round-trips through ``_pitch`` with
``period_offset=0`` at the PSG clock and a divider of 32.

The waveform buffer is written through 0x06 while the voice is disabled and
not in direct-D/A mode; the write index resets on a 1->0 direct-D/A transition
and advances on each disabled write (``pce_psg.cpp``).  A driver usually
reloads all 32 bytes before a note, and that image becomes the instrument's
wavetable.  Rapid mid-note rewrites become the instrument's wave macro.

Direct-D/A mode turns 0x06 into a 5-bit PCM register.  The DAC stream control
commands (0x90-0x95) feed those 8-bit DDA levels from a type-0x05 data block;
each play becomes an 8-bit sample body.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.vgm import STREAM_C6280, VgmFile

HUC6280_CHANNELS = 6
HUC6280_WAVE_LEN = 32
HUC6280_DEFAULT_CLOCK = 3579545  # PSG clock (COLOR_NTSC)
# The chip's counter runs at PSG clock / 32 with a 12-bit period register.
HUC6280_CPU_DIVIDER = 32
HUC6280_MAX_PERIOD = 0xFFF
# A driver writing the note's own registers a few milliseconds after the gate
# is still describing this note, not a bend (same rule as SCC/C140).
HUC6280_REFIT_SAMPLES = 735
# The LO and HI bytes of one 12-bit frequency update land within a few samples.
HUC6280_FREQ_PAIR_GAP = 64
# A driver writes the note's attack volume/balance a few samples after the
# gate. A change this close is that note's first level, not an envelope step;
# folding it into the note's own volume keeps the attack transient on the row
# instead of dropping it as a same-row volume step.
HUC6280_ATTACK_WINDOW = 64
# A whole-waveform rewrite is a burst of byte writes; only the settled image is
# a step of a morph.
WAVE_BURST_SAMPLES = 64
WAVE_PTS_MAX = 4096
# Shorter gates are driver housekeeping, not music.
HUC6280_MIN_NOTE = 100

REG_SELECT = 0x00
REG_GLOBAL_BALANCE = 0x01
REG_FREQ_LO = 0x02
REG_FREQ_HI = 0x03
REG_CTRL = 0x04
REG_CH_BALANCE = 0x05
REG_WAVE = 0x06
REG_NOISE = 0x07
REG_LFO_FREQ = 0x08
REG_LFO_CTRL = 0x09

CTRL_ENABLE = 0x80
CTRL_DDA = 0x40
CTRL_VOLUME = 0x1F
NOISE_ENABLE = 0x80
NOISE_MASK = 0x1F

# The chip scales a 4-bit balance nibble through this table before summing the
# three attenuators.  The converter maps the balance register straight to
# Furnace's 08xy, so the core applies the same table itself; the constant is
# only used for the effective-level helper and docs.
BALANCE_SCALE = (
    0x00, 0x03, 0x05, 0x07, 0x09, 0x0B, 0x0D, 0x0F,
    0x10, 0x13, 0x15, 0x17, 0x19, 0x1B, 0x1D, 0x1F,
)


def _scale_inv(att: int) -> int:
    """The balance nibble whose BALANCE_SCALE entry is closest to `att`."""
    return min(range(16), key=lambda i: abs(BALANCE_SCALE[i] - att))


_BALANCE_INV = tuple(_scale_inv(a) for a in range(32))


def pce_effective(control_vol: int, balance: int) -> tuple[int, int]:
    """Split the PCE attenuators into (volume column, 08xy pan).

    The chip's per-side level is ``0x1F - min(0x1F, gbal + bal + al)`` where
    ``al = 0x1F - control_vol`` and each balance nibble becomes
    ``0x1F - BALANCE_SCALE[nibble]``.  A driver may use the control volume or
    the balance register (or both) for its envelope, so the converter puts the
    louder side's level on the volume column and the left/right ratio on the
    pan.  Feeding that back through the same chip model reproduces the original
    two levels exactly: the louder side is nibble 15 with ``al = 0x1F - peak``,
    and the quieter side picks the nibble whose attenuation closes the gap.
    """
    al = 0x1F - (control_vol & 0x1F)
    bl = 0x1F - BALANCE_SCALE[(balance >> 4) & 0x0F]
    br = 0x1F - BALANCE_SCALE[balance & 0x0F]
    left = 0x1F - min(0x1F, bl + al)
    right = 0x1F - min(0x1F, br + al)
    peak = max(left, right)
    if peak <= 0:
        return 0, 0
    pl = _BALANCE_INV[left + 0x1F - peak]
    pr = _BALANCE_INV[right + 0x1F - peak]
    return peak, ((pl & 15) << 4) | (pr & 15)


def huc6280_period_hz(clock: int, period: int) -> float:
    """Voice frequency for a 12-bit period register, in Hz."""
    p = period if period else 4096
    return (clock or HUC6280_DEFAULT_CLOCK) / (HUC6280_CPU_DIVIDER * p)


def huc6280_note(clock: int, period: int) -> tuple[int, int]:
    """Period register -> (Furnace note, E5) for the same register.

    Furnace's PC Engine platform writes the pitch-table value without an
    offset, and its table runs at the doubled CPU clock with a divider of 64,
    which is the PSG clock with a divider of 32.
    """
    return furnace_period_note(
        clock=clock or HUC6280_DEFAULT_CLOCK,
        divider=HUC6280_CPU_DIVIDER,
        value=max(0, min(HUC6280_MAX_PERIOD, period)),
        period_offset=0,
        maximum=HUC6280_MAX_PERIOD,
    )


def huc6280_noise_note(noise: int) -> int:
    """Furnace note for a ``0x07`` periodic-noise value (properNoiseLayout).

    The platform writes ``note - 60`` back to the noise register, so the
    register value is the note's offset from C-4.
    """
    return 60 + (noise & NOISE_MASK)


def _effective_period(period: int) -> int:
    # A period of 0 loads the counter with 4096, the longest (lowest) period.
    return period if period else 4096


def _period_delta(base: int, new: int) -> int:
    """1/64-semitone change from one 12-bit period to another."""
    base = _effective_period(max(0, base))
    new = _effective_period(max(0, new))
    return int(round(64.0 * 12.0 * math.log2(base / new)))


@dataclass
class Huc6280Hit:
    sample_time: int
    chip_id: int
    voice: int
    period: int
    vol: int                  # control volume (0-31)
    wave: bytes               # 32 five-bit samples
    balance: int = 0          # channel balance register (0x05)
    noise: int = 0            # 0x07 register
    off_sample: int = 0
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    balance_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)
    noise_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Huc6280Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class Huc6280Note:
    """A gate-to-gate interval of one voice, with everything it needs."""

    hit: Huc6280Hit
    end_sample: int  # 0 = still sounding
    wave_index: int = 0


def _blank_chip() -> dict:
    return {
        "select": 0,
        "wave": [bytearray(HUC6280_WAVE_LEN) for _ in range(HUC6280_CHANNELS)],
        "widx": [0] * HUC6280_CHANNELS,
        "freq": [0] * HUC6280_CHANNELS,
        "control": [0] * HUC6280_CHANNELS,
        "balance": [0] * HUC6280_CHANNELS,
        "noise": [0] * HUC6280_CHANNELS,
        "global_balance": 0,
        "lfo_freq": 0,
        "lfo_ctrl": 0,
        "active": [None] * HUC6280_CHANNELS,
        "gate_time": [0] * HUC6280_CHANNELS,
        "refit_until": [0] * HUC6280_CHANNELS,
    }


def collect_huc6280(
    vgm: VgmFile,
) -> tuple[list[Huc6280Note], list[Huc6280Off], list[bytes], list[str]]:
    """Walk the 0xB9 register stream into notes.

    Returns (notes, key-offs, distinct waveforms, warnings).  Waveforms are
    deduplicated so voices or notes sharing one image share one instrument.
    """
    warnings: list[str] = []
    notes: list[Huc6280Note] = []
    offs: list[Huc6280Off] = []
    waves: dict[bytes, int] = {}
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "huc6280"})
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

    def _on(st: dict, voice: int) -> bool:
        control = st["control"][voice]
        return bool(control & CTRL_ENABLE) and (control & CTRL_VOLUME) > 0

    def open_gate(st: dict, cid: int, voice: int, t: int) -> None:
        wave = bytes(st["wave"][voice])
        hit = Huc6280Hit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=st["freq"][voice],
            vol=st["control"][voice] & CTRL_VOLUME,
            wave=wave,
            balance=st["balance"][voice],
            noise=st["noise"][voice],
        )
        st["active"][voice] = hit
        st["gate_time"][voice] = t
        st["refit_until"][voice] = t + HUC6280_REFIT_SAMPLES

    def close(st: dict, cid: int, voice: int, t: int, *, emit_off: bool = True) -> None:
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= HUC6280_MIN_NOTE:
            if emit_off:
                offs.append(Huc6280Off(t, cid, voice))
            notes.append(Huc6280Note(
                hit=hit, end_sample=t, wave_index=wave_index(hit.wave),
            ))
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def update_gate(st: dict, cid: int, voice: int, t: int) -> None:
        on = _on(st, voice)
        hit = st["active"][voice]
        if on and hit is None:
            open_gate(st, cid, voice, t)
        elif not on and hit is not None:
            close(st, cid, voice, t)

    def note_live(st: dict, voice: int):
        return st["active"][voice]

    def update_pitch(st: dict, voice: int, t: int) -> None:
        hit = note_live(st, voice)
        if hit is None:
            return
        period = st["freq"][voice]
        cur = hit.pitch_pts[-1][1] if hit.pitch_pts else hit.period
        if cur == period:
            return
        if t <= st["refit_until"][voice]:
            # The driver rewrote the note's own pitch inside the frame.
            hit.period = period
            return
        # A 12-bit update is a LO/HI pair; the two writes can land a few
        # samples apart. Keep only the settled value so the transient low byte
        # never becomes a pitch step (a 730-sample merge would instead eat the
        # driver's per-frame vibrato).
        if hit.pitch_pts and t - hit.pitch_pts[-1][0] <= HUC6280_FREQ_PAIR_GAP:
            hit.pitch_pts[-1] = (t, period)
        else:
            hit.pitch_pts.append((t, period))

    def update_volume(st: dict, voice: int, t: int) -> None:
        hit = note_live(st, voice)
        if hit is None:
            return
        vol = st["control"][voice] & CTRL_VOLUME
        if t == hit.sample_time:
            hit.vol = vol
        elif not hit.vol_pts or hit.vol_pts[-1][1] != vol:
            hit.vol_pts.append((t, vol))

    def update_balance(st: dict, voice: int, t: int) -> None:
        hit = note_live(st, voice)
        if hit is None:
            return
        balance = st["balance"][voice]
        if t == hit.sample_time:
            hit.balance = balance
        elif not hit.balance_pts or hit.balance_pts[-1][1] != balance:
            hit.balance_pts.append((t, balance))

    def update_noise(st: dict, voice: int, t: int) -> None:
        hit = note_live(st, voice)
        if hit is None:
            return
        noise = st["noise"][voice]
        if t == hit.sample_time:
            hit.noise = noise
        elif not hit.noise_pts or hit.noise_pts[-1][1] != noise:
            hit.noise_pts.append((t, noise))

    def write_wave(st: dict, voice: int, val: int, t: int) -> None:
        control = st["control"][voice]
        if not (control & CTRL_DDA):
            # A waveform byte, not a direct-D/A sample.
            st["wave"][voice][st["widx"][voice]] = val & 0x1F
        if (control & 0xC0) == 0x00:
            st["widx"][voice] = (st["widx"][voice] + 1) & 0x1F
        hit = note_live(st, voice)
        if hit is None or (control & CTRL_DDA):
            return
        img = bytes(st["wave"][voice])
        if not any(img):
            return
        last = hit.wave_pts[-1] if hit.wave_pts else None
        if last is not None and t - last[0] <= WAVE_BURST_SAMPLES:
            # A whole-image rewrite is a burst of byte writes.
            hit.wave_pts[-1] = (last[0], img)
            return
        if last is not None and img == last[1]:
            return
        if len(hit.wave_pts) >= WAVE_PTS_MAX:
            acc["wave_mid"] += 1
            return
        hit.wave_pts.append((t, img))

    for w in vgm.writes:
        if w.chip != "huc6280":
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        t = w.sample
        if reg == REG_SELECT:
            st["select"] = val & 0x07
            continue
        if reg == REG_GLOBAL_BALANCE:
            st["global_balance"] = val
            continue
        if reg == REG_LFO_FREQ:
            st["lfo_freq"] = val
            continue
        if reg == REG_LFO_CTRL:
            st["lfo_ctrl"] = val
            continue
        voice = st["select"]
        if voice >= HUC6280_CHANNELS:
            continue
        if reg == REG_FREQ_LO:
            st["freq"][voice] = (st["freq"][voice] & 0x0F00) | val
            update_pitch(st, voice, t)
        elif reg == REG_FREQ_HI:
            st["freq"][voice] = (st["freq"][voice] & 0x00FF) | ((val & 0x0F) << 8)
            update_pitch(st, voice, t)
        elif reg == REG_CTRL:
            old = st["control"][voice]
            if (old & CTRL_DDA) and not (val & CTRL_DDA):
                st["widx"][voice] = 0
            if not (old & CTRL_ENABLE) and (val & CTRL_ENABLE):
                if not (val & CTRL_DDA):
                    st["widx"][voice] = (st["widx"][voice] + 1) & 0x1F
            st["control"][voice] = val
            update_gate(st, cid, voice, t)
            update_volume(st, voice, t)
        elif reg == REG_CH_BALANCE:
            st["balance"][voice] = val
            update_balance(st, voice, t)
        elif reg == REG_WAVE:
            write_wave(st, voice, val, t)
        elif reg == REG_NOISE:
            st["noise"][voice] = val
            update_noise(st, voice, t)

    for cid, st in state.items():
        for voice in range(HUC6280_CHANNELS):
            if st["active"][voice] is not None:
                hit = st["active"][voice]
                notes.append(Huc6280Note(
                    hit=hit, end_sample=0, wave_index=wave_index(hit.wave),
                ))
                st["active"][voice] = None

    if acc.get("glitch"):
        warnings.append(
            f"HuC6280: ignored {acc['glitch']} sub-frame tone gates"
        )
    if acc.get("wave_mid"):
        warnings.append(
            f"{acc['wave_mid']} HuC6280 waveform changes past the "
            f"{WAVE_PTS_MAX}-entry cap were dropped from a note's morph"
        )
    wave_list = [b""] * len(waves)
    for wave, idx in waves.items():
        wave_list[idx] = wave
    return notes, offs, wave_list, warnings


# ---------------------------------------------------------------------------
# Direct-D/A PCM (DAC stream control 0x90-0x95 and direct 0x06 writes)

@dataclass
class Huc6280PcmSample:
    """One 8-bit body converted from a stream of 5-bit DDA levels."""

    name: str
    data: bytes
    rate: int
    loop: bool = False
    start: int = 0
    end: int = 0


@dataclass
class Huc6280PcmHit:
    sample_time: int
    end_time: int
    sample_index: int
    voice: int  # channel the DDA stream writes (0-5)
    chip_id: int = 0
    # The DAC stream byte rate at this play.  The driver changes it 23442
    # times on the faceball rip, so it is the note's pitch, not a fixed
    # per-body property.
    rate: int = 0


def _dda_to_s8(body: bytes) -> bytes:
    """5-bit DDA level -> the 8-bit body Furnace's PCE sample path replays.

    Furnace turns a sample byte back into its DDA register with
    ``(int8_t)(b ^ 0x80) >> 3 & 0x1f``.  Choosing ``b = ((v << 3) + 0x80)``
    makes that expression return ``v`` for all 0-31, so the register stream is
    reproduced exactly.
    """
    return bytes((((v & 0x1F) << 3) + 0x80) & 0xFF for v in body)


def _pcm_body(
    play, vgm: VgmFile,
) -> tuple[bytes, bool] | None:
    """The DDA body one DAC-stream play reads, or None if the bank is absent."""
    if play.block >= 0:
        blocks = getattr(vgm, "huc6280_pcm_blocks", ())
        if 0 <= play.block < len(blocks):
            return blocks[play.block], play.loop
        return None
    bank = getattr(vgm, "huc6280_pcm", b"")
    if not bank:
        return None
    start = max(0, play.offset & 0x7FFFFFFF)
    if play.length > 0:
        end = start + play.length
    else:
        end = len(bank)
    body = bytes(bank[start:end])
    return body, play.loop


def collect_huc6280_pcm(
    vgm: VgmFile,
) -> tuple[list[Huc6280PcmSample], list[Huc6280PcmHit], list[str]]:
    """DAC-stream PCM plays (0x93 offset/length, 0x95 whole-block)."""
    warnings: list[str] = []
    samples: list[Huc6280PcmSample] = []
    hits: list[Huc6280PcmHit] = []
    plays = [
        p for p in getattr(vgm, "dac_stream_plays", ())
        if p.chip_type == STREAM_C6280
    ]
    if not plays:
        return samples, hits, warnings
    readable = bool(getattr(vgm, "huc6280_pcm", b"")) or bool(
        getattr(vgm, "huc6280_pcm_blocks", ())
    )
    if not readable:
        warnings.append(
            f"{len(plays)} HuC6280 DAC stream play(s) but no type-0x05 PCM "
            "data block; those samples are silent"
        )
        return samples, hits, warnings

    by_data: dict[tuple[bytes, bool], int] = {}
    unreadable = 0
    for play in plays:
        got = _pcm_body(play, vgm)
        if got is None or not got[0]:
            unreadable += 1
            continue
        body, loop = got
        data = _dda_to_s8(body)
        key = (data, loop)
        idx = by_data.get(key)
        if idx is None:
            idx = len(samples)
            by_data[key] = idx
            samples.append(Huc6280PcmSample(
                name=f"HuC6280 PCM {idx:02d}",
                data=data,
                rate=max(1, play.rate or 16000),
                loop=loop,
                start=play.sample,
                end=play.stop_sample or 0,
            ))
        voice = (play.port if play.port is not None else play.stream) & 0x07
        if voice >= HUC6280_CHANNELS:
            voice = 0
        end = play.stop_sample
        if not end and play.length > 0 and play.rate > 0:
            end = play.sample + int(round(
                (len(body) / play.rate) * 44100
            ))
        hits.append(Huc6280PcmHit(
            sample_time=play.sample,
            end_time=end or 0,
            sample_index=idx,
            voice=voice,
            chip_id=play.chip_id,
            rate=max(1, play.rate or 16000),
        ))
    if unreadable:
        warnings.append(
            f"{unreadable} HuC6280 DAC stream play(s) point at missing block "
            "or bank data; those samples are silent"
        )
    return samples, hits, warnings
