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
In-memory registry only: `{container_id: {pid, logfile, status, ...}}`. `docker run`/`exec` spawns the real proot-wrapped process via the udocker engine, stdout/stderr redirected to a per-container log file. Daemon restart loses live state — acceptable, since the underlying proot processes don't survive a daemon restart anyway (no real init/supervisor layer).

### Process cleanup on daemon exit
No root, no namespaces, no cgroups available (Termux) — orphan prevention relies on two kernel-native mechanisms, no daemon polling/supervisor needed:
- **Crash path (kill -9, OOM, unhandled crash)**: every spawned proot process gets `prctl(PR_SET_PDEATHSIG, SIGKILL)` set before exec, plus its own process group (`setpgid(0, 0)`). Flag survives `execve`, so descendants spawned by proot after exec inherit it. Kernel SIGKILLs the whole tree the instant the daemon process dies — no daemon code has to run for this to work.
- **Graceful path (daemon SIGTERM/normal stop)**: SIGTERM handler walks the container registry, `os.killpg(pgid, SIGTERM)` per container, grace period, then `os.killpg(pgid, SIGKILL)` for stragglers. Lets containerized processes see SIGTERM first instead of being hard-killed, while PDEATHSIG remains the backstop if this path is skipped (e.g. SIGKILL to daemon itself).
- Registry entry stores `(pid, pgid, logfile, status)`; both cleanup paths key off `pgid`.

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

Runtime-external, not bundled: `curl`, `proot`/`fakechroot` binaries (udocker downloads these itself on first use, as it already does today).
