"""Smoke test: udockerd boots inside the test harness and responds to
the real `docker` CLI over DOCKER_HOST.
"""

import subprocess


def test_docker_cli_version(harness_port: int) -> None:
    result = subprocess.run(
        ["docker", "version"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "udockerd" in result.stdout


def test_ping(harness_port: int) -> None:
    result = subprocess.run(
        [
            "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
            f"http://127.0.0.1:{harness_port}/_ping",
        ],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "200"
