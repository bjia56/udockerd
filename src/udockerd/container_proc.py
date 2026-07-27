"""Spawns and tracks container processes via udocker's execution engines.

udocker's engine.run() (PRootEngine, FakechrootEngine, ...) builds its
command list from self.opt and calls subprocess.call(cmd_l, ...) directly,
synchronously, with no exposed Popen object and no factored-out "build
command" step. There's no non-invasive way to get the child pid/pgid or
otherwise hook the actual exec through the public API.

Rather than reimplement command construction (fragile, duplicates a lot of
udocker's real per-mode logic: uid mapping, volume bindings, qemu, network
maps, kernel emulation), we monkeypatch subprocess.Popen for the scope of
the engine.run() call only, to capture the resulting Popen object (for
pid/pgid) and prepend our process supervisor (see supervisor.py/.c) to the
command being run. This is coupled to udocker calling subprocess.call/
Popen in engine.run(); udockerd already pins udocker to an exact version
for the same class of internal-API coupling (see udocker_ctx.py,
routes/images.py).

Cosmopolitan Python has no ctypes (_ctypes isn't compiled in), so
PR_SET_PDEATHSIG can't be set in-process via a preexec_fn. The supervisor
is a tiny compiled C program that forks, sets PDEATHSIG, and stays alive
to reap/clean up its subtree — see supervisor.c for why a plain exec
wrapper isn't enough (proot forks its own traced child, which doesn't
inherit a PDEATHSIG watch).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from udocker.engine.execmode import ExecutionMode

from udockerd import supervisor, udocker_ctx

if TYPE_CHECKING:
    from udocker.engine.base import ExecutionEngineCommon

_patch_lock = threading.Lock()
_original_popen = subprocess.Popen

# The real container-launch call happens directly inside these engines'
# run() methods (PRootEngine.run, FakechrootEngine.run). udocker's own
# internal helpers (e.g. HostInfo.cmd_has_option probing proot's
# --kill-on-exit support) also call subprocess.Popen/check_output during
# run(), before the real launch — checking the immediate caller frame
# tells them apart directly, rather than relying on an incidental kwarg
# (e.g. close_fds) that happens to differ today but isn't a documented
# distinction udocker guarantees to keep.
_ENGINE_RUN_FILENAMES = ("engine/proot.py", "engine/fakechroot.py")


def _called_from_engine_run() -> bool:
    # Depth from here: 0=this function, 1=_patched_popen (our caller),
    # 2=subprocess.call (Popen's direct caller, "with Popen(...)"),
    # 3=engine.run (the frame we actually want to identify).
    frame = sys._getframe(3)
    filename = frame.f_code.co_filename.replace("\\", "/")
    return frame.f_code.co_name == "run" and filename.endswith(_ENGINE_RUN_FILENAMES)

# engine.run() reads these opt keys directly but they're only ever
# populated by udocker's own cmdp/CLI-argument parsing (_get_run_options in
# cli.py), which we bypass entirely to drive the engine programmatically.
# Values match what an unset CLI flag would default to. If a future
# (pinned) udocker version's run() reads additional opt keys not covered
# by ExecutionEngineCommon's class-level defaults, they'll need adding here.
_EXTRA_OPT_DEFAULTS: dict[str, Any] = {
    "kernel": "",
    "netcoop": False,
}


def _apply_default_opt(engine: ExecutionEngineCommon) -> None:
    for key, default in _EXTRA_OPT_DEFAULTS.items():
        engine.opt.setdefault(key, default)


def _prepend_supervisor(args: Any, supervisor_path: str) -> Any:
    cmd = args[0] if args else None
    if isinstance(cmd, (list, tuple)):
        return ([supervisor_path, *list(cmd)], *args[1:])
    return args


def _make_patched_popen(proc: ContainerProc, supervisor_path: str) -> Any:
    def _patched_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        # engine.run() isn't the only thing calling subprocess.Popen/
        # check_output while patched is active — udocker's own internal
        # helpers (e.g. HostInfo.cmd_has_option, probing for proot's
        # --kill-on-exit support) also shell out during run(), before the
        # real container launch. Only intercept the call that's actually
        # coming from inside the engine's run() method.
        if not _called_from_engine_run():
            return _original_popen(*args, **kwargs)  # noqa: S603

        # Self-unpatch once we've matched the real launch call: restoring
        # immediately narrows the window subprocess.Popen is globally
        # patched to just this one call, so _patch_lock only needs to
        # guard up to here, not the container's entire (blocking,
        # possibly long-running) run.
        subprocess.Popen = _original_popen  # type: ignore[misc]
        args = _prepend_supervisor(args, supervisor_path)
        # No stdout/stderr redirection here would otherwise inherit the
        # daemon's own stdout/stderr. Route container output to its log
        # file instead.
        log_fh = open(proc.logfile, "ab")  # noqa: SIM115 - closed via Popen's fd ownership
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = log_fh
        popen_proc = _original_popen(*args, **kwargs)  # noqa: S603
        log_fh.close()  # Popen dup'd the fd; safe to close our copy
        with proc.lock:
            proc.pid = popen_proc.pid
            # Not os.getpgid(popen_proc.pid): the supervisor calls setsid()
            # (pgid := its own pid) right after start but before it forks
            # the real command, and reading the pgid via a syscall
            # immediately after Popen() returns races that — it can
            # observe the pre-setsid value. setsid() guarantees
            # pgid == pid once it runs, and that pid is exactly
            # popen_proc.pid, so this is correct without waiting on the
            # race.
            proc.pgid = popen_proc.pid
            proc.status = "running"
            proc.started_at = time.time()
        return popen_proc

    return _patched_popen


@dataclass
class ContainerProc:
    container_id: str
    name: str
    image: str
    pid: int | None = None
    pgid: int | None = None
    status: str = "created"  # created, running, exited
    exit_code: int | None = None
    logfile: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ContainerRegistry:
    def __init__(self) -> None:
        self._containers: dict[str, ContainerProc] = {}
        self._lock = threading.Lock()

    def add(self, proc: ContainerProc) -> None:
        with self._lock:
            self._containers[proc.container_id] = proc

    def get(self, container_id: str) -> ContainerProc | None:
        with self._lock:
            return self._containers.get(container_id)

    def remove(self, container_id: str) -> None:
        with self._lock:
            self._containers.pop(container_id, None)

    def all(self) -> list[ContainerProc]:
        with self._lock:
            return list(self._containers.values())


registry = ContainerRegistry()


def _run_engine_patched(
    engine: ExecutionEngineCommon, container_id: str, proc: ContainerProc, supervisor_path: str
) -> int:
    with _patch_lock:
        subprocess.Popen = _make_patched_popen(proc, supervisor_path)  # type: ignore[misc]
        try:
            return int(engine.run(container_id))
        finally:
            subprocess.Popen = _original_popen  # type: ignore[misc]


def spawn(
    container_id: str,
    name: str,
    image: str,
    logfile: str,
    opt: dict[str, Any],
) -> ContainerProc:
    """Starts engine.run() in a background thread so the API call that
    triggered it (POST /containers/{id}/start) can return immediately,
    the same way the real Docker daemon does.

    `opt` overrides engine.opt entries (cmd, env, vol, user, cwd, ...) —
    the same fields udocker's own cmdp/CLI-argument parsing would set,
    which we bypass to drive the engine programmatically from Docker API
    request JSON instead.
    """
    uctx = udocker_ctx.get()
    supervisor_path = str(supervisor.ensure_supervisor())

    proc = ContainerProc(container_id=container_id, name=name, image=image, logfile=logfile)
    registry.add(proc)

    def target() -> None:
        exec_mode = ExecutionMode(uctx.local, container_id)
        engine = exec_mode.get_engine()
        _apply_default_opt(engine)
        engine.opt.update(opt)
        exit_code = _run_engine_patched(engine, container_id, proc, supervisor_path)
        with proc.lock:
            proc.status = "exited"
            proc.exit_code = exit_code
            proc.finished_at = time.time()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    # Poll the registry entry briefly for the pid/pgid the patched Popen
    # call fills in; falls through (still "created") if the engine never
    # reaches a Popen call within the timeout, e.g. a setup error.
    deadline = time.time() + 10
    while time.time() < deadline:
        with proc.lock:
            if proc.pid is not None or proc.status == "exited":
                break
        time.sleep(0.05)

    return proc


def stop(proc: ContainerProc, grace_seconds: float = 10.0) -> None:
    """Graceful path: SIGTERM the container's process group, wait, then
    SIGKILL stragglers. Complements the shim's PDEATHSIG crash-path
    cleanup, which fires automatically without any code here running.
    """
    with proc.lock:
        pgid = proc.pgid
        status = proc.status
    if status != "running" or pgid is None:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        with proc.lock:
            if proc.status == "exited":
                return
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def stop_all(grace_seconds: float = 10.0) -> None:
    """Called from the daemon's SIGTERM handler to sweep all running
    containers before the daemon process itself exits.
    """
    for proc in registry.all():
        stop(proc, grace_seconds)
