"""Source-vs-export key-on audit used by the variant verifier.

The FM half matches every source 0x28 key-on to an export key-on on the same
channel within 100 ms and compares the effective operator state.  The SN76489
half does the same for its volume-gated notes.  Only effective bits count:
0x50 bit 5 and 0x60 bits 6-5 are don't-cares, and OPN DT=4 is negative zero.
"""

from __future__ import annotations

from vgm2151fur.fm import OPN_OP_TO_MASK
from vgm2151fur.sn76489 import collect_sn76489
from vgm2151fur.vgm import VgmFile
from vgm2151fur.ym2612 import YM2612State

# 100 ms.  A fine grid can delay a note by up to a few EDxx ticks, but a
# wider window pairs too many repeated FM notes with the next instrument.
WINDOW = 4410
FIELDS = ("dt_mul", "tl", "ks_ar", "am_d1r", "sr", "d1l_rr", "ssg_eg")


def _canon(field: str, value: int) -> int:
    if field == "ks_ar":
        return value & 0xDF
    if field == "am_d1r":
        return value & 0x9F
    if field == "dt_mul":
        dt = (value >> 4) & 7
        if dt == 4:
            dt = 0
        return (dt << 4) | (value & 15)
    return value


def fm_snapshots(vgm: VgmFile) -> list[dict]:
    st = YM2612State()
    out: list[dict] = []
    i = 0
    ws = vgm.writes
    while i < len(ws):
        sample = ws[i].sample
        group = []
        while i < len(ws) and ws[i].sample == sample:
            group.append(ws[i])
            i += 1
        for w in group:
            if w.chip == "ym2612" and w.chip_id == 0:
                st.write(w.reg, w.val)
        for w in group:
            if w.chip != "ym2612" or w.chip_id != 0:
                continue
            if w.reg & 0x100 or (w.reg & 0xFF) != 0x28:
                continue
            ch = w.val & 7
            if ch in (3, 7):
                continue
            ch = ch if ch < 3 else ch - 1
            mask = (w.val >> 4) & 0xF
            if not mask:
                continue
            out.append({
                "sample": sample, "ch": ch, "mask": mask,
                "ops": tuple(st.ops[ch]), "fb_alg": st.fb_alg[ch],
            })
    return out


def _match(a: list[dict], b: list[dict], key, window: int = WINDOW):
    used: set[int] = set()
    pairs = []
    for s in a:
        best = None
        for j, e in enumerate(b):
            if j in used or key(s) != key(e):
                continue
            d = abs(e["sample"] - s["sample"])
            if d <= window and (best is None or d < best[0]):
                best = (d, j)
        if best is None:
            pairs.append((s, None))
        else:
            used.add(best[1])
            pairs.append((s, b[best[1]]))
    return pairs, len(b) - len(used)


def compare_fm(src: VgmFile, exp: VgmFile) -> dict:
    a = fm_snapshots(src)
    b = fm_snapshots(exp)
    if not a:
        return {"matched": 0, "missed": 0, "fields": {}, "startup": {}, "tl": 0}
    bad: dict[str, int] = {}
    startup: dict[str, int] = {}
    matched = missed = 0
    for ch in range(6):
        pairs, _extra = _match(
            [s for s in a if s["ch"] == ch],
            [s for s in b if s["ch"] == ch],
            key=lambda x: x["ch"],
        )
        for s, e in pairs:
            if e is None:
                missed += 1
                continue
            matched += 1
            bucket = startup if s["sample"] == 0 else bad
            for op in range(4):
                if not (s["mask"] & (1 << OPN_OP_TO_MASK[op])):
                    continue
                x, y = s["ops"][op], e["ops"][op]
                for f in FIELDS:
                    if _canon(f, getattr(x, f)) != _canon(f, getattr(y, f)):
                        bucket[f] = bucket.get(f, 0) + 1
            if (s["fb_alg"] & 7) != (e["fb_alg"] & 7):
                bucket["alg"] = bucket.get("alg", 0) + 1
            if (s["fb_alg"] >> 3) != (e["fb_alg"] >> 3):
                bucket["fb"] = bucket.get("fb", 0) + 1
    return {
        "matched": matched, "missed": missed, "fields": bad,
        "startup": startup, "tl": bad.get("tl", 0),
    }


def compare_psg(src: VgmFile, exp: VgmFile) -> dict:
    sh, _, _ = collect_sn76489(src)
    eh, _, _ = collect_sn76489(exp)
    if not sh:
        return {
            "matched": 0, "missed": 0, "extra": 0,
            "period": 0, "vol": 0, "noise": 0,
        }
    # _match expects dicts; use a dict view of the PSG hits.
    def settled(h) -> dict:
        # Furnace applies a note's E5/mode effect just after the note-on and
        # a driver may write its steady volume a tick later.  Compare the
        # state after 10 ms, not the gate byte.
        end = h.sample_time + 441
        period = h.period
        vol = h.vol
        mode = h.noise_mode
        for t, value in h.pitch_pts:
            if t <= end:
                period = value
        for t, value in h.vol_pts:
            if t <= end:
                vol = value
        for t, value in h.mode_pts:
            if t <= end:
                mode = value
        return {
            "sample": h.sample_time, "voice": h.voice, "period": period,
            "vol": vol, "noise": h.noise, "noise_mode": mode,
        }

    sd = [settled(h) for h in sh]
    ed = [settled(h) for h in eh]
    used: set[int] = set()
    pairs = []
    for s in sd:
        best = None
        for j, e in enumerate(ed):
            if j in used or e["voice"] != s["voice"]:
                continue
            dt = abs(e["sample"] - s["sample"])
            if dt > WINDOW:
                continue
            if s["noise"]:
                dv = abs((e["noise_mode"] & 7) - (s["noise_mode"] & 7))
            else:
                dv = abs(e["period"] - s["period"])
            score = (dv, dt)
            if best is None or score < best[0]:
                best = (score, j)
        if best is None:
            pairs.append((s, None))
        else:
            used.add(best[1])
            pairs.append((s, ed[best[1]]))
    extra = len(ed) - len(used)
    missed = period = vol = noise = 0
    for s, e in pairs:
        if e is None:
            missed += 1
            continue
        if s["noise"]:
            if (s["noise_mode"] & 7) != (e["noise_mode"] & 7):
                noise += 1
        elif s["period"] != e["period"]:
            period += 1
        if s["vol"] != e["vol"]:
            vol += 1
    return {
        "matched": len(sd) - missed, "missed": missed, "extra": extra,
        "period": period, "vol": vol, "noise": noise,
    }


def compare_dac(src: VgmFile, exp: VgmFile) -> dict:
    """Count source DAC notes matched to an exported DAC stream start."""
    from vgm2151fur.analyze import analyze

    song = analyze(src)
    notes = [e.sample for e in song.events if e.dac and e.on]
    # Ignore the exporter's end-of-file sample flush.
    limit = src.total_samples or (max(notes, default=0) + 1)
    starts = [t for t, _block, _rate in exp.dac_block_plays if t < limit]
    if not notes:
        return {"matched": 0, "missed": 0, "extra": len(starts), "rate_bad": 0}
    used: set[int] = set()
    missed = 0
    for onset in notes:
        best = None
        for j, t in enumerate(starts):
            if j in used:
                continue
            d = abs(t - onset)
            if d <= WINDOW and (best is None or d < best[0]):
                best = (d, j)
        if best is None:
            missed += 1
        else:
            used.add(best[1])
    # Blocks are exported in sample order; allow 2 Hz C-4 rounding.
    rate_bad = 0
    if len(exp.dac_blocks) == len(song.dac_samples):
        for _t, block, rate in exp.dac_block_plays:
            if block >= len(song.dac_samples):
                continue
            want = song.dac_samples[block].c4_rate
            if want >= 192000:
                continue
            if abs(rate - want) > 2:
                rate_bad += 1
    return {
        "matched": len(notes) - missed,
        "missed": missed,
        "extra": len(starts) - len(used),
        "rate_bad": rate_bad,
    }


def compare_keyons(src: VgmFile, exp: VgmFile) -> dict:
    fm = compare_fm(src, exp)
    psg = compare_psg(src, exp)
    dac = compare_dac(src, exp)
    fields = dict(fm["fields"])
    missed = fm["missed"] + psg["missed"]
    matched = fm["matched"] + psg["matched"]
    return {
        "matched": matched,
        "missed": missed,
        "fields": fields,
        "startup": fm.get("startup", {}),
        "tl": fields.get("tl", 0),
        "psg_missed": psg["missed"],
        "psg_period": psg["period"],
        "psg_vol": psg["vol"],
        "psg_noise": psg["noise"],
        "dac_matched": dac["matched"],
        "dac_missed": dac["missed"],
        "dac_extra": dac["extra"],
        "dac_rate_bad": dac["rate_bad"],
    }
