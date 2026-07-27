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
from udocker.tools import UdockerTools


@dataclass
class UdockerContext:
    local: LocalRepository
    dockerioapi: DockerIoAPI
    lock: threading.Lock


_context: UdockerContext | None = None


def init() -> UdockerContext:
    """Idempotent: safe to call once at startup before serving requests."""
    global _context
    if _context is not None:
        return _context

    Config().getconf()

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
