"""Regression test for the ExecutionEngineCommon.opt list-sharing bug in
apply_default_opt(): opt is a class-level dict, and udocker's own engine
code mutates its list-valued defaults in place (e.g.
_set_volume_bindings()'s opt["vol"].extend(...)) rather than reassigning
them. A shallow dict(engine.opt) copy leaves those lists shared across
every engine instance, so per-container state (e.g. hostauth's
/etc/passwd+/etc/group bind-file entries) silently leaked and
accumulated across every container/build the daemon ever ran -- a build
container's `groupadd` output could end up bound into a later, unrelated
container's /etc/group.

No Docker daemon needed: exercises ExecutionEngineCommon.opt directly.
"""

from __future__ import annotations

from udocker.engine.base import ExecutionEngineCommon

from udockerd.container_proc import apply_default_opt


def test_apply_default_opt_gives_each_engine_independent_lists() -> None:
    engine_a = ExecutionEngineCommon(None, None)
    apply_default_opt(engine_a)
    engine_b = ExecutionEngineCommon(None, None)
    apply_default_opt(engine_b)

    for key, value in engine_a.opt.items():
        if isinstance(value, list):
            assert value is not engine_b.opt[key], f"opt[{key!r}] list is shared across engines"

    # Simulates udocker's own in-place mutation (e.g. _set_volume_bindings()
    # appending a hostauth bind-file entry for engine_a's container).
    vol_b_before = list(engine_b.opt["vol"])
    engine_a.opt["vol"].append("engine-a-hostauth-file:/etc/group")
    assert engine_b.opt["vol"] == vol_b_before
