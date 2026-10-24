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

# Carrier sets in register order (op1..op4).  YM2151 and YM2203 use the same
# eight algorithms: 0-3 are chains ending on op4, 4 splits into two pairs,
# 5 and 6 add op1-fed carriers, 7 is additive.
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
        """
        op_keys = tuple(self.ops[i].as_tuple(include_tl=True) for i in range(4))
        return (self.alg & 7, self.fb & 7, self.opn, op_keys)

    def carrier_tl(self) -> int:
        tls = [self.ops[i].tl & 0x7F for i in CARRIERS.get(self.alg & 7, (3,))]
        return min(tls) if tls else 0x7F
