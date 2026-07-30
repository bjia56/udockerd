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
    """Reads the real stored registry manifest for actual config/layer
    digests instead of fabricating an id. v1-schema manifests carry no
    config digest; callers fall back to _synthetic_id.
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
    """Fallback id for manifests with no real config digest (v1 schema)."""
    digest = hashlib.sha256(f"{imagerepo}:{tag}".encode()).hexdigest()
    return f"sha256:{digest}"


def _is_hex_id(name: str) -> bool:
    """True for a bare digest id (full sha256:... or a short hex prefix,
    e.g. what docker-py's images.build() parses out of "Successfully
    built <12hex>" and then inspects by).
    """
    digest = name.split(":", 1)[1] if name.startswith("sha256:") else name
    return bool(digest) and all(c in "0123456789abcdef" for c in digest)


def _resolve_by_digest_prefix(
    uctx: udocker_ctx.UdockerContext, digest: str
) -> tuple[str, str] | None:
    for imagerepo, tag in uctx.local.get_imagerepos():
        info = _manifest_info(imagerepo, tag)
        candidate_id = info["id"] if info else _synthetic_id(imagerepo, tag)
        if candidate_id.split(":", 1)[1].startswith(digest):
            return imagerepo, tag
    return None


def _resolve_by_name_or_id(name: str) -> tuple[str, str] | None:
    """Resolves a repo:tag/short name, a full sha256:... digest, or a
    short hex id prefix (docker-py's images.build() inspects the built
    image by the short id parsed from the build's success message).
    """
    uctx = udocker_ctx.get()
    if _is_hex_id(name):
        digest = name.split(":", 1)[1] if name.startswith("sha256:") else name
        return _resolve_by_digest_prefix(uctx, digest)
    imagerepo, tag = udocker_ctx.split_imagespec(name)
    return udocker_ctx.resolve_imagerepo(uctx, imagerepo, tag)


def _created_timestamp(image_json: dict[str, Any] | None) -> int:
    """Converts config JSON's RFC3339 "created" string to unix timestamp;
    falls back to 0 rather than fabricating a value.
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
    Emits a minimal status/error stream (no per-layer progress; udocker's
    DockerIoAPI.get() exposes no per-layer callbacks).
    """
    query = parse_qs(urlsplit(ctx.path).query)
    from_image = query.get("fromImage", [""])[0]
    tag = query.get("tag", ["latest"])[0]
    if not from_image:
        ctx.send_json(400, {"message": "fromImage is required"})
        return

    uctx = udocker_ctx.get()
    ctx.start_streaming(200, {"Content-Type": "application/json", "Connection": "close"})
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
    uctx = udocker_ctx.get()
    with uctx.lock:
        resolved = _resolve_by_name_or_id(name)
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
    uctx = udocker_ctx.get()
    with uctx.lock:
        resolved = _resolve_by_name_or_id(name)
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
