# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`udockerd` is a Docker Engine API-compatible daemon backed by [udocker](https://github.com/indigo-dc/udocker) instead of docker/runc/containerd. It runs on Termux (Android), backgrounded on the host, while `docker` CLI / docker-py running inside a `proot-distro` guest talks to it over `DOCKER_HOST=tcp://127.0.0.1:PORT`. No root, no namespaces/cgroups — containers run via udocker's proot/fakechroot userspace engines.

Read `docs/DESIGN.md` before making architectural changes — it documents *why* things are built the way they are (wire-protocol quirks, the process-supervisor design, Cosmopolitan bundling constraints), not just what the code does. Treat it as living documentation: update it when a decision it describes changes.

## Commands

```bash
pip install -e ".[dev]"   # install with dev deps (pytest, docker SDK, mypy, ruff)

ruff check src tests      # lint
mypy                       # type check (strict mode, configured via pyproject.toml)

pytest -v                  # run tests (builds a docker test-harness image; needs a docker daemon)
pytest -v tests/test_docker_api.py::test_name   # run a single test
pytest -v -k harness_smoke                       # run a subset by keyword
```

Tests require a working `docker` daemon — `tests/conftest.py`'s `harness_port` fixture builds `tests/docker/Dockerfile` into an unprivileged container (mirrors the real Termux deployment: no host docker socket, no special privileges), runs udockerd inside it, and tests drive it with the **real** `docker` CLI/SDK via `DOCKER_HOST` pointed at the exposed port. This validates actual Docker API client compatibility rather than a hand-rolled test client. If docker isn't available, harness-dependent tests skip.

CI (`.github/workflows/test.yml`, `build.yml`) runs `ruff check src tests`, `mypy`, `pytest -v`, and a Cosmopolitan-bundled multiplatform build (`scripts/add_to_zip.py` embeds udockerd + deps into a cosmo Python executable, then chimplink packages it for x86_64/aarch64 natively plus other Linux arches via blink).

## Hard constraints (apply to any change in `src/`)

- **Pure stdlib + pure-Python deps only.** The whole point is Cosmopolitan libc bundling: no C-extension imports anywhere in the import graph, including transitively. This is why `udocker_ctx.py` builds `DockerIoAPI`/`LocalRepository`/`Config`/`UdockerTools` directly instead of importing `udocker.cli.UdockerCLI` (which pulls in `ctypes` via `udocker.helper.unshare`, unused but still import-time-fatal). Adding a new dependency or import needs the same scrutiny.
- **No `ctypes`.** Cosmopolitan Python has no `_ctypes` backing. This is why process supervision (`supervisor.py` + `data/supervisor.c`) is a separately-compiled C helper rather than a `PR_SET_PDEATHSIG` ctypes call — compiled and cached (`~/.udockerd/`) on first use, requires a C compiler on PATH at runtime.
- **udocker's HTTP path must stay on `curl` executable, never pycurl.** Enforced via `UDOCKER_USE_CURL_EXECUTABLE` env var in `config.py`, set before `Config().getconf()` is called anywhere.
- **Linux only.** `config.check_linux()` fails fast elsewhere; don't add other-OS code paths (proot has no non-Linux target).
- **`udocker==1.3.17` is pinned exactly** in `pyproject.toml` — this codebase reaches into udocker's internal classes (`LocalRepo`, `DockerIoAPI`, `ContainerStructure`, engine internals), not just its documented CLI/API surface, so an unpinned upgrade can silently break internals we depend on.

## Architecture

Request flow: `__main__.py` does startup checks (`config.check_linux`, `check_curl_available`, `configure_udocker`) → `udocker_ctx.init()` builds one process-wide `UdockerContext` (shared `LocalRepository`, `DockerIoAPI`, lock) → `server.serve()` builds the route table and starts a threaded stdlib HTTP server.

- **`http.py`** — minimal regex-based `Router` on top of `BaseHTTPRequestHandler` + `ThreadingMixIn` (one thread per connection). No framework, by the stdlib-only constraint. `RequestContext` wraps a request/response and provides the primitives routes need: JSON in/out, raw streaming (`start_streaming`), and the hijack upgrade handshake (`is_upgrade_request`/`start_hijack`) for non-detached exec/attach. `protocol_version = "HTTP/1.1"` is required — the docker CLI's Go client hangs on hijack responses over HTTP/1.0.
- **`routes/{system,images,containers,exec,build}.py`** — one `register(router)` per module, called from `server.build_router()`. Route handlers take a `RequestContext` and talk to `udocker_ctx` + `container_proc` directly; there's no service/repository layer beyond that.
- **`container_proc.py`** — the core runtime piece. `ContainerRegistry` (module-level `registry`) tracks `ContainerProc` dataclasses (pid/pgid/status/logfile/tty state) by container id or exec id. `spawn()` runs udocker's `ExecutionMode(...).get_engine().run()` in a background thread; since that engine calls `subprocess` directly with no exposed `Popen` object, `subprocess.Popen` is monkeypatched for the scope of that specific call (frame-checked via `called_from_engine_run` so unrelated internal `Popen` calls, e.g. udocker's `HostInfo.cmd_has_option`, pass through untouched) to capture pid/pgid and prepend the compiled supervisor binary in front of the real command. TTY containers get a pty (`pty.openpty()`) with a single dedicated reader thread (`spawn_tty_reader`) fanning output out to subscribers, since pty reads are destructive. `stream_session`/`tail_log` are shared by exec-start, attach, and logs streaming. `patch_lock`/`original_popen`/`called_from_engine_run`/`apply_default_opt`/`apply_engine_opt` are exported (not private) since `builder.py`'s RUN executor reuses the same Popen-patch pattern synchronously, without the supervisor/pid tracking.
- **`builder.py`** — classic `docker build` implementation (Dockerfile parsing + stage execution) backing `routes/build.py`'s `POST /build`. See `docs/DESIGN.md`'s Build section for the design, and the gotcha below for the concurrency hazard specific to this module.
- **`udocker_ctx.py`** — the one process-wide udocker context (`LocalRepository`, `DockerIoAPI`, a lock), initialized once at startup. `split_imagespec`/`resolve_imagerepo` are shared image short-name-resolution helpers used by `routes/images.py`, `routes/containers.py`, and `builder.py`.

  **Concurrency gotcha:** `LocalRepository` (`uctx.local`) has unsynchronized "currently selected" cursor state — `cur_repodir`/`cur_tagdir`/`cur_containerdir` — mutated by `cd_imagerepo`/`setup_imagerepo`/`setup_tag`/`setup_container` and read by `get_image_attributes`/`save_json`/`load_json`/`add_image_layer`/`set_version`. This isn't just an `udockerd` concern — udocker's own library methods (`ContainerStructure.create_fromimage`, `DockerIoAPI.get`/`get_v2`) are themselves unlocked multi-statement sequences over this cursor. Since `uctx.local` is one instance shared by every request thread (`ThreadingMixIn`, one thread per connection), **any code that calls `cd_imagerepo`/`setup_*` and later depends on the cursor still pointing there must hold `uctx.lock` for the entire span** — acquiring it only around the initial `cd_imagerepo`/`create_fromimage` call and releasing before a dependent read (or another `setup_*`/`save_json` later in the same logical operation) leaves a window where a concurrent request can repoint the cursor and corrupt both operations' data (e.g. one build's manifest getting written into a different image's tag directory). Every route handler gets this right; `builder.py`'s `commit_layer()` and `_materialize_image_root()` (backing `COPY --from=<image>`) are the two call sites that needed this — check `with uctx.lock:` scope carefully if you touch either.
- **`supervisor.py` / `data/supervisor.c`** — orphan prevention without ctypes/namespaces/cgroups. The C supervisor `setsid()`s, sets `PR_SET_PDEATHSIG`, forks, execs the real command, and reaps it. Needed specifically because udocker's proot engine forks its own traced child that wouldn't inherit a plain PDEATHSIG wrapper's watch. See `docs/DESIGN.md`'s "Process cleanup on daemon exit" section for the full reasoning (crash path vs. graceful-shutdown path, TTY ctty/pgid ordering subtleties).
- **`server.py`** — SIGTERM handling: since `httpd.shutdown()` must be called off the `serve_forever()` thread, the handler spawns a thread that stops all containers (`container_proc.stop_all()`) then shuts the server down.

### Docker Engine API wire-protocol gotchas already solved here

These are non-obvious and easy to accidentally regress — see `docs/DESIGN.md` for the full detail if touching streaming/exec/attach code:
- Non-detached exec/attach hijack via `Connection: Upgrade`/`Upgrade: tcp` → `101 UPGRADED`, then raw duplex bytes; `close_connection = True` must be set right after or the handler tries to parse a next HTTP request off the raw socket.
- Non-TTY streams are multiplex-framed (`http.stream_frame`: 1-byte type + 3 reserved + 4-byte BE length + payload); TTY streams are raw passthrough.
- `/containers/{id}/wait` must send response headers immediately (before the container exits) and block on the body write, not the reverse.
- Every long-lived/unknown-length streaming response needs `Connection: close` (HTTP/1.1 defaults to keep-alive).
- `/containers/{id}/kill` must exist separately from `/stop` — `docker run` calls it defensively in its own error paths.

### Known scope boundaries (v1)

Networks-as-objects, volumes-as-objects, swarm, and compose-level features are explicitly out of scope. `docker build` is implemented (classic builder only, see `builder.py` and `docs/DESIGN.md`'s Build section) but with its own sub-scope limits: no BuildKit, no build cache, one flattened layer per build, no `HEALTHCHECK`/`ONBUILD`/`SHELL`/`--mount=`, no remote/git build contexts. Networking is always effectively `--net=host` (proot has no real network namespace) — API responses report this honestly (empty/host IP, no fabricated per-container IPs) rather than faking Docker-compatible network isolation.
