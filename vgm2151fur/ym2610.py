"""YM2610 / YM2610B (OPNB) support.

The chip is an OPN core with 4 FM channels (6 on the YM2610B), the AY SSG,
and two sample engines: ADPCM-A (six fixed-rate channels) and ADPCM-B (one
variable-rate channel).  Furnace's YM2610 platforms run in linear pitch mode
with a 9440540 frequency base, so the FM note inverse is the YM2612 formula
with that base.  The sample engines use Furnace's ADPCM-A/B sample formats
and sample-map instruments.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from vgm2151fur.fm import FMPatch, OpRegs, OPN_CARRIERS, OPN_OP_TO_MASK
from vgm2151fur.ym2612 import (
    ym2612_note_fnum,
    ym2612_note_for,
)

YM2610_DEFAULT_CLOCK = 8000000
# DivPlatformYM2610Base passes 9440540.0 to DivPlatformOPN.
YM2610_FREQ_BASE = 9440540.0
# Furnace's YM2610 SSG divider is 32 (the YM2203 uses 16).
YM2610_SSG_DIVIDER = 32
# ADPCM-A is fixed at clock/432; ADPCM-B is clock/144 * (freq / 65535).
ADPCM_A_DIV = 432
ADPCM_B_DIV = 144
ADPCM_MAP_SLOTS = 120

# Channel register offsets per port.  The 4-channel part places FM1-2 on port
# 0 offsets 1/2 and FM3-4 on port 1 offsets 1/2.  The B part is 3+3.
CHAN_OFF_4 = ((0, 1), (0, 2), (1, 1), (1, 2))
CHAN_OFF_6 = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2))
# 0x28 channel field -> channel index.
KON_4 = {1: 0, 2: 1, 5: 2, 6: 3}
KON_6 = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4, 6: 5}
MAX_FNUM = 2047


def ym2610_note_fnum(note: int, clock: int, e5: int = 0x80):
    return ym2612_note_fnum(note, clock, e5, YM2610_FREQ_BASE)


def ym2610_note_for(block: int, fnum: int, clock: int) -> tuple[int, int]:
    return ym2612_note_for(block, fnum, clock, YM2610_FREQ_BASE)


def pan_byte(bits: int) -> int:
    """2-bit L/R field -> Furnace 08xy pan value."""
    return (0x00, 0x0F, 0xF0, 0xFF)[bits & 3]


def _blank_op() -> OpRegs:
    return OpRegs(
        dt_mul=0, tl=0, ks_ar=0, am_d1r=0, dt2_d2r=0, d1l_rr=0,
        sr=0, ssg_eg=0,
    )


class YM2610State:
    """Register image of one YM2610's FM and ADPCM-B halves."""

    def __init__(self, b: bool = False) -> None:
        self.b = b
        self.channels = 6 if b else 4
        self.chan_off = CHAN_OFF_6 if b else CHAN_OFF_4
        self.kon_map = KON_6 if b else KON_4
        self.fnum = [0] * self.channels
        self.block = [0] * self.channels
        self.fb_alg = [0] * self.channels
        self.pan_pms_ams = [0xC0] * self.channels
        self.ops = [[_blank_op() for _ in range(4)] for _ in range(self.channels)]
        self.kon = [False] * self.channels
        self.op_mask = [0] * self.channels
        self.mode = 0
        self.lfo = 0
        self._hi: list[int | None] = [None] * self.channels
        # Channel-2 per-operator frequencies (0xA8-0xAE on port 0).
        self.special = [0] * 3
        self.special_block = [0] * 3
        self.special_set = [False] * 3
        self._special_hi: list[int | None] = [None] * 3
        # ADPCM-A registers (port 1).  Start/end are address/256.
        self.a_control = 0
        self.a_master = 0
        self.a_vol = [0] * 6
        self.a_pan = [0] * 6
        self.a_start = [0] * 6
        self.a_end = [0] * 6
        # ADPCM-B registers (port 0).  Start/end are address/256.
        self.b_control = 0
        self.b_pan = 0
        self.b_start = 0
        self.b_end = 0
        self.b_freq = 0
        self.b_vol = 0
        self.b_flags = 0

    @property
    def multi_freq(self) -> bool:
        return self.mode in (1, 2)

    def _channel(self, bank: int, offset: int) -> int | None:
        for ch, (b, o) in enumerate(self.chan_off):
            if b == bank and o == offset:
                return ch
        return None

    def channel_of(self, reg: int) -> int | None:
        """Channel selected by an FM register, or None for global registers."""
        bank = 1 if reg & 0x100 else 0
        r = reg & 0xFF
        if 0x30 <= r <= 0x9E or 0xA0 <= r <= 0xA6 or 0xB0 <= r <= 0xB6:
            return self._channel(bank, r & 3)
        return None

    def _write_op(self, ch: int, idx: int, page: int, val: int) -> None:
        o = replace(self.ops[ch][idx])
        if page == 0x30:
            o.dt_mul = val
        elif page == 0x40:
            o.tl = val & 0x7F
        elif page == 0x50:
            o.ks_ar = val
        elif page == 0x60:
            o.am_d1r = val
        elif page == 0x70:
            o.sr = val & 0x1F
        elif page == 0x80:
            o.d1l_rr = val
        elif page == 0x90:
            o.ssg_eg = val & 0x0F
        self.ops[ch][idx] = o

    def write(self, reg: int, val: int) -> None:
        bank = 1 if reg & 0x100 else 0
        r = reg & 0xFF
        if not bank:
            if r == 0x22:
                self.lfo = val
                return
            if r == 0x27:
                self.mode = (val >> 6) & 0x03
                return
            if r == 0x28:
                ch = self.kon_map.get(val & 7)
                if ch is not None:
                    self.op_mask[ch] = (val >> 4) & 0x0F
                return
            if 0x10 <= r <= 0x1F:
                self._write_adpcm_b(r, val)
                return
        else:
            if r <= 0x2D:
                self._write_adpcm_a(r, val)
                return
        if 0x30 <= r <= 0x9E:
            ch = self._channel(bank, r & 3)
            if ch is not None:
                self._write_op(ch, (r >> 2) & 3, r & 0xF0, val)
            return
        if 0xA0 <= r <= 0xA6:
            ch = self._channel(bank, r & 3)
            if ch is None:
                return
            if r < 0xA4:
                hi = self._hi[ch]
                if hi is not None:
                    self.fnum[ch] = val | ((hi & 0x07) << 8)
                    self.block[ch] = (hi >> 3) & 0x07
                    self._hi[ch] = None
            else:
                self._hi[ch] = val
            return
        if not bank and 0xA8 <= r <= 0xAE:
            idx = r & 3
            if idx < 3:
                if r < 0xAC:
                    hi = self._special_hi[idx]
                    if hi is not None:
                        self.special[idx] = val | ((hi & 0x07) << 8)
                        self.special_block[idx] = (hi >> 3) & 0x07
                        self.special_set[idx] = True
                        self._special_hi[idx] = None
                else:
                    self._special_hi[idx] = val
            return
        if 0xB0 <= r <= 0xB2:
            ch = self._channel(bank, r & 3)
            if ch is not None:
                self.fb_alg[ch] = val
            return
        if 0xB4 <= r <= 0xB6:
            ch = self._channel(bank, r & 3)
            if ch is not None:
                self.pan_pms_ams[ch] = val
            return

    def _write_adpcm_a(self, r: int, val: int) -> None:
        if r == 0x00:
            self.a_control = val
        elif r == 0x01:
            self.a_master = val & 0x3F
        elif 0x08 <= r <= 0x0D:
            ch = r - 0x08
            self.a_vol[ch] = val & 0x1F
            self.a_pan[ch] = (val >> 6) & 0x03
        elif 0x10 <= r <= 0x15:
            ch = r - 0x10
            self.a_start[ch] = (self.a_start[ch] & 0xFF00) | val
        elif 0x18 <= r <= 0x1D:
            ch = r - 0x18
            self.a_start[ch] = (self.a_start[ch] & 0x00FF) | (val << 8)
        elif 0x20 <= r <= 0x25:
            ch = r - 0x20
            self.a_end[ch] = (self.a_end[ch] & 0xFF00) | val
        elif 0x28 <= r <= 0x2D:
            ch = r - 0x28
            self.a_end[ch] = (self.a_end[ch] & 0x00FF) | (val << 8)

    def _write_adpcm_b(self, r: int, val: int) -> None:
        if r == 0x10:
            self.b_control = val
        elif r == 0x11:
            self.b_pan = (val >> 6) & 0x03
        elif r == 0x12:
            self.b_start = (self.b_start & 0xFF00) | val
        elif r == 0x13:
            self.b_start = (self.b_start & 0x00FF) | (val << 8)
        elif r == 0x14:
            self.b_end = (self.b_end & 0xFF00) | val
        elif r == 0x15:
            self.b_end = (self.b_end & 0x00FF) | (val << 8)
        elif r == 0x19:
            self.b_freq = (self.b_freq & 0xFF00) | val
        elif r == 0x1A:
            self.b_freq = (self.b_freq & 0x00FF) | (val << 8)
        elif r == 0x1B:
            self.b_vol = val
        elif r == 0x1C:
            self.b_flags = val

    def frequency(self, ch: int) -> tuple[int, int]:
        if ch == 2 and self.multi_freq:
            for idx in range(3):
                if self.special_set[idx]:
                    return self.special_block[idx], self.special[idx]
        return self.block[ch], self.fnum[ch]

    def frequency_op(self, ch: int, op: int) -> tuple[int, int]:
        if ch == 2 and self.multi_freq and 0 <= op < 3 and self.special_set[op]:
            return self.special_block[op], self.special[op]
        return self.block[ch], self.fnum[ch]

    def snapshot(self, ch: int, mask: int = 0xF) -> FMPatch:
        fb_alg = self.fb_alg[ch]
        b4 = self.pan_pms_ams[ch]
        ops = []
        for i, op in enumerate(self.ops[ch]):
            op = replace(op)
            if not mask & (1 << OPN_OP_TO_MASK[i]):
                op.tl = 0x7F
            ops.append(op)
        return FMPatch(
            alg=fb_alg & 0x07,
            fb=(fb_alg >> 3) & 0x07,
            ams=(b4 >> 4) & 0x03,
            pms=b4 & 0x07,
            ops=tuple(ops),  # type: ignore[arg-type]
            opn=True,
        )

    def pan_byte_ch(self, ch: int) -> int:
        b4 = self.pan_pms_ams[ch]
        return pan_byte(((1 if b4 & 0x80 else 0) << 1) | (1 if b4 & 0x40 else 0))

    def carrier_tl(self, ch: int) -> int:
        alg = self.fb_alg[ch] & 0x07
        carriers = OPN_CARRIERS.get(alg, (3,))
        return min(self.ops[ch][i].tl for i in carriers)

    def adpcm_a_level(self, ch: int) -> int:
        """Effective 5-bit level with the master attenuation folded in."""
        atten = 0x1F - (self.a_vol[ch] & 0x1F)
        atten += (0x3F - (self.a_master & 0x3F)) * 0x1F // 0x3F
        return max(0, 0x1F - min(0x1F, atten))


@dataclass
class AdpcmAHit:
    sample_time: int
    chip_id: int
    channel: int
    start: int
    end: int
    vol: int
    pan: int
    end_time: int = 0


@dataclass
class AdpcmASample:
    chip_id: int
    start: int
    end: int
    data: bytes
    hits: list[AdpcmAHit] = field(default_factory=list)


@dataclass
class AdpcmBHit:
    sample_time: int
    chip_id: int
    start: int
    end: int
    rate: int
    vol: int
    pan: int
    loop: bool
    end_time: int = 0


@dataclass
class AdpcmBSample:
    chip_id: int
    start: int
    end: int
    rate: int
    data: bytes
    hits: list[AdpcmBHit] = field(default_factory=list)


def _rom_slice(rom: bytes, start: int, end: int) -> bytes:
    """One 256-byte-aligned ADPCM range, padded to the format's block size."""
    if not rom:
        return b""
    start &= ~0xFF
    end = (end & ~0xFF) | 0xFF
    if end < start:
        end = start + 0xFF
    data = rom[start:min(len(rom), end + 1)]
    if len(data) < 256:
        data += b"\x80" * (256 - len(data))
    if len(data) & 0xFF:
        data += b"\x80" * (0x100 - (len(data) & 0xFF))
    return bytes(data)


def collect_ym2610_adpcm(
    vgm, rom: bytes, rom_b: bytes | None = None,
) -> tuple[list[AdpcmASample], list[AdpcmBSample], list[str]]:
    """Walk the YM2610 register stream and build ADPCM-A/B sample lists.

    A uses the 0x82 ROM.  B uses the 0x83 ROM when that block carries data
    at the selected range, else 0x82.  Rips differ here: some put both banks
    in 0x82, others put the ADPCM-B bank in 0x83.
    """
    if rom_b is None:
        rom_b = rom
    warnings: list[str] = []
    a_st = YM2610State(vgm.ym2610_b)
    b_st = YM2610State(vgm.ym2610_b)
    a_active: dict[tuple[int, int], AdpcmAHit] = {}
    b_active: dict[int, AdpcmBHit] = {}
    a_hits: list[AdpcmAHit] = []
    b_hits: list[AdpcmBHit] = []

    for w in vgm.writes:
        if w.chip != "ym2610":
            continue
        reg, val = w.reg, w.val
        bank = 1 if reg & 0x100 else 0
        r = reg & 0xFF
        if bank:
            a_st.write(reg, val)
            # Cyber-bat style drivers write the real channel level a few
            # samples after the key-on (0xC0 at the gate, 0xC8 shortly after).
            # Update the active hit for that short window so the note carries
            # the audible level rather than the pre-gate mute.
            if 0x08 <= r <= 0x0D:
                ch = r - 0x08
                hit = a_active.get((w.chip_id, ch))
                if hit is not None and 0 <= w.sample - hit.sample_time <= 64:
                    hit.vol = a_st.adpcm_a_level(ch)
                    hit.pan = pan_byte(a_st.a_pan[ch])
            elif r == 0x01:
                for (cid, ch), hit in a_active.items():
                    if cid == w.chip_id and 0 <= w.sample - hit.sample_time <= 64:
                        hit.vol = a_st.adpcm_a_level(ch)
            if r != 0x00:
                continue
            start_bits = val & 0x3F
            stop = bool(val & 0x80)
            if stop:
                for ch in range(6):
                    if start_bits & (1 << ch):
                        hit = a_active.pop((w.chip_id, ch), None)
                        if hit is not None:
                            hit.end_time = w.sample
                continue
            # A non-stop 0x100 write retriggers every selected channel, even
            # one that is already sounding.  Furnace's export writes combined
            # masks (0x3, 0x6) on one row, so a 0->1 test would lose them.
            for ch in range(6):
                if not (start_bits & (1 << ch)):
                    continue
                old = a_active.pop((w.chip_id, ch), None)
                if old is not None:
                    old.end_time = w.sample
                start = a_st.a_start[ch] << 8
                end = (a_st.a_end[ch] << 8) | 0xFF
                hit = AdpcmAHit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    channel=ch,
                    start=start,
                    end=end,
                    vol=a_st.adpcm_a_level(ch),
                    pan=pan_byte(a_st.a_pan[ch]),
                )
                a_hits.append(hit)
                a_active[(w.chip_id, ch)] = hit
            continue

        b_st.write(reg, val)
        # The driver commonly writes the pan (and sometimes the level) two
        # samples after the start bit.  Fold those into the new hit.
        hit = b_active.get(w.chip_id)
        if hit is not None and 0 <= w.sample - hit.sample_time <= 64:
            if r == 0x11:
                hit.pan = pan_byte(b_st.b_pan)
            elif r in (0x19, 0x1A):
                # Most drivers set the rate a few samples after the start bit.
                hit.rate = b_st.b_freq
            elif r == 0x1B:
                hit.vol = b_st.b_vol
        if r != 0x10:
            continue
        start = bool(val & 0x80)
        # Bit 0 resets Start/Repeat; bit 6 is Repeat.
        reset = bool(val & 0x01)
        if not start or reset:
            hit = b_active.pop(w.chip_id, None)
            if hit is not None:
                hit.end_time = w.sample
            b_prev = val
            continue
        old = b_active.pop(w.chip_id, None)
        if old is not None:
            old.end_time = w.sample
        hit = AdpcmBHit(
            sample_time=w.sample,
            chip_id=w.chip_id,
            start=b_st.b_start << 8,
            end=(b_st.b_end << 8) | 0xFF,
            rate=b_st.b_freq,
            vol=b_st.b_vol,
            pan=pan_byte(b_st.b_pan),
            loop=bool(val & 0x40),
        )
        b_hits.append(hit)
        b_active[w.chip_id] = hit
        b_prev = val

    end = vgm.total_samples or 0
    for hit in a_active.values():
        hit.end_time = end
    for hit in b_active.values():
        hit.end_time = end

    a_samples: dict[tuple[int, int, int], AdpcmASample] = {}
    if rom:
        for hit in a_hits:
            key = (hit.chip_id, hit.start, hit.end)
            smp = a_samples.get(key)
            if smp is None:
                data = _rom_slice(rom, hit.start, hit.end)
                smp = AdpcmASample(hit.chip_id, hit.start, hit.end, data)
                a_samples[key] = smp
            smp.hits.append(hit)

    b_samples: dict[tuple[int, int, int, int], AdpcmBSample] = {}
    for hit in b_hits:
        key = (hit.chip_id, hit.start, hit.end, hit.rate)
        smp = b_samples.get(key)
        if smp is None:
            data = _rom_slice(rom_b, hit.start, hit.end)
            if not any(data) and rom_b is not rom:
                data = _rom_slice(rom, hit.start, hit.end)
            smp = AdpcmBSample(hit.chip_id, hit.start, hit.end, hit.rate, data)
            b_samples[key] = smp
        smp.hits.append(hit)

    if a_hits and not rom:
        warnings.append(
            "YM2610 ADPCM-A key-ons but no ADPCM ROM; sample channels are silent"
        )
    if b_hits and not rom and not rom_b:
        warnings.append(
            "YM2610 ADPCM-B key-ons but no ADPCM ROM; sample channels are silent"
        )
    return list(a_samples.values()), list(b_samples.values()), warnings
