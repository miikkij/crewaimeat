"""Deterministic helpers for AIMEAT data packages, and the crew tools built on them.

The rule this module obeys: NO second reader, NO second schema inference, NO second content hash.
Every byte of parsing is `aimeat_crewai.datapackage`; what lives here is the shape a crew needs —
resolve an address, hand the model a schema instead of a data dictionary, and run aggregations the
model describes rather than writes.

WHY THE MODEL NEVER SEES COLUMN DOCUMENTATION. `open_package` returns the Table Schema: every
column's name and type, and the row count. That is the whole contract — an agent that still has to
ask "what is in this file" has found a gap in the schema, and THAT is the finding, not a reason to
write documentation. So no tool here accepts or emits a column description.

ONE WORKAROUND, REPORTED RATHER THAN HIDDEN — see `resolve_descriptor`.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from aimeat_crewai import (
    QualityGateRefused,
    package_versions,
    publish_package,
    read_package,
    to_dataframe,
    to_parquet,
)

NODE = "https://aimeat.io"


def resolve_descriptor(url: str, *, timeout: int = 60) -> str:
    """Return an address `read_package` accepts, given either kind of address.

    WORKAROUND, and it should not need to exist. `GET /v1/datapackages/<owner>/<name>` — the address
    the docs give — answers with the node's REST ENVELOPE:

        {ok, protocol, node, …, data: {descriptor: {...aimeat: {...}}, descriptor_url, latest}}

    `read_package` looks for the `aimeat` block at the top level of what it fetched, does not find it
    one layer down, and refuses with "is a Frictionless descriptor without an `aimeat` block … Read
    it with frictionless directly." The block IS there, at `data.descriptor.aimeat`. So the
    documented address fails on a package that is completely well-formed, and the message sends you
    off to another library.

    This unwraps the envelope and hands back `data.descriptor_url` — the permanent content-hash
    address, which `read_package` reads without complaint. A bare descriptor URL passes through
    untouched, so callers can hold either kind."""
    if "/v1/datapackages/" not in url:
        return url
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — a node address, not user input
        body = json.load(r)
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and data.get("descriptor_url"):
        return data["descriptor_url"]
    raise ValueError(
        f"{url} answered without a `data.descriptor_url`, so there is no address to read. "
        f"Top-level keys: {sorted(body) if isinstance(body, dict) else type(body).__name__}"
    )


def open_package(url: str) -> dict[str, Any]:
    """The schema and the provenance — everything an agent needs before asking a question.

    Deliberately NOT the rows: a model reasoning over 718 rows in its context would be guessing at
    scale. It gets the column names and types and then names an aggregation."""
    pkg = read_package(resolve_descriptor(url))
    res = pkg.resource()
    schema = (res or {}).get("schema") or {}
    return {
        "descriptor_url": resolve_descriptor(url),
        "resource_url": pkg.resource_url(),
        "columns": [{"name": f.get("name"), "type": f.get("type")} for f in schema.get("fields", [])],
        "row_count": (res or {}).get("rowCount"),
        "license": pkg.license,
        "supersedes": pkg.supersedes,
    }


def frame(url: str):
    """The typed dataframe. Types come from the schema, never from inference — that is the whole
    reason this exists rather than `pandas.read_csv(resource_url)`. Measured on laake-saatavuus:
    175 of 718 `vnr` identifiers are zero-padded, and read_csv turns every one of them into an int."""
    return to_dataframe(read_package(resolve_descriptor(url)))


def aggregate(
    url: str,
    value: str,
    how: str = "mean",
    group_by: str | None = None,
    where: str | None = None,
    top: int = 10,
) -> dict[str, Any]:
    """One typed aggregation, described rather than written.

    `where` is a pandas query string, which is the one place a model's text reaches the data. It
    cannot reach anything else: `DataFrame.query` evaluates comparisons over columns, not python.
    A bad column name fails loudly with the name, which is the answer the caller needs anyway."""
    df = frame(url)
    if where:
        df = df.query(where)
    if value not in df.columns:
        raise KeyError(f"no column {value!r}; the schema has {list(df.columns)}")
    if group_by:
        if group_by not in df.columns:
            raise KeyError(f"no column {group_by!r}; the schema has {list(df.columns)}")
        s = getattr(df.groupby(group_by)[value], how)().sort_values(ascending=False).head(top)
        return {"rows": len(df), "group_by": group_by, "value": value, "how": how, "result": s.round(2).to_dict()}
    return {"rows": len(df), "value": value, "how": how, "result": float(getattr(df[value], how)())}


def versions(owner: str, name: str, *, agent: str = "datapkg-analyst") -> list[dict[str, Any]]:
    """Every version, newest first — the list a pin is chosen from."""
    from aimeat_crewai import serve_client

    return package_versions(serve_client(agent), owner, name)


def save_parquet(url: str, path: str) -> int:
    """Hand the package on as Parquet. Raises when pyarrow is missing rather than writing a CSV."""
    return to_parquet(read_package(resolve_descriptor(url)), path)


def publish(name: str, rows: list[dict], changes: str, *, agent: str = "datapkg-analyst", **kw) -> dict[str, Any]:
    """Publish a new version. `changes` is required by the node and must be WRITTEN, not filled in.

    A stranger reading the version list decides from this sentence alone whether to move to it, so
    "update" is a non-answer. QualityGateRefused travels up untouched: a refusal names the row and
    the field, and swallowing it would leave the caller believing a version exists."""
    from aimeat_crewai import serve_client

    if not changes or changes.strip().lower() in {"update", "updated", "new version", "päivitys"}:
        raise ValueError(
            f"changes={changes!r} says nothing a stranger could decide from. Write what actually "
            "differs from the previous version — what was added, what was corrected, what to expect."
        )
    return publish_package(serve_client(agent), name, rows, changes=changes, **kw)


__all__ = [
    "QualityGateRefused",
    "aggregate",
    "frame",
    "open_package",
    "publish",
    "resolve_descriptor",
    "save_parquet",
    "versions",
]
