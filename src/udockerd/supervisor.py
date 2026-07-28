"""Installs the container process supervisor (see supervisor.c).

Prefers a prebuilt static binary for the host arch; falls back to
compiling supervisor.c with cc/gcc/clang if none is bundled.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from udockerd import __version__

_CACHE_DIR = Path.home() / ".udockerd"
_COMPILERS = ("cc", "gcc", "clang")

_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


def _source_text() -> str:
    return resources.files("udockerd").joinpath("data", "supervisor.c").read_text()


def _cached_binary_path(name: str) -> Path:
    return _CACHE_DIR / f"udockerd-supervisor-{name}"


def _find_compiler() -> str:
    for candidate in _COMPILERS:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "udockerd requires a C compiler on PATH (gcc or clang) to build its "
        "container process supervisor"
    )


def _install_prebuilt() -> Path | None:
    arch = _ARCH_ALIASES.get(platform.machine().lower())
    if arch is None:
        return None

    prebuilt = resources.files("udockerd").joinpath("data", "bin", f"supervisor-{arch}")
    if not prebuilt.is_file():
        return None

    binary_path = _cached_binary_path(f"prebuilt-{arch}-{__version__}")
    if binary_path.exists():
        return binary_path

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = binary_path.with_suffix(".tmp")
    tmp_path.write_bytes(prebuilt.read_bytes())
    tmp_path.chmod(0o755)
    tmp_path.rename(binary_path)
    return binary_path


def _compile_from_source() -> Path:
    source = _source_text()
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    binary_path = _cached_binary_path(f"compiled-{digest}")
    if binary_path.exists():
        return binary_path

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    source_path = _CACHE_DIR / "supervisor.c"
    source_path.write_text(source)

    compiler = _find_compiler()
    tmp_path = binary_path.with_suffix(".tmp")
    result = subprocess.run(  # noqa: S603
        [compiler, "-O2", "-o", str(tmp_path), str(source_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to compile udockerd supervisor: {result.stderr}")

    tmp_path.chmod(0o755)
    tmp_path.rename(binary_path)
    return binary_path


def ensure_supervisor() -> Path:
    """Idempotent: caches the installed/compiled binary, refreshing on upgrade."""
    return _install_prebuilt() or _compile_from_source()
