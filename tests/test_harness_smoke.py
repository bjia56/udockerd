"""Smoke test: build the test-harness image, run udockerd in it, and hit
it with the real `docker` CLI over DOCKER_HOST. Requires a working local
`docker` daemon; skipped otherwise (e.g. most CI runners without DinD).
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_TAG = "udockerd-test-harness"
CONTAINER_NAME = "udockerd-test-harness-pytest"
HOST_PORT = 12376


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True)
    return result.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker daemon not available")


@pytest.fixture(scope="module")
def harness():
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
        for _ in range(30):
            result = subprocess.run(
                ["curl", "-sf", "-o", "/dev/null", f"http://127.0.0.1:{HOST_PORT}/_ping"],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("udockerd did not become ready in time")
        yield HOST_PORT
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


def test_docker_cli_version(harness):
    result = subprocess.run(
        ["docker", "version"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "udockerd" in result.stdout


def test_ping(harness):
    result = subprocess.run(
        ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:{harness}/_ping"],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "200"
