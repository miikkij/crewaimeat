# Verification

Use an isolated checkout with its own virtual environment:

```powershell
uv sync --frozen --group dev
uv run ruff format --check src tests crews
uv run ruff check src tests crews
uv run pytest
uv run crewaimeat doctor --strict
```

CI runs these checks on Ubuntu and Windows. Tests use a temporary `AIMEAT_HOME`, discard inherited
API credentials, and construct models with a dummy key. Socket connections and external DNS fail
the test even when application code catches ordinary exceptions. Structural crew tests explicitly
disable optional embedding services; model behavior tests supply a fake model.

An in-process FastAPI TestClient needs no network exemption. A test that starts a real local server
must declare `@pytest.mark.loopback`; external connections stay blocked. Python subprocess tests
must declare `@pytest.mark.local_process`, use the current interpreter, and inherit the environment.
The inherited `sitecustomize` guard blocks network and further subprocesses. This is an accidental
I/O guard for trusted tests, not a sandbox for hostile code. `@pytest.mark.node_syntax` permits only
`node --check <file>` to parse JavaScript without executing it.
Mock connector and model boundaries
explicitly instead of permitting a production node or an installed CLI.

`test_state_reliability.py` covers delayed and concurrent approvals, restart behavior, expiration,
preference migration, latest chat turns, SQLite rollback/closure, and doctor verdict consistency.
`test_runtime_boundaries.py` covers transport recovery, identity refusal, binary reads, failed
publication and completion retries. The rest of the suite covers crew wiring and domain behavior.

The doctor baseline contains no accepted source findings. Machine-only configuration warnings are
reported separately and cannot be added by `--accept-baseline`. Do not use baseline acceptance to
hide a new route or failure-handling regression. Live doctor checks require an authorized node and
are separate from this offline gate.

The desktop workflow builds on relevant pull requests, manual dispatches, and release tags. It runs
`scripts/smoke_agency_installer.ps1` against the NSIS bundle before publishing: silent installation,
packaged source and uv discovery, frozen dependency provisioning, loopback startup, rejected missing
token, brain create/read/delete, memory route and HTML. Provisioning needs package-network access;
the smoke uses a temporary home and does not connect an account or start a fleet. It exercises the
installed backend, not WebView interaction or the updater UI. Test those manually when changing the
Rust shell or the browser user flow. Only release tags enable signing and publication.
