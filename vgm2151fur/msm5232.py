"""OKI MSM5232 tone/noise register model.

The MSM5232 is an eight-voice square-wave/additive tone generator, not a
sample player.  Registers 0x00-0x07 hold one pitch/gate byte per voice:
bit 7 is key-on, and values 0xD8-0xFF select noise.  Registers 0x08/0x09 and
0x0A/0x0B set the attack and decay of the two four-channel groups, and
0x0C/0x0D hold the group output enables and sustain bit.

VGM command 0x43 writes (register, data); older libvgm builds used 0x2D.
The module uses Furnace's native MSM5232 system, so this collector only
records notes and group state changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MSM5232_VOICES = 8
MSM5232_NOTE_BASE = 72

# Furnace's MSM5232 platform maps the attack/decay effect value through these
# tables before writing the chip (platform/msm5232.cpp).  The tables are not
# the identity: effect 5 is hardware decay 9, for example.  The collector has
# to invert them so the chip receives the source register value.
FURNACE_MSM5232_ATTACK_MAP = (0, 1, 2, 3, 4, 5, 5, 5)
FURNACE_MSM5232_DECAY_MAP = (
    0, 1, 2, 3, 8, 9, 4, 10, 5, 11, 12, 13, 13, 13, 13, 13,
)
# Inverse of the decay table, keyed by the hardware register value.  Values
# the platform cannot reach (6, 7, 14 and 15) use the nearest hardware rate.
FURNACE_MSM5232_DECAY_FX = (
    0, 1, 2, 3, 6, 8, 9, 10, 4, 5, 7, 9, 10, 11, 11, 11,
)


def msm5232_attack_fx(register: int) -> int:
    """Effect value that makes Furnace write hardware attack ``register``."""
    return min(register & 0x07, 5)


def msm5232_decay_fx(register: int) -> int:
    """Effect value that makes Furnace write hardware decay ``register``."""
    return FURNACE_MSM5232_DECAY_FX[register & 0x0F]


def msm5232_volume_fx(value: int) -> int:
    """TA7630 group volume (0-15) as a Furnace 0-127 channel level."""
    db = 0.0
    step = 1.5
    inc = 0.125
    amp = 0.0
    value = max(0, min(15, value))
    for index in range(16):
        if 15 - index == value:
            amp = 1.0 / (10.0 ** (db / 20.0))
            break
        db += step
        step += inc
    return max(0, min(127, int(round(amp * 127.0))))


@dataclass
class Msm5232Hit:
    sample_time: int
    chip_id: int
    voice: int
    pitch: int
    noise: bool
    volume: int = 15
    control: int = 0x0F
    off_sample: int = 0


@dataclass
class Msm5232Vol:
    sample_time: int
    chip_id: int
    group: int
    value: int


@dataclass
class Msm5232Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class Msm5232Fx:
    """A group-level register write, carried as a pattern effect."""

    sample_time: int
    chip_id: int
    group: int
    code: int
    value: int


def _env_fx(
    fx: list[Msm5232Fx],
    last_env: dict[tuple[int, int, int], int],
    write,
    group: int,
    code: int,
    register: int,
    mapper,
) -> None:
    """Record a group attack/decay change once, mapped for Furnace."""
    key = (write.chip_id, group, code)
    if last_env.get(key) == register:
        return
    last_env[key] = register
    fx.append(
        Msm5232Fx(write.sample, write.chip_id, group, code, mapper(register))
    )


def collect_msm5232(
    vgm,
) -> tuple[
    list[Msm5232Hit], list[Msm5232Off], list[Msm5232Fx], list[Msm5232Vol],
    list[str],
]:
    """Collect MSM5232 key-on/key-off, group and volume writes."""
    hits: list[Msm5232Hit] = []
    offs: list[Msm5232Off] = []
    fx: list[Msm5232Fx] = []
    vols: list[Msm5232Vol] = []
    warnings: list[str] = []
    keyed = [False] * MSM5232_VOICES
    active: list[Msm5232Hit | None] = [None] * MSM5232_VOICES
    # libvgm starts both TA7630 group volumes at maximum; the group control
    # output-enable mask resets to all four parts on.
    ext_vol: dict[int, list[int]] = {}
    group_ctrl: dict[int, list[int]] = {}
    # Repeated group writes with the same value do nothing on the chip.  Keep
    # the last source register per group so 60 Hz refresh writes don't flood
    # the pattern with identical 12xx/13xx effects.
    last_env: dict[tuple[int, int, int], int] = {}
    for w in vgm.writes:
        if w.chip != "msm5232":
            continue
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        group_vol = ext_vol.setdefault(w.chip_id, [15, 15])
        ctrl = group_ctrl.setdefault(w.chip_id, [0x0F, 0x0F])
        if reg < 0x08:
            voice = reg
            if val & 0x80:
                if active[voice] is not None:
                    active[voice].off_sample = w.sample
                pitch = val & 0x7F
                hit = Msm5232Hit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    voice=voice,
                    pitch=pitch,
                    noise=pitch >= 0x58,
                    volume=group_vol[voice >> 2],
                    control=ctrl[voice >> 2] & 0x1F,
                )
                hits.append(hit)
                active[voice] = hit
                keyed[voice] = True
            else:
                if keyed[voice]:
                    offs.append(Msm5232Off(w.sample, w.chip_id, voice))
                    if active[voice] is not None:
                        active[voice].off_sample = w.sample
                        active[voice] = None
                keyed[voice] = False
            continue
        if reg == 0x08:
            _env_fx(fx, last_env, w, 0, 0x12, val & 0x07,
                    msm5232_attack_fx)
        elif reg == 0x09:
            _env_fx(fx, last_env, w, 1, 0x12, val & 0x07,
                    msm5232_attack_fx)
        elif reg == 0x0A:
            _env_fx(fx, last_env, w, 0, 0x13, val & 0x0F,
                    msm5232_decay_fx)
        elif reg == 0x0B:
            _env_fx(fx, last_env, w, 1, 0x13, val & 0x0F,
                    msm5232_decay_fx)
        elif reg == 0x0C:
            ctrl[0] = val & 0x1F
        elif reg == 0x0D:
            ctrl[1] = val & 0x1F
        elif reg == 0x1E:
            value = val & 0x0F
            if group_vol[0] != value:
                group_vol[0] = value
                vols.append(Msm5232Vol(w.sample, w.chip_id, 0, value))
        elif reg == 0x1F:
            value = val & 0x0F
            if group_vol[1] != value:
                group_vol[1] = value
                vols.append(Msm5232Vol(w.sample, w.chip_id, 1, value))
    if not hits and fx:
        warnings.append(
            "MSM5232 group writes but no key-on; the voices stay silent"
        )
    return hits, offs, fx, vols, warnings
