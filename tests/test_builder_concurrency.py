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

import pytest
from udocker.container.structure import ContainerStructure

from udockerd import udocker_ctx
from udockerd.builder import StageConfig, _materialize_image_root, commit_layer


@pytest.fixture
def isolated_uctx(monkeypatch, tmp_path):
    """Fresh UdockerContext against a throwaway UDOCKER_DIR, with a
    minimal fake base image already registered so create_fromimage has
    something to extract.
    """
    monkeypatch.setenv("UDOCKER_DIR", str(tmp_path))
    monkeypatch.setenv("UDOCKER_USE_CURL_EXECUTABLE", "curl")

    udocker_ctx._context = None  # noqa: SLF001 - reset the module singleton between tests
    uctx = udocker_ctx.init()

    imagerepo, tag = "test/fakeimg", "latest"
    uctx.local.setup_imagerepo(imagerepo)
    uctx.local.setup_tag(tag)
    uctx.local.set_version("v2")

    layer_path = tmp_path / "fakelayer.tar"
    with tarfile.open(layer_path, "w") as tf:
        data = b"hello"
        info = tarfile.TarInfo("hello.txt")
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

    yield uctx, imagerepo, tag

    udocker_ctx._context = None  # noqa: SLF001


def test_concurrent_commit_layer_does_not_corrupt_cursor(isolated_uctx):
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
        result = original_setup_tag(t)
        time.sleep(0.2)
        return result

    uctx.local.setup_tag = slow_setup_tag  # type: ignore[method-assign]

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


def test_concurrent_materialize_image_root_does_not_corrupt_cursor(isolated_uctx):
    """Same hazard, for _materialize_image_root() (COPY --from=<image>)."""
    uctx, imagerepo, tag = isolated_uctx

    original_cd_imagerepo = uctx.local.cd_imagerepo

    def slow_cd_imagerepo(repo: str, t: str):  # noqa: ANN001
        result = original_cd_imagerepo(repo, t)
        time.sleep(0.2)
        return result

    uctx.local.cd_imagerepo = slow_cd_imagerepo  # type: ignore[method-assign]

    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            root, container_id = _materialize_image_root(f"{imagerepo}:{tag}")
            # The extracted ROOT must actually contain this image's file;
            # a corrupted cursor could point create_fromimage at a
            # half-configured or wrong tag directory instead.
            assert (root / "hello.txt").exists(), f"{name}: missing expected file in ROOT"
            uctx.local.del_container(container_id, force=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(target=worker, args=("A",))
    thread_b = threading.Thread(target=worker, args=("B",))
    thread_a.start()
    time.sleep(0.05)
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert not errors, f"materialize_image_root raced and failed: {errors}"
