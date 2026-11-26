# Changelog

## [1.2.1] — 2026-11-26

First public release.

### Security
- File paths are passed to AppleScript as arguments instead of being pasted into the script, so a crafted filename can no longer run commands when you copy or delete it.
- The local server rejects requests with a foreign `Host` header (DNS rebinding) and state-changing requests from other websites (foreign `Origin`).

### Changed
- MIT licence; README gains requirements, "tested on", privacy/security notes and a stronger "as is / no liability" disclaimer.
- Removed the original design spec and machine-specific references.

## [1.2.0] — 2026-11-24

### Added
- **Folder filter** under the search box: All folders + one button per folder with live match counts; `F` / `⇧F` to cycle; remembered between visits. Results show a folder badge, the preview shows the folder.
- **One-command install**: `./install.sh` (finds or installs Python, creates the venv, installs and starts the background service, guides through Full Disk Access) and `./uninstall.sh`. Background service label: `com.screenfind.server`.
- Hover tooltips everywhere, including a clear explanation of **Reindex** vs **Re-OCR every file** (with a time estimate).
- **Restart ScreenFind** button after granting Full Disk Access; clickable status-bar warning and a red dot on ⚙ Settings when access is blocked.
- `#settings` link opens Settings directly.
- README with screenshots (demo data) and an "as is / vibe-coded" disclaimer.

### Changed
- Larger, clearer header: Settings button with icon, bigger theme colour dots with names on hover, larger help button; slightly larger result and preview text and buttons (Open is the primary action).
- Default watched folder for new installs is the macOS screenshot location instead of a hard-coded path.

### Fixed
- **In-image highlights never appeared**: the OCR worker read Vision's word boxes with the wrong API and silently stored none. Re-OCR existing folders to get highlights on older screenshots.
- Highlight boxes were offset by the preview padding.
- After a restart the server could move to port 5124 because lingering connections were mistaken for a busy port; graceful shutdown is now capped at 3 s.
- Removed the hard-coded LaunchAgent plist, outdated `HOWTO.md` and pinned `requirements.txt` (dependencies live in `pyproject.toml`).

## [1.1.1] — 2026-11-24

### Changed
- Redesigned ⚙ Settings: one card per folder with status badge, progress bar, counts and relative last-indexed time; secondary actions (Re-OCR, Remove) moved into a ⋯ menu; clearer "Add a folder" form with a subfolders switch; index statistics as tiles; inline success/error messages; works on narrow screens and in all themes.
- Per-folder progress: only the folder being indexed shows a progress bar, others show "Queued".

### Added
- "Open settings & show file" button on the macOS-blocked warning (`POST /api/open-fda-settings`) plus a Copy path button.

### Fixed
- After a restart the server could grab port 5124 instead of 5123 (the old process hadn't released it yet), breaking bookmarks and the browser keyword. It now waits up to 10 s for the preferred port before falling back.
- Browsers could keep serving a stale `style.css`/`app.js` after an update; static files now revalidate on every load.
- A folder that doesn't exist yet (e.g. OneDrive not mounted at login) was reported as "watching" and never picked up later; it's now retried every minute and indexed once it appears.

## [1.1.0] — 2026-11-23

### Added
- Multiple watched folders (`watched_folders` in config; legacy `watched_folder` migrates automatically).
- ⚙ Settings overlay: add folder (path or native Browse… dialog), optional subfolder scanning, per-folder Reindex / Re-OCR all / Remove, live per-folder counts and progress.
- API: `GET/POST/DELETE /api/folders`, `POST /api/folders/reindex`, `POST /api/pick-folder`.
- CLI: `--reindex --folder PATH`; `--stats` lists per-folder counts.
- `folder` column on the index (auto-migrated, existing rows backfilled).
- Reindex requests are queued and de-duplicated (one OCR job at a time).
- Folders unavailable at startup (e.g. OneDrive not yet mounted) are retried every minute.
- Detects macOS privacy blocks on the launchd service: status-bar warning, per-folder warning with the program to allow, and files are no longer marked as OCR errors when access is denied.

### Fixed
- Arrow keys moved the selection two rows (duplicate handlers).

## [1.0.0] — 2026-06-17

### Added
- Initial release.
- Apple Vision OCR indexer with multiprocessing (`spawn` context, `cpu_count - 1` workers).
- SQLite FTS5 full-text index with BM25 ranking and stop-word stripping.
- File watcher via `watchdog` FSEventsObserver with 500 ms debounce.
- OneDrive Files On-Demand detection and download-trigger with configurable timeout.
- FastAPI web server on `localhost:5123` (auto-advances on conflict).
- Vanilla JS single-page UI: instant search, two-panel layout, 7 built-in themes.
- In-image word-level highlight overlay on canvas with `H` toggle.
- Full keyboard navigation (arrows, Enter, Cmd+Enter, Escape, /, ?, T).
- On-the-fly JPEG thumbnail generation with LRU in-memory cache.
- `rich` progress bar with ETA for bulk indexing.
- Resumable bulk indexing (skips already-indexed files by mtime).
- `--reindex`, `--reindex --full`, `--serve`, `--watch`, `--prune`, `--stats` CLI commands.
- `POST /api/reindex` endpoint for background reindex from the UI.
- launchd LaunchAgent plist for auto-start on login.
- `~/.screenshot-search/config.json` with all settings configurable.
