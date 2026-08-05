"""End-to-end coverage of the Docker Engine API surface udockerd
implements, driven by the real docker SDK/CLI against the test harness —
not a hand-rolled client. Each test cleans up the containers/images it
creates so the shared session-scoped harness stays usable across tests.
"""

import os
import pty
import subprocess
import time
from collections.abc import Iterator

import docker
import pytest

IMAGE = "alpine:latest"


@pytest.fixture
def client(harness_port: int) -> Iterator[docker.DockerClient]:
    c = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    yield c
    c.close()


@pytest.fixture(scope="session", autouse=True)
def pulled_image(harness_port: int) -> None:
    """Pulls once per session; re-pulling per test would dominate runtime."""
    client = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    client.images.pull(IMAGE)
    client.close()


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def test_image_pull_and_list(client: docker.DockerClient) -> None:
    images = client.images.list()
    assert any(IMAGE in (img.tags or []) for img in images)


def test_image_inspect(client: docker.DockerClient) -> None:
    image = client.images.get(IMAGE)
    assert image.attrs["Id"].startswith("sha256:")
    assert image.attrs["Architecture"]


def test_container_lifecycle(client: docker.DockerClient) -> None:
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


def test_container_logs(client: docker.DockerClient) -> None:
    name = _unique_name("logs")
    container = client.containers.run(
        IMAGE, command=["sh", "-c", "echo hello; echo world"], name=name, detach=True
    )
    container.wait()
    logs = container.logs()
    assert b"hello" in logs
    assert b"world" in logs
    container.remove()


def test_container_logs_follow_terminates(client: docker.DockerClient) -> None:
    name = _unique_name("logsfollow")
    container = client.containers.run(
        IMAGE, command=["sh", "-c", "echo one; sleep 1; echo two"], name=name, detach=True
    )
    start = time.monotonic()
    lines = list(container.logs(stream=True, follow=True))
    elapsed = time.monotonic() - start

    assert any(b"one" in line for line in lines)
    assert any(b"two" in line for line in lines)
    # Regression guard: /logs?follow used to hang without Connection: close.
    assert elapsed < 10
    container.remove()


def test_container_wait(client: docker.DockerClient) -> None:
    name = _unique_name("wait")
    container = client.containers.run(IMAGE, command=["sh", "-c", "exit 3"], name=name, detach=True)
    result = container.wait()
    assert result["StatusCode"] == 3
    container.remove()


def test_container_exec(client: docker.DockerClient) -> None:
    name = _unique_name("exec")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    exit_code, output = container.exec_run(["echo", "exec output"])
    assert exit_code == 0
    assert b"exec output" in output
    container.stop(timeout=5)
    container.remove()


def test_container_exec_survives_after_container_running(client: docker.DockerClient) -> None:
    """Regression guard: exec into a running container used to hang on
    the shared subprocess.Popen patch lock.
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


def test_container_kill(client: docker.DockerClient) -> None:
    name = _unique_name("kill")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    container.kill()
    container.reload()
    assert container.status == "exited"
    container.remove()


def test_container_start_bad_exec_reports_error(client: docker.DockerClient) -> None:
    """Regression guard: engine.run() raising before it ever exec's (e.g.
    an unexecutable Cmd like "--") used to only print to the daemon's own
    console; /start unconditionally sent 204. The client must see a
    synchronous error instead of a fake success.
    """
    name = _unique_name("badexec")
    container = client.containers.create(IMAGE, command=["--", "bash"], name=name)
    with pytest.raises(docker.errors.APIError):
        container.start()

    container.reload()
    assert container.status == "exited"
    assert container.attrs["State"]["ExitCode"] != 0
    assert container.attrs["State"]["Error"]
    container.remove()


def test_container_exec_bad_cmd_reports_error(client: docker.DockerClient) -> None:
    """Same underlying spawn() fix, exercised via exec: a bad exec target
    used to leave the exec instance stuck with ExitCode None forever.
    """
    name = _unique_name("execbadcmd")
    container = client.containers.run(IMAGE, command=["sleep", "30"], name=name, detach=True)
    exit_code, _output = container.exec_run(["--"])
    assert exit_code != 0
    container.stop(timeout=5)
    container.remove()


def test_container_network_settings_shape(client: docker.DockerClient) -> None:
    """Guards the NetworkSettings.Networks nested-map shape (not a flat
    IPAddress field).
    """
    name = _unique_name("netshape")
    container = client.containers.run(IMAGE, command=["sleep", "5"], name=name, detach=True)
    container.reload()
    networks = container.attrs["NetworkSettings"]["Networks"]
    assert "host" in networks
    assert networks["host"]["IPAddress"] == ""
    container.stop(timeout=5)
    container.remove()


def test_docker_run_detached_does_not_hang(harness_port: int) -> None:
    """Regression guard: docker run -d calls ContainerWait (blocks on
    response headers) before ContainerStart; /wait must send headers
    before the container exits.
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


def test_docker_run_foreground_attaches(harness_port: int) -> None:
    """Regression guard: attach can connect before /start creates the logfile."""
    result = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "echo", "attach output"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "attach output" in result.stdout


def test_container_tty_logs_capture_output(client: docker.DockerClient) -> None:
    """Regression guard: spawn_tty_reader's first os.read(master_fd) reliably
    hit EIO (pty master read races the supervisor's forked child setsid()ing
    and opening the slave) and treated any OSError as fatal, killing the
    reader thread before it ever delivered a byte -- TTY container output
    was silently lost for the container's entire life.
    """
    name = _unique_name("ttylogs")
    container = client.containers.run(
        IMAGE, command=["sh", "-c", "echo tty-output"], name=name, tty=True, detach=True
    )
    container.wait()
    logs = container.logs()
    assert b"tty-output" in logs
    container.remove()


def test_container_resize(client: docker.DockerClient) -> None:
    name = _unique_name("resize")
    container = client.containers.run(
        IMAGE, command=["sleep", "5"], name=name, tty=True, detach=True
    )
    container.resize(height=30, width=100)  # raises APIError on non-2xx
    container.stop(timeout=5)
    container.remove()


def test_docker_run_tty_does_not_hang(harness_port: int) -> None:
    """Exact user-facing repro: `docker run -it` hung until the CLI was
    killed. Root cause was two bugs: (1) above, output never arriving, and
    (2) stream_session's stdin-forwarding thread blocks on rfile.read(1);
    once the container exited, BaseHTTPRequestHandler.finish()'s
    rfile.close() needed the same internal lock that blocked read held,
    deadlocking the connection closed and hanging the client forever.
    `-t` needs a real pty since the docker CLI checks its local stdin.
    """
    name = _unique_name("ttyhang")
    env = {"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"}
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["docker", "run", "-it", "--name", name, IMAGE, "sh", "-c", "echo tty-hang-guard; sleep 1"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
    )
    os.close(slave_fd)
    start = time.monotonic()
    try:
        returncode = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        os.close(master_fd)
        subprocess.run(["docker", "rm", "-f", name], env=env, capture_output=True)
        pytest.fail("docker run -it hung past 15s (TTY session deadlock regression)")
    elapsed = time.monotonic() - start

    output = b""
    try:
        while True:
            chunk = os.read(master_fd, 65536)
            if not chunk:
                break
            output += chunk
    except OSError:
        pass
    finally:
        os.close(master_fd)

    assert returncode == 0
    assert elapsed < 10
    assert b"tty-hang-guard" in output

    subprocess.run(["docker", "rm", "-f", name], env=env, capture_output=True)


def test_docker_run_bad_exec_reports_error_and_does_not_hang(harness_port: int) -> None:
    """Exact user-facing repro: `docker run image -- bash` makes "--" the
    exec target, which fails to exec. Real dockerd returns this error to
    the CLI synchronously; it must not hang and must not exit 0.
    """
    name = _unique_name("clibadexec")
    start = time.monotonic()
    result = subprocess.run(
        ["docker", "run", "--name", name, IMAGE, "--", "bash"],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - start
    assert result.returncode != 0
    assert elapsed < 5
    assert result.stderr.strip()

    subprocess.run(
        ["docker", "rm", "-f", name],
        env={"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )


def test_system_prune_all_removes_stopped_container_and_unused_image(
    harness_port: int,
) -> None:
    """The real end-to-end use case: `docker system prune -a -f` as the
    docker CLI actually drives it (containers -> networks -> volumes
    -> images -> build cache, five separate API calls). Uses a
    throwaway image distinct from the session-shared `alpine` fixture
    so this doesn't disturb other tests. Placed last in the file since
    -a legitimately removes any image not referenced by a container.

    Checks removal via `docker inspect` on the specific container/image
    id rather than `docker ps --filter`/`docker images <ref>`: neither
    /containers/json nor /images/json here honor the `filters` query
    param, so those would silently return the harness's full (and
    test-order-dependent) container/image set instead of a filtered one.
    """
    env = {"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"}
    prune_image = "busybox:latest"
    name = _unique_name("pruneme")

    subprocess.run(["docker", "pull", prune_image], env=env, check=True, capture_output=True)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name, prune_image, "true"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = run.stdout.strip()
    subprocess.run(
        ["docker", "wait", container_id], env=env, check=True, capture_output=True, timeout=15
    )

    result = subprocess.run(
        ["docker", "system", "prune", "-a", "-f", "--volumes"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    inspect_container = subprocess.run(
        ["docker", "inspect", container_id], env=env, capture_output=True, text=True
    )
    assert inspect_container.returncode != 0

    inspect_image = subprocess.run(
        ["docker", "inspect", prune_image], env=env, capture_output=True, text=True
    )
    assert inspect_image.returncode != 0


def test_system_prune_all_does_not_remove_running_container_or_its_image(
    harness_port: int,
) -> None:
    """/containers/prune must only remove non-running containers
    (real docker's `container prune` semantic — `docker ps -a` still
    shows a running container as unprunable). Also confirms the
    `-a` image-prune path treats a running container's image as
    "in use": it must survive even though nothing else references it.
    """
    env = {"DOCKER_HOST": f"tcp://127.0.0.1:{harness_port}", "PATH": "/usr/bin:/bin"}
    prune_image = "busybox:latest"
    name = _unique_name("keepme")

    subprocess.run(["docker", "pull", prune_image], env=env, check=True, capture_output=True)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name, prune_image, "sleep", "30"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = run.stdout.strip()

    try:
        result = subprocess.run(
            ["docker", "system", "prune", "-a", "-f", "--volumes"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
            env=env,
            capture_output=True,
            text=True,
        )
        assert running.returncode == 0, running.stderr
        assert running.stdout.strip() == "true"

        inspect_image = subprocess.run(
            ["docker", "inspect", prune_image], env=env, capture_output=True, text=True
        )
        assert inspect_image.returncode == 0, inspect_image.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], env=env, capture_output=True)
