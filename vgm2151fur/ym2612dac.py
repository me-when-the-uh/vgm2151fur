"""YM2612 DAC PCM bank -> rate-exact PCM phrases (default) or slices.

VGM 1.50 encodes a DAC stream as a type-0 data block (unsigned 8-bit PCM)
plus 0xE0 (set read pointer) and 0x80-0x8F (play one byte, then wait 0-15
samples).  Direct 0x2A writes are the same audio without a bank address.
Modern logs use 0x92 (rate) and 0x95 (one type-0 block).

The default is a sequence of rate-exact PCM chunks, each played by one FM6
note.  Chunks split at pan changes and at most every 0.25 s, capped at 240
samples because Furnace 0.6.8.3 rejects dense sample-map sets with "invalid
sample count!" (a 130-sample probe loads; 392 slices do not).  The sample
rate is the source's smallest positive write interval in that chunk
(`round(44100 / min_interval)`), so every source byte has a distinct frame.
Phrases split only on pauses over 2,048 VGM samples.  A silence-only phrase
and leading silence create no sample and no note; a chunk starts on audio and
one trailing 0x80 frame sets the DAC's hold to silence.  The pan at each
chunk comes from 0xB6, so the module follows the source's left/right changes
instead of holding the first pan for the whole track.

`collect_dac(..., slices=True)` is the explicit editing representation: source
runs at their own rate, deduplicated, same-row merged, one sample map per 120
slices.  A row that is pure silence gets no note; the preceding sample gets a
zero tail.  `collect_dac(..., timeline=True)` is the old 22.05 kHz comparison
body.  `group_dac_plays` places the chosen plays on the tick grid.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from vgm2151fur.timing import place_tick
from vgm2151fur.vgm import VGM_RATE, VgmFile

DAC_GAP = 512
DAC_MIN_SAMPLES = 8
DAC_MAP_CAPACITY = 120
# The dense-timeline fallback is kept for comparison; the release-capped
# chunked path is the default.  A gap larger than this starts another phrase.
DAC_RUN_GAP = 2048
DAC_TIMELINE_RATE = 22050
# Release Furnace rejects very large sample sets.  The 130-sample probe loads
# in 0.6.8.3; a 392-sample slice track does not.  Stay well below the wall.
DAC_RELEASE_SAMPLES = 240
# A chunk is at most this long, so a dense track becomes a real sequence of
# PCM chunks rather than one matryoshka sample.  Short sources stay one chunk.
DAC_CHUNK_SECONDS = 0.25
# Furnace clamps a sample-playback rate to this value.
DAC_RATE_MAX = 192000
DAC_RATE_MIN = 1


@dataclass(frozen=True)
class DacSample:
    start: int
    length: int
    data: bytes  # signed 8-bit
    c4_rate: int
    onset: int


@dataclass(frozen=True)
class DacPlay:
    """One source run played on FM6.

    `span` is the source time from the first byte to the last, in VGM
    samples.  A run whose bytes are all equal stores one byte and sets
    `collapsed`; the hold is recreated from `span` when runs are merged.
    """

    onset: int
    sample: int
    pan: int
    span: int
    collapsed: bool = False


def _signed_bank(raw: bytes) -> bytes:
    return bytes((x - 0x80) & 0xFF for x in raw)


def _pan_byte(raw: int) -> int:
    """Genesis channel-6 pan as Furnace's split 4-bit pan byte.

    YM2612 0xB6 bits 7/6 are left/right enables; Furnace's 08 effect uses
    one 4-bit nibble per side, so C0 (both) must become FF, not C0 (left
    only), and 40 (right) must become 0F.
    """
    left = bool(raw & 0x80)
    right = bool(raw & 0x40)
    if left and right:
        return 0xFF
    if left:
        return 0xF0
    if right:
        return 0x0F
    return 0


def _pan_timeline(vgm: VgmFile) -> list[tuple[int, int]]:
    """(sample, raw 0xB6 pan bits) for the first YM2612."""
    out: list[tuple[int, int]] = []
    for w in vgm.writes:
        if (
            w.chip == "ym2612" and w.chip_id == 0
            and not (w.reg & 0x100) and (w.reg & 0xFF) == 0xB6
        ):
            out.append((w.sample, w.val & 0xC0))
    return out


def _pan_at(timeline: list[tuple[int, int]], t: int) -> int:
    i = bisect.bisect_right(timeline, (t, 0xFFFF)) - 1
    raw = timeline[i][1] if i >= 0 else 0xC0
    return _pan_byte(raw)


def _dac_enable_timeline(vgm: VgmFile) -> list[tuple[int, bool]]:
    """(sample, enabled) for the first YM2612's 0x2B writes.

    A source with no 0x2B write is treated as enabled: some rips omit the
    register even though their DAC stream is audible.  A log that writes 0x2B
    but never sets bit 7 has no audible DAC at all (track 01 of the Contra
    set is one); its FM6 key-ons are ordinary FM notes.
    """
    return [
        (w.sample, bool(w.val & 0x80))
        for w in vgm.writes
        if (
            w.chip == "ym2612" and w.chip_id == 0
            and not (w.reg & 0x100) and (w.reg & 0xFF) == 0x2B
        )
    ]


def _dac_enabled_at(timeline: list[tuple[int, bool]], t: int) -> bool:
    if not timeline:
        return True
    i = bisect.bisect_right(timeline, (t, True)) - 1
    return timeline[i][1] if i >= 0 else False


def _clamp_rate(rate: float, stats: dict) -> int:
    value = int(round(rate))
    if value > DAC_RATE_MAX:
        value = DAC_RATE_MAX
        stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
    elif value < DAC_RATE_MIN:
        value = DAC_RATE_MIN
        stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
    return value


def _byte_events(
    vgm: VgmFile, stats: dict, enable_tl: list[tuple[int, bool]],
) -> list[tuple[int, int, int]]:
    """(sample, signed byte, wait) for every audible DAC word, in order.

    Old bank plays carry their wait nibble.  Direct 0x2A writes have no
    explicit wait, so their hold is the time to the next DAC word, capped at
    `DAC_GAP` (a longer gap is a new run).  Pointer changes are folded into
    the bank address and must not break the stream by themselves.

    Writes while 0x2B bit 7 is clear are inaudible; the cursor still advances
    so the next enabled read lands at the source address.
    """
    bank = bytes(vgm.dac_data)
    signed = _signed_bank(bank)
    words: list[list[int]] = []
    cursor = 0
    pos = 0
    for t, kind, value in vgm.dac_events:
        if kind == 0:
            cursor = max(0, value)
            pos = 0
            continue
        if kind == 1:
            addr = cursor + pos
            pos += 1
            if addr >= len(bank):
                stats["dac_bank_overrun"] = stats.get("dac_bank_overrun", 0) + 1
                continue
            byte = signed[addr]
        elif kind == 2:
            byte = (value - 0x80) & 0xFF
        else:
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            if byte:
                stats["dac_disabled_audible"] = stats.get("dac_disabled_audible", 0) + 1
            continue
        words.append([t, byte, value & 0x0F if kind == 1 else 0, kind])
    for i, w in enumerate(words):
        if w[3] != 2:
            continue
        nxt = words[i + 1][0] if i + 1 < len(words) else w[0]
        w[2] = max(0, min(DAC_GAP, nxt - w[0]))
    return [(w[0], w[1], w[2]) for w in words]


def _add_sample(
    samples: list[DacSample], by_key: dict[tuple[bytes, int], int],
    data: bytes, rate: int, onset: int,
) -> int:
    key = (data, rate)
    idx = by_key.get(key)
    if idx is None:
        idx = len(samples)
        samples.append(DacSample(-1, len(data), data, rate, onset))
        by_key[key] = idx
    return idx


def _collect_slices(
    vgm: VgmFile,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Slice representation: source runs, deduplicated and row-merged.

    This is the explicit editing representation (`collect_dac(..., slices=True)`).
    It is not the default because release Furnace has a hard sample-count
    limit and a dense Contra track produces thousands of samples.
    """
    if not vgm.dac_events and not vgm.dac_block_plays:
        return [], [], [], {}

    stats: dict = {}
    warnings: list[str] = []
    pan_timeline = _pan_timeline(vgm)
    enable_tl = _dac_enable_timeline(vgm)
    words = _byte_events(vgm, stats, enable_tl)
    samples: list[DacSample] = []
    by_key: dict[tuple[bytes, int], int] = {}
    plays: list[DacPlay] = []

    run: list[tuple[int, int, int]] = []

    def flush() -> None:
        nonlocal run
        if not run:
            return
        onset = run[0][0]
        last = run[-1][0]
        span = last - onset
        data = bytes(v[1] for v in run)
        count = len(data)
        stats["dac_runs"] = stats.get("dac_runs", 0) + 1
        stats["dac_source_bytes"] = stats.get("dac_source_bytes", 0) + count
        collapsed = count > 1 and len(set(data)) == 1
        if collapsed:
            data = data[:1]
            rate = VGM_RATE
            stats["dac_collapsed_bytes"] = stats.get("dac_collapsed_bytes", 0) + count
        elif count == 1:
            rate = VGM_RATE
        elif span > 0:
            rate = _clamp_rate(VGM_RATE * (count - 1) / span, stats)
        else:
            # Defensive only: the run loop splits same-timestamp groups before
            # they reach here.
            data = data[-1:]
            rate = VGM_RATE
            collapsed = True
        idx = _add_sample(samples, by_key, data, rate, onset)
        plays.append(DacPlay(onset, idx, _pan_at(pan_timeline, onset), span, collapsed))
        run = []

    i = 0
    while i < len(words):
        t, byte, wait = words[i]
        j = i + 1
        while j < len(words) and words[j][0] == t:
            j += 1
        if j - i > 1:
            # One driver burst at a single VGM timestamp.  The reference
            # player writes them all at one output sample, so only the final
            # value sounds.  Keep that value as its own run; the row grouper
            # then bridges the hold to the next byte.  Playing the
            # intermediate values at 192 kHz would push every later byte in
            # the row late and is not what a player emits.
            if run:
                flush()
            kept = words[j - 1]
            if len({w[1] for w in words[i:j]}) == 1:
                # A repeated value is a hold, not a lost waveform.  Store it
                # once; the row grouper recreates the hold from the times.
                stats["dac_collapsed_bytes"] = (
                    stats.get("dac_collapsed_bytes", 0) + (j - i)
                )
            else:
                # Distinct values at one timestamp: the reference player
                # writes them at one output sample, so only the final value
                # sounds.  Count the bytes that cannot be timed.
                stats["dac_same_time_runs"] = (
                    stats.get("dac_same_time_runs", 0) + 1
                )
                stats["dac_same_time_bytes"] = (
                    stats.get("dac_same_time_bytes", 0) + (j - i)
                )
                stats["dac_same_time_dropped"] = (
                    stats.get("dac_same_time_dropped", 0) + (j - i - 1)
                )
            # `flush` counts the one stored byte; add the collapsed rest so
            # the source-byte statistic still matches the log.
            stats["dac_source_bytes"] = (
                stats.get("dac_source_bytes", 0) + (j - i - 1)
            )
            run = [kept]
            flush()
            i = j
            continue
        if run and t > run[-1][0] + run[-1][2]:
            flush()
        run.append((t, byte, wait))
        i += 1
    flush()

    # Modern 0x92/0x95 block plays.  Each 0x95 supplies one whole body at a
    # known rate; that is already a slice.
    for t, block, rate in vgm.dac_block_plays:
        if not (0 <= block < len(vgm.dac_blocks)):
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            continue
        body = _signed_bank(bytes(vgm.dac_blocks[block]))
        if not body:
            continue
        rate = _clamp_rate(rate, stats)
        span = int(round(len(body) * VGM_RATE / rate))
        idx = _add_sample(samples, by_key, body, rate, t)
        plays.append(DacPlay(t, idx, _pan_at(pan_timeline, t), span, False))
        stats["dac_block_plays"] = stats.get("dac_block_plays", 0) + 1

    plays.sort(key=lambda p: p.onset)
    stats["dac_unique_samples"] = len(samples)
    stats["dac_sample_bytes"] = sum(len(s.data) for s in samples)

    if stats.get("dac_disabled_bytes") and not plays:
        warnings.append(
            f"{stats['dac_disabled_bytes']} YM2612 DAC writes while the DAC is "
            "disabled (0x2B bit 7 clear); no DAC audio"
        )
    elif stats.get("dac_disabled_audible"):
        warnings.append(
            f"{stats['dac_disabled_audible']} non-silent YM2612 DAC writes while "
            "the DAC is disabled; ignored"
        )
    if stats.get("dac_rate_clamped"):
        warnings.append(
            f"{stats['dac_rate_clamped']} YM2612 DAC runs clamped to "
            f"{DAC_RATE_MAX} Hz; their bytes are preserved but the timing is "
            "longer than the source"
        )
    if stats.get("dac_same_time_runs"):
        warnings.append(
            f"{stats['dac_same_time_runs']} YM2612 DAC bursts "
            f"({stats.get('dac_same_time_bytes', 0)} bytes) share one VGM "
            "sample; only the final value is kept because the intermediate "
            "writes cannot be timed at VGM resolution"
        )
    if stats.get("dac_bank_overrun"):
        warnings.append(
            f"{stats['dac_bank_overrun']} YM2612 DAC reads ran past the PCM bank"
        )
    return samples, plays, warnings, stats


def _packed_words(
    vgm: VgmFile, stats: dict, enable_tl: list[tuple[int, bool]],
) -> list[tuple[float, int, int]]:
    """Canonical words with block plays expanded, same-time groups reduced."""
    words: list[tuple[float, int, int]] = list(
        _byte_events(vgm, stats, enable_tl)
    )
    for t, block, rate in vgm.dac_block_plays:
        if not (0 <= block < len(vgm.dac_blocks)):
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            continue
        body = _signed_bank(bytes(vgm.dac_blocks[block]))
        if not body:
            continue
        step = VGM_RATE / (rate if rate > 0 else VGM_RATE)
        for i, byte in enumerate(body):
            words.append((t + i * step, byte, 0))
        stats["dac_block_plays"] = stats.get("dac_block_plays", 0) + 1
    if not words:
        return []
    words.sort(key=lambda w: w[0])
    reduced: list[tuple[float, int, int]] = []
    i = 0
    while i < len(words):
        t = words[i][0]
        j = i + 1
        while j < len(words) and words[j][0] == t:
            j += 1
        group = words[i:j]
        kept = group[-1]
        if len(group) > 1:
            values = {w[1] for w in group}
            if len(values) == 1:
                stats["dac_collapsed_bytes"] = (
                    stats.get("dac_collapsed_bytes", 0) + len(group)
                )
            else:
                stats["dac_same_time_runs"] = (
                    stats.get("dac_same_time_runs", 0) + 1
                )
                stats["dac_same_time_bytes"] = (
                    stats.get("dac_same_time_bytes", 0) + len(group)
                )
                stats["dac_same_time_dropped"] = (
                    stats.get("dac_same_time_dropped", 0) + len(group) - 1
                )
        reduced.append(kept)
        i = j
    return reduced


def _collect_packed(
    vgm: VgmFile,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Release-capped PCM chunks, no silence notes, pan changes preserved.

    A phrase is split only on a long pause, so a drum run and its short
    inter-hit 0x80 rests stay one real PCM stream.  Leading silence, a
    silence-only phrase, and the long tail after the last audio write become
    no note at all.  Within a phrase the audio is split at every 0xB6 pan
    change and into at most 0.25 s chunks; the total is capped at
    DAC_RELEASE_SAMPLES (240) because release Furnace rejects very large
    sample sets.  A chunk starts on an audio word, so no note is a rest; one
    trailing zero frame sets the hold to silence.  The rate is the chunk's
    smallest write interval, so every source byte has a distinct frame.  The
    old 22.05 kHz timeline is not used.
    """
    if not vgm.dac_events and not vgm.dac_block_plays:
        return [], [], [], {}
    stats: dict = {}
    warnings: list[str] = []
    pan_timeline = _pan_timeline(vgm)
    enable_tl = _dac_enable_timeline(vgm)
    words = _packed_words(vgm, stats, enable_tl)

    if not words:
        if stats.get("dac_disabled_bytes"):
            warnings.append(
                f"{stats['dac_disabled_bytes']} YM2612 DAC writes while the DAC "
                "is disabled (0x2B bit 7 clear); no DAC audio"
            )
        return [], [], warnings, stats

    phrases: list[list[tuple[float, int, int]]] = []
    current: list[tuple[float, int, int]] = []
    for word in words:
        if current and word[0] - current[-1][0] > DAC_RUN_GAP:
            phrases.append(current)
            current = []
        current.append(word)
    if current:
        phrases.append(current)

    phrase_info: list[dict] = []
    trimmed = 0
    total_span = 0.0
    for phrase in phrases:
        audio = [i for i, w in enumerate(phrase) if w[1] != 0]
        if not audio:
            stats["dac_silent_phrases"] = (
                stats.get("dac_silent_phrases", 0) + 1
            )
            trimmed += len(phrase)
            continue
        first_i = audio[0]
        last_i = audio[-1]
        tail_i = None
        for j in range(last_i + 1, len(phrase)):
            if phrase[j][1] == 0:
                tail_i = j
                break
        trimmed += first_i + (
            len(phrase) - 1 - last_i - (1 if tail_i is not None else 0)
        )
        span = max(1.0, phrase[last_i][0] - phrase[first_i][0])
        phrase_info.append({
            "phrase": phrase,
            "audio": audio,
            "first_i": first_i,
            "last_i": last_i,
            "tail_i": tail_i,
            "span": span,
            "pans": [_pan_at(pan_timeline, phrase[i][0]) for i in audio],
        })
        total_span += span
        stats["dac_phrases"] = stats.get("dac_phrases", 0) + 1

    total_span = max(total_span, 1.0)
    target_chunks = max(1, min(
        DAC_RELEASE_SAMPLES,
        int(total_span / (VGM_RATE * DAC_CHUNK_SECONDS) + 0.9999),
    ))
    stats["dac_target_chunks"] = target_chunks

    samples: list[DacSample] = []
    plays: list[DacPlay] = []
    pan_chunks = 0
    for info in phrase_info:
        phrase = info["phrase"]
        audio = info["audio"]
        pans = info["pans"]
        budget = max(1, int(round(
            target_chunks * info["span"] / total_span
        )))
        segments: list[tuple[int, int, int]] = []
        seg_start = 0
        for k in range(1, len(audio)):
            if pans[k] != pans[seg_start]:
                segments.append((seg_start, k - 1, pans[seg_start]))
                seg_start = k
        segments.append((seg_start, len(audio) - 1, pans[seg_start]))
        allocations: list[int] = []
        for s0, s1, _pan in segments:
            count = max(1, int(round(
                budget * (s1 - s0 + 1) / len(audio)
            )))
            count = min(count, s1 - s0 + 1)
            allocations.append(count)
        for (seg0, seg1, seg_pan), count in zip(segments, allocations):
            seg_len = seg1 - seg0 + 1
            last_pan = None
            for k in range(count):
                a0 = seg0 + (k * seg_len) // count
                a1 = seg0 + ((k + 1) * seg_len) // count - 1
                if a1 < a0:
                    continue
                w0 = audio[a0]
                w1 = audio[a1]
                chunk_tail = None
                for j in range(w1 + 1, len(phrase)):
                    if phrase[j][1] == 0:
                        chunk_tail = j
                        break
                selected = phrase[w0:w1 + 1] + (
                    [phrase[chunk_tail]] if chunk_tail is not None else []
                )
                start = selected[0][0]
                times = [w[0] for w in selected]
                deltas = [
                    b - a for a, b in zip(times, times[1:]) if b > a
                ]
                min_wait = min(deltas) if deltas else 1.0
                rate = max(
                    DAC_RATE_MIN,
                    min(DAC_RATE_MAX, int(round(VGM_RATE / min_wait))),
                )
                out = bytearray()
                previous = 0x80
                for t, byte, _wait in selected:
                    frame = int(round((t - start) * rate / VGM_RATE))
                    if frame < len(out):
                        out[frame] = byte
                        stats["dac_frame_collisions"] = (
                            stats.get("dac_frame_collisions", 0) + 1
                        )
                        previous = byte
                        continue
                    if frame > len(out):
                        out.extend(bytes([previous]) * (frame - len(out)))
                    out.append(byte)
                    previous = byte
                data = bytes(out)
                idx = len(samples)
                samples.append(DacSample(0, len(data), data, rate, start))
                chunk_pan = _pan_at(pan_timeline, start)
                plays.append(DacPlay(
                    start, idx, chunk_pan,
                    int(round(selected[-1][0] - start)),
                ))
                if chunk_pan != last_pan:
                    pan_chunks += 1
                    last_pan = chunk_pan
                stats["dac_chunks"] = stats.get("dac_chunks", 0) + 1
                if chunk_pan != seg_pan:
                    stats["dac_pan_mismatch"] = (
                        stats.get("dac_pan_mismatch", 0) + 1
                    )

    if len(samples) > DAC_RELEASE_SAMPLES:
        stats["dac_release_trimmed"] = len(samples) - DAC_RELEASE_SAMPLES
        del samples[DAC_RELEASE_SAMPLES:]
        plays = [p for p in plays if p.sample < DAC_RELEASE_SAMPLES]

    stats["dac_mode"] = "chunks"
    stats["dac_source_bytes"] = len(words)
    stats["dac_trimmed_silence_bytes"] = trimmed
    stats["dac_chunks"] = len(samples)
    stats["dac_pan_chunks"] = pan_chunks
    stats["dac_unique_samples"] = len(samples)
    stats["dac_sample_bytes"] = sum(len(s.data) for s in samples)
    stats["dac_notes"] = len(plays)

    if stats.get("dac_disabled_audible"):
        warnings.append(
            f"{stats['dac_disabled_audible']} non-silent YM2612 DAC writes "
            "while the DAC is disabled; ignored"
        )
    if stats.get("dac_same_time_runs"):
        warnings.append(
            f"{stats['dac_same_time_runs']} YM2612 DAC bursts "
            f"({stats.get('dac_same_time_bytes', 0)} bytes) share one VGM "
            "sample; only the final value is kept"
        )
    if stats.get("dac_frame_collisions"):
        warnings.append(
            f"{stats['dac_frame_collisions']} YM2612 DAC writes rounded to an "
            "already-written phrase frame; the later value at that frame is kept"
        )
    if stats.get("dac_bank_overrun"):
        warnings.append(
            f"{stats['dac_bank_overrun']} YM2612 DAC reads ran past the PCM bank"
        )
    if not samples:
        warnings.append(
            "YM2612 DAC stream contains only silence; no DAC notes written"
        )
    return samples, plays, warnings, stats


def collect_dac(
    vgm: VgmFile, *, slices: bool = False, timeline: bool = False,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Return (samples, plays, warnings, stats).

    Default: release-capped rate-exact PCM chunks, split at pan changes.
    `slices=True`: the per-run sample-map editor representation.
    `timeline=True`: the old 22.05 kHz comparison body.
    """
    if timeline:
        return _collect_timeline(vgm)
    if slices:
        return _collect_slices(vgm)
    return _collect_packed(vgm)


def _group_rate(
    samples: list[DacSample], items: list[DacPlay],
    next_onset: dict[int, int],
) -> int:
    """One playback rate for a merged row.

    Natural rates are weighted by byte count.  The lower bound makes every
    run's bytes fit before the next run starts, so the merged sample's holds
    can land each source onset exactly.
    """
    weighted = 0.0
    weight = 0
    for p in items:
        n = len(samples[p.sample].data)
        if n > 1 and not p.collapsed:
            weighted += samples[p.sample].c4_rate * n
            weight += n
    natural = weighted / weight if weight else float(VGM_RATE)
    lower = 0.0
    for p in items:
        n = len(samples[p.sample].data)
        nxt = next_onset.get(id(p))
        if n > 1 and nxt is not None and nxt > p.onset:
            lower = max(lower, VGM_RATE * n / (nxt - p.onset))
    return int(round(max(natural, lower)))


def _held_byte(
    samples: list[DacSample], previous: int | None, previous_time: int,
    cut_time: int,
) -> int:
    """The value the DAC is holding when the next note starts."""
    if previous is None:
        return 0x80
    smp = samples[previous]
    frame = int(round((cut_time - previous_time) * smp.c4_rate / VGM_RATE))
    frame = max(0, min(len(smp.data) - 1, frame))
    return smp.data[frame]


def group_dac_plays(
    samples: list[DacSample],
    plays: list[DacPlay],
    *,
    hz: float,
    speed: int,
    t0: int = 0,
    total_samples: int = 0,
    map_capacity: int = DAC_MAP_CAPACITY,
) -> tuple[
    list[DacSample], list[tuple[int, int, int]], list[tuple[int, int]],
    list[str], dict,
]:
    """Merge same-row runs and return (samples, notes, pan_events, warnings, stats).

    A note is ``(placed_sample, sample_index, pan)``; the sample index selects
    the slot inside its 120-entry map.  Runs in one row become one sample so
    no source byte or onset is dropped.  `placed_sample` is on the tick grid,
    and the sample starts with the previously held byte for the residual.
    """
    if not plays:
        return samples, [], [], [], {}
    stats: dict = {}
    row_samples = (VGM_RATE / hz) * speed
    tick = VGM_RATE / hz

    def place(sample: int) -> tuple[int, int]:
        return place_tick(sample, t0, row_samples, speed, tick, floor=True)

    def placed_time(row: int, delay: int) -> int:
        return int(round(t0 + row * row_samples + delay * tick))

    ordered = sorted(plays, key=lambda p: p.onset)
    next_onset = {
        id(p): (ordered[i + 1].onset if i + 1 < len(ordered) else total_samples)
        for i, p in enumerate(ordered)
    }

    groups: dict[int, list[DacPlay]] = {}
    for p in ordered:
        row, _delay = place(p.onset)
        groups.setdefault(row, []).append(p)

    out: list[DacSample] = []
    by_key: dict[tuple[bytes, int], int] = {}
    notes: list[tuple[int, int, int]] = []
    pan_events: list[tuple[int, int]] = []
    previous_sample: int | None = None
    previous_time = 0

    def is_silent(p: DacPlay) -> bool:
        payload = samples[p.sample].data
        return bool(payload) and not any(payload)

    for row in sorted(groups):
        items = groups[row]
        if all(is_silent(p) for p in items):
            # A row whose only DAC content is 0x80 is a rest, not a PCM
            # event.  Do not write a note or a sample-map selector for it.
            # Make the preceding audio sample end on a zero byte instead, so
            # Furnace holds silence after it; the source's hold until this
            # row is the preceding byte.
            stats["dac_silent_groups"] = stats.get("dac_silent_groups", 0) + 1
            stats["dac_silent_notes"] = stats.get("dac_silent_notes", 0) + 1
            stats["dac_silent_plays"] = (
                stats.get("dac_silent_plays", 0) + len(items)
            )
            stats["dac_silent_bytes"] = (
                stats.get("dac_silent_bytes", 0)
                + sum(len(samples[p.sample].data) for p in items)
            )
            if notes:
                prev_time, prev_idx, prev_pan = notes[-1]
                prev = out[prev_idx]
                if prev.data and prev.data[-1] != 0:
                    target = int(round(
                        (items[0].onset - prev_time) * prev.c4_rate / VGM_RATE
                    ))
                    target = max(target, len(prev.data))
                    body = (
                        prev.data
                        + bytes([prev.data[-1]]) * (target - len(prev.data))
                        + b"\x00"
                    )
                    if any(i == prev_idx for _t, i, _p in notes[:-1]):
                        new_idx = _add_sample(
                            out, by_key, body, prev.c4_rate, prev.onset,
                        )
                    else:
                        by_key.pop((prev.data, prev.c4_rate), None)
                        out[prev_idx] = DacSample(
                            prev.start, len(body), body, prev.c4_rate, prev.onset,
                        )
                        by_key[(body, prev.c4_rate)] = prev_idx
                        new_idx = prev_idx
                    notes[-1] = (prev_time, new_idx, prev_pan)
                    previous_sample = new_idx
                    previous_time = prev_time
                    stats["dac_silence_tail_bytes"] = (
                        stats.get("dac_silence_tail_bytes", 0)
                        + len(body) - len(prev.data)
                    )
            continue
        first = items[0]
        _row, delay = place(first.onset)
        note_time = placed_time(_row, delay)
        rate = _group_rate(samples, items, next_onset)
        if rate > DAC_RATE_MAX:
            rate = DAC_RATE_MAX
            stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
        elif rate < DAC_RATE_MIN:
            rate = DAC_RATE_MIN
            stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
        lead = max(0, first.onset - note_time)
        lead_frames = int(round(lead * rate / VGM_RATE))
        data = bytearray()
        held = _held_byte(out, previous_sample, previous_time, note_time)
        if lead_frames:
            data.extend(bytes([held]) * lead_frames)
            stats["dac_lead_bytes"] = stats.get("dac_lead_bytes", 0) + lead_frames

        note_pan = first.pan
        for i, p in enumerate(items):
            target = int(round((p.onset - note_time) * rate / VGM_RATE))
            if len(data) > target:
                over = len(data) - target
                stats["dac_overshoot"] = stats.get("dac_overshoot", 0) + over
            else:
                if target > len(data):
                    fill = target - len(data)
                    data.extend(bytes([held]) * fill)
                    stats["dac_hold_bytes"] = stats.get("dac_hold_bytes", 0) + fill
            payload = samples[p.sample].data
            data.extend(payload)
            held = payload[-1]
            if i and p.pan != items[i - 1].pan:
                # Pan is channel state, not a note: land it at the row start.
                # An ED on this cell would delay the row's note as well, and
                # the note's own onset is the more important timing.
                prow, _pdelay = place(p.onset)
                pan_events.append((placed_time(prow, 0), p.pan))

        body = bytes(data)
        idx = _add_sample(out, by_key, body, rate, note_time)
        notes.append((note_time, idx, note_pan))
        previous_sample = idx
        previous_time = note_time
        stats["dac_groups"] = stats.get("dac_groups", 0) + 1

    stats["dac_unique_samples"] = len(out)
    stats["dac_notes"] = len(notes)
    stats["dac_sample_bytes"] = sum(len(s.data) for s in out)
    stats["dac_maps"] = (len(out) + map_capacity - 1) // map_capacity
    warnings: list[str] = []
    if stats.get("dac_overshoot"):
        warnings.append(
            f"{stats['dac_overshoot']} YM2612 DAC samples were longer than the "
            "gap to the next run; the later bytes start late by that many frames"
        )
    return out, notes, pan_events, warnings, stats


def _collect_timeline(
    vgm: VgmFile,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Compatibility fallback: one continuous 22.05 kHz sample per phrase.

    Kept so the sliced path can be compared against the old representation.
    It materialises held and silent frames and is not the default.
    """
    warnings: list[str] = []
    bank = _signed_bank(bytes(vgm.dac_data))
    if not bank:
        return [], [], warnings, {}
    pan_timeline = _pan_timeline(vgm)
    samples: list[DacSample] = []
    plays: list[DacPlay] = []
    start = 0
    pos = 0
    run_start: int | None = None
    run_last = 0
    out = bytearray()
    cur = 0x80
    last_idx = 0

    def flush() -> None:
        nonlocal run_start, out, cur, last_idx
        if run_start is None:
            return
        need = int(round((run_last - run_start + 1) * DAC_TIMELINE_RATE / VGM_RATE))
        if need > len(out):
            out.extend(bytes([cur]) * (need - len(out)))
        if len(out) >= DAC_MIN_SAMPLES:
            data = bytes(out)
            idx = len(samples)
            samples.append(DacSample(-1, len(data), data, DAC_TIMELINE_RATE, run_start))
            plays.append(DacPlay(
                run_start, idx, _pan_at(pan_timeline, run_start),
                len(data) * VGM_RATE // DAC_TIMELINE_RATE,
            ))
        run_start = None
        out = bytearray()
        cur = 0x80
        last_idx = 0

    for t, kind, value in vgm.dac_events:
        if kind == 0:
            start = max(0, value)
            pos = 0
            continue
        if kind == 2:
            byte = (value - 0x80) & 0xFF
        elif kind == 1:
            if start + pos >= len(bank):
                continue
            byte = bank[start + pos] & 0xFF
            pos += 1
        else:
            continue
        if run_start is None:
            run_start = t
            run_last = t
            out = bytearray()
            cur = byte
            out.append(byte)
            last_idx = 0
            continue
        if t - run_last > DAC_RUN_GAP:
            flush()
            run_start = t
            run_last = t
            out = bytearray()
            cur = byte
            out.append(byte)
            last_idx = 0
            continue
        idx = int(round((t - run_start) * DAC_TIMELINE_RATE / VGM_RATE))
        if idx < last_idx:
            idx = last_idx
        if idx > len(out):
            out.extend(bytes([cur]) * (idx - len(out)))
        if idx == len(out):
            out.append(byte)
        else:
            out[idx] = byte
        last_idx = idx
        run_last = t
        cur = byte
    flush()
    return samples, plays, warnings, {"dac_timeline": 1}
