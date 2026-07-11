"""vision.py — attachment analysis routing (download -> vision/text). No network: storage + HTTP mocked."""

from __future__ import annotations

from crewaimeat import vision


def test_extract_text_decodes_text_file():
    out = vision.extract_document_text(b"hello\nworld", "text/plain", "notes.txt")
    assert "hello" in out and "world" in out


def test_extract_text_truncates_long_text():
    big = ("x" * 20000).encode()
    out = vision.extract_document_text(big, "text/plain", "big.txt")
    assert "truncated" in out and len(out) < 20000


def test_extract_text_unknown_type():
    out = vision.extract_document_text(b"\x00\x01", "application/octet-stream", "blob.bin")
    assert "no reader" in out.lower()


def test_extract_pdf_no_text_layer():
    # A tiny non-PDF passed as pdf -> pypdf fails -> we report it, never raise.
    out = vision.extract_document_text(b"not really a pdf", "application/pdf", "x.pdf")
    assert out.startswith("(") and "pdf" in out.lower()


def test_analyze_image_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    out = vision.analyze_image(b"\x89PNG", "image/png")
    assert "OPENROUTER_API_KEY" in out


def test_analyze_image_posts_data_uri(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "a red fox in snow. Text: 'SALE'."}}]}

        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return FakeResp()

    monkeypatch.setattr(vision.requests, "post", fake_post)
    out = vision.analyze_image(b"\x89PNG\r\n", "image/png")
    assert "red fox" in out
    content = captured["body"]["messages"][0]["content"]
    img = next(p for p in content if p["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_analyze_attachment_routes_image_to_vision(monkeypatch):
    monkeypatch.setattr(vision.storage, "fetch_bytes", lambda agent, key: (b"\x89PNG", "image/png"))
    monkeypatch.setattr(vision, "analyze_image", lambda data, mime, prompt=None, agent=None: "VISION-READ")
    out = vision.analyze_attachment("concierge", {"storageKey": "dm/u/pic.png", "name": "pic.png", "mime": "image/png"})
    assert "VISION-READ" in out and "pic.png" in out


def test_analyze_attachment_routes_pdf_to_text(monkeypatch):
    monkeypatch.setattr(vision.storage, "fetch_bytes", lambda agent, key: (b"%PDF", "application/pdf"))
    monkeypatch.setattr(vision, "extract_document_text", lambda data, mime, name: "DOC-TEXT")
    out = vision.analyze_attachment(
        "concierge", {"storageKey": "dm/u/f.pdf", "name": "f.pdf", "mime": "application/pdf"}
    )
    assert "DOC-TEXT" in out and "f.pdf" in out


def test_analyze_attachment_download_fail(monkeypatch):
    monkeypatch.setattr(vision.storage, "fetch_bytes", lambda agent, key: None)
    out = vision.analyze_attachment("concierge", {"storageKey": "dm/u/x.png", "name": "x.png"})
    assert "could not download" in out.lower()


def test_analyze_attachment_no_key():
    out = vision.analyze_attachment("concierge", {"name": "x.png"})
    assert "no storage key" in out.lower()
