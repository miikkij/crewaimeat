"""postman process_mail durable dedup — proves a mail already SENT from this machine is never
re-sent, even when the workspace record stays 'requested' (the 'Market scan re-sent on every fleet
start' bug). No SMTP, no network: send + the local marker are faked in memory."""

from crewaimeat import mail_contract as mc

OID, WID = "org", "ws"


def _stuck_requested_read(_tool, _payload):
    """A workspace that ALWAYS returns the same record as 'requested' — i.e. the 'done' write never
    sticks (cross-agent settle / frozen read). Write/publish calls just succeed."""
    if _tool == "aimeat_workspace_read":
        return {
            "manifest": {},
            "objects": {
                mc.IN_SPACE: [
                    {"id": "mail-scan-x", "subject": "Market scan · x", "body_md": "body", "status": "requested"}
                ]
            },
        }
    return {"ok": True}  # write / publish succeed


def _run(monkeypatch, marker_store):
    sends = []
    monkeypatch.setattr(mc, "_call", _stuck_requested_read)
    monkeypatch.setattr(mc, "send_mail", lambda *a, **k: sends.append(a) or None)  # None = success
    monkeypatch.setattr(mc, "last_local_run", lambda name, rid: marker_store.get(rid))
    monkeypatch.setattr(mc, "mark_local_run", lambda name, rid: marker_store.__setitem__(rid, "ts"))
    mc._PROCESSED.clear()
    res = mc.process_mail(targets=[(OID, WID)])
    return sends, res


def test_first_pass_sends_then_marker_blocks_resend(monkeypatch):
    store = {}
    # Pass 1: no marker → sends once, records the marker.
    sends1, res1 = _run(monkeypatch, store)
    assert len(sends1) == 1 and res1["sent"] == 1
    assert "mail-scan-x" in store

    # Pass 2: workspace STILL says 'requested' (done didn't stick), but the marker blocks the resend.
    sends2, res2 = _run(monkeypatch, store)
    assert sends2 == [] and res2["sent"] == 0


def test_already_marked_never_sends(monkeypatch):
    store = {"mail-scan-x": "ts"}  # pretend a previous machine-run already delivered it
    sends, res = _run(monkeypatch, store)
    assert sends == [] and res["sent"] == 0
