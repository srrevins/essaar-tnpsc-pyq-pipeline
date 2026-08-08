#!/usr/bin/env python3
"""Small health endpoint plus one idempotent extraction batch per Space boot."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_LOCK = threading.Lock()
STATE = {
    "status": "starting",
    "startedAt": None,
    "finishedAt": None,
    "exitCode": None,
    "logTail": [],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_state(**values) -> None:
    with STATE_LOCK:
        STATE.update(values)


def run_batch() -> None:
    limit = os.environ.get("PROCESS_LIMIT", "3")
    set_state(status="running", startedAt=now())
    command = [
        "python",
        str(ROOT / "pipeline.py"),
        "sync",
        "--manifest",
        str(ROOT / "data" / "manifest.json"),
        "--limit",
        limit,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    lines = completed.stdout.splitlines()
    set_state(
        status="completed" if completed.returncode == 0 else "failed",
        finishedAt=now(),
        exitCode=completed.returncode,
        logTail=lines[-80:],
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        with STATE_LOCK:
            payload = json.dumps(STATE, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


if __name__ == "__main__":
    threading.Thread(target=run_batch, daemon=True).start()
    port = int(os.environ.get("PORT", "7860"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
