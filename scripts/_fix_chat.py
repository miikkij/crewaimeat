"""Fix live-chat-room.html: the specialist guessed the AimeatRealtime event names.
Real lib emits 'open'/'close'/'broadcast' (payload nested under .payload), not
'connected'/'disconnected'/'message'. Fetch -> patch -> republish in place."""
import os
import requests
from crewaimeat.author_tool import make_author_tools

base = "https://aimeat.io"
u = os.getenv("AIMEAT_APP_LOGIN_USER")
pw = os.getenv("AIMEAT_APP_LOGIN_PASSWORD")
tok = requests.post(f"{base}/v1/ghii/login", json={"username": u, "password": pw}, timeout=30).json()["data"]["token"]
html = requests.get(f"{base}/v1/apps/happydude500001/live-chat-room.html?mode=download",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=30).text

fixes = [
    # critical: input is enabled only in the connect-success handler -> must be 'open'
    ("rt.on('connected', function (msg) {", "rt.on('open', function (msg) {"),
    ("rt.on('disconnected', function (msg) {", "rt.on('close', function (msg) {"),
    # received broadcast event is 'broadcast' and the payload is nested under .payload
    ("rt.on('message', function (msg) {",
     "rt.on('broadcast', function (envelope) {\n        var msg = envelope.payload || {};"),
]
for old, new in fixes:
    n = html.count(old)
    assert n == 1, f"expected exactly 1 of {old!r}, found {n}"
    html = html.replace(old, new)
print("all 3 event-name fixes applied")

tools, _ = make_author_tools("aimeat-realtime-builder")
byname = {t.name: t for t in tools}
print(byname["publish_app"].run(
    filename="live-chat-room.html", html=html,
    name="Live Chat Room", description="Real-time multiplayer chat (event-name fix)",
    uses_cortex_json='["live-chat-room"]'))
