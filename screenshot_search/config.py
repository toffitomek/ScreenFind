import json
import os
import sys
import tempfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".screenshot-search"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOGS_DIR = CONFIG_DIR / "logs"

DEFAULT_STOP_WORDS = [
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "this", "that",
    "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them", "my", "your", "his", "its", "our",
    "their", "what", "which", "who", "file", "edit", "view", "insert",
    "format", "tools", "window", "help", "ok", "cancel", "yes", "no",
    "new", "open", "save", "close", "print", "copy", "paste", "undo", "redo",
]

def _default_screenshot_folder() -> str:
    """Where macOS saves screenshots (System Settings / ⌘⇧5 Options), else ~/Desktop."""
    try:
        import subprocess
        out = subprocess.run(
            ["defaults", "read", "com.apple.screencapture", "location"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out and os.path.isdir(os.path.expanduser(out)):
            return os.path.abspath(os.path.expanduser(out))
    except Exception:
        pass
    return str(Path.home() / "Desktop")


_DEFAULT_FOLDER = _default_screenshot_folder()

DEFAULT_CONFIG: dict = {
    # "watched_folders" is the source of truth; "watched_folder" mirrors the first
    # entry for backward compatibility (CLI, older tooling).
    "watched_folder": _DEFAULT_FOLDER,
    "watched_folders": [{"path": _DEFAULT_FOLDER, "recursive": False}],
    "db_path": str(CONFIG_DIR / "index.db"),
    "port": 5123,
    "port_auto_advance": True,
    "log_level": "WARNING",
    "show_relative_path": False,
    "onedrive_download_timeout_seconds": 30,
    "ocr_language_correction": True,
    "thumbnail_cache_size": 500,
    "default_result_limit": 50,
    "default_theme": "warm",
    "stop_words": DEFAULT_STOP_WORDS,
}


def python_app_path() -> str:
    """The program to allow under Full Disk Access.

    macOS attributes file access to the *responsible process*: the executable launchd
    started (bin/python3.x). Homebrew's launcher then re-executes into
    Python.app/Contents/MacOS/Python, but adding that one does NOT help (verified in the
    tccd log: subject = bin/python3.x, result denied).
    """
    return os.path.realpath(sys.executable)


def normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path).strip())).rstrip("/") or "/"


def normalize_folders(config: dict) -> bool:
    """Make config['watched_folders'] a clean list of {path, recursive} dicts.

    Migrates the legacy single 'watched_folder' string, drops duplicates, and keeps
    'watched_folder' mirroring the first entry. Returns True if config changed.
    """
    before = json.dumps([config.get("watched_folder"), config.get("watched_folders")], sort_keys=True)

    raw = config.get("watched_folders")
    if not raw:
        legacy = config.get("watched_folder")
        raw = [{"path": legacy, "recursive": False}] if legacy else []

    folders: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"path": item, "recursive": False}
        path = normalize_path(item.get("path", ""))
        if not item.get("path") or path in seen:
            continue
        seen.add(path)
        folders.append({"path": path, "recursive": bool(item.get("recursive", False))})

    config["watched_folders"] = folders
    config["watched_folder"] = folders[0]["path"] if folders else ""

    after = json.dumps([config.get("watched_folder"), config.get("watched_folders")], sort_keys=True)
    return before != after


def save_config(config: dict) -> None:
    """Atomically write config to disk (temp file + rename)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Config created at {CONFIG_PATH} — please verify the watched folder (or add one in the web UI).")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with open(CONFIG_PATH) as f:
        data = json.load(f)

    # Migrate/clean folders first so the default folder never overrides a custom legacy one.
    updated = normalize_folders(data)
    for key, value in DEFAULT_CONFIG.items():
        if key not in data:
            data[key] = value
            updated = True

    if updated:
        save_config(data)

    # Resolve ~ in db_path
    data["db_path"] = str(Path(data["db_path"]).expanduser())
    return data
