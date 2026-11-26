import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import normalize_folders, python_app_path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screenshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT    NOT NULL UNIQUE,
    filename      TEXT    NOT NULL,
    relative_path TEXT    NOT NULL,
    file_mtime    REAL    NOT NULL,
    file_size     INTEGER NOT NULL,
    ocr_text      TEXT,
    ocr_text_raw  TEXT,
    ocr_boxes     TEXT,
    indexed_at    REAL    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'ok'
);

CREATE VIRTUAL TABLE IF NOT EXISTS screenshots_fts USING fts5(
    filename,
    ocr_text,
    content = screenshots,
    content_rowid = id,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS screenshots_ai AFTER INSERT ON screenshots BEGIN
    INSERT INTO screenshots_fts(rowid, filename, ocr_text)
    VALUES (new.id, new.filename, new.ocr_text);
END;

CREATE TRIGGER IF NOT EXISTS screenshots_au AFTER UPDATE ON screenshots BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, filename, ocr_text)
    VALUES ('delete', old.id, old.filename, old.ocr_text);
    INSERT INTO screenshots_fts(rowid, filename, ocr_text)
    VALUES (new.id, new.filename, new.ocr_text);
END;

CREATE TRIGGER IF NOT EXISTS screenshots_ad AFTER DELETE ON screenshots BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, filename, ocr_text)
    VALUES ('delete', old.id, old.filename, old.ocr_text);
END;

CREATE INDEX IF NOT EXISTS idx_screenshots_mtime  ON screenshots(file_mtime DESC);
CREATE INDEX IF NOT EXISTS idx_screenshots_status ON screenshots(status);
"""

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# Respawn the OCR subprocess after this many files to bound memory growth in Vision/pyobjc.
_OCR_WORKER_RESPAWN_AFTER = 250
# Per-file OCR wall-clock cap. Above this we kill the worker and mark the file as error.
_OCR_TIMEOUT_SECONDS = 60


class _OCRClient:
    """Persistent stdin/stdout pipe to a single OCR worker subprocess."""

    def __init__(self, use_language_correction: bool, recognition_level: str):
        self.use_language_correction = use_language_correction
        self.recognition_level = recognition_level
        self._proc: Optional[subprocess.Popen] = None
        self._files_done = 0
        self._lock = threading.Lock()

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "screenshot_search.ocr_worker_proc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._files_done = 0

    def _ensure_alive(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()

    def ocr(self, path: str) -> dict:
        """Run OCR on `path`. Returns dict {ok, ...} matching worker protocol."""
        with self._lock:
            self._ensure_alive()
            if self._files_done >= _OCR_WORKER_RESPAWN_AFTER:
                self._shutdown()
                self._spawn()

            msg = json.dumps({
                "path": path,
                "lang": self.use_language_correction,
                "level": self.recognition_level,
            })
            try:
                assert self._proc and self._proc.stdin and self._proc.stdout
                self._proc.stdin.write(msg + "\n")
                self._proc.stdin.flush()
                line = _readline_with_timeout(self._proc.stdout, _OCR_TIMEOUT_SECONDS)
            except (BrokenPipeError, OSError) as exc:
                self._kill()
                return {"ok": False, "path": path, "error": f"worker pipe died: {exc}"}

            if line is None:
                self._kill()
                return {"ok": False, "path": path, "error": "worker timed out"}
            if not line:
                self._kill()
                return {"ok": False, "path": path, "error": "worker exited unexpectedly"}

            try:
                result = json.loads(line)
            except json.JSONDecodeError as exc:
                return {"ok": False, "path": path, "error": f"bad worker response: {exc}"}

            self._files_done += 1
            return result

    def _shutdown(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                try:
                    self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    self._proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()
        finally:
            self._proc = None

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass
        finally:
            self._proc = None

    def close(self) -> None:
        with self._lock:
            self._shutdown()


def _readline_with_timeout(stream, timeout: float) -> Optional[str]:
    """Read one line from a pipe, returning None on timeout. Uses select on the fd."""
    import select
    end = time.time() + timeout
    buf = []
    fd = stream.fileno()
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, 65536)
        if not chunk:
            return "".join(buf) if buf else ""
        text = chunk.decode("utf-8", errors="replace")
        nl = text.find("\n")
        if nl >= 0:
            buf.append(text[: nl + 1])
            # Anything past the newline shouldn't happen with our line protocol; ignore.
            return "".join(buf)
        buf.append(text)


class Indexer:
    def __init__(self, config: dict):
        self.config = config
        normalize_folders(self.config)
        self.db_path = config["db_path"]
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._ocr_client: Optional[_OCRClient] = None
        self._ocr_client_lock = threading.Lock()
        self.stop_event = threading.Event()
        # Live progress of the running bulk job (read by the web UI).
        self.progress: dict = {}
        # Folders where macOS refused file access (privacy / TCC), keyed by folder path.
        self.access_denied: dict[str, str] = {}

    # -- DB helpers ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(screenshots)")}
            if "folder" not in cols:
                conn.execute("ALTER TABLE screenshots ADD COLUMN folder TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_screenshots_folder ON screenshots(folder)")
            orphans = conn.execute("SELECT id, path FROM screenshots WHERE folder IS NULL").fetchall()
            if orphans:
                updates = [(self._folder_for(r["path"]), r["id"]) for r in orphans]
                conn.executemany(
                    "UPDATE screenshots SET folder=? WHERE id=?",
                    [(f, i) for f, i in updates if f],
                )

    # -- Watched folders -----------------------------------------------------

    def folders(self) -> list[dict]:
        """Configured folders as [{'path': str, 'recursive': bool}, ...]."""
        return self.config.get("watched_folders", [])

    def _folder_for(self, path: str) -> Optional[str]:
        """Most specific configured folder that contains `path` (None if none does)."""
        best: Optional[str] = None
        candidates = {path, os.path.realpath(path)}
        for entry in self.folders():
            root = entry["path"]
            for base in {root, os.path.realpath(root)}:
                for cand in candidates:
                    if cand.startswith(base + os.sep) and (best is None or len(root) > len(best)):
                        # Non-recursive folders only own direct children.
                        if not entry.get("recursive") and os.sep in cand[len(base) + 1:]:
                            continue
                        best = root
        return best

    def _list_images(self, entry: dict) -> list[str]:
        root = entry["path"]
        if not entry.get("recursive"):
            return [
                str(p) for p in Path(root).iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            ]
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.startswith(".") and os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                    found.append(os.path.join(dirpath, name))
        return found

    @staticmethod
    def _can_read(path: str) -> bool:
        """False only when the OS refuses to open the file (e.g. macOS privacy block)."""
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            return True
        except PermissionError:
            return False
        except OSError:
            return True  # vanished/other I/O problems are handled by the normal OCR path

    # -- OCR client lifecycle ------------------------------------------------

    def _get_ocr_client(self) -> _OCRClient:
        with self._ocr_client_lock:
            if self._ocr_client is None:
                self._ocr_client = _OCRClient(
                    use_language_correction=self.config.get("ocr_language_correction", True),
                    recognition_level=self.config.get("ocr_recognition_level", "accurate"),
                )
            return self._ocr_client

    def shutdown(self) -> None:
        """Signal any running bulk job to stop and tear down the OCR worker."""
        self.stop_event.set()
        with self._ocr_client_lock:
            if self._ocr_client is not None:
                self._ocr_client.close()
                self._ocr_client = None

    # -- Stop words ----------------------------------------------------------

    def _strip_stop_words(self, text: str) -> str:
        stop_words = set(self.config.get("stop_words", []))
        words = re.findall(r"\w+", text.lower())
        return " ".join(w for w in words if w not in stop_words)

    # -- OneDrive availability -----------------------------------------------

    @staticmethod
    def _is_locally_available(path: str) -> bool:
        """Cheap check: cloud-only files have no allocated disk blocks. Does NOT trigger a download."""
        try:
            st = os.stat(path)
            return st.st_size > 0 and st.st_blocks > 0
        except OSError:
            return False

    def _ensure_local(self, path: str) -> bool:
        if self._is_locally_available(path):
            return True
        try:
            subprocess.Popen(
                ["xattr", "-p", "com.apple.metadata:kMDItemWhereFroms", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        return False

    # -- Single-file index ---------------------------------------------------

    def index_file(self, path: str, force: bool = False) -> str:
        """Index one file. Returns: 'ok', 'error', 'pending', 'skipped', 'denied'."""
        path = str(Path(path).resolve())
        root = self._folder_for(path) or str(Path(path).parent)

        try:
            stat = os.stat(path)
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            logger.error("Cannot stat %s", path)
            return "error"

        if not force:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT file_mtime FROM screenshots WHERE path = ?", (path,)
                ).fetchone()
                if row and abs(row["file_mtime"] - mtime) < 0.01:
                    return "skipped"

        if not self._ensure_local(path):
            self._upsert(path, root, mtime, size, None, None, None, "pending")
            return "pending"

        if not self._can_read(path):
            self._note_denied(root)
            return "denied"
        self.access_denied.pop(root, None)

        result = self._get_ocr_client().ocr(path)
        if result.get("ok"):
            ocr_raw = result.get("ocr_text_raw", "")
            ocr_text = self._strip_stop_words(ocr_raw)
            ocr_boxes = json.dumps(result.get("boxes", []))
            self._upsert(path, root, mtime, size, ocr_text, ocr_raw, ocr_boxes, "ok")
            return "ok"
        else:
            logger.warning("OCR failed for %s: %s", path, result.get("error"))
            self._upsert(path, root, mtime, size, None, None, None, "error")
            return "error"

    def _note_denied(self, root: str) -> None:
        if root not in self.access_denied:
            logger.warning(
                "macOS denied access to files in %s. Grant Full Disk Access to %s "
                "(System Settings > Privacy & Security).", root, python_app_path(),
            )
        self.access_denied[root] = python_app_path()

    def _upsert(self, path, root, mtime, size, ocr_text, ocr_raw, ocr_boxes, status):
        filename = os.path.basename(path)
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = filename

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO screenshots
                    (path, filename, relative_path, file_mtime, file_size,
                     ocr_text, ocr_text_raw, ocr_boxes, indexed_at, status, folder)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    relative_path=excluded.relative_path,
                    file_mtime=excluded.file_mtime,
                    file_size=excluded.file_size,
                    ocr_text=excluded.ocr_text,
                    ocr_text_raw=excluded.ocr_text_raw,
                    ocr_boxes=excluded.ocr_boxes,
                    indexed_at=excluded.indexed_at,
                    status=excluded.status,
                    folder=excluded.folder
                """,
                (path, filename, rel, mtime, size, ocr_text, ocr_raw, ocr_boxes, time.time(), status, root),
            )

    def mark_deleted(self, path: str) -> None:
        path = str(Path(path).resolve())
        with self._connect() as conn:
            conn.execute(
                "UPDATE screenshots SET status='deleted' WHERE path=?", (path,)
            )

    # -- Bulk index (sequential, single long-lived OCR subprocess) ----------

    def run_bulk_index(
        self,
        force: bool = False,
        show_progress: bool = True,
        paths: Optional[list] = None,
        folder: Optional[str] = None,
    ) -> dict:
        """Index watched folders. `folder` limits the scan to one configured folder;
        `paths` indexes exactly those files (used for retrying pending ones)."""
        from rich.progress import Progress, BarColumn, TimeRemainingColumn, MofNCompleteColumn

        self.stop_event.clear()

        # (path, root-folder) pairs to consider
        all_files: list[tuple[str, str]] = []
        if paths is not None:
            for p in paths:
                rp = str(Path(p).resolve())
                all_files.append((rp, self._folder_for(rp) or str(Path(rp).parent)))
        else:
            entries = [e for e in self.folders() if folder is None or e["path"] == folder]
            for entry in entries:
                try:
                    all_files.extend((f, entry["path"]) for f in self._list_images(entry))
                except FileNotFoundError:
                    logger.warning("Folder not found (unmounted?): %s", entry["path"])
                except PermissionError:
                    self._note_denied(entry["path"])

        with self._connect() as conn:
            indexed_mtimes = {
                row["path"]: row["file_mtime"]
                for row in conn.execute(
                    "SELECT path, file_mtime FROM screenshots WHERE status NOT IN ('deleted','pending','error')"
                )
            }

        to_process: list[tuple[str, str]] = []
        cloud_only: list[tuple] = []
        already_done = 0
        for f, root in all_files:
            try:
                st = os.stat(f)
                mtime = st.st_mtime
            except OSError:
                continue
            if not force and f in indexed_mtimes and abs(indexed_mtimes[f] - mtime) < 0.01:
                already_done += 1
                continue
            if st.st_size > 0 and st.st_blocks == 0:
                cloud_only.append((f, root, mtime, st.st_size))
            else:
                to_process.append((f, root))

        if cloud_only:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO screenshots
                        (path, filename, relative_path, file_mtime, file_size,
                         ocr_text, ocr_text_raw, ocr_boxes, indexed_at, status, folder)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 'pending', ?)
                    ON CONFLICT(path) DO UPDATE SET
                        file_mtime=excluded.file_mtime,
                        file_size=excluded.file_size,
                        indexed_at=excluded.indexed_at,
                        status='pending',
                        folder=excluded.folder
                    """,
                    [
                        (p, os.path.basename(p), os.path.relpath(p, root), mt, sz, time.time(), root)
                        for p, root, mt, sz in cloud_only
                    ],
                )

        total = len(all_files)
        if show_progress:
            print(
                f"Found {total:,} images. Already indexed: {already_done:,}. "
                f"Cloud-only (pending): {len(cloud_only):,}. To OCR now: {len(to_process):,}."
            )

        stats = {"ok": 0, "error": 0, "pending": len(cloud_only), "skipped": already_done, "denied": 0}

        if not to_process:
            if show_progress:
                print("Nothing to OCR. Cloud-only files will be retried hourly.")
            return stats

        client = self._get_ocr_client()
        start_time = time.time()
        self.progress = {"folder": folder, "done": 0, "total": len(to_process)}

        progress_cols = [
            "[progress.description]{task.description}",
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
        ]

        denied_roots: set[str] = set()
        try:
            with Progress(*progress_cols, disable=not show_progress) as progress:
                task_id = progress.add_task("Indexing", total=len(to_process))

                for path, root in to_process:
                    if self.stop_event.is_set():
                        logger.info("Bulk index stop requested — exiting after %d files.", stats["ok"] + stats["error"])
                        break

                    self.progress["done"] += 1
                    self.progress["current"] = root
                    progress.advance(task_id)

                    # Once macOS has blocked a folder, don't grind through the rest of it.
                    if root in denied_roots:
                        stats["denied"] += 1
                        continue

                    try:
                        stat = os.stat(path)
                        mtime, size = stat.st_mtime, stat.st_size
                    except OSError:
                        stats["error"] += 1
                        continue

                    if not self._can_read(path):
                        self._note_denied(root)
                        denied_roots.add(root)
                        stats["denied"] += 1
                        continue
                    self.access_denied.pop(root, None)

                    result = client.ocr(path)
                    try:
                        if result.get("ok"):
                            ocr_raw = result.get("ocr_text_raw", "")
                            ocr_text = self._strip_stop_words(ocr_raw)
                            ocr_boxes = json.dumps(result.get("boxes", []))
                            self._upsert(path, root, mtime, size, ocr_text, ocr_raw, ocr_boxes, "ok")
                            stats["ok"] += 1
                        else:
                            logger.warning("OCR failed for %s: %s", os.path.basename(path), result.get("error"))
                            self._upsert(path, root, mtime, size, None, None, None, "error")
                            stats["error"] += 1
                    except sqlite3.OperationalError as db_exc:
                        logger.warning("DB upsert failed for %s: %s — skipping", path, db_exc)
                        stats["error"] += 1
        finally:
            self.progress = {}

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        msg = (
            f"Indexed: {stats['ok']:,}. Errors: {stats['error']:,}. "
            f"Pending: {stats['pending']:,}. Denied: {stats['denied']:,}. Time: {mins}m {secs:02d}s."
        )
        if show_progress:
            print(f"Done. {msg}")
        else:
            logger.info("Background reindex done. %s", msg)
        return stats

    # -- Retry pending files -------------------------------------------------

    def retry_pending(self) -> int:
        with self._connect() as conn:
            pending = [
                row["path"]
                for row in conn.execute("SELECT path FROM screenshots WHERE status='pending'")
            ]
        now_local = [p for p in pending if Path(p).exists() and self._is_locally_available(p)]
        if not now_local:
            return 0
        return self.run_bulk_index(force=True, show_progress=False, paths=now_local)["ok"]

    # -- Prune deleted -------------------------------------------------------

    def prune(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM screenshots WHERE status='deleted'")
            return cur.rowcount

    # -- Stats ---------------------------------------------------------------

    def stats(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(status='ok')      AS total_indexed,
                    SUM(status='error')   AS total_errors,
                    SUM(status='pending') AS total_pending,
                    SUM(status='deleted') AS total_deleted,
                    MAX(indexed_at)       AS last_indexed_at
                FROM screenshots
                """
            ).fetchone()

        db_size_mb = round(Path(self.db_path).stat().st_size / 1_048_576, 1) if Path(self.db_path).exists() else 0.0

        import datetime
        last_at = None
        if row["last_indexed_at"]:
            last_at = datetime.datetime.utcfromtimestamp(row["last_indexed_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "total_indexed": row["total_indexed"] or 0,
            "total_errors": row["total_errors"] or 0,
            "total_pending": row["total_pending"] or 0,
            "total_deleted": row["total_deleted"] or 0,
            "db_size_mb": db_size_mb,
            "last_indexed_at": last_at,
            "watched_folder": self.config.get("watched_folder", ""),
            "folder_count": len(self.folders()),
        }

    def folder_stats(self) -> list[dict]:
        """Per-folder counts for every configured folder (zeros if nothing indexed yet)."""
        with self._connect() as conn:
            rows = {
                r["folder"]: r
                for r in conn.execute(
                    """
                    SELECT folder,
                           SUM(status='ok')      AS indexed,
                           SUM(status='error')   AS errors,
                           SUM(status='pending') AS pending,
                           MAX(indexed_at)       AS last_indexed_at
                    FROM screenshots
                    WHERE status != 'deleted'
                    GROUP BY folder
                    """
                )
            }

        import datetime
        out = []
        for entry in self.folders():
            r = rows.get(entry["path"])
            last = r["last_indexed_at"] if r else None
            out.append({
                "path": entry["path"],
                "recursive": bool(entry.get("recursive")),
                "exists": os.path.isdir(entry["path"]),
                "indexed": (r["indexed"] or 0) if r else 0,
                "errors": (r["errors"] or 0) if r else 0,
                "pending": (r["pending"] or 0) if r else 0,
                "last_indexed_at": (
                    datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else None
                ),
                "access_denied": self.access_denied.get(entry["path"]),
            })
        return out

    def purge_folder(self, folder: str) -> int:
        """Remove every index row belonging to `folder` (files on disk are untouched)."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM screenshots WHERE folder = ?", (folder,))
            return cur.rowcount

    # -- Search --------------------------------------------------------------

    def folder_labels(self) -> dict[str, str]:
        """Short display name per configured folder: its basename, or 'parent/basename'
        when two folders share a basename."""
        paths = [f["path"] for f in self.folders()]
        base = {p: os.path.basename(p.rstrip("/")) or p for p in paths}
        dupes = {b for b in base.values() if list(base.values()).count(b) > 1}
        return {
            p: (os.path.join(os.path.basename(os.path.dirname(p)), base[p]) if base[p] in dupes else base[p])
            for p in paths
        }

    def search(self, query: str, limit: int = 50, offset: int = 0, folder: Optional[str] = None) -> dict:
        """FTS search (or most-recent list when query is empty), newest first.

        `folder` limits results to one watched folder. The response always includes
        per-folder match counts for the *unfiltered* query, so the UI can show them
        on the folder filter buttons.
        """
        fts = bool(query.strip())
        base_from = (
            "FROM screenshots_fts JOIN screenshots s ON s.id = screenshots_fts.rowid "
            "WHERE screenshots_fts MATCH ? AND s.status = 'ok'"
            if fts else
            "FROM screenshots s WHERE s.status = 'ok'"
        )
        base_args: list = [query] if fts else []
        folder_sql, folder_args = ("", [])
        if folder:
            folder_sql, folder_args = (" AND s.folder = ?", [folder])

        excerpt = (
            "snippet(screenshots_fts, 1, '<mark>', '</mark>', '…', 25) AS excerpt, bm25(screenshots_fts) AS rank"
            if fts else "NULL AS excerpt, 0.0 AS rank"
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT s.id, s.path, s.filename, s.relative_path, s.file_mtime, s.file_size,
                           s.folder, {excerpt}
                    {base_from}{folder_sql}
                    ORDER BY s.file_mtime DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*base_args, *folder_args, limit, offset],
                ).fetchall()
                counts = {
                    r[0]: r[1]
                    for r in conn.execute(f"SELECT s.folder, COUNT(*) {base_from} GROUP BY s.folder", base_args)
                }
        except sqlite3.OperationalError as exc:
            raise ValueError(str(exc)) from exc

        labels = self.folder_labels()
        all_total = sum(counts.values())
        result = {
            "query": query,
            "total": counts.get(folder, 0) if folder else all_total,
            "all_total": all_total,
            "folders": [
                {"path": p, "label": labels[p], "count": counts.get(p, 0)} for p in labels
            ],
            "results": [{**dict(r), "folder_label": labels.get(r["folder"], "")} for r in rows],
        }
        if not fts:
            result["recent"] = True
        return result
