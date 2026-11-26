import asyncio
import functools
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .config import load_config, normalize_path, python_app_path, save_config
from .indexer import IMAGE_EXTENSIONS, Indexer
from .watcher import Watcher

logger = logging.getLogger(__name__)

_indexer: Indexer | None = None
_watcher: Watcher | None = None
_config: dict = {}
_executor = ThreadPoolExecutor(max_workers=1)
_config_lock = threading.Lock()

# Reindex jobs queued or running, as (folder-or-"*", full) keys. The single-worker
# executor runs them one at a time; this set only de-duplicates identical requests.
_reindex_lock = threading.Lock()
_reindex_jobs: set[tuple[str, bool]] = set()


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------

def _make_thumbnail_cache(maxsize: int):
    @functools.lru_cache(maxsize=maxsize)
    def _get(path: str, mtime_int: int) -> bytes:
        img = Image.open(path)
        img.thumbnail((320, 240))
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    return _get


_thumbnail_cache = None  # initialised in lifespan


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

def _submit_reindex(folder: str | None = None, full: bool = False) -> bool:
    """Queue a bulk index (one folder, or all if folder is None). False if already queued."""
    key = (folder or "*", full)
    with _reindex_lock:
        if key in _reindex_jobs or ("*", full) in _reindex_jobs:
            return False
        _reindex_jobs.add(key)
    try:
        _executor.submit(_run_reindex, folder, full)
    except RuntimeError:  # executor already shut down
        with _reindex_lock:
            _reindex_jobs.discard(key)
        return False
    return True


def _run_reindex(folder: str | None = None, full: bool = False) -> None:
    key = (folder or "*", full)
    try:
        assert _indexer is not None
        _indexer.run_bulk_index(force=full, show_progress=False, folder=folder)
    except Exception:
        logger.exception("Reindex failed (folder=%s)", folder or "all")
    finally:
        with _reindex_lock:
            _reindex_jobs.discard(key)


def _folder_busy(path: str) -> bool:
    with _reindex_lock:
        return any(f in (path, "*") for f, _ in _reindex_jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _indexer, _watcher, _config, _thumbnail_cache

    _config = load_config()
    _indexer = Indexer(_config)
    _thumbnail_cache = _make_thumbnail_cache(_config.get("thumbnail_cache_size", 500))

    _watcher = Watcher(_config, _indexer, on_folder_available=lambda p: _submit_reindex(p))
    _watcher.start()

    # Auto-start a background reindex to catch up on new/pending files at startup
    _submit_reindex()

    yield

    # Signal the background reindex to stop and tear down the OCR worker subprocess.
    _indexer.shutdown()
    _watcher.stop()
    _executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(lifespan=lifespan)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _hostname(value: str) -> str:
    """'localhost:5123' -> 'localhost'; '[::1]:5123' -> '[::1]'; 'http://x:1' -> 'x'."""
    value = value.split("://", 1)[-1].split("/", 1)[0].lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


@app.middleware("http")
async def _local_only(request: Request, call_next):
    """Only answer pages served from this machine. Blocks DNS-rebinding (foreign Host
    header) and other websites triggering actions via cross-site requests (Origin)."""
    if _hostname(request.headers.get("host", "")) not in _LOCAL_HOSTS:
        return Response("Forbidden host", status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and _hostname(origin) not in _LOCAL_HOSTS:
        return Response("Forbidden origin", status_code=403)
    return await call_next(request)


def _osascript(script: str, *args: str, **kw) -> subprocess.CompletedProcess:
    """Run AppleScript with values passed as arguments (never pasted into the script)."""
    lines = ["-e", "on run argv", *sum((["-e", l] for l in script.splitlines()), []), "-e", "end run"]
    return subprocess.run(["osascript", *lines, *args], capture_output=True, text=True, **kw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_row(id: int) -> dict:
    assert _indexer is not None
    import sqlite3
    conn = sqlite3.connect(_indexer.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM screenshots WHERE id=?", (id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def api_search(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    folder: str = Query(default=""),
):
    assert _indexer is not None
    try:
        result = _indexer.search(q, limit=limit, offset=offset, folder=folder or None)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": f"invalid query syntax: {exc}"},
        )
    return result


@app.get("/api/image/{id}")
async def api_image(id: int):
    row = _get_row(id)
    path = row["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not on disk")
    ext = Path(path).suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/api/image/{id}/thumb")
async def api_thumb(id: int):
    assert _thumbnail_cache is not None
    row = _get_row(id)
    path = row["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not on disk")
    mtime_int = int(os.path.getmtime(path) * 1000)
    try:
        data = _thumbnail_cache(path, mtime_int)
    except Exception as exc:
        logger.error("Thumbnail failed for %s: %s", path, exc)
        raise HTTPException(status_code=500, detail="Thumbnail generation failed")
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/image/{id}/boxes")
async def api_boxes(id: int):
    row = _get_row(id)
    boxes_json = row.get("ocr_boxes")
    boxes = json.loads(boxes_json) if boxes_json else []
    return {"id": id, "boxes": boxes}


@app.get("/api/open/{id}")
async def api_open(id: int):
    row = _get_row(id)
    subprocess.Popen(["open", row["path"]])
    return {"ok": True}


@app.get("/api/open-finder/{id}")
async def api_open_finder(id: int):
    row = _get_row(id)
    subprocess.Popen(["open", "-R", row["path"]])
    return {"ok": True}


@app.post("/api/copy-image/{id}")
async def api_copy_image(id: int):
    """Copy image file contents onto the macOS clipboard as a PNG."""
    row = _get_row(id)
    path = row["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not on disk")
    # AppleScript: read POSIX file as PNGf class, set clipboard.
    proc = _osascript('set the clipboard to (read POSIX file (item 1 of argv) as «class PNGf»)', path)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"clipboard copy failed: {proc.stderr.strip()}")
    return {"ok": True}


@app.delete("/api/file/{id}")
async def api_delete_file(id: int):
    """Move the file to macOS Trash and remove its index row."""
    assert _indexer is not None
    row = _get_row(id)
    path = row["path"]
    if os.path.exists(path):
        # AppleScript moves to Trash (recoverable), not unlink.
        proc = _osascript('tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)', path)
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"trash failed: {proc.stderr.strip()}")
    import sqlite3
    conn = sqlite3.connect(_indexer.db_path)
    conn.execute("DELETE FROM screenshots WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/stats")
async def api_stats():
    assert _indexer is not None
    s = _indexer.stats()
    s["server_port"] = _config.get("port", 5123)
    s["reindex_running"] = bool(_reindex_jobs)
    s["access_denied"] = dict(_indexer.access_denied)
    return s


@app.post("/api/reindex")
async def api_reindex(full: bool = False):
    if not _submit_reindex(None, full):
        return Response(status_code=202, content="Reindex already running")
    return Response(status_code=202)


# ---------------------------------------------------------------------------
# Watched folders
# ---------------------------------------------------------------------------

def _folders_payload() -> dict:
    assert _indexer is not None
    rows = _indexer.folder_stats()
    for row in rows:
        row["reindexing"] = _folder_busy(row["path"])
        row["watching"] = bool(_watcher and _watcher.is_watching(row["path"]))
    progress = dict(_indexer.progress)
    return {
        "folders": rows,
        "progress": progress or None,
        "python": python_app_path(),
        "can_restart": _managed_by_launchd(),
    }


def _probe_access(path: str, timeout: float = 5.0) -> str | None:
    """Try to list the folder and read one image. None = ok, else a short reason.
    Runs in a helper thread because a macOS privacy block can hang instead of failing."""
    result: dict = {}

    def work() -> None:
        try:
            for name in os.listdir(path):
                fp = os.path.join(path, name)
                if Path(name).suffix.lower() in IMAGE_EXTENSIONS and os.path.isfile(fp):
                    with open(fp, "rb") as fh:
                        fh.read(1)
                    break
        except PermissionError:
            result["err"] = "denied"
        except OSError as exc:
            result["err"] = str(exc)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return "timeout"
    return result.get("err")


@app.get("/api/folders")
async def api_folders():
    return _folders_payload()


@app.post("/api/folders")
async def api_add_folder(payload: dict = Body(...)):
    assert _indexer is not None and _watcher is not None
    raw = str(payload.get("path", "")).strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Folder path is required.")
    path = normalize_path(raw)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"Not a folder: {path}")

    with _config_lock:
        if any(f["path"] == path for f in _config["watched_folders"]):
            raise HTTPException(status_code=409, detail="That folder is already being watched.")

    problem = await asyncio.to_thread(_probe_access, path)
    if problem in ("denied", "timeout"):
        raise HTTPException(
            status_code=400,
            detail=(
                "macOS is blocking ScreenFind from reading this folder. Open System Settings > "
                "Privacy & Security > Full Disk Access, add this program, then restart ScreenFind: "
                f"{python_app_path()}"
            ),
        )
    if problem:
        raise HTTPException(status_code=400, detail=f"Cannot read folder: {problem}")

    with _config_lock:
        _config["watched_folders"].append({"path": path, "recursive": bool(payload.get("recursive", False))})
        _config["watched_folder"] = _config["watched_folders"][0]["path"]
        save_config(_config)
    _watcher.sync()
    _submit_reindex(path)
    return _folders_payload()


@app.delete("/api/folders")
async def api_remove_folder(path: str = Query(...)):
    """Stop watching a folder and drop its screenshots from the index (files stay on disk)."""
    assert _indexer is not None and _watcher is not None
    path = normalize_path(path)
    with _config_lock:
        remaining = [f for f in _config["watched_folders"] if f["path"] != path]
        if len(remaining) == len(_config["watched_folders"]):
            raise HTTPException(status_code=404, detail="Folder is not being watched.")
        _config["watched_folders"] = remaining
        _config["watched_folder"] = remaining[0]["path"] if remaining else ""
        save_config(_config)
    _watcher.sync()
    removed = _indexer.purge_folder(path)
    _indexer.access_denied.pop(path, None)
    payload = _folders_payload()
    payload["removed_from_index"] = removed
    return payload


@app.post("/api/folders/reindex")
async def api_reindex_folder(path: str = Query(...), full: bool = False):
    path = normalize_path(path)
    if not any(f["path"] == path for f in _config["watched_folders"]):
        raise HTTPException(status_code=404, detail="Folder is not being watched.")
    _submit_reindex(path, full)
    return Response(status_code=202)


def _managed_by_launchd() -> bool:
    return os.environ.get("XPC_SERVICE_NAME", "").startswith("com.screenfind")


@app.post("/api/restart")
async def api_restart():
    """Exit so launchd (KeepAlive) starts a fresh process — needed after granting
    Full Disk Access, which macOS only applies to newly started processes."""
    if not _managed_by_launchd():
        raise HTTPException(status_code=409, detail="Not running as a background service; restart it from Terminal.")

    def _exit() -> None:
        time.sleep(0.5)
        try:
            if _indexer:
                _indexer.shutdown()
        finally:
            os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True}


@app.post("/api/open-fda-settings")
async def api_open_fda_settings():
    """Open the Full Disk Access pane and reveal the program to allow in Finder."""
    program = python_app_path()
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
    subprocess.Popen(["open", "-R", program])
    return {"ok": True, "path": program}


@app.post("/api/pick-folder")
async def api_pick_folder():
    """Show the native macOS folder chooser (the server runs on the user's own Mac)."""
    script = (
        'tell current application\n'
        '  activate\n'
        '  return POSIX path of (choose folder with prompt "Choose a folder of screenshots to index")\n'
        'end tell'
    )
    try:
        proc = await asyncio.to_thread(
            subprocess.run, ["osascript", "-e", script], capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        return {"path": None}
    if proc.returncode != 0:
        if "-128" in proc.stderr or "canceled" in proc.stderr.lower():
            return {"path": None}
        raise HTTPException(status_code=500, detail=f"folder picker failed: {proc.stderr.strip()}")
    return {"path": normalize_path(proc.stdout.strip())}


class _RevalidatingStatic(StaticFiles):
    """Static files that browsers must revalidate (ETag) on every load, so UI updates
    appear immediately instead of being served from a stale cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Static files must be mounted last
_static_dir = Path(__file__).parent / "static"
app.mount("/", _RevalidatingStatic(directory=str(_static_dir), html=True), name="static")


# ---------------------------------------------------------------------------
# Port selection + startup helper
# ---------------------------------------------------------------------------

def _can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Same option uvicorn uses: lingering TIME_WAIT connections from the previous
        # process must not count as "port in use".
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_free_port(start: int, count: int = 10, wait_seconds: float = 10.0) -> int:
    """Prefer `start`. After a restart the previous process may still be releasing it,
    so wait briefly before falling back to the next ports (bookmarks and the browser
    keyword all point at the preferred port)."""
    deadline = time.monotonic() + wait_seconds
    while not _can_bind(start):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    else:
        return start
    for port in range(start + 1, start + count):
        if _can_bind(port):
            return port
    raise RuntimeError(
        f"Could not bind to any port in range {start}–{start + count - 1}. "
        f"Set a different port with --port."
    )


def serve(port: int = 5123, open_browser: bool = True) -> None:
    import uvicorn

    config = load_config()
    actual_port = find_free_port(port)
    if actual_port != port:
        print(f"Web UI available at http://localhost:{actual_port}")

    if open_browser:
        import threading, webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{actual_port}")).start()

    # Pass port into config for /api/stats
    config["port"] = actual_port

    uvicorn.run(
        "screenshot_search.server:app",
        host="127.0.0.1",
        port=actual_port,
        log_level=config.get("log_level", "WARNING").lower(),
        access_log=False,
        # Don't let open browser connections delay shutdown; a restart needs the port back fast.
        timeout_graceful_shutdown=3,
    )
