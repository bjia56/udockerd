"""Spawns and tracks container processes via udocker's execution engines.

engine.run() (PRootEngine, FakechrootEngine, ...) calls subprocess
directly with no exposed Popen object, so we monkeypatch subprocess.Popen
for the scope of that call to capture pid/pgid and prepend our process
supervisor (supervisor.py/.c, needed since Cosmopolitan Python has no
ctypes for PR_SET_PDEATHSIG).
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

# udocker's own internal helpers (e.g. HostInfo.cmd_has_option) also call
# subprocess.Popen during run(), before the real launch, so we check the
# caller frame to only intercept the actual container-launch call.
_ENGINE_RUN_FILENAMES = ("engine/proot.py", "engine/fakechroot.py")


def _called_from_engine_run() -> bool:
    # 0=here, 1=_patched_popen, 2=subprocess.call, 3=engine.run
    frame = sys._getframe(3)
    filename = frame.f_code.co_filename.replace("\\", "/")
    return frame.f_code.co_name == "run" and filename.endswith(_ENGINE_RUN_FILENAMES)

# engine.run() reads these opt keys but they're normally populated by
# udocker's own CLI-argument parsing, which we bypass. Values match the
# unset-CLI-flag defaults.
_EXTRA_OPT_DEFAULTS: dict[str, Any] = {
    "kernel": "",
    "netcoop": False,
}


def _apply_default_opt(engine: ExecutionEngineCommon) -> None:
    # ExecutionEngineCommon.opt is a class-level mutable dict shared by
    # every engine instance; copy before mutating or state leaks across
    # unrelated containers.
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
        if not _called_from_engine_run():
            return _original_popen(*args, **kwargs)  # noqa: S603

        # Release the lock (and unpatch) now: engine.run() is about to
        # block for the container's entire runtime inside Popen().wait(),
        # and holding the lock that long would serialize every other
        # container/exec start behind whichever one runs longest.
        unpatch()

        if proc.tty:
            master_fd, slave_fd = pty.openpty()
            proc.pty_master_fd = master_fd
            tty_slave_path = os.ttyname(slave_fd)
            os.close(slave_fd)  # supervisor's forked child reopens it after its own setsid
            args = _prepend_supervisor(args, supervisor_path, tty_slave_path)
            # No stdin/stdout/stderr kwargs: supervisor.c's own dup2()
            # sequence makes the pty slave the controlling terminal.
            popen_proc = _original_popen(*args, **kwargs)  # noqa: S603
            spawn_tty_reader(proc, master_fd)
        else:
            args = _prepend_supervisor(args, supervisor_path, None)
            log_fh = open(proc.logfile, "ab")  # noqa: SIM115 - closed via Popen's fd ownership
            kwargs["stdout"] = log_fh
            kwargs["stderr"] = log_fh
            popen_proc = _original_popen(*args, **kwargs)  # noqa: S603
            log_fh.close()  # Popen dup'd the fd; safe to close our copy
        with proc.lock:
            proc.pid = popen_proc.pid
            # Not os.getpgid(): supervisor's setsid() races a fresh
            # syscall read, but pgid == its own pid == popen_proc.pid.
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
    # engine.opt overrides captured at /containers/create time, applied
    # when /start spawns the process.
    opt: dict[str, Any] = field(default_factory=dict)
    # Condition, not Lock: /wait blocks on notify_all() instead of polling.
    lock: threading.Condition = field(default_factory=threading.Condition)

    tty: bool = False
    pty_master_fd: int | None = None
    pty_lock: threading.Lock = field(default_factory=threading.Lock)
    # Live attach/exec clients subscribed to this session's pty output, as
    # (write_callable, on_error_callable) pairs.
    subscribers: list[Any] = field(default_factory=list)


class ContainerRegistry:
    def __init__(self) -> None:
        self._containers: dict[str, ContainerProc] = {}
        self._lock = threading.Lock()

    def add(self, proc: ContainerProc, key: str | None = None) -> None:
        """key defaults to proc.container_id; exec instances pass exec_id
        instead, since their container_id field holds the target container.
        """
        with self._lock:
            self._containers[key or proc.container_id] = proc

    def get(self, id_or_name: str) -> ContainerProc | None:
        """Resolves by id or name (Docker API accepts either)."""
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
    fields; ignores fields with no proot/fakechroot equivalent (e.g.
    resource limits). Shared by containers.py and exec.py create.
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
        # Safety net: if run() errored before reaching Popen, unpatch()
        # above never ran and the lock would otherwise stay held forever.
        unpatch()


def spawn(proc: ContainerProc) -> None:
    """Runs engine.run() in a background thread so POST /start returns
    immediately, like the real Docker daemon. Mutates the given
    (already-registered) ContainerProc in place so existing references
    (e.g. a concurrent /wait) keep observing the same object.
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

    # Wait briefly for pid/pgid (or immediate failure); falls through
    # (still "created") if the engine never reaches Popen within timeout.
    with proc.lock:
        proc.lock.wait_for(lambda: proc.pid is not None or proc.status == "exited", timeout=10)


def stop(proc: ContainerProc, grace_seconds: float = 10.0) -> None:
    """SIGTERM the container's process group, wait, then SIGKILL stragglers."""
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
    """Direct signal for /containers/{id}/kill: no grace period, unlike /stop."""
    with proc.lock:
        pgid = proc.pgid
        status = proc.status
    if status != "running" or pgid is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def stop_all(grace_seconds: float = 10.0) -> None:
    """Sweeps all running containers; called from the daemon's SIGTERM handler."""
    for proc in registry.all():
        stop(proc, grace_seconds)


def spawn_tty_reader(proc: ContainerProc, master_fd: int) -> None:
    """One reader thread per TTY session: fans pty output out to the
    logfile and any subscribed clients. Single reader since pty reads are
    destructive (two readers would race for bytes).
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
    """Registers a live client for pty output; on_error fires once on
    write failure (client disconnected).
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
    """Shared by exec start and attach. TTY path: subscribe to the pty
    reader, forward stdin, raw passthrough. Non-TTY: tail_log
    (output-only, multiplex-framed). `in_` is the rfile for stdin
    forwarding, or None to skip it.
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
    """Streams logfile bytes as written. follow=False: read what's there
    and stop. follow=True: tail until exit or disconnect (`docker logs
    -f` / attach). `frame` wraps each chunk (multiplex framing) or None
    for raw bytes.
    """
    if follow:
        # docker run's attach can happen before start, so the logfile may
        # not exist yet; wait (bounded) for spawn() to create it.
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
