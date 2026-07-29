"""Classic `docker build`: Dockerfile parsing and stage execution."""

from __future__ import annotations

import json
import queue
import shlex
import shutil
import subprocess
import tarfile
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from udocker.engine.execmode import ExecutionMode

from udockerd import udocker_ctx
from udockerd.container_proc import called_from_engine_run, original_popen, patch_lock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Unsupported: no stdlib/proot equivalent, or BuildKit-only. Fail loudly
# instead of silently no-opping.
_UNSUPPORTED_INSTRUCTIONS = frozenset({"HEALTHCHECK", "ONBUILD", "SHELL"})

# Metadata-only: recorded into stage config, not executed against the filesystem.
_METADATA_INSTRUCTIONS = frozenset(
    {
        "ENV",
        "WORKDIR",
        "USER",
        "LABEL",
        "CMD",
        "ENTRYPOINT",
        "EXPOSE",
        "VOLUME",
        "ARG",
        "STOPSIGNAL",
    }
)

_KNOWN_INSTRUCTIONS = _METADATA_INSTRUCTIONS | {"FROM", "RUN", "COPY", "ADD"}


class ParseError(Exception):
    """Malformed or unsupported Dockerfile content."""


class BuildError(Exception):
    """A build step failed (nonzero RUN exit, missing COPY source, etc)."""


@dataclass
class Instruction:
    op: str
    args: str
    raw: str  # post-substitution line, for Step N/M display


@dataclass
class Stage:
    base_image: str
    base_tag: str
    name: str | None
    instructions: list[Instruction] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """Only strips lines starting with #; inline # (e.g. "RUN echo '#'") stays."""
    if line.strip().startswith("#"):
        return ""
    return line


def _join_continuations(text: str) -> list[str]:
    """Joins trailing-backslash continuations into logical lines."""
    raw_lines = text.splitlines()
    logical_lines: list[str] = []
    buffer = ""
    for raw_line in raw_lines:
        line = _strip_comment(raw_line)
        if not line.strip() and not buffer:
            continue
        stripped = line.rstrip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buffer += stripped[:-1] + "\n"
            continue
        buffer += line
        if buffer.strip():
            logical_lines.append(buffer)
        buffer = ""
    if buffer.strip():
        logical_lines.append(buffer)
    return logical_lines


def _substitute(line: str, env: dict[str, str]) -> str:
    """Expands $VAR / ${VAR} from ARG/ENV values seen so far in the stage.
    Unknown vars expand to empty string.
    """
    result = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "$" and i + 1 < len(line):
            if line[i + 1] == "{":
                end = line.find("}", i + 2)
                if end != -1:
                    name = line[i + 2 : end]
                    result.append(env.get(name, ""))
                    i = end + 1
                    continue
            else:
                j = i + 1
                while j < len(line) and (line[j].isalnum() or line[j] == "_"):
                    j += 1
                if j > i + 1:
                    name = line[i + 1 : j]
                    result.append(env.get(name, ""))
                    i = j
                    continue
        result.append(ch)
        i += 1
    return "".join(result)


def _split_instruction(logical_line: str) -> tuple[str, str]:
    parts = logical_line.strip().split(None, 1)
    op = parts[0].upper()
    args = parts[1] if len(parts) > 1 else ""
    return op, args


def _parse_from_args(args: str) -> tuple[str, str, str | None]:
    tokens = shlex.split(args)
    if not tokens:
        raise ParseError("FROM requires an image argument")
    image_spec = tokens[0]
    stage_name = None
    if len(tokens) >= 3 and tokens[1].upper() == "AS":
        stage_name = tokens[2]
    if "@" in image_spec:
        base_image, base_tag = image_spec.split("@", 1)
    elif ":" in image_spec:
        base_image, base_tag = image_spec.split(":", 1)
    else:
        base_image, base_tag = image_spec, "latest"
    return base_image, base_tag, stage_name


def parse_dockerfile(text: str, buildargs: dict[str, str] | None = None) -> list[Stage]:
    """Single-pass parse into a per-stage instruction list. ARG/ENV state
    resets at each FROM; buildargs seeds ARG values.
    """
    buildargs = buildargs or {}
    stages: list[Stage] = []
    current: Stage | None = None
    env: dict[str, str] = {}

    for logical_line in _join_continuations(text):
        op, raw_args = _split_instruction(logical_line)
        if op not in _KNOWN_INSTRUCTIONS:
            if op in _UNSUPPORTED_INSTRUCTIONS:
                raise ParseError(f"unsupported instruction: {op}")
            raise ParseError(f"unknown instruction: {op}")

        args = _substitute(raw_args, env)

        if op == "FROM":
            base_image, base_tag, stage_name = _parse_from_args(args)
            current = Stage(base_image=base_image, base_tag=base_tag, name=stage_name)
            stages.append(current)
            env = {}
            continue

        if current is None:
            raise ParseError(f"{op} before any FROM")

        if op == "ARG":
            name, _, default = args.partition("=")
            name = name.strip()
            env[name] = buildargs.get(name, default)
        elif op == "ENV":
            # ENV KEY=VALUE or ENV KEY VALUE (legacy single-pair form)
            if "=" in args.split(None, 1)[0]:
                for pair in shlex.split(args):
                    key, _, value = pair.partition("=")
                    env[key] = value
            else:
                key, _, value = args.partition(" ")
                env[key.strip()] = value.strip()

        current.instructions.append(Instruction(op=op, args=args, raw=logical_line.strip()))

    if not stages:
        raise ParseError("Dockerfile has no FROM instruction")

    return stages


def _parse_copy_args(args: str) -> tuple[list[str], str]:
    """COPY/ADD ["src", ..., "dest"] or whitespace-separated shell form.
    --chown=/--chmod= flags are accepted and ignored (no uid mapping
    under proot). Returns (sources, dest); last item is always dest.
    """
    stripped = args.strip()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ParseError(f"invalid COPY/ADD instruction: {args}")
        tokens = list(parsed)
    else:
        tokens = [t for t in shlex.split(stripped) if not t.startswith("--")]
    if len(tokens) < 2:
        raise ParseError(f"COPY/ADD requires at least one source and a destination: {args}")
    return tokens[:-1], tokens[-1]


def _resolve_context_path(context_dir: Path, src: str) -> Path:
    """Resolves src against the build context, rejecting escapes."""
    resolved = (context_dir / src).resolve()
    context_resolved = context_dir.resolve()
    if resolved != context_resolved and context_resolved not in resolved.parents:
        raise BuildError(f"path outside build context: {src}")
    return resolved


def _resolve_dest_path(root: Path, workdir: str, dest: str) -> Path:
    """Resolves dest against the container ROOT + current WORKDIR, same
    as real Docker's relative-COPY-destination semantics.
    """
    base = root / workdir.lstrip("/") if workdir else root
    dest_path = base / dest.lstrip("/") if not dest.startswith("/") else root / dest.lstrip("/")
    return dest_path


def _is_url(src: str) -> bool:
    return src.startswith(("http://", "https://"))


def _fetch_url(url: str, dest: Path) -> None:
    """Downloads src to dest via the system curl binary (same PATH
    resolution udocker itself uses for its own downloads).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["curl", "-fsSL", "-o", str(dest), url],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(f"ADD: failed to fetch {url}: {result.stderr.decode(errors='replace')}")


def copy_instruction(op: str, args: str, root: Path, workdir: str, context_dir: Path) -> None:
    """Executes COPY/ADD (no --from) against the given container ROOT.
    ADD additionally auto-extracts tarballs and fetches URLs; COPY treats
    every source as a plain file/directory copy.
    """
    sources, dest = _parse_copy_args(args)
    dest_path = _resolve_dest_path(root, workdir, dest)

    # Dest is a directory (not a rename target) when there are multiple
    # sources or the Dockerfile dest ends in "/", matching real Docker.
    dest_is_dir = len(sources) > 1 or dest.endswith("/")
    if dest_is_dir:
        dest_path.mkdir(parents=True, exist_ok=True)

    for src in sources:
        if op == "ADD" and _is_url(src):
            target = dest_path / src.rsplit("/", 1)[-1] if dest_is_dir else dest_path
            _fetch_url(src, target)
            continue

        src_path = _resolve_context_path(context_dir, src)
        if not src_path.exists():
            raise BuildError(f"{op} failed: no such file or directory: {src}")

        if op == "ADD" and src_path.is_file() and tarfile.is_tarfile(src_path):
            dest_path.mkdir(parents=True, exist_ok=True)
            with tarfile.open(src_path, mode="r:*") as tar:
                tar.extractall(dest_path)  # noqa: S202 - context-local tar, escape already ruled out above
            continue

        target = dest_path / src_path.name if dest_is_dir else dest_path
        if src_path.is_dir():
            shutil.copytree(src_path, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, target)


def _shell_form_cmd(instruction_args: str) -> list[str]:
    """RUN <shell-string> runs via /bin/sh -c, same as real Docker."""
    return ["/bin/sh", "-c", instruction_args]


def _exec_form_cmd(instruction_args: str) -> list[str] | None:
    """RUN ["executable", "arg", ...] (JSON array). Returns None if
    instruction_args isn't valid JSON array syntax, so callers fall back
    to shell form.
    """
    stripped = instruction_args.strip()
    if not stripped.startswith("["):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
        return None
    return list(parsed)


def run_instruction(
    instruction_args: str, container_id: str, opt: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Executes RUN synchronously against the given (already-created)
    scratch container, yielding {"stream": ...} lines as output arrives.
    Raises BuildError on nonzero exit.
    """
    cmd = _exec_form_cmd(instruction_args) or _shell_form_cmd(instruction_args)

    uctx = udocker_ctx.get()
    exec_mode = ExecutionMode(uctx.local, container_id)
    engine = exec_mode.get_engine()
    engine.opt = dict(engine.opt)
    engine.opt.setdefault("kernel", "")
    engine.opt.setdefault("netcoop", False)
    engine.opt.update(opt)
    engine.opt["cmd"] = cmd

    lines: queue.Queue[str] = queue.Queue()
    reader_done = threading.Event()

    def reader(pipe: Any) -> None:
        for raw_line in iter(pipe.readline, b""):
            lines.put(raw_line.decode("utf-8", errors="replace"))
        pipe.close()
        reader_done.set()

    def make_patched_popen(unpatch: Any) -> Any:
        def patched_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            if not called_from_engine_run():
                return original_popen(*args, **kwargs)  # noqa: S603
            unpatch()
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT
            popen_proc = original_popen(*args, **kwargs)  # noqa: S603
            threading.Thread(target=reader, args=(popen_proc.stdout,), daemon=True).start()
            return popen_proc

        return patched_popen

    exit_code_box: list[int] = []

    def run_engine() -> None:
        patch_lock.acquire()
        released = False

        def unpatch() -> None:
            # Once released, another caller may already hold the lock
            # with their own patched Popen installed; a stale second call
            # here must not stomp on it.
            nonlocal released
            if released:
                return
            released = True
            subprocess.Popen = original_popen  # type: ignore[misc]
            patch_lock.release()

        subprocess.Popen = make_patched_popen(unpatch)  # type: ignore[misc]
        try:
            exit_code_box.append(int(engine.run(container_id)))
        finally:
            unpatch()
            # If run() failed before ever reaching Popen, no reader thread
            # was ever started to set this.
            reader_done.set()

    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    while not reader_done.is_set() or not lines.empty():
        try:
            yield {"stream": lines.get(timeout=0.05)}
        except queue.Empty:
            continue

    engine_thread.join()
    exit_code = exit_code_box[0] if exit_code_box else 1
    if exit_code != 0:
        raise BuildError(f"The command '{' '.join(cmd)}' returned a non-zero code: {exit_code}")


def build(
    *,
    context_dir: Path,
    dockerfile_path: Path,
    tags: list[str],
    buildargs: dict[str, str],
    labels: dict[str, str],
    target: str | None,
) -> Iterator[dict[str, Any]]:
    """Runs every stage, yields Docker-API-shaped JSON-lines progress dicts."""
    raise NotImplementedError
