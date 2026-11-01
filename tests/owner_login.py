"""How a live suite makes an owner and signs them in: POST /v1/ghii with a password, then /v1/ghii/login.

The same two calls as aimeat-protocol's `test/helpers/owner-login.ts` (`registerOwner`). Until node 4.0.0 the
live suites registered with POST /v1/owners and signed `name + NODE_ID + timestamp` with the account key the
registration returned; 4.0.0 refuses that with OWNER_KEY_LOGIN_OFF, so every `*_live.py` stopped in its fixture
(measured 2026-11-01). An agent still signs in with its own key on /v1/auth/token, which is not this.
"""

from __future__ import annotations

import time

import requests

# Passes the node's strength rule; the same one aimeat-protocol's helper gives its owners.
OWNER_PASSWORD = "E2eOwnerPass-2026"


def _post_patiently(url: str, body: dict) -> requests.Response:
    """POST, and again while the node answers 429: the registration limiter is not what a suite tests."""
    waited = 0.0
    r = requests.post(url, json=body, timeout=20)
    while r.status_code == 429 and waited < 90:
        pause = min(max(float(r.headers.get("retry-after") or 0), 1.5), 30)
        time.sleep(pause)
        waited += pause
        r = requests.post(url, json=body, timeout=20)
    return r


def register_owner(base: str, username: str, password: str = OWNER_PASSWORD) -> tuple[str, dict]:
    """Register `username` with a password and sign them in. Returns (owner name, Authorization headers)."""
    r = _post_patiently(f"{base}/v1/ghii", {"username": username, "display_name": username, "password": password})
    assert r.status_code == 201, f"register {username}: {r.status_code} {r.text}"
    name = r.json()["data"]["owner"]["name"]
    login = _post_patiently(f"{base}/v1/ghii/login", {"username": name, "password": password})
    token = (login.json().get("data") or {}).get("token")
    assert login.status_code == 200 and isinstance(token, str), f"sign-in {name}: {login.status_code} {login.text}"
    return name, {"Authorization": f"Bearer {token}"}
