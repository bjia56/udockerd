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
import pty
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
    # ExecutionEngineCommon.opt is a *class-level* mutable dict (`opt = {}`
    # on the class body), shared by every engine instance unless replaced.
    # Without this, engine.opt.update(...) below would mutate that shared
    # dict, leaking cmd/env/etc from one container's run into every
    # subsequent one for the lifetime of the daemon process — confirmed
    # by reproducing exactly that: stale echoed args from earlier test
    # containers appearing in a later, unrelated container's output.
    engine.opt = dict(engine.opt)
    for key, default in _EXTRA_OPT_DEFAULTS.items():
        engine.opt.setdefault(key, default)


def _prepend_supervisor(args: Any, supervisor_path: str, tty_slave_path: str | None) -> Any:
    cmd = args[0] if args else None
    if not isinstance(cmd, (list, tuple)):
        return args
    if tty_slave_path is not None:
        prefix = [supervisor_path, "tty", tty_slave_path]
    else:
        prefix = [supervisor_path, "notty"]
    return ([*prefix, *list(cmd)], *args[1:])


def _make_patched_popen(proc: ContainerProc, supervisor_path: str, unpatch: Any) -> Any:
    def _patched_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        # engine.run() isn't the only thing calling subprocess.Popen/
        # check_output while patched is active — udocker's own internal
        # helpers (e.g. HostInfo.cmd_has_option, probing for proot's
        # --kill-on-exit support) also shell out during run(), before the
        # real container launch. Only intercept the call that's actually
        # coming from inside the engine's run() method.
        if not _called_from_engine_run():
            return _original_popen(*args, **kwargs)  # noqa: S603

        # Self-unpatch AND release _patch_lock once we've matched the
        # real launch call: engine.run() then blocks for the container's
        # entire (possibly long) runtime inside subprocess.call().wait().
        # _patch_lock must not still be held at that point, or every
        # other container/exec start serializes behind whichever one
        # happens to be running longest — confirmed exactly that: an
        # exec into a running container hung indefinitely because the
        # container's own engine.run() thread still held this lock deep
        # inside os.waitpid(), long after its own Popen call was done.
        unpatch()

        if proc.tty:
            master_fd, slave_fd = pty.openpty()
            proc.pty_master_fd = master_fd
            tty_slave_path = os.ttyname(slave_fd)
            os.close(slave_fd)  # supervisor's forked child reopens it after its own setsid
            args = _prepend_supervisor(args, supervisor_path, tty_slave_path)
            # No stdin/stdout/stderr kwargs: the pty slave becomes the
            # container process's controlling terminal via supervisor.c's
            # own open()+dup2() sequence, not through Popen's redirection.
            popen_proc = _original_popen(*args, **kwargs)  # noqa: S603
            spawn_tty_reader(proc, master_fd)
        else:
            args = _prepend_supervisor(args, supervisor_path, None)
            # No stdout/stderr redirection here would otherwise inherit
            # the daemon's own stdout/stderr. Route container output to
            # its log file instead.
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
            proc.lock.notify_all()
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
    # engine.opt overrides captured at /containers/create time (cmd, env,
    # user, cwd, ...) and applied when /start actually spawns the process.
    opt: dict[str, Any] = field(default_factory=dict)
    # A Condition (not a plain Lock) so /wait can block until status
    # changes via notify_all() instead of polling.
    lock: threading.Condition = field(default_factory=threading.Condition)

    # TTY session state (see spawn_tty_reader below). None for non-TTY
    # containers/execs, which keep using the plain logfile-tailing path.
    tty: bool = False
    pty_master_fd: int | None = None
    pty_lock: threading.Lock = field(default_factory=threading.Lock)
    # Live attach/exec clients currently subscribed to this session's pty
    # output, as (write_callable, on_error_callable) pairs. Populated by
    # attach/exec-start handlers, drained by the reader thread on write
    # failure (client disconnected).
    subscribers: list[Any] = field(default_factory=list)


class ContainerRegistry:
    def __init__(self) -> None:
        self._containers: dict[str, ContainerProc] = {}
        self._lock = threading.Lock()

    def add(self, proc: ContainerProc, key: str | None = None) -> None:
        """key defaults to proc.container_id. Exec instances reuse
        ContainerProc/spawn() but need a different key (exec_id) than the
        container_id field, which for them holds the *target* container
        rather than their own identity.
        """
        with self._lock:
            self._containers[key or proc.container_id] = proc

    def get(self, id_or_name: str) -> ContainerProc | None:
        """Docker API endpoints accept either the container id or its
        name interchangeably (e.g. `docker logs mytest`); resolve both.
        """
        with self._lock:
            proc = self._containers.get(id_or_name)
            if proc is not None:
                return proc
            for candidate in self._containers.values():
                if candidate.name == id_or_name:
                    return candidate
            return None

    def remove(self, container_id: str) -> None:
        with self._lock:
            self._containers.pop(container_id, None)

    def all(self) -> list[ContainerProc]:
        with self._lock:
            return list(self._containers.values())


registry = ContainerRegistry()


def opt_from_request_body(body: dict[str, Any]) -> dict[str, Any]:
    """Maps Docker API create/exec request JSON to udocker engine opt
    fields. Only covers the fields udocker's engines actually read (see
    _EXTRA_OPT_DEFAULTS above and ExecutionEngineCommon.opt); silently
    ignores Docker API fields with no proot/fakechroot equivalent (e.g.
    resource limits) rather than erroring, since there's no meaningful
    way to honor them here. Shared by routes/containers.py (create) and
    routes/exec.py (exec create) — both request shapes use the same
    field names for Cmd/Env/WorkingDir/User.
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


def _run_engine_patched(
    engine: ExecutionEngineCommon, container_id: str, proc: ContainerProc, supervisor_path: str
) -> int:
    _patch_lock.acquire()
    released = False

    def unpatch() -> None:
        nonlocal released
        subprocess.Popen = _original_popen  # type: ignore[misc]
        if not released:
            released = True
            _patch_lock.release()

    subprocess.Popen = _make_patched_popen(proc, supervisor_path, unpatch)  # type: ignore[misc]
    try:
        return int(engine.run(container_id))
    finally:
        # Safety net: if engine.run() never reached the real launch call
        # (e.g. errored out during setup before Popen), unpatch() above
        # never ran and the lock is still held — release it here so a
        # failed start doesn't wedge every other container/exec forever.
        unpatch()


def spawn(proc: ContainerProc) -> None:
    """Starts engine.run() in a background thread so the API call that
    triggered it (POST /containers/{id}/start) can return immediately,
    the same way the real Docker daemon does.

    Mutates the given (already-registered, created by /containers/create)
    ContainerProc in place rather than constructing a new one, so any
    existing reference to it (e.g. a concurrent /wait call already
    blocked on its condition variable) keeps observing the same object.

    proc.opt overrides engine.opt entries (cmd, env, vol, user, cwd, ...)
    — the same fields udocker's own cmdp/CLI-argument parsing would set,
    which we bypass to drive the engine programmatically from Docker API
    request JSON instead.
    """
    uctx = udocker_ctx.get()
    supervisor_path = str(supervisor.ensure_supervisor())
    container_id = proc.container_id
    opt = proc.opt

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
            proc.lock.notify_all()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    # Wait briefly for the pid/pgid the patched Popen call fills in (or
    # for an immediate failure); falls through (still "created") if the
    # engine never reaches a Popen call within the timeout, e.g. a setup
    # error. notify_all() from _make_patched_popen/target wakes this
    # immediately rather than polling.
    with proc.lock:
        proc.lock.wait_for(lambda: proc.pid is not None or proc.status == "exited", timeout=10)


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

    with proc.lock:
        exited = proc.lock.wait_for(lambda: proc.status == "exited", timeout=grace_seconds)
    if exited:
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def send_signal(proc: ContainerProc, sig: int) -> None:
    """Direct signal delivery for /containers/{id}/kill, which — unlike
    /stop — sends exactly the requested signal (SIGKILL by default) with
    no grace period or SIGTERM-first courtesy.
    """
    with proc.lock:
        pgid = proc.pgid
        status = proc.status
    if status != "running" or pgid is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def stop_all(grace_seconds: float = 10.0) -> None:
    """Called from the daemon's SIGTERM handler to sweep all running
    containers before the daemon process itself exits.
    """
    for proc in registry.all():
        stop(proc, grace_seconds)


def spawn_tty_reader(proc: ContainerProc, master_fd: int) -> None:
    """One reader thread per TTY session, started right after the pty is
    allocated. Continuously reads the pty master and fans each chunk out
    to the logfile (always, so `docker logs` works even after every live
    client has disconnected) and to any subscribed live attach/exec
    clients (see subscribe_tty/unsubscribe_tty). A dedicated thread
    rather than each attach/exec handler reading the master directly,
    since pty reads are destructive — two readers on the same master
    would race for bytes instead of both seeing everything.
    """

    def reader() -> None:
        with open(proc.logfile, "ab") as logf:
            while True:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                logf.write(chunk)
                logf.flush()
                with proc.pty_lock:
                    dead = []
                    for write, on_error in proc.subscribers:
                        try:
                            write(chunk)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            dead.append((write, on_error))
                    for entry in dead:
                        proc.subscribers.remove(entry)
                        entry[1]()

    threading.Thread(target=reader, daemon=True).start()


def subscribe_tty(proc: ContainerProc, write: Any, on_error: Any) -> None:
    """Registers a live attach/exec client to receive pty output as it
    arrives. `on_error` is called once, without arguments, when the
    subscriber is dropped (write failure = client disconnected) — used to
    let the caller know its connection should be considered closed.
    """
    with proc.pty_lock:
        proc.subscribers.append((write, on_error))


def unsubscribe_tty(proc: ContainerProc, write: Any, on_error: Any) -> None:
    with proc.pty_lock, contextlib.suppress(ValueError):
        proc.subscribers.remove((write, on_error))


def write_tty_stdin(proc: ContainerProc, data: bytes) -> None:
    if proc.pty_master_fd is not None:
        with contextlib.suppress(OSError):
            os.write(proc.pty_master_fd, data)


def stream_session(proc: ContainerProc, out: Any, in_: Any, *, frame: Any) -> None:
    """Entry point shared by exec start and attach for streaming a live
    session to a hijacked (or plain-streamed) connection. Dispatches to
    the TTY path (subscribe to the pty reader thread, forward stdin, raw
    passthrough — no frame() wrapping, since a real terminal expects raw
    bytes) or the non-TTY path (tail_log, output-only, multiplex-framed).

    `in_` is the request's rfile for stdin forwarding; pass None to skip
    stdin handling entirely (e.g. the plain-HTTP-framing fallback path,
    which real docker clients don't use for interactive input anyway).
    """
    if not proc.tty:
        tail_log(proc, out, follow=True, frame=frame)
        return

    done = threading.Event()

    def write(chunk: bytes) -> None:
        out.write(chunk)

    def on_disconnect() -> None:
        done.set()

    subscribe_tty(proc, write, on_disconnect)

    def forward_stdin() -> None:
        if in_ is None:
            return
        while not done.is_set():
            try:
                chunk = in_.read(1)
            except OSError:
                break
            if not chunk:
                break
            write_tty_stdin(proc, chunk)

    stdin_thread = threading.Thread(target=forward_stdin, daemon=True)
    stdin_thread.start()

    try:
        with proc.lock:
            proc.lock.wait_for(lambda: proc.status == "exited" or done.is_set())
    finally:
        unsubscribe_tty(proc, write, on_disconnect)
        done.set()


def tail_log(proc: ContainerProc, out: Any, *, follow: bool, frame: Any) -> None:
    """Streams logfile bytes as they're written rather than dumping the
    whole file at once — shared by exec start (always follows until the
    exec exits), /containers/{id}/attach, and /containers/{id}/logs?follow.

    follow=False: read what's there now and stop (plain `docker logs`).
    follow=True: keep tailing new bytes until the container exits or the
    write side breaks (client disconnected) — `docker logs -f` / attach.

    `frame` wraps each chunk before writing (Docker's multiplexed stream
    framing for hijacked connections — see http.py's stream_frame) or is
    None to write raw bytes (plain, non-hijacked /logs responses).
    """
    if follow:
        # docker run's attach happens before start (same reasoning as
        # ContainerWait — see routes/containers.py's wait() docstring),
        # so the logfile may not exist yet. Wait for spawn() to create it
        # rather than giving up immediately, bounded so a container that
        # never starts doesn't hang this forever.
        deadline = time.time() + 10
        while not os.path.exists(proc.logfile) and time.time() < deadline:
            with proc.lock:
                if proc.status == "exited":
                    break
            time.sleep(0.05)

    with contextlib.suppress(FileNotFoundError), open(proc.logfile, "rb") as f:
        while True:
            chunk = f.read(65536)
            if chunk:
                try:
                    out.write(frame(chunk) if frame else chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                continue
            if not follow:
                return
            with proc.lock:
                if proc.status == "exited":
                    return
            time.sleep(0.05)
