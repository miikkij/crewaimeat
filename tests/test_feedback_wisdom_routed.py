"""feedback-wisdom: (1) the SHARED recipe-routing helpers now live in contract_adopt so any
contract agent can honor a routed organism; (2) the agent advertises capability tags/services so
AIMEAT's ecosystem picker recommends it. member_workspaces / _aimeat_call / adopt_contract are
monkeypatched — no live calls."""

from crewaimeat import contract_adopt as ca

CONTRACT = {"id": "c", "spaces": [{"space": "out-space", "namespace": "ns.out", "mode": "records"}]}


def _task(org):
    return {"scope": [{"name": "organism", "value": org, "type": "text"}], "description": "x"}


# ── routed_organism: read the routed org from the task (shared) ──────────────────
def test_routed_organism_from_scope_list():
    assert ca.routed_organism(_task("org-123")) == "org-123"


def test_routed_organism_organism_id_and_dict_scope():
    assert ca.routed_organism({"scope": {"organism_id": "org-9"}}) == "org-9"


def test_routed_organism_description_fallback():
    assert ca.routed_organism({"scope": [], "description": 'write into the "acme" organism. granted'}) == "acme"


def test_routed_organism_none_for_plain_task():
    assert ca.routed_organism({"scope": [], "description": "just run me"}) is None
    assert ca.routed_organism(None) is None


# ── ensure_routed_workspaces: filter to the org, adopt if undeclared (shared) ────
def test_ensure_adopts_when_undeclared(monkeypatch):
    monkeypatch.setattr(ca, "member_workspaces", lambda a: [("ORG", "w1"), ("OTHER", "w2")])
    monkeypatch.setattr(ca, "_aimeat_call", lambda a, t, p: {"manifest": {}, "objects": {}})
    adopted = []
    monkeypatch.setattr(ca, "adopt_contract", lambda agent, c, oid, wid: adopted.append((oid, wid)) or "ok")
    out = ca.ensure_routed_workspaces("AG", CONTRACT, _task("ORG"))
    assert out == [("ORG", "w1")]       # only the routed org, not OTHER
    assert adopted == [("ORG", "w1")]   # spaces provisioned because the workspace lacked them


def test_ensure_skips_adopt_when_already_declared(monkeypatch):
    monkeypatch.setattr(ca, "member_workspaces", lambda a: [("ORG", "w1")])
    monkeypatch.setattr(ca, "_aimeat_call", lambda a, t, p: {"manifest": {}, "objects": {"out-space": []}})
    adopted = []
    monkeypatch.setattr(ca, "adopt_contract", lambda *a: adopted.append(a))
    assert ca.ensure_routed_workspaces("AG", CONTRACT, _task("ORG")) == [("ORG", "w1")]
    assert adopted == []                # already declares the contract space → no re-adopt


def test_ensure_no_routed_org_never_lists_workspaces(monkeypatch):
    called = []
    monkeypatch.setattr(ca, "member_workspaces", lambda a: called.append(a) or [])
    assert ca.ensure_routed_workspaces("AG", CONTRACT, {"scope": [], "description": "run"}) == []
    assert called == []                 # no routed org → no network at all


def test_ensure_empty_when_no_accessible_workspace(monkeypatch):
    monkeypatch.setattr(ca, "member_workspaces", lambda a: [("OTHER", "w2")])  # routed org not visible
    monkeypatch.setattr(ca, "_aimeat_call", lambda a, t, p: None)
    assert ca.ensure_routed_workspaces("AG", CONTRACT, _task("ORG")) == []


# ── merge_targets: dedup, preserve order (shared) ────────────────────────────────
def test_merge_targets_dedups_preserving_order():
    a, b = [("o", "w1"), ("o", "w2")], [("o", "w2"), ("o2", "w3")]
    assert ca.merge_targets(a, b) == [("o", "w1"), ("o", "w2"), ("o2", "w3")]


# ── capability tags + the SPECIFIC capability report (what the picker's matcher reads) ───────
def test_crew_advertises_feedback_analysis_tag_and_versioned_domain_capabilities():
    import re
    from crews import feedback_wisdom_crew as crew
    assert "feedback-analysis" in crew.CAPABILITY_TAGS          # the manifest's primary match (a tag)
    for t in crew.CAPABILITY_TAGS:                              # tags must be charset-safe
        assert re.fullmatch(r"[a-z0-9._-]+", t), f"tag {t!r} carries chars AIMEAT rejects (: or @)"
    domain = crew.CAPABILITIES["domain"]                        # the @1 ids ride DOMAIN capabilities
    assert "consumes:feedback-stats@1" in domain and "produces:support-advisory@1" in domain


def test_crewspec_accepts_tags_and_capabilities():
    from crewaimeat.aimeat_crew import CrewSpec
    spec = CrewSpec(agent_name="x", build_domain=lambda ctx: ([], []),
                    tags=["feedback-analysis"], capabilities={"domain": ["feedback analysis"]})
    assert spec.tags == ["feedback-analysis"] and spec.capabilities["domain"] == ["feedback analysis"]
