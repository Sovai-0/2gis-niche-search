"""Сборка переносимого приложения на текущей платформе."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "desktop"
DIST = ROOT / "dist"
NAME = "2GIS Niche Search"


def main() -> None:
    system = platform.system()
    if system not in ("Windows", "Darwin"):
        raise SystemExit("Сборку запускай на Windows или macOS.")
    native_arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(native_arch, native_arch)
    if arch not in ("arm64", "x86_64"):
        raise SystemExit(f"Неподдерживаемая архитектура: {arch}")
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(exist_ok=True)
    options = ["--target-architecture", arch] if system == "Darwin" else []
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--onedir", "--name", NAME, "--collect-all", "playwright",
        "--workpath", str(WORK / "work"), "--distpath", str(DIST),
        "--specpath", str(WORK), "--paths", str(ROOT / "src"),
        *options, str(ROOT / "desktop.py"),
    ], cwd=ROOT, check=True)
    archive = DIST / f"2GIS-Niche-Search-{'macOS' if system == 'Darwin' else 'Windows'}-{arch}"
    if system == "Darwin":
        archive = archive.with_suffix(".zip")
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(DIST / f"{NAME}.app"), str(archive)], check=True)
    else:
        archive = shutil.make_archive(str(archive), "zip", root_dir=DIST, base_dir=NAME)
    print(archive)


if __name__ == "__main__":
    main()
