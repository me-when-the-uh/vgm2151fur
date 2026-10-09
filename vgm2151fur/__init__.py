"""vgm2151fur: transcribe arcade YM2151 + sample-chip VGMs into Furnace .fur.

A 90/10 tool, not a general VGM importer. It wires OPM key-on, KC, KF and
patch writes plus the sample chips (K007232, MSM6295, MSM6258, SegaPCM, C140,
C352) into a .fur that Furnace plays and a human can finish by hand.
Furnace 0.6.8.3 does not emulate C352 (chip 0xD0); those channels are still
written. See docs/c352.md. The operating guide is docs/operating.md.
"""

from __future__ import annotations

__version__ = "0.9.40"

from vgm2151fur.convert import convert_vgm, dump_pcm, report_vgm

__all__ = ["convert_vgm", "dump_pcm", "report_vgm", "__version__"]
