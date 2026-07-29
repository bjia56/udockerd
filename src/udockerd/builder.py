"""Classic `docker build`: Dockerfile parsing and stage execution."""

from __future__ import annotations

import hashlib
import json
import platform
import queue
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from udocker.container.structure import ContainerStructure
from udocker.engine.execmode import ExecutionMode
from udocker.helper.hostinfo import HostInfo
from udocker.utils.fileutil import FileUtil

from udockerd import udocker_ctx
from udockerd.container_proc import called_from_engine_run, original_popen, patch_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """Extracts tar_path into dest_dir via the system tar binary, same
    approach udocker itself uses for image layers. Members are validated
    for path-traversal with tarfile first, since tar -x itself won't
    reject them.
    """
    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, mode="r:*") as tar:
        for member in tar.getmembers():
            member_path = (dest_resolved / member.name).resolve()
            if member_path != dest_resolved and dest_resolved not in member_path.parents:
                raise BuildError(f"tar entry escapes destination: {member.name}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["tar", "-C", str(dest_dir), "-xf", str(tar_path)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(f"failed to extract tar: {result.stderr.decode(errors='replace')}")


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


def _shell_form_cmd(instruction_args: str) -> list[str]:
    """<shell-string> runs via /bin/sh -c, same as real Docker."""
    return ["/bin/sh", "-c", instruction_args]


def _exec_form_cmd(instruction_args: str) -> list[str] | None:
    """["executable", "arg", ...] (JSON array) form. Returns None if
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


@dataclass
class StageConfig:
    """Accumulates metadata-instruction state for one stage. Field names
    match Docker's image config JSON `Config` section; Env is kept as a
    dict here for easy merging/lookup and converted to Docker's
    "KEY=VALUE" list wire form by to_image_config().
    """

    Env: dict[str, str] = field(default_factory=dict)
    WorkingDir: str = ""
    User: str = ""
    Cmd: list[str] | None = None
    Entrypoint: list[str] | None = None
    Labels: dict[str, str] = field(default_factory=dict)
    ExposedPorts: dict[str, dict[str, Any]] = field(default_factory=dict)
    Volumes: dict[str, dict[str, Any]] = field(default_factory=dict)
    StopSignal: str = ""

    def to_engine_opt(self) -> dict[str, Any]:
        """Shape run_instruction's engine.opt expects (matches
        container_proc.opt_from_request_body's key names).
        """
        opt: dict[str, Any] = {}
        if self.Env:
            opt["env"] = [f"{k}={v}" for k, v in self.Env.items()]
        if self.WorkingDir:
            opt["cwd"] = self.WorkingDir
        if self.User:
            opt["user"] = self.User
        return opt

    def to_image_config(self) -> dict[str, Any]:
        """Docker image config JSON's `Config` section shape: Env as
        "KEY=VALUE" strings, matching what a real docker build produces
        and what docker-py/CLI expect to parse.
        """
        return {
            "Env": [f"{k}={v}" for k, v in self.Env.items()],
            "WorkingDir": self.WorkingDir,
            "User": self.User,
            "Cmd": self.Cmd,
            "Entrypoint": self.Entrypoint,
            "Labels": self.Labels,
            "ExposedPorts": self.ExposedPorts,
            "Volumes": self.Volumes,
            "StopSignal": self.StopSignal,
        }


def apply_metadata_instruction(op: str, args: str, config: StageConfig) -> None:
    """Mutates config for one metadata-only instruction (ENV, WORKDIR,
    USER, LABEL, CMD, ENTRYPOINT, EXPOSE, VOLUME, ARG, STOPSIGNAL). ARG is
    a no-op here: its substitution effect already happened at parse time
    (parse_dockerfile's env dict); it carries no image config.
    """
    if op == "ARG":
        return
    if op == "WORKDIR":
        # Relative WORKDIR is relative to the previous one, like real Docker.
        config.WorkingDir = args if args.startswith("/") else f"{config.WorkingDir}/{args}"
    elif op == "USER":
        config.User = args
    elif op == "STOPSIGNAL":
        config.StopSignal = args
    elif op == "ENV":
        if "=" in args.split(None, 1)[0]:
            for pair in shlex.split(args):
                key, _, value = pair.partition("=")
                config.Env[key] = value
        else:
            key, _, value = args.partition(" ")
            config.Env[key.strip()] = value.strip()
    elif op == "LABEL":
        for pair in shlex.split(args):
            key, _, value = pair.partition("=")
            config.Labels[key] = value
    elif op == "EXPOSE":
        for port in args.split():
            spec = port if "/" in port else f"{port}/tcp"
            config.ExposedPorts[spec] = {}
    elif op == "VOLUME":
        volumes = json.loads(args) if args.strip().startswith("[") else args.split()
        for volume in volumes:
            config.Volumes[volume] = {}
    elif op == "CMD":
        config.Cmd = _exec_form_cmd(args) or _shell_form_cmd(args)
    elif op == "ENTRYPOINT":
        config.Entrypoint = _exec_form_cmd(args) or _shell_form_cmd(args)


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


def _parse_copy_args(args: str) -> tuple[list[str], str, str | None]:
    """COPY/ADD ["src", ..., "dest"] or whitespace-separated shell form.
    --chown=/--chmod= are accepted and ignored (no uid mapping under
    proot). Returns (sources, dest, from_stage); last item is always dest.
    """
    stripped = args.strip()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ParseError(f"invalid COPY/ADD instruction: {args}")
        raw_tokens = list(parsed)
    else:
        raw_tokens = shlex.split(stripped)

    from_stage = None
    tokens = []
    for token in raw_tokens:
        if token.startswith("--from="):
            from_stage = token.split("=", 1)[1]
        elif token.startswith("--"):
            continue
        else:
            tokens.append(token)

    if len(tokens) < 2:
        raise ParseError(f"COPY/ADD requires at least one source and a destination: {args}")
    return tokens[:-1], tokens[-1], from_stage


def _resolve_source_path(base_dir: Path, src: str) -> Path:
    """Resolves src against base_dir (the build context, or a --from
    stage/image ROOT), rejecting escapes.
    """
    resolved = (base_dir / src).resolve()
    base_resolved = base_dir.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise BuildError(f"path outside source root: {src}")
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


def _materialize_image_root(image_spec: str) -> tuple[Path, str]:
    """COPY --from=<image> (not a prior stage): pulls/creates a throwaway
    container from that image so its files can be host-copied. Returns
    (root, container_id); caller must del_container(container_id) once done.
    """
    uctx = udocker_ctx.get()
    imagerepo, tag = udocker_ctx.split_imagespec(image_spec)
    resolved = udocker_ctx.resolve_imagerepo(uctx, imagerepo, tag)
    if resolved is None:
        raise BuildError(f"COPY --from: no such image: {image_spec}")
    imagerepo, tag = resolved
    container_id = ContainerStructure(uctx.local).create_fromimage(imagerepo, tag)
    if not container_id:
        raise BuildError(f"COPY --from: failed to materialize image: {image_spec}")
    container_dir = uctx.local.cd_container(container_id)
    return Path(container_dir) / "ROOT", container_id


def copy_instruction(
    op: str,
    args: str,
    root: Path,
    workdir: str,
    context_dir: Path,
    stage_containers: dict[str, Path],
) -> None:
    """Executes COPY/ADD against the given container ROOT. --from=<name>
    copies from a prior stage's ROOT (stage_containers, keyed by stage
    name and stringified index); --from=<image> materializes a throwaway
    container from that image first. Without --from, ADD additionally
    auto-extracts tarballs and fetches URLs; COPY treats every source as
    a plain file/directory copy.
    """
    sources, dest, from_stage = _parse_copy_args(args)
    dest_path = _resolve_dest_path(root, workdir, dest)

    # Dest is a directory (not a rename target) when there are multiple
    # sources or the Dockerfile dest ends in "/", matching real Docker.
    dest_is_dir = len(sources) > 1 or dest.endswith("/")
    if dest_is_dir:
        dest_path.mkdir(parents=True, exist_ok=True)

    from_root: Path | None = None
    from_container_id: str | None = None
    if from_stage is not None:
        from_root = stage_containers.get(from_stage)
        if from_root is None:
            from_root, from_container_id = _materialize_image_root(from_stage)

    try:
        for src in sources:
            if from_root is not None:
                src_path = _resolve_source_path(from_root, src.lstrip("/"))
                if not src_path.exists():
                    raise BuildError(f"{op} failed: no such file or directory: {src}")
            elif op == "ADD" and _is_url(src):
                target = dest_path / src.rsplit("/", 1)[-1] if dest_is_dir else dest_path
                _fetch_url(src, target)
                continue
            else:
                src_path = _resolve_source_path(context_dir, src)
                if not src_path.exists():
                    raise BuildError(f"{op} failed: no such file or directory: {src}")

            is_tar_add = (
                from_root is None
                and op == "ADD"
                and src_path.is_file()
                and tarfile.is_tarfile(src_path)
            )
            if is_tar_add:
                extract_tar(src_path, dest_path)
                continue

            target = dest_path / src_path.name if dest_is_dir else dest_path
            if src_path.is_dir():
                shutil.copytree(src_path, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, target)
    finally:
        if from_container_id is not None:
            udocker_ctx.get().local.del_container(from_container_id, force=True)


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


_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
_LAYER_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


def _sha256_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def commit_layer(container_id: str, config: StageConfig, tags: list[str]) -> str:
    """Flattens the given container's ROOT into a single image layer,
    synthesizes a v2-schema2 manifest + config, and registers each
    requested tag against it. Returns the image (config) digest.

    Mirrors the setup_tag() -> set_version("v2") -> save_json("manifest",
    ...) -> add_image_layer() sequence DockerIoAPI.get_v2() uses for
    pulled images, so inspect/list read this the same way as a real pull.
    """
    uctx = udocker_ctx.get()
    container_dir = Path(uctx.local.cd_container(container_id))
    root = container_dir / "ROOT"

    with tempfile.TemporaryDirectory() as tmp:
        layer_tar = Path(tmp) / "layer.tar"
        if not FileUtil().tar(str(layer_tar), sourcedir=str(root)):
            raise BuildError("failed to create image layer tar")
        layer_digest = _sha256_digest(layer_tar)
        layer_size = layer_tar.stat().st_size

        docker_arch = HostInfo().get_arch("uname", platform.machine(), "docker")

        config_json = {
            "architecture": docker_arch[0] if docker_arch else "amd64",
            "os": "linux",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config": config.to_image_config(),
        }
        config_bytes = json.dumps(config_json).encode("utf-8")
        config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"

        manifest = {
            "schemaVersion": 2,
            "mediaType": _MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": _CONFIG_MEDIA_TYPE,
                "size": len(config_bytes),
                "digest": config_digest,
            },
            "layers": [
                {
                    "mediaType": _LAYER_MEDIA_TYPE,
                    "size": layer_size,
                    "digest": layer_digest,
                }
            ],
        }

        if not tags:
            tags = [f"udockerd-build:{config_digest.split(':', 1)[1][:12]}"]

        for tag_spec in tags:
            imagerepo, tag = udocker_ctx.split_imagespec(tag_spec)
            uctx.local.setup_imagerepo(imagerepo)
            if not (uctx.local.setup_tag(tag) and uctx.local.set_version("v2")):
                raise BuildError(f"failed to register tag: {tag_spec}")
            uctx.local.save_json(config_digest, config_json)
            uctx.local.save_json("manifest", manifest)

            layer_dest = Path(uctx.local.layersdir) / layer_digest
            if not layer_dest.exists():
                shutil.copy2(layer_tar, layer_dest)
            uctx.local.add_image_layer(str(layer_dest))

    return config_digest


def _find_target_index(stages: list[Stage], target: str) -> int:
    for i, stage in enumerate(stages):
        if stage.name == target:
            return i
    raise BuildError(f"target stage {target!r} could not be found")


def build(
    *,
    context_dir: Path,
    dockerfile_path: Path,
    tags: list[str],
    buildargs: dict[str, str],
    labels: dict[str, str],
    target: str | None,
) -> Iterator[dict[str, Any]]:
    """Runs every stage in order, yielding Docker-API-shaped JSON-lines
    progress dicts. The last stage run (the whole file, or up to --target)
    gets committed as the final image; earlier stages are scratch space
    for COPY --from and deleted once the build finishes.
    """
    stages = parse_dockerfile(dockerfile_path.read_text(), buildargs)
    last_index = len(stages) - 1 if target is None else _find_target_index(stages, target)
    active_stages = stages[: last_index + 1]

    total_steps = sum(1 + len(stage.instructions) for stage in active_stages)
    step = 0

    uctx = udocker_ctx.get()
    stage_containers: dict[str, Path] = {}
    stage_container_ids: list[str] = []
    final_container_id = ""
    final_config = StageConfig()

    try:
        for i, stage in enumerate(active_stages):
            step += 1
            base_spec = f"{stage.base_image}:{stage.base_tag}"
            yield {"stream": f"Step {step}/{total_steps} : FROM {base_spec}\n"}

            with uctx.lock:
                resolved = udocker_ctx.resolve_imagerepo(uctx, stage.base_image, stage.base_tag)
                if resolved is None:
                    raise BuildError(f"no such image: {base_spec}")
                imagerepo, tag = resolved
                container_id = ContainerStructure(uctx.local).create_fromimage(imagerepo, tag)
            if not container_id:
                raise BuildError(f"failed to create build stage from {stage.base_image}")

            stage_container_ids.append(container_id)
            container_dir = Path(uctx.local.cd_container(container_id))
            root = container_dir / "ROOT"

            config = StageConfig()
            for instruction in stage.instructions:
                step += 1
                yield {"stream": f"Step {step}/{total_steps} : {instruction.raw}\n"}

                if instruction.op == "RUN":
                    opt = config.to_engine_opt()
                    yield from run_instruction(instruction.args, container_id, opt)
                elif instruction.op in ("COPY", "ADD"):
                    copy_instruction(
                        instruction.op,
                        instruction.args,
                        root,
                        config.WorkingDir,
                        context_dir,
                        stage_containers,
                    )
                else:
                    apply_metadata_instruction(instruction.op, instruction.args, config)

            if stage.name is not None:
                stage_containers[stage.name] = root
            stage_containers[str(i)] = root

            if i == last_index:
                final_container_id = container_id
                final_config = config

        final_config.Labels.update(labels)
        image_id = commit_layer(final_container_id, final_config, tags)
        yield {"stream": f"Successfully built {image_id[:12]}\n"}
        for tag_spec in tags:
            yield {"stream": f"Successfully tagged {tag_spec}\n"}
    finally:
        # All build containers are scratch space: once commit_layer has
        # tarred the final one's ROOT into the image layer (or the build
        # failed), none of them need to stick around.
        with uctx.lock:
            for container_id in stage_container_ids:
                uctx.local.del_container(container_id, force=True)
