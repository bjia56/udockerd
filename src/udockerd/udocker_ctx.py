"""Bootstraps a single shared udocker context (config + local repo +
DockerIoAPI) at daemon startup, mirroring what udocker's own UMain does
before running any command.

Deliberately builds DockerIoAPI directly instead of going through
udocker.cli.UdockerCLI: importing udocker.cli pulls in
udocker.helper.unshare, which does `import ctypes` at module scope purely
to support `udocker setup --fixperm` (a CLI-only namespace-exec chown
fixup we never invoke). ctypes has no `_ctypes` backing on Cosmopolitan
Python, so that unused import chain would otherwise break bundling.
DockerIoAPI/LocalRepository/Config/UdockerTools have no such dependency.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from udocker.config import Config
from udocker.container.localrepo import LocalRepository
from udocker.docker import DockerIoAPI
from udocker.msg import Msg
from udocker.tools import UdockerTools


@dataclass
class UdockerContext:
    local: LocalRepository
    dockerioapi: DockerIoAPI
    lock: threading.Lock


_context: UdockerContext | None = None
_context_lock = threading.Lock()


def _udocker_msg_level(verbose: int, quiet: bool) -> int:
    """Mirror __main__.py's -v/-q -> logging level mapping onto udocker's
    own Msg verbosity, since they're separate systems udocker doesn't log
    through the stdlib `logging` module at all.
    """
    if quiet:
        return int(Msg.WAR)
    if verbose >= 2:  # noqa: PLR2004
        return int(Msg.DBG)
    if verbose == 1:
        return int(Msg.VER)
    return int(Msg.INF)


def init(verbose: int = 0, quiet: bool = False) -> UdockerContext:
    """Idempotent: safe to call more than once, or concurrently, though in
    practice it's only ever called once at startup before serving requests.
    """
    global _context
    with _context_lock:
        if _context is not None:
            return _context

        Config().getconf()
        # udocker only relays subprocess (tar/find, etc) stderr instead of
        # swallowing it to /dev/null when Msg.level >= Msg.DBG; below that,
        # failures like "Error: while extracting image layer" print with no
        # detail about what actually went wrong. Gated behind -vv rather
        # than always-on so default output stays as quiet as before.
        Msg().setlevel(_udocker_msg_level(verbose, quiet))
        # Only proot/fakechroot are supported (no root, no namespaces/cgroups
        # on Termux); default_execution_modes otherwise falls back to 'R1'
        # (runc) for unrecognized/DEFAULT and ppc64le arches, which isn't
        # usable here and isn't synchronized for concurrent engine use in
        # udocker's own runc/singularity engines.
        Config.conf["default_execution_modes"]["DEFAULT"] = "P1"
        Config.conf["default_execution_modes"]["ppc64le"] = "P1"

        local = LocalRepository()
        if not local.is_repo():
            local.create_repo()

        if not UdockerTools(local).install(False):
            raise RuntimeError("failed to install udocker execution tools (proot/fakechroot)")

        _context = UdockerContext(
            local=local,
            dockerioapi=DockerIoAPI(local),
            lock=threading.Lock(),
        )
        return _context


def get() -> UdockerContext:
    if _context is None:
        raise RuntimeError("udocker context not initialized; call udocker_ctx.init() first")
    return _context


def split_imagespec(imagespec: str) -> tuple[str, str]:
    if "@" in imagespec:
        imagerepo, tag = imagespec.split("@", 1)
    elif ":" in imagespec:
        imagerepo, tag = imagespec.split(":", 1)
    else:
        imagerepo, tag = imagespec, "latest"
    return imagerepo, tag


def resolve_imagerepo(uctx: UdockerContext, imagerepo: str, tag: str) -> tuple[str, str] | None:
    """Images are stored under a qualified path (docker.io/library/alpine)
    but clients ask for the short name; reuses DockerIoAPI's own
    name-qualification logic.
    """
    if uctx.local.cd_imagerepo(imagerepo, tag):
        return imagerepo, tag

    _, remoterepo = uctx.dockerioapi._parse_imagerepo(imagerepo)  # noqa: SLF001
    for candidate in (remoterepo, f"docker.io/{remoterepo}"):
        if candidate != imagerepo and uctx.local.cd_imagerepo(candidate, tag):
            return candidate, tag
    return None
