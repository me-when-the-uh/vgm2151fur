"""Shared FM patch model for the OPM (YM2151), OPN (YM2203) and OPL paths.

OPM and OPN have four operators per channel and the same eight connection
algorithms, so the note events, the instrument list and the .fur FM feature
carry one model.  The fields that differ are the register meaning of the
per-operator bytes: OPM keeps DT2 and D2R in one register, OPN has a separate
sustain rate (0x70) and SSG-EG (0x90), and no DT2.  ``FMPatch.opn`` tells the
writer which layout to emit.

The OPL family joins it with two operators per channel (four on an OPL3 4-op
pair) and its own register split, so ``FMPatch.opl``/``FMPatch.four`` pick the
third layout.  An OPL operator has no DT1/DT2/D2R and a 6-bit TL, so the
decoded OPL fields ride beside the raw OPM/OPN bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

# YM2151 (OPM) carrier sets in its register order ($40/$48/$50/$58 are op1..op4
# for one channel, so the index is the slot number minus one). Algorithms 0-3 are
# chains ending on op4, 5 and 6 add fed carriers, 7 is additive.
#
# Algorithm 4 outputs on op3 and op4. Measured, not assumed: across 49 YM2151
# rips (Batrider and Battle Garegga sets) every single mid-note TL automation in
# the corpus - the writes a driver makes to shape loudness while a note sounds -
# is on op3 and op4, paired exactly (138 writes each, always together; e.g.
# "Purple Twilight" walks $70/$78 through 39,37,35...1E for its build-up while
# op1 and op2 hold 1E). Automation lands on what the platform scales, so that
# pair is the carrier set. Getting it wrong freezes a voice at whatever level the
# instrument interned first: on "Purple Twilight" that is 0x39 - the quietest
# step of a crescendo - and with the volume column already at its maximum of 127
# nothing further can be added, so the entrance is inaudible.
#
# Note this differs from OPN's table for the same algorithm number even though
# both say (2, 3): OPN's operator array is in register order S1,S3,S2,S4 and
# OPM's is plain S1..S4, so OPN's (2,3) names slots S2,S4 while OPM's names
# S3,S4. See OPN_CARRIERS and OPM_OP_TO_MASK.
CARRIERS = {
    0: (3,),
    1: (3,),
    2: (3,),
    3: (3,),
    4: (2, 3),
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

# OPL/OPL2/OPL3 two-operator channels have one connection bit (0xC0 bit 0):
# 0 is FM (op1 modulates op2) and 1 is additive, so both operators sound.
# Furnace keeps the four-value field of its 4-op mode, and its isOutputL table
# lets algorithms 2 and 3 alias 0 and 1 on a two-operator voice (opl.cpp).
OPL_CARRIERS = {
    0: (1,),
    1: (0, 1),
    2: (1,),
    3: (0, 1),
}

# OPL3 4-op algorithms.  The instrument's operator order is not the chain
# order: commitState walks the slot rows with orderedOpsL (0,2,1,3), so row 3
# is instrument operator 3, row 2 is operator 1 and row 1 is operator 2.
# isOutputL[1] marks the rows that reach the output:
#   0 (1>2>3>4) row 3, 1 (1+(2>3>4)) rows 0 and 3,
#   2 ((1>2)+(3>4)) rows 1 and 3, 3 (1+(2>3)+4) rows 0, 2 and 3.
OPL4_CARRIERS = {
    0: (3,),
    1: (0, 3),
    2: (2, 3),
    3: (0, 1, 3),
}


def carriers_for(alg: int, opn: bool, opl: bool = False, four: bool = False) -> tuple[int, ...]:
    if opl:
        table = OPL4_CARRIERS if four else OPL_CARRIERS
        return table.get(alg & 3, (1,) if not four else (3,))
    return (OPN_CARRIERS if opn else CARRIERS).get(alg & 7, (3,))


# The 0x28 KON mask bits follow the datasheet operator order S1,S2,S3,S4,
# while DivInstrumentFM/OpRegs store the operators in register order
# S1,S3,S2,S4.  Index by *register* operator: which mask bit enables it.
OPN_OP_TO_MASK = (0, 2, 1, 3)

# Ext channel-3 layout: which ``ops[]`` entry an operator column plays.
# ``DivPlatformYM2203Ext::dispatch`` maps its four columns (channels 2..5) as
# ``int ch = c.chan - 2; int ordch = orderedOps[ch];`` and writes the volume
# column's scaling to ``chan[2].state.op[ordch]`` - so channel 3 (column 1)
# plays ``ops[2]``, not ``ops[1]``. ``orderedOps`` is {0,2,1,3} in
# fmsharedbase.h. ``DIV_CMD_VOLUME`` applies scaling unconditionally, so every
# column's level is reachable through its own volume (unlike ``DIV_CMD_FM_TL``,
# which gates on KVS). Same tuple as OPN_OP_TO_MASK, different meaning: these are
# operator indices per column rather than mask bits per operator.
OPN_ORDERED_OPS = (0, 2, 1, 3)

# The register-order operator a channel-3 TL write names: pages advance by four
# within the channel, so $42/$46/$4A/$4E are S1/S3/S2/S4 = ops[0..3].
def opn_tl_op(reg: int) -> int:
    return (reg >> 2) & 0x03

# The YM2151 key-on byte is twisted the same way: ``DivPlatformArcade::
# commitState`` composes it as ``(op[0]?1)|(op[2]?2)|(op[1]?4)|(op[3]?8)`` and
# writes ``($08 = opMask << 3 | channel)``, so bit1 of the logged mask enables
# operator 3 and bit2 enables operator 2. Its *register* pages are plain S1..S4
# (``$60/$68/$70/$78`` = TL of op1..op4), unlike OPN, but the mask->operator
# table is identical. Read as identity, a voice keyed on op1+op3 blanks op3 and
# keeps op2 - an instrument whose carriers all sit at TL 7F, i.e. silence.
OPM_OP_TO_MASK = (0, 2, 1, 3)


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
    # OPL family only (FMPatch.opl): the decoded register fields.  An OPL
    # operator has no DT1/DT2/D2R and a 6-bit TL, so these cannot share the
    # raw OPM/OPN bytes above.  They take part in the identity.
    am: int = 0     # 0x20 bit 7
    vib: int = 0    # 0x20 bit 6
    sus: int = 0    # 0x20 bit 5 (the period/hold flag, inverted)
    ksr: int = 0    # 0x20 bit 4
    mult: int = 1   # 0x20 bits 3-0
    ksl: int = 0    # 0x40 bits 7-6
    ar: int = 0     # 0x60 bits 7-4
    dr: int = 15    # 0x60 bits 3-0
    sl: int = 0     # 0x80 bits 7-4
    rr: int = 0     # 0x80 bits 3-0
    ws: int = 0     # 0xE0 (absent on the YM3526)

    def as_tuple(self, include_tl: bool) -> tuple:
        opl = (self.am, self.vib, self.sus, self.ksr, self.mult,
               self.ksl, self.ar, self.dr, self.sl, self.rr, self.ws)
        if include_tl:
            return (
                self.dt_mul, self.tl, self.ks_ar, self.am_d1r,
                self.dt2_d2r, self.d1l_rr, self.sr, self.ssg_eg, opl,
            )
        return (
            self.dt_mul, self.ks_ar, self.am_d1r, self.dt2_d2r,
            self.d1l_rr, self.sr, self.ssg_eg, opl,
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
    # True for an OPL-family instrument (DIV_INS_OPL). `four` marks an OPL3
    # 4-op voice, whose operators are the pair's four slots; `drum` marks the
    # DIV_INS_OPL_DRUMS instrument that carries snare/tom/cymbal/hi-hat.
    opl: bool = False
    four: bool = False
    drum: bool = False
    # Per-note pitch motion for OPN: absolute pitch2 values in 1/128
    # semitones (the module uses Furnace's default linear pitch mode).
    pitch_macro: tuple[int, ...] = ()
    pitch_speed: int = 1
    pitch_loop: int = 255
    # OPL only: per-operator output level automation, as the log wrote it.
    # Entries are ``(operator index, values, speed)``: running from the key-on,
    # the operator takes one entry of `values` every `speed` ticks and then holds
    # its last, each entry an absolute `$4n`. An operator macro has no delay of
    # its own - it starts with the note - so a level that first moves several
    # ticks in repeats its starting entry across that hold. The volume column
    # reaches only an operator marked KVS, so a driver that scripts a modulator's
    # level has no other way to say it: the voice would play one bright or dull
    # colour where the chip moved. Furnace reads these from an ``O1``..``O4``
    # feature, macro code 6 (DIV_MACRO_OP_TL), and a non-KVS operator writes the
    # value straight to ``$4n``.
    op_tl_macro: tuple[tuple[int, tuple[int, ...], int], ...] = ()

    def identity(self) -> tuple:
        """Cluster key: full timbre including carrier TLs.

        Furnace applies volume on a log curve, so a stripped TL cannot be
        reconstructed from pattern volume. Keep TL on the instrument.
        OPN also keeps PMS/AMS: the platform writes 0xB4 from the instrument
        at note-on only when the instrument changes, so two patches that
        differ only in LFO sensitivity must not merge.
        """
        op_keys = tuple(self.ops[i].as_tuple(include_tl=True) for i in range(4))
        if self.opl:
            # The OPL platform has no channel LFO depth register, and 0xB4's
            # PMS/AMS fields do not exist; the connection bit and, on OPL3,
            # the waveforms are all in the operators.
            return (self.alg & 3, self.fb & 7, self.opl, self.four, self.drum, op_keys)
        if self.opn:
            return (self.alg & 7, self.fb & 7, self.opn, self.ams, self.pms, op_keys)
        return (self.alg & 7, self.fb & 7, self.opn, op_keys)

    def carriers(self) -> tuple[int, ...]:
        if self.drum:
            return (0, 1, 2, 3)
        return carriers_for(self.alg & 7, self.opn, self.opl, self.four)

    def carrier_tl(self) -> int:
        mask = 0x3F if self.opl else 0x7F
        tls = [self.ops[i].tl & mask for i in self.carriers()]
        return min(tls) if tls else mask
