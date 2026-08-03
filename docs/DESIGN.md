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

**Hardlink extraction failures (`Error: while extracting/modifying attributes of image layer`)**: `ContainerStructure._untar_layers` runs a bare `tar -x` directly on the host — not wrapped in proot, unlike the container's own runtime — so on hosts where the destination filesystem/kernel policy rejects the `hardlink()` syscall (observed on Termux/Android, most likely an SELinux restriction on the app's domain; ordinary Linux filesystems allow unprivileged hardlinks fine), hardlink-type tar members fail and are silently dropped from the extracted layer. This is non-fatal to the container starting (GNU tar only aborts a whole archive on `--fatal-warnings`, which udocker doesn't pass) but leaves files missing. Hardlink-type tar entries carry zero payload bytes in the archive, so there's nothing for tar to fall back to writing — the only fix is re-extracting under something that translates `hardlink()` into `symlink()`.

`udocker_ctx.py` patches this two ways, mirroring [termux-packages' udocker patches](https://github.com/termux/termux-packages/tree/master/packages/udocker) but scoped to only the parts that generalize past Termux:
- `Config.conf["proot_link2symlink"] = True` unconditionally — this is upstream PRoot's own documented flag for its ptrace-based path translation crossing bind-mount boundaries (which can hit `EXDEV`/`EPERM` on *any* Linux host, not just Termux), so safe to enable everywhere; only takes effect if the resolved proot binary supports it.
- `ContainerStructure._untar_layers` is monkeypatched (not vendored — `udocker==1.3.17` stays pinned as-is) to retry a failed extraction wrapped in `proot --link2symlink`, engaged **only on failure**, so hosts where hardlinks already work (everywhere off Termux, in practice) never pay for it. `--overwrite` (already in udocker's tar invocation) makes the full retry safe to just re-run wholesale rather than tracking which specific layer/member failed.

Deliberately *not* ported: termux's patch also always wraps the extraction tar call in `proot --link2symlink` unconditionally, and adds `--sysvipc -L -p` to the container-runtime proot invocation for a separate shared-memory/IPC symptom. Neither has evidence of being a general-Linux problem (vs. Android-specific), so left out to avoid unconditional proot overhead / behavior changes (hardlinks → symlinks always) on hosts that don't need it.

### API scope (v1)
Core container + image lifecycle, plus exec and attach/logs streaming:
- `/containers/create`, `/start`, `/stop`, `/rm`, `/json` (list), `/{id}/json` (inspect), `/{id}/logs`
- `/containers/{id}/exec` + `/exec/{id}/start` (exec)
- `/containers/{id}/attach` (attach/streaming)
- `/images/create` (pull), `/images/json` (list), `/images/{name}` (rm), `/images/{name}/json` (inspect)
- `/build` (design only, not yet implemented — see Build section below)
- `/version`, `/info`, `/_ping`

Out of scope for v1: networks-as-objects, volumes-as-objects, swarm, compose-level features. `build` is designed (see below) but not yet implemented.

### Build (`docker build`)

**Protocol: classic builder only (`POST /build`), not BuildKit.** Modern `docker` CLI defaults to BuildKit — a gRPC session over an HTTP/2-upgraded connection, solving an LLB graph — which has no pure-stdlib Python implementation and conflicts outright with the cosmo-bundling constraint. Classic `/build` (tar context in, JSON-lines progress out) is what docker-py's `APIClient.build()`/`images.build()` speak by default, and what the `docker` CLI speaks with `DOCKER_BUILDKIT=0`. Document that env var (or docker-py) as a client-side runtime requirement, same tier as "needs `curl` on PATH."

Query params handled: `t` (repeatable — tag(s) to apply to the final image), `dockerfile` (path within the context, default `Dockerfile`), `buildargs` (JSON-encoded dict), `labels` (JSON-encoded dict), `target` (stage name, multi-stage). Accepted and ignored: `q`, `nocache`, `rm`, `forcerm`, `pull`, `cachefrom`, `networkmode`, `shmsize`, `squash`, `platform` — there's no build cache or separate "intermediate container" concept here (see Layering), and no cross-arch support. Request body is the build context tar, optionally gzipped (`tarfile.open(mode="r|*")` autodetects), extracted to a temp dir with `tarfile.extractall`.

Response is `Content-Type: application/json`, `Connection: close`, chunked JSON-lines — the same streaming shape as `/images/create`. Each instruction emits `{"stream": "Step N/M : <instruction>\n"}`, with captured `RUN` output as further `{"stream": ...}` lines. Failure: `{"errorDetail": {"message": ...}, "error": ...}`, then stop. **Success must end with a `{"stream": "Successfully built <12-hex-id>\n"}` line** (plus one `{"stream": "Successfully tagged <repo>:<tag>\n"}` per `-t`) — docker-py's `images.build()` regexes exactly that phrase out of the stream to resolve the built image id, and raises `BuildError` on an otherwise-successful build if it's missing.

**Layering: one flattened layer per build, not per-instruction.** Real docker gets cheap per-instruction layers from OverlayFS CoW diffs; proot/Termux has no such filesystem, so matching that would mean a full-tree snapshot-and-hash before/after every `RUN`/`COPY` — expensive, and still not real fidelity. Same trade-off already made for networking (see Network fidelity below): be honest about the platform's limits instead of faking it. Consequence: no build cache (`nocache` is moot), and multi-instruction Dockerfiles produce one bigger layer instead of docker's many small reused ones.

Mechanics: each stage builds in a throwaway container via `ContainerStructure(uctx.local).create_fromimage(base_imagerepo, base_tag)` — the same call `/containers/create` already uses — running instructions against its `container_dir/ROOT`. At the end of the *final* stage, the whole `ROOT` is tarred (`FileUtil(...).tar()`) into one layer blob, and a v2-schema2 manifest + config JSON are synthesized, mirroring the `setup_tag()` → `set_version("v2")` → `save_json("manifest", ...)` → `add_image_layer()` sequence `DockerIoAPI.get_v2()` already uses for pulled images. Non-final stages build the same way but stay unregistered scratch containers (never appear in `/images/json`) until every `COPY --from=<stage>` referencing them has run, then get deleted.

**Dockerfile support:** a single-pass parser (trailing-`\` line continuation, `#` comments, `ARG`/`ENV` `$VAR`/`${VAR}` substitution) producing an instruction list per stage. Supported: `FROM [... AS name]`, `RUN` (shell and exec form), `COPY [--from=stage|image]`, `ADD` (local files, tar auto-extraction, URL fetch via the same forced-curl path as image pulls — no remote/git build contexts), `ENV`, `WORKDIR`, `CMD`, `ENTRYPOINT`, `USER`, `LABEL`, `EXPOSE` (metadata-only, consistent with no real network namespace), `ARG`, `VOLUME` (metadata-only, consistent with no volumes-as-objects), `STOPSIGNAL`. Unsupported instructions (`HEALTHCHECK`, `ONBUILD`, `SHELL`, BuildKit-only flags like `--mount=`) fail the build with a clear `{"error": ...}` instead of silently no-opping.

- `RUN` executes through the same `ExecutionMode(...).get_engine()` + supervisor-prepend path as `container_proc.spawn()`, but synchronously (a build is one blocking streamed request, unlike `/start`'s fire-and-return) with stdout/stderr piped line-by-line into `{"stream": ...}` frames instead of a logfile. Non-zero exit aborts with `{"error": "The command '...' returned a non-zero code: N"}`, matching real docker's message shape.
- `COPY`/`ADD` without `--from` resolve against the extracted build-context temp dir straight on the host filesystem — no proot involved, same as how container creation already untars image layers directly.
- `COPY --from=<stage>` copies between the two scratch containers' `ROOT` dirs directly (host-side copy). `COPY --from=<image>` runs `ContainerStructure.create_fromimage` on that image into a throwaway extraction dir first.

New module: `routes/build.py` (`POST /build`) as thin request/response glue, backed by a new `builder.py` for Dockerfile parsing and stage execution — keeping the same split `container_proc.py`/routes already use.

Explicitly out of scope, same tier as the list above: BuildKit protocol, build cache, per-instruction layers, `HEALTHCHECK`/`ONBUILD`/`SHELL`, remote/git build contexts, `--mount=`/secrets, cross-arch `--platform` builds, image squashing beyond the single-layer default.

### Process tracking
In-memory registry only: `{container_id: {pid, pgid, logfile, status, ...}}`. `docker run`/`exec` spawns the real proot-wrapped process via the udocker engine, monkeypatching `subprocess.Popen` for the scope of the engine's own `run()` call (it builds its command and calls `subprocess.call()` directly with no exposed hook — see `container_proc.py`). stdout/stderr redirected to a per-container log file. Daemon restart loses live state — acceptable, since the proot processes don't survive a daemon restart anyway.

### Process cleanup on daemon exit
No root/namespaces/cgroups (Termux), no ctypes in Cosmopolitan Python (rules out `prctl` via FFI). Orphan prevention uses a small C supervisor (`data/supervisor.c`), prepended in front of every container command. Prebuilt static binaries ship for x86_64/aarch64; other arches compile it on first use via `cc`/`gcc`/`clang` on PATH.

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
2. Build static supervisor binaries for x86_64/aarch64 with `zig cc` (musl target), dropped into `data/bin/` before packaging — see [supervisor.py](#process-cleanup-on-daemon-exit).
3. Bundle setuptools into it (needed for the pip install step against the frozen interpreter).
4. `pip wheel` udockerd, `pip install` it (pulling in udocker as a normal dependency) into a temp dir, zip-embed both into the cosmo Python executable's `Lib/site-packages/`.
5. Append `scripts/.args` (`-m udockerd`) so the resulting executable runs the daemon by default with no arguments needed.
6. chimplink for multiplatform APE support — x86_64/aarch64 natively via `ape-*.elf`, plus blink for less-common Linux architectures (powerpc64le, i386, riscv64, loongarch64, s390x). Unlike bodega, **no non-Linux targets** — udockerd only runs on Linux (proot has no other target). `udockerd.config.check_linux()` fails fast on a non-Linux platform.
7. CI verifies the built binary actually runs (`--help`) on a real Linux x86_64 GitHub Actions runner.

Runtime-external, not bundled: `curl`, `proot`/`fakechroot` binaries (udocker downloads these itself on first use), and a C compiler for the process supervisor (compiled on first use, since cosmo Python has no `ctypes`).
