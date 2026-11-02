"""A run the node refused ends as REFUSED, not completed.

On a sold seat (2026-09-29) a crew ran to exit 0 while the node refused every write with SCOPE_DENIED,
and the customer's task stayed queued with nothing on screen saying why. The node keeps every refusal
(GET /v1/agents/{name}/refusals?since=), and aimeat-crewai 0.31.0 asks it after the kickoff -- but this
scaffold completes the task INSIDE the kickoff, and on the deterministic paths inside the builder, so
by then the task was already done. These tests hold the places where that is decided:

  complete_callback asks FIRST, and fails a refused run instead of completing it -- and records the
    refusal even when its own /fail is refused too, because then the exit code is all that is left;
  the node read tells "no route yet" (404, reads as none) from "could not ask" (completes, and says so);
  the window opens before the builder, and a spawn worker's first task inherits the worker's start so
    the start-up's refused identity push -- the actual sold-seat write -- counts against it;
  run_once exits 3 for a refused run, and a token rejection keeps its own 2;
  a call the node answered SCOPE_DENIED counts even when the node's list leaves it out (crew77,
    2026-11-02: the agent's record held the word, its token did not, and the task went green).

No node, no network: the node read and the tool calls are replaced at the seam they are called through.
"""

from __future__ import annotations

import pytest

from crewaimeat import lifecycle
from crewaimeat.lifecycle import LifecycleCallbacks

REFUSAL = {
    "needed": ["agent:write"],
    "any_of": False,
    "call": "PATCH /v1/agents/:name/tags",
    "count": 2,
    "first_at": "2026-09-30T05:00:00.000Z",
    "last_at": "2026-09-30T05:00:01.000Z",
}


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The refused set and the worker start are process-wide; every test starts from nothing."""
    lifecycle._REFUSED.clear()
    lifecycle._CALLS_REFUSED.clear()
    lifecycle._WORKER_RUN_START["at"] = None
    yield
    lifecycle._REFUSED.clear()
    lifecycle._CALLS_REFUSED.clear()
    lifecycle._WORKER_RUN_START["at"] = None


class _Recorder:
    """Stands in for `_aimeat_call`: records every tool call and answers what it is told to."""

    def __init__(self, answers: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    def __call__(self, agent_name, tool, payload, **_kw):
        self.calls.append((tool, payload))
        return self.answers.get(tool, {"ok": True})

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


def _callbacks(call: _Recorder, refusals, todos: list | None = None) -> LifecycleCallbacks:
    marked = todos if todos is not None else []
    return LifecycleCallbacks(
        call=call,
        eval_ctx=lambda _info: None,
        mark_todos_done=lambda agent, tid: marked.append(tid),
        deliverable_keys={},
        refusals=refusals,
    )


# ── complete_callback asks the node first ────────────────────────────────────────────────────


def test_a_refused_run_is_failed_and_not_completed():
    call = _Recorder()
    todos: list = []
    cb = _callbacks(call, lambda agent, since: [REFUSAL], todos).complete_callback(
        "concierge", "task-1", since="2026-09-30T04:59:55.000Z"
    )
    cb(None)
    assert call.tools() == ["aimeat_task_fail"], "a refused run must not also be completed"
    assert todos == [], "the plan's todos stay open on a run that did not do its job"
    message = call.calls[0][1]["message"]
    assert "PATCH /v1/agents/:name/tags" in message, "name the call"
    assert "agent:write" in message, "name the permission"
    assert "Profile > Agents > Manage access rights" in message, "and where the owner gives it"


def test_the_refusal_is_recorded_even_when_the_fail_itself_is_refused():
    # An agent refused task:write cannot fail its own task either. Then the exit code is the only thing
    # left that says the run was refused, so the record must not depend on the /fail landing.
    call = _Recorder({"aimeat_task_fail": None})
    cb = _callbacks(call, lambda agent, since: [REFUSAL]).complete_callback("concierge", "task-1", since="x")
    cb(None)
    assert "task-1" in lifecycle.refused_runs()
    assert "aimeat_task_complete" not in call.tools()


def test_a_run_with_no_refusals_completes_as_before():
    call = _Recorder()
    todos: list = []
    cb = _callbacks(call, lambda agent, since: [], todos).complete_callback(
        "concierge", "task-1", mem_key="crews.concierge.out", since="x"
    )
    cb(None)
    assert call.tools() == ["aimeat_task_complete"]
    assert todos == ["task-1"]
    assert call.calls[0][1]["deliverable_key"] == "crews.concierge.out"
    assert lifecycle.refused_runs() == {}


def test_when_the_node_cannot_be_asked_the_task_completes_and_says_so():
    # Failing a good run because the CHECK failed would trade one silent lie for another. It completes,
    # and the task's own message says the check did not happen.
    call = _Recorder()
    cb = _callbacks(call, lambda agent, since: None).complete_callback("concierge", "task-1", since="x")
    cb(None)
    assert call.tools() == ["aimeat_task_complete"]
    assert "could not be asked" in call.calls[0][1]["message"]


def test_a_check_that_raises_never_breaks_the_finalize():
    def boom(agent, since):
        raise RuntimeError("tunnel dropped")

    call = _Recorder()
    _callbacks(call, boom).complete_callback("concierge", "task-1", since="x")(None)
    assert call.tools() == ["aimeat_task_complete"]


def test_without_a_run_start_nothing_is_asked():
    asked: list = []
    call = _Recorder()
    _callbacks(call, lambda agent, since: asked.append(since) or [REFUSAL]).complete_callback("concierge", "task-1")(
        None
    )
    assert asked == [], "no window, no question"
    assert call.tools() == ["aimeat_task_complete"]


def test_the_refusal_comes_before_the_verify_gate():
    # A write that never landed is the more fundamental reason; a verify verdict read on top of it would
    # be about the wrong thing. So the refusal answers first, and the gate is never consulted.
    call = _Recorder()
    cb = _callbacks(call, lambda agent, since: [REFUSAL]).complete_callback(
        "builder", "task-9", require_verify=True, since="x"
    )
    cb(None)
    assert call.tools() == ["aimeat_task_fail"]
    assert "missing permission" in call.calls[0][1]["message"]


def test_the_since_the_callback_was_given_is_the_one_it_asks_with():
    seen: list = []
    call = _Recorder()
    _callbacks(call, lambda agent, since: seen.append((agent, since)) or []).complete_callback(
        "concierge", "task-1", since="2026-09-30T04:59:55.000Z"
    )(None)
    assert seen == [("concierge", "2026-09-30T04:59:55.000Z")]


# ── the summary sentence ─────────────────────────────────────────────────────────────────────


def test_the_summary_says_or_for_any_of_and_and_for_all_of():
    both = lifecycle.refusal_summary([{"needed": ["a:x", "b:y"], "any_of": False, "call": "GET /1"}])
    either = lifecycle.refusal_summary([{"needed": ["a:x", "b:y"], "any_of": True, "call": "GET /2"}])
    assert "a:x and b:y" in both
    assert "a:x or b:y" in either


def test_the_summary_names_five_and_counts_the_rest():
    many = [{"needed": [f"s:{i}"], "call": f"GET /{i}"} for i in range(8)]
    text = lifecycle.refusal_summary(many)
    assert "GET /4" in text and "GET /5" not in text
    assert "(and 3 more)" in text


# ── the window ───────────────────────────────────────────────────────────────────────────────


def test_the_run_start_is_iso_z_and_opens_a_few_seconds_early():
    # The node compares ISO strings, so the form matters; the margin keeps a node whose clock is a few
    # seconds behind from hiding the run's own refusals (the same margin aimeat-crewai 0.31.0 uses).
    iso = lifecycle.run_started_iso(now=1_000_000.0)
    assert iso == "1970-01-12T13:46:35.000Z"
    assert lifecycle.RUN_CLOCK_MARGIN_S == 5.0


def test_a_spawn_workers_first_task_inherits_the_workers_start():
    lifecycle.set_worker_run_start("2026-09-30T05:00:00.000Z")
    assert lifecycle.take_run_since() == "2026-09-30T05:00:00.000Z", (
        "the start-up's refused identity push belongs to the run it was made for"
    )
    later = lifecycle.take_run_since()
    assert later != "2026-09-30T05:00:00.000Z", "every later task takes its own window"


def test_a_continuous_daemon_task_takes_its_own_start():
    assert lifecycle._WORKER_RUN_START["at"] is None
    assert lifecycle.take_run_since().endswith("Z")


# ── the node read ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def rest(monkeypatch):
    """Replace the REST transport and remember what it was asked."""
    from crewaimeat import aimeat_crew

    seen: dict = {}

    def install(answer):
        def fake(agent_name, method, path, body=None, **kw):
            seen.update(agent=agent_name, method=method, path=path, kw=kw)
            return answer

        monkeypatch.setattr(aimeat_crew, "_aimeat_rest", fake)
        return seen

    return install


def test_the_node_read_returns_the_refusals(rest):
    from crewaimeat.aimeat_crew import _run_refusals

    seen = rest({"agent": "g", "granted_scopes": ["memory:read"], "refusals": [REFUSAL, "junk"]})
    got = _run_refusals("concierge#owner@node", "2026-09-30T05:00:00.000Z")
    assert got == [REFUSAL], "only well-formed entries"
    assert seen["method"] == "GET"
    assert seen["path"].startswith("/v1/agents/concierge/refusals?since="), "the bare NAME goes in the path"
    assert "2026-09-30T05%3A00%3A00.000Z" in seen["path"], "the time is encoded"
    assert seen["kw"].get("return_error") is True, "a 404 must be told apart from a dropped tunnel"


def test_a_node_without_the_route_means_no_refusals_known(rest):
    from crewaimeat.aimeat_crew import _run_refusals

    rest({"ok": False, "error": {"code": "NOT_FOUND"}, "http_status": 404})
    assert _run_refusals("concierge", "x") == []


def test_a_node_that_could_not_be_asked_is_unknown_not_empty(rest):
    from crewaimeat.aimeat_crew import _run_refusals

    rest(None)
    assert _run_refusals("concierge", "x") is None


def test_another_refusal_of_the_read_itself_is_unknown(rest):
    from crewaimeat.aimeat_crew import _run_refusals

    rest({"ok": False, "error": {"code": "ACCESS_DENIED"}, "http_status": 403})
    assert _run_refusals("concierge", "x") is None


def test_a_body_without_the_list_is_unknown(rest):
    from crewaimeat.aimeat_crew import _run_refusals

    rest({"agent": "g"})
    assert _run_refusals("concierge", "x") is None


def test_the_scaffold_wires_the_node_read_into_its_callbacks():
    from crewaimeat import aimeat_crew

    assert aimeat_crew._lifecycle_callbacks().refusals is aimeat_crew._run_refusals


# ── what the package found after the kickoff ─────────────────────────────────────────────────


def test_the_packages_refusal_is_recorded_for_the_exit_code():
    from aimeat_crewai.daemon import NodeRefusedDuringRun

    from crewaimeat.aimeat_crew import _on_daemon_error

    _on_daemon_error(NodeRefusedDuringRun("AIMEAT refused calls of this run: ..."))
    assert list(lifecycle.refused_runs().values()) == ["AIMEAT refused calls of this run: ..."]


def test_an_ordinary_crash_is_not_a_refusal():
    from crewaimeat.aimeat_crew import _on_daemon_error

    _on_daemon_error(RuntimeError("the model returned nothing"))
    assert lifecycle.refused_runs() == {}


# ── run_once's exit code ─────────────────────────────────────────────────────────────────────


def test_a_refused_run_exits_3(capsys):
    from crewaimeat.run_once import EXIT_REFUSED, _refused_exit

    lifecycle.note_refused("task-1", "AIMEAT refused calls of this run: GET /x needs a:b.")
    assert EXIT_REFUSED == 3
    assert _refused_exit("concierge", 0) == 3
    assert "REFUSED (task-1)" in capsys.readouterr().err, "the reason stands beside the exit code"


def test_a_crash_in_a_refused_run_is_reported_as_refused():
    from crewaimeat.run_once import _refused_exit

    lifecycle.note_refused("task-1", "refused")
    assert _refused_exit("concierge", 1) == 3, "the refusal has to be fixed first; a re-run meets it again"


def test_a_token_rejection_keeps_its_own_code():
    from crewaimeat.run_once import _refused_exit

    lifecycle.note_refused("task-1", "refused")
    assert _refused_exit("concierge", 2) == 2, "re-approval comes before any permission can matter"


def test_a_clean_run_keeps_its_code():
    from crewaimeat.run_once import _refused_exit

    assert _refused_exit("concierge", 0) == 0
    assert _refused_exit("concierge", 1) == 1


# ── the run's own count: what the node's list leaves out ─────────────────────────────────────

# The node's words, as auth/middleware.ts writes them; crew77's call_app_tool met this one on 2026-11-02.
INVOKE_DENIED = {
    "code": "SCOPE_DENIED",
    "message": 'Scope "exchange:invoke" required. Agent scopes: [memory:read, ai:use]',
}


def test_a_refused_call_the_nodes_list_leaves_out_fails_the_run():
    since = lifecycle.run_started_iso()
    lifecycle.note_call_refused("crew77", "POST /v1/apps/o/a/webmcp/tools/t", INVOKE_DENIED)
    call = _Recorder()

    def refusals(agent, s):
        return lifecycle.refusals_of_this_run([], agent, s)

    _callbacks(call, refusals).complete_callback("crew77", "task-1", since=since)(None)
    assert call.tools() == ["aimeat_task_fail"]
    message = call.calls[0][1]["message"]
    assert "POST /v1/apps/o/a/webmcp/tools/t" in message and "exchange:invoke" in message
    assert "ai:use" not in message, "what the agent HOLDS is not what it needs"
    assert "issued without it" in message, "the owner's page may show the word ticked; say what that means"
    assert "task-1" in lifecycle.refused_runs()


def test_a_call_refused_before_this_run_does_not_count():
    lifecycle.note_call_refused("crew77", "POST /v1/x", INVOKE_DENIED)
    assert lifecycle.refusals_of_this_run([], "crew77", "2999-01-01T00:00:00.000Z") == []


def test_another_agents_refusal_does_not_count():
    since = lifecycle.run_started_iso()
    lifecycle.note_call_refused("other", "POST /v1/x", INVOKE_DENIED)
    assert lifecycle.refusals_of_this_run([], "crew77", since) == []


def test_two_owners_agents_of_one_name_stay_apart_and_a_bare_name_is_the_same_agent():
    since = lifecycle.run_started_iso()
    lifecycle.note_call_refused("a#one@node", "POST /v1/x", INVOKE_DENIED)
    assert lifecycle.refusals_of_this_run([], "a#two@node", since) == []
    assert lifecycle.refusals_of_this_run([], "a", since)
    assert lifecycle.refusals_of_this_run([], "a#one@node", since)


def test_a_permission_the_node_names_is_not_listed_twice():
    since = lifecycle.run_started_iso()
    node_says = [dict(REFUSAL, needed=["exchange:invoke"], call="POST /v1/apps/:owner/:file/webmcp/tools/:tool")]
    lifecycle.note_call_refused("crew77", "POST /v1/apps/o/a/webmcp/tools/t", INVOKE_DENIED)
    assert lifecycle.refusals_of_this_run(node_says, "crew77", since) == node_says


def test_a_node_that_cannot_be_asked_does_not_hide_what_the_run_saw():
    since = lifecycle.run_started_iso()
    assert lifecycle.refusals_of_this_run(None, "crew77", since) is None, "nothing seen: still unknown"
    lifecycle.note_call_refused("crew77", "POST /v1/x", INVOKE_DENIED)
    assert [r["call"] for r in lifecycle.refusals_of_this_run(None, "crew77", since)] == ["POST /v1/x"]


@pytest.mark.parametrize(
    "error",
    [
        None,
        "tunnel not connected",
        {"code": "NOT_FOUND", "message": "no such key"},
        {"code": "NOT_GRANTED", "message": "the owner has not granted this server"},
    ],
)
def test_only_the_nodes_scope_refusal_counts(error):
    lifecycle.note_call_refused("crew77", "aimeat_memory_read", error)
    assert lifecycle._CALLS_REFUSED == {}


@pytest.mark.parametrize(
    ("message", "needed", "any_of"),
    [
        ('Scope "memory:write" required. Agent scopes: [memory:read, ai:use]', ["memory:write"], False),
        (
            'Scope "memory:write-as-owner" required to write into the owner\'s namespace.',
            ["memory:write-as-owner"],
            False,
        ),
        ("One of these scopes is required: a:x, b:y. Agent scopes: [memory:read]", ["a:x", "b:y"], True),
        ("This needs ai:use.", ["ai:use"], False),
        ("Forbidden.", [], False),
    ],
)
def test_the_needed_words_come_from_the_nodes_message(message, needed, any_of):
    since = lifecycle.run_started_iso()
    lifecycle.note_call_refused("a", "c", {"code": "SCOPE_DENIED", "message": message})
    got = lifecycle.refusals_of_this_run([], "a", since)
    assert got[0]["needed"] == needed and got[0]["any_of"] is any_of


class _Answer:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body
        self.headers: dict = {}
        self.text = str(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, answer: _Answer):
        self.answer = answer

    def post(self, *_a, **_k):
        return self.answer

    def request(self, *_a, **_k):
        return self.answer


def _noop(*_a, **_k):
    return None


def _transport(answer: _Answer):
    from crewaimeat.transport import NodeTransport

    return NodeTransport(
        serve_api=lambda: ("http://127.0.0.1:1", _Session(answer)),
        reset=_noop,
        identity_guard=lambda *_a: True,
        read_token=None,
        subprocess_call=_noop,
        transient_error=lambda _e: False,
        warn_provenance=_noop,
        note_refusal=lifecycle.note_call_refused,
    )


DENIED_403 = {"ok": False, "error": INVOKE_DENIED}


def _calls_seen() -> list[str]:
    return [call for (_who, call, _needed) in lifecycle._CALLS_REFUSED]


def test_all_three_doors_of_the_transport_report_a_refusal():
    tx = _transport(_Answer(403, DENIED_403))
    assert tx.call("a", "aimeat_memory_write", {}, return_error=True)["ok"] is False
    assert tx.rest("a", "POST", "/v1/apps/o/a/webmcp/tools/t?x=1", {}) is None
    assert tx.request("a", "POST", "/v1/storage?y=2").status_code == 403
    assert _calls_seen() == ["aimeat_memory_write", "POST /v1/apps/o/a/webmcp/tools/t", "POST /v1/storage"]


def test_a_probe_that_expects_a_refusal_does_not_count():
    _transport(_Answer(403, DENIED_403)).call("a", "aimeat_organism_get", {}, quiet=True)
    assert _calls_seen() == []


def test_a_streamed_body_is_left_for_its_caller():
    class Unreadable(_Answer):
        def json(self):
            raise AssertionError("a streamed body must not be read here")

    _transport(Unreadable(403, {})).request("a", "GET", "/v1/storage/k", stream=True)
    assert _calls_seen() == []


def test_the_scaffolds_transport_reports_to_the_lifecycle():
    from crewaimeat.aimeat_crew import _node_transport

    assert _node_transport().note_refusal is lifecycle.note_call_refused
