"""Reconvert the C140 packs (Namco System 2/21) with the current converter."""
import subprocess
import sys
from pathlib import Path

BASE = Path(r"C:\Users\User\Desktop\Pets\ripper\tools\arcade-vgm-rip")
FUR_DIR = BASE / "vgm2151fur"
PACKS = {
    "assault": r"VGM_collection\newest\Assault_(Namco_System_2)",
    "cyber-sled": r"VGM_collection\newest\Cyber_Sled_(Namco_System_21)",
    "metal-hawk": r"VGM_collection\newest\Metal_Hawk_(Namco_System_2)",
    "mirai-ninja": r"VGM_collection\newest\Mirai_Ninja_(Namco_System_2)",
    "starblade": r"VGM_collection\newest\Starblade_(Namco_System_21)",
    "valkyrie": r"VGM_collection\newest\Valkyrie_no_Densetsu_(Namco_System_2)",
}
failed = []
ok = 0
for slug, rel in PACKS.items():
    outdir = BASE / "output" / slug / "fur"
    outdir.mkdir(parents=True, exist_ok=True)
    for vgz in sorted((BASE / rel).glob("*.vgz")):
        proc = subprocess.run([sys.executable, "-m", "vgm2151fur", "convert",
                               str(vgz), "-o", str(outdir)],
                              cwd=str(FUR_DIR), capture_output=True, text=True)
        fur = outdir / f"{vgz.stem}.fur"
        if proc.returncode != 0 or not fur.is_file():
            failed.append(vgz.name)
            print(f"FAIL {vgz.name}: {(proc.stdout or '')[-160:]}")
        else:
            ok += 1
            print(f"ok   {vgz.stem[:50]:50s} {fur.stat().st_size:8d} bytes", flush=True)
print(f"done: {ok} ok, {len(failed)} failed")
