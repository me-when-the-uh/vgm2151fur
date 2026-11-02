"""YM2413 (OPLL) register image: 2-op channels, presets and the user patch.

The OPLL is an OPL relative with a much smaller register map: nine channels of
two operators, one user instrument in ``$00-$07`` and fifteen ROM presets
selected by the high nibble of ``$3x``. ``$0E`` turns the last three channels
into five percussion voices.

The chip has no per-channel output level of its own: the low nibble of ``$3x``
is the carrier's attenuation (0 loudest, 15 quietest) and the carrier TL of a
user patch never exists as a separate register. Furnace's OPLL platform reads
the pattern volume column into that nibble (``$3x = 15 - outVol``), so a
converted note carries its level in the volume column and the carrier operator
of the instrument stays at 0.

Pitch follows the OPL rules with the OPLL core's own frequency base and a
nine-bit f-number; ``vgm2151fur.opl`` has the tables.
"""

from __future__ import annotations

from vgm2151fur.fm import FMPatch, OpRegs

OPLL_KIND = "opll"
OPLL_DEFAULT_CLOCK = 3579545
OPLL_CHANNELS = 9
OPLL_MELODIC = 9
OPLL_DRUMS_CHANNELS = 11
OPLL_DRUMS_MELODIC = 6

# Furnace's OPLL-with-drums channels, in system order: BD, SD, TM, TP, HH.
# `hw` is the register channel the chip tunes, `reg`/`shift` give the volume
# nibble. The chip shares a frequency register between SD/HH and TM/TP.
OPLL_DRUMS = (
    (6, "BD", 6, 0x36, 0, 0x10),
    (7, "SD", 7, 0x37, 0, 0x08),
    (8, "TM", 8, 0x38, 4, 0x04),
    (9, "TP", 8, 0x38, 0, 0x02),
    (10, "HH", 7, 0x37, 4, 0x01),
)

DRUM_NAMES_LONG = ("Kick", "Snare", "Tom", "Top", "HiHat")
DRUM_NAMES_SHORT = ("BD", "SD", "TM", "TP", "HH")
# Samples a key-on waits before its voice is read. Drivers raise bit 4 of
# ``$2x`` and write the f-num's low byte right after; the page at the end of
# this window is the note. It stays far inside one row (a YM2413 driver ticks
# every frame or two, and the shortest grid row this project builds is four).
OPLL_SETTLE = 150
# The $0e latch bits in Furnace's column order: BD, SD, TM, TP, HH. The grid
# estimator reads key-onset times from here (timing.py).
KIT_TRIGGERS = tuple((entry[5], entry[1]) for entry in OPLL_DRUMS)


def _idle_op() -> OpRegs:
    """An operator the chip never reaches: it stays silent."""
    return OpRegs(tl=0x3F, mult=1)


def _carrier_op() -> OpRegs:
    """Silent on its own: the level rides the $3x volume nibble."""
    return OpRegs(tl=0, mult=1)


class OPLLState:
    """Register image of one YM2413."""

    def __init__(self, drums: bool = False) -> None:
        self.drums = drums
        self.regs = [0] * 0x40

    # -- image ---------------------------------------------------------------

    def write(self, reg: int, val: int) -> None:
        self.regs[reg & 0x3F] = val & 0xFF

    @property
    def rhythm(self) -> bool:
        return bool(self.regs[0x0E] & 0x20)

    @property
    def triggers(self) -> int:
        return self.regs[0x0E] & 0x1F

    # -- channel reads -------------------------------------------------------

    def preset(self, fch: int) -> int:
        return (self.regs[0x30 + fch] >> 4) & 15

    def volume(self, fch: int) -> int:
        return self.regs[0x30 + fch] & 15

    def block_fnum(self, fch: int) -> tuple[int, int]:
        hi = self.regs[0x20 + fch]
        return (hi >> 1) & 7, ((hi & 1) << 8) | self.regs[0x10 + fch]

    def keyed(self, fch: int) -> bool:
        return bool(self.regs[0x20 + fch] & 0x10)

    def sustain(self, fch: int) -> int:
        return 1 if (self.regs[0x20 + fch] & 0x20) else 0

    def drum_volume(self, reg: int, shift: int) -> int:
        return (self.regs[reg] >> shift) & 15

    # -- instruments ---------------------------------------------------------

    def _custom_ops(self) -> tuple[OpRegs, OpRegs]:
        r = self.regs

        def op(addr: int, ksl_reg: int, ardr_reg: int, slrr_reg: int,
               tl: int) -> OpRegs:
            amvib = r[addr]
            ksl_tl = r[ksl_reg]
            ardr = r[ardr_reg]
            slrr = r[slrr_reg]
            et = (amvib >> 5) & 1
            return OpRegs(
                tl=tl,
                ksl=(ksl_tl >> 6) & 3,
                ar=(ardr >> 4) & 15,
                dr=ardr & 15,
                sl=(slrr >> 4) & 15,
                rr=slrr & 15,
                am=(amvib >> 7) & 1,
                vib=(amvib >> 6) & 1,
                ksr=(amvib >> 4) & 1,
                mult=amvib & 15,
                ssg_eg=8 if et else 0,
            )

        return (
            op(0x00, 0x02, 0x04, 0x06, r[0x02] & 0x3F),
            # The carrier level is the $3x volume nibble, never $02/$03.
            op(0x01, 0x03, 0x05, 0x07, 0),
        )

    def custom_patch(self, fch: int) -> FMPatch:
        mod, car = self._custom_ops()
        return FMPatch(
            alg=self.sustain(fch),
            fb=self.regs[0x03] & 7,
            ams=(self.regs[0x03] >> 3) & 1,
            pms=(self.regs[0x03] >> 4) & 1,
            ops=(mod, car, _idle_op(), _idle_op()),
            opl=True,
            opll=True,
            preset=0,
        )

    def preset_patch(self, fch: int) -> FMPatch:
        """A ROM voice. Its operators come from the chip, not the module."""
        return FMPatch(
            alg=self.sustain(fch),
            fb=0,
            ams=0,
            pms=0,
            ops=(_idle_op(), _carrier_op(), _idle_op(), _idle_op()),
            opl=True,
            opll=True,
            preset=self.preset(fch),
        )

    def snapshot(self, fch: int) -> FMPatch:
        if self.preset(fch) == 0:
            return self.custom_patch(fch)
        return self.preset_patch(fch)

    def drum_patch(self) -> FMPatch:
        """A placeholder voice for a percussion column.

        The chip plays a fixed drum patch in rhythm mode, so the operators are
        never loaded; the column only needs an instrument of the right type.
        """
        return FMPatch(
            alg=0, fb=0, ams=0, pms=0,
            ops=(_idle_op(), _carrier_op(), _idle_op(), _idle_op()),
            opl=True, opll=True, preset=0,
        )


def opll_rhythm_plan(vgm, ids) -> tuple[bool, dict]:
    """Decide whether a log really latches the OPLL drum kit.

    ``$0E`` bit 5 enables rhythm mode and the low five bits are the level-
    triggered drum keys. A driver that pokes the enable bit during init and
    clears it without a key never sounds a drum, so only a trigger that rises
    while the mode is on counts.
    """
    stats = {"latches": 0, "mode_samples": 0, "melodic_on_kit": 0}
    per: dict[int, list] = {}
    for w in vgm.writes:
        if w.chip == "opll":
            per.setdefault(w.chip_id, []).append(w)
    end = int(vgm.total_samples or 0)
    for ws in per.values():
        mode = False
        since = 0
        trig = 0
        for w in ws + [None]:
            if w is None:
                if mode and end > since:
                    stats["mode_samples"] += end - since
                break
            r = w.reg & 0xFF
            if r != 0x0E:
                if 0x20 <= r <= 0x28 and (w.val & 0x10) and not mode:
                    # A key-on on a channel the kit takes over.
                    ch = r - 0x20
                    if ch >= 6:
                        stats["melodic_on_kit"] += 1
                continue
            now = bool(w.val & 0x20)
            if now != mode:
                stats["segments"] = stats.get("segments", 0) + 1
                if mode:
                    stats["mode_samples"] += w.sample - since
                mode, since = now, w.sample
            bits = (w.val & 0x1F) if now else 0
            for k in range(5):
                bit = 1 << k
                if (bits & bit) and not (trig & bit):
                    stats["latches"] += 1
            trig = bits
    return bool(stats["latches"]), stats
