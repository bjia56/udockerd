"""End-to-end coverage of the Docker Engine API surface udockerd
implements, driven by the real docker SDK/CLI against the test harness —
not a hand-rolled client. Each test cleans up the containers/images it
creates so the shared session-scoped harness stays usable across tests.
"""

import subprocess
import time

import docker
import pytest

IMAGE = "alpine:latest"


@pytest.fixture
def client(harness_port):
    c = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    yield c
    c.close()


@pytest.fixture(scope="session", autouse=True)
def pulled_image(harness_port):
    """Pulls once per session — every test after the first reuses it,
    since re-pulling alpine per test would dominate the suite's runtime.
    """
    client = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    client.images.pull(IMAGE)
    client.close()


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def test_image_pull_and_list(client):
    images = client.images.list()
    assert any(IMAGE in (img.tags or []) for img in images)


def test_image_inspect(client):
    image = client.images.get(IMAGE)
    assert image.attrs["Id"].startswith("sha256:")
    assert image.attrs["Architecture"]


def test_container_lifecycle(client):
    name = _unique_name("lifecycle")
    container = client.containers.create(IMAGE, command=["sleep", "30"], name=name)
    assert container.status == "created"

    container.start()
    container.reload()
    assert container.status == "running"

    listed = client.containers.list()
    assert any(c.name == name for c in listed)

    container.stop(timeout=5)
    container.reload()
    assert container.status == "exited"

    container.remove()
    assert not any(c.name == name for c in client.containers.list(all=True))


def test_container_logs(client):
    name = _unique_name("logs")
    container = client.containers.run(
        IMAGE, command=["sh", "-c", "echo hello; echo world"], name=name, detach=True
    )
    container.wait()
    logs = container.logs()
    assert b"hello" in logs
    assert b"world" in logs
    container.remove()


def test_container_logs_follow_terminates(client):
    name = _unique_name("logsfollow")
    container = client.containers.run(
        IMAGE, command=["sh", "-c", "echo one; sleep 1; echo two"], name=name, detach=True
    )
    start = time.monotonic()
    lines = list(container.logs(stream=True, follow=True))
    elapsed = time.monotonic() - start

    assert any(b"one" in line for line in lines)
    assert any(b"two" in line for line in lines)
    # Should terminate once the container exits, not hang — this guards
    # against the HTTP/1.1 keep-alive regression where /logs?follow never
    # returned without an explicit Connection: close.
    assert elapsed < 10
    container.remove()


def test_container_wait(client):
    name = _unique_name("wait")
    container = client.containers.run(IMAGE, command=["sh", "-c", "exit 3"], name=name, detach=True)
    result = container.wait()
    assert result["StatusCode"] == 3
    container.remove()


def test_container_exec(client):
    name = _unique_name("exec")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    exit_code, output = container.exec_run(["echo", "exec output"])
    assert exit_code == 0
    assert b"exec output" in output
    container.stop(timeout=5)
    container.remove()


def test_container_exec_survives_after_container_running(client):
    """A regression guard for the lock-scoping bug where exec into a
    running container hung indefinitely because the container's own
    engine.run() thread still held the shared subprocess.Popen patch
    lock.
    """
    name = _unique_name("execconcurrent")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    exit_code, output = container.exec_run(["echo", "concurrent exec"])
    assert exit_code == 0
    assert b"concurrent exec" in output

    container.reload()
    assert container.status == "running"
    container.stop(timeout=5)
    container.remove()


def test_container_kill(client):
    name = _unique_name("kill")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    container.kill()
    container.reload()
    assert container.status == "exited"
    container.remove()


def test_container_network_settings_shape(client):
    """Guards the NetworkSettings.Networks nested-map shape (not a flat
    IPAddress field) that real Docker uses and that `docker inspect -f`
    templates depend on.
    """
    name = _unique_name("netshape")
    container = client.containers.run(IMAGE, command=["sleep", "5"], name=name, detach=True)
    container.reload()
    networks = container.attrs["NetworkSettings"]["Networks"]
    assert "host" in networks
    assert networks["host"]["IPAddress"] == ""
    container.stop(timeout=5)
    container.remove()


def test_docker_run_detached_does_not_hang(harness_port):
    """Regression guard for the /wait header-timing bug: docker run -d
    calls ContainerWait (which blocks on response headers) before
    ContainerStart. If /wait sends headers only after the container
    exits, docker run hangs indefinitely before /start is ever called.
    """
    name = _unique_name("rundhang")
    start = time.monotonic()
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name, IMAGE, "sleep", "30"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    assert elapsed < 5

    subprocess.run(
        ["docker", "rm", "-f", name],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )


def test_docker_run_foreground_attaches(harness_port):
    """Regression guard for attach connecting before /start creates the
    logfile (tail_log used to give up immediately on FileNotFoundError).
    """
    result = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "echo", "attach output"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "attach output" in result.stdout
