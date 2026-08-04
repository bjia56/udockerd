"""End-to-end coverage of POST /build (classic builder), driven by the
real docker SDK against the test harness.
"""

import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.errors
import pytest

IMAGE = "alpine:latest"
UNPULLED_IMAGE = "busybox:latest"


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


def test_build_cmd_empty_array_stays_exec_form(
    client: docker.DockerClient, tmp_path: Path
) -> None:
    """CMD [] / ENTRYPOINT [] is valid exec-form syntax for "no args", not
    a shell-form string "[]" wrapped in /bin/sh -c.
    """
    tag = _unique_tag("build-cmd-empty")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        ENTRYPOINT ["true"]
        CMD []
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    assert image.attrs["Config"]["Cmd"] in (None, [])
    assert image.attrs["Config"]["Entrypoint"] == ["true"]


def test_build_distinct_content_gets_distinct_image_id(
    client: docker.DockerClient, tmp_path: Path
) -> None:
    """Two builds with identical Config (same Cmd, no ENV/LABEL diff) but
    different RUN-produced file content must still get different image
    IDs. The config JSON's digest (the image ID) has no other way to
    depend on layer content -- without a rootfs.diff_ids field tying it
    to the actual layer, two such builds landing in the same wall-clock
    "created" second would hash identically and collide, corrupting
    both images' tag->ID mapping (confirmed against a live harness: the
    second build's own client-side Image object came back holding the
    first build's tag).
    """
    dockerfile = f"""
    FROM {IMAGE}
    RUN echo {{content}} > /marker.txt
    CMD ["cat", "/marker.txt"]
    """
    tag_a = _unique_tag("build-distinct-a")
    context_a = _write_dockerfile(tmp_path, dockerfile.format(content="content-a"))
    image_a, _ = client.images.build(path=str(context_a), tag=tag_a)

    tmp_path_b = tmp_path / "b"
    tmp_path_b.mkdir()
    tag_b = _unique_tag("build-distinct-b")
    context_b = _write_dockerfile(tmp_path_b, dockerfile.format(content="content-b"))
    image_b, _ = client.images.build(path=str(context_b), tag=tag_b)

    assert image_a.id != image_b.id
    assert any(tag_a in (t or "") for t in image_a.tags)
    assert any(tag_b in (t or "") for t in image_b.tags)


def test_build_run_multiline_continuation(client: docker.DockerClient, tmp_path: Path) -> None:
    """Backslash-continued RUN lines must join onto one shell command, not
    one with an embedded newline -- a bare "&& foo" on its own line is a
    shell syntax error, unlike the joined "... && foo" real Docker runs.
    """
    tag = _unique_tag("build-continuation")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        RUN echo start \\
            && echo middle \\
            && echo end > /marker.txt
        CMD ["cat", "/marker.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    assert any(tag in (t or "") for t in image.tags)

    output = client.containers.run(tag, remove=True)
    assert output.strip() == b"end"


def test_build_copy_single_file_into_workdir(client: docker.DockerClient, tmp_path: Path) -> None:
    """COPY <file> . (dest "." with no trailing slash, len(sources) == 1)
    must still copy *into* WORKDIR, not clobber WORKDIR itself with a file
    named "app" -- real Docker treats an already-existing directory dest
    as a directory target regardless of trailing slash. A subsequent
    COPY <dir>/ <dir>/ into the same WORKDIR is included since it's what
    exposed the bug: once WORKDIR got overwritten by a file, the next
    COPY's mkdir underneath it failed with ENOTDIR.
    """
    tag = _unique_tag("build-copy-workdir")
    (tmp_path / "app.txt").write_text("hello from app.txt")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "inner.txt").write_text("hello from subdir")
    _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        WORKDIR /app
        COPY app.txt .
        COPY subdir/ subdir/
        CMD ["sh", "-c", "cat /app/app.txt && cat /app/subdir/inner.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(tmp_path), tag=tag)
    output = client.containers.run(tag, remove=True)
    assert output == b"hello from app.txthello from subdir"


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


def test_build_inherits_base_image_config(client: docker.DockerClient, tmp_path: Path) -> None:
    """Regression test: each stage's config used to start empty instead
    of inheriting the base image's own Config (Env/Cmd/Entrypoint/etc),
    so a build that didn't repeat e.g. PATH or CMD lost it entirely -
    `docker run` on the result failed with "command not found" even
    though the base image itself runs fine.
    """
    tag = _unique_tag("build-inherit-config")
    base_image = client.images.get(IMAGE)
    base_env = base_image.attrs["Config"]["Env"] or []
    base_cmd = base_image.attrs["Config"]["Cmd"]
    assert base_cmd, "test assumes the base image has a default Cmd"

    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        RUN echo hi
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    assert image.attrs["Config"]["Env"] == base_env
    assert image.attrs["Config"]["Cmd"] == base_cmd

    # No CMD/ENTRYPOINT override in the Dockerfile: this only succeeds
    # if the base image's Cmd carried through to the built image.
    client.containers.run(tag, remove=True)


def test_build_accepts_chunked_transfer_encoding(
    harness_port: int, client: docker.DockerClient, tmp_path: Path
) -> None:
    """Regression test: the real `docker` CLI (unlike docker-py's SDK)
    streams the build context with `Transfer-Encoding: chunked` and no
    Content-Length, since it doesn't know the tar's size upfront. RequestContext.read_body()
    used to only honor Content-Length, so the daemon read an empty body
    and the build failed with a bogus "empty file" tar error.
    """
    tag = _unique_tag("build-chunked")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {IMAGE}
        RUN echo chunked-ok > /marker.txt
        CMD ["cat", "/marker.txt"]
        """,
    )
    tar_path = tmp_path / "context.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(context / "Dockerfile", arcname="Dockerfile")

    result = subprocess.run(
        [
            "curl", "-sf",
            "-H", "Transfer-Encoding: chunked",
            "--data-binary", f"@{tar_path}",
            f"http://127.0.0.1:{harness_port}/build?t={tag}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '"error"' not in result.stdout, result.stdout

    image = client.images.get(tag)
    assert any(tag in (t or "") for t in image.tags)


def test_build_pulls_missing_base_image(client: docker.DockerClient, tmp_path: Path) -> None:
    """Regression test: `FROM <image>` in a Dockerfile only resolved
    against images already present locally and errored with "no such
    image" otherwise, unlike the real daemon which pulls a missing base
    image automatically.
    """
    with pytest.raises(docker.errors.ImageNotFound):
        client.images.get(UNPULLED_IMAGE)

    tag = _unique_tag("build-autopull")
    context = _write_dockerfile(
        tmp_path,
        f"""
        FROM {UNPULLED_IMAGE}
        RUN echo autopulled > /marker.txt
        CMD ["cat", "/marker.txt"]
        """,
    )
    image, _logs = client.images.build(path=str(context), tag=tag)
    output = client.containers.run(tag, remove=True)
    assert output.strip() == b"autopulled"

    # The pull as a side effect of the build should also have populated
    # the local image cache for the base image itself.
    assert client.images.get(UNPULLED_IMAGE) is not None
