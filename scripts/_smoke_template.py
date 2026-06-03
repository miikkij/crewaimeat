"""Smoke-test the AIMEAT starter template end-to-end with the DEPLOYED libs.
Publishes a tiny app built from read_app_template() and verifies it renders (login bar +
await-login + session.fetch). Proves the template before agents are told to rely on it."""
import os
import re
from crewaimeat.author_tool import make_author_tools

AGENT = "aimeat-app-builder"  # onboarded; has a token
tools, state = make_author_tools(AGENT)
byname = {t.name: t for t in tools}


def run(_tool, **kw):
    return byname[_tool].run(**kw) if kw else byname[_tool].run()


# 1) fetch the live template
tpl = run("read_app_template")
print("=== read_app_template (first 200 chars) ===")
print(tpl[:200], "\n")

# 2) extract the HTML skeleton from the ```html ... ``` block
m = re.search(r"```html\s*(.*?)```", tpl, re.S)
assert m, "no html code block in template"
html = m.group(1).strip()

# 3) replace startApp body with something that proves session.fetch (parsed JSON, no .json())
new_startapp = (
    "async function startApp(session) {\n"
    "      try {\n"
    "        const res = await session.fetch('/v1/agents');\n"
    "        const agents = (res && res.data && (res.data.agents || res.data.items)) || [];\n"
    "        document.getElementById('app').innerHTML = "
    "'<h2>Template Smoke Test OK</h2><p>Agents visible: ' + agents.length + '</p>';\n"
    "      } catch (e) {\n"
    "        document.getElementById('app').innerHTML = '<h2>Template Smoke Test FAILED</h2><pre>' + e + '</pre>';\n"
    "      }\n"
    "    }"
)
html = re.sub(r"async function startApp\(session\)\s*\{.*?\n    \}", new_startapp, html, count=1, flags=re.S)
assert "Template Smoke Test OK" in html, "startApp injection failed"
# title the app
html = html.replace("<title>App Name</title>", "<title>Template Smoke Test</title>")

# 4) publish it
print("=== publish_app ===")
print(run("publish_app", filename="template-smoketest.html", html=html,
          name="Template Smoke Test", description="starter-template smoke test"), "\n")

# 5) verify render
print("=== verify_render ===")
print(run("verify_render", filename="template-smoketest.html", expect_csv="Template Smoke Test OK"))
