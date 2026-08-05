"""Spawns and tracks container processes via udocker's execution engines.

engine.run() calls subprocess directly with no exposed Popen object, so
we monkeypatch subprocess.Popen for the scope of that call to capture
pid/pgid and prepend our process supervisor (supervisor.py/.c, needed
since Cosmopolitan Python has no ctypes for PR_SET_PDEATHSIG).

patch_lock/original_popen/called_from_engine_run are shared with
builder.py's RUN executor, which patches Popen the same way but without
the supervisor or pid tracking.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from udocker.engine.execmode import ExecutionMode
from udocker.utils.uenv import Uenv

from udockerd import supervisor, udocker_ctx
from udockerd.http import STREAM_STDERR, STREAM_STDOUT, stream_frame

if TYPE_CHECKING:
    from udocker.engine.base import ExecutionEngineCommon

patch_lock = threading.Lock()
original_popen = subprocess.Popen

# udocker's own helpers (e.g. HostInfo.cmd_has_option) also call Popen
# during run(), before the real launch; frame-check to only intercept
# the actual container-launch call.
_ENGINE_RUN_FILENAMES = ("engine/proot.py", "engine/fakechroot.py")


def called_from_engine_run() -> bool:
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


def apply_default_opt(engine: ExecutionEngineCommon) -> None:
    # ExecutionEngineCommon.opt is a class-level dict shared by every
    # engine instance, and udocker mutates its list-valued defaults in
    # place (e.g. opt["vol"].extend(...)) rather than reassigning them --
    # a plain dict() copy still shares those lists, so per-container state
    # (e.g. hostauth's /etc/passwd+/etc/group bind files) leaks and
    # accumulates across every container/build the daemon ever runs.
    engine.opt = {
        key: list(value) if isinstance(value, list) else value for key, value in engine.opt.items()
    }
    # opt["env"] is a Uenv object, not a plain list, so it needs its own copy too.
    engine.opt["env"] = Uenv(engine.opt["env"].list())
    for key, default in _EXTRA_OPT_DEFAULTS.items():
        engine.opt.setdefault(key, default)


def apply_engine_opt(engine: ExecutionEngineCommon, opt: dict[str, Any]) -> None:
    """Merges a plain opt dict (opt_from_request_body's shape) into
    engine.opt, without mutating opt itself. opt["env"], if present, is
    a list[str] and must be merged into the existing Uenv rather than
    overwrite it — udocker's engine.run() calls Uenv-only methods
    (extendif, etc) on opt["env"]. Shared by container_proc.spawn() and
    builder.run_instruction().
    """
    engine.opt.update({k: v for k, v in opt.items() if k != "env"})
    env = opt.get("env")
    if env:
        engine.opt["env"].extend(env)


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
        if not called_from_engine_run():
            return original_popen(*args, **kwargs)  # noqa: S603

        # Release the lock (and unpatch) now: engine.run() is about to
        # block for the container's entire runtime inside Popen().wait(),
        # and holding the lock that long would serialize every other
        # container/exec start behind whichever one runs longest.
        unpatch()

        if proc.tty:
            master_fd, slave_fd = pty.openpty()
            with proc.pty_lock:
                proc.pty_master_fd = master_fd
            try:
                tty_slave_path = os.ttyname(slave_fd)
            except OSError:
                # os.ttyname() throws EINVAL under Cosmopolitan libc on
                # Android even though the fd itself is a perfectly good pty
                # slave (pty.openpty() above succeeded) -- ttyname()'s own
                # implementation is the broken part, not the fd. Fall back
                # to resolving the same path via /proc instead.
                tty_slave_path = os.readlink(f"/proc/self/fd/{slave_fd}")
            os.close(slave_fd)  # supervisor's forked child reopens it after its own setsid
            args = _prepend_supervisor(args, supervisor_path, tty_slave_path)
            # No stdin/stdout/stderr kwargs: supervisor.c's own dup2()
            # sequence makes the pty slave the controlling terminal.
            popen_proc = original_popen(*args, **kwargs)  # noqa: S603
            spawn_tty_reader(proc, master_fd)
        else:
            args = _prepend_supervisor(args, supervisor_path, None)
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
            popen_proc = original_popen(*args, **kwargs)  # noqa: S603
            # stdout/stderr kept as separate pipes (not one merged fd) so
            # each chunk can be tagged with its real stream type; the
            # logfile stores the docker mux-frame bytes directly (header +
            # payload) rather than plain text, so tail_log can stream it
            # straight through without knowing which stream each byte was.
            #
            # engine.run() calls subprocess.call() internally, whose
            # `with Popen(...) as p:` closes p.stdout/p.stderr the instant
            # p.wait() sees the child exit -- racing the log reader threads
            # below, which may still be draining buffered output on those
            # same file objects (ValueError: read of closed file, and
            # potential loss of the tail of the container's output). Reader
            # threads get their own duped fds so that close only affects
            # popen_proc's handles, not theirs.
            assert popen_proc.stdout is not None  # noqa: S101 - guaranteed by stdout=PIPE above
            assert popen_proc.stderr is not None  # noqa: S101 - guaranteed by stderr=PIPE above
            stdout_pipe = os.fdopen(os.dup(popen_proc.stdout.fileno()), "rb")
            stderr_pipe = os.fdopen(os.dup(popen_proc.stderr.fileno()), "rb")
            spawn_log_reader(proc, stdout_pipe, STREAM_STDOUT)
            spawn_log_reader(proc, stderr_pipe, STREAM_STDERR)
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
    # Set when engine.run() raises before/without ever exec'ing the target
    # (e.g. missing/non-executable Cmd) so /start and /wait can report it,
    # instead of the exception only reaching the daemon's own stderr.
    error: str | None = None
    logfile: str = ""
    # Guards logfile writes: non-TTY containers have two reader threads
    # (stdout/stderr) appending to the same file, and a torn write would
    # interleave one chunk's frame header with another chunk's payload.
    log_lock: threading.Lock = field(default_factory=threading.Lock)
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
    patch_lock.acquire()
    released = False

    def unpatch() -> None:
        # Once released, some other caller may already hold the lock and
        # have installed their own patched Popen; only the holder may
        # touch the global, or a delayed second call here (e.g. this
        # function's own finally, after Popen() already unpatched once)
        # would stomp on that other caller's patch out from under it.
        nonlocal released
        if released:
            return
        released = True
        subprocess.Popen = original_popen  # type: ignore[misc]
        patch_lock.release()

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
        apply_default_opt(engine)
        apply_engine_opt(engine, opt)
        try:
            exit_code = _run_engine_patched(engine, container_id, proc, supervisor_path)
        except Exception as exc:  # noqa: BLE001 - surface as container State.Error
            with proc.lock:
                proc.status = "exited"
                proc.exit_code = 127
                proc.error = str(exc)
                proc.finished_at = time.time()
                proc.lock.notify_all()
            return
        with proc.lock:
            proc.status = "exited"
            proc.exit_code = exit_code
            proc.finished_at = time.time()
            if proc.pid is None and exit_code != 0:
                # engine.run() never reached Popen at all (e.g. udocker's
                # own pre-exec check in ExecutionEngineCommon._check_executable
                # rejected a missing/non-executable Cmd/Entrypoint and
                # returned early) -- distinct from a process that started
                # and later exited non-zero on its own, where pid is set.
                cmd = proc.opt.get("entryp") or proc.opt.get("cmd") or []
                proc.error = f"command not found or has no execute bit set: {cmd}"
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


def spawn_log_reader(proc: ContainerProc, pipe: Any, stream_type: int) -> None:
    """One reader thread per non-TTY stdout/stderr pipe: tags each chunk
    with its real stream type and appends the framed bytes to the shared
    logfile, so a later tail_log() can stream the file straight through
    without needing to know which stream any given byte came from.
    """

    def reader() -> None:
        with pipe, open(proc.logfile, "ab") as logf:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                with proc.log_lock:
                    logf.write(stream_frame(stream_type, chunk))
                    logf.flush()

    threading.Thread(target=reader, daemon=True).start()


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
                except OSError as exc:
                    # EIO: the supervisor's forked child hasn't setsid()'d and
                    # opened the pty slave yet (we start reading right after
                    # Popen() returns, which only guarantees fork() happened,
                    # not that the child has run). Linux returns EIO rather
                    # than blocking when a pty master is read before any
                    # process holds the slave open. Retry until either the
                    # slave opens (read succeeds) or the container has
                    # actually exited (real, permanent hangup).
                    if exc.errno == errno.EIO:
                        with proc.lock:
                            exited = proc.status == "exited"
                        if exited:
                            break
                        time.sleep(0.02)
                        continue
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
    with proc.pty_lock:
        fd = proc.pty_master_fd
    if fd is None:
        return
    with contextlib.suppress(OSError):
        os.write(fd, data)


def resize_tty(proc: ContainerProc, height: int, width: int) -> bool:
    """Backs /containers/{id}/resize and /exec/{id}/resize. Returns False
    (caller sends 404/409) if there's no pty, or the ioctl itself failed
    (e.g. the container exited and the pty was torn down between the
    None-check and the call) -- not swallowed silently, since the docker
    CLI's own resize retry/give-up loop already treats a non-2xx here as
    the expected best-effort failure path.
    """
    with proc.pty_lock:
        fd = proc.pty_master_fd
    if fd is None:
        return False
    winsize = struct.pack("HHHH", height, width, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        return False
    return True


def stream_session(
    proc: ContainerProc, out: Any, in_: Any, *, frame: Any, on_stop: Any = None
) -> None:
    """Shared by exec start and attach. TTY path: subscribe to the pty
    reader, forward stdin, raw passthrough. Non-TTY: tail_log
    (output-only, multiplex-framed).

    `on_stop`, if given, runs once the session ends, to unblock
    forward_stdin's blocking read -- rfile.close() alone deadlocks (see
    RequestContext.shutdown_read).
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
        if on_stop is not None:
            on_stop()
        stdin_thread.join(timeout=1)


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
