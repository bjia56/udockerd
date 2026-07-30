"""Deterministic (non-timing-dependent) regression test for the
LocalRepository cursor race fixed in commit_layer(): setup_tag/
save_json/add_image_layer read/write LocalRepository's unsynchronized
cur_repodir/cur_tagdir cursor, so commit_layer() must hold uctx.lock for
its full per-tag span or a concurrent commit can repoint the cursor
mid-sequence and misdirect one build's manifest into another's tag dir.

No Docker daemon needed — exercises udocker_ctx/builder directly
against an isolated UDOCKER_DIR, with an injected delay in setup_tag to
force the race window open deterministically (relying on raw thread
timing without this, as an end-to-end `docker build` race would, is too
flaky to assert on: commit_layer's per-tag work is microseconds against
a build's overall multi-hundred-millisecond wall clock).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from udocker.container.structure import ContainerStructure

from udockerd import udocker_ctx
from udockerd.builder import StageConfig, _materialize_image_root, commit_layer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from udockerd.udocker_ctx import UdockerContext


def _register_fake_image(
    uctx: UdockerContext, tmp_path: Path, imagerepo: str, tag: str, filename: str
) -> None:
    """Registers a minimal fake image (one layer containing a single,
    distinctively-named file) in the given repo:tag so create_fromimage
    has something to extract.
    """
    uctx.local.setup_imagerepo(imagerepo)
    uctx.local.setup_tag(tag)
    uctx.local.set_version("v2")

    layer_path = tmp_path / f"{filename}.tar"
    with tarfile.open(layer_path, "w") as tf:
        data = filename.encode()
        info = tarfile.TarInfo(filename)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    layer_digest = "sha256:" + hashlib.sha256(layer_path.read_bytes()).hexdigest()
    config = {
        "architecture": "amd64",
        "os": "linux",
        "created": "2024-01-01T00:00:00Z",
        "config": {},
    }
    config_bytes = json.dumps(config).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    uctx.local.save_json(config_digest, config)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "x",
        "config": {"mediaType": "x", "size": len(config_bytes), "digest": config_digest},
        "layers": [{"mediaType": "x", "size": layer_path.stat().st_size, "digest": layer_digest}],
    }
    uctx.local.save_json("manifest", manifest)
    layer_dest = Path(uctx.local.layersdir) / layer_digest
    shutil.copy2(layer_path, layer_dest)
    uctx.local.add_image_layer(str(layer_dest))


@pytest.fixture
def isolated_uctx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[UdockerContext, str, str]]:
    """Fresh UdockerContext against a throwaway UDOCKER_DIR, with a
    minimal fake base image already registered so create_fromimage has
    something to extract.
    """
    monkeypatch.setenv("UDOCKER_DIR", str(tmp_path))
    monkeypatch.setenv("UDOCKER_USE_CURL_EXECUTABLE", "curl")

    udocker_ctx._context = None  # noqa: SLF001 - reset the module singleton between tests
    uctx = udocker_ctx.init()

    imagerepo, tag = "test/fakeimg", "latest"
    _register_fake_image(uctx, tmp_path, imagerepo, tag, "hello.txt")

    yield uctx, imagerepo, tag

    udocker_ctx._context = None  # noqa: SLF001


@pytest.fixture
def isolated_uctx_two_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[UdockerContext, tuple[str, str], tuple[str, str]]]:
    """Like isolated_uctx, but with two distinct fake images registered
    so cursor cross-contamination between them is observable (unlike a
    single shared image, where both threads materializing the same
    repo:tag would look identical regardless of any cursor mixup).
    """
    monkeypatch.setenv("UDOCKER_DIR", str(tmp_path))
    monkeypatch.setenv("UDOCKER_USE_CURL_EXECUTABLE", "curl")

    udocker_ctx._context = None  # noqa: SLF001 - reset the module singleton between tests
    uctx = udocker_ctx.init()

    imagerepo_a, tag_a = "test/fakeimg-a", "latest"
    imagerepo_b, tag_b = "test/fakeimg-b", "latest"
    _register_fake_image(uctx, tmp_path, imagerepo_a, tag_a, "hello-a.txt")
    _register_fake_image(uctx, tmp_path, imagerepo_b, tag_b, "hello-b.txt")

    yield uctx, (imagerepo_a, tag_a), (imagerepo_b, tag_b)

    udocker_ctx._context = None  # noqa: SLF001


def test_concurrent_commit_layer_does_not_corrupt_cursor(
    isolated_uctx: tuple[UdockerContext, str, str],
) -> None:
    uctx, imagerepo, tag = isolated_uctx

    uctx.local.cd_imagerepo(imagerepo, tag)
    container_a = ContainerStructure(uctx.local).create_fromimage(imagerepo, tag)
    uctx.local.cd_imagerepo(imagerepo, tag)
    container_b = ContainerStructure(uctx.local).create_fromimage(imagerepo, tag)

    # Widen the race window: without commit_layer holding uctx.lock for
    # its full per-tag span, thread B's setup_tag() would repoint
    # cur_tagdir while thread A is still mid-sequence (between its own
    # setup_tag and save_json calls), and A's save_json would land in
    # B's tag directory instead of its own.
    original_setup_tag = uctx.local.setup_tag

    def slow_setup_tag(t: str) -> bool:
        result: bool = original_setup_tag(t)
        time.sleep(0.2)
        return result

    uctx.local.setup_tag = slow_setup_tag

    def worker(name: str, container_id: str, image_name: str) -> None:
        config = StageConfig()
        config.Env[name] = "value"
        commit_layer(container_id, config, [f"{image_name}:latest"])

    thread_a = threading.Thread(target=worker, args=("A", container_a, "concurrent-a"))
    thread_b = threading.Thread(target=worker, args=("B", container_b, "concurrent-b"))
    thread_a.start()
    time.sleep(0.05)
    thread_b.start()
    thread_a.join()
    thread_b.join()

    uctx.local.cd_imagerepo("concurrent-a", "latest")
    attrs_a, _ = uctx.local.get_image_attributes()
    uctx.local.cd_imagerepo("concurrent-b", "latest")
    attrs_b, _ = uctx.local.get_image_attributes()

    assert attrs_a is not None, "image A's manifest/config went missing — cursor corrupted"
    assert attrs_b is not None, "image B's manifest/config went missing — cursor corrupted"
    assert attrs_a["config"]["Env"] == ["A=value"]
    assert attrs_b["config"]["Env"] == ["B=value"]


def test_concurrent_materialize_image_root_does_not_corrupt_cursor(
    isolated_uctx_two_images: tuple[UdockerContext, tuple[str, str], tuple[str, str]],
) -> None:
    """Same hazard, for _materialize_image_root() (COPY --from=<image>).

    Uses two *distinct* source images: create_fromimage's own sequence is
    cd_imagerepo() (repoints the cursor) followed by get_image_attributes()
    (reads it), so without the lock spanning both, thread B's cd_imagerepo
    can repoint the cursor while thread A is between those two calls,
    making A extract B's layers into A's container. Both threads pulling
    the *same* image/tag can't observe this — the wrong-cursor result is
    indistinguishable from the right one. Distinct images make a mixup
    detectable: each ROOT must contain only its own image's file.
    """
    uctx, (imagerepo_a, tag_a), (imagerepo_b, tag_b) = isolated_uctx_two_images

    original_cd_imagerepo = uctx.local.cd_imagerepo

    def slow_cd_imagerepo(repo: str, t: str) -> str:
        result: str = original_cd_imagerepo(repo, t)
        time.sleep(0.2)
        return result

    uctx.local.cd_imagerepo = slow_cd_imagerepo

    errors: list[BaseException] = []

    def worker(name: str, imagerepo: str, tag: str, expected_file: str, other_file: str) -> None:
        try:
            root, container_id = _materialize_image_root(f"{imagerepo}:{tag}")
            assert (root / expected_file).exists(), f"{name}: missing expected file in ROOT"
            assert not (root / other_file).exists(), (
                f"{name}: found {other_file} — cursor corrupted, wrong image materialized"
            )
            uctx.local.del_container(container_id, force=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(
        target=worker, args=("A", imagerepo_a, tag_a, "hello-a.txt", "hello-b.txt")
    )
    thread_b = threading.Thread(
        target=worker, args=("B", imagerepo_b, tag_b, "hello-b.txt", "hello-a.txt")
    )
    thread_a.start()
    time.sleep(0.05)
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert not errors, f"materialize_image_root raced and failed: {errors}"
