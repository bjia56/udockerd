"""Compiles and caches the container process supervisor (supervisor.c).

Cosmopolitan Python has no ctypes (_ctypes isn't compiled in), so PDEATHSIG
can't be set via a preexec_fn in-process. Instead we shell out to a tiny
compiled C supervisor that forks, sets PDEATHSIG, and reaps/cleans up its
subtree — see supervisor.c for why a plain exec wrapper isn't enough.
Requires a C compiler (gcc or clang) on PATH at runtime — same tier of
external dependency as requiring curl (see config.py).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from importlib import resources
from pathlib import Path

_CACHE_DIR = Path.home() / ".udockerd"
_COMPILERS = ("cc", "gcc", "clang")


def _source_text() -> str:
    return resources.files("udockerd").joinpath("data", "supervisor.c").read_text()


def _cached_binary_path(source: str) -> Path:
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return _CACHE_DIR / f"udockerd-supervisor-{digest}"


def _find_compiler() -> str:
    for candidate in _COMPILERS:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "udockerd requires a C compiler on PATH (gcc or clang) to build its "
        "container process supervisor"
    )


def ensure_supervisor() -> Path:
    """Idempotent: compiles the supervisor once, reuses the cached binary
    on subsequent calls (and subsequent daemon runs, keyed by source hash
    so a udockerd upgrade with a changed supervisor recompiles
    automatically).
    """
    source = _source_text()
    binary_path = _cached_binary_path(source)
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
