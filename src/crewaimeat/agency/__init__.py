"""crewaimeat.agency — the aimeat-agency local operator surface.

The cockpit (`crewaimeat.agency.cockpit`) is a small FastAPI server the desktop appliance's Tauri shell
spawns and points its webview at. It reuses the existing crewaimeat read models and controls
(brains / brain_templates / local_memory / fleet_state / tui.actions) — no fleet logic is reimplemented
here. See docs/internal/aimeat-agency-slice1-plan.md.
"""
