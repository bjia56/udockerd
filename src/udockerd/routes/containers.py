"""Container lifecycle endpoints: create, start, stop, rm, list, inspect, logs."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from udocker.container.structure import ContainerStructure

from udockerd import container_proc, udocker_ctx

if TYPE_CHECKING:
    from udockerd.container_proc import ContainerProc
    from udockerd.http import RequestContext, Router

_LOG_DIR = Path.home() / ".udockerd" / "logs"


def _query(ctx: RequestContext) -> dict[str, list[str]]:
    return parse_qs(urlsplit(ctx.path).query)


def _split_imagespec(imagespec: str) -> tuple[str, str]:
    if "@" in imagespec:
        imagerepo, tag = imagespec.split("@", 1)
    elif ":" in imagespec:
        imagerepo, tag = imagespec.split(":", 1)
    else:
        imagerepo, tag = imagespec, "latest"
    return imagerepo, tag


def _resolve_imagerepo(imagerepo: str, tag: str) -> tuple[str, str] | None:
    """Same short-name resolution as routes/images.py — kept local since
    the two call sites use it slightly differently (this one only needs
    to check existence, not read manifest details) and importing across
    route modules for a five-line helper isn't worth the coupling.
    """
    uctx = udocker_ctx.get()
    if uctx.local.cd_imagerepo(imagerepo, tag):
        return imagerepo, tag
    _, remoterepo = uctx.dockerioapi._parse_imagerepo(imagerepo)  # noqa: SLF001
    for candidate in (remoterepo, f"docker.io/{remoterepo}"):
        if candidate != imagerepo and uctx.local.cd_imagerepo(candidate, tag):
            return candidate, tag
    return None


def _container_opt_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Maps Docker API container-create request JSON to udocker engine
    opt fields. Only covers the fields udocker's engines actually read
    (see container_proc.py); silently ignores Docker API fields with no
    proot/fakechroot equivalent (e.g. resource limits) rather than
    erroring, since there's no meaningful way to honor them here.
    """
    opt: dict[str, Any] = {}
    cmd = body.get("Cmd")
    if cmd:
        opt["cmd"] = list(cmd)
    entrypoint = body.get("Entrypoint")
    if entrypoint:
        opt["entryp"] = entrypoint if isinstance(entrypoint, str) else " ".join(entrypoint)
    env = body.get("Env")
    if env:
        opt["env"] = list(env)
    workdir = body.get("WorkingDir")
    if workdir:
        opt["cwd"] = workdir
    user = body.get("User")
    if user:
        opt["user"] = user
    return opt


def _summary(proc: ContainerProc) -> dict[str, Any]:
    return {
        "Id": proc.container_id,
        "Names": [f"/{proc.name}"],
        "Image": proc.image,
        "Command": "",
        "Created": int(proc.started_at or time.time()),
        "State": proc.status,
        "Status": proc.status,
        # No real network namespace under proot — see routes/images.py's
        # sibling honesty note; ports/networking are stubbed empty here
        # too rather than fabricated.
        "Ports": [],
        "Labels": {},
    }


def _inspect_json(proc: ContainerProc) -> dict[str, Any]:
    return {
        "Id": proc.container_id,
        "Name": f"/{proc.name}",
        "Image": proc.image,
        "State": {
            "Status": proc.status,
            "Running": proc.status == "running",
            "Pid": proc.pid or 0,
            "ExitCode": proc.exit_code or 0,
            "StartedAt": "",
            "FinishedAt": "",
        },
        "Config": {
            "Image": proc.image,
        },
        # Honest network stub: proot shares the host network stack, no
        # per-container isolation to report.
        "NetworkSettings": {
            "IPAddress": "",
            "Ports": {},
        },
    }


def create(ctx: RequestContext) -> None:
    query = _query(ctx)
    name = query.get("name", [""])[0]
    body = ctx.read_json() or {}
    image = body.get("Image", "")
    if not image:
        ctx.send_json(400, {"message": "Image is required"})
        return

    imagerepo, tag = _split_imagespec(image)
    uctx = udocker_ctx.get()
    with uctx.lock:
        resolved = _resolve_imagerepo(imagerepo, tag)
        if resolved is None:
            ctx.send_json(404, {"message": f"No such image: {image}"})
            return
        imagerepo, tag = resolved
        container_id = ContainerStructure(uctx.local).create_fromimage(imagerepo, tag)
        if name:
            uctx.local.set_container_name(container_id, name)

    if not container_id:
        ctx.send_json(500, {"message": "container creation failed"})
        return

    display_name = name or container_id
    proc = container_proc.ContainerProc(
        container_id=container_id,
        name=display_name,
        image=image,
        logfile=str(_LOG_DIR / f"{container_id}.log"),
        opt=_container_opt_from_body(body),
    )
    container_proc.registry.add(proc)

    ctx.send_json(201, {"Id": container_id, "Warnings": []})


def start(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return
    if proc.status == "running":
        ctx.send_empty(304)
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    container_proc.spawn(proc)
    ctx.send_empty(204)


def stop(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return
    query = _query(ctx)
    grace = float(query.get("t", ["10"])[0])
    container_proc.stop(proc, grace_seconds=grace)
    ctx.send_empty(204)


def remove(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    query = _query(ctx)
    force = query.get("force", ["0"])[0] in ("1", "true")

    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return
    if proc.status == "running" and not force:
        ctx.send_json(
            409, {"message": "container is running: stop it before removing or use force"}
        )
        return
    if proc.status == "running":
        container_proc.stop(proc, grace_seconds=5)

    uctx = udocker_ctx.get()
    with uctx.lock:
        uctx.local.del_container(proc.container_id, force=force)
    container_proc.registry.remove(proc.container_id)
    with contextlib.suppress(OSError):
        os.remove(proc.logfile)
    ctx.send_empty(204)


def list_containers(ctx: RequestContext) -> None:
    query = _query(ctx)
    show_all = query.get("all", ["0"])[0] in ("1", "true")
    summaries = []
    for proc in container_proc.registry.all():
        if not show_all and proc.status != "running":
            continue
        summaries.append(_summary(proc))
    ctx.send_json(200, summaries)


def inspect(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return
    ctx.send_json(200, _inspect_json(proc))


def logs(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    ctx.start_streaming(200, {"Content-Type": "application/vnd.docker.raw-stream"})
    with contextlib.suppress(FileNotFoundError), open(proc.logfile, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            ctx.wfile.write(chunk)


def wait(ctx: RequestContext) -> None:
    """Blocks until the container exits, as `docker run` (not just
    `docker start`) relies on to know when to stop attaching/return.
    """
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    with proc.lock:
        proc.lock.wait_for(lambda: proc.status == "exited")
        exit_code = proc.exit_code or 0
    ctx.send_json(200, {"StatusCode": exit_code})


def register(router: Router) -> None:
    router.add("POST", r"^/containers/create$", create)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/start$", start)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/stop$", stop)
    router.add("DELETE", r"^/containers/(?P<id>[^/]+)$", remove)
    router.add("GET", r"^/containers/json$", list_containers)
    router.add("GET", r"^/containers/(?P<id>[^/]+)/json$", inspect)
    router.add("GET", r"^/containers/(?P<id>[^/]+)/logs$", logs)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/wait$", wait)
