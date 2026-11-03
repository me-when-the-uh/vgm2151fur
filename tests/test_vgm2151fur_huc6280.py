"""HuC6280 (PC Engine) wavetable, direct-D/A PCM and clock parsing."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_NES, CHIP_PCE, INS_PCE, write_fur,
)
from vgm2151fur.huc6280 import (
    HUC6280_DEFAULT_CLOCK, _dda_to_s8, collect_huc6280,
    collect_huc6280_pcm, huc6280_note, huc6280_period_hz,
)
from vgm2151fur.vgm import ChipWrite, VgmFile, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_huc6280"
TMP.mkdir(exist_ok=True)


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), **kw,
    )


def _raw_vgm(body: bytes, *, huc_clock: int = 0, nes_clock: int = 0,
             blocks=()) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    if huc_clock:
        buf[0xA4:0xA8] = (huc_clock & 0xFFFFFFFF).to_bytes(4, "little")
    if nes_clock:
        buf[0x84:0x88] = (nes_clock & 0xFFFFFFFF).to_bytes(4, "little")
    body_parts = bytearray()
    for typ, payload in blocks:
        body_parts += (
            bytes([0x67, 0x66, typ]) + len(payload).to_bytes(4, "little")
            + payload
        )
    body_parts += body + b"\x66"
    payload = bytes(buf) + bytes(body_parts)
    out = bytearray(payload)
    out[4:8] = (len(out) - 4).to_bytes(4, "little")
    return bytes(out)


def _w(sample, reg, val, chip_id=0):
    return ChipWrite(sample, "huc6280", chip_id, reg, val)


def _load_wave(wave_vals, period=0x100, volume=31, balance=0xFF,
               sample=0, channel=0):
    """The register sequence a PCE driver uses to start one wavetable note."""
    writes = [
        _w(sample, 0x01, 0xFF),
        _w(sample, 0x00, channel),
        _w(sample, 0x04, 0x00),      # clear DDA -> waveform index 0
    ]
    for v in wave_vals:
        writes.append(_w(sample, 0x06, v))
    writes += [
        _w(sample, 0x04, 0x40),      # DDA mode: forces index reset on enable
        _w(sample, 0x02, period & 0xFF),
        _w(sample, 0x03, (period >> 8) & 0x0F),
        _w(sample, 0x05, balance),
        _w(sample, 0x04, 0x80 | (volume & 0x1F)),
    ]
    return writes


def _square() -> bytes:
    return bytes([31] * 16 + [0] * 16)


def _sine() -> bytes:
    return bytes(int(round(15.5 + 15.5 * math.sin(2 * math.pi * i / 32)))
                 for i in range(32))


def _wave_values(block: bytes) -> tuple[int, list[int]]:
    name_end = block.index(0)
    pos = name_end + 1
    length = struct.unpack_from("<I", block, pos)[0]
    pos += 4
    pos += 4  # min
    maximum = struct.unpack_from("<I", block, pos)[0]
    pos += 4
    return maximum, [
        struct.unpack_from("<I", block, pos + 4 * i)[0] for i in range(length)
    ]


class TestHuc6280Parsing(unittest.TestCase):
    def test_register_clock_and_pcm_block(self):
        body = bytes([
            0xB9, 0x00, 0x02,        # select channel 2
            0xB9, 0x04, 0x9F,        # enable + volume 31
            0xB9, 0x06, 0x1F,        # waveform byte
        ])
        raw = _raw_vgm(
            body, huc_clock=3579545,
            blocks=[(0x05, bytes(range(16)))],
        )
        p = TMP / "parse_huc.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.huc6280_clock, HUC6280_DEFAULT_CLOCK)
        self.assertEqual(
            [(w.reg, w.val) for w in v.writes if w.chip == "huc6280"],
            [(0x00, 0x02), (0x04, 0x9F), (0x06, 0x1F)],
        )
        self.assertEqual(bytes(v.huc6280_pcm), bytes(range(16)))
        self.assertEqual(len(v.huc6280_pcm_blocks), 1)

    def test_nes_clock_is_at_0x84(self):
        # Regression: 0x80 is the Game Boy DMG clock; the NES moved to 0x84.
        raw = _raw_vgm(b"", nes_clock=1789773)
        p = TMP / "nes_clock.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.nes_clock, 1789773)

    def test_second_chip_bit(self):
        body = bytes([0xB9, 0x80 | 0x04, 0x1F])
        raw = _raw_vgm(body, huc_clock=3579545)
        p = TMP / "second.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        writes = [w for w in v.writes if w.chip == "huc6280"]
        self.assertEqual(writes[0].chip_id, 1)
        self.assertEqual(writes[0].reg, 0x04)


class TestHuc6280Tones(unittest.TestCase):
    def test_wave_note(self):
        vgm = _vgm(_load_wave(_square()), huc6280_clock=3579545)
        notes, offs, waves, warn = collect_huc6280(vgm)
        self.assertEqual(len(notes), 1)
        self.assertEqual(warn, [])
        hit = notes[0].hit
        self.assertEqual(hit.voice, 0)
        self.assertEqual(hit.period, 0x100)
        self.assertEqual(hit.vol, 31)
        self.assertEqual(hit.wave, _square())
        self.assertEqual(waves, [_square()])

    def test_wave_dedup(self):
        # Two channels playing the same image share one wavetable.
        writes = _load_wave(_square(), channel=0)
        writes += _load_wave(_square(), channel=1)
        notes, _offs, waves, _warn = collect_huc6280(
            _vgm(writes, huc6280_clock=3579545)
        )
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(waves), 1)

    def test_note_mapping_round_trip(self):
        # Furnace writes the period register back unchanged.
        for period in (0x100, 0x3F9, 0x0FF):
            note, e5 = huc6280_note(3579545, period)
            self.assertGreaterEqual(note, 0)
            self.assertLessEqual(note, 179)
            self.assertGreaterEqual(e5, 0)
            self.assertLessEqual(e5, 255)
        self.assertAlmostEqual(huc6280_period_hz(3579545, 0x100), 437.0, places=1)

    def test_gate_off_closes_note(self):
        writes = _load_wave(_square(), sample=0)
        writes.append(_w(10000, 0x04, 0x00))  # disable the channel
        notes, offs, _waves, _warn = collect_huc6280(
            _vgm(writes, huc6280_clock=3579545)
        )
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].hit.off_sample, 10000)
        self.assertEqual(offs[0].sample_time, 10000)

    def test_module_output(self):
        song = analyze(_vgm(_load_wave(_sine()), huc6280_clock=3579545))
        self.assertEqual(song.huc6280_ids, [0])
        self.assertEqual(song.huc6280_count, 1)
        self.assertEqual(len(song.huc6280_waves), 1)
        self.assertEqual(len(song.huc6280_ins), 1)
        data = write_fur(song, include_pcm=True, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_PCE])
        self.assertEqual(CHIP_CHANNELS[CHIP_PCE], 6)
        self.assertEqual(mod.n_ch, 6)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertEqual(ins_types, [INS_PCE])
        waves = [
            block for _off, mag, _size, block in iter_blocks(data)
            if mag == b"WAVE"
        ]
        self.assertEqual(len(waves), 1)
        maximum, values = _wave_values(waves[0])
        self.assertEqual(maximum, 31)
        self.assertEqual(values, list(_sine()))

    def test_volume_and_balance(self):
        notes, _offs, _waves, _warn = collect_huc6280(
            _vgm(
                _load_wave(_square(), volume=20, balance=0xB9),
                huc6280_clock=3579545,
            )
        )
        hit = notes[0].hit
        self.assertEqual(hit.vol, 20)
        self.assertEqual(hit.balance, 0xB9)

    def test_balance_envelope_kept_as_row_steps(self):
        # LOX2-style drivers fade with the balance register while the control
        # volume stays at 31.  Those steps must reach the pattern as 08xy,
        # not be folded away by an empty volume macro.
        writes = _load_wave(_square(), sample=0)
        writes += [_w(5000, 0x00, 0), _w(5000, 0x05, 0x87)]
        song = analyze(_vgm(writes, huc6280_clock=3579545))
        note = [e for e in song.events if e.on and e.ch >= 8][0]
        self.assertTrue(note.vol_pts)
        # The balance 0x87 attenuates to an effective level of 16/31, carried
        # on the volume column; the normalized L/R goes on 08xy.
        self.assertEqual(note.vol_pts[-1][1], 16)
        self.assertEqual(note.vol_pts[-1][2], 0xFE)
        # And the module still writes (it would fail if the row steps were
        # dropped and the empty macro left the note without a level).
        data = write_fur(song, include_pcm=True, include_fm=False)
        self.assertEqual(parse_fur(data).chips, [CHIP_PCE])

    def test_noise_transient_then_tone_tail(self):
        # A percussion hit: noise on at the gate, then 0x07=0 mid-gate
        # switches back to the waveform.  It stays one note; the switch is the
        # instrument's noise macro so it lands on the tick, not the row.
        writes = [
            _w(0, 0x01, 0xFF), _w(0, 0x00, 4), _w(0, 0x04, 0x00),
        ]
        for v in _square():
            writes.append(_w(0, 0x06, v))
        writes += [
            _w(0, 0x04, 0x40),
            _w(0, 0x02, 0x00), _w(0, 0x03, 0x04),  # period 0x400
            _w(0, 0x07, 0x9D),                     # noise on, n=29
            _w(0, 0x05, 0xFF), _w(0, 0x04, 0x9F),  # gate
            _w(500, 0x07, 0x00),                   # noise off
            _w(500, 0x02, 0x00), _w(500, 0x03, 0x02),  # tone-tail pitch
            _w(5000, 0x04, 0x00),                  # gate off
        ]
        song = analyze(_vgm(writes, huc6280_clock=3579545))
        ch4 = [e for e in song.events if e.on and e.ch == 12]
        self.assertEqual(len(ch4), 1)
        note = ch4[0]
        self.assertTrue(note.tone_noise)
        self.assertEqual(note.tone_noise_end, 500)
        # The instrument's noise macro switches on and then off.
        macros = [i.noise for i in song.huc6280_ins if i.noise]
        self.assertTrue(macros)
        macro = macros[0]
        self.assertEqual(macro[0], 1)
        self.assertIn(0, macro)

    def test_noise_note_before_waveform_load(self):
        # A snare can fire before the channel has loaded any waveform.  Noise
        # does not use the wave, so the note must survive the all-zero check.
        writes = [
            _w(0, 0x01, 0xFF), _w(0, 0x00, 5),
            _w(0, 0x07, 0x9F), _w(0, 0x05, 0xFF), _w(0, 0x04, 0x9F),
            _w(5000, 0x04, 0x00),
        ]
        song = analyze(_vgm(writes, huc6280_clock=3579545))
        ch5 = [e for e in song.events if e.on and e.ch == 13]
        self.assertEqual(len(ch5), 1)
        self.assertTrue(ch5[0].tone_noise)

    def test_every_pce_instrument_has_a_noise_macro(self):
        # Every converted PCE instrument states its noise mode explicitly so a
        # following tone note always clears a preceding noise hit.
        song = analyze(_vgm(_load_wave(_square()), huc6280_clock=3579545))
        self.assertTrue(song.huc6280_ins)
        self.assertTrue(all(i.noise for i in song.huc6280_ins))
        self.assertTrue(all(i.noise == (0,) for i in song.huc6280_ins))

    def test_no_empty_opm_for_non_fm_source(self):
        # A PCE-only rip must not gain the analyzer's YM2151 placeholder: the
        # extra device rescales the mix and shows eight silent columns.
        song = analyze(_vgm(_load_wave(_square()), huc6280_clock=3579545))
        data = write_fur(song, include_pcm=True)  # include_fm defaults to True
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_PCE])
        self.assertEqual(mod.n_ch, 6)


class TestHuc6280Pcm(unittest.TestCase):
    def test_play_block_and_offset(self):
        pcm = bytes([16, 20, 24, 28, 24, 20, 16, 12, 8, 4, 0, 4, 8, 12,
                     16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16,
                     16, 16, 16, 16, 16])
        # Setup one stream on channel 2, register 6 (direct D/A).
        body = bytes([
            0x90, 0x00, 27, 0x02, 0x06,          # setup: stream 0, ch 2, reg 6
            0x91, 0x00, 0x05, 0x01, 0x00,        # bank 5, step 1, base 0
            0x92, 0x00, 0x40, 0x1F, 0x00, 0x00,  # 8000 Hz
            0x95, 0x00, 0x00, 0x00, 0x00,        # play block 0
            0x94, 0x00,                          # stop
        ])
        raw = _raw_vgm(
            body, huc_clock=3579545, blocks=[(0x05, pcm)],
        )
        p = TMP / "pcm.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        samples, hits, warn = collect_huc6280_pcm(v)
        self.assertEqual(warn, [])
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(samples[0].rate, 8000)
        self.assertEqual(hits[0].voice, 2)
        # Every DDA level maps back to the register value.
        self.assertEqual(samples[0].data, _dda_to_s8(pcm))

    def test_dda_conversion_round_trip(self):
        raw = bytes(range(32))
        converted = _dda_to_s8(raw)
        for v, b in zip(raw, converted):
            signed = struct.unpack("<b", bytes([b ^ 0x80]))[0]
            self.assertEqual((signed >> 3) & 0x1F, v)

    def test_module_has_sample_instruments(self):
        pcm = bytes([16, 20, 24, 28, 24, 20, 16, 12] * 64)
        body = bytes([
            0x90, 0x00, 27, 0x00, 0x06,
            0x91, 0x00, 0x05, 0x01, 0x00,
            0x92, 0x00, 40, 0x1F, 0x00, 0x00,
            0x95, 0x00, 0x00, 0x00, 0x00,
        ])
        raw = _raw_vgm(body, huc_clock=3579545, blocks=[(0x05, pcm)])
        p = TMP / "pcm_mod.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        song = analyze(v)
        self.assertEqual(len(song.huc6280_pcm_samples), 1)
        self.assertEqual(song.huc6280_pcm_count, 1)
        data = write_fur(song, include_pcm=True, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_PCE])
        self.assertEqual(len(mod.samples), 1)
        pcm_notes = [e for e in song.events if e.on and e.pcm and e.note == 108]
        self.assertEqual(len(pcm_notes), 1)


if __name__ == "__main__":
    unittest.main()
