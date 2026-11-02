"""YM2608 (OPNA) support: two banked FM blocks, rhythm and delta-T.

The OPNA reaches its registers through two buses. ``$56`` writes bus A: the SSG
below ``$10``, the rhythm section at ``$10-$1F``, then FM block 1 from ``$20``
(ymfm's ``ym2608::write_data``, ymfm_opn.cpp:1179-1209). ``$57`` writes bus B:
ADPCM-B below ``$11``, FM block 2 above it (``write_data_hi``, 1229-1259).
``vgm.py`` flattens that into ``(bus << 8) | register``, which is the address
space ``YM2610State`` already decodes. Furnace's channel offsets for the OPNA
are ``{0x00, 0x01, 0x02, 0x100, 0x101, 0x102}`` and its key-on values
``{0, 1, 2, 4, 5, 6}`` (ym2608.h:41-47): one register on bus A keys all six
channels, bit 2 picking the block. Those are ``CHAN_OFF_6`` and ``KON_6``, so
the FM half is a six-channel YM2610B and only the two low pages move.

The rhythm page is the YM2610's ADPCM-A page in a different place: ymfm gives
the same engine ``$10 & 0x0F`` as its register number, so control lands on 0,
the global level on 1 and ``$18+d`` - ``(pan << 6) | level`` - on ``8+d``,
which is what ``_write_adpcm_a`` decodes and ``adpcm_a_level`` folds. What
differs is the data: the six voices are ranges of a ROM compiled into Furnace
(ymfm_opn.cpp:1006-1011), so no sample travels with the module and a voice is
chosen by which bit of ``$10`` the driver sets. Bit 7 makes those bits a
release instead of a hit.

ADPCM-B is the same delta-T engine one bus over, with its registers ``$10``
lower than the YM2610's page - so ``+ 0x10`` hands them straight to
``_write_adpcm_b``. Its addresses are byte offsets divided by 32 (Furnace
recomputes them as ``offset >> 5``, ym2608.cpp:1182-1186), its rate is
``chipClock/144 * delta / 65535`` (ym2608.cpp:297-303) and repeat - what
Furnace sets for a loopable sample, ``0xb0`` against ``0xa0`` (line 1061) - is
bit 4 of the control byte. Bus A ``$29`` is the IRQ mask, where the YM2612
keeps its DAC data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.ym2610 import (
    ADPCM_A_DIV,
    ADPCM_B_DIV,
    AdpcmBHit,
    AdpcmBSample,
    YM2610State,
    pan_byte,
)
from vgm2151fur.ym2612 import YM2612_FREQ_BASE

# Furnace's own default is 8 MHz; a real FM Towns rip says 7987200 and the
# difference moves every f-num, so the clock travels with the module.
YM2608_DEFAULT_CLOCK = 7_987_200
# DivPlatformYM2608's ctor: fmFreqBase 9437184, fmDivBase 72 and ayDiv 32 at
# the /6 prescale (ym2608.cpp:2091-2097). The frequency base is the YM2612's to
# the unit, so the OPNA needs no note path of its own - every banked-OPN chip
# converts f-num to note through ym2612_note_for, and only the clock is per
# chip. Only the SSG divider differs from the YM2203's 16.
YM2608_FREQ_BASE = YM2612_FREQ_BASE
YM2608_SSG_DIVIDER = 32

# $102-$105 hold the byte address shifted right by five.
ADPCM_B_ADDR_SHIFT = 5

# ymfm's ADPCM-A channel order, which is also the order sysDef names the six
# rhythm columns (sysDef.cpp:1365-1370): bit i of $10 keys voice i, and
# $18+i is its level.
RHYTHM_VOICES = (
    ("BD", "Kick"),
    ("SD", "Snare"),
    ("TP", "Top"),
    ("HH", "HiHat"),
    ("TM", "Tom"),
    ("RM", "Rim"),
)

# Where each voice lives in the rhythm ROM. The engine reads one nibble per
# chipClock/432 tick, so the range alone says how long a hit can still be
# heard - without any sample data in the module.
RHYTHM_ROM = (
    (0x0000, 0x01BF),  # bass drum
    (0x01C0, 0x043F),  # snare drum
    (0x0440, 0x1B7F),  # top cymbal
    (0x1B80, 0x1CFF),  # high hat
    (0x1D00, 0x1F7F),  # tom tom
    (0x1F80, 0x1FFF),  # rim shot
)

# Channels per system: six FM (nine when channel 3 is split), three SSG, six
# rhythm and one ADPCM-B. sysDef gives 16 and 19 (sysDef.cpp:1352,1379).
YM2608_CHANNELS = 16
YM2608_EXT_CHANNELS = 19


class YM2608State(YM2610State):
    """Register image of one YM2608.

    The FM decode, the key-on map and channel-3 special mode are inherited
    from the six-channel YM2610B unchanged. Only the two low pages move, and
    they replace what the YM2610 keeps there: its delta-T page on bus A
    becomes the rhythm page, and its ADPCM-A page on bus B becomes ADPCM-B.
    Bus B ``$10`` is the IRQ flag control and ``$11-$2D`` are unimplemented,
    so both stop there rather than reach the inherited decoders.
    """

    def __init__(self) -> None:
        super().__init__(b=True)
        # The $10 key byte as last written, so a release reads as one.
        self.rhythm_key = 0

    # The banked-OPN collector asks whether the DAC has taken a channel over.
    # The OPNA has no DAC: bus A $2A/$29 is its IRQ mask, and channel 6 stays
    # an FM voice.
    dac_enabled = False

    def write(self, reg: int, val: int) -> None:
        bank = 1 if reg & 0x100 else 0
        r = reg & 0xFF
        if not bank:
            if 0x10 <= r <= 0x1F:
                self._write_adpcm_a(r & 0x0F, val)
                if r == 0x10:
                    self.rhythm_key = val
                return
        elif r <= 0x2D:
            if r < 0x10:
                self._write_adpcm_b(r + 0x10, val)
            return
        super().write(reg, val)

    def pan_byte(self, ch: int) -> int:
        """The stereo byte of ``$B4`` as the banked-OPN collector names it.

        The YM2610 module calls this ``pan_byte_ch``; the shared collector was
        written against the YM2612, so the OPNA answers to both names.
        """
        return self.pan_byte_ch(ch)

    def rhythm_level(self, voice: int) -> int:
        """The 0-31 column value that reproduces ``$18+voice``.

        The level field is a loudness, and the global ``$11`` scales every
        voice; folding it in (as the YM2610 ADPCM-A path does) leaves one
        value per note that carries the driver's whole mix balance.
        """
        return self.adpcm_a_level(voice)

    def rhythm_pan(self, voice: int) -> int:
        return pan_byte(self.a_pan[voice])


@dataclass
class RhythmHit:
    """One rhythm voice sounding between two writes of the key byte."""

    sample_time: int
    chip_id: int
    voice: int
    vol: int
    pan: int
    end_time: int = 0
    # Level and pan moves while the voice sounds, past its gate window.
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)


def _voice_playing(clock: int, hit: RhythmHit, t: int) -> bool:
    """True while the voice's fixed ROM body can still be heard at ``t``.

    A level write on a voice that has already run out of sample is the driver
    tidying its registers. Keeping it would hang a volume cell on a row where
    the chip is silent, and change nothing anyone can hear.
    """
    lo, hi = RHYTHM_ROM[hit.voice]
    seconds = (hi - lo + 1) * 2.0 / (clock / ADPCM_A_DIV)
    return (t - hit.sample_time) / 44100.0 <= seconds


def collect_ym2608_rhythm(vgm) -> tuple[list[RhythmHit], list[str]]:
    """Turn the bus-A rhythm page into one hit per voice key.

    A driver gates a voice by rewriting ``$10``; the same byte with bit 7 set
    releases it. A key on a voice that is already sounding is a retrigger, so
    the old hit ends where the new one starts. A level written inside the
    gate window belongs to the note about to sound and moves its column, a
    later one is a step on the voice already playing.
    """
    warnings: list[str] = []
    clock = vgm.ym2608_clock or YM2608_DEFAULT_CLOCK
    states: dict[int, YM2608State] = {}
    active: dict[tuple[int, int], RhythmHit] = {}
    hits: list[RhythmHit] = []

    def state(cid: int) -> YM2608State:
        st = states.get(cid)
        if st is None:
            st = states[cid] = YM2608State()
        return st

    for w in vgm.writes:
        if w.chip != "ym2608":
            continue
        reg, val = w.reg, w.val
        r = reg & 0xFF
        if reg & 0x100 or not (0x10 <= r <= 0x1F):
            continue
        st = state(w.chip_id)
        if r == 0x12:
            continue  # the trigger sample register; it drives no voice here
        st.write(reg, val)
        if r == 0x10:
            bits = val & 0x3F
            release = bool(val & 0x80)
            for voice in range(len(RHYTHM_VOICES)):
                if not (bits & (1 << voice)):
                    continue
                old = active.pop((w.chip_id, voice), None)
                if old is not None:
                    old.end_time = w.sample
                if release:
                    continue
                hit = RhythmHit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    voice=voice,
                    vol=st.rhythm_level(voice),
                    pan=st.rhythm_pan(voice),
                )
                hits.append(hit)
                active[(w.chip_id, voice)] = hit
            continue
        if r == 0x11:
            # The global level moves every voice that is sounding.
            for (cid, voice), hit in active.items():
                if cid != w.chip_id:
                    continue
                vol = st.rhythm_level(voice)
                dt = w.sample - hit.sample_time
                if 0 <= dt <= 64:
                    hit.vol = vol
                elif _voice_playing(clock, hit, w.sample):
                    last = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
                    if vol != last:
                        hit.vol_pts.append((w.sample, vol))
            continue
        voice = r - 0x18
        if not 0 <= voice < len(RHYTHM_VOICES):
            continue
        hit = active.get((w.chip_id, voice))
        if hit is None:
            continue
        vol = st.rhythm_level(voice)
        pan = st.rhythm_pan(voice)
        dt = w.sample - hit.sample_time
        if 0 <= dt <= 64:
            hit.vol = vol
            hit.pan = pan
        elif _voice_playing(clock, hit, w.sample):
            last = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
            if vol != last:
                hit.vol_pts.append((w.sample, vol))
            last = hit.pan_pts[-1][1] if hit.pan_pts else hit.pan
            if pan != last:
                hit.pan_pts.append((w.sample, pan))

    end = vgm.total_samples or 0
    for hit in active.values():
        if not hit.end_time:
            hit.end_time = end
    return hits, warnings


def deltat_store_body(data: bytes) -> bytes:
    """Pad a delta-T body out to the 256-byte length Furnace will play.

    ``DivSample`` sizes a depth-6 sample as ``(((count+1)/2)+255)&~0xff``
    (sample.cpp:593) and the platform plays ``offset`` to ``offset + lengthB -
    1``, so a body whose declared length is not a multiple of 256 makes the chip
    read whatever sample was packed after it. Tailing on the idle nibble costs
    at most 255 bytes of silence and keeps that read inside our own sample.
    """
    if len(data) & 0xFF:
        data = bytes(data) + b"\x80" * (0x100 - (len(data) & 0xFF))
    return bytes(data)


def _deltat_slice(rom: bytes, start: int, end: int) -> bytes:
    """One delta-T range at the chip's own 32-byte address granularity.

    `$102-$105` name blocks of 32, so this is the byte span the source actually
    played - no wider. Rounding down to a 256-byte block instead, which is what
    an ADPCM-A ROM bank wants, would put bytes from *before* the sample in front
    of it and delay its attack by up to 255 bytes. The tail is `deltat_store_body`
    business, on the other end. A range running past the end of the data block is
    padded with the nibble that holds the accumulator still.
    """
    if not rom:
        return b""
    size = 1 << ADPCM_B_ADDR_SHIFT
    start &= ~(size - 1)
    end = (end & ~(size - 1)) | (size - 1)
    if end < start:
        end = start + size - 1
    data = bytes(rom[start:min(len(rom), end + 1)])
    if len(data) < end - start + 1:
        data += b"\x80" * (end - start + 1 - len(data))
    return data


def collect_ym2608_delta_t(
    vgm, rom: bytes,
) -> tuple[list[AdpcmBSample], list[str]]:
    """Walk bus B's ADPCM-B page and group its plays into samples.

    A play is a write of ``$100`` with bit 7 set; bit 4 asks for a repeat and
    bit 0 resets the engine. Clearing bit 7 stops it too - ymfm's
    ``adpcm_b_channel::clock`` returns to idle without it, which is how a
    driver releases a sample it does not want to hear again.
    """
    warnings: list[str] = []
    clock = vgm.ym2608_clock or YM2608_DEFAULT_CLOCK
    states: dict[int, YM2608State] = {}
    active: dict[int, AdpcmBHit] = {}
    hits: list[AdpcmBHit] = []

    def state(cid: int) -> YM2608State:
        st = states.get(cid)
        if st is None:
            st = states[cid] = YM2608State()
        return st

    def advance(hit: AdpcmBHit, t: int) -> bool:
        """Move the hit's play position to ``t``. False once it has ended."""
        if hit.ended:
            return False
        dt = t - hit.play_t
        if dt > 0 and not hit.loop:
            rate_hz = clock / ADPCM_B_DIV * (hit.play_rate & 0xFFFF) / 65535.0
            hit.play_pos += dt * rate_hz / 44100.0
        hit.play_t = t
        if not hit.loop and hit.play_pos >= (hit.end - hit.start + 1):
            hit.ended = True
            return False
        return True

    def follows(wi: int, cid: int, reg_no: int, t: int, span: int) -> bool:
        """True when a write of this bus-B register follows within ``span``.

        The low rate byte alone is half a value: treating it as a change would
        step the sample to whatever stale high byte was left in the register.
        A second control write inside the gate window is a retrigger, so the
        moves it carries belong to the next note, not this one.
        """
        limit = 8 if reg_no == 0x09 else 64
        for nw in vgm.writes[wi + 1:wi + span]:
            if nw.sample - t > limit:
                break
            if (nw.chip == "ym2608" and nw.chip_id == cid
                    and (nw.reg & 0x1FF) == (0x100 | reg_no)):
                return True
        return False

    for wi, w in enumerate(vgm.writes):
        if w.chip != "ym2608":
            continue
        reg, val = w.reg, w.val
        r = reg & 0xFF
        if not (reg & 0x100) or r > 0x0F:
            continue
        st = state(w.chip_id)
        st.write(reg, val)
        hit = active.get(w.chip_id)
        if hit is not None:
            dt = w.sample - hit.sample_time
            if 0 <= dt <= 64:
                # The driver commonly writes the pan, and sometimes the level
                # or rate, a few samples after the start bit. That is this
                # note's own setting, not a step inside it.
                if r == 0x01:
                    hit.pan = pan_byte(st.b_pan)
                elif r in (0x09, 0x0A):
                    hit.rate = st.b_freq
                    hit.play_rate = hit.rate
                elif r == 0x0B:
                    hit.vol = st.b_vol
            elif dt > 64 and not follows(wi, w.chip_id, 0x00, w.sample, 25) \
                    and advance(hit, w.sample):
                if r == 0x01:
                    pan = pan_byte(st.b_pan)
                    last = hit.pan_pts[-1][1] if hit.pan_pts else hit.pan
                    if pan != last:
                        hit.pan_pts.append((w.sample, pan))
                elif r == 0x0B:
                    vol = st.b_vol
                    last = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
                    if vol != last:
                        hit.vol_pts.append((w.sample, vol))
                elif r == 0x0A or (r == 0x09 and not follows(
                    wi, w.chip_id, 0x0A, w.sample, 9
                )):
                    rate = st.b_freq
                    last = hit.rate_pts[-1][1] if hit.rate_pts else hit.rate
                    if rate != last:
                        hit.rate_pts.append((w.sample, rate))
                        hit.play_rate = rate
        if r != 0x00:
            continue
        start = bool(val & 0x80)
        reset = bool(val & 0x01)
        old = active.pop(w.chip_id, None)
        if not start or reset:
            if old is not None:
                old.end_time = w.sample
            continue
        if old is not None:
            old.end_time = w.sample
        lo = st.b_start << ADPCM_B_ADDR_SHIFT
        hi = ((st.b_end << ADPCM_B_ADDR_SHIFT)
              | ((1 << ADPCM_B_ADDR_SHIFT) - 1))
        if hi < lo:
            hi = lo + ((1 << ADPCM_B_ADDR_SHIFT) - 1)
        hit = AdpcmBHit(
            sample_time=w.sample,
            chip_id=w.chip_id,
            start=lo,
            end=hi,
            rate=st.b_freq,
            vol=st.b_vol,
            pan=pan_byte(st.b_pan),
            loop=bool(val & 0x10),
            play_t=w.sample,
            play_rate=st.b_freq,
        )
        hits.append(hit)
        active[w.chip_id] = hit

    end = vgm.total_samples or 0
    for hit in active.values():
        if not hit.end_time:
            hit.end_time = end

    # A pan of 0 silences the voice, but Furnace's panning command reads 0 as
    # center. A mute that never returns ends the note where it was written, so
    # the sample cannot play on past where the source fell silent.
    for hit in hits:
        for idx, (t, pan) in enumerate(hit.pan_pts):
            if pan or any(p for _t, p in hit.pan_pts[idx + 1:]):
                continue
            hit.end_time = t
            hit.pan_pts = hit.pan_pts[:idx]
            hit.vol_pts = [pt for pt in hit.vol_pts if pt[0] < t]
            hit.rate_pts = [pt for pt in hit.rate_pts if pt[0] < t]
            break

    samples: dict[tuple[int, int, int, int], AdpcmBSample] = {}
    for hit in hits:
        key = (hit.chip_id, hit.start, hit.end, hit.rate)
        smp = samples.get(key)
        if smp is None:
            data = _deltat_slice(rom, hit.start, hit.end)
            smp = AdpcmBSample(hit.chip_id, hit.start, hit.end, hit.rate, data)
            samples[key] = smp
        smp.hits.append(hit)

    if hits and not rom:
        warnings.append(
            "YM2608 ADPCM-B key-ons but no delta-T data block (0x81); the "
            "ADPCM channel is silent"
        )
    return list(samples.values()), warnings
