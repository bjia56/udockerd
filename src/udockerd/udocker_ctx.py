"""Bootstraps a single shared udocker context (config + local repo + CLI
helpers) at daemon startup, mirroring what udocker's own UMain does before
running any command.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from udocker.cli import UdockerCLI
from udocker.config import Config
from udocker.container.localrepo import LocalRepository
from udocker.tools import UdockerTools

if TYPE_CHECKING:
    from udocker.docker import DockerIoAPI


@dataclass
class UdockerContext:
    local: LocalRepository
    cli: UdockerCLI
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

    cli = UdockerCLI(local)
    _context = UdockerContext(
        local=local,
        cli=cli,
        dockerioapi=cli.dockerioapi,
        lock=threading.Lock(),
    )
    return _context


def get() -> UdockerContext:
    if _context is None:
        raise RuntimeError("udocker context not initialized; call udocker_ctx.init() first")
    return _context
