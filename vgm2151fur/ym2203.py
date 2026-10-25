"""YM2203 (OPN) support: pitch mapping and the three-channel FM state.

The YM2203's FM half is the three-channel OPN core: the same register layout
as the YM2612 without the DAC and with registers 0x70 (sustain rate) and 0x90
(SSG-EG) instead of the OPM's DT2.  Furnace runs the YM2203 platform in
linear-pitch mode; a pattern note becomes ``note << 7`` engine units, E5 adds
1/128-semitone units, and the platform converts that to block/fnum.  The
helpers here invert that mapping, so a converted note writes back the source's
frequency register.
"""

from __future__ import annotations

import math
from dataclasses import replace

from vgm2151fur.fm import FMPatch, OpRegs, OPN_CARRIERS, OPN_OP_TO_MASK

YM2203_DEFAULT_CLOCK = 3579545
YM2203_SSG_DIVIDER = 16

# DivPlatformOPN's fmFreqBase (144 * 32768, rounded by the OPN's nominal
# 4.72 MHz rate).
OPN_FREQ_BASE = 4720270.0

_FNUM_CACHE: dict[tuple[int, int], tuple[int, int]] = {}
_NOTE_CACHE: dict[tuple[int, int, int], tuple[int, int]] = {}
_MAX_FNUM = 2047


def opn_note_fnum(note: int, clock: int) -> tuple[int, int]:
    """Furnace's (block, fnum) for a pattern note on this clock.

    A literal port of the ``NOTE_FNUM_BLOCK(note, 11, 0)`` macro: the note
    picks the block, the OPN frequency base turns it into an 11-bit fnum and
    the boundary cases move the block.
    """
    note = max(0, min(179, int(note)))
    clock = clock or YM2203_DEFAULT_CLOCK
    key = (note, clock)
    hit = _FNUM_CACHE.get(key)
    if hit is not None:
        return hit
    base = 440.0 * 2.0 ** ((note - 60 + 3) / 12.0)
    # The module keeps Furnace's default linear pitch mode, where the note is
    # converted with round() rather than the legacy f-num path's truncation.
    bf = int(round(base * (OPN_FREQ_BASE / clock)))
    block = (note - 60) // 12
    block = max(0, min(7, block))
    bf >>= block
    if bf < 0:
        bf = 0
    boundary_bottom = int(440.0 * 2.0 ** 0.25 * (OPN_FREQ_BASE / clock))
    boundary_top = int(2.0 * 440.0 * 2.0 ** 0.25 * (OPN_FREQ_BASE / clock))
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


def opn_note_for(block: int, fnum: int, clock: int) -> tuple[int, int]:
    """Source (block, fnum) -> (pattern note, E5 fine-tune byte).

    The note search uses the same forward mapping Furnace uses, so the pair
    (note, E5) reproduces the source's frequency to the E5 resolution of
    1/128 semitone.
    """
    clock = clock or YM2203_DEFAULT_CLOCK
    block = max(0, min(7, int(block)))
    fnum = max(1, min(_MAX_FNUM, int(fnum)))
    key = (block, fnum, clock)
    hit = _NOTE_CACHE.get(key)
    if hit is not None:
        return hit
    best: tuple[float, int, int, int] | None = None
    for note in range(180):
        b, f = opn_note_fnum(note, clock)
        if f <= 0:
            continue
        # fnum the source would have at this note's block
        target = fnum * 2.0 ** (block - b)
        if target <= 0:
            continue
        # The module keeps Furnace's default (linear) pitch mode, where E5 is
        # 1/128 semitone and the OPN platform feeds it through the same
        # exponential mapping as a pattern note.  Convert the residual ratio
        # to those units instead of adding raw f-num steps.
        off = int(round(1536.0 * math.log2(target / f)))
        off = max(-128, min(127, off))
        actual = f * 2.0 ** (off / 1536.0)
        err = abs(math.log2(actual / target))
        cand = (err, abs(off), note, off)
        if best is None or cand < best:
            best = cand
    assert best is not None
    _err, _a, note, off = best
    out = (note, 0x80 + off)
    _NOTE_CACHE[key] = out
    return out


def opn_pitch_units(block: int, fnum: int) -> float:
    """Pitch in 1/64 semitones; only differences are meaningful."""
    if fnum <= 0:
        fnum = 1
    return 768.0 * (math.log2(fnum) + block)


class YM2203State:
    """Register image of one YM2203's FM half."""

    def __init__(self) -> None:
        self.fb_alg = [0] * 3
        self.fnum = [0] * 3
        self.block = [0] * 3
        # A YM2203 powers up with all FM registers at zero.  This differs from
        # the OPM patch defaults (TL 0x7F, RR 0xF7) and matters when a driver
        # writes only part of a patch: hardware AR=0 is silent, while a stale
        # OPM default would make the operator audible.
        blank = OpRegs(dt_mul=0, tl=0, ks_ar=0, am_d1r=0,
                       dt2_d2r=0, d1l_rr=0, sr=0, ssg_eg=0)
        self.ops: list[list[OpRegs]] = [[blank for _ in range(4)] for _ in range(3)]
        self.kon = [False] * 3
        self.op_mask = [0] * 3
        # Channel 3 can switch to per-operator frequencies (3-slot mode,
        # 0x27 bit 6/7 plus 0xA8-0xAE).  Furnace's six-voice system has one
        # note per channel, so the converter uses one of the three extra
        # operators as the channel pitch and reports the approximation.
        self.special = False
        self.special_lo = [0, 0, 0]
        self.special_hi = [0, 0, 0]
        self.special_set = [False, False, False]

    @staticmethod
    def _op_index(reg: int) -> tuple[int, int] | None:
        """(channel, operator) for the 0x30-0x9E operator pages."""
        if not 0x30 <= reg <= 0x9E:
            return None
        ch = reg & 3
        if ch >= 3:
            return None
        return ch, (reg >> 2) & 3

    def write(self, reg: int, val: int) -> None:
        op = self._op_index(reg)
        if op is not None:
            ch, idx = op
            o = replace(self.ops[ch][idx])
            page = reg & 0xF0
            if page == 0x30:
                o.dt_mul = val
            elif page == 0x40:
                o.tl = val
            elif page == 0x50:
                o.ks_ar = val
            elif page == 0x60:
                o.am_d1r = val
            elif page == 0x70:
                # On OPN 0x70 is the sustain rate; the E5 field in the
                # instrument carries the raw 5-bit register.
                o.sr = val & 0x1F
            elif page == 0x80:
                o.d1l_rr = val
            elif page == 0x90:
                o.ssg_eg = val & 0x0F
            self.ops[ch][idx] = o
            return
        if reg == 0x27:
            # Bit 6/7 select CSM / 3-slot mode.  Only the per-operator
            # frequency part changes the pitch model here.
            self.special = bool(val & 0xC0)
            return
        if 0xA0 <= reg <= 0xA6:
            ch = reg & 3
            if ch < 3:
                if reg < 0xA4:
                    self.fnum[ch] = (self.fnum[ch] & 0x700) | (val & 0xFF)
                else:
                    self.fnum[ch] = (self.fnum[ch] & 0xFF) | ((val & 0x07) << 8)
                    self.block[ch] = (val >> 3) & 0x07
            return
        if 0xA8 <= reg <= 0xAE:
            idx = reg & 3
            if idx < 3:
                if reg <= 0xAA:
                    self.special_lo[idx] = val & 0xFF
                else:
                    self.special_hi[idx] = val
                self.special_set[idx] = True
            return
        if 0xB0 <= reg <= 0xB2:
            self.fb_alg[reg - 0xB0] = val

    def frequency(self, ch: int) -> tuple[int, int]:
        if ch == 2 and self.special:
            for idx in range(3):
                if self.special_set[idx]:
                    hi = self.special_hi[idx]
                    return (hi >> 3) & 0x07, self.special_lo[idx] | (
                        (hi & 0x07) << 8
                    )
        return self.block[ch], self.fnum[ch]

    def frequency_op(self, ch: int, op: int) -> tuple[int, int]:
        """(block, fnum) for one operator of a 3-slot channel-3 voice.

        Falls back to the normal channel frequency until the driver has
        written that operator's per-operator registers.
        """
        if ch == 2 and self.special and 0 <= op < 3 and self.special_set[op]:
            hi = self.special_hi[op]
            return (hi >> 3) & 0x07, self.special_lo[op] | (
                (hi & 0x07) << 8
            )
        return self.block[ch], self.fnum[ch]

    def snapshot(self, ch: int, mask: int = 0xF) -> FMPatch:
        fb_alg = self.fb_alg[ch]
        ops = []
        for i, op in enumerate(self.ops[ch]):
            op = replace(op)
            if not mask & (1 << OPN_OP_TO_MASK[i]):
                op.tl = 0x7F
            ops.append(op)
        return FMPatch(
            alg=fb_alg & 0x07,
            fb=(fb_alg >> 3) & 0x07,
            ams=0,
            pms=0,
            ops=tuple(ops),  # type: ignore[arg-type]
            opn=True,
        )

    def carrier_tl(self, ch: int) -> int:
        alg = self.fb_alg[ch] & 0x07
        carriers = OPN_CARRIERS.get(alg, (3,))
        return min(self.ops[ch][i].tl for i in carriers)


def opn_carrier_octave(patch) -> int:
    """Octave of the loudest audible carrier, relative to the channel f-num.

    The pattern note should show the pitch that is actually heard.  Use the
    carrier with the lowest TL (the loudest); equally loud carriers tie-break
    to the highest power-of-two ratio, which keeps the historical MULT=2
    label on patches whose carriers are mixed at equal volume.  Unkeyed
    operators arrive here with TL=0x7F and are ignored.

    Return 0 when the loudest carrier is not an exact power of two (MULT
    3/5/6/...), or when every carrier is silent: the channel note already is
    the best label and nothing should move.
    """
    if not getattr(patch, "opn", False):
        return 0
    best_tl = 0x80
    best_octave: int | None = None
    for i in OPN_CARRIERS.get(patch.alg & 7, ()):
        op = patch.ops[i]
        tl = op.tl & 0x7F
        if tl >= 0x7F:
            continue
        mult = op.dt_mul & 0x0F
        ratio = 0.5 if mult == 0 else float(mult)
        octave = (
            int(round(math.log2(ratio)))
            if ratio in (0.5, 1.0, 2.0, 4.0, 8.0)
            else None
        )
        if tl < best_tl:
            best_tl = tl
            best_octave = octave
        elif tl == best_tl and octave is not None:
            if best_octave is None or octave > best_octave:
                best_octave = octave
    return best_octave or 0
