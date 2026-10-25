"""Shared FM patch model for the OPM (YM2151) and OPN (YM2203) paths.

Both chips have four operators per channel and the same eight connection
algorithms, so the note events, the instrument list and the .fur FM feature
carry one model.  The fields that differ are the register meaning of the
per-operator bytes: OPM keeps DT2 and D2R in one register, OPN has a separate
sustain rate (0x70) and SSG-EG (0x90), and no DT2.  ``FMPatch.opn`` tells the
writer which layout to emit.
"""

from __future__ import annotations

from dataclasses import dataclass

# YM2151 (OPM) carrier sets in its register order (0x40/0x48/0x50/0x58).
# Algorithms 0-3 are chains ending on op4, 4 splits into two pairs, 5 and 6
# add op1-fed carriers, 7 is additive.
CARRIERS = {
    0: (3,),
    1: (3,),
    2: (3,),
    3: (3,),
    4: (1, 3),
    5: (1, 2, 3),
    6: (1, 2, 3),
    7: (0, 1, 2, 3),
}

# OPN (YM2203/YM2612) carrier sets in *register* order.  The OPN register
# pages are S1, S3, S2, S4 (0x30/0x34/0x38/0x3C), while the datasheet's
# algorithm diagrams use S1..S4.  Furnace's Genesis/OPN platform uses the
# same register order (opOffs 0,4,8,12) and its isOutput table is:
#   alg 4 -> S2, S4 = registers 0x38, 0x3C -> indices 2, 3
# Using the OPM/datasheet-order pair (1,3) here normalizes S3 and S4 and
# leaves the real carrier S2 at full level, which changes both gain and the
# modulator/carrier balance of every algorithm-4 voice.
OPN_CARRIERS = {
    0: (3,),
    1: (3,),
    2: (3,),
    3: (3,),
    4: (2, 3),
    5: (1, 2, 3),
    6: (1, 2, 3),
    7: (0, 1, 2, 3),
}


def carriers_for(alg: int, opn: bool) -> tuple[int, ...]:
    return (OPN_CARRIERS if opn else CARRIERS).get(alg & 7, (3,))


# The 0x28 KON mask bits follow the datasheet operator order S1,S2,S3,S4,
# while DivInstrumentFM/OpRegs store the operators in register order
# S1,S3,S2,S4.  Index by *register* operator: which mask bit enables it.
OPN_OP_TO_MASK = (0, 2, 1, 3)


@dataclass
class OpRegs:
    dt_mul: int = 0x01
    tl: int = 0x7F
    ks_ar: int = 0x1F
    am_d1r: int = 0x00
    dt2_d2r: int = 0x00
    d1l_rr: int = 0xF7
    # YM2203 only: register 0x70 (sustain rate) and 0x90 (SSG-EG).  Kept in
    # the identity so a driver that switches an envelope shape starts a new
    # instrument.
    sr: int = 0
    ssg_eg: int = 0

    def as_tuple(self, include_tl: bool) -> tuple:
        if include_tl:
            return (
                self.dt_mul, self.tl, self.ks_ar, self.am_d1r,
                self.dt2_d2r, self.d1l_rr, self.sr, self.ssg_eg,
            )
        return (
            self.dt_mul, self.ks_ar, self.am_d1r, self.dt2_d2r,
            self.d1l_rr, self.sr, self.ssg_eg,
        )


@dataclass
class FMPatch:
    alg: int
    fb: int
    ams: int
    pms: int
    ops: tuple[OpRegs, OpRegs, OpRegs, OpRegs]  # op1..op4 (register order)
    name: str = ""
    uses: int = 0
    fms_macro: tuple[int, ...] = ()
    ams_macro: tuple[int, ...] = ()
    macro_speed: int = 1
    # True for a YM2203 (OPN) instrument: no DT2, the 0x70 register is the
    # sustain rate, and 0x90 is SSG-EG.
    opn: bool = False
    # Per-note pitch motion for OPN: absolute pitch2 values in 1/128
    # semitones (the module uses Furnace's default linear pitch mode).
    pitch_macro: tuple[int, ...] = ()
    pitch_speed: int = 1
    pitch_loop: int = 255

    def identity(self) -> tuple:
        """Cluster key: full timbre including carrier TLs.

        Furnace applies volume on a log curve, so a stripped TL cannot be
        reconstructed from pattern volume. Keep TL on the instrument.
        OPN also keeps PMS/AMS: the platform writes 0xB4 from the instrument
        at note-on only when the instrument changes, so two patches that
        differ only in LFO sensitivity must not merge.
        """
        op_keys = tuple(self.ops[i].as_tuple(include_tl=True) for i in range(4))
        if self.opn:
            return (self.alg & 7, self.fb & 7, self.opn, self.ams, self.pms, op_keys)
        return (self.alg & 7, self.fb & 7, self.opn, op_keys)

    def carriers(self) -> tuple[int, ...]:
        return carriers_for(self.alg & 7, self.opn)

    def carrier_tl(self) -> int:
        tls = [self.ops[i].tl & 0x7F for i in self.carriers()]
        return min(tls) if tls else 0x7F
