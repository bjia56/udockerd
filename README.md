# udockerd

`udockerd` is a Docker Engine API-compatible daemon backed by [udocker](https://github.com/indigo-dc/udocker).
It's built for running containers on Termux (Android) without any extra privileges.

The daemon runs on the Termux host. The `docker` CLI (or docker-py, or any Docker Engine API client) can run either
directly on Termux, or inside a `proot-distro` guest, and talks to it over `DOCKER_HOST=tcp://127.0.0.1:PORT`.

## Installation

```bash
pip install udockerd
```

Requires Python 3.12+ and a `curl` executable on `PATH` (Termux: `pkg install curl`, often preinstalled elsewhere).
udocker downloads its own proot/fakechroot execution backends the first time it needs them.

A self-contained [Cosmopolitan Libc](https://github.com/jart/cosmopolitan) build is also produced by CI as a single
multiplatform executable, requiring no host Python dependency at runtime.

## Usage

```
$ udockerd --help
usage: udockerd [-h] [--host HOST] [--port PORT] [--data-dir DIR] [-v] [-q] [--version]

Docker Engine API-compatible daemon backed by udocker.
Runs proot/fakechroot containers with no root, no namespaces, no cgroups.

options:
  -h, --help       show this help message and exit
  --host HOST      address to bind to (env: UDOCKERD_HOST, default: 127.0.0.1)
  --port PORT      port to listen on (env: UDOCKERD_PORT, default: 2375)
  --data-dir DIR   udocker data directory for images/containers/layers (env: UDOCKER_DIR, default: ~/.udocker)
  -v, --verbose    increase log verbosity (-v: info, -vv: debug)
  -q, --quiet      suppress all logging except warnings and errors
  --version        show program's version number and exit

examples:
  udockerd
      listen on 127.0.0.1:2375 (default)
  udockerd --host 0.0.0.0 --port 2375
      listen on all interfaces
  udockerd -v --data-dir ~/.udockerd-data
      verbose logging, custom udocker data directory

then point the docker CLI/SDK at it:
  export DOCKER_HOST=tcp://127.0.0.1:2375
  docker ps
```

With the daemon running and `DOCKER_HOST` pointed at it, the regular `docker` CLI and docker-py work as normal:

```bash
docker pull alpine
docker run --rm alpine echo hello
docker build -t myimage .
docker exec -it mycontainer sh
```

`docker build` only speaks the classic builder protocol, not BuildKit (see Scope below). If your Docker CLI
defaults to BuildKit, set `DOCKER_BUILDKIT=0`.

## Scope

`udockerd` covers the parts of the Docker Engine API used for container and image lifecycle operations, plus
`docker build`. It doesn't aim for full Docker parity.

Implemented: container create/start/stop/rm/list/inspect/logs, exec, attach, TTY sessions, image
pull/list/rm/inspect, and `docker build` (classic builder, one flattened layer per build, no build cache).

Not implemented: BuildKit, `HEALTHCHECK`/`ONBUILD`/`SHELL`/`--mount=`, remote or git build contexts,
networks-as-objects, volumes-as-objects, swarm, and compose-level features.

Networking is always effectively `--net=host`, since proot has no real network namespace and containers share the
Termux host's network stack. API responses report this honestly (empty/host IP, no fabricated per-container IPs)
rather than faking an isolated network.

## How it works

`udockerd` is a stdlib-only HTTP server (`http.server` plus `ThreadingMixIn`) that implements Docker Engine API
routes by driving udocker's Python library directly (`LocalRepository`, `DockerIoAPI`, and per-container
proot/fakechroot engines) rather than shelling out to a `udocker` CLI. Running containers are tracked in an
in-memory registry of pids, pgids, logs, and status. A small compiled C supervisor is prepended in front of every
container process to prevent orphaned processes when the daemon exits.

The daemon itself is pure Python, stdlib plus pure-Python `udocker`, which is what makes it possible to bundle into
a single Cosmopolitan Libc executable with no C extensions anywhere in the import graph. Native runtime
dependencies like `curl`, proot, fakechroot, and a C compiler for the supervisor stay external, fetched or invoked
on first use rather than bundled in.

## Supported platforms

Linux only. proot has no other target, and `udockerd` fails fast on anything else.

| Hardware architecture | Execution |
|-|-|
| x86_64, aarch64 | native (Cosmopolitan APE) |
| powerpc64le, i386, riscv64, loongarch64, s390x | via [Blink](https://github.com/jart/blink) |

Native x86_64/aarch64 need no extra tooling. The other architectures run the x86_64 build under Blink emulation.

## Building from source

```bash
pip install -e ".[dev]"   # install with dev deps (pytest, docker SDK, mypy, ruff)

ruff check src tests      # lint
mypy                       # type check (strict mode)
pytest -v                  # run tests (needs a working docker daemon)
```

Tests build `tests/docker/Dockerfile` into an unprivileged container, with no host docker socket and no special
privileges, mirroring the real Termux deployment. They run `udockerd` inside it and drive it with the real `docker`
CLI/SDK. If docker isn't available, harness-dependent tests just skip.

The Cosmopolitan-bundled multiplatform executable is built in CI (`.github/workflows/build.yml`). A Cosmopolitan
Python executable gets `udockerd` (and its `udocker` dependency) pip-installed and zip-embedded into its
`Lib/site-packages/`, static supervisor binaries for x86_64/aarch64 are built with `zig cc` and dropped into
`data/bin/`, and [`chimplink`](https://github.com/bjia56/chimp) bundles in Blink for the remaining Linux
architectures.

## Licensing

The code in this repository is licensed under Apache-2.0. `udockerd` depends on `udocker`, which is separately
licensed. See [udocker's repository](https://github.com/indigo-dc/udocker) for its license terms.
