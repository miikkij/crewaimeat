# Security policy

## Reporting a vulnerability

Report privately through GitHub's **[Report a vulnerability](https://github.com/miikkij/crewaimeat/security/advisories/new)**
button (Security tab → Advisories). It opens a private advisory only the maintainers can see — please
use it instead of a public issue, so a fix can ship before the detail is public.

Include what you were doing, what happened, and enough to reproduce it. You will get a reply in the
advisory thread; that thread is where the fix and disclosure are coordinated.

## What is in scope

- The `crewaimeat` toolkit and the crews under `crews/`.
- The `aimeat-agency` desktop app (`aimeat-agency/`).
- The CI and release workflows under `.github/`.

Agent behaviour driven by a model prompt is not itself a vulnerability report unless it crosses a
trust boundary the code was supposed to hold (a task-runner acting without owner approval, a scope
that is not enforced, a secret that leaves the `.aimeat` home). See `CLAUDE.md` for those boundaries.

## Supported versions

`main` is the supported line. Fixes land there; there is no separate maintenance branch.
