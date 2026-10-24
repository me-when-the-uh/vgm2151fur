"""vgm2151fur: transcribe arcade YM2151/YM2203 + sample-chip VGMs into .fur.

A 90/10 tool, not a general VGM importer. It wires YM2151 (OPM) key-on, KC,
KF and patch writes, and YM2203 (OPN) three-operator FM plus its AY-compatible
SSG, plus the chips around them: the sample players K007232, MSM6295, MSM6258,
SegaPCM, C140 and C352, and the pitched Konami SCC/SCC+ (K051649 / K052539)
and AY-8910. Furnace 0.6.8.3 does not emulate C352 (chip 0xD0); those channels
are still written. See docs/c352.md, docs/scc-ay8910.md and docs/ym2203.md.
The operating guide is docs/operating.md.
"""

from __future__ import annotations

__version__ = "0.9.41"

from vgm2151fur.convert import convert_vgm, dump_pcm, report_vgm

__all__ = ["convert_vgm", "dump_pcm", "report_vgm", "__version__"]
