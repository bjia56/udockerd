"""Shared test-harness fixture: builds the udockerd test-harness image
once per session, runs it as an unprivileged container (same shape as
the real Termux deployment). Tests drive it via DOCKER_HOST with the
real docker CLI/SDK.

Harness build/run uses the docker CLI directly, not the SDK's
images.build() — the SDK's classic-builder path handles USER/WORKDIR
permissions differently than BuildKit and produced a spurious
permission error building udockerd's own wheel.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_TAG = "udockerd-test-harness"
CONTAINER_NAME = "udockerd-test-harness-pytest"
HOST_PORT = 12376


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True)
    return result.returncode == 0


@pytest.fixture(scope="session")
def harness_port() -> Iterator[int]:
    """Yields the host port udockerd is listening on inside the harness
    container. Session-scoped; tests clean up their own containers/images.
    """
    if not docker_available():
        pytest.skip("docker daemon not available")

    subprocess.run(
        [
            "docker", "build",
            "-f", str(REPO_ROOT / "tests/docker/Dockerfile"),
            "-t", IMAGE_TAG,
            str(REPO_ROOT),
        ],
        check=True,
    )
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", CONTAINER_NAME,
            "-p", f"127.0.0.1:{HOST_PORT}:2375",
            IMAGE_TAG,
        ],
        check=True,
    )
    try:
        # udocker's own tool install (proot/fakechroot download) on first
        # boot can take a while.
        deadline = time.time() + 60
        while time.time() < deadline:
            result = subprocess.run(
                ["curl", "-sf", "-o", "/dev/null", f"http://127.0.0.1:{HOST_PORT}/_ping"],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", CONTAINER_NAME], capture_output=True, text=True
            )
            raise RuntimeError(
                f"udockerd did not become ready in time:\n{logs.stdout}\n{logs.stderr}"
            )
        yield HOST_PORT
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
