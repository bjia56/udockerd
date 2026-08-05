"""udockerd startup configuration, including forcing udocker's pure-python HTTP path."""

import os
import platform
import shutil


def is_termux() -> bool:
    """PREFIX is always set in a Termux shell environment; TERMUX_VERSION
    is set once termux-tools is installed. Checked in two places: which
    proot binary to prefer, and whether to skip straight to the
    proot-wrapped tar extraction for hardlink handling (see udocker_ctx.py).
    """
    return "TERMUX_VERSION" in os.environ or os.environ.get(
        "PREFIX", ""
    ).startswith("/data/data/com.termux")


def _termux_proot() -> str | None:
    """Termux/proot-distro deployments almost always already have Termux's
    own `proot` package installed, since proot-distro (the guest the docker
    CLI runs from) itself depends on it to create/enter distro rootfs.
    Termux's build is maintained specifically for Android SELinux/seccomp
    quirks, so prefer it over udocker's bundled proot when it's on PATH.
    Flag differences from udocker's assumed bundle are already probed
    dynamically elsewhere (HostInfo.cmd_has_option), so a mismatch degrades
    gracefully instead of breaking.
    """
    if not is_termux():
        return None
    return shutil.which("proot")


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

    On Termux, points udocker at the system `proot` package instead of its
    own bundled binary (UDOCKER_USE_PROOT_EXECUTABLE, read by udocker's own
    Config().getconf()) when one is found on PATH — see `_termux_proot`.
    """
    os.environ.setdefault("UDOCKER_USE_CURL_EXECUTABLE", "curl")
    os.environ.setdefault("PROOT_NO_SECCOMP", "1")
    termux_proot = _termux_proot()
    if termux_proot is not None:
        os.environ.setdefault("UDOCKER_USE_PROOT_EXECUTABLE", termux_proot)
    if data_dir is not None:
        os.environ["UDOCKER_DIR"] = data_dir


def check_curl_available() -> None:
    if shutil.which("curl") is None:
        raise RuntimeError(
            "udockerd requires the 'curl' executable on PATH"
        )


def check_tar_available() -> None:
    """Both udocker's own image-layer extraction (ContainerStructure via
    udocker_ctx.py's proot-retry patch) and builder.py's COPY/ADD/layer-flatten
    paths shell out to a bare `tar` on PATH. Checked at startup, same as
    curl, so a missing tar fails loudly and immediately instead of surfacing
    deep inside a request as a bare `[Errno 2] No such file or directory: 'tar'`.
    """
    if shutil.which("tar") is None:
        raise RuntimeError(
            "udockerd requires the 'tar' executable on PATH"
        )


def check_linux() -> None:
    """udockerd only runs on Linux (proot has no other target)."""
    if platform.system() != "Linux":
        raise RuntimeError(
            f"udockerd only runs on Linux (detected: {platform.system()})"
        )
