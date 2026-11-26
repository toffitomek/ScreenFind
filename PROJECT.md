# ScreenFind — Project Guide

Everything needed to install, run, move, maintain and understand ScreenFind. For a short overview, see [README.md](README.md).

> **Disclaimer:** vibe-coded with AI assistance and provided as is, without warranty, support or any liability. MIT licensed. See the README for requirements and what it has been tested on.

---

## What it is

ScreenFind is a local macOS tool that watches one or more folders of screenshots, runs Apple Vision OCR on each image, and makes the text instantly searchable. It has a browser UI (FastAPI server on `localhost:5123`) and a CLI. No cloud, no API keys, no telemetry.

**Stack:** Python 3.11+ · Apple Vision (`pyobjc-framework-Vision`) · SQLite FTS5 · FastAPI + uvicorn · watchdog (FSEvents) · vanilla JS frontend (no build step).

```
 watched folders           FSEvents            one long-lived
 (Desktop, OneDrive…) ───► watchdog ───────►   OCR subprocess (Apple Vision)
                                                      │ text + word boxes
                                                      ▼
 browser UI  ◄────── FastAPI server ◄─────── SQLite FTS5 index
 localhost:5123            ▲                 ~/.screenshot-search/index.db
                           └── catch-up reindex on startup, queued jobs
```

---

## Quick start

```bash
git clone https://github.com/toffitomek/ScreenFind.git ~/code/ScreenFind
cd ~/code/ScreenFind && ./install.sh
```

Then allow Full Disk Access when prompted (see [§macOS privacy permission](#macos-privacy-permission-important)), open **http://localhost:5123**, and add folders under **⚙ Settings**.

### Where things live

| Thing | Location |
|---|---|
| Project source | wherever you cloned it, e.g. `~/code/ScreenFind` — **not** inside OneDrive/iCloud/Dropbox |
| Virtualenv | `~/.screenshot-search/venv` |
| Config + index DB | `~/.screenshot-search/config.json`, `~/.screenshot-search/index.db` |
| Background service | `~/Library/LaunchAgents/com.screenfind.server.plist` (label `com.screenfind.server`) |
| Logs | `~/.screenshot-search/logs/server.log`, `~/.screenshot-search/logs/install.log` |

Synced folders lock files, and Python imports then fail with `OSError: [Errno 11] Resource deadlock avoided`. If the repo sits in a synced folder anyway, `install.sh` installs a copy instead of linking to it.

---

## Daily operations

| What you want | How |
|---|---|
| Open the UI | http://localhost:5123 (or `ss <words>` from the address bar, see below) |
| Restart the service | `launchctl kickstart -k gui/$(id -u)/com.screenfind.server`, or ⚙ Settings → Restart (shown when needed) |
| Stop until next login | `launchctl bootout gui/$(id -u)/com.screenfind.server` |
| Start again | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.screenfind.server.plist` |
| Update after `git pull` | `./install.sh` (safe to re-run) |
| Tail the log | `tail -f ~/.screenshot-search/logs/server.log` |
| Index status | ⚙ Settings, or `screenfind --stats` |
| Catch up one folder | ⚙ Settings → Reindex, or `screenfind --reindex --folder PATH` |
| Re-OCR everything | `curl -X POST "localhost:5123/api/reindex?full=true"` |
| Search from terminal | `screenfind "invoice"` (`--json` for scripting) |
| Drop rows of deleted files | `screenfind --prune` |

`screenfind` is an optional alias: `alias screenfind="$HOME/.screenshot-search/venv/bin/screenshot-search"`.

### Browser address-bar search

- **Chrome / Edge / Brave / Arc:** Settings → Search engine → Manage search engines → **Add**. Name `ScreenFind`, shortcut `ss`, URL `http://localhost:5123/?q=%s`. Then type `ss`, space, and your words.
- **Firefox:** right-click the search box on the page → *Add a Keyword for this Search…*
- **Safari:** no built-in support.

`http://localhost:5123/#settings` opens Settings directly.

---

## Install details

`install.sh` does the following, and is idempotent:

1. Finds Python 3.11+. It re-uses the Python of an existing install, so a Full Disk Access grant keeps working. Otherwise it looks for Homebrew 3.13 → 3.12 → 3.11 → any `python3`, then python.org. If none is found and Homebrew exists, it runs `brew install python@3.13`.
2. Creates `~/.screenshot-search/venv` and runs `pip install -e <repo>`. In a synced folder it installs a copy without `-e`. Pip output goes to `logs/install.log`.
3. Creates the default config on first run. The default watched folder is the macOS screenshot location (`defaults read com.apple.screencapture location`), or `~/Desktop`.
4. Replaces any previous ScreenFind service, waits for the old process to exit, writes the LaunchAgent plist and starts it. `RunAtLoad`, `KeepAlive` and `ThrottleInterval 10` are set.
5. Prints the Full Disk Access instructions, optionally opens the settings page and reveals the program in Finder, then opens the UI.

`./install.sh --no-service` installs only the command, without the background service. Start it yourself with `screenshot-search --serve`.

`./uninstall.sh` removes the service and venv and keeps `~/.screenshot-search`. `./uninstall.sh --all` deletes the index and settings too. It never touches screenshots.

### macOS privacy permission (important)

A service started by launchd does **not** inherit Terminal's file permissions. Without Full Disk Access, it can't read Desktop, Documents, Downloads or anything under `~/Library/CloudStorage` (OneDrive, iCloud). ScreenFind detects this: the status bar shows **"⚠ macOS is blocking folder access — click to fix"**, and the folder card in ⚙ Settings shows the fix. Affected files are *not* marked as OCR errors; they're simply retried once access works.

1. In ⚙ Settings, click **Open settings & show file**. System Settings → Full Disk Access opens, and Finder highlights the program.
2. **Drag that file onto the list** and switch it on. Don't use the `+` button: its picker greys the file out, because Spotlight doesn't classify files under `/opt/homebrew`.
   - The file is the `bin/python3.x` launcher. For Homebrew that's `/opt/homebrew/Cellar/python@3.13/<version>/Frameworks/Python.framework/Versions/3.13/bin/python3.13`.
   - Do **not** add `Python.app`. macOS checks the program launchd started, not the one it re-executes into. This was confirmed in the `tccd` log.
3. Click **Restart ScreenFind** on the same card. macOS applies the grant only to newly started processes.

To see macOS's decision: `/usr/bin/log show --last 5m --predicate 'subsystem == "com.apple.TCC"' | grep -i -E "python|AllFiles"` (`authValue=0` = denied, `2` = allowed).

The grant is tied to that exact path, which contains the Python version. **After `brew upgrade python@3.13`, re-run `./install.sh` and repeat the grant.**

---

## Migrating to another Mac

**Option A — keep the index (no re-OCR):**

```bash
# old Mac
tar czf ~/screenfind-data.tgz --exclude .screenshot-search/venv -C ~ .screenshot-search
# new Mac: clone the repo, then
tar xzf screenfind-data.tgz -C ~
./install.sh
```

Paths in the DB are absolute. If the username or folder locations differ, rewrite them:

```bash
sqlite3 ~/.screenshot-search/index.db \
  "UPDATE screenshots SET path = REPLACE(path, '/Users/OLD', '/Users/NEW'), folder = REPLACE(folder, '/Users/OLD', '/Users/NEW')"
```

Then fix the same paths in `config.json` (`watched_folders`).

**Option B — fresh index:** clone, run `./install.sh`, and add folders. The first pass runs at roughly 2–3 screenshots per second on Apple Silicon.

---

## Using the UI

### Search syntax

The frontend rewrites what you type into safe FTS5 syntax:

| You type | Sent to FTS5 | Behaviour |
|---|---|---|
| `agen` | `agen*` | 3+ letters auto-prefix: agent, agency… |
| `expense card` | `"expense card*"` | phrase (adjacent words), prefix on the last word |
| `expense AND card` | unchanged | both words anywhere |
| `azure OR aws` | unchanged | either word |
| `azure NOT devops` | unchanged | exclude a word |
| `"power supply"` | unchanged | exact phrase |
| `jane.doe@example.com` | `"jane doe example com*"` | `.@/-` split into words |

Typing `AND`/`OR`/`NOT`, quotes, `*`, `^` or parentheses switches to advanced mode, which passes the input straight through. Results are always **newest first**.

### Folder filter

With two or more watched folders, a **Folders** bar appears under the search box: **All folders** plus one button per folder. Each shows how many screenshots match the current search in that folder. Click a button, or press `F` / `⇧F`, to switch. The choice is remembered. In "All folders", every result shows a small folder badge, and the preview shows the folder too. Folder names are the folder's base name; when two folders share a name, they're shown as `parent/name`.

### Watched folders (⚙ Settings)

Each folder is a card with:
- a status badge: **Watching**, **Indexing 44 / 86** (with a progress bar), **Queued**, **Folder not found**, or **Blocked by macOS**;
- counts of indexed files, errors and cloud-only files, plus when it was last indexed.

Every button explains itself on hover. The actions:

- **Reindex** — a quick catch-up. It OCRs only new or changed files, plus earlier failures and cloud-only files that have since downloaded. It takes seconds when nothing changed. New screenshots are indexed automatically, so it's rarely needed.
- **⋯ → Re-OCR every file** — re-reads every file in the folder, even unchanged ones, at about 0.4 s per file. Use it after changing OCR settings or if text looks wrong. Search keeps working while it runs. It can't fix damaged files or download cloud-only ones.
- **⋯ → Remove folder** — stops watching the folder and drops its rows from the index, after asking for confirmation. Files are never touched.
- **Add a folder** — paste a path or use **Browse…** (native Finder dialog), and optionally **Include subfolders** (hidden folders are skipped). The folder is validated: it must exist and be readable by the service. It's then saved, watched and indexed.
- **Reindex all folders** — runs Reindex for every folder.

Jobs run one at a time; others show "Queued". The panel refreshes every 2 s while open. A folder that isn't available at startup (e.g. OneDrive still mounting) is retried every minute and indexed when it appears.

### Keyboard shortcuts

| Key | Action |
|---|---|
| any letter | focus the search box |
| `↑` `↓` | move selection (mouse hover never changes it) |
| `Enter` / `⌘ Enter` | open the file / reveal in Finder |
| `C` | copy the image to the clipboard |
| `Del` / `Backspace` | move the file to the Trash and remove it from the index |
| `F` / `⇧F` | next / previous folder filter |
| `H` | toggle in-image highlights |
| `T` | cycle the 7 colour themes (or click a colour dot) |
| `/` | jump to search · `Esc` clear / close · `?` help |

---

## Configuration

`~/.screenshot-search/config.json` is created on first run. Missing keys are added automatically, and writes are atomic.

| Key | Default | Notes |
|---|---|---|
| `watched_folders` | macOS screenshot folder | `[{"path": "...", "recursive": false}, …]`. Managed from ⚙ Settings |
| `watched_folder` | (mirror) | Legacy key, mirrors the first folder. Old single-folder configs are migrated |
| `db_path` | `~/.screenshot-search/index.db` | |
| `port` | `5123` | After a restart the server waits up to 10 s for this port, then tries the next 9 |
| `log_level` | `WARNING` | `INFO` for verbose logs |
| `ocr_language_correction` | `true` | Vision language correction |
| `ocr_recognition_level` | `accurate` | `fast` is 3–5× quicker but less accurate. Re-OCR afterwards |
| `thumbnail_cache_size` | `500` | in-memory JPEG thumbnail cache |
| `default_result_limit` | `50` | CLI result limit |
| `default_theme` | `warm` | first visit only. The UI remembers your choice |
| `stop_words` | (list) | words dropped from the index |

After editing by hand, restart the service.

---

## Architecture

| File | Role |
|---|---|
| `cli.py` | argparse entry point: search, `--serve`, `--reindex [--full] [--folder]`, `--watch`, `--prune`, `--stats` |
| `config.py` | load/migrate/save config, folder normalisation, `python_app_path()` (the program needing Full Disk Access) |
| `indexer.py` | schema + migrations, FTS5 search with folder filter and per-folder counts, bulk and single-file indexing, access-denied detection |
| `ocr_worker_proc.py` | long-lived Vision OCR subprocess speaking JSON lines over stdin/stdout. Returns text and per-word boxes |
| `server.py` | FastAPI app, reindex job queue (one worker, de-duplicated), folder management, restart, static files (`Cache-Control: no-cache`) |
| `watcher.py` | one FSEvents observer for all folders, 500 ms debounce, re-attaches folders that appear later, hourly retry of pending files |
| `static/` | `index.html`, `style.css`, `app.js` — single-page UI with CSS-variable themes |

**Why one OCR subprocess?** Apple's Neural Engine serialises Vision requests, so a process pool gave no speedup. It did add noisy `BrokenPipeError` shutdowns and a brittle pyobjc/multiprocessing/uvicorn mix. The worker respawns every 250 files to bound memory, and each file has a 60 s timeout.

**Row status:** `ok` (indexed) · `pending` (OneDrive cloud-only placeholder, detected via `st_blocks == 0`, retried hourly) · `error` (Vision couldn't read it, e.g. 0-byte or 2×2 px files) · `deleted` (removed from disk, cleared by `--prune`).

**Database:** SQLite in WAL mode.
```
screenshots(id, path UNIQUE, filename, relative_path, folder, file_mtime, file_size,
            ocr_text, ocr_text_raw, ocr_boxes, indexed_at, status)
screenshots_fts(filename, ocr_text)   -- FTS5 external-content table, kept in sync by triggers
```
`ocr_boxes` holds Vision's normalised word rectangles (origin bottom-left). The UI uses them to draw highlights on the image.

**Restart:** `POST /api/restart` exits the process, and launchd's `KeepAlive` starts a new one within ~10 s (`ThrottleInterval`). It's only offered when running under launchd.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Page doesn't load | `launchctl list \| grep screenfind`. If listed, check the log. If not, re-run `./install.sh` |
| UI on 5124 instead of 5123 | another server held 5123 for over 10 s. `pkill -f "screenshot-search --serve"`, then kickstart |
| ⚠ "macOS is blocking folder access", or log shows `PermissionError` / `cannot decode image` | Full Disk Access — see above |
| `Errno 11 Resource deadlock avoided` | repo or venv inside a synced folder. Move it, then re-run `./install.sh` |
| Service won't start after `brew upgrade` | the venv's Python was removed. Re-run `./install.sh`, then re-grant Full Disk Access |
| No highlights on older screenshots | they were indexed before v1.2 (word boxes weren't saved). ⋯ → Re-OCR every file |
| A folder shows "Folder not found" | unmounted drive or OneDrive not ready. It's picked up automatically once it appears |
| Search finds nothing for 1–2 letters | prefix search starts at 3 letters |
| `pgrep -l screenshot-search` shows nothing | the process is named `Python`. Use `pgrep -lf screenshot-search` |

---

## API reference

All endpoints are on `http://localhost:5123`. There is no authentication; the server binds to `127.0.0.1` only. Requests whose `Host` header isn't `localhost`/`127.0.0.1`/`[::1]` are refused (DNS-rebinding guard), and so are state-changing requests that carry a foreign `Origin` header (cross-site guard). File paths are passed to AppleScript as arguments, never interpolated into the script.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/search?q=&limit=50&offset=0&folder=` | `{query, total, all_total, folders[{path,label,count}], results[{id,path,filename,folder,folder_label,file_mtime,file_size,excerpt}], recent?}`. `q` is FTS5 syntax, and an empty `q` lists the newest first |
| GET | `/api/image/{id}` · `/thumb` · `/boxes` | original image · 320×240 JPEG · `{boxes:[{text,x,y,w,h}]}` |
| GET | `/api/open/{id}` · `/api/open-finder/{id}` | open in the default app · reveal in Finder |
| POST | `/api/copy-image/{id}` | copy to the clipboard as PNG |
| DELETE | `/api/file/{id}` | move to the Trash and delete the row |
| GET | `/api/stats` | totals, `last_indexed_at`, `reindex_running`, `access_denied{folder: program}` |
| POST | `/api/reindex?full=false` | queue a reindex of all folders (202) |
| GET | `/api/folders` | `{folders[{path,recursive,exists,watching,indexed,errors,pending,last_indexed_at,reindexing,access_denied}], progress{done,total,current}, python, can_restart}` |
| POST | `/api/folders` | body `{path, recursive}`. Returns 400 if invalid or blocked, 409 if a duplicate |
| DELETE | `/api/folders?path=` | stop watching and drop the folder's rows |
| POST | `/api/folders/reindex?path=&full=false` | queue one folder |
| POST | `/api/pick-folder` | native folder chooser, returning `{path}` |
| POST | `/api/open-fda-settings` | open Full Disk Access and reveal the program |
| POST | `/api/restart` | restart under launchd (409 otherwise) |

---

## Not implemented

- Semantic/vector search
- Windows/Linux (no Apple Vision there)
- Automated tests
- Cloud sync of the index
