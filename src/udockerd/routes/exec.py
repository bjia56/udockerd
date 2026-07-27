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

import contextlib
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from udockerd import container_proc
from udockerd.http import STREAM_STDOUT, stream_frame

if TYPE_CHECKING:
    from udockerd.http import RequestContext, Router

_LOG_DIR = Path.home() / ".udockerd" / "logs" / "exec"

# Separate registry, keyed by exec_id rather than container_id — Docker
# API treats exec instances as their own object space distinct from
# containers, even though under the hood they reuse the same
# spawn/patch/supervisor machinery as container_proc.spawn().
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

    # docker exec (non-detached) sends Connection: Upgrade / Upgrade: tcp
    # and expects the 101 hijack response; anything else falls back to a
    # plain streamed response inside normal HTTP framing.
    if ctx.is_upgrade_request():
        ctx.start_hijack()
    else:
        ctx.start_streaming(200, {"Content-Type": "application/vnd.docker.raw-stream"})

    _tail_log_until_exit(exec_proc, ctx.wfile)


def _tail_log_until_exit(proc: container_proc.ContainerProc, out: Any) -> None:
    """Streams new bytes from the logfile as they're written, rather than
    blocking until exit and dumping the whole file at once — an
    interactive `docker exec` should see output as it happens. Each chunk
    is wrapped in Docker's multiplexed stream framing (see http.py); the
    logfile mixes stdout+stderr together (Popen redirects both to the
    same fd), so everything is framed as stdout.
    """
    with contextlib.suppress(FileNotFoundError), open(proc.logfile, "rb") as f:
        while True:
            chunk = f.read(65536)
            if chunk:
                out.write(stream_frame(STREAM_STDOUT, chunk))
                continue
            with proc.lock:
                if proc.status == "exited":
                    break
            time.sleep(0.05)
        # Drain any bytes written between the last read and exit.
        remainder = f.read()
        if remainder:
            out.write(stream_frame(STREAM_STDOUT, remainder))


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
    """Called when a container is stopped/removed so its exec instances
    (independent proot invocations against the same ROOT, per the module
    docstring) don't keep running as orphans once the container they were
    attached to is gone.
    """
    for exec_proc in _registry.all():
        if exec_proc.container_id != container_id:
            continue
        container_proc.stop(exec_proc, grace_seconds=grace_seconds)
        _registry.remove(exec_proc.name)


def register(router: Router) -> None:
    router.add("POST", r"^/containers/(?P<id>[^/]+)/exec$", create)
    router.add("POST", r"^/exec/(?P<id>[^/]+)/start$", start)
    router.add("GET", r"^/exec/(?P<id>[^/]+)/json$", inspect)
