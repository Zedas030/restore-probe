# Changelog

## 1.1.0 – 2026-11-05

- Redesigned web report: shared homelab theme, light/dark toggle, optional dashboard link.
  - Overall verdict on top ("All 6 backups restorable" or which ones failed) and KPI tiles
    (services ok, last run, duration, oldest backup age).
  - One card per service with a clean three-step stage list, backup facts as a key/value list,
    comparison values, notes, sandbox proof and error details still collapsible.
  - Works on phones (375 px) and offline (theme inlined, no external assets); status-colored favicon kept.
- New optional settings `ui.home_url` (default empty = no button) and `ui.home_label` (default "Dashboard").

## 1.0.0

- First release.
