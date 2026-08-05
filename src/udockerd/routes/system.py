"""System-level Docker Engine API endpoints: /version, /info, /_ping."""

import platform

from udockerd import __version__
from udockerd.http import RequestContext, Router

# Docker API version we claim to speak. Kept conservative; bump only once
# the corresponding response shapes/behaviors are actually implemented.
API_VERSION = "1.41"
MIN_API_VERSION = "1.24"


def version(ctx: RequestContext) -> None:
    ctx.send_json(
        200,
        {
            "Version": __version__,
            "ApiVersion": API_VERSION,
            "MinAPIVersion": MIN_API_VERSION,
            "GitCommit": "",
            "GoVersion": "",  # not Go, but clients generally just display this
            "Os": platform.system().lower(),
            "Arch": platform.machine(),
            "KernelVersion": platform.release(),
            "Components": [
                {"Name": "udockerd", "Version": __version__, "Details": {}},
            ],
        },
    )


def info(ctx: RequestContext) -> None:
    ctx.send_json(
        200,
        {
            "ID": "udockerd",
            "Containers": 0,
            "ContainersRunning": 0,
            "ContainersPaused": 0,
            "ContainersStopped": 0,
            "Images": 0,
            "Driver": "udocker",
            "OperatingSystem": platform.platform(),
            "OSType": platform.system().lower(),
            "Architecture": platform.machine(),
            "NCPU": 1,
            "MemTotal": 0,
            "ServerVersion": __version__,
            # No real network namespace under proot; be honest about it.
            "SecurityOptions": [],
        },
    )


def ping(ctx: RequestContext) -> None:
    ctx.start_streaming(200, {"Content-Length": "2", "API-Version": API_VERSION})
    ctx.wfile.write(b"OK")


def networks_prune(ctx: RequestContext) -> None:
    """POST /networks/prune — no networks-as-objects exist (see
    CLAUDE.md scope boundaries), so nothing is ever deleted. Still
    needs a real 200 here rather than 404: `docker system prune` calls
    this endpoint unconditionally (not gated by `-a`/`--volumes`), and
    the CLI aborts the whole command on any non-2xx/non-501 response.
    """
    ctx.send_json(200, {"NetworksDeleted": None})


def volumes_prune(ctx: RequestContext) -> None:
    """POST /volumes/prune — same rationale as networks_prune: no
    volumes-as-objects exist, so nothing to delete. Only reached from
    `docker system prune` when the client also passes `--volumes`.
    """
    ctx.send_json(200, {"VolumesDeleted": None, "SpaceReclaimed": 0})


def build_prune(ctx: RequestContext) -> None:
    """POST /build/prune — there is no build cache at all (one
    flattened layer per build, no cache, see docs/DESIGN.md's Build
    section). `docker system prune` requires a real 2xx here or it
    aborts the whole command, so answer the honest empty result: no
    cache entries exist, so none are deleted.
    """
    ctx.send_json(200, {"CachesDeleted": [], "SpaceReclaimed": 0})


def register(router: Router) -> None:
    router.add("GET", r"^/version$", version)
    router.add("GET", r"^/info$", info)
    router.add("GET", r"^/_ping$", ping)
    router.add("HEAD", r"^/_ping$", ping)
    router.add("POST", r"^/networks/prune$", networks_prune)
    router.add("POST", r"^/volumes/prune$", volumes_prune)
    router.add("POST", r"^/build/prune$", build_prune)
