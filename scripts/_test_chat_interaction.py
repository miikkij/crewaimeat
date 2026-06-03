"""Real interaction test for live-chat-room.html (what verify_render can't do):
log in -> create a room -> assert msg-input gets ENABLED (the 'open' fix) -> send a
message -> assert it renders. Single client (echo-dependent for the render step)."""
import os
from playwright.sync_api import sync_playwright

base = "https://aimeat.io"
u = os.getenv("AIMEAT_APP_LOGIN_USER")
pw = os.getenv("AIMEAT_APP_LOGIN_PASSWORD")
url = f"{base}/v1/apps/happydude500001/live-chat-room.html?mode=inline"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
    pg.goto(url, wait_until="networkidle", timeout=45000)
    for _ in range(30):
        try:
            if pg.evaluate("() => !!(window.AIMEAT && window.AIMEAT.auth)"):
                break
        except Exception:
            pass
        pg.wait_for_timeout(500)
    login = pg.evaluate(
        "async ([u, pw]) => { try { await AIMEAT.auth.loginWithPassword(u, pw); return 'ok'; }"
        " catch (e) { return 'ERR: ' + (e && e.message || e); } }", [u, pw])
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(3000)
    # create + enter a room
    pg.fill("#new-room-name", "verify-test-room")
    pg.click("#btn-create-room")
    enabled = False
    for _ in range(40):
        try:
            if pg.eval_on_selector("#msg-input", "el => el.disabled") is False:
                enabled = True
                break
        except Exception:
            pass
        pg.wait_for_timeout(500)
    print("login:", login)
    print("msg-input ENABLED after entering room (the fix):", enabled)
    rendered = None
    if enabled:
        pg.fill("#msg-input", "hello-verify-123")
        pg.click("#btn-send")
        pg.wait_for_timeout(3000)
        msgs = pg.eval_on_selector("#messages", "el => el.innerText") or ""
        rendered = "hello-verify-123" in msgs
        print("sent message rendered in #messages:", rendered)
        print("messages sample:", msgs[:200].replace(chr(10), " "))
    print("console_errors:", errors[:5])
    b.close()
