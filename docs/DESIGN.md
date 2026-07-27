# udockerd Design

Docker Engine API-compatible daemon, backed by [udocker](https://github.com/indigo-dc/udocker) instead of docker/runc/containerd.

## Use case

Runs in background on Termux (Android). Client tooling (`docker` CLI, docker-py, etc.) runs inside a `proot-distro` guest and talks to udockerd on the Termux host.

## Feasibility summary

- udocker (`pip install udocker`) is a real, modular Python package (`LocalRepo`, `DockerIoAPI`, per-backend engines: proot/fakechroot/runc/singularity), not just a CLI wrapper.
- udocker's own Python code is **pure stdlib** — no compiled/C-extension dependencies in the package itself.
- Its HTTP downloader (`udocker/utils/curl.py`) optionally uses `pycurl` (C extension) but falls back cleanly to shelling out to a `curl` executable. We force the exeCurl path and never import pycurl.
- No root required; udocker runs entirely as the invoking user, downloading its execution backends (proot/fakechroot binaries) on first use — same tier of "external native binary dependency" as requiring `curl` on PATH.
- Conclusion: **feasible**, and compatible with Cosmopolitan libc bundling (pure-Python daemon code + pure-Python udocker package, both zipped into a cosmo Python executable per the bodega build-multiplatform pattern). Native binaries (curl, proot, fakechroot) stay external runtime dependencies fetched/installed separately — they are not part of the bundle.

## Architecture

```
Termux host
 ├── udockerd (cosmo executable, backgrounded)
 │     ├── HTTP server (stdlib http.server + ThreadingMixIn) on 127.0.0.1:PORT
 │     ├── Docker Engine API route handlers
 │     ├── udocker library calls (LocalRepo, DockerIoAPI, engine.proot, ...)
 │     └── in-memory container/process registry
 └── ~/.udocker (image/container storage, managed by udocker itself)

proot-distro guest
 └── docker CLI / docker-py, DOCKER_HOST=tcp://127.0.0.1:PORT
```

## Key decisions

### Transport
TCP socket, `127.0.0.1:PORT`, bound on the Termux host. proot-distro shares host loopback/network, so `DOCKER_HOST=tcp://127.0.0.1:PORT` just works. No unix-socket bind-mount complexity.

### udocker integration
Import udocker as a library (not CLI subprocess). Direct access to `LocalRepo`, `DockerIoAPI`, and engine classes gives structured errors and easier progress streaming for pulls, vs. brittle stdout parsing. Force udocker's exeCurl download path (skip pycurl probing) so no C extension is ever imported inside the cosmo interpreter. Runtime requires a `curl` binary on PATH (Termux: `pkg install curl` or often preinstalled).

### API scope (v1)
Core container + image lifecycle, plus exec and attach/logs streaming:
- `/containers/create`, `/start`, `/stop`, `/rm`, `/json` (list), `/{id}/json` (inspect), `/{id}/logs`
- `/containers/{id}/exec` + `/exec/{id}/start` (exec)
- `/containers/{id}/attach` (attach/streaming)
- `/images/create` (pull), `/images/json` (list), `/images/{name}` (rm), `/images/{name}/json` (inspect)
- `/version`, `/info`, `/_ping`

Out of scope for v1: networks-as-objects, volumes-as-objects, swarm, build, compose-level features.

### Process tracking
In-memory registry only: `{container_id: {pid, pgid, logfile, status, ...}}`. `docker run`/`exec` spawns the real proot-wrapped process via the udocker engine, monkeypatching `subprocess.Popen` for the scope of the engine's own `run()` call (it builds its command and calls `subprocess.call()` directly with no exposed hook — see `container_proc.py`). stdout/stderr redirected to a per-container log file. Daemon restart loses live state — acceptable, since the proot processes don't survive a daemon restart anyway.

### Process cleanup on daemon exit
No root/namespaces/cgroups (Termux), no ctypes in Cosmopolitan Python (rules out `prctl` via FFI). Orphan prevention uses a small compiled C supervisor (`data/supervisor.c`, compiled and cached on first use, requires `cc`/`gcc`/`clang` on PATH), prepended in front of every container command:

- **Why a supervisor, not a plain PDEATHSIG exec wrapper**: `PR_SET_PDEATHSIG` only fires on the exact process that set it. udocker's proot engine forks its own traced child to run the container command — that fork doesn't inherit the watch, so a plain wrapper would kill proot on daemon death but orphan proot's child.
- **The supervisor**: `setsid()`s itself, sets its own `PR_SET_PDEATHSIG` to `SIGTERM`, then `fork()`s — the child execs the real command, the supervisor stays alive reaping its child via a `waitpid` loop.
- **Crash path**: kernel delivers `SIGTERM` via PDEATHSIG. Supervisor forwards it, waits a self-managed grace period (5s) since it can't tell whether the daemon is still alive to send a follow-up kill, then `killpg(0, SIGKILL)`.
- **Graceful path**: daemon's SIGTERM handler does `os.killpg(pgid, SIGTERM)` per registry entry, waits, then `SIGKILL` for stragglers — converges with the crash path's behavior.
- Registry entry's `pgid` is read as `popen_proc.pid` directly, not `os.getpgid()`: the supervisor's `setsid()` is async after `Popen()` returns, but it's guaranteed to make pgid == its own pid == `popen_proc.pid`.

### Network fidelity
proot has no real network namespace — all containers share the Termux host's network stack (effectively always `--net=host`). API responses report this honestly: empty/host IP in `NetworkSettings`, no fabricated per-container IPs. Avoids lying to clients that actually inspect networking; simple `docker ps/run/logs/exec` flows don't care.

### HTTP stack
`http.server.BaseHTTPRequestHandler` + `socketserver.ThreadingMixIn`, stdlib only (required for cosmo bundling). One thread per connection; long-lived streaming connections just block their own thread, fine at expected single-user Termux concurrency. `protocol_version = "HTTP/1.1"` set explicitly — stdlib default HTTP/1.0 isn't recognized by the docker CLI's Go client for hijack responses.

### Docker Engine API wire-protocol details that aren't obvious from the docs
Found by tracing the actual `docker` CLI / `moby` client source:
- **Exec/attach hijack**: non-detached `docker exec`/`attach` send `Connection: Upgrade` + `Upgrade: tcp`; server must respond `101 UPGRADED` with matching headers, then the connection becomes a raw duplex byte stream.
- **Stream multiplexing**: on a non-TTY hijacked connection, output must be framed — 8-byte header (1 byte stream type: 1=stdout/2=stderr, 3 reserved, 4-byte big-endian length) before each chunk.
- **`/containers/{id}/wait` must send response headers before the container exits.** `ContainerWait` blocks synchronously on receiving headers (so `docker run` can call it before `ContainerStart` to arm the wait), reading the body separately. Fix: send headers immediately (`Connection: close`), block on the body write instead.
- **`/containers/{id}/kill` needs to exist** even though `/stop` covers graceful shutdown — `docker run` calls `kill` defensively in its own error paths.
- **Every long-lived streaming response needs `Connection: close`.** `protocol_version = "HTTP/1.1"` defaults to keep-alive; `/wait`, `/images/create`, `/logs?follow`, `/attach`, exec's non-hijack fallback all have unknown body length and no other end-of-body marker.
- **Hijacked connections need to be force-closed server-side once the handler returns**, or `BaseHTTPRequestHandler`'s request loop tries to parse a next request off the now-raw socket. Fix: `self.close_connection = True` right after the `101` response.

### Interactive sessions (TTY)
`docker exec -it` / `docker run -it` need a real pty for raw-mode passthrough and terminal semantics.

- **Ctty assignment across the supervisor/fork boundary**: opening a pty slave requires a session leader with no ctty. The supervisor already `setsid()`s itself, so its forked child is a session member, not leader — `supervisor.c` has the child `setsid()` again before opening the slave, landing it in a different session/pgid. Cleanup does `killpg(child_pid, SIGKILL)` before its own `killpg(0, ...)` in TTY mode (order matters: killing its own group first would skip the second call).
- **Output fan-out**: pty reads are destructive, so one dedicated reader thread per TTY session (`spawn_tty_reader`) owns the master fd, writes to the logfile, and fans out to subscribed exec/attach connections. Disconnecting doesn't kill the process (matches real Docker).
- **Stdin forwarding**: a per-connection thread reads the hijacked `rfile` and writes to the pty master.
- **Non-TTY stays multiplex-framed**; TTY is raw passthrough. `stream_session()` dispatches on `ContainerProc.tty`.

## Testing (non-Termux dev host)

Run udockerd inside a plain (non-privileged, no docker-in-docker) Docker container as the test target:
- Test image: minimal Linux + `pip install udocker` + udockerd, no host docker socket, no special privileges — mirrors the real unprivileged Termux deployment shape.
- Expose udockerd's port from that container.
- Drive it with the **real** `docker` CLI on the host/CI runner, via `DOCKER_HOST=tcp://localhost:<exposed-port>` — validates actual Docker API client compatibility, not just a hand-rolled test client.
- CI-friendly: no privileged mode, no nested docker daemon required.

## Deployment / release

Cosmopolitan libc bundling (`.github/workflows/build.yml`), following [bodega's build-multiplatform job](https://github.com/bjia56/bodega/blob/main/.github/workflows/build.yml):
1. Start from Cosmopolitan Python executable (APE).
2. Bundle setuptools into it (needed for the pip install step against the frozen interpreter).
3. `pip wheel` udockerd, `pip install` it (pulling in udocker as a normal dependency) into a temp dir, zip-embed both into the cosmo Python executable's `Lib/site-packages/`.
4. Append `scripts/.args` (`-m udockerd`) so the resulting executable runs the daemon by default with no arguments needed.
5. chimplink for multiplatform APE support — x86_64/aarch64 natively via `ape-*.elf`, plus blink for less-common Linux architectures (powerpc64le, i386, riscv64, loongarch64, s390x). Unlike bodega, **no non-Linux targets** — udockerd only runs on Linux (proot has no other target). `udockerd.config.check_linux()` fails fast on a non-Linux platform.
6. CI verifies the built binary actually runs (`--help`) on a real Linux x86_64 GitHub Actions runner.

Runtime-external, not bundled: `curl`, `proot`/`fakechroot` binaries (udocker downloads these itself on first use), and a C compiler for the process supervisor (compiled on first use, since cosmo Python has no `ctypes`).
