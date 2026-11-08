"""Atari POKEY parsing, register model, collector and module emission."""

from __future__ import annotations

import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import CHIP_POKEY, INS_POKEY, write_fur
from vgm2151fur.pitch import module_clock, pitch_table
from vgm2151fur.pokey import (
    POKEY_DEFAULT_CLOCK,
    POKEY_PITCH_MAX,
    _furnace_wave,
    collect_pokey,
    furnace_written_register,
    pokey_note,
    pokey_value,
)
from vgm2151fur.vgm import (
    ChipWrite, VgmFile, chips_in_head, load_vgm,
)


def _vgm(writes, **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        writes=list(writes), pokey_clock=POKEY_DEFAULT_CLOCK, **kw,
    )


def _raw(cmds: bytes, clock: int = POKEY_DEFAULT_CLOCK) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    buf[0xB0:0xB4] = (clock & 0xFFFFFFFF).to_bytes(4, "little")
    raw = bytes(buf) + cmds + b"\x66"
    return raw[:4] + (len(raw) - 4).to_bytes(4, "little") + raw[8:]


def _pokey(sample, reg, val, chip_id=0):
    return ChipWrite(sample, "pokey", chip_id, reg, val)


class TestPokeyParsing(unittest.TestCase):
    def test_clock_and_dual_register_select(self):
        raw = _raw(
            bytes([0xBB, 0x00, 0xAF, 0xBB, 0x85, 0x12]),
            clock=0x40000000 | POKEY_DEFAULT_CLOCK,
        )
        path = Path("pokey_parse.vgm")
        path.write_bytes(raw)
        try:
            v = load_vgm(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(v.pokey_clock, POKEY_DEFAULT_CLOCK)
        self.assertTrue(v.pokey_dual)
        self.assertEqual(
            [(w.chip_id, w.reg, w.val) for w in v.writes
             if w.chip == "pokey"],
            [(0, 0x00, 0xAF), (1, 0x05, 0x12)],
        )

    def test_header_census(self):
        raw = _raw(bytes([0x66]), clock=0x40000000 | POKEY_DEFAULT_CLOCK)
        kinds = chips_in_head(raw)
        self.assertIn("pokey", kinds)
        self.assertIn("pokey_dual", kinds)


class TestPokeyPitch(unittest.TestCase):
    def test_plain_high_and_15khz_round_trip(self):
        cases = [
            (0x00, 0, 5, 100, None),
            (0x40, 0, 5, 100, None),   # high clock voice 1
            (0x20, 2, 5, 100, None),   # high clock voice 3
            (0x01, 2, 5, 200, None),   # 15 kHz
        ]
        for audctl, voice, wave, target, high in cases:
            t = pokey_value(
                target, audctl, voice, wave, joined=False, high=high,
            )
            self.assertEqual(
                furnace_written_register(t, audctl, voice, wave, False),
                target,
                (audctl, voice, wave, target),
            )

    def test_joined_binary_register(self):
        # Kalinka's joined 1+2 voice: AUDF1=0xA7, AUDF2=0x06, AUDCTL=0x50.
        t = pokey_value(
            0xA7, 0x50, 0, 5, joined=True, high=0x06,
        )
        self.assertEqual(
            furnace_written_register(t, 0x50, 0, 5, True), 0x06A7,
        )
        note, e5 = pokey_note(POKEY_DEFAULT_CLOCK, t)
        self.assertTrue(0 <= note <= 179)

    def test_waveform_snapping_round_trips(self):
        for wave in (6, 7):
            for audctl, voice, joined in ((0x00, 0, False), (0x40, 0, False)):
                t0 = pokey_value(80, audctl, voice, 5, joined=joined)
                target = furnace_written_register(t0, audctl, voice, wave, joined)
                t = pokey_value(
                    target & 0xFF, audctl, voice, wave, joined=joined,
                )
                self.assertEqual(
                    furnace_written_register(t, audctl, voice, wave, joined),
                    target,
                    (wave, audctl, voice, target),
                )


class TestPokeyCollector(unittest.TestCase):
    def test_gate_and_volume_envelope(self):
        writes = [_pokey(0, 8, 0x00), _pokey(0, 0, 100), _pokey(0, 1, 0xAF)]
        for k in range(1, 15):
            writes.append(_pokey(735 * k, 1, 0xA0 | (15 - k)))
        writes.append(_pokey(735 * 15, 1, 0x00))
        hits, offs, audctl, waves, _warn = collect_pokey(_vgm(writes))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual((hit.voice, hit.vol), (0, 15))
        self.assertEqual(len(hit.vol_pts), 14)
        self.assertEqual(hit.vol_pts[-1], (735 * 14, 1))
        self.assertEqual([o.sample_time for o in offs], [735 * 15])
        self.assertEqual(
            furnace_written_register(hit.value, 0, 0, 5, False), 100,
        )
        self.assertEqual(audctl, [])
        self.assertEqual(waves[0], (0, 0, 0, 5))

    def test_distortion_6_uses_long_poly4_wave(self):
        # Distortion 6 must use Furnace wave 7: both write AUDC 6, but wave
        # 6's short snap table cannot represent AUDF 51 (it skips 51/52),
        # which changes the poly4 sampling phase.  The original modules use
        # wave 7.
        self.assertEqual(_furnace_wave(6), 7)
        writes = [
            _pokey(0, 8, 0x00),
            _pokey(0, 2, 51), _pokey(0, 3, 0xCF),
            _pokey(735 * 4, 3, 0x00),
        ]
        hits, _offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.wave, 7)
        self.assertEqual(
            furnace_written_register(hit.value, 0, 1, 7, False), 51,
        )

    def test_filter_control_keeps_silent_note(self):
        # AUDCTL bit 2 filters channel 1 with channel 3.  Voice 3 is silent
        # (AUDC volume 0) but its AUDF still drives the filter, so it needs a
        # volume-0 note in the module.
        writes = [
            _pokey(0, 4, 190), _pokey(0, 5, 0xA0),
            _pokey(0, 0, 192), _pokey(0, 1, 0xAF),
            _pokey(0, 8, 0x04),
            _pokey(735 * 4, 8, 0x00),
        ]
        hits, _offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        control = [h for h in hits if h.voice == 2]
        self.assertEqual(len(control), 1)
        self.assertEqual(control[0].vol, 0)
        self.assertEqual(
            furnace_written_register(
                control[0].value, 0x04, 2, 5, False,
            ),
            190,
        )

    def test_pitch_plateau_hits_the_exact_register(self):
        # The exact note/E5 must land inside the transform's plateau, not on
        # its lower edge (which would write target-1).
        for target, audctl, voice, wave in (
            (192, 0x04, 0, 5),
            (51, 0x00, 1, 7),
            (102, 0x00, 1, 7),
            (190, 0x04, 2, 5),
        ):
            t = pokey_value(target, audctl, voice, wave)
            note, e5 = pokey_note(POKEY_DEFAULT_CLOCK, t)
            value = pitch_table(
                module_clock(POKEY_DEFAULT_CLOCK), 1,
                maximum=POKEY_PITCH_MAX,
            ).value(note, e5 - 0x80)
            self.assertEqual(
                furnace_written_register(value, audctl, voice, wave, False),
                target,
                (target, audctl, voice, wave),
            )

    def test_volume_rise_retriggers(self):
        writes = [
            _pokey(0, 0, 100), _pokey(0, 1, 0xAF),
            _pokey(2 * 735, 1, 0xA5),   # decayed? no, still loud: 15 -> 5 drop
            _pokey(3 * 735, 1, 0xAF),   # rise back to 15: new hit
            _pokey(4 * 735, 1, 0x00),
        ]
        hits, offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].sample_time, 0)
        self.assertEqual(hits[1].sample_time, 3 * 735)
        self.assertEqual([o.sample_time for o in offs], [4 * 735])

    def test_joined_pair_uses_high_audc(self):
        writes = [
            _pokey(0, 8, 0x50),
            _pokey(0, 0, 0xA7), _pokey(0, 2, 0x06),
            _pokey(0, 3, 0xA5),                 # AUDC2: vol 5, wave 5
            _pokey(600, 2, 0x07),               # high byte moves
            _pokey(1200, 3, 0x00),              # volume off
        ]
        hits, offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.voice, 0)
        self.assertEqual(hit.vol, 5)
        self.assertEqual(
            furnace_written_register(hit.value, 0x50, 0, 5, True), 0x06A7,
        )
        self.assertEqual(len(hit.freq_pts), 1)
        self.assertEqual(hit.freq_pts[0][0], 600)
        self.assertEqual(
            furnace_written_register(hit.freq_pts[0][1], 0x50, 0, 5, True),
            0x07A7,
        )
        self.assertEqual([o.sample_time for o in offs], [1200])

    def test_join_off_reopens_independent_voice(self):
        writes = [
            _pokey(0, 8, 0x50),
            _pokey(0, 0, 0xA7), _pokey(0, 2, 0x06),
            _pokey(0, 1, 0x00), _pokey(0, 3, 0xA5),
            _pokey(1000, 8, 0x00),   # join off; AUDC1=0, AUDC2=5
            _pokey(2000, 3, 0x00),
        ]
        hits, offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        self.assertTrue(any(h.voice == 1 for h in hits))
        conv = [h for h in hits if h.voice == 1]
        self.assertEqual(len(conv), 1)
        self.assertEqual(conv[0].sample_time, 1000)
        self.assertEqual([o.sample_time for o in offs], [2000])

    def test_dual_chip_ids(self):
        writes = [
            _pokey(0, 0, 100, 0), _pokey(0, 1, 0xAF, 0),
            _pokey(0, 0, 200, 1), _pokey(0, 1, 0xAF, 1),
            _pokey(735 * 10, 1, 0, 0), _pokey(735 * 10, 1, 0, 1),
        ]
        hits, _offs, _audctl, _waves, _warn = collect_pokey(_vgm(writes))
        self.assertEqual(sorted(h.chip_id for h in hits), [0, 1])


class TestPokeyWriter(unittest.TestCase):
    def test_chip_instrument_and_register_round_trip(self):
        writes = [
            _pokey(0, 8, 0x00), _pokey(0, 0, 100), _pokey(0, 1, 0xAF),
            _pokey(735 * 10, 1, 0x00),
        ]
        song = analyze(_vgm(writes))
        self.assertEqual(song.pokey_ids, [0])
        self.assertEqual(song.pokey_count, 1)
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_POKEY])
        self.assertEqual(mod.n_ch, 4)
        rows = mod.patterns[(0, 0)]
        note_row = next(r for r in rows if r.note >= 0)
        self.assertEqual(note_row.note, 111)
        self.assertIn(0x10, [cmd for cmd, _val in note_row.fx])
        e5 = next(
            (val for cmd, val in note_row.fx if cmd == 0xE5), 0x80,
        )
        self.assertEqual(
            furnace_written_register(
                pitch_table(
                    module_clock(POKEY_DEFAULT_CLOCK), 1,
                    maximum=POKEY_PITCH_MAX,
                ).value(note_row.note, e5 - 0x80),
                0, 0, 5, False,
            ),
            100,
        )
        self.assertEqual(
            next(r for r in rows if r.note == 180).note, 180,
        )

    def test_dual_chip_module_layout(self):
        writes = [
            _pokey(0, 0, 100, 0), _pokey(0, 1, 0xAF, 0),
            _pokey(0, 0, 200, 1), _pokey(0, 1, 0xAF, 1),
            _pokey(735 * 4, 1, 0, 0), _pokey(735 * 4, 1, 0, 1),
        ]
        song = analyze(_vgm(writes))
        self.assertEqual(song.pokey_ids, [0, 1])
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_POKEY, CHIP_POKEY])
        self.assertEqual(mod.n_ch, 8)

    def test_pokey_instrument_type(self):
        self.assertEqual(INS_POKEY, 20)


if __name__ == "__main__":
    unittest.main()
