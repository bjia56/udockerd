"""udockerd startup configuration, including forcing udocker's pure-python HTTP path."""

import shutil

from udocker.config import Config


def configure_udocker() -> None:
    """Force udocker onto its curl-executable download path, never pycurl.

    pycurl is a C extension; importing it would break Cosmopolitan bundling
    (pure-python only). udocker falls back to shelling out to a `curl`
    executable when use_curl_executable is set to any non-empty value.
    """
    Config.conf["use_curl_executable"] = "curl"


def check_curl_available() -> None:
    if shutil.which("curl") is None:
        raise RuntimeError(
            "udockerd requires the 'curl' executable on PATH"
        )
