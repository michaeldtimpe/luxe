#!/usr/bin/env python3
"""Install luxe's vendored oMLX patches into the active oMLX installation.

oMLX ships as a Homebrew formula with its own bundled Python env, so patches
live under ``<cellar>/libexec/.../site-packages/omlx/patches/``. That tree is
**replaced wholesale by ``brew upgrade omlx``**, which is exactly why the
sources are authored and tested in this repo and merely *copied* there.

    uv run python vendor/omlx_patches/install.py --check
    uv run python vendor/omlx_patches/install.py
    uv run python vendor/omlx_patches/install.py --uninstall

Wiring into ``omlx/oq.py`` is deliberately NOT automated — see ``--check``
output. That file is a 309 KB vendor source whose edit cannot be exercised
until the Muse Glimmer architecture lands (open PR Blaizzy/mlx-vlm#1838), and
an untestable automated edit to it is a worse trade than a printed snippet.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHES = ["muse_glimmer"]

WIRING_SNIPPET = '''\
# --- luxe: muse_glimmer ATEM tool parser -------------------------------
# Inference-only; safe to apply unconditionally. Place next to the other
# apply_*_patch() calls in oq.py (near the deepseek_v4 block, ~line 3666).
try:
    from omlx.patches.muse_glimmer import apply_muse_glimmer_patch

    apply_muse_glimmer_patch()
except Exception as patch_err:  # pragma: no cover - vendor tree
    logger.debug(f"muse_glimmer patch not applied: {patch_err}")
# -----------------------------------------------------------------------
'''


def omlx_patches_dir() -> Path | None:
    """Locate the patches dir inside the oMLX Homebrew env."""
    omlx_bin = shutil.which("omlx")
    if not omlx_bin:
        return None
    try:
        shebang = Path(omlx_bin).resolve().read_text(errors="ignore").split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    interpreter = shebang[2:].strip()
    try:
        out = subprocess.run(
            [interpreter, "-c", "import omlx, pathlib; print(pathlib.Path(omlx.__file__).parent)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip()) / "patches"


def is_wired(patches: Path) -> bool:
    oq = patches.parent / "oq.py"
    try:
        return "apply_muse_glimmer_patch" in oq.read_text(errors="ignore")
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report status, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove installed patches")
    args = ap.parse_args()

    patches = omlx_patches_dir()
    if patches is None or not patches.is_dir():
        print("omlx patches dir not found - is oMLX installed and on PATH?")
        return 2

    print(f"oMLX patches dir: {patches}")

    for name in PATCHES:
        src, dst = HERE / name, patches / name

        if args.uninstall:
            if dst.exists():
                shutil.rmtree(dst)
                print(f"  removed  {name}")
            else:
                print(f"  absent   {name}")
            continue

        if args.check:
            state = "installed" if dst.exists() else "NOT installed"
            print(f"  {name}: {state}")
            continue

        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"  installed {name} -> {dst}")

    if not args.uninstall:
        wired = is_wired(patches)
        print(f"\noq.py wiring: {'PRESENT' if wired else 'MISSING'}")
        if not wired:
            print(
                "\nThe patch is copied but nothing calls it yet. Add this to\n"
                f"{patches.parent / 'oq.py'} beside the other apply_*_patch() calls:\n"
            )
            print(WIRING_SNIPPET)
        print("NOTE: `brew upgrade omlx` replaces this tree - re-run after upgrades.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
