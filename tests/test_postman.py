"""postman: the 07:00 morning report reaches the agent as a TASK, and is done in code."""

from __future__ import annotations


def test_postman_takes_the_scheduled_task_and_does_it_without_a_model():
    """The 07:00 schedule creates a TASK. postman listened for `dms` only, so that task could never be
    executed: it sat stalled from 2026-09-07 and the morning email stopped after 2026-09-05."""
    import ast
    from pathlib import Path

    src = Path("crews/postman_crew.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    spec = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "CrewSpec")
    kw = {k.arg: k.value for k in spec.keywords}
    listen = [ast.literal_eval(e) for e in kw["listen_for"].elts]
    assert "tasks" in listen, listen
    assert "on_task" in kw, "the scheduled task must be handled, and deterministically"
