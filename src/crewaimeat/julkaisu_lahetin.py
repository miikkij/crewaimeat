"""JULKAISUPÖYTÄ → LÄHETIN: the approved pieces become draft posts in the team's social media desk.

The publishing desk (`julkaisupoyta`) leaves its pieces in owner memory: the LinkedIn post at
``julkaisu.{ref}.linkedin``, the X thread at ``julkaisu.{ref}.x``, the images at
``julkaisu.{ref}.kuvat``. The GTM plan (doc-mvamr945zwlo) puts them where a person reviews and sends
them: LÄHETIN, whose space is an organism workspace with a `post` record space (namespace
``lahetin.posts``). This step writes ONE post record per piece, after the workflow's last gate
(``julkaisu.{ref}.portti``) said `hyvaksy`.

NO MODEL, AND NOTHING IS SENT. The step copies text that a person approved into draft records, and
it never calls a publish or schedule route of the connections API; a person sends from LÄHETIN.

A PERSON'S WORK IS NEVER OVERWRITTEN. Writing the same ref again replaces a post only when the post
is still exactly what this step wrote: its `updatedAt` equals the one this step recorded at
``julkaisu.{ref}.lahetin``, and its `published` list is empty. A post somebody edited or sent, or one
this step did not write, is left as it is and named in the report.

THE PICTURE (LÄHETIN skill §4). An attachment is a file in the uploader's own storage, visibility
`workspace` bound to the space, recorded as `perChannel.<provider>.storageKey` and `storageOwner`. The
desk's image is in julkaisu-kuva's storage, public. So the step copies the first image into its OWN
storage under ``lahetin/<postId>-linkedin-<file>`` with that binding and records itself as the owner;
LÄHETIN copies it into the sender's storage before a send. When the copy fails, the post is written
without the picture and the report says why.

Every piece of this is read and written through `_aimeat_call` / `_aimeat_request`, as the agent.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import urllib.parse

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_request, record_deliverable_key
from crewaimeat.julkaisu_pipeline import (
    KUVAT_KEY,
    LAHETIN_KEY,
    PIECE_KEY,
    VALINTA_KEY,
    run_address,
    scope_entries,
    scope_vars,
    x_posts,
)
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.workspace_tools import WorkspaceWriteError, _current, _refused

# The LÄHETIN space "AIMEAT-viestintä" (AIMEAT organism). A run may name another with the variable
# `lahetin_space` = "<organism_id>/<ws>".
LAHETIN_ORG = "fbb51de5-56d5-4143-9871-b998a1187655"
LAHETIN_WS = "ws-mv8q4o8aefu"
POST_SPACE, POST_NS = "post", "lahetin.posts"

OFFER_ID = "vie-lahettimeen"
PORTTI_KEY = "julkaisu.{ref}.portti"
AINEISTO_KEY = "julkaisu.{ref}.aineisto"
CAMPAIGN = "Julkaisupöytä"
APPROVED = "hyvaksy"

_REF_IN_ID = re.compile(r"[^a-z0-9._-]+")
_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(agent_name: str, key: str):
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def gate_answer(value) -> str:
    """The option the person picked at the gate, or "" when nobody answered.

    The engine writes ``{picks: [<option id>], other?, by?, answeredAt}`` (aimeat-protocol
    services/workflow/engine-human.ts applyHumanAnswer); a hand-written `paatos` is read too.
    """
    if isinstance(value, dict):
        picks = value.get("picks")
        if isinstance(picks, list) and picks:
            return str(picks[0]).strip().casefold()
        return str(value.get("paatos") or value.get("decision") or "").strip().casefold()
    return str(value or "").strip().casefold()


def target_space(task: dict | None) -> tuple[str, str]:
    named = scope_vars(task).get("lahetin_space", "")
    if "/" in named:
        org, ws = named.split("/", 1)
        if org.strip() and ws.strip():
            return org.strip(), ws.strip()
    return LAHETIN_ORG, LAHETIN_WS


def post_id(ref: str, channel: str) -> str:
    return f"jp-{_REF_IN_ID.sub('-', ref.casefold()).strip('-')}-{channel}"


def first_version(piece) -> tuple[str, int]:
    """(the text of the piece's first version, how many versions it has). A single-version piece is
    the flat {text, notes}; several are {versiot: [{nro, kieli, text, notes}]}."""
    if not isinstance(piece, dict):
        return "", 0
    versions = piece.get("versiot")
    if isinstance(versions, list) and versions:
        first = next((v for v in versions if isinstance(v, dict) and str(v.get("text") or "").strip()), None)
        return (str(first.get("text")).strip() if first else ""), len(versions)
    text = str(piece.get("text") or "").strip()
    return text, (1 if text else 0)


def title_for(agent_name: str, ref: str) -> str:
    """The person's chosen angle (valinta), else the editor's (aineisto), else the ref."""
    valinta = _json(agent_name, VALINTA_KEY.format(ref=ref))
    angle = valinta.get("kulma") if isinstance(valinta, dict) else None
    if isinstance(angle, dict):
        for field in ("otsikko", "kulma"):
            if str(angle.get(field) or "").strip():
                return str(angle[field]).strip()
    aineisto = _json(agent_name, AINEISTO_KEY.format(ref=ref))
    if isinstance(aineisto, dict):
        for field in ("kulma", "valittu"):
            if str(aineisto.get(field) or "").strip():
                return str(aineisto[field]).strip()
    return f"Julkaisupöytä {ref}"


def own_gaii(agent_name: str) -> str | None:
    """Who owns a file this agent stores: its GAII (a node-defined agent runs under it already)."""
    if "#" in agent_name:
        return agent_name
    from crewaimeat.seedream_gen import _own_gaii

    return _own_gaii(agent_name)


def attach_picture(agent_name: str, image: dict, pid: str, org: str, ws: str) -> tuple[dict | None, str]:
    """Copy one desk image into this agent's storage, bound to the space. ({storageKey, storageOwner}, "")
    or (None, why not). Nothing is changed anywhere when it fails."""
    import requests

    url = str(image.get("url") or "")
    path = urllib.parse.urlparse(url).path
    if not path.startswith("/v1/pub/"):
        return None, f"the image has no node address to read it from ({url or 'no url'})"
    owner = own_gaii(agent_name)
    if not owner:
        return None, "this agent's own GAII could not be read, so the file would have no owner to name"
    try:
        got = _aimeat_request(agent_name, "GET", path, timeout=120)
        if got.status_code != 200 or not got.content:
            return None, f"reading the image answered HTTP {got.status_code}"
        mime = (got.headers.get("Content-Type") or "image/png").split(";")[0].strip()
        name = _FILE_NAME.sub("-", path.rsplit("/", 1)[-1]) or "kuva.png"
        key = f"lahetin/{pid}-linkedin-{name}"
        presign = {
            "key": key,
            "mime_type": mime,
            "visibility": "workspace",
            "workspace_ref": f"{org}/{ws}",
            "mode": "presigned",
        }
        r = _aimeat_request(agent_name, "POST", "/v1/storage", json=presign, timeout=60)
        upload_url = ((r.json() or {}).get("data") or {}).get("upload_url") if r.status_code == 200 else None
        if not upload_url:
            return None, f"the node did not give an upload address (HTTP {r.status_code})"
        put = requests.put(upload_url, data=got.content, headers={"Content-Type": mime}, timeout=180)
        if put.status_code not in (200, 201):
            return None, f"the upload answered HTTP {put.status_code}"
    except Exception as exc:  # noqa: BLE001 — a picture that did not copy must not lose the post
        return None, f"copying the image failed ({exc!r})"
    return {"storageKey": key, "storageOwner": owner}, ""


def _record(pid, title, body, channel, per_channel, ref, created_by, created_at, now) -> dict:
    return {
        "id": pid,
        "title": title,
        "body": body,
        "channels": [channel],
        "perChannel": {channel: per_channel},
        "tags": ["julkaisupoyta", ref],
        "campaign": CAMPAIGN,
        "plannedFor": None,
        "published": [],
        "createdBy": created_by,
        "createdAt": created_at,
        "updatedAt": now,
    }


def _write_post(agent_name: str, org: str, ws: str, record: dict, ref: str) -> str:
    """Write the post as a draft and publish it so LÄHETIN lists it. "" or the node's refusal."""
    payload = {
        "organism_id": org,
        "ws": ws,
        "space": POST_SPACE,
        "id": record["id"],
        "value": record,
        "ai_provenance": declare(
            Level.SYNTHESIZED,
            method=Method.SYNTHESIZED,
            human_involvement=HumanInvolvement.NONE,
            notes=f"julkaisupöytä {ref}: the piece a person approved at the gate, copied unchanged as a "
            "LÄHETIN draft; not sent anywhere.",
        ),
    }
    why = _refused(_aimeat_call(agent_name, "aimeat_workspace_write", payload, return_error=True), "write")
    if why:
        return why
    why = _refused(
        _aimeat_call(
            agent_name,
            "aimeat_workspace_publish",
            {"organism_id": org, "ws": ws, "namespace": POST_NS, "id": record["id"]},
            return_error=True,
        ),
        "publish",
    )
    return f"{why} The draft was written but LÄHETIN does not list it." if why else ""


def vie_lahettimeen(agent_name: str, task: dict | None = None, task_id: str | None = None) -> str:
    """For this run's ref: write the LinkedIn post and the X thread into LÄHETIN as draft posts.

    Returns the report. The step's own record (``julkaisu.{ref}.lahetin``) is written on every
    finished run, also when the gate did not approve, so the workflow sees the step ended and why.
    """
    key, ref, rule = run_address(task, "lahetin")
    org, ws = target_space(task)
    print(f"[{agent_name}] lahetin -> {org}/{ws}, record {key} ({rule})", file=sys.stderr)
    previous = _json(agent_name, LAHETIN_KEY.format(ref=ref))
    previous = previous if isinstance(previous, dict) else {}
    wrote_before = previous.get("posts") if isinstance(previous.get("posts"), dict) else {}

    answer = gate_answer(_json(agent_name, PORTTI_KEY.format(ref=ref)))
    lines: list[str] = []
    posts = dict(wrote_before)
    if answer != APPROVED:
        said = (
            f"the gate {PORTTI_KEY.format(ref=ref)} answered {answer!r}" if answer else "nobody has answered the gate"
        )
        lines.append(f"NOTHING WRITTEN: {said}; only an approved ({APPROVED}) run goes to LÄHETIN.")
    else:
        lines += _write_pieces(agent_name, ref, org, ws, posts)

    memo = {
        "space": {"organism_id": org, "ws": ws},
        "gate": answer or None,
        "posts": posts,
        "report": lines,
        "at": _now(),
    }
    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": key, "value": memo, "visibility": "owner", "tags": ["julkaisupoyta", "lahetin", f"ref:{ref}"]},
    )
    if written is None:
        lines.append(
            f"WARNING: {key} was NOT written, so the next run cannot tell its own posts from a person's "
            "and will leave them all as they are."
        )
    else:
        record_deliverable_key(task_id, key)
    return " ".join(lines)


def _write_pieces(agent_name: str, ref: str, org: str, ws: str, posts: dict) -> list[str]:
    title = title_for(agent_name, ref)
    created_by = agent_name.split("#", 1)[0]
    pieces = []
    linkedin, n_li = first_version(_json(agent_name, PIECE_KEY.format(ref=ref, channel="linkedin")))
    if linkedin:
        pieces.append(("linkedin", linkedin, {}, n_li))
    x_text, n_x = first_version(_json(agent_name, PIECE_KEY.format(ref=ref, channel="x")))
    if x_text:
        thread = "\n---\n".join(x_posts(x_text))
        pieces.append(("x", thread, {"format": "thread"}, n_x))

    lines: list[str] = []
    if not linkedin:
        lines.append(f"linkedin: no piece at {PIECE_KEY.format(ref=ref, channel='linkedin')}, so no post.")
    if not x_text:
        lines.append(f"x: no piece at {PIECE_KEY.format(ref=ref, channel='x')}, so no post.")

    for channel, body, per_channel, versions in pieces:
        pid = post_id(ref, channel)
        try:
            existing = _current(agent_name, org, ws, POST_SPACE, pid)
        except WorkspaceWriteError as exc:
            lines.append(f"{channel}: {pid} NOT written, because whether it exists could not be read: {exc}")
            continue
        if existing is not None:
            mine = (posts.get(pid) or {}).get("updatedAt")
            if existing.get("published"):
                lines.append(f"{channel}: {pid} was already sent, so it was left as it is.")
                continue
            if not mine or existing.get("updatedAt") != mine:
                lines.append(
                    f"{channel}: {pid} was changed by somebody after this step wrote it (or this step did not "
                    "write it), so it was left as it is."
                )
                continue
        now = _now()
        created_at = str((existing or {}).get("createdAt") or now)
        picture = ""
        if channel == "linkedin":
            kuvat = _json(agent_name, KUVAT_KEY.format(ref=ref))
            images = (
                [k for k in (kuvat or {}).get("kuvat") or [] if isinstance(k, dict)] if isinstance(kuvat, dict) else []
            )
            if images:
                got, why = attach_picture(agent_name, images[0], pid, org, ws)
                if got:
                    per_channel = {**per_channel, **got}
                    picture = f", with the first image ({got['storageKey']}; add its alt text in LÄHETIN)"
                else:
                    picture = f", WITHOUT the image: {why}"
        record = _record(pid, title, body, channel, per_channel, ref, created_by, created_at, now)
        why = _write_post(agent_name, org, ws, record, ref)
        if why:
            lines.append(f"{channel}: {pid} NOT written: {why}")
            continue
        posts[pid] = {"channel": channel, "updatedAt": now}
        more = f" (the first of {versions} versions)" if versions > 1 else ""
        lines.append(f"{channel}: draft {pid} {'replaced' if existing else 'written'}{more}{picture}.")
    return lines


def is_lahetin_task(task: dict | None) -> bool:
    """A workflow step's task names its offer in the scope entry `offer`."""
    entries = scope_entries(task)
    return OFFER_ID in (entries.get("offer"), entries.get("offer_id"))
