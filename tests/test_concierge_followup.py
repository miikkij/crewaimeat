"""The concierge follow-up of 2026-10-02: a declined request is not done, the verify report stays out of
the deliverable, and a crew definition can WRITE the owner's workspace.

The brief (aimeat-commercial docs/prompt-for-crewfive-concierge.md items 2 and 3, and the Yrittajan
peruspaketti CRM agent of aimeat-apps bundles/BUNDLE-AGENTS.md). The same three against a real node are
in test_concierge_followup_live.py.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from crewaimeat import decline, workspace_tools
from crewaimeat.lifecycle import LifecycleCallbacks
from crewaimeat.verify_report import report_message, split_verify

# ── 1. a declined request ends as declined, not done ────────────────────────────────────────


class Recorder:
    def __init__(self, answer=...):
        self.calls: list[tuple[str, dict]] = []
        self.answer = {"ok": True} if answer is ... else answer  # Recorder(None): the node did not answer

    def __call__(self, agent_name, tool, payload, **kw):
        self.calls.append((tool, payload))
        return self.answer

    def tools(self):
        return [t for t, _ in self.calls]


def _callbacks(rec, refusals=None, todos=None):
    return LifecycleCallbacks(
        call=rec,
        eval_ctx=lambda info: None,
        mark_todos_done=lambda agent, tid: (todos if todos is not None else []).append(tid),
        deliverable_keys={},
        refusals=refusals,
    )


@pytest.fixture(autouse=True)
def _no_leftover_declines():
    decline._DECLINED.clear()
    yield
    decline._DECLINED.clear()


def test_a_declined_task_is_failed_with_its_reason_and_not_completed():
    rec, todos = Recorder(), []
    decline.note_declined("t1", "I have no tool that writes to the CRM. The CADENCE agent can.")
    _callbacks(rec, todos=todos).complete_callback("concierge", "t1", mem_key="crews.concierge.t1")(None)

    assert rec.tools() == ["aimeat_task_fail"], "failed, never completed"
    msg = rec.calls[0][1]["message"]
    assert msg.startswith("Declined:")
    assert "I have no tool that writes to the CRM" in msg and "The CADENCE agent can" in msg
    assert "crews.concierge.t1" in msg, "the person can find the full reply"
    assert todos == [], "its todos are not marked done either"


def test_a_task_nobody_declined_completes_as_before():
    rec = Recorder()
    _callbacks(rec).complete_callback("concierge", "t1", mem_key="k")(None)
    assert rec.tools() == ["aimeat_task_complete"]


def test_a_refusal_by_the_node_wins_over_a_decline_and_the_decline_is_cleared():
    rec = Recorder()
    decline.note_declined("t1", "I could not write it.")
    refused = [{"call": "POST /v1/organisms/o/publish", "needed": ["organism:write"]}]
    _callbacks(rec, refusals=lambda a, s: refused).complete_callback(
        "concierge", "t1", since="2026-10-02T00:00:00.000Z"
    )(None)
    assert rec.tools() == ["aimeat_task_fail"]
    assert "organism:write" in rec.calls[0][1]["message"], "the more fundamental reason is the one named"
    assert decline.take_declined("t1") is None, "and nothing is carried to the next run"


def test_a_decline_the_node_does_not_take_fails_loudly():
    with pytest.raises(RuntimeError, match="Declining task t1 failed"):
        decline.note_declined("t1", "no")
        _callbacks(Recorder(answer=None)).complete_callback("concierge", "t1")(None)


def test_the_decline_tool_records_against_its_own_task_only():
    tool = decline.make_decline_tool("concierge", "t1")
    out = tool.run(reason="Not mine to do.")
    assert "declined" in out
    assert decline.take_declined("t1") == "Not mine to do."
    assert decline.take_declined("t1") is None, "taken once"

    no_task = decline.make_decline_tool("concierge", None)
    no_task.run(reason="whatever")
    assert decline._DECLINED == {}, "a run without a task has nothing to mark"


def test_the_first_reason_stands():
    decline.note_declined("t1", "first")
    decline.note_declined("t1", "second")
    assert decline.take_declined("t1") == "first"


def test_a_crew_definition_gets_decline_bound_to_the_run_s_task():
    from crewaimeat.crew_def import TOOL_PURPOSES, resolve_tool, validate_crew_doc

    [tool] = resolve_tool("decline")("crm", SimpleNamespace(task={"id": "t9"}))
    assert tool.name == "decline_request"
    tool.run(reason="That is LAHETIN's work.")
    assert decline.take_declined("t9") == "That is LAHETIN's work."
    assert "decline" in TOOL_PURPOSES
    doc = {
        "agent_name": "crm",
        "agents": [{"role": "Clerk", "goal": "g", "backstory": "b", "tools": ["workspace_write", "decline"]}],
        "tasks": [{"id": "run", "agent": "Clerk", "description": "Do {{ctx.prompt}}", "expected_output": "x"}],
    }
    assert validate_crew_doc(doc) == []


@pytest.fixture
def concierge():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "crews" / "concierge_crew.py"
    spec = importlib.util.spec_from_file_location("concierge_crew_followup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_concierge_can_decline_an_assigned_task(concierge):
    from crewaimeat.aimeat_crew import BuildContext

    ctx = BuildContext(task={"id": "t5"}, prompt="Add a contact to the CRM", llm="gpt-4o-mini", today="2026-10-02")
    [agent], [task] = concierge.build_domain(ctx)
    assert "decline_request" in [t.name for t in agent.tools]
    assert "decline_request" in task.description
    assert "Proposing an agent for it IS doing it" in task.description, "a proposal is not a refusal"


def test_a_direct_message_has_no_list_to_decline_on(concierge):
    task = concierge._task("Add a contact to the CRM", "", None, "2026-10-02")
    assert "decline_request" not in task.description
    names = [t.name for t in concierge._concierge_tools({"attachments": []}, ask_to="u", ask_conv="c")]
    assert "decline_request" not in names


def test_every_proposed_agent_can_decline():
    from crewaimeat.concierge_propose import build_crew_def

    d = build_crew_def(
        name="morning-deals",
        display_name="Morning deals",
        purpose="Reads the open deals.",
        instructions="Read them.",
        tools=["workspace"],
        workspace=None,
        delivers="",
    )
    assert d["agents"][0]["tools"] == ["workspace", "decline"]
    assert "decline_request" in d["agents"][0]["backstory"]


# ── 2. the verify report stays out of the deliverable ───────────────────────────────────────

MEASURED = (
    "Acme Oy offers bookkeeping for small firms.\n\n"
    "Our service is GDPR compliant [unverified: not in sources] and costs 49 EUR a month.\n\n"
    "---\n"
    "Verify: faithfulness | score=1 | unsupported=1 | GDPR claim not found in sources\n"
)


def test_the_measured_deliverable_comes_out_clean_and_the_report_is_kept():
    body, report = split_verify(MEASURED)
    assert "Verify:" not in body and "[unverified" not in body
    assert body.startswith("Acme Oy offers bookkeeping") and "costs 49 EUR a month." in body
    assert not body.rstrip().endswith("---"), "the rule before the verdict goes with it"
    assert report["score"] == 1 and report["unsupported"] == 1
    assert "GDPR claim not found in sources" in report["verdict"]
    assert report["flagged"] == ["Our service is GDPR compliant [unverified: not in sources] and costs 49 EUR a month."]
    assert "1 line(s) carried an [unverified] mark" in report_message(report)


@pytest.mark.parametrize(
    "line",
    [
        "Verify: pass",
        "Verify: fixed - added the missing date",
        "**Verify: faithfulness | score=5 | unsupported=0 | all supported**",
        "- Verify: pass",
        "`Verify: pass`",
    ],
)
def test_every_verdict_shape_the_reviewer_writes_is_taken_out(line):
    body, report = split_verify(f"The answer.\n\n{line}\n")
    assert body.strip() == "The answer."
    assert report and report["verdict"]


def test_a_deliverable_s_own_verify_line_is_left_alone():
    text = "Checklist:\n- Verify: the door is locked\n- Leave"
    assert split_verify(text) == (text, None)


def test_text_without_a_report_is_untouched():
    assert split_verify("Plain answer.") == ("Plain answer.", None)
    assert split_verify("") == ("", None)


def test_the_published_deliverable_is_the_clean_one_and_the_crew_s_cleaner_still_runs():
    from crewaimeat.aimeat_crew import _without_verify_report

    rec = Recorder()
    cb = _callbacks(rec).publish_callback(
        "concierge", "crews.concierge.t1", task_id="t1", clean=_without_verify_report(str.upper)
    )
    cb(SimpleNamespace(raw=MEASURED))
    published = rec.calls[0][1]["value"]
    assert "VERIFY:" not in published and "[UNVERIFIED" not in published
    assert published.startswith("ACME OY"), "the crew's own cleaner ran after"


def test_a_report_only_output_is_published_as_it_is_rather_than_empty():
    from crewaimeat.aimeat_crew import _without_verify_report

    assert _without_verify_report(None)("Verify: pass") == "Verify: pass"


def test_the_verify_link_logs_the_report_feeds_the_chain_raw_and_leaves_the_output_clean(monkeypatch, capsys):
    from crewaimeat import aimeat_crew

    rec = Recorder()
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", rec)
    seen = []
    task = SimpleNamespace(callback=lambda out: seen.append(out.raw))
    aimeat_crew._keep_verify_report_out("concierge", "t1", task)
    out = SimpleNamespace(raw=MEASURED)
    task.callback(out)

    assert seen == [MEASURED], "the publish chain (and the score link in it) still read the raw text"
    assert "Verify:" not in out.raw and "[unverified" not in out.raw, "what the finalize step sees is clean"
    [(tool, ev)] = rec.calls
    assert tool == "aimeat_task_event" and ev["type"] == "verification" and ev["task_id"] == "t1"
    assert ev["details"]["score"] == 1 and "GDPR claim" in ev["message"]
    err = capsys.readouterr().err
    assert "verify report (kept out of the deliverable)" in err and "unverified: Our service is GDPR" in err


def test_a_message_run_logs_the_report_but_has_no_task_to_attach_it_to(monkeypatch):
    from crewaimeat import aimeat_crew

    rec = Recorder()
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", rec)
    task = SimpleNamespace(callback=None)
    aimeat_crew._keep_verify_report_out("concierge", "msg-1", task)
    task.callback(SimpleNamespace(raw=MEASURED))
    assert rec.calls == []


def test_a_failed_publish_still_fails_through_the_verify_link(monkeypatch):
    from crewaimeat import aimeat_crew

    monkeypatch.setattr(aimeat_crew, "_aimeat_call", Recorder())

    def broken(out):
        raise RuntimeError("Deliverable publication failed")

    task = SimpleNamespace(callback=broken)
    aimeat_crew._keep_verify_report_out("concierge", "t1", task)
    with pytest.raises(RuntimeError, match="Deliverable publication failed"):
        task.callback(SimpleNamespace(raw=MEASURED))


def test_the_reviewer_is_told_to_remove_not_mark():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "crewaimeat" / "aimeat_crew.py").read_text(encoding="utf-8")
    assert "remove or mark '[unverified]'" not in src
    assert "REMOVE anything not in the contributions" in src


# ── 3. writing the owner's workspace ────────────────────────────────────────────────────────

MANIFEST = {
    "objectTypes": [
        {"name": "contact", "namespace": "crm.contacts", "backing": "memory", "mode": "records"},
        {"name": "mailmessage", "namespace": "crm.mail", "backing": "rows"},
    ]
}


class WsNode:
    """The four workspace tools as the connector answers them; refuses what it is told to."""

    def __init__(self, *, refuse=(), objects=None, drafts=None):
        self.calls: list[tuple[str, dict]] = []
        self.refuse = set(refuse)
        self.objects = objects or {}
        self.drafts = drafts or {}

    def __call__(self, agent_name, tool, payload, **kw):
        self.calls.append((tool, payload))
        if tool in self.refuse:
            return {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "This needs organism:write."}}
        if tool == "aimeat_workspace_read":
            return {"manifest": MANIFEST, "objects": self.objects, "drafts": self.drafts}
        if tool == "aimeat_workspace_write":
            return {"written": "k", "id": payload.get("id") or "gen-1", "space": payload["space"], "mode": "records"}
        if tool == "aimeat_workspace_publish":
            return {"published": True}
        if tool == "aimeat_workspace_rows_append":
            return {"written": len(payload["rows"]), "row_ids": [r.get("row_id", "r") for r in payload["rows"]]}
        return None

    def sent(self, tool):
        return [p for t, p in self.calls if t == tool]


@pytest.fixture
def ws(monkeypatch):
    def install(**kw):
        n = WsNode(**kw)
        monkeypatch.setattr(workspace_tools, "_aimeat_call", n)
        return n

    return install


def test_a_new_record_is_written_and_published_under_the_space_s_namespace(ws):
    n = ws()
    done = workspace_tools.write_record("crm", "o", "w", "contact", {"name": "Testi Asiakas"})
    rid = done["id"]
    assert re.fullmatch(r"contact-[0-9a-f]{12}", rid), "the node makes no id for a record, so code does"
    assert done == {"id": rid, "space": "contact", "namespace": "crm.contacts", "created": True}
    [w] = n.sent("aimeat_workspace_write")
    assert w["space"] == "contact" and w["value"] == {"name": "Testi Asiakas"} and w["id"] == rid
    [p] = n.sent("aimeat_workspace_publish")
    assert p == {"organism_id": "o", "ws": "w", "namespace": "crm.contacts", "id": rid}


def test_an_update_is_merged_onto_the_record_as_it_stands(ws):
    published = {"id": "c1", "name": "Testi Asiakas", "email": "t@example.com", "_version": 3}
    draft = {
        "id": "c1",
        "name": "Testi Asiakas",
        "email": "t@example.com",
        "phone": "040",
        "title": "CEO",
        "_updatedAt": "x",
    }
    n = ws(objects={"contact": [published]}, drafts={"contact": [draft]})
    done = workspace_tools.write_record("crm", "o", "w", "contact", {"phone": "050", "email": None}, "c1")
    assert done["created"] is False
    [w] = n.sent("aimeat_workspace_write")
    assert w["id"] == "c1"
    assert w["value"] == {"id": "c1", "name": "Testi Asiakas", "phone": "050", "title": "CEO"}, (
        "the newer draft is the base, the node's _meta is not the record's, a None removes a field"
    )


def test_a_given_id_that_does_not_exist_yet_is_created_under_it(ws):
    n = ws()
    done = workspace_tools.write_record("crm", "o", "w", "contact", {"name": "X"}, "c-new")
    assert done["created"] is True and done["id"] == "c-new"
    assert n.sent("aimeat_workspace_write")[0]["id"] == "c-new"


def test_a_space_that_is_not_there_names_the_ones_that_are(ws):
    ws()
    with pytest.raises(workspace_tools.WorkspaceWriteError, match="no space 'deals'.*contact, mailmessage"):
        workspace_tools.write_record("crm", "o", "w", "deals", {"x": 1})


def test_a_row_space_is_not_written_as_a_record(ws):
    ws()
    with pytest.raises(workspace_tools.WorkspaceWriteError, match="row space"):
        workspace_tools.write_record("crm", "o", "w", "mailmessage", {"x": 1})


def test_a_refused_publish_says_the_draft_is_there_but_not_shown(ws):
    ws(refuse={"aimeat_workspace_publish"})
    with pytest.raises(workspace_tools.WorkspaceWriteError) as exc:
        workspace_tools.write_record("crm", "o", "w", "contact", {"name": "X"})
    msg = str(exc.value)
    assert "organism:write" in msg, "the node's own words"
    assert re.search(r"draft of contact-[0-9a-f]{12} was written but is not published", msg)


def test_a_refused_write_publishes_nothing(ws):
    n = ws(refuse={"aimeat_workspace_write"})
    with pytest.raises(workspace_tools.WorkspaceWriteError, match="refused the write"):
        workspace_tools.write_record("crm", "o", "w", "contact", {"name": "X"})
    assert n.sent("aimeat_workspace_publish") == []


def test_an_empty_record_is_not_sent(ws):
    n = ws()
    with pytest.raises(workspace_tools.WorkspaceWriteError):
        workspace_tools.write_record("crm", "o", "w", "contact", {})
    assert n.calls == []


def test_rows_are_appended_and_a_known_row_id_replaces_its_row(ws):
    n = ws()
    done = workspace_tools.append_rows(
        "crm", "o", "w", "mailmessage", [{"body": {"subject": "Hei"}, "row_id": "m-1", "extra": "dropped"}]
    )
    assert done["row_ids"] == ["m-1"]
    [a] = n.sent("aimeat_workspace_rows_append")
    assert a == {
        "organism_id": "o",
        "ws": "w",
        "space": "mailmessage",
        "rows": [{"body": {"subject": "Hei"}, "row_id": "m-1"}],
    }


@pytest.mark.parametrize("rows", [[], [{"row_id": "x"}], [{"body": "text"}]])
def test_a_row_without_a_body_object_is_not_sent(ws, rows):
    n = ws()
    with pytest.raises(workspace_tools.WorkspaceWriteError):
        workspace_tools.append_rows("crm", "o", "w", "mailmessage", rows)
    assert n.calls == []


def test_the_tools_answer_the_model_in_words_and_never_raise(ws):
    ws(refuse={"aimeat_workspace_publish"})
    tools = {t.name: t for t in workspace_tools.make_workspace_write_tools("crm")}
    assert set(tools) == {"list_workspaces", "read_workspace", "write_workspace_record", "append_workspace_rows"}
    out = tools["write_workspace_record"].run(organism_id="o", ws="w", space="contact", fields_json='{"name": "X"}')
    assert out.startswith("NOT WRITTEN.") and "organism:write" in out
    out = tools["write_workspace_record"].run(organism_id="o", ws="w", space="contact", fields_json="{not json")
    assert out.startswith("NOT WRITTEN.") and "not valid JSON" in out


def test_the_tools_say_what_they_did(ws):
    ws()
    tools = {t.name: t for t in workspace_tools.make_workspace_write_tools("crm")}
    out = tools["write_workspace_record"].run(organism_id="o", ws="w", space="contact", fields_json='{"name": "X"}')
    assert re.fullmatch(r"Created and published contact contact-[0-9a-f]{12} in workspace w\.", out)
    out = tools["append_workspace_rows"].run(
        organism_id="o", ws="w", space="mailmessage", rows_json='{"body": {"a": 1}}'
    )
    assert out.startswith("Wrote 1 row(s) to mailmessage")


def test_reading_and_writing_are_two_tool_ids_and_the_menu_lists_both():
    import crewaimeat.crew_invoke as ci
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY

    assert {"workspace", "workspace_write", "decline"} <= set(TOOL_REGISTRY) & set(TOOL_PURPOSES)
    assert "Read-only" in TOOL_PURPOSES["workspace"] and "organism:write" in TOOL_PURPOSES["workspace_write"]
    ok, menu = ci.handle("crew.menu", {}, agent_name="concierge")
    assert ok and {"workspace", "workspace_write", "decline"} <= {t["id"] for t in menu["tools"]}


def test_a_proposed_writer_asks_for_what_the_node_enforces():
    from crewaimeat.concierge_propose import PROPOSABLE_TOOLS, compute_scopes

    assert PROPOSABLE_TOOLS["workspace_write"] == ("organism:read", "memory:write", "organism:write")
    assert PROPOSABLE_TOOLS["workspace"] == ("organism:read",)
    scopes, left_out = compute_scopes(["workspace_write"], ["*"])
    assert {"organism:read", "memory:write", "organism:write"} <= set(scopes) and left_out == []


def test_a_proposed_writer_is_told_to_look_up_before_it_writes():
    from crewaimeat.concierge_propose import build_crew_def

    d = build_crew_def(
        name="crm-clerk",
        display_name="CRM clerk",
        purpose="Adds and updates contacts in CADENCE.",
        instructions="Add or update what you are asked.",
        tools=["workspace_write"],
        workspace={"name": "CADENCE", "organism_id": "o", "ws": "w"},
        delivers="",
    )
    desc = d["tasks"][0]["description"]
    assert "write_workspace_record" in desc and "look the record up first" in desc
    assert d["agents"][0]["tools"] == ["workspace_write", "decline"]


def test_the_build_wires_both_halves_when_a_verify_pass_runs():
    """_build is a closure inside run_crew and is reached only by a live daemon, so its wiring is read
    from the source: the publish strips the report, and the verify task carries the outermost link --
    both only when a verify pass was added."""
    import ast
    import inspect

    from crewaimeat import aimeat_crew

    tree = ast.parse(inspect.getsource(aimeat_crew.run_crew))
    build = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build")
    src = ast.unparse(build)
    assert "clean=_without_verify_report(spec.clean_deliverable) if verified else spec.clean_deliverable" in src
    guarded = [
        n
        for n in ast.walk(build)
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "verified"
        and "_keep_verify_report_out(spec.agent_name, tid, tasks[-1])" in ast.unparse(n)
    ]
    assert guarded, "the verify task's report link is attached, and only when a verify pass ran"
