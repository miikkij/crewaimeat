"""Sweep every AIMEAT tool call in this repo for parameters the connector does not accept.

3.3.1 REFUSES an undeclared parameter instead of ignoring it, so a field we send that the tool does
not publish now fails the call outright. This finds them statically, before the fleet meets them.
"""

import ast
import json
import pathlib
import subprocess

ROOT = pathlib.Path("E:/dev/GitHub/crewfive")
NODE_PKG = "C:/Program Files/nodejs/node_modules/aimeat"

# Mirror withDeclaredInputOnly exactly: tool.input UNION the shared catalog's input, and a tool
# that declares nothing anywhere stays permissive (declared.size <= 4 -> unguarded).
js = """Promise.all([import('./dist/src/cli/connect/tool-call.js'),
              import('./dist/src/mcp/catalog/index.js').catch(()=>null),
              import('./dist/src/mcp/catalog/definitions/index.js').catch(()=>null)]).then(([m,c1,c2])=>{
  const cat = (c1&&(c1.getAimeatToolDefinition||c1.default?.getAimeatToolDefinition))
           || (c2&&(c2.getAimeatToolDefinition||c2.default?.getAimeatToolDefinition));
  const out={};
  for(const x of m.CONNECT_CLI_TOOLS){
    const a=Object.keys(x.input??{}), b=Object.keys((cat?cat(x.name):null)?.input??{});
    const d=[...new Set([...a,...b])];
    out[x.name] = d.length ? d : null;   // null = declares nothing -> permissive
  }
  console.log(JSON.stringify(out));
});"""
accepted = json.loads(subprocess.run(["node", "-e", js], cwd=NODE_PKG, capture_output=True, text=True).stdout)
print(f"connector 3.3.1 publishes {len(accepted)} shell-callable tools")

CALLERS = {"_aimeat_call", "_call", "_tool"}
findings, seen_calls, unknown_tools = [], 0, set()

for p in (
    list((ROOT / "src").rglob("*.py")) + list((ROOT / "crews").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if fn not in CALLERS:
            continue
        # tool name is the first string-literal arg; payload is the first dict literal
        tool = next(
            (
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("aimeat_")
            ),
            None,
        )
        payload = next((a for a in node.args if isinstance(a, ast.Dict)), None)
        if not tool or payload is None:
            continue
        seen_calls += 1
        if tool not in accepted:
            unknown_tools.add(tool)
            continue
        if accepted[tool] is None:  # declares nothing anywhere -> the guard leaves it permissive
            continue
        keys = [k.value for k in payload.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        extra = [k for k in keys if k not in accepted[tool] and k != "agent_name"]
        if extra:
            findings.append((str(p.relative_to(ROOT)).replace("\\", "/"), node.lineno, tool, extra, accepted[tool]))

print(f"scanned {seen_calls} tool calls with literal payloads\n")
if unknown_tools:
    print("tools NOT on the shell surface at all (called anyway):")
    for t in sorted(unknown_tools):
        print(f"   {t}")
    print()
if not findings:
    print("NO undeclared parameters. Nothing here breaks on the UNKNOWN_PARAMETER change.")
for f, ln, tool, extra, ok in sorted(findings):
    print(f"{f}:{ln}  {tool}")
    print(f"    sends but tool does not accept: {extra}")
    print(f"    accepted: {ok}")
