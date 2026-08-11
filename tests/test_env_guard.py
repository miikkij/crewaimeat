"""The .env loader + shadowing tripwire.

Both halves earned their tests the expensive way (2026-08-09/10): nothing on the fleet path ever
loaded `.env`, and when the environment also defined a key it won silently. A stale
OPENROUTER_API_KEY inherited by every VS Code terminal 401'd every LLM call for two days, and the
only visible symptom was an evening edition with two red steps.

Deterministic: no network, no real .env, no LLM.
"""

import pytest

from crewaimeat import env_guard


@pytest.fixture(autouse=True)
def _fresh():
    """load_env() is idempotent by design, so each test must start from a clean slate."""
    env_guard._done = False
    yield
    env_guard._done = False


def write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


# ── the loading half: .env must actually reach the process ───────────────────
def test_env_file_is_applied_when_the_environment_is_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    p = write_env(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-fromfile-0000000000000001\n")
    assert env_guard.load_env(p) == []
    import os

    assert os.environ["OPENROUTER_API_KEY"].endswith("0001")


def test_parses_export_quotes_and_comments(tmp_path):
    p = write_env(
        tmp_path,
        "# a comment\n\nexport ALPHA=one\nBETA=\"two\"\nGAMMA='three'\nNOT_A_PAIR\n",
    )
    got = env_guard._parse_env_file(p)
    assert got == {"ALPHA": "one", "BETA": "two", "GAMMA": "three"}


def test_a_missing_env_file_is_reported_not_fatal(tmp_path, capsys):
    assert env_guard.load_env(tmp_path / "nope.env") == []
    assert "no .env" in capsys.readouterr().err


# ── the tripwire half: the environment may win, but never in silence ─────────
def test_a_shadowed_key_is_shouted_about_with_both_fingerprints(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-26badeb95STALEKEYVALUE00000b183")
    p = write_env(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-578baGOODKEYVALUE0000002031\n")

    assert env_guard.load_env(p) == ["OPENROUTER_API_KEY"]

    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY DIFFERS" in err
    assert "ENVIRONMENT WINS" in err
    assert "sk-or-v1-2" in err and "sk-or-v1-5" in err  # both fingerprints, so they can be told apart
    assert "b183" in err and "2031" in err
    assert "STALEKEYVALUE" not in err and "GOODKEYVALUE" not in err  # never the whole secret


def test_the_environment_keeps_precedence(tmp_path, monkeypatch):
    """Reporting it is the change; the contract is not. Scripts and CI rely on env-wins."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-envwins-000000000000000000001")
    p = write_env(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-filevalue-00000000000000002\n")
    env_guard.load_env(p)
    import os

    assert os.environ["OPENROUTER_API_KEY"].endswith("0001")


def test_an_identical_value_is_not_a_warning(tmp_path, monkeypatch, capsys):
    same = "sk-or-v1-identical-0000000000000000001"
    monkeypatch.setenv("OPENROUTER_API_KEY", same)
    p = write_env(tmp_path, f"OPENROUTER_API_KEY={same}\n")
    assert env_guard.load_env(p) == []
    assert "DIFFERS" not in capsys.readouterr().err


def test_only_the_differing_variable_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-stale-00000000000000000000001")
    monkeypatch.setenv("AIMEAT_OWNER", "happydude500001")
    p = write_env(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-good-000000000000000000002\nAIMEAT_OWNER=happydude500001\n")
    assert env_guard.load_env(p) == ["OPENROUTER_API_KEY"]
    assert "AIMEAT_OWNER DIFFERS" not in capsys.readouterr().err


# ── fingerprinting: enough to tell two keys apart, useless if stolen ─────────
def test_secret_values_are_never_printed_whole():
    fp = env_guard.fingerprint("OPENROUTER_API_KEY", "sk-or-v1-" + "z" * 60 + "TAIL")
    assert "zzzzzzzzzz" not in fp and fp.endswith("TAIL") and "len=73" in fp


def test_non_secret_values_are_shown_as_is():
    assert env_guard.fingerprint("AIMEAT_OWNER", "happydude500001") == "happydude500001"


def test_a_short_secret_is_not_fingerprinted_into_the_clear():
    assert "abc" not in env_guard.fingerprint("SOME_TOKEN", "abc123")


def test_load_env_runs_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-stale-00000000000000000000001")
    p = write_env(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-good-000000000000000000002\n")
    assert env_guard.load_env(p) == ["OPENROUTER_API_KEY"]
    capsys.readouterr()
    assert env_guard.load_env(p) == []  # second call is a no-op — the host and a crew both call it
    assert capsys.readouterr().err == ""
