#!/usr/bin/env python3
"""Standalone enroll API for control-plane boxes (no hermes_storage import).

Creates one-time codes, waits for agent PCs, issues X.509 bundles via
enroll-agent.sh, returns tar.gz.

Env:
  HERMES_CONTROL_DIR   (default: parent of scripts/)
  HERMES_ENROLL_PORT   (default: 8743)
  HERMES_MONGO_HOSTS   host:27017,host:27018,...
  HERMES_REPLICA_SET   (default: rs0)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
import subprocess
import tarfile
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ALPHABET = (string.ascii_uppercase + string.digits).replace("O", "").replace("0", "").replace("I", "").replace("1", "")
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
CONTROL = Path(os.environ.get("HERMES_CONTROL_DIR", Path(__file__).resolve().parents[1])).resolve()
PORT = int(os.environ.get("HERMES_ENROLL_PORT", "8743"))
HOSTS = os.environ.get("HERMES_MONGO_HOSTS", "").strip()
REPLICA = os.environ.get("HERMES_REPLICA_SET", "rs0").strip() or "rs0"
PENDING = CONTROL / "enroll_pending"
PENDING.mkdir(parents=True, exist_ok=True)


def _code() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(2))


def _norm(code: str) -> str:
    return code.strip().upper().replace(" ", "")


def _safe(name: str) -> str:
    return SAFE.sub("-", (name or "").strip()).strip("-") or "agent"


def create_pending(*, profile: str = "default", name: str | None = None, ttl: int = 300) -> dict:
    code = _code()
    doc = {
        "code": code,
        "profile": profile or "default",
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expires_at": time.time() + ttl,
        "used": False,
    }
    path = PENDING / f"{code}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return doc


def load_pending(code: str) -> dict | None:
    path = PENDING / f"{_norm(code)}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def mark_used(code: str, used_by: str) -> None:
    path = PENDING / f"{_norm(code)}.json"
    doc = load_pending(code) or {}
    doc["used"] = True
    doc["used_by"] = used_by
    doc["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def issue_bundle(name: str, profile: str) -> Path:
    script = CONTROL / "scripts" / "enroll-agent.sh"
    out = CONTROL / "bundles" / _safe(name)
    env = os.environ.copy()
    if HOSTS:
        env["HERMES_MONGO_HOSTS"] = HOSTS
    env["HERMES_REPLICA_SET"] = REPLICA
    subprocess.check_call(
        ["bash", str(script), "--name", name, "--profile", profile, "--out", str(out)],
        cwd=str(CONTROL),
        env=env,
    )
    return out


def pack_tar(bundle_dir: Path) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(bundle_dir, arcname=bundle_dir.name)
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[enroll] {self.address_string()} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path.startswith("/pending/"):
            code = _norm(path.split("/pending/", 1)[-1])
            doc = load_pending(code)
            if not doc:
                self._json(404, {"valid": False})
                return
            valid = (not doc.get("used")) and time.time() < float(doc.get("expires_at", 0))
            self._json(200, {
                "valid": valid,
                "used": bool(doc.get("used")),
                "expires_in": max(0, int(float(doc.get("expires_at", 0)) - time.time())),
                "profile": doc.get("profile"),
                "name": doc.get("name"),
            })
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/enroll":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        code = _norm(str(body.get("code") or ""))
        if not code:
            self._json(400, {"error": "code required"})
            return
        doc = load_pending(code)
        if not doc:
            self._json(404, {"error": "unknown code"})
            return
        if doc.get("used") or time.time() > float(doc.get("expires_at", 0)):
            self._json(410, {"error": "code expired or used"})
            return
        name = _safe(str(body.get("name") or doc.get("name") or f"pc-{code.split('-')[0].lower()}"))
        profile = str(body.get("profile") or doc.get("profile") or "default")
        try:
            bundle = issue_bundle(name, profile)
            blob = pack_tar(bundle)
            mark_used(code, name)
        except Exception as exc:
            self._json(500, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}.tar.gz"')
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Enroll listening on :{PORT}  control={CONTROL}")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hermes standalone enroll")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "create-code"])
    parser.add_argument("--name", default=None)
    parser.add_argument("--profile", default=os.environ.get("HERMES_PROFILE", "default"))
    parser.add_argument("--ttl", type=int, default=int(os.environ.get("HERMES_ENROLL_TTL", "300")))
    args = parser.parse_args()
    if args.command == "create-code":
        doc = create_pending(profile=args.profile, name=args.name, ttl=args.ttl)
        print(doc["code"])
    else:
        main()
