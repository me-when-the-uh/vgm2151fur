"""Nintendo Famicom Disk System: one 64-step wavetable voice.

VGM carries FDS writes through the NES command ``0xB4``.  libvgm remaps
offsets ``0x20-0x3F`` to the FDS register file (``$4080-$408A`` plus the
``$4023`` I/O enable) and leaves ``0x40-0x7F`` as the 64-byte wave table.
The parser already splits those into the ``fds`` chip stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile

FDS_DEFAULT_CLOCK = 1_789_773
# Furnace's FDS platform is a frequency-mode pitch table with CHIP_FREQBASE.
FDS_FREQ_DIV = 262_144
FDS_MAX_FREQ = 0xFFFF
FDS_WAVE_LEN = 64
FDS_MOD_LEN = 32
# A frequency jump this large (1/64 semitone units) starts a new note.
FDS_NOTE_JUMP = 64
# Shorter than this is a click or a re-attack of the same note.
FDS_MIN_NOTE = 64
# A volume write that lands this soon after the frequency write belongs to
# the same key-on.
FDS_SETTLE = 96


@dataclass
class FdsHit:
    """One FDS note with its starting state and live changes."""

    sample_time: int
    chip_id: int
    freq: int
    wave: bytes
    mod_table: bytes
    mod_depth: int
    mod_speed: int
    mod_pos: int
    volume: int
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)
    mod_pts: list[tuple[int, int, int]] = field(default_factory=list)
    off_sample: int = 0


def _freq_value(low: int, high: int) -> int:
    return ((high & 0x0F) << 8) | (low & 0xFF)


def collect_fds(vgm: VgmFile) -> tuple[list[FdsHit], list[str]]:
    """Walk the FDS register stream and return its notes."""
    warnings: list[str] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "fds"})
    if not chips:
        return [], warnings

    total = vgm.total_samples or 0
    hits: list[FdsHit] = []
    state: dict[int, dict] = {}
    for cid in chips:
        state[cid] = {
            "wave": bytearray(FDS_WAVE_LEN),
            "mod": bytearray(FDS_MOD_LEN),
            "mod_pos": 0,
            "low": 0,
            "high": 0,
            "volume": 0,
            "env_mode": 0,
            "mod_depth": 0,
            "mod_speed": 0,
            "mod_position": 0,
            "active": None,
            "last_note_t": -1,
            "wave_burst": False,
            "wave_dirty": False,
            "mod_burst": False,
            "pending_off": 0,
        }

    def current_freq(st: dict) -> int:
        return _freq_value(st["low"], st["high"])

    def close(cid: int, when: int, off: bool) -> None:
        st = state[cid]
        hit: FdsHit | None = st["active"]
        if hit is None:
            return
        if off and when > hit.sample_time:
            hit.off_sample = when
        st["active"] = None

    def open_hit(cid: int, when: int, freq: int) -> FdsHit:
        st = state[cid]
        hit = FdsHit(
            sample_time=when,
            chip_id=cid,
            freq=freq,
            wave=bytes(st["wave"]),
            mod_table=bytes(st["mod"]),
            mod_depth=st["mod_depth"],
            mod_speed=st["mod_speed"],
            mod_pos=st["mod_position"],
            volume=max(0, min(32, st["volume"])),
        )
        hits.append(hit)
        st["active"] = hit
        return hit

    for w in vgm.writes:
        if w.chip != "fds":
            continue
        cid = w.chip_id
        st = state[cid]
        t = w.sample
        reg = w.reg
        val = w.val & 0xFF

        # Wave table writes (0x40-0x7F) and the 0x89 write-enable toggle.
        if 0x40 <= reg <= 0x7F:
            st["wave"][(reg - 0x40) & 0x3F] = val & 0x3F
            st["wave_burst"] = True
            st["wave_dirty"] = True
            continue

        # A non-wave register write ends a wave upload burst.
        if st["wave_burst"]:
            hit = st["active"]
            if hit is not None and st["wave_dirty"]:
                body = bytes(st["wave"])
                if body != hit.wave:
                    hit.wave_pts.append((t, body))
                    hit.wave = body
            st["wave_burst"] = False
            st["wave_dirty"] = False

        if reg == 0x80:
            st["volume"] = val & 0x3F
            st["env_mode"] = (val >> 6) & 0x03
            hit = st["active"]
            if hit is not None:
                level = max(0, min(32, st["volume"]))
                if not hit.vol_pts or hit.vol_pts[-1][1] != level:
                    hit.vol_pts.append((t, level))
                if level == 0:
                    close(cid, t, off=True)
            continue
        if reg == 0x82:
            st["low"] = val
            hit = st["active"]
            if hit is not None and not hit.freq_pts:
                # A low-byte rewrite on its own is half a pitch write; wait for
                # the high byte so the pair is logged once.
                pass
            continue
        if reg == 0x83:
            old_high = st["high"]
            st["high"] = val
            freq = current_freq(st)
            silenced = bool(val & 0x80)
            hit_old = st["active"]
            if silenced or freq == 0:
                if hit_old is not None:
                    close(cid, t, off=True)
                continue
            # A new note unless this is a small pitch change on a live note.
            jump = (
                abs(freq - hit_old.freq) >= FDS_NOTE_JUMP
                if hit_old is not None else True
            )
            reattack = (
                hit_old is not None
                and st["volume"] > 0
                and bool(old_high & 0x80)
            )
            fresh = (
                hit_old is None
                or jump
                or reattack
                or t - st["last_note_t"] >= FDS_MIN_NOTE
                and not (old_high & 0x80)
            )
            if hit_old is not None and not fresh:
                hit_old.freq_pts.append((t, freq))
                hit_old.freq = freq
            else:
                close(cid, t, off=hit_old is not None)
                if st["volume"] > 0 or not silenced:
                    open_hit(cid, t, freq)
                    st["last_note_t"] = t
            continue
        if reg == 0x84:
            # $4084: bit 6 = increase, bit 7 = disable envelope, low six
            # bits = depth.  Furnace's platform uses a fixed depth.
            st["mod_depth"] = val & 0x3F
            st["mod_position"] = 0
            hit = st["active"]
            if hit is not None:
                hit.mod_pts.append((t, 0x11, st["mod_depth"]))
                hit.mod_depth = st["mod_depth"]
            continue
        if reg == 0x85:
            st["mod_position"] = val & 0x7F
            hit = st["active"]
            if hit is not None:
                hit.mod_pts.append((t, 0x14, st["mod_position"]))
                hit.mod_pos = st["mod_position"]
            continue
        if reg == 0x86:
            st["mod_speed"] = (st["mod_speed"] & 0xF00) | val
            hit = st["active"]
            if hit is not None:
                hit.mod_pts.append((t, 0x13, val))
                hit.mod_pts.append((t, 0x12, (st["mod_speed"] >> 8) & 0x0F))
            continue
        if reg == 0x87:
            st["mod_speed"] = (st["mod_speed"] & 0x0FF) | ((val & 0x0F) << 8)
            if val & 0x80:
                # Modulator halted: the next 0x88 writes upload the table.
                st["mod_pos"] = 0
                st["mod_burst"] = True
            hit = st["active"]
            if hit is not None:
                hit.mod_pts.append((t, 0x12, (st["mod_speed"] >> 8) & 0x0F))
                hit.mod_pts.append((t, 0x11, st["mod_depth"]))
            continue
        if reg == 0x88:
            st["mod"][st["mod_pos"] & 0x1F] = val & 0x07
            st["mod_pos"] = (st["mod_pos"] + 1) & 0x1F
            continue
        if reg == 0x89:
            # Bit 7 enables wave writes.  The bit itself is not audible.
            continue
        if reg == 0x8A:
            st["mod_speed"] = val
            hit = st["active"]
            if hit is not None:
                hit.mod_pts.append((t, 0x12, (val >> 4) & 0x0F))
            continue
        # $4023 and anything else is setup, not a note.

    for cid in chips:
        close(cid, total, off=False)

    # Drop empty or degenerate hits and sort for the caller.
    hits = [
        h for h in hits
        if h.freq > 0 and (h.off_sample == 0 or h.off_sample > h.sample_time)
    ]
    hits.sort(key=lambda h: (h.sample_time, h.chip_id))
    return hits, warnings
