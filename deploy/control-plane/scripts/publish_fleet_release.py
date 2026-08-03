#!/usr/bin/env python3
"""Publish hermes_shared.fleet_release after installDB / control-plane refresh.

Standalone (pymongo only) so the DB box does not need a full Hermes checkout.

Env:
  HERMES_FLEET_VERSION   required unless --version
  HERMES_MONGO_REF       default main
  HERMES_MONGO_REPO      default KiberSlesar/hermes-agent-MongoDB
  HERMES_ORCH_MONGO_URI / app-credentials (same as orchestrator)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

CONTROL = Path(os.environ.get("HERMES_CONTROL_DIR", Path(__file__).resolve().parents[1])).resolve()
CERTS = CONTROL / "certs"


def _mongo_uri() -> str:
    uri = os.environ.get("HERMES_ORCH_MONGO_URI", "").strip()
    if uri:
        return uri
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
    if not password:
        raise SystemExit("Need HERMES_ORCH_MONGO_URI or certs/app-credentials.txt")
    return (
        f"mongodb://{user}:{password}@{hosts}/"
        f"?replicaSet={replica}&authSource=admin&directConnection=true"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Publish fleet_release for agent hermes update")
    p.add_argument("--version", default=os.environ.get("HERMES_FLEET_VERSION", "").strip())
    p.add_argument("--ref", default=os.environ.get("HERMES_MONGO_REF", "main").strip() or "main")
    p.add_argument(
        "--repo",
        default=os.environ.get("HERMES_MONGO_REPO", "KiberSlesar/hermes-agent-MongoDB").strip(),
    )
    p.add_argument("--published-by", default="installDB")
    args = p.parse_args()
    version = (args.version or "").strip()
    if not version:
        # Best-effort: try reading hermes_cli version from a sibling agent checkout
        for candidate in (
            Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "__init__.py",
            Path.home() / "hermes-agent" / "hermes_cli" / "__init__.py",
        ):
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("__version__"):
                        version = line.split("=", 1)[1].strip().strip("\"'")
                        break
            if version:
                break
    if not version:
        raise SystemExit(
            "Set HERMES_FLEET_VERSION=x.y.z (or --version) to publish fleet_release"
        )

    from pymongo import MongoClient

    client = MongoClient(_mongo_uri(), serverSelectionTimeoutMS=8000)
    shared = client[os.environ.get("HERMES_SHARED_DB", "hermes_shared")]
    doc = {
        "version": version,
        "ref": args.ref,
        "repo": args.repo or "KiberSlesar/hermes-agent-MongoDB",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "published_by": args.published_by,
    }
    shared["fleet_release"].update_one({"_id": "default"}, {"$set": doc}, upsert=True)
    print(f"Published fleet_release: {doc['version']}@{doc['ref']} ({doc['repo']})")


if __name__ == "__main__":
    main()
