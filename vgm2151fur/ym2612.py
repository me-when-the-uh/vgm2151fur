"""YM2612 (OPN2) support: pitch mapping and the six-channel FM state.

The YM2612 is the six-channel OPN2 core: the same operator register layout
as the YM2203's FM half (0x70 is the sustain rate, 0x90 is SSG-EG, there is
no DT2), plus a second register bank for channels 4-6, a stereo/LFO
sensitivity register and one DAC channel.

Furnace runs its YM2612 platform in linear-pitch mode.  It uses
``DivPlatformGenesis``'s ``fmFreqBase`` of 9437184, twice the OPN value, so
the same pattern note number is one octave above a YM2203's.  The helpers
here invert the platform's own note conversion, not a generic OPN formula,
so the exported f-num/block is the source's.
"""

from __future__ import annotations

import math
from dataclasses import replace

from vgm2151fur.fm import FMPatch, OpRegs, OPN_CARRIERS, OPN_OP_TO_MASK

YM2612_DEFAULT_CLOCK = 7670453

# DivPlatformGenesis's fmFreqBase.  ``CONVERT_FNUM_BLOCK`` in Furnace turns
# a linear-pitch note into ``round(fbase * (divider / clock))`` and shifts
# the note's block out of it.
YM2612_FREQ_BASE = 9437184.0

_FNUM_CACHE: dict[tuple[int, int, int, float], tuple[int, int]] = {}
_NOTE_CACHE: dict[tuple[int, int, int, float], tuple[int, int]] = {}
_MAX_FNUM = 2047

# Furnace's ext columns write their frequency to operator (1,2,0,3)[c]:
# column 0 -> 0xA9/0xAD, column 1 -> 0xAA/0xAE, column 2 -> 0xA8/0xAC,
# column 3 -> the normal channel f-num.  The state stores the three
# supplementary pairs by (register & 3), so 0 = 0xA8/0xAC, 1 = 0xA9/0xAD,
# 2 = 0xAA/0xAE, 3 = normal.
EXT_FREQ_OP = (1, 2, 0, 3)


def ym2612_note_fnum(
    note: int, clock: int, e5: int = 0x80, freq_base: float = YM2612_FREQ_BASE,
) -> tuple[int, int]:
    """Furnace's (block, fnum) for a pattern note and E5 byte.

    A literal port of ``NOTE_FNUM_BLOCK(note, 11, 0)`` through
    ``calcFreq`` with the platform's frequency base: the E5 value joins the
    note in ``nbase`` before the exponential, which is also why negative fine
    tune can move the block down.  The YM2610 shares the formula with a
    different base.
    """
    note = max(0, min(179, int(note)))
    clock = clock or YM2612_DEFAULT_CLOCK
    key = (note, clock, e5, freq_base)
    hit = _FNUM_CACHE.get(key)
    if hit is not None:
        return hit
    nbase = note * 128 + (max(0, min(255, int(e5))) - 0x80)
    fbase = 440.0 * 2.0 ** ((nbase + 384 - 7680) / 1536.0)
    # The module keeps Furnace's default linear pitch mode, where the note is
    # converted with round() rather than the legacy f-num path's truncation.
    bf = int(round(fbase * (freq_base / clock)))
    nnote = nbase >> 7
    block = (nnote - 60) // 12
    block = max(0, min(7, block))
    bf >>= block
    if bf < 0:
        bf = 0
    boundary_bottom = int(440.0 * 2.0 ** 0.25 * (freq_base / clock))
    boundary_top = int(2.0 * 440.0 * 2.0 ** 0.25 * (freq_base / clock))
    # CONVERT_FNUM_BLOCK scales both boundaries down until the top one fits
    # the 11-bit f-num; the block shift then chooses the same octave.
    while boundary_top > _MAX_FNUM:
        boundary_top >>= 1
        boundary_bottom >>= 1
    while bf > 0 and bf < boundary_bottom and block > 0:
        bf <<= 1
        block -= 1
    if bf > boundary_top:
        while block < 7 and bf > boundary_top:
            bf >>= 1
            block += 1
        if bf > _MAX_FNUM:
            bf = _MAX_FNUM
    out = (block, bf)
    _FNUM_CACHE[key] = out
    return out


def ym2612_note_for(
    block: int, fnum: int, clock: int, freq_base: float = YM2612_FREQ_BASE,
) -> tuple[int, int]:
    """Source (block, fnum) -> (pattern note, E5 fine-tune byte).

    The note search uses the platform's forward mapping, so the pair
    (note, E5) reproduces the source's frequency to the E5 resolution of
    1/128 semitone (one semitone of range).  The YM2610 passes its own
    frequency base.
    """
    clock = clock or YM2612_DEFAULT_CLOCK
    block = max(0, min(7, int(block)))
    fnum = max(1, min(_MAX_FNUM, int(fnum)))
    key = (block, fnum, clock, freq_base)
    hit = _NOTE_CACHE.get(key)
    if hit is not None:
        return hit
    best: tuple[float, int, int, int] | None = None
    want = ym2612_pitch_units(block, fnum)
    for note in range(180):
        b, f = ym2612_note_fnum(note, clock, freq_base=freq_base)
        if f <= 0:
            continue
        # fnum the source would have at this note's block
        target = fnum * 2.0 ** (block - b)
        if target <= 0:
            continue
        # Linear pitch mode: E5 is 1/128 semitone and goes through the same
        # exponential mapping as the note.
        off = int(round(1536.0 * math.log2(target / f)))
        off = max(-128, min(127, off))
        # Score the pair through the platform's own formula, not the
        # un-rounded ratio, so the exported byte is the predicted one.
        ab, af = ym2612_note_fnum(note, clock, 0x80 + off, freq_base)
        err = abs(ym2612_pitch_units(ab, af) - want)
        cand = (err, abs(off), note, off)
        if best is None or cand < best:
            best = cand
    assert best is not None
    _err, _a, note, off = best
    out = (note, 0x80 + off)
    _NOTE_CACHE[key] = out
    return out


def ym2612_pitch_units(block: int, fnum: int) -> float:
    """Pitch in 1/64 semitones; only differences are meaningful."""
    if fnum <= 0:
        fnum = 1
    return 768.0 * (math.log2(fnum) + block)


def _blank_op() -> OpRegs:
    # A YM2612 powers up with every register at zero.  A driver that writes
    # only part of a patch must not inherit an audible OPM default.
    return OpRegs(
        dt_mul=0, tl=0, ks_ar=0, am_d1r=0, dt2_d2r=0, d1l_rr=0,
        sr=0, ssg_eg=0,
    )


class YM2612State:
    """Register image of one YM2612."""

    def __init__(self) -> None:
        self.fnum = [0] * 6
        self.block = [0] * 6
        self.fb_alg = [0] * 6
        # 0xB4: L/R output, AMS (bits 4-5), PMS (bits 0-2).  ymfm resets the
        # OPN2 to 0xC0 (both outputs on).
        self.pan_pms_ams = [0xC0] * 6
        self.ops: list[list[OpRegs]] = [[_blank_op() for _ in range(4)] for _ in range(6)]
        self.kon = [False] * 6
        self.op_mask = [0] * 6
        self.mode = 0  # 0x27 bits 6-7: 0 normal, 1 special, 2 CSM
        self.lfo = 0  # 0x22
        self.dac_enabled = False
        self.dac_data = 0
        # The frequency registers are latched pairs: the high byte is held
        # until a low write commits it (ymfm's opn register model).
        self._hi: list[int | None] = [None] * 6
        # Channel 3's supplementary frequencies (0xA8/AC, 0xA9/AD, 0xAA/AE).
        self.special = [0] * 3
        self.special_block = [0] * 3
        self.special_set = [False] * 3
        self._special_hi: list[int | None] = [None] * 3

    @property
    def multi_freq(self) -> bool:
        """True in channel-3 special mode and in CSM mode (ymfm's rule)."""
        return self.mode in (1, 2)

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
            # OPN 0x70 is the 5-bit sustain rate.
            o.sr = val & 0x1F
        elif page == 0x80:
            o.d1l_rr = val
        elif page == 0x90:
            o.ssg_eg = val & 0x0F
        self.ops[ch][idx] = o

    def write(self, reg: int, val: int) -> None:
        bank = 0x100 if reg & 0x100 else 0
        r = reg & 0xFF
        if not bank:
            if r == 0x22:
                self.lfo = val
                return
            if r == 0x27:
                self.mode = (val >> 6) & 0x03
                return
            if r == 0x2A:
                self.dac_data = val
                return
            if r == 0x2B:
                self.dac_enabled = bool(val & 0x80)
                return
        if 0x30 <= r <= 0x9E:
            ch = (r & 0x03) + (3 if bank else 0)
            if (r & 0x03) == 3:
                return
            self._write_op(ch, (r >> 2) & 0x03, r & 0xF0, val)
            return
        if 0xA0 <= r <= 0xA6:
            ch = (r & 0x03) + (3 if bank else 0)
            if (r & 0x03) == 3:
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
            idx = r & 0x03
            if idx == 3:
                return
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
            ch = (r - 0xB0) + (3 if bank else 0)
            self.fb_alg[ch] = val
            return
        if 0xB4 <= r <= 0xB6:
            ch = (r - 0xB4) + (3 if bank else 0)
            self.pan_pms_ams[ch] = val
            return

    def frequency(self, ch: int) -> tuple[int, int]:
        """The channel f-num/block; in special mode channel 3's first pair."""
        if ch == 2 and self.multi_freq:
            for idx in range(3):
                if self.special_set[idx]:
                    return self.special_block[idx], self.special[idx]
        return self.block[ch], self.fnum[ch]

    def frequency_op(self, ch: int, op: int) -> tuple[int, int]:
        """(block, fnum) for frequency group ``op`` of a special channel-3.

        Group 0 is 0xA8/0xAC, 1 is 0xA9/0xAD, 2 is 0xAA/0xAE and 3 is the
        normal channel f-num.  Falls back to the normal frequency until the
        driver has written that pair.
        """
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

    def pan_byte(self, ch: int) -> int:
        b4 = self.pan_pms_ams[ch]
        left = bool(b4 & 0x80)
        right = bool(b4 & 0x40)
        if left and right:
            return 0xFF
        if left:
            return 0xF0
        if right:
            return 0x0F
        return 0x00

    def carrier_tl(self, ch: int) -> int:
        alg = self.fb_alg[ch] & 0x07
        carriers = OPN_CARRIERS.get(alg, (3,))
        return min(self.ops[ch][i].tl for i in carriers)
