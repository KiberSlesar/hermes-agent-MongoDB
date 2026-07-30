#!/usr/bin/env python3
"""Self-hosted orchestrator — mTLS HTTPS on :8744 (no Docker, pymongo only)."""

from __future__ import annotations

import json
import os
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CONTROL = Path(os.environ.get("HERMES_CONTROL_DIR", Path(__file__).resolve().parents[1])).resolve()
PORT = int(os.environ.get("HERMES_ORCHESTRATOR_PORT", "8744"))
CERTS = CONTROL / "certs"
SERVER_PEM = Path(os.environ.get("HERMES_ORCH_SERVER_PEM", str(CERTS / "mongo-server.pem")))
CA_CRT = Path(os.environ.get("HERMES_ORCH_CA", str(CERTS / "ca.crt")))
STATE_ID = "default"


def peer_cn(cert) -> str | None:
    if not cert:
        return None
    for rdn in cert.get("subject") or ():
        for key, val in rdn:
            if key == "commonName":
                return str(val)
    return None


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    ctx.load_cert_chain(certfile=str(SERVER_PEM), keyfile=str(SERVER_PEM))
    ctx.load_verify_locations(cafile=str(CA_CRT))
    return ctx


def mongo():
    from pymongo import MongoClient

    uri = os.environ.get("HERMES_ORCH_MONGO_URI", "").strip()
    if not uri:
        creds = CERTS / "app-credentials.txt"
        hosts = os.environ.get("HERMES_MONGO_HOSTS", "127.0.0.1:27017").split(",")[0]
        user = os.environ.get("HERMES_APP_USER", "hermesApp")
        password = os.environ.get("HERMES_APP_PASSWORD", "")
        if creds.is_file():
            for line in creds.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    if k.strip() == "HERMES_APP_USER":
                        user = v.strip()
                    elif k.strip() == "HERMES_APP_PASSWORD":
                        password = v.strip()
        replica = os.environ.get("HERMES_REPLICA_SET", "rs0")
        # Local SCRAM without requiring client TLS on loopback admin path
        uri = f"mongodb://{user}:{password}@{hosts}/?replicaSet={replica}&authSource=admin&directConnection=true"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client[os.environ.get("HERMES_SHARED_DB", "hermes_shared")]


_DB = None


def db():
    global _DB
    if _DB is None:
        _DB = mongo()
    return _DB


def cluster_status() -> dict:
    shared = db()
    state = shared["cluster_state"].find_one({"_id": STATE_ID}) or {}
    state.pop("_id", None)
    nodes = list(shared["cluster_nodes"].find().sort("heartbeat_at", -1).limit(50))
    for n in nodes:
        n.pop("_id", None)
        # stringify dates
        for k, v in list(n.items()):
            if hasattr(v, "isoformat"):
                n[k] = v.isoformat()
    return {"state": state, "nodes": nodes}


def set_active(target: str, reason: str = "manual") -> dict:
    shared = db()
    nodes = list(shared["cluster_nodes"].find())
    match = None
    needle = target.strip().lower()
    for node in nodes:
        for field in ("node_id", "machine_id", "hostname"):
            val = str(node.get(field) or "").lower()
            if val == needle or val.startswith(needle):
                match = node
                break
        if match:
            break
    if not match:
        raise ValueError(f"No node matched {target!r}")
    node_id = match["node_id"]
    shared["cluster_state"].update_one(
        {"_id": STATE_ID},
        {"$set": {
            "active_node_id": node_id,
            "messaging_owner": node_id,
            "handoff_state": "idle",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_reason": reason,
        }},
        upsert=True,
    )
    return cluster_status()


def heartbeat(doc: dict) -> None:
    shared = db()
    node_id = doc.get("node_id") or doc.get("machine_id")
    if not node_id:
        raise ValueError("node_id required")
    payload = dict(doc)
    payload["node_id"] = node_id
    payload["heartbeat_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    shared["cluster_nodes"].update_one(
        {"node_id": node_id},
        {"$set": payload},
        upsert=True,
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        cn = peer_cn(getattr(self.connection, "getpeercert", lambda: None)())
        print(f"[orch] {self.address_string()} cn={cn} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_cert(self) -> str | None:
        cn = peer_cn(self.connection.getpeercert())
        if not cn:
            self._json(401, {"error": "client certificate required"})
            return None
        return cn

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if not self._require_cert():
            return
        if path == "/cluster/status":
            try:
                self._json(200, cluster_status())
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if not self._require_cert():
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        try:
            if path == "/cluster/activate":
                self._json(200, set_active(str(body.get("target") or ""), str(body.get("reason") or "manual")))
                return
            if path == "/cluster/heartbeat":
                heartbeat(body)
                self._json(200, {"ok": True})
                return
        except Exception as exc:
            self._json(400, {"error": str(exc)})
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    if not SERVER_PEM.is_file() or not CA_CRT.is_file():
        raise SystemExit(f"Missing TLS files: {SERVER_PEM} / {CA_CRT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.socket = ssl_ctx().wrap_socket(httpd.socket, server_side=True)
    print(f"Orchestrator mTLS on :{PORT}  control={CONTROL}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
