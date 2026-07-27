"""Container lifecycle endpoints: create, start, stop, rm, list, inspect, logs."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from udocker.container.structure import ContainerStructure

from udockerd import container_proc, udocker_ctx
from udockerd.http import STREAM_STDOUT, stream_frame
from udockerd.routes import exec as exec_routes

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
    """Same short-name resolution as routes/images.py, kept local: this
    call site only checks existence, not manifest details.
    """
    uctx = udocker_ctx.get()
    if uctx.local.cd_imagerepo(imagerepo, tag):
        return imagerepo, tag
    _, remoterepo = uctx.dockerioapi._parse_imagerepo(imagerepo)  # noqa: SLF001
    for candidate in (remoterepo, f"docker.io/{remoterepo}"):
        if candidate != imagerepo and uctx.local.cd_imagerepo(candidate, tag):
            return candidate, tag
    return None


def _summary(proc: ContainerProc) -> dict[str, Any]:
    return {
        "Id": proc.container_id,
        "Names": [f"/{proc.name}"],
        "Image": proc.image,
        "Command": "",
        "Created": int(proc.started_at or time.time()),
        "State": proc.status,
        "Status": proc.status,
        "Ports": [],
        "Labels": {},
        "NetworkSettings": {"Networks": _network_settings()["Networks"]},
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
            "Tty": proc.tty,
            "Cmd": proc.opt.get("cmd", []),
            "Env": proc.opt.get("env", []),
            "WorkingDir": proc.opt.get("cwd", ""),
            "User": proc.opt.get("user", ""),
            "AttachStdin": False,
            "AttachStdout": True,
            "AttachStderr": True,
            "OpenStdin": proc.tty,
        },
        "NetworkSettings": _network_settings(),
    }


def _network_settings() -> dict[str, Any]:
    """proot has no real network namespace (effectively always
    --net=host). Reports a single "host" entry under Networks, matching
    real Docker's map shape, with address fields honestly empty rather
    than fabricated.
    """
    return {
        "Ports": {},
        "Networks": {
            "host": {
                "IPAMConfig": None,
                "Links": None,
                "Aliases": None,
                "NetworkID": "",
                "EndpointID": "",
                "Gateway": "",
                "IPAddress": "",
                "IPPrefixLen": 0,
                "IPv6Gateway": "",
                "GlobalIPv6Address": "",
                "GlobalIPv6PrefixLen": 0,
                "MacAddress": "",
            },
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
        opt=container_proc.opt_from_request_body(body),
        tty=bool(body.get("Tty")),
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
    exec_routes.stop_execs_for(proc.container_id, grace_seconds=grace)
    ctx.send_empty(204)


def kill(ctx: RequestContext) -> None:
    """Unlike /stop, no grace period. docker run also calls this
    defensively on failed starts.
    """
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    query = _query(ctx)
    sig_name = query.get("signal", ["KILL"])[0]
    if not sig_name.startswith("SIG"):
        sig_name = f"SIG{sig_name}"
    sig = getattr(signal, sig_name, signal.SIGKILL)
    container_proc.send_signal(proc, sig)
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
    exec_routes.stop_execs_for(proc.container_id, grace_seconds=5)

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

    query = _query(ctx)
    follow = query.get("follow", ["0"])[0] in ("1", "true")

    # Connection: close required: no Content-Length, not a hijack, so
    # under HTTP/1.1 the client needs an explicit end-of-body signal.
    ctx.start_streaming(
        200, {"Content-Type": "application/vnd.docker.raw-stream", "Connection": "close"}
    )
    container_proc.tail_log(
        proc, ctx.wfile, follow=follow, frame=lambda chunk: stream_frame(STREAM_STDOUT, chunk)
    )


def attach(ctx: RequestContext) -> None:
    """TTY: joins the live pty session (raw passthrough, stdin forwarded).
    Non-TTY: live-tail-until-exit, same as /logs?follow.
    """
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    hijacked = ctx.is_upgrade_request()
    if hijacked:
        ctx.start_hijack()
    else:
        ctx.start_streaming(
            200, {"Content-Type": "application/vnd.docker.raw-stream", "Connection": "close"}
        )

    container_proc.stream_session(
        proc,
        ctx.wfile,
        ctx.rfile if hijacked else None,
        frame=lambda chunk: stream_frame(STREAM_STDOUT, chunk),
    )


def wait(ctx: RequestContext) -> None:
    """Blocks until the container exits. The real client's ContainerWait
    blocks on receiving *response headers* (before ContainerStart), reading
    the body separately in the background — so headers must be sent
    immediately here, before we block, or docker run hangs waiting for
    headers that only arrive after the thing it's waiting on already happened.
    """
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    ctx.start_streaming(200, {"Content-Type": "application/json", "Connection": "close"})

    with proc.lock:
        proc.lock.wait_for(lambda: proc.status == "exited")
        exit_code = proc.exit_code or 0
    ctx.wfile.write(json.dumps({"StatusCode": exit_code}).encode("utf-8"))


def register(router: Router) -> None:
    router.add("POST", r"^/containers/create$", create)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/start$", start)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/stop$", stop)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/kill$", kill)
    router.add("DELETE", r"^/containers/(?P<id>[^/]+)$", remove)
    router.add("GET", r"^/containers/json$", list_containers)
    router.add("GET", r"^/containers/(?P<id>[^/]+)/json$", inspect)
    router.add("GET", r"^/containers/(?P<id>[^/]+)/logs$", logs)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/wait$", wait)
    router.add("POST", r"^/containers/(?P<id>[^/]+)/attach$", attach)
