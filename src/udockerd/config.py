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
    """
    os.environ.setdefault("UDOCKER_USE_CURL_EXECUTABLE", "curl")
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
