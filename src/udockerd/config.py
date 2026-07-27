"""udockerd startup configuration, including forcing udocker's pure-python HTTP path."""

import os
import shutil


def configure_udocker() -> None:
    """Force udocker onto its curl-executable download path, never pycurl.

    pycurl is a C extension; importing it would break Cosmopolitan bundling
    (pure-python only). udocker falls back to shelling out to a `curl`
    executable when use_curl_executable is set to any non-empty value.

    Set via env var (not Config.conf directly) so it survives regardless of
    call order relative to udocker's own Config().getconf(), which re-reads
    this env var and only falls back to the in-memory value if unset.
    """
    os.environ.setdefault("UDOCKER_USE_CURL_EXECUTABLE", "curl")


def check_curl_available() -> None:
    if shutil.which("curl") is None:
        raise RuntimeError(
            "udockerd requires the 'curl' executable on PATH"
        )
