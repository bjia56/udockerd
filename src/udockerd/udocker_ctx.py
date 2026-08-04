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

import os
import subprocess
import threading
from dataclasses import dataclass

from udocker.config import Config
from udocker.container.localrepo import LocalRepository
from udocker.container.structure import ContainerStructure
from udocker.docker import DockerIoAPI
from udocker.helper.hostinfo import HostInfo
from udocker.msg import Msg
from udocker.tools import UdockerTools
from udocker.utils.fileutil import FileUtil

from udockerd.config import is_termux


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


def _resolve_proot_executable(local: LocalRepository) -> str | None:
    """Mirrors PRootEngine.select_proot()'s executable resolution without
    building a full engine (no container/exec-mode context exists yet
    during layer extraction, which happens before a container is run).
    """
    executable = Config.conf["use_proot_executable"]
    if executable == "UDOCKER" or not executable:
        arch = HostInfo().arch()
        if HostInfo().oskernel_isgreater([4, 8, 0]):
            image_list = [f"proot-{arch}-4_8_0", f"proot-{arch}", "proot"]
        else:
            image_list = [f"proot-{arch}", "proot"]
        executable = FileUtil(local.bindir).find_file_in_dir(image_list)
    if executable and os.path.exists(executable):
        return str(executable)
    return None


def _capable_proot_executable(local: LocalRepository) -> str | None:
    """Resolved proot binary, but only if it actually supports
    --link2symlink -- otherwise None, so callers can treat "no usable
    proot" and "proot without the flag" identically.
    """
    proot_exec = _resolve_proot_executable(local)
    if proot_exec and HostInfo().cmd_has_option(proot_exec, "--link2symlink"):
        return proot_exec
    return None


def _patch_untar_layers_proot_retry(local: LocalRepository) -> None:
    """ContainerStructure._untar_layers runs a bare `tar -x` directly on
    the host (no proot involved -- that only wraps the container's own
    runtime, not image-layer extraction). On hosts where the destination
    filesystem/kernel policy rejects the hardlink() syscall (observed on
    Termux/Android, likely an SELinux restriction on the app's domain --
    ordinary Linux filesystems allow unprivileged hardlinks fine), any
    hardlink-type tar member fails and is silently dropped: such members
    carry zero payload bytes in the archive (verified: `tar tv` shows
    `size 0` for an 'h' typeflag entry), so tar has nothing to fall back
    to writing and just skips the path entirely, non-fatally, then
    continues with the rest of the layer (GNU tar only aborts a whole
    archive on --fatal-warnings, which udocker doesn't pass).

    Fix mirrors termux-packages' udocker patch: retry the failed
    extraction wrapped in `proot --link2symlink`, which turns hardlink()
    calls into symlink() instead. Only engaged when the first attempt
    fails, and only if a proot binary supporting the flag is available,
    so hosts where hardlinks already work (everywhere off Termux, in
    practice) never pay for it. `--overwrite` (already in udocker's tar
    invocation) makes re-running the whole extraction safe: previously
    -extracted files get harmlessly rewritten identically, and only the
    previously-missing hardlink members change (to symlinks).
    """
    original_untar_layers = ContainerStructure._untar_layers  # noqa: SLF001

    def _patched_untar_layers(
        self: ContainerStructure, tarfiles: list[str], destdir: str
    ) -> bool:
        proot_exec = _capable_proot_executable(local)

        # On Termux the bare-tar attempt is known to fail on any layer
        # carrying a hardlink member (SELinux rejects hardlink()), so skip
        # straight to the proot-wrapped retry instead of paying for a
        # doomed first pass -- but only once a capable proot is confirmed,
        # so a Termux host without one still gets the real attempt rather
        # than an unconditional false failure. Elsewhere, only pay for the
        # retry on failure.
        if is_termux() and proot_exec:
            status = False
        else:
            status = bool(original_untar_layers(self, tarfiles, destdir))
        if status or not tarfiles:
            return status

        if not proot_exec:
            return status

        Msg().out(
            "Info: extracting layer under",
            proot_exec,
            "--link2symlink"
            + ("" if is_termux() else " (host rejected a hardlink)"),
            l=Msg.INF,
        )
        optional_flags = ["--wildcards", "--delay-directory-restore"]
        for option in list(optional_flags):
            if not HostInfo().cmd_has_option("tar", option):
                optional_flags.remove(option)

        retry_status = True
        gid = str(HostInfo.gid)
        for tarf in tarfiles:
            verbose = "v" if Msg.level >= Msg.VER else ""
            cmd = [
                proot_exec,
                "--link2symlink",
                "tar",
                "-C",
                destdir,
                "-x" + verbose,
                "--one-file-system",
                "--no-same-owner",
                "--overwrite",
                "--exclude=dev/*",
                "--exclude=etc/udev/devices/*",
                "--no-same-permissions",
                r"--exclude=.wh.*",
                *optional_flags,
                "-f",
                tarf,
            ]
            if subprocess.call(cmd, stderr=Msg.chlderr, close_fds=True):
                Msg().err("Error: while extracting image layer (proot retry)")
                retry_status = False

        find_cmd = [
            "find",
            destdir,
            "(", "-type", "d", "!", "-perm", "-u=x", "-exec", "chmod", "u+x", "{}", ";", ")", ",",
            "(", "!", "-perm", "-u=w", "-exec", "chmod", "u+w", "{}", ";", ")", ",",
            "(", "!", "-perm", "-u=r", "-exec", "chmod", "u+r", "{}", ";", ")", ",",
            "(", "!", "-gid", gid, "-exec", "chgrp", gid, "{}", ";", ")", ",",
            "(", "-name", ".wh.*", "-exec", "rm", "-f", "--preserve-root", "{}", ";", ")",
        ]
        if subprocess.call(find_cmd, stderr=Msg.chlderr, close_fds=True):
            retry_status = False
            Msg().err("Error: while modifying attributes of image layer (proot retry)")

        return retry_status

    ContainerStructure._untar_layers = _patched_untar_layers  # noqa: SLF001


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
        # PRoot's ptrace-based path translation crosses bind-mount
        # boundaries for the container rootfs, which can make a guest
        # process's own hardlink() calls hit EXDEV/EPERM regardless of
        # host OS -- this is a documented PRoot limitation (hence PRoot
        # shipping --link2symlink at all), not a Termux/Android-only
        # issue. Safe to enable unconditionally: only takes effect when
        # the resolved proot binary actually supports the flag.
        Config.conf["proot_link2symlink"] = True

        local = LocalRepository()
        if not local.is_repo():
            local.create_repo()

        if not UdockerTools(local).install(False):
            raise RuntimeError("failed to install udocker execution tools (proot/fakechroot)")

        _patch_untar_layers_proot_retry(local)

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
