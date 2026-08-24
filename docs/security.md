# Security: what runs, where to watch, how to respond

This repo carries the same GitHub-native security layer as the AIMEAT platform repo. Everything here
is **advisory**: findings land on the **Security tab** and inform the next fix, and only one thing
blocks a merge (the dependency licence/CVE gate on a pull request). The goal is one pane where every
signal shows up, not a wall that stops work.

## The layers

| Layer | Catches | Reports to | When |
|---|---|---|---|
| **CodeQL** (`.github/workflows/codeql.yml`) | bugs and injection in our Python and our workflows (`security-extended` suite) | Code scanning | every push/PR to main, weekly Mon 04:41 |
| **Dependency review** (`dependency-review.yml`) | a new dependency with a high-severity CVE or a GPL/AGPL licence | fails the PR | every PR |
| **OSSF Scorecard** (`scorecard.yml`) | supply-chain posture: unpinned actions, over-broad workflow scopes, missing branch protection | Code scanning + OSSF dashboard | weekly Mon 05:20, push to main |
| **Dependabot alerts** | known CVEs in `uv.lock` (pip) and `Cargo.lock` (the Tauri app) | Dependabot | continuously |
| **Secret scanning + push protection** | a committed credential — and it blocks the push that would add one | Secret scanning | on every push |
| **Private vulnerability reporting** | an outside report, privately (see `SECURITY.md`) | Security advisories | on report |
| **Dependabot updater** (`.github/dependabot.yml`) | action pins going stale | one grouped PR / week | weekly |

Every action in the workflows is pinned to a commit SHA. The updater is what keeps those pins from
silently rotting — it is the maintenance half of pinning, scoped to `github-actions` only so the pip
tree stays on security alerts without routine-bump noise.

## Where to watch

**Security tab** (`https://github.com/miikkij/crewaimeat/security`):
- **Code scanning** — CodeQL + Scorecard findings. Filter `is:open branch:main`.
- **Dependabot** — dependency CVEs, grouped by severity.
- **Secret scanning** — any credential that reached the repo.

A green push does not mean zero findings: the scanners are advisory, so a finding sits on the tab
without failing the build. Check the tab, not the checkmark.

## Response playbook

### A Dependabot alert (a dependency CVE)

Two moves, and the alert itself tells you which:

1. **Bump**, when a patched version exists. Python is uv-managed:
   ```bash
   uv lock --upgrade-package <pkg>        # repeat --upgrade-package per package
   uv sync && uv run pytest               # prove the tree still resolves and the floor passes
   ```
   The Tauri crates are cargo-managed and update the lockfile without touching `Cargo.toml`:
   ```bash
   cd aimeat-agency/src-tauri && cargo update -p <crate>
   ```
   A transitive dep held back by a parent (crewai pinning an older `mcp`, say) moves when you bump
   the parent too — `--upgrade-package crewai` alongside it. Commit the lockfile alone.

2. **Dismiss as not reachable**, when the vulnerable code is never run here. Give a reason on the
   Security tab (or `gh api -X PATCH .../dependabot/alerts/<n> -f state=dismissed -f dismissed_reason=not_used`).
   Two patterns recur:
   - **A network/server CVE in a library we only embed.** `chromadb`'s pre-auth server injection is
     the case: crewai uses it as a local client and never starts the server, and the agents run
     protected with no outward network. Not reachable.
   - **A platform dependency we do not ship.** `glib` is a Linux GTK crate pulled transitively by
     Tauri; the agency builds Windows-only (WebView2), so `glib` is never compiled. Not shipped.

   Dismiss only when you can name why the path cannot be hit. A parsing or client-side CVE
   (`aiohttp`, `cryptography`, `pillow`, `pypdf`) is real — bump it.

### A CodeQL or Scorecard finding

Fix at the source. If it is a false positive for how the code is actually reached, dismiss it on the
Security tab **with a reason** — a bare dismissal reads as "ignored" to the next person. Scorecard's
posture findings (pin an action, tighten a workflow scope) are usually a small workflow edit.

### A secret-scanning hit

Treat the secret as burned: **rotate it first**, then dismiss the alert. Push protection should stop
most before they land; if one is flagged post-hoc, rotation is the fix, not deletion from history
(the old value is already public).

### A failing dependency-review on a PR

The PR added a dependency with a high-severity CVE or a GPL/AGPL licence. That is the one blocking
gate, on purpose: a copyleft dep in a shipped/hosted product is a licence decision, not a lockfile
accident. Either pick a different dependency, or make it a deliberate, recorded approval.

## Keeping it healthy

- **Leave the settings on.** Dependabot alerts, secret scanning, push protection and private
  vulnerability reporting are the floor. Turning one off is a silent regression.
- **Let the weekly updater PR through** after a glance — it keeps the SHA pins current.
- **Advisory is the default; make a rule blocking only once its false positives are understood.**
  A scanner that blocks on noise gets muted, and a muted scanner sees nothing.

## The first howl (2026-08-24)

Turning the layer on surfaced **44 dependency alerts** that had been invisible: 1 critical, 18 high,
17 moderate, 8 low — 43 in the pip tree (`uv.lock`), 1 in the Tauri crates. Across about ten packages:

- **chromadb** (critical) — dismissed, not reachable (embedded client, no server, protected agents).
- **glib** (moderate) — dismissed, not shipped (Windows-only build).
- **The rest** (`cryptography`, `aiohttp`, `pillow`, `pypdf`, `starlette`, `python-multipart`, `h2`)
  — all real, all patched upstream, all cleared by one `uv lock` upgrade that moves crewai a patch
  level (1.15.6 → 1.15.17) and pulls the fixed versions with it. Apply, then `uv sync && uv run pytest`.

This is the shape every future howl takes: a handful of packages, most fixed by a re-lock, a few
dismissed with a named reason.

## What is deliberately not here

The AIMEAT platform repo also runs a bespoke layer — ast-grep structural rules, Semgrep taint, and an
AI-triage pass — that encodes *that node's* security invariants (identity resolution, scope gates,
namespace fences). Those are specific to the node and do not transfer. If crewaimeat grows invariants
worth mechanising (a scope that must always be checked before a task runs, a boundary the fleet must
never cross), that is when to add a rule here — not before, because a rule with nothing true to assert
is just noise on the tab.
