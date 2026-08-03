"""udockerd startup configuration, including forcing udocker's pure-python HTTP path."""

import os
import platform
import shutil


def configure_udocker(data_dir: str | None = None) -> None:
    """Force udocker onto its curl-executable path, never pycurl (a C
    extension that would break Cosmopolitan bundling). Set via env var
    since Config().getconf() re-reads it regardless of call order.

    `data_dir` overrides udocker's data directory (default ~/.udocker) via
    the same env var udocker itself reads (UDOCKER_DIR), so `--data-dir`
    behaves identically to setting UDOCKER_DIR by hand.

    Also forces proot's ptrace/seccomp-acceleration ("seccomp mode 2") off.
    udocker's PRootEngine.run() checks os.getenv("PROOT_NO_SECCOMP") directly
    (udocker/engine/proot.py), not through Config's env-override table, so
    this must be set here rather than via a udocker Config key. Android/Termux
    kernels routinely can't correctly emulate syscalls under that
    acceleration path, killing the traced process with SIGSYS (signal 31) —
    e.g. `apt-get install` inside `RUN` steps during `docker build`.
    """
    os.environ.setdefault("UDOCKER_USE_CURL_EXECUTABLE", "curl")
    os.environ.setdefault("PROOT_NO_SECCOMP", "1")
    if data_dir is not None:
        os.environ["UDOCKER_DIR"] = data_dir


def check_curl_available() -> None:
    if shutil.which("curl") is None:
        raise RuntimeError(
            "udockerd requires the 'curl' executable on PATH"
        )


def check_linux() -> None:
    """udockerd only runs on Linux (proot has no other target)."""
    if platform.system() != "Linux":
        raise RuntimeError(
            f"udockerd only runs on Linux (detected: {platform.system()})"
        )
