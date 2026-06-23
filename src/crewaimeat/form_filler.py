"""Form-filler — read a fillable PDF's fields and fill them in place.

`extract_fields` returns the AcroForm fields (name/type/options) of a fillable PDF, or [] for a FLAT or
scanned PDF (no form layer — the common case for government forms). `fill_pdf` writes values into an
AcroForm and returns the filled PDF bytes. For flat forms the caller reads the text (vision.extract_
document_text) and produces a completed-answers DOCUMENT instead — the universal path; this module's
deterministic job is just the AcroForm case. Pure pypdf, no external services, never raises.
"""

from __future__ import annotations

import io


def extract_fields(pdf_bytes: bytes) -> list[dict]:
    """-> [{name, type, options}] for each AcroForm field, or [] if the PDF has no fillable fields. `type`
    is the PDF field type stripped of '/' (Tx text, Btn button/checkbox, Ch choice, Sig signature)."""
    try:
        from pypdf import PdfReader

        fields = PdfReader(io.BytesIO(pdf_bytes)).get_fields()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for name, f in (fields or {}).items():
        ftype, opts = "Tx", None
        try:
            ftype = str(f.get("/FT") or "Tx").lstrip("/") or "Tx"
            raw = f.get("/_States_") or f.get("/Opt")
            opts = [str(x) for x in raw] if raw else None
        except Exception:  # noqa: BLE001
            pass
        out.append({"name": str(name), "type": ftype, "options": opts})
    return out


def is_fillable(pdf_bytes: bytes) -> bool:
    """True if the PDF has an AcroForm we can fill in place (vs a flat/scanned form)."""
    return bool(extract_fields(pdf_bytes))


def fill_pdf(pdf_bytes: bytes, values: dict) -> bytes | None:
    """Fill the AcroForm fields named in `values` (name -> value) and return the filled PDF bytes, or None.
    Unknown field names are ignored by pypdf. Sets NeedAppearances so the values render in viewers."""
    if not values:
        return None
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        writer.append(reader)
        str_values = {str(k): str(v) for k, v in values.items() if v is not None}
        for page in writer.pages:
            try:
                writer.update_page_form_field_values(page, str_values, auto_regenerate=False)
            except Exception:  # noqa: BLE001 — a page without the field just skips
                continue
        try:
            writer.set_need_appearances_writer(True)  # make filled values visible in all viewers
        except Exception:  # noqa: BLE001
            pass
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        return data or None
    except Exception:  # noqa: BLE001
        return None
