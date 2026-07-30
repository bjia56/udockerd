"""End-to-end coverage of POST /build (classic builder), driven by the
real docker SDK against the test harness.
"""

import time
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.errors
import pytest

IMAGE = "alpine:latest"


@pytest.fixture
def client(harness_port: int) -> Iterator[docker.DockerClient]:
    c = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    yield c
    c.close()


@pytest.fixture(scope="session", autouse=True)
def pulled_image(harness_port: int) -> None:
    """Build stages FROM this image; pytest fixtures don't share across
    test files, so this mirrors test_docker_api.py's own pull fixture.
    """
    client = docker.DockerClient(base_url=f"tcp://127.0.0.1:{harness_port}")
    client.images.pull(IMAGE)
    client.close()


def _unique_tag(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}:test"


def _write_dockerfile(tmp_path: Path, content: str) -> Path:
    (tmp_path / "Dockerfile").write_text(content)
    return tmp_path


def test_build_single_stage(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-single")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        RUN echo building > /marker.txt
        CMD ["cat", "/marker.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    assert any(tag in (t or "") for t in image.tags)

    output = client.containers.run(tag, remove=True)
    assert output.strip() == b"building"


def test_build_copy_from_context(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-copy")
    (tmp_path / "app.txt").write_text("hello from context")
    _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        COPY app.txt /app.txt
        CMD ["cat", "/app.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(tmp_path), tag=tag)
    output = client.containers.run(tag, remove=True)
    assert output == b"hello from context"


def test_build_multi_stage_copy_from(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-multistage")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE} AS builder
        RUN echo from-builder > /out.txt

        FROM {IMAGE}
        COPY --from=builder /out.txt /out.txt
        CMD ["cat", "/out.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    output = client.containers.run(tag, remove=True)
    assert output.strip() == b"from-builder"


def test_build_buildargs(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-args")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        ARG GREETING=default
        RUN echo $GREETING > /greeting.txt
        CMD ["cat", "/greeting.txt"]
        """,
    )
    image, _logs = client.images.build(
        path=str(context), tag=tag, buildargs={"GREETING": "hi-from-arg"}
    )
    output = client.containers.run(tag, remove=True)
    assert output.strip() == b"hi-from-arg"


def test_build_target_stops_at_stage(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-target")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE} AS builder
        RUN echo builder-stage > /marker.txt

        FROM {IMAGE}
        RUN echo final-stage > /marker.txt
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag, target="builder")
    output = client.containers.run(tag, ["cat", "/marker.txt"], remove=True)
    assert output.strip() == b"builder-stage"


def test_build_run_failure_reports_error(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-fail")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        RUN exit 3
        """,
    )
    with pytest.raises(docker.errors.BuildError):
        client.images.build(path=str(context), tag=tag)

    with pytest.raises(docker.errors.ImageNotFound):
        client.images.get(tag)


def test_build_unsupported_instruction_fails(client: docker.DockerClient, tmp_path: Path) -> None:
    tag = _unique_tag("build-unsupported")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        HEALTHCHECK CMD true
        """,
    )
    with pytest.raises(docker.errors.BuildError):
        client.images.build(path=str(context), tag=tag)
