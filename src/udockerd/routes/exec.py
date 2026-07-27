"""Exec endpoints: /containers/{id}/exec (create) + /exec/{id}/start.

udocker/proot has no real namespaces, so there's no way to join a running
container's process/mount namespace the way real Docker exec does. The
closest honest approximation: each exec runs as a second, independent
proot invocation against the same container's persisted ROOT directory —
it shares filesystem state with the main container process (if one is
running) but not process/PID visibility (`ps` inside an exec session
won't show the main container's processes). Documented in DESIGN.md.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from udockerd import container_proc
from udockerd.http import STREAM_STDOUT, stream_frame

if TYPE_CHECKING:
    from udockerd.http import RequestContext, Router

_LOG_DIR = Path.home() / ".udockerd" / "logs" / "exec"

# Keyed by exec_id, distinct object space from containers per Docker API,
# though it reuses container_proc.spawn()'s machinery underneath.
_registry = container_proc.ContainerRegistry()


def create(ctx: RequestContext) -> None:
    container_id = ctx.params["id"]
    proc = container_proc.registry.get(container_id)
    if proc is None:
        ctx.send_json(404, {"message": f"No such container: {container_id}"})
        return

    body = ctx.read_json() or {}
    exec_id = str(uuid.uuid4())
    exec_proc = container_proc.ContainerProc(
        container_id=proc.container_id,  # which container's ROOT to run against
        name=exec_id,
        image=proc.image,
        logfile=str(_LOG_DIR / f"{exec_id}.log"),
        opt=container_proc.opt_from_request_body(body),
        tty=bool(body.get("Tty")),
    )
    _registry.add(exec_proc, key=exec_id)

    ctx.send_json(201, {"Id": exec_id})


def start(ctx: RequestContext) -> None:
    exec_id = ctx.params["id"]
    exec_proc = _registry.get(exec_id)
    if exec_proc is None:
        ctx.send_json(404, {"message": f"No such exec instance: {exec_id}"})
        return
    if exec_proc.status == "running":
        ctx.send_json(409, {"message": "exec instance already started"})
        return

    body = ctx.read_json() or {}
    detach = body.get("Detach", False)

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    container_proc.spawn(exec_proc)

    if detach:
        ctx.send_empty(200)
        return

    hijacked = ctx.is_upgrade_request()
    if hijacked:
        ctx.start_hijack()
    else:
        ctx.start_streaming(
            200, {"Content-Type": "application/vnd.docker.raw-stream", "Connection": "close"}
        )

    container_proc.stream_session(
        exec_proc,
        ctx.wfile,
        ctx.rfile if hijacked else None,
        frame=lambda chunk: stream_frame(STREAM_STDOUT, chunk),
    )


def inspect(ctx: RequestContext) -> None:
    exec_id = ctx.params["id"]
    exec_proc = _registry.get(exec_id)
    if exec_proc is None:
        ctx.send_json(404, {"message": f"No such exec instance: {exec_id}"})
        return
    ctx.send_json(
        200,
        {
            "ID": exec_id,
            "Running": exec_proc.status == "running",
            "ExitCode": exec_proc.exit_code,
            "ContainerID": exec_proc.container_id,
        },
    )


def stop_execs_for(container_id: str, grace_seconds: float = 5.0) -> None:
    """Stops orphaned exec instances when their container is stopped/removed."""
    for exec_proc in _registry.all():
        if exec_proc.container_id != container_id:
            continue
        container_proc.stop(exec_proc, grace_seconds=grace_seconds)
        _registry.remove(exec_proc.name)


def register(router: Router) -> None:
    router.add("POST", r"^/containers/(?P<id>[^/]+)/exec$", create)
    router.add("POST", r"^/exec/(?P<id>[^/]+)/start$", start)
    router.add("GET", r"^/exec/(?P<id>[^/]+)/json$", inspect)
