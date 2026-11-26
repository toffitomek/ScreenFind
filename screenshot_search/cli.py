import argparse
import sys

from .config import load_config, normalize_path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="screenshot-search",
        description="OCR-powered screenshot search tool for macOS",
    )

    parser.add_argument("query", nargs="?", help="Search query (FTS5 syntax)")
    parser.add_argument("--serve", action="store_true", help="Start web UI + file watcher")
    parser.add_argument("--reindex", action="store_true", help="Reindex (skip up-to-date files)")
    parser.add_argument("--full", action="store_true", help="Force re-OCR every file (use with --reindex)")
    parser.add_argument("--folder", metavar="PATH", help="Limit --reindex to one watched folder")
    parser.add_argument("--watch", action="store_true", help="Run file watcher only")
    parser.add_argument("--prune", action="store_true", help="Remove deleted-file entries from index")
    parser.add_argument("--stats", action="store_true", help="Print index statistics")
    parser.add_argument("--port", type=int, default=None, help="Web server port (default: 5123)")
    parser.add_argument("--limit", type=int, default=None, help="Max results (default: 50)")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser when starting --serve")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    config = load_config()

    # --stats
    if args.stats:
        from .indexer import Indexer
        indexer = Indexer(config)
        s = indexer.stats()
        print(f"Total indexed : {s['total_indexed']:,}")
        print(f"Errors        : {s['total_errors']:,}")
        print(f"Pending       : {s['total_pending']:,}")
        print(f"Deleted       : {s['total_deleted']:,}")
        print(f"DB size       : {s['db_size_mb']} MB")
        print(f"Last indexed  : {s['last_indexed_at'] or 'never'}")
        print("Watched folders:")
        for f in indexer.folder_stats():
            flag = "" if f["exists"] else "  [NOT FOUND]"
            sub = " (+subfolders)" if f["recursive"] else ""
            print(f"  {f['path']}{sub}: {f['indexed']:,} indexed, {f['errors']:,} errors, {f['pending']:,} pending{flag}")
        return

    # --prune
    if args.prune:
        from .indexer import Indexer
        indexer = Indexer(config)
        removed = indexer.prune()
        print(f"Pruned {removed} deleted entries.")
        return

    # --reindex
    if args.reindex:
        from .indexer import Indexer
        indexer = Indexer(config)
        folder = None
        if args.folder:
            folder = normalize_path(args.folder)
            if not any(f["path"] == folder for f in indexer.folders()):
                known = ", ".join(f["path"] for f in indexer.folders()) or "none"
                print(f"Not a watched folder: {folder}\nWatched folders: {known}", file=sys.stderr)
                sys.exit(1)
        indexer.run_bulk_index(force=args.full, folder=folder)
        return

    # --watch (watcher only, no server)
    if args.watch:
        from .indexer import Indexer
        from .watcher import Watcher
        import time, signal
        indexer = Indexer(config)
        watcher = Watcher(config, indexer)
        watcher.start()
        print("Watching " + ", ".join(f["path"] for f in config["watched_folders"]) + " — press Ctrl-C to stop.")
        try:
            signal.pause()
        except (AttributeError, KeyboardInterrupt):
            watcher.stop()
        return

    # --serve
    if args.serve:
        from .server import serve
        port = args.port or config.get("port", 5123)
        serve(port=port, open_browser=not args.no_open)
        return

    # Search query
    if args.query:
        from .indexer import Indexer
        import json as _json

        indexer = Indexer(config)
        limit = args.limit or config.get("default_result_limit", 50)

        try:
            result = indexer.search(args.query, limit=limit)
        except ValueError as exc:
            print(f"Query error: {exc}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(_json.dumps(result, indent=2))
            return

        results = result.get("results", [])
        if not results:
            print("No results.")
            return

        import re
        mark_re = re.compile(r"</?mark>")

        for i, r in enumerate(results, 1):
            import datetime
            dt = datetime.datetime.fromtimestamp(r["file_mtime"])
            date_str = dt.strftime("%d %b %Y")
            excerpt = mark_re.sub("", r.get("excerpt") or "")
            print(f"[{i}] {date_str}  {r['filename']}")
            if excerpt:
                print(f"    {excerpt.strip()}")
            print(f"    {r['path']}")
            print()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
