"""fleet TUI i18n — lookup, fallback, language cycling, env default."""

from crewaimeat.tui import i18n


def test_t_returns_lang_and_falls_back():
    assert i18n.t("sb.running", "en") == "running"
    assert i18n.t("sb.running", "fi") == "ajossa"
    assert i18n.t("sb.running", "de") == "running"  # unknown lang -> en
    assert i18n.t("no.such.key", "fi") == "no.such.key"  # unknown key -> the key itself


def test_next_lang_cycles():
    assert i18n.next_lang("en") == "fi"
    assert i18n.next_lang("fi") == "en"
    assert i18n.next_lang("xx") == "fi"  # unknown -> start of cycle, then next


def test_default_lang_from_env(monkeypatch):
    monkeypatch.setenv("AIMEAT_TUI_LANG", "fi")
    assert i18n.default_lang() == "fi"
    monkeypatch.setenv("AIMEAT_TUI_LANG", "xx")
    assert i18n.default_lang() == "en"
    monkeypatch.delenv("AIMEAT_TUI_LANG", raising=False)
    assert i18n.default_lang() == "en"


def test_every_string_has_english_and_finnish():
    for key, entry in i18n.STRINGS.items():
        assert "en" in entry and entry["en"], f"{key} missing en"
        assert "fi" in entry and entry["fi"], f"{key} missing fi"
