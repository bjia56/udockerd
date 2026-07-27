"""Image endpoints: /images/create (pull), /images/json (list),
/images/{name}/json (inspect), /images/{name} (rm).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from udockerd import udocker_ctx

if TYPE_CHECKING:
    from udockerd.http import RequestContext, Router


def _manifest_info(imagerepo: str, tag: str) -> dict[str, Any]:
    """Read the real registry manifest udocker already stored on pull
    (local/latest/manifest, saved verbatim by DockerIoAPI) to get the
    actual config digest and layer digests — the same values the real
    Docker daemon would report — instead of fabricating an id.

    v1-schema manifests (older Docker Hub images, no longer the common
    case) don't carry a config digest at all; callers get an empty dict
    back and should fall back to a clearly-synthetic id.
    """
    uctx = udocker_ctx.get()
    if not uctx.local.cd_imagerepo(imagerepo, tag):
        return {}
    manifest = uctx.local.load_json("manifest")
    if not manifest or "config" not in manifest:
        return {}
    return {
        "id": manifest["config"]["digest"],
        "layer_digests": [layer["digest"] for layer in manifest.get("layers", [])],
    }


def _synthetic_id(imagerepo: str, tag: str) -> str:
    """Fallback id for manifests with no real config digest (v1 schema).
    Distinct from the repo:tag string so `docker images`, which reads Id
    for the IMAGE ID column, doesn't display the tag there instead.
    """
    digest = hashlib.sha256(f"{imagerepo}:{tag}".encode()).hexdigest()
    return f"sha256:{digest}"


def _split_imagespec(imagespec: str) -> tuple[str, str]:
    if "@" in imagespec:
        imagerepo, tag = imagespec.split("@", 1)
    elif ":" in imagespec:
        imagerepo, tag = imagespec.split(":", 1)
    else:
        imagerepo, tag = imagespec, "latest"
    return imagerepo, tag


def _resolve_imagerepo(imagerepo: str, tag: str) -> tuple[str, str] | None:
    """Images pulled via DockerIoAPI.get() end up stored under a qualified
    path (e.g. docker.io/library/alpine), but clients commonly ask for the
    short name (alpine) — same as the real Docker daemon accepts both.
    Reuses DockerIoAPI's own name-qualification logic rather than
    reimplementing Docker Hub's namespacing rules.
    """
    uctx = udocker_ctx.get()
    if uctx.local.cd_imagerepo(imagerepo, tag):
        return imagerepo, tag

    _, remoterepo = uctx.dockerioapi._parse_imagerepo(imagerepo)  # noqa: SLF001
    for candidate in (remoterepo, f"docker.io/{remoterepo}"):
        if candidate != imagerepo and uctx.local.cd_imagerepo(candidate, tag):
            return candidate, tag
    return None


def _created_timestamp(image_json: dict[str, Any] | None) -> int:
    """Real Docker config JSON has a "created" RFC3339 string; convert to
    the unix timestamp /images/json reports. Falls back to 0 (epoch) when
    unavailable rather than fabricating a value.
    """
    created = (image_json or {}).get("created")
    if not created:
        return 0
    try:
        return int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _image_summary(imagerepo: str, tag: str) -> dict[str, Any]:
    uctx = udocker_ctx.get()
    layers = uctx.local.get_layers(imagerepo, tag)
    size = sum(layer_size for _, layer_size in layers if layer_size)
    manifest_info = _manifest_info(imagerepo, tag)
    image_id = manifest_info["id"] if manifest_info else _synthetic_id(imagerepo, tag)
    image_json, _ = uctx.local.get_image_attributes()
    return {
        "Id": image_id,
        "ParentId": "",
        "RepoTags": [f"{imagerepo}:{tag}"],
        "RepoDigests": [f"{imagerepo}@{manifest_info['id']}"] if manifest_info else [],
        "Created": _created_timestamp(image_json),
        "Size": size,
        "VirtualSize": size,
        "SharedSize": 0,
        "Labels": {},
        "Containers": -1,
    }


def create(ctx: RequestContext) -> None:
    """POST /images/create?fromImage=<repo>&tag=<tag> — pull an image.

    Docker API streams progress as newline-delimited JSON; we emit a
    minimal status/error stream rather than faked per-layer progress,
    since udocker's DockerIoAPI.get() doesn't expose per-layer callbacks.
    """
    query = parse_qs(urlsplit(ctx.path).query)
    from_image = query.get("fromImage", [""])[0]
    tag = query.get("tag", ["latest"])[0]
    if not from_image:
        ctx.send_json(400, {"message": "fromImage is required"})
        return

    uctx = udocker_ctx.get()
    ctx.start_streaming(200, {"Content-Type": "application/json"})
    with uctx.lock:
        files = uctx.dockerioapi.get(from_image, tag)
    status = "Download complete" if files else "Error pulling image"
    line = json.dumps({"status": f"{status}: {from_image}:{tag}"}) + "\n"
    ctx.wfile.write(line.encode("utf-8"))
    if not files:
        error_line = json.dumps({"error": f"no files downloaded for {from_image}:{tag}"}) + "\n"
        ctx.wfile.write(error_line.encode("utf-8"))


def list_images(ctx: RequestContext) -> None:
    uctx = udocker_ctx.get()
    with uctx.lock:
        images_list = uctx.local.get_imagerepos()
        summaries = [_image_summary(imagerepo, tag) for imagerepo, tag in images_list]
    ctx.send_json(200, summaries)


def inspect(ctx: RequestContext) -> None:
    name = ctx.params["name"]
    imagerepo, tag = _split_imagespec(name)
    uctx = udocker_ctx.get()
    with uctx.lock:
        resolved = _resolve_imagerepo(imagerepo, tag)
        if resolved is None:
            ctx.send_json(404, {"message": f"No such image: {name}"})
            return
        imagerepo, tag = resolved
        image_json, _layers = uctx.local.get_image_attributes()
        layers = uctx.local.get_layers(imagerepo, tag)
        manifest_info = _manifest_info(imagerepo, tag)

    if not image_json:
        ctx.send_json(404, {"message": f"No such image: {name}"})
        return

    size = sum(layer_size for _, layer_size in layers if layer_size)
    image_id = manifest_info["id"] if manifest_info else _synthetic_id(imagerepo, tag)
    ctx.send_json(
        200,
        {
            "Id": image_id,
            "RepoTags": [f"{imagerepo}:{tag}"],
            "RepoDigests": [f"{imagerepo}@{manifest_info['id']}"] if manifest_info else [],
            "Parent": "",
            "Comment": "",
            "Created": image_json.get("created", ""),
            "Architecture": image_json.get("architecture", "unknown"),
            "Os": image_json.get("os", "unknown"),
            "Size": size,
            "VirtualSize": size,
            "Config": image_json.get("config", {}),
            "RootFS": {
                "Type": "layers",
                "Layers": manifest_info.get("layer_digests", []) if manifest_info else [],
            },
        },
    )


def remove(ctx: RequestContext) -> None:
    name = ctx.params["name"]
    imagerepo, tag = _split_imagespec(name)
    uctx = udocker_ctx.get()
    with uctx.lock:
        resolved = _resolve_imagerepo(imagerepo, tag)
        if resolved is None:
            ctx.send_json(404, {"message": f"No such image: {name}"})
            return
        imagerepo, tag = resolved
        if uctx.local.isprotected_imagerepo(imagerepo, tag):
            ctx.send_json(409, {"message": f"image is protected: {name}"})
            return
        ok = uctx.local.del_imagerepo(imagerepo, tag, force=False)
    if not ok:
        ctx.send_json(404, {"message": f"No such image: {name}"})
        return
    ctx.send_json(200, [{"Untagged": f"{imagerepo}:{tag}"}])


def register(router: Router) -> None:
    router.add("POST", r"^/images/create$", create)
    router.add("GET", r"^/images/json$", list_images)
    router.add("GET", r"^/images/(?P<name>[^/]+(?:/[^/]+)*)/json$", inspect)
    router.add("DELETE", r"^/images/(?P<name>[^/]+(?:/[^/]+)*)$", remove)
