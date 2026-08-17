"""The wake-spin sweeper: never eats a fresh push, always drains a real spin to empty.

No sockets. Everything the module touches goes through `_code`, so the daemon is one stub.
"""

import pytest

from crewaimeat import wake_spin
from crewaimeat.wake_spin import MIN_STUCK_SECONDS, SpinSweeper


@pytest.fixture
def daemon(monkeypatch):
    """A fake daemon. `queues[agent]` is how many elements are waiting; wake reports 200 while any
    remain, and each tasks/next consumes one. Returns the dict so a test can set the depth."""
    queues: dict[str, int] = {}
    calls = {"wake": 0, "take": 0}
    touched: list[str] = []  # which agents had tasks/next called on them at all

    def _code(url: str):
        agent = url.split("agent=")[1]
        if "/local/wake/next" in url:
            calls["wake"] += 1
            return 200 if queues.get(agent, 0) > 0 else 204
        calls["take"] += 1
        touched.append(agent)
        if queues.get(agent, 0) > 0:
            queues[agent] -= 1
            return 200
        return 204

    monkeypatch.setattr(wake_spin, "_code", _code)
    return queues, calls, touched


# ── rule 1: a fresh wake is a real push until it has been hot long enough ──
def test_a_fresh_spin_is_left_alone(daemon):
    """Eating a push before the daemon reads it makes the task wait for the ~5-minute safety net."""
    queues, calls, touched = daemon
    queues["news-fetcher"] = 1
    assert SpinSweeper().sweep(1234, ["news-fetcher"], now=1000.0) == []
    assert calls["take"] == 0, "nothing may be consumed on first sight"
    assert queues["news-fetcher"] == 1


def test_it_is_drained_once_it_has_been_hot_long_enough(daemon):
    queues, _c, _t = daemon
    queues["news-fetcher"] = 1
    s = SpinSweeper()
    s.sweep(1234, ["news-fetcher"], now=1000.0)  # first sight: starts the clock
    assert s.sweep(1234, ["news-fetcher"], now=1000.0 + MIN_STUCK_SECONDS) == [("news-fetcher", 1)]
    assert queues["news-fetcher"] == 0


def test_the_clock_restarts_when_an_agent_settles_on_its_own(daemon):
    """A queue that clears itself must not carry credit toward a later, unrelated push."""
    queues, calls, touched = daemon
    queues["postman"] = 1
    s = SpinSweeper()
    s.sweep(1234, ["postman"], now=1000.0)
    queues["postman"] = 0
    s.sweep(1234, ["postman"], now=1030.0)  # settled -> forgotten
    queues["postman"] = 1
    assert s.sweep(1234, ["postman"], now=1060.0) == [], "the new push is fresh, not 60s old"
    assert calls["take"] == 0


# ── rule 2: drain to empty, not once ──
def test_a_queue_of_repeats_is_drained_to_204(daemon):
    """One element taken from a queue of five spins just as hard as five."""
    queues, _c, _t = daemon
    queues["crypto-weekly-reporter"] = 5
    s = SpinSweeper()
    s.sweep(1234, ["crypto-weekly-reporter"], now=0.0)
    assert s.sweep(1234, ["crypto-weekly-reporter"], now=MIN_STUCK_SECONDS) == [("crypto-weekly-reporter", 5)]
    assert queues["crypto-weekly-reporter"] == 0


def test_draining_is_bounded_so_one_agent_cannot_hold_the_watchdog(daemon):
    queues, _c, _t = daemon
    queues["runaway"] = 10_000
    assert wake_spin.drain(1234, "runaway", max_items=7) == 7


# ── the rest of the fleet is never punished for one agent ──
def test_a_clean_agent_is_probed_and_left_alone(daemon):
    queues, calls, touched = daemon
    queues.update({"stuck": 1, "clean": 0})
    s = SpinSweeper()
    s.sweep(1234, ["stuck", "clean"], now=0.0)
    assert s.sweep(1234, ["stuck", "clean"], now=MIN_STUCK_SECONDS) == [("stuck", 1)]
    # `drain` reads until 204, so the stuck agent sees one 200 and one confirming 204. What matters
    # is that the CLEAN agent's queue was never consumed from at all.
    assert set(touched) == {"stuck"}
    assert touched.count("stuck") == 2


def test_a_daemon_that_does_not_answer_is_not_an_error(monkeypatch):
    """It restarts often; the sweeper must skip the pass rather than kill the supervisor."""
    monkeypatch.setattr(wake_spin, "_code", lambda _u: None)
    assert SpinSweeper().sweep(1234, ["postman"], now=0.0) == []


def test_agents_of_reads_a_serve_json(tmp_path):
    p = tmp_path / "serve.json"
    p.write_text('{"port": 62894, "agents": [{"agent": "postman"}, {"agent": "joker"}, {}]}', encoding="utf-8")
    assert wake_spin.agents_of(p) == (62894, ["postman", "joker"])


def test_a_missing_or_half_written_serve_json_is_not_an_error(tmp_path):
    """The file is rewritten while the daemon starts, so a partial read is normal, not a failure."""
    assert wake_spin.agents_of(tmp_path / "nope.json") == (None, [])
    half = tmp_path / "serve.json"
    half.write_text('{"port": 1, "agents": [{"age', encoding="utf-8")
    assert wake_spin.agents_of(half) == (None, [])
