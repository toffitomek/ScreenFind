import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileMovedEvent, FileDeletedEvent
from watchdog.observers import Observer

from .indexer import Indexer, IMAGE_EXTENSIONS

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 0.5


class _Handler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer):
        self._indexer = indexer
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str, action: str) -> None:
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            return
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(_DEBOUNCE_SECONDS, self._run, args=(path, action))
            self._timers[path] = t
            t.daemon = True
            t.start()

    def _run(self, path: str, action: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        if action == "delete":
            logger.info("Marking deleted: %s", path)
            self._indexer.mark_deleted(path)
        else:
            logger.info("Indexing changed file: %s", path)
            self._indexer.index_file(path)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "index")

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "index")

    def on_moved(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "delete")
            self._schedule(event.dest_path, "index")

    def on_deleted(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "delete")


class Watcher:
    """Watches every folder in config['watched_folders'] with one shared observer."""

    _TICK_SECONDS = 60
    _PENDING_RETRY_SECONDS = 3600

    def __init__(self, config: dict, indexer: Indexer, on_folder_available=None):
        self._config = config
        self._indexer = indexer
        self._on_folder_available = on_folder_available  # called with folder path
        self._handler = _Handler(indexer)
        self._observer: Observer | None = None
        self._watches: dict[str, object] = {}   # folder path -> ObservedWatch
        self._lock = threading.Lock()
        self._pending_retry_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._observer = Observer()
        self._observer.start()
        self.sync()

        self._pending_retry_thread = threading.Thread(target=self._retry_loop, daemon=True)
        self._pending_retry_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join()

    # -- folder management ---------------------------------------------------

    def sync(self) -> list[str]:
        """Make the observer match config: attach new/missing folders, detach removed ones.
        Returns the folders that could not be attached (missing/unreadable)."""
        wanted = {f["path"]: bool(f.get("recursive")) for f in self._config.get("watched_folders", [])}
        failed: list[str] = []
        with self._lock:
            if self._observer is None:
                return list(wanted)
            for path in list(self._watches):
                if path not in wanted:
                    self._observer.unschedule(self._watches.pop(path))
                    logger.info("Stopped watching %s", path)
            for path, recursive in wanted.items():
                if path in self._watches:
                    continue
                if not os.path.isdir(path):
                    # FSEvents would happily "watch" a missing path; keep it pending so the
                    # retry loop attaches it (and triggers indexing) once it appears.
                    logger.warning("Folder not available yet: %s", path)
                    failed.append(path)
                    continue
                try:
                    self._watches[path] = self._observer.schedule(self._handler, path, recursive=recursive)
                    logger.info("Watching %s%s", path, " (recursive)" if recursive else "")
                except (OSError, FileNotFoundError) as exc:
                    logger.warning("Cannot watch %s yet: %s", path, exc)
                    failed.append(path)
        return failed

    def is_watching(self, path: str) -> bool:
        return path in self._watches

    def _retry_loop(self) -> None:
        since_pending = 0
        while not self._stop_event.wait(self._TICK_SECONDS):
            # Folders that weren't mounted at startup (e.g. OneDrive not ready yet) get picked up here.
            missing = [f["path"] for f in self._config.get("watched_folders", []) if f["path"] not in self._watches]
            if missing:
                self.sync()
                for path in missing:
                    if self.is_watching(path):
                        logger.info("Folder became available: %s", path)
                        if self._on_folder_available:
                            self._on_folder_available(path)

            since_pending += self._TICK_SECONDS
            if since_pending >= self._PENDING_RETRY_SECONDS:
                since_pending = 0
                count = self._indexer.retry_pending()
                if count:
                    logger.info("Retried %d pending files", count)
