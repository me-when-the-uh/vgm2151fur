"""A synthetic YM2151 edge-case log through the converter and back.

The driver keys only the last operator of a four-operator voice, leaving the
other three un-keyed. The converter has to mute those operators in the
instrument (they never start an envelope) while keeping the carrier level that
was written at the key-on. The test runs a self-contained log through
`convert_vgm`/`export_fur` and checks the exported register writes.
"""

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.convert import convert_vgm
from vgm2151fur.diag import export_fur
from vgm2151fur.furnace import find_furnace
from vgm2151fur.vgm import load_vgm

CLOCK = 3579545
PULSE = 20000
TOTAL_SAMPLES = 188000
HAVE_FURNACE = find_furnace() is not None


def _last_tl(vgm, sample, reg):
    last = None
    for w in vgm.writes:
        if w.sample > sample:
            break
        if w.chip == "ym2151" and w.reg == reg:
            last = w.val
    return last


def _ym(reg, val):
    return bytes([0x54, reg & 0xFF, val & 0xFF])


def _wait(samples):
    return bytes([0x61]) + samples.to_bytes(2, "little")


def _raw_vgm(cmds: bytes, total_samples: int = TOTAL_SAMPLES) -> bytes:
    """A loadable 1.61 VGM with a YM2151 clock and a command body."""
    buf = bytearray(0x40)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = total_samples.to_bytes(4, "little")
    buf[0x30:0x34] = CLOCK.to_bytes(4, "little")
    buf[0x34:0x38] = (0x40 - 0x34).to_bytes(4, "little")
    raw = bytes(buf) + cmds + b"\x66"
    return raw[:4] + (len(raw) - 4).to_bytes(4, "little") + raw[8:]


def _voice_setup(ch: int, carrier_tl: int) -> bytes:
    """One algorithm-0 voice; only its last operator is keyed."""
    out = bytearray()
    for reg, val in [
        (0x20 | ch, 0xC0),                 # RL both, feedback 0, CON=0
        (0x60 | ch, 0x10),                 # op1 (un-keyed)
        (0x68 | ch, 0x12),                 # op2 (un-keyed)
        (0x70 | ch, 0x14),                 # op3 (un-keyed)
        (0x78 | ch, carrier_tl),           # op4, the carrier
        (0x80 | ch, 0x1F), (0x88 | ch, 0x1F),
        (0x90 | ch, 0x1F), (0x98 | ch, 0x1F),
        (0xA0 | ch, 0x00), (0xA8 | ch, 0x00),
        (0xB0 | ch, 0x00), (0xB8 | ch, 0x00),
        (0xC0 | ch, 0x00), (0xC8 | ch, 0x00),
        (0xD0 | ch, 0x00), (0xD8 | ch, 0x00),
        (0xE0 | ch, 0x0F), (0xE8 | ch, 0x0F),
        (0xF0 | ch, 0x0F), (0xF8 | ch, 0x0F),
        (0x28 | ch, 0x40), (0x30 | ch, 0x00),
    ]:
        out += _ym(reg, val)
    return bytes(out)


def _edge_case_log() -> bytes:
    cmds = bytearray()
    # A key-on heartbeat on the other channels gives the grid estimator a
    # regular pulse to work with.
    for ch in range(1, 8):
        cmds += _ym(0x08, 0x78 | ch)
        cmds += _wait(2000)
        cmds += _ym(0x08, ch)
        cmds += _wait(2000)
    # Eight held notes on channel 0, each keying only operator 4.
    for i in range(8):
        cmds += _voice_setup(0, 0x30 + i * 4)
        cmds += _ym(0x08, 0x40)            # mask bit for operator 4 only
        cmds += _wait(PULSE - 2000)
        cmds += _ym(0x08, 0)
        cmds += _wait(2000)
    return _raw_vgm(bytes(cmds))


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class CbtLitmusTest(unittest.TestCase):
    def test_carrier_tl_and_operator_flattening(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src_path = td / "cbt-edge.vgm"
            src_path.write_bytes(_edge_case_log())
            src = load_vgm(src_path)
            keyons = [
                (w.sample, w.val)
                for w in src.writes
                if w.chip == "ym2151" and w.reg == 0x08
                and (w.val & 0x78) and (w.val & 7) == 0
            ]
            self.assertGreaterEqual(len(keyons), 8)

            fur = td / "cbt-edge.fur"
            vgm_path = td / "cbt-edge-out.vgm"
            convert_vgm(src_path, fur)
            export_fur(fur, vgm_path)
            out = load_vgm(vgm_path)
            out_keys = [
                w.sample for w in out.writes
                if w.chip == "ym2151" and w.reg == 0x08
                and (w.val & 0x78) and (w.val & 7) == 0
            ]
            checked = 0
            for sample, _val in keyons:
                if not out_keys:
                    break
                near = min(out_keys, key=lambda t: abs(t - sample))
                if abs(near - sample) > 2000:
                    continue
                checked += 1
                for reg in (0x60, 0x68, 0x70):
                    self.assertEqual(
                        _last_tl(out, near, reg), 127,
                        f"unkeyed operator {reg:#x} not muted at "
                        f"{sample / 44100:.3f}s",
                    )
                self.assertEqual(
                    _last_tl(out, near, 0x78) is not None,
                    True,
                )
            src_key_tls = {
                _last_tl(src, sample, 0x78) for sample, _v in keyons
            }
            out_tls = {
                w.val for w in out.writes
                if w.chip == "ym2151" and w.reg == 0x78
            }
            self.assertEqual(src_key_tls - out_tls, set(),
                             "key-on carrier TL levels missing from the module")
            self.assertGreaterEqual(checked, 8)


if __name__ == "__main__":
    unittest.main()
