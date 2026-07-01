---
name: release-prep
description: >
  Stage an aimeat-agency release: bump the version manifest + CHANGELOG, show the exact tag
  command, then STOP. Manual only — invoke with /release-prep <version>. Never tags, never pushes.
disable-model-invocation: true
allowed-tools: Read, Edit, Bash(git status:*), Bash(git diff:*)
---

# release-prep — stage a release, then stop

Target version: **$1** (e.g. `0.8.28`). If no version was given, ask for it and stop.

> Releases happen ONLY when the owner explicitly asks. This skill **stages** a release and prints
> the tag command — it does **not** tag or push. Wait for an explicit "ship it" before anything
> touches a tag or the remote.

## What CI gates (so the bump must be exact)
A pushed `agency-v*.*.*` tag triggers `.github/workflows/release-agency.yml`. CI verifies the tag's
numeric part **equals** `aimeat-agency/src-tauri/tauri.conf.json` `version`, and **fails the build**
if they differ. So the manifest bump must land before the tag. All three version files are currently
in sync — keep them so.

## Steps
1. `git status` — confirm a clean-enough tree; note anything unexpected.
2. Bump the version to **$1** in all three, keeping them identical:
   - `aimeat-agency/src-tauri/tauri.conf.json` → `"version"` (the one CI checks)
   - `aimeat-agency/package.json` → `"version"`
   - `aimeat-agency/src-tauri/Cargo.toml` → `version`
3. Add a `CHANGELOG.md` entry for **$1** summarizing the changes since the last release
   (read recent commits / the diff for the substance — don't invent).
4. `git diff` — show exactly what changed so the owner can eyeball it.

## Then STOP — print, do not run
Output the exact command for the owner to run **when they say ship it**:

```
git add -A && git commit -m "release(agency): $1" && git tag agency-v$1 && git push origin main && git push origin agency-v$1
```

Do not run any `git tag`, `git commit`, or `git push` yourself. End with: "Staged $1 — say *ship it*
to release." Leave the working tree staged for the owner's review.

## Related
- Memory: `only-release-when-explicitly-asked` (commit to main freely; build ONLY when asked).
- Workflow file: `.github/workflows/release-agency.yml`.
