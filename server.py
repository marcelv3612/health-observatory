#!/usr/bin/env python3
"""Private, dependency-free loopback dashboard. No external calls or telemetry."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import sqlite3
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from health import MAX_UPLOAD, ImportError, dashboard, load_export
from watch_metrics import MAX_REQUEST_BYTES
from watch_sync import WatchStore

MAX_WATCH_BATCH = MAX_REQUEST_BYTES

ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS = {
    "target_hours": 8, "timezone_mode": "recorded", "timezone": "UTC",
    "source_order": [], "annotations": {}, "session_gap_hours": 4,
    "primary_owner": "You", "protected_windows": [],
    "widgets": {"duration": True, "consistency": True, "stages": True, "gaps": True},
}


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.token = secrets.token_urlsafe(32)
        self.dataset = self.read("sleep.json") or self.demo()
        self.settings = validate_settings({**DEFAULT_SETTINGS, **(self.read("settings.json") or {})})
        self.dataset_version = self.version("sleep.json")
        self.watch = WatchStore(directory / "watch")

    def demo(self) -> dict[str, Any]:
        return json.loads((ROOT / "fixtures/demo.json").read_text())

    def read(self, name: str) -> Any:
        path = self.directory / name
        return json.loads(path.read_text()) if path.exists() else None

    def version(self, name: str) -> tuple[int, int] | None:
        path = self.directory / name
        return (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None

    def refresh_dataset(self) -> None:
        version = self.version("sleep.json")
        if version != self.dataset_version:
            incoming = self.read("sleep.json")
            if incoming is not None:
                dashboard(incoming, self.settings)
                self.dataset = incoming
            self.dataset_version = version

    def write(self, name: str, value: Any, expected_version: Any = ...) -> None:
        lock_fd = os.open(self.directory / ".write.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fd, tmp = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                if expected_version is not ... and self.version(name) != expected_version:
                    raise ValueError("History changed during import. Retry the feed; newer data was preserved.")
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            os.replace(tmp, self.directory / name)
        finally:
            os.close(lock_fd)
            if os.path.exists(tmp):
                os.unlink(tmp)


class LocalServer(ThreadingHTTPServer):
    # Normal shutdown waits for imports and their temporary raw-file cleanup.
    daemon_threads = False

    def __init__(self, port: int, store: Store):
        # The bind address cannot be changed to a LAN/public address.
        super().__init__(("127.0.0.1", port), Handler)
        self.store = store
        # Preserve serialized store operations while idle browser preconnections
        # and static assets are handled independently.
        self.api_lock = RLock()
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            # A closed browser connection is routine; no request/data is logged.
            pass

    def log_message(self, *_: Any) -> None:
        # Do not log filenames, query strings, sources, dates, or health data.
        pass

    def send(self, status: int, content: bytes, mime: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(content)

    def json(self, value: Any, status: int = 200) -> None:
        self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def permitted(self, mutate: bool = False) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in self.server.allowed_hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.json({"error": "Only the local dashboard origin is allowed."}, 403)
            return False
        if origin and origin not in self.server.allowed_origins:
            self.json({"error": "Cross-origin requests are blocked."}, 403)
            return False
        if mutate and (origin not in self.server.allowed_origins or not secrets.compare_digest(
                self.headers.get("X-Local-Token", ""), self.server.store.token)):
            self.json({"error": "Reload the local dashboard before changing data."}, 403)
            return False
        return True

    def state(self) -> dict[str, Any]:
        self.server.store.refresh_dataset()
        query = parse_qs(urlsplit(self.path).query)
        start = query.get("start", [""])[0]
        end = query.get("end", [""])[0]
        for date in (start, end):
            if date:
                from datetime import date as Date
                Date.fromisoformat(date)
        if start and end and start > end:
            raise ValueError("Start date must be before the end date.")
        result = dashboard(self.server.store.dataset, self.server.store.settings, start, end)
        result["token"] = self.server.store.token
        result["feed"] = self.server.store.read("feed-status.json")
        return result

    def do_GET(self) -> None:
        if urlsplit(self.path).path.startswith("/api/"):
            with self.server.api_lock:
                self.get()
        else:
            self.get()

    def get(self) -> None:
        if not self.permitted():
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                self.json(self.state())
            except ValueError as exc:
                self.json({"error": str(exc)}, 400)
        elif path == "/api/watch":
            try:
                query = parse_qs(urlsplit(self.path).query)
                self.json(self.server.store.watch.dashboard(
                    timezone=query.get("timezone", ["UTC"])[0],
                    start=query.get("start", [""])[0], end=query.get("end", [""])[0],
                    settings=self.server.store.settings))
            except (ValueError, TypeError) as exc:
                self.json({"error": str(exc)}, 400)
            except sqlite3.Error:
                self.json({"error": "Watch storage is unavailable. Retry the local view."}, 503)
        elif path in ("/", "/index.html", "/app.js", "/styles.css",
                      "/ceramic.mjs", "/ceramic-motion.mjs",
                      "/ceramic-onion.mjs", "/ceramic-onion-scene.mjs", "/onion-health.mjs",
                      "/vendor/three/three.module.js", "/vendor/three/three.core.js"):
            name = "index.html" if path == "/" else path[1:]
            mime = "text/html; charset=utf-8" if name == "index.html" else (
                "text/css; charset=utf-8" if name == "styles.css" else "text/javascript; charset=utf-8")
            self.send(200, (ROOT / "static" / name).read_bytes(), mime)
        elif path == "/favicon.ico":
            self.send(204, b"")
        else:
            self.json({"error": "Not found."}, 404)

    def body_length(self, limit: int) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ImportError("Invalid file length.") from exc
        if length <= 0 or length > limit:
            raise ImportError(f"Choose a nonempty file smaller than {limit//1024//1024} MiB.")
        return length

    def do_POST(self) -> None:
        with self.server.api_lock:
            self.post()

    def post(self) -> None:
        self.connection.settimeout(60)
        if not self.permitted(True):
            return
        path = urlsplit(self.path).path
        store = self.server.store
        try:
            if path == "/api/import":
                before = store.version("sleep.json")
                length = self.body_length(MAX_UPLOAD)
                fd, tmp = tempfile.mkstemp(dir=store.directory, suffix=".incoming")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(64*1024, remaining))
                            if not chunk:
                                raise ImportError("Transfer ended early. The current dataset was kept.")
                            stream.write(chunk)
                            remaining -= len(chunk)
                    new_data = load_export(Path(tmp))
                    # Validate analysis before replacing any persisted or in-memory dataset.
                    dashboard(new_data, store.settings)
                    store.write("sleep.json", new_data, expected_version=before)
                    store.dataset = new_data
                finally:
                    Path(tmp).unlink(missing_ok=True)
                self.json({"ok": True, "records": len(new_data["records"]), "stats": new_data["import_stats"]})
            elif path == "/api/settings":
                length = self.body_length(256*1024)
                incoming = json.loads(self.rfile.read(length))
                new = validate_settings({**store.settings, **incoming})
                dashboard(store.dataset, new)
                store.write("settings.json", new)
                store.settings = new
                self.json({"ok": True})
            elif path == "/api/watch/import":
                length = self.body_length(MAX_WATCH_BATCH)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Transfer ended early. Watch history was kept.")
                self.json(store.watch.ingest(json.loads(raw)))
            elif path == "/api/watch/retry":
                length = self.body_length(1024)
                if json.loads(self.rfile.read(length)) != {}:
                    raise ValueError("Retry accepts an empty object only.")
                self.json(store.watch.request_retry())
            else:
                self.json({"error": "Not found."}, 404)
        except (sqlite3.Error, RecursionError):
            self.json({"error": "Watch batch could not be processed. Retry the same batch after checking local storage."}, 400)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            message = str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else "Local file operation failed. The current dataset was kept."
            self.json({"error": message}, 400)


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    from math import isfinite
    result = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    if isinstance(result["target_hours"], bool) or not isinstance(result["target_hours"], (int, float)) or not 1 <= result["target_hours"] <= 16 or not isfinite(result["target_hours"]):
        raise ValueError("Sleep target must be between 1 and 16 hours.")
    if result["timezone_mode"] not in ("recorded", "fixed"):
        raise ValueError("Choose recorded or fixed timezone mode.")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Use a valid IANA timezone, such as UTC.") from exc
    if isinstance(result["session_gap_hours"], bool) or not isinstance(result["session_gap_hours"], (int, float)) or result["session_gap_hours"] not in (2, 3, 4, 6):
        raise ValueError("Session grouping gap must be 2, 3, 4, or 6 hours.")
    primary_owner = result["primary_owner"]
    if not isinstance(primary_owner, str) or not primary_owner.strip() or len(primary_owner) > 1000:
        raise ValueError("Primary wearer must contain 1 to 1,000 characters.")
    result["primary_owner"] = primary_owner.strip()

    def valid_interval(value: dict[str, Any]) -> bool:
        # UTC timestamps are bounded to the representable calendar (years 1–9999).
        return (all(not isinstance(value.get(k), bool) and isinstance(value.get(k), (int, float))
                    and -62135596800 <= value[k] <= 253402300799 and isfinite(value[k])
                    for k in ("start", "end")) and value["end"] > value["start"])

    windows = result["protected_windows"]
    if not isinstance(windows, list) or len(windows) > 1000:
        raise ValueError("Use at most 1,000 protected windows.")
    clean_windows = []
    gap_count = 0
    for window in windows:
        if (not isinstance(window, dict) or set(window) - {"start", "end", "owner", "note", "reported_gaps"}
                or not valid_interval(window)):
            raise ValueError("Invalid protected window: use numeric UTC start and end times.")
        owner = window.get("owner")
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 1000:
            raise ValueError("Each protected window needs a wearer of 1 to 1,000 characters.")
        if "note" in window and (not isinstance(window["note"], str) or len(window["note"]) > 1000):
            raise ValueError("Protected-window notes may contain at most 1,000 characters.")
        clean = {**window, "owner": owner.strip()}
        gaps = window.get("reported_gaps", [])
        if not isinstance(gaps, list) or len(gaps) > 100:
            raise ValueError("Use at most 100 reported gaps per protected window.")
        gap_count += len(gaps)
        if gap_count > 1000:
            raise ValueError("Use at most 1,000 reported gaps in total.")
        for gap in gaps:
            if (not isinstance(gap, dict) or set(gap) - {"start", "end", "label"}
                    or not valid_interval(gap) or gap["start"] < window["start"] or gap["end"] > window["end"]):
                raise ValueError("Reported gaps need numeric UTC times inside their protected window.")
            if "label" in gap and (not isinstance(gap["label"], str) or len(gap["label"]) > 1000):
                raise ValueError("Reported-gap labels may contain at most 1,000 characters.")
        clean["reported_gaps"] = [dict(gap) for gap in gaps]
        clean_windows.append(clean)
    result["protected_windows"] = clean_windows
    order = result["source_order"]
    if not isinstance(order, list) or len(order) > 1000 or any(not isinstance(s, str) or len(s) > 100 for s in order) or len(set(order)) != len(order):
        raise ValueError("Invalid source priority list.")
    annotations = result["annotations"]
    if not isinstance(annotations, dict) or len(annotations) > 20_000:
        raise ValueError("Invalid annotation list.")
    for nid, annotation in annotations.items():
        if not isinstance(nid, str) or len(nid) != 16 or not isinstance(annotation, dict):
            raise ValueError("Invalid night annotation.")
        if set(annotation) - {"excluded", "owner", "note", "start", "end"}:
            raise ValueError("Unknown annotation fields.")
        if "excluded" in annotation and not isinstance(annotation["excluded"], bool):
            raise ValueError("Exclusion must be true or false.")
        for key in ("owner", "note"):
            if key in annotation and (not isinstance(annotation[key], str) or len(annotation[key]) > 1000):
                raise ValueError("Annotations may contain at most 1,000 characters.")
        if "start" in annotation or "end" in annotation:
            if not valid_interval(annotation):
                raise ValueError("Invalid annotation interval.")
    widgets = result["widgets"]
    if not isinstance(widgets, dict) or set(widgets) != set(DEFAULT_SETTINGS["widgets"]) or any(not isinstance(v, bool) for v in widgets.values()):
        raise ValueError("Invalid dashboard card settings.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".private")
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.data_dir.resolve())
    server = LocalServer(args.port, store)
    def stop(*_: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print(f"Private dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Mode: {store.dataset['mode']} · Ctrl+C stops the server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
