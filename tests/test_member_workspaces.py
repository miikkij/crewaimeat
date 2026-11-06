"""member_workspaces: the (organism, workspace) pairs a contract agent serves, and what it leaves out.

The node answers aimeat_workspace_list with each workspace's `archived` flag (measured on aimeat.io
2026-11-06: `{id, name, access, archived, created_at, created_by}`). An archived workspace has no
manifest, so the node refuses its read and its record subscription.
"""

from __future__ import annotations

from crewaimeat import aimeat_crew


def _node(monkeypatch, orgs: dict[str, list[dict]]):
    def call(agent, tool, payload, **_kw):
        if tool == "aimeat_organism_list":
            return {"organisms": [{"id": oid} for oid in orgs]}
        if tool == "aimeat_workspace_list":
            return {"workspaces": orgs.get(payload["organism_id"], [])}
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(aimeat_crew, "_aimeat_call", call)
    monkeypatch.delenv("AIMEAT_CONTRACT_ORGS", raising=False)


def test_an_archived_workspace_is_not_served(monkeypatch):
    _node(
        monkeypatch,
        {
            "org-a": [
                {"id": "ws-old", "name": "Postin jalostus", "access": "owner", "archived": True},
                {"id": "ws-new", "name": "Postin jalostus", "access": "owner", "archived": False},
            ],
            "org-b": [{"id": "ws-b", "name": "Materiaalit"}],  # a node that sends no flag
        },
    )
    assert aimeat_crew.member_workspaces("image-maker") == [("org-a", "ws-new"), ("org-b", "ws-b")]


def test_the_subscription_list_leaves_archived_workspaces_out(monkeypatch):
    _node(monkeypatch, {"org-a": [{"id": "ws-old", "archived": True}, {"id": "ws-new", "archived": False}]})
    contract = {"spaces": [{"mode": "records", "namespace": "shared.image_requests"}]}
    assert aimeat_crew.contract_record_spaces("image-maker", contract) == [
        {"organism_id": "org-a", "ws": "ws-new", "space": "shared.image_requests"}
    ]
