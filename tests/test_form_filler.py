"""form_filler.py — AcroForm extraction + fill. Pure pypdf, no network."""

from __future__ import annotations

from crewaimeat import form_filler


def test_extract_fields_empty_for_non_pdf():
    assert form_filler.extract_fields(b"not a pdf at all") == []
    assert form_filler.is_fillable(b"not a pdf") is False


def test_extract_fields_empty_for_flat_pdf():
    # A minimal valid PDF with no AcroForm -> no fillable fields.
    flat = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )
    assert form_filler.extract_fields(flat) == []


def test_fill_pdf_none_for_empty_values():
    assert form_filler.fill_pdf(b"%PDF-1.4", {}) is None


def test_fill_pdf_soft_fails_on_garbage():
    # Not a real PDF -> pypdf raises internally -> we return None, never raise.
    assert form_filler.fill_pdf(b"garbage", {"name": "x"}) is None
