"""POST /build (classic builder, not BuildKit): query params + build
context tar in, JSON-lines progress out.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from udockerd import builder

if TYPE_CHECKING:
    from udockerd.http import RequestContext, Router


def _query(ctx: RequestContext) -> dict[str, list[str]]:
    return parse_qs(urlsplit(ctx.path).query)


def _parse_json_param(query: dict[str, list[str]], key: str) -> dict[str, str]:
    raw = query.get(key, [""])[0]
    if not raw:
        return {}
    value = json.loads(raw)
    return dict(value) if isinstance(value, dict) else {}


def build(ctx: RequestContext) -> None:
    query = _query(ctx)
    tags = query.get("t", [])
    dockerfile_rel = query.get("dockerfile", ["Dockerfile"])[0]
    buildargs = _parse_json_param(query, "buildargs")
    labels = _parse_json_param(query, "labels")
    target = query.get("target", [None])[0]

    body = ctx.read_body()
    context_dir = Path(tempfile.mkdtemp(prefix="udockerd-build-"))
    try:
        context_tar = context_dir / ".context.tar"
        context_tar.write_bytes(body)
        try:
            builder.extract_tar(context_tar, context_dir)
        except builder.BuildError as exc:
            ctx.send_json(400, {"message": f"invalid build context: {exc}"})
            return
        finally:
            context_tar.unlink(missing_ok=True)

        dockerfile_path = context_dir / dockerfile_rel
        if not dockerfile_path.is_file():
            ctx.send_json(400, {"message": f"Cannot locate specified Dockerfile: {dockerfile_rel}"})
            return

        ctx.start_streaming(200, {"Content-Type": "application/json", "Connection": "close"})
        _stream_build(ctx, context_dir, dockerfile_path, tags, buildargs, labels, target)
    finally:
        shutil.rmtree(context_dir, ignore_errors=True)


def _write_line(ctx: RequestContext, payload: dict[str, Any]) -> None:
    ctx.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))


def _stream_build(
    ctx: RequestContext,
    context_dir: Path,
    dockerfile_path: Path,
    tags: list[str],
    buildargs: dict[str, str],
    labels: dict[str, str],
    target: str | None,
) -> None:
    try:
        for line in builder.build(
            context_dir=context_dir,
            dockerfile_path=dockerfile_path,
            tags=tags,
            buildargs=buildargs,
            labels=labels,
            target=target,
        ):
            _write_line(ctx, line)
    except (builder.ParseError, builder.BuildError) as exc:
        _write_line(ctx, {"errorDetail": {"message": str(exc)}, "error": str(exc)})


def register(router: Router) -> None:
    router.add("POST", r"^/build$", build)
