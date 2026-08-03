"""Fleet runtime versioning for Mongo-fork agents.

Desired release lives in Mongo ``hermes_shared.fleet_release``. Publish via
``hermes cluster update`` on the DB box. Agents apply manually with
``hermes update`` (tarball install-agent path — not upstream Nous ZIP).
Idle auto-apply is disabled.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FLEET_RELEASE_ID = "default"
STAMP_NAME = ".fleet_install_stamp"
DEFAULT_REPO = "KiberSlesar/hermes-agent-MongoDB"
DEFAULT_REF = "main"

_APPLY_LOCK = threading.Lock()
_APPLY_THREAD: Optional[threading.Thread] = None
_LAST_APPLY_ATTEMPT = 0.0
_APPLY_BACKOFF_S = 300.0


def local_agent_version() -> str:
    try:
        from hermes_cli import __version__

        return str(__version__ or "").strip()
    except Exception:
        return ""


def stamp_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / STAMP_NAME


def read_install_stamp() -> dict[str, Any]:
    path = stamp_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_install_stamp(*, version: str = "", ref: str = "", repo: str = "") -> None:
    path = stamp_path()
    doc = {
        "version": (version or local_agent_version()).strip(),
        "ref": (ref or "").strip(),
        "repo": (repo or "").strip(),
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": platform.system().lower(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.debug("Failed to write fleet install stamp", exc_info=True)


def local_install_ref() -> str:
    stamp = read_install_stamp()
    ref = str(stamp.get("ref") or "").strip()
    if ref:
        return ref
    return os.environ.get("HERMES_MONGO_REF", "").strip() or DEFAULT_REF


def presence_version_fields() -> dict[str, Any]:
    stamp = read_install_stamp()
    return {
        "agent_version": local_agent_version(),
        "install_ref": str(stamp.get("ref") or local_install_ref()),
        "install_repo": str(stamp.get("repo") or os.environ.get("HERMES_MONGO_REPO") or DEFAULT_REPO),
        "os": platform.system().lower(),
    }


def normalize_release(doc: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    version = str(doc.get("version") or "").strip()
    ref = str(doc.get("ref") or doc.get("install_ref") or "").strip()
    repo = str(doc.get("repo") or DEFAULT_REPO).strip() or DEFAULT_REPO
    if not version and not ref:
        return {}
    out = {
        "version": version,
        "ref": ref or DEFAULT_REF,
        "repo": repo,
        "published_at": doc.get("published_at"),
        "published_by": doc.get("published_by") or "",
    }
    if doc.get("artifact"):
        out["artifact"] = doc.get("artifact")
    return out


def versions_in_sync(
    *,
    agent_version: str,
    install_ref: str,
    desired: Optional[dict[str, Any]],
) -> bool:
    """True when agent matches desired release (version and/or ref)."""
    want = normalize_release(desired)
    if not want:
        return True  # nothing published → no skew
    av = (agent_version or "").strip()
    ar = (install_ref or "").strip()
    if want["version"] and av and want["version"] != av:
        return False
    if want["ref"] and ar and want["ref"] != ar:
        # Allow main tip drift only when versions match and stamp missing version-only policy:
        # if both version fields present and equal, ref mismatch alone is ok for floating main.
        if want["version"] and av and want["version"] == av:
            return True
        if want["ref"] != ar:
            return False
    if want["version"] and not av:
        return False
    return True


def node_in_sync(node: dict[str, Any], desired: Optional[dict[str, Any]]) -> bool:
    return versions_in_sync(
        agent_version=str(node.get("agent_version") or ""),
        install_ref=str(node.get("install_ref") or ""),
        desired=desired,
    )


def format_version_skew_warning(
    *,
    agent_version: str,
    install_ref: str,
    desired: Optional[dict[str, Any]],
) -> str:
    want = normalize_release(desired)
    if not want or versions_in_sync(
        agent_version=agent_version, install_ref=install_ref, desired=want
    ):
        return ""
    parts = [
        "⚠️ Внимание: версия агента не совпадает с целевой версией флота "
        f"(агент {agent_version or '?'}@{install_ref or '?'}, "
        f"флот {want.get('version') or '?'}@{want.get('ref') or '?'}). "
        "Выполните hermes update на этой машине."
    ]
    return "".join(parts)


class MongoFleetReleaseStore:
    """Singleton desired-release document in ``fleet_release``."""

    def __init__(self, collection):
        self._col = collection

    def get(self) -> dict[str, Any]:
        doc = self._col.find_one({"_id": FLEET_RELEASE_ID}) or {}
        doc = dict(doc)
        doc.pop("_id", None)
        return normalize_release(doc)

    def put(
        self,
        *,
        version: str,
        ref: str = DEFAULT_REF,
        repo: str = DEFAULT_REPO,
        published_by: str = "",
        artifact: Optional[str] = None,
    ) -> dict[str, Any]:
        from hermes_storage.backend import utcnow

        doc = {
            "version": str(version or "").strip(),
            "ref": str(ref or DEFAULT_REF).strip() or DEFAULT_REF,
            "repo": str(repo or DEFAULT_REPO).strip() or DEFAULT_REPO,
            "published_at": utcnow(),
            "published_by": str(published_by or "").strip(),
        }
        if artifact:
            doc["artifact"] = str(artifact)
        if not doc["version"]:
            raise ValueError("version required")
        self._col.update_one(
            {"_id": FLEET_RELEASE_ID},
            {"$set": doc},
            upsert=True,
        )
        return normalize_release(doc)


def get_fleet_release_from_mongo(shared_db) -> dict[str, Any]:
    try:
        return MongoFleetReleaseStore(shared_db["fleet_release"]).get()
    except Exception:
        logger.debug("fleet_release read failed", exc_info=True)
        return {}


def enrich_cluster_nodes(
    nodes: list[dict[str, Any]],
    desired: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    want = normalize_release(desired)
    out = []
    for node in nodes:
        row = dict(node)
        row["version_in_sync"] = node_in_sync(row, want) if want else True
        row["update_stale"] = bool(want) and not row["version_in_sync"]
        out.append(row)
    return out


def maybe_schedule_fleet_update(
    storage: Any,
    *,
    desired: Optional[dict[str, Any]] = None,
    active_turns: int = 0,
    force: bool = False,
) -> dict[str, Any]:
    """If stale and idle, spawn background apply (deduped)."""
    global _APPLY_THREAD, _LAST_APPLY_ATTEMPT

    want = normalize_release(desired)
    if want is None or want == {}:
        try:
            if getattr(storage, "fleet_release", None) is not None:
                want = normalize_release(storage.fleet_release.get())
            elif getattr(storage, "shared_db", None) is not None:
                want = get_fleet_release_from_mongo(storage.shared_db)
        except Exception:
            want = {}

    local_v = local_agent_version()
    local_r = local_install_ref()
    result: dict[str, Any] = {
        "needed": False,
        "scheduled": False,
        "desired": want,
        "agent_version": local_v,
        "install_ref": local_r,
    }
    if not want:
        return result
    if versions_in_sync(agent_version=local_v, install_ref=local_r, desired=want):
        return result
    result["needed"] = True

    if not force and int(active_turns or 0) > 0:
        result["deferred"] = "active_turns"
        _set_node_update_status(storage, "pending", detail="waiting for idle")
        return result

    try:
        state = storage.cluster.get_state() or {}
        if not force and (state.get("handoff_state") or "idle") not in {"idle", "done", None, ""}:
            result["deferred"] = "handoff"
            _set_node_update_status(storage, "pending", detail="handoff in progress")
            return result
    except Exception:
        pass

    now = time.time()
    if not force and (now - _LAST_APPLY_ATTEMPT) < _APPLY_BACKOFF_S:
        result["deferred"] = "backoff"
        return result

    with _APPLY_LOCK:
        if _APPLY_THREAD and _APPLY_THREAD.is_alive():
            result["deferred"] = "already_running"
            return result
        _LAST_APPLY_ATTEMPT = now
        _set_node_update_status(storage, "applying", detail=f"→ {want.get('version')}@{want.get('ref')}")
        thread = threading.Thread(
            target=_apply_worker,
            args=(want,),
            name="hermes-fleet-update",
            daemon=True,
        )
        _APPLY_THREAD = thread
        thread.start()
        result["scheduled"] = True
    return result


def _set_node_update_status(storage: Any, status: str, *, detail: str = "") -> None:
    try:
        storage.cluster.heartbeat({
            "node_id": storage.node_id,
            "machine_id": storage.machine_id,
            "update_status": status,
            "update_detail": (detail or "")[:200],
            **presence_version_fields(),
        })
    except Exception:
        logger.debug("update_status heartbeat failed", exc_info=True)


def _apply_worker(desired: dict[str, Any]) -> None:
    lock = None
    try:
        from hermes_cli.update_lock import UpdateLock

        lock = UpdateLock()
        if not lock.acquire():
            logger.warning(
                "Fleet update deferred — another update holds the lock (pid=%s)",
                getattr(lock.holder, "pid", "?"),
            )
            return

        code = run_mongo_fork_install(desired)
        if code == 0:
            write_install_stamp(
                version=str(desired.get("version") or local_agent_version()),
                ref=str(desired.get("ref") or ""),
                repo=str(desired.get("repo") or ""),
            )
            logger.info(
                "Fleet update applied: %s@%s — restart gateway to load new code",
                desired.get("version"),
                desired.get("ref"),
            )
            _restart_gateway_best_effort()
        else:
            logger.error("Fleet update failed with exit %s", code)
    except Exception:
        logger.exception("Fleet update worker crashed")
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def run_mongo_fork_install(desired: dict[str, Any]) -> int:
    """Run install-agent style upgrade (subprocess). Returns exit code."""
    import shutil
    import subprocess
    import sys
    import urllib.request

    repo = str(desired.get("repo") or DEFAULT_REPO)
    ref = str(desired.get("ref") or DEFAULT_REF)
    env = os.environ.copy()
    env["HERMES_YES"] = "1"
    env["HERMES_SKIP_CONNECT"] = "1"
    env["HERMES_MONGO_REPO"] = repo
    env["HERMES_MONGO_REF"] = ref
    if desired.get("version"):
        env["HERMES_FLEET_VERSION"] = str(desired["version"])

    # Prefer checkout-bundled installer when running from a source tree.
    candidates: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        candidates.append(home / "hermes-agent" / "install" / "install-agent.sh")
        candidates.append(home / "hermes-agent" / "install" / "install-agent.ps1")
    except Exception:
        pass
    here = Path(__file__).resolve()
    candidates.append(here.parents[1] / "install" / "install-agent.sh")
    candidates.append(here.parents[1] / "install" / "install-agent.ps1")

    is_windows = platform.system().lower().startswith("win")
    script: Optional[Path] = None
    for c in candidates:
        if c.is_file() and (c.suffix == ".ps1") == is_windows:
            script = c
            break
        if c.is_file() and not is_windows and c.suffix == ".sh":
            script = c
            break

    if script is None:
        # Download installer from the desired ref.
        raw_name = "install-agent.ps1" if is_windows else "install-agent.sh"
        url = f"https://raw.githubusercontent.com/{repo}/{ref}/install/{raw_name}"
        tmp = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")
        tmp.mkdir(parents=True, exist_ok=True)
        script = tmp / f"hermes-{raw_name}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                script.write_bytes(resp.read())
        except Exception as exc:
            logger.error("Failed to download install-agent from %s: %s", url, exc)
            return 1

    if is_windows:
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    else:
        bash = shutil.which("bash") or "bash"
        cmd = [bash, str(script)]

    logger.info("Running fleet install: %s (repo=%s ref=%s)", cmd, repo, ref)
    try:
        proc = subprocess.run(cmd, env=env, check=False)
        return int(proc.returncode)
    except Exception as exc:
        logger.error("Fleet install spawn failed: %s", exc)
        return 1


def _restart_gateway_best_effort() -> None:
    import shutil
    import subprocess
    import sys

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        cmd = [hermes_bin, "gateway", "restart"]
    else:
        cmd = [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Requested gateway restart after fleet update: %s", cmd)
    except Exception as exc:
        logger.warning("Could not restart gateway after fleet update: %s", exc)


def is_mongo_agent_install() -> bool:
    """True when this node should use fleet ``hermes update`` (not Nous ZIP)."""
    try:
        from hermes_storage import is_mongo_mode

        if is_mongo_mode():
            return True
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        if (home / "bootstrap.yaml").is_file():
            return True
        if stamp_path().is_file():
            return True
    except Exception:
        pass
    return False


def resolve_desired_fleet_release(storage: Any = None) -> dict[str, Any]:
    """Read desired release from storage / Mongo / orchestrator response cache."""
    want: dict[str, Any] = {}
    if storage is not None:
        try:
            if getattr(storage, "fleet_release", None) is not None:
                want = normalize_release(storage.fleet_release.get())
        except Exception:
            want = {}
        if want:
            return want
        try:
            if getattr(storage, "shared_db", None) is not None:
                want = get_fleet_release_from_mongo(storage.shared_db)
        except Exception:
            want = {}
        if want:
            return want
    try:
        from hermes_storage import get_storage, is_mongo_mode

        if is_mongo_mode():
            st = storage or get_storage(force=True)
            if getattr(st, "fleet_release", None) is not None:
                return normalize_release(st.fleet_release.get())
            if getattr(st, "shared_db", None) is not None:
                return get_fleet_release_from_mongo(st.shared_db)
    except Exception:
        logger.debug("resolve_desired_fleet_release failed", exc_info=True)
    return {}


def apply_fleet_update_sync(
    desired: Optional[dict[str, Any]] = None,
    *,
    storage: Any = None,
) -> dict[str, Any]:
    """Synchronously apply fleet release (for ``hermes update`` on agents)."""
    want = normalize_release(desired) or resolve_desired_fleet_release(storage)
    local_v = local_agent_version()
    local_r = local_install_ref()
    result: dict[str, Any] = {
        "needed": False,
        "ok": True,
        "exit_code": 0,
        "desired": want,
        "agent_version": local_v,
        "install_ref": local_r,
    }
    if not want:
        result["ok"] = False
        result["error"] = "no fleet_release published (run hermes cluster update on DB)"
        result["exit_code"] = 1
        return result
    if versions_in_sync(agent_version=local_v, install_ref=local_r, desired=want):
        result["message"] = "already in sync"
        return result
    result["needed"] = True
    if storage is not None:
        _set_node_update_status(
            storage, "applying", detail=f"→ {want.get('version')}@{want.get('ref')}"
        )
    lock = None
    try:
        from hermes_cli.update_lock import UpdateLock

        lock = UpdateLock()
        if not lock.acquire():
            result["ok"] = False
            result["exit_code"] = 2
            result["error"] = "another update holds the lock"
            if storage is not None:
                _set_node_update_status(storage, "error", detail="lock held")
            return result
        code = run_mongo_fork_install(want)
        result["exit_code"] = code
        result["ok"] = code == 0
        if code == 0:
            write_install_stamp(
                version=str(want.get("version") or local_agent_version()),
                ref=str(want.get("ref") or ""),
                repo=str(want.get("repo") or ""),
            )
            result["agent_version"] = local_agent_version()
            result["install_ref"] = local_install_ref()
            _restart_gateway_best_effort()
            if storage is not None:
                _set_node_update_status(storage, "idle", detail="applied")
        else:
            if storage is not None:
                _set_node_update_status(storage, "error", detail=f"exit {code}")
    except Exception as exc:
        logger.exception("apply_fleet_update_sync failed")
        result["ok"] = False
        result["exit_code"] = 1
        result["error"] = str(exc)
        if storage is not None:
            _set_node_update_status(storage, "error", detail=str(exc)[:200])
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
    return result


def hermes_db_home() -> Path:
    return Path(os.environ.get("HERMES_DB_HOME") or (Path.home() / "hermes-db")).expanduser()


def download_repo_tarball(*, repo: str, ref: str, dest: Path) -> Path:
    """Download GitHub tarball to ``dest`` (file). Returns dest."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        f"https://codeload.github.com/{repo}/tar.gz/{ref}",
        f"https://api.github.com/repos/{repo}/tarball/{ref}",
    ]
    last_err: Optional[Exception] = None
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    for url in urls:
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            req.add_header("User-Agent", "hermes-cluster-update")
            with urllib.request.urlopen(req, timeout=120) as resp:
                dest.write_bytes(resp.read())
            return dest
        except Exception as exc:
            last_err = exc
            logger.debug("tarball download failed from %s: %s", url, exc)
    raise RuntimeError(f"Failed to download {repo}@{ref}: {last_err}")


def refresh_control_plane_scripts_from_tarball(
    tarball: Path, *, scripts_dir: Path
) -> list[str]:
    """Extract ``deploy/control-plane/scripts/*`` from tarball into scripts_dir."""
    import tarfile

    scripts_dir.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []
    with tarfile.open(tarball, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        for m in members:
            # github tarball: <repo>-<sha>/deploy/control-plane/scripts/foo.py
            parts = Path(m.name).parts
            if "deploy" in parts and "control-plane" in parts and "scripts" in parts:
                try:
                    idx = parts.index("scripts")
                except ValueError:
                    continue
                rel = Path(*parts[idx + 1 :])
                if not rel.parts or ".." in rel.parts:
                    continue
                out = scripts_dir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    continue
                out.write_bytes(src.read())
                updated.append(str(rel))
    return updated


def restart_control_plane_units_best_effort() -> dict[str, Any]:
    import shutil
    import subprocess

    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"restarted": False, "reason": "systemctl not found"}
    units = ["hermes-orchestrator", "hermes-enroll"]
    ok = []
    failed = []
    for unit in units:
        try:
            proc = subprocess.run(
                [systemctl, "--user", "restart", unit],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                ok.append(unit)
            else:
                failed.append({"unit": unit, "stderr": (proc.stderr or "")[:200]})
        except Exception as exc:
            failed.append({"unit": unit, "error": str(exc)})
    return {"restarted": bool(ok), "ok": ok, "failed": failed}


def run_cluster_server_update(
    *,
    version: str = "",
    ref: str = "",
    repo: str = "",
    storage: Any = None,
    published_by: str = "cluster-update",
) -> dict[str, Any]:
    """DB-side: download client tarball, refresh CP scripts, publish fleet_release."""
    ref = (ref or os.environ.get("HERMES_MONGO_REF") or DEFAULT_REF).strip() or DEFAULT_REF
    repo = (
        repo or os.environ.get("HERMES_MONGO_REPO") or DEFAULT_REPO
    ).strip() or DEFAULT_REPO
    if repo == "KiberSlesar/hermes-agent-MongoDB-private":
        repo = DEFAULT_REPO
    version = (version or os.environ.get("HERMES_FLEET_VERSION") or "").strip()
    if not version:
        version = local_agent_version()
    if not version:
        raise ValueError(
            "version required (--version or HERMES_FLEET_VERSION or hermes_cli.__version__)"
        )

    db_home = hermes_db_home()
    release_dir = db_home / "releases" / ref
    tarball = release_dir / "src.tgz"
    download_repo_tarball(repo=repo, ref=ref, dest=tarball)

    scripts_dir = db_home / "scripts"
    # Prefer HERMES_CONTROL_DIR/scripts when set
    control = os.environ.get("HERMES_CONTROL_DIR", "").strip()
    if control:
        scripts_dir = Path(control) / "scripts"
    updated_scripts = refresh_control_plane_scripts_from_tarball(
        tarball, scripts_dir=scripts_dir
    )

    artifact = str(tarball)
    doc: dict[str, Any] = {}
    if storage is not None and getattr(storage, "fleet_release", None) is not None:
        doc = storage.fleet_release.put(
            version=version,
            ref=ref,
            repo=repo,
            published_by=published_by,
            artifact=artifact,
        )
    else:
        from hermes_storage import get_storage, is_mongo_mode

        if not is_mongo_mode():
            raise RuntimeError("Mongo mode required for hermes cluster update")
        st = get_storage(force=True)
        if st.fleet_release is None:
            raise RuntimeError("fleet_release store unavailable")
        doc = st.fleet_release.put(
            version=version,
            ref=ref,
            repo=repo,
            published_by=published_by,
            artifact=artifact,
        )

    restart = restart_control_plane_units_best_effort()
    return {
        "ok": True,
        "fleet_release": doc,
        "artifact": artifact,
        "scripts_dir": str(scripts_dir),
        "scripts_updated": updated_scripts,
        "restart": restart,
        "next_step": "On each agent PC run: hermes update",
    }


def run_mongo_agent_update_cli(args: Any) -> None:
    """CLI entry for Mongo-fork ``hermes update`` / ``hermes update --check``."""
    import json as _json
    import sys

    from hermes_storage import get_storage, is_mongo_mode

    storage = None
    if is_mongo_mode():
        try:
            storage = get_storage(force=True)
        except Exception:
            storage = None

    desired = resolve_desired_fleet_release(storage)
    av = local_agent_version()
    ar = local_install_ref()
    in_sync = versions_in_sync(agent_version=av, install_ref=ar, desired=desired)

    if getattr(args, "check", False):
        print(
            _json.dumps(
                {
                    "in_sync": in_sync,
                    "agent_version": av,
                    "install_ref": ar,
                    "fleet_release": desired,
                    "channel": "mongo-fleet",
                },
                indent=2,
                default=str,
            )
        )
        raise SystemExit(0 if in_sync else 1)

    print("Mongo fleet update (not upstream Nous)…")
    if not desired:
        print("Error: no fleet_release published. On the DB server run:")
        print("  hermes cluster update --version <x.y.z>")
        raise SystemExit(1)
    if in_sync:
        print(
            _json.dumps(
                {
                    "ok": True,
                    "message": "already in sync",
                    "agent_version": av,
                    "install_ref": ar,
                    "fleet_release": desired,
                },
                indent=2,
                default=str,
            )
        )
        return

    result = apply_fleet_update_sync(desired, storage=storage)
    print(_json.dumps(result, indent=2, default=str))
    raise SystemExit(0 if result.get("ok") else int(result.get("exit_code") or 1))

