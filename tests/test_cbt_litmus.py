import tempfile
import unittest
from pathlib import Path

from vgm2151fur.convert import convert_vgm
from vgm2151fur.diag import export_fur
from vgm2151fur.vgm import load_vgm

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "VGM_collection" / "YM2151" / "CBT wav to ym2151 edge case test.vgm"


def _tl_values(vgm, sample, reg, window):
    vals = set()
    for w in vgm.writes:
        if w.sample > sample + window:
            break
        if w.chip == "ym2151" and w.reg == reg and w.sample >= sample:
            vals.add(w.val)
    return vals


def _last_tl(vgm, sample, reg):
    last = None
    for w in vgm.writes:
        if w.sample > sample:
            break
        if w.chip == "ym2151" and w.reg == reg:
            last = w.val
    return last


class CbtLitmusTest(unittest.TestCase):
    def test_carrier_tl_and_operator_flattening(self):
        if not SRC.is_file():
            self.skipTest("CBT edge-case VGM is not present")
        src = load_vgm(SRC)
        keyons = [
            (w.sample, w.val)
            for w in src.writes
            if w.chip == "ym2151" and w.reg == 0x08
            and (w.val & 0x78) and (w.val & 7) == 0
        ]
        self.assertGreaterEqual(len(keyons), 8)
        with tempfile.TemporaryDirectory() as td:
            fur = Path(td) / "cbt.fur"
            vgm_path = Path(td) / "cbt.vgm"
            convert_vgm(SRC, fur)
            export_fur(fur, vgm_path)
            out = load_vgm(vgm_path)
            out_keys = [
                w.sample for w in out.writes
                if w.chip == "ym2151" and w.reg == 0x08
                and (w.val & 0x78) and (w.val & 7) == 0
            ]
            checked = 0
            for sample, val in keyons:
                if not out_keys:
                    break
                near = min(out_keys, key=lambda t: abs(t - sample))
                if abs(near - sample) > 2000:
                    continue
                checked += 1
                for r in (0x60, 0x68, 0x70):
                    self.assertEqual(
                        _last_tl(out, near, r), 127,
                        f"unkeyed operator {r:#x} not muted at "
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
            self.assertGreaterEqual(checked, 6)


if __name__ == "__main__":
    unittest.main()
