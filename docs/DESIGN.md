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
In-memory registry only: `{container_id: {pid, pgid, logfile, status, ...}}`. `docker run`/`exec` spawns the real proot-wrapped process via the udocker engine (monkeypatching `subprocess.Popen` for the scope of the engine's own `run()` call, since it builds its command and calls `subprocess.call()` directly with no exposed hook — see `container_proc.py` for why and how the real launch call is distinguished from udocker's other internal `subprocess` use). stdout/stderr redirected to a per-container log file. Daemon restart loses live state — acceptable, since the underlying proot processes don't survive a daemon restart anyway.

### Process cleanup on daemon exit
No root, no namespaces, no cgroups available (Termux), and no ctypes in Cosmopolitan Python (`_ctypes` isn't compiled in — ruled out `prctl` via FFI directly). Orphan prevention uses a small compiled C supervisor (`data/supervisor.c`, compiled and cached on first use — requires `cc`/`gcc`/`clang` on PATH, same tier of runtime dependency as `curl`), prepended in front of every container command instead of exec'ing it directly:

- **Why a supervisor, not a plain PDEATHSIG exec wrapper**: `PR_SET_PDEATHSIG` only fires on the exact process that set it. udocker's proot engine forks its own traced child to run the actual container command — that fork does not inherit an active PDEATHSIG watch, so a plain wrapper would correctly kill proot when the daemon dies, but proot's child (the real container process) would be orphaned to init and keep running. Verified this gap empirically before fixing it.
- **The supervisor**: `setsid()`s itself, sets its own `PR_SET_PDEATHSIG` to `SIGTERM` (catchable, unlike `SIGKILL`, so it gets a chance to clean up), then `fork()`s — the child `exec`s the real command (inheriting the supervisor's pgid), the supervisor itself stays alive, reaping its child (and any double-forked descendants that don't call their own `setsid()`, same residual gap a real init has) via a `waitpid` loop.
- **Crash path (kill -9, OOM, daemon crash)**: kernel delivers `SIGTERM` to the supervisor via PDEATHSIG. Supervisor forwards `SIGTERM` to its child, waits a self-managed grace period (5s), then `killpg(0, SIGKILL)` on its own process group if anything's still alive. Self-managed because the supervisor can't tell whether a live daemon will still send a follow-up `SIGKILL` or whether the daemon is already gone — it has to assume the latter and act on its own.
- **Graceful path (daemon SIGTERM/normal stop)**: daemon's own handler does `os.killpg(pgid, SIGTERM)` per registry entry, waits a grace period, `os.killpg(pgid, SIGKILL)` for stragglers — same signal the supervisor would get from PDEATHSIG, so both paths converge on the same forward-then-escalate behavior.
- Registry entry's `pgid` is read as `popen_proc.pid` directly, not `os.getpgid()` — the supervisor's `setsid()` happens asynchronously after `Popen()` returns, so a syscall read immediately after risks observing the pre-`setsid()` value. `setsid()` guarantees pgid becomes the supervisor's own pid, which is exactly `popen_proc.pid`, so this is correct by construction rather than by timing.

### Network fidelity
proot has no real network namespace — all containers share the Termux host's network stack (effectively always `--net=host`). API responses report this honestly: empty/host IP in `NetworkSettings`, no fabricated per-container IPs. Avoids lying to clients that actually inspect networking; simple `docker ps/run/logs/exec` flows don't care.

### HTTP stack
`http.server.BaseHTTPRequestHandler` + `socketserver.ThreadingMixIn`, stdlib only (required for cosmo bundling — no aiohttp/etc). One thread per connection; long-lived exec/attach/logs streaming connections simply block their own thread, which is fine at expected single-user Termux concurrency levels.

## Testing (non-Termux dev host)

Run udockerd inside a plain (non-privileged, no docker-in-docker) Docker container as the test target:
- Test image: minimal Linux + `pip install udocker` + udockerd, no host docker socket, no special privileges — mirrors the real unprivileged Termux deployment shape.
- Expose udockerd's port from that container.
- Drive it with the **real** `docker` CLI on the host/CI runner, via `DOCKER_HOST=tcp://localhost:<exposed-port>` — validates actual Docker API client compatibility, not just a hand-rolled test client.
- CI-friendly: no privileged mode, no nested docker daemon required.

## Deployment / release

Cosmopolitan libc bundling, following [bodega's build-multiplatform job](https://github.com/bjia56/bodega/blob/main/.github/workflows/build.yml):
1. Start from Cosmopolitan Python executable (APE, dual x86_64/aarch64).
2. Install udockerd + udocker (pure-Python only) into a temp dir.
3. Zip-embed the packages into the cosmo Python executable.
4. Package entrypoint script in, produce a single portable `udockerd` executable.

Runtime-external, not bundled: `curl`, `proot`/`fakechroot` binaries (udocker downloads these itself on first use, as it already does today), and a C compiler (`cc`/`gcc`/`clang`) for the process supervisor (see above) — cosmo Python has no `ctypes`, so this is compiled on first use rather than embedded.
