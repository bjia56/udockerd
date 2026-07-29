"""Classic `docker build` implementation: Dockerfile parsing and stage
execution. See docs/DESIGN.md's Build section for the full design
rationale (why classic builder not BuildKit, one flattened layer per
build, etc).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

# Instructions with no pure-stdlib/proot-compatible equivalent, or that are
# BuildKit-only; failing loudly beats silently no-opping.
_UNSUPPORTED_INSTRUCTIONS = frozenset({"HEALTHCHECK", "ONBUILD", "SHELL"})

# Metadata-only instructions: recorded into the stage's config but not
# executed against the filesystem (no real network namespace or volumes
# object, consistent with the rest of udockerd's scope).
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
    """Raised for malformed or unsupported Dockerfile content; caught by
    routes/build.py and surfaced as a Docker-API-shaped {"error": ...} line.
    """


class BuildError(Exception):
    """Raised when a build step fails (nonzero RUN exit, missing COPY
    source, etc). Message shape matches real docker's own errors where
    docker-py/CLI pattern-match on them.
    """


@dataclass
class Instruction:
    op: str
    args: str
    raw: str  # original (post-substitution) line, for Step N/M display


@dataclass
class Stage:
    base_image: str
    base_tag: str
    name: str | None
    instructions: list[Instruction] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """# comments only when # starts the (stripped) line; Dockerfile syntax
    has no inline-comment support, so "RUN echo '#'" is left untouched.
    """
    if line.strip().startswith("#"):
        return ""
    return line


def _join_continuations(text: str) -> list[str]:
    """Joins trailing-backslash line continuations into single logical
    lines, dropping blank/comment-only lines.
    """
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
    """Expands $VAR and ${VAR} using the ARG/ENV values accumulated so far
    in the current stage. Unknown vars are left as empty string, matching
    Dockerfile semantics for unset ARGs with no default.
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
    """Single-pass Dockerfile parser producing a per-stage instruction
    list. ARG/ENV substitution is tracked per-stage (real Dockerfile scoping
    resets ARG at each FROM); buildargs seeds ARG values for stages that
    declare them.
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
