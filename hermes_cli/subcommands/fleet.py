"""``hermes fleet`` — low-level release publish / check (prefer ``cluster update`` / ``update``)."""

from __future__ import annotations

from typing import Callable


def build_fleet_parser(subparsers, *, cmd_fleet: Callable) -> None:
    parser = subparsers.add_parser(
        "fleet",
        help="Fleet release show/set/check (prefer: hermes cluster update / hermes update)",
        description=(
            "Low-level Mongo fleet_release tools. "
            "Prefer: hermes cluster update (DB) and hermes update (agents)."
        ),
    )
    fleet_sub = parser.add_subparsers(dest="fleet_command")

    rel = fleet_sub.add_parser("release", help="Show or set desired fleet release")
    rel_sub = rel.add_subparsers(dest="fleet_release_command")

    show = rel_sub.add_parser("show", help="Show published fleet_release")
    show.set_defaults(func=cmd_fleet)

    st = rel_sub.add_parser("set", help="Publish desired version/ref for agents")
    st.add_argument("--version", required=True, help="Semver / package version string")
    st.add_argument("--ref", default="main", help="Git ref/branch/tag for tarball (default main)")
    st.add_argument(
        "--repo",
        default="KiberSlesar/hermes-agent-MongoDB",
        help="GitHub owner/repo for install-agent tarball",
    )
    st.add_argument("--published-by", default="cli", dest="published_by")
    st.set_defaults(func=cmd_fleet)

    check = fleet_sub.add_parser("check", help="Compare this agent vs fleet_release")
    check.set_defaults(func=cmd_fleet)

    upd = fleet_sub.add_parser(
        "update",
        help="Deprecated alias — use hermes update",
    )
    upd.add_argument(
        "--now",
        action="store_true",
        help="Ignored (kept for compatibility); use hermes update",
    )
    upd.set_defaults(func=cmd_fleet)

    parser.set_defaults(func=cmd_fleet)


def cmd_fleet(args) -> None:
    import json as _json

    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.fleet_update import (
        local_agent_version,
        local_install_ref,
        normalize_release,
        versions_in_sync,
    )

    if not is_mongo_mode():
        print("Error: Mongo mode required for hermes fleet")
        raise SystemExit(1)
    storage = get_storage(force=True)
    if storage.fleet_release is None:
        print("Error: fleet_release store unavailable")
        raise SystemExit(1)

    cmd = getattr(args, "fleet_command", None)
    if cmd == "release":
        sub = getattr(args, "fleet_release_command", None) or "show"
        if sub == "show":
            print(_json.dumps(storage.fleet_release.get(), indent=2, default=str))
            return
        if sub == "set":
            doc = storage.fleet_release.put(
                version=args.version,
                ref=getattr(args, "ref", None) or "main",
                repo=getattr(args, "repo", None) or "KiberSlesar/hermes-agent-MongoDB",
                published_by=getattr(args, "published_by", None) or "cli",
            )
            print(_json.dumps({"ok": True, "fleet_release": doc}, indent=2, default=str))
            print("Agents should run: hermes update")
            return
        print("usage: hermes fleet release <show|set>")
        raise SystemExit(2)

    if cmd == "check":
        desired = normalize_release(storage.fleet_release.get())
        av = local_agent_version()
        ar = local_install_ref()
        ok = versions_in_sync(agent_version=av, install_ref=ar, desired=desired)
        print(
            _json.dumps(
                {
                    "in_sync": ok,
                    "agent_version": av,
                    "install_ref": ar,
                    "fleet_release": desired,
                },
                indent=2,
                default=str,
            )
        )
        raise SystemExit(0 if ok else 1)

    if cmd == "update":
        print("hermes fleet update is deprecated.")
        print("On agents run: hermes update")
        print("On DB server run: hermes cluster update --version <x.y.z>")
        raise SystemExit(2)

    print("usage: hermes fleet <release|check|update>")
    raise SystemExit(2)
