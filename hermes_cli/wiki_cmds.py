"""``hermes wiki`` — fleet technical reference pages in Mongo."""

from __future__ import annotations

import json
from typing import Callable


def build_wiki_parser(subparsers, *, cmd_wiki: Callable) -> None:
    parser = subparsers.add_parser(
        "wiki",
        help="Fleet wiki (technical reference pages in Mongo)",
        description=(
            "Store and recall technical reference (hosts, nginx addresses, "
            "runbooks). Not for procedures — those stay in skills."
        ),
    )
    wiki_sub = parser.add_subparsers(dest="wiki_command")

    lst = wiki_sub.add_parser("list", help="List wiki pages")
    lst.add_argument("--tag", default=None, help="Filter by tag")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_wiki)

    show = wiki_sub.add_parser("show", help="Show one page by slug or title")
    show.add_argument("slug", help="Page slug or title")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_wiki)

    put = wiki_sub.add_parser("put", help="Create or update a wiki page")
    put.add_argument("--title", required=True, help="Page title")
    put.add_argument("--slug", default=None, help="Optional slug (default: from title)")
    put.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="Tag (repeatable)",
    )
    put.add_argument(
        "--body",
        default=None,
        help="Page body (markdown). If omitted, read stdin.",
    )
    put.add_argument(
        "--file",
        default=None,
        help="Read body from a markdown file",
    )
    put.set_defaults(func=cmd_wiki)

    search = wiki_sub.add_parser("search", help="Search wiki titles/bodies/tags")
    search.add_argument("query", help="Search query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_wiki)

    delete = wiki_sub.add_parser("delete", help="Delete a wiki page")
    delete.add_argument("slug", help="Page slug")
    delete.set_defaults(func=cmd_wiki)

    parser.set_defaults(func=cmd_wiki)


def cmd_wiki(args) -> None:
    """Entry point for ``hermes wiki …``."""
    import sys
    from pathlib import Path

    from hermes_storage import get_storage, is_mongo_mode

    if not is_mongo_mode():
        print("Error: Mongo mode required for fleet wiki")
        raise SystemExit(1)

    storage = get_storage(force=True)
    if storage is None or storage.wiki is None:
        print("Error: wiki store unavailable")
        raise SystemExit(1)

    sub = getattr(args, "wiki_command", None)
    if sub == "list":
        pages = storage.wiki.list_pages(tag=getattr(args, "tag", None))
        if getattr(args, "json", False):
            print(json.dumps(pages, indent=2, ensure_ascii=False, default=str))
            return
        if not pages:
            print("(empty wiki)")
            return
        for p in pages:
            tags = ",".join(p.get("tags") or []) or "—"
            print(f"{p.get('slug')}\t{p.get('title')}\t[{tags}]")
        return

    if sub == "show":
        page = storage.wiki.get_page(args.slug)
        if not page:
            # Try title match via search
            hits = storage.wiki.search(args.slug, limit=5)
            if len(hits) == 1:
                page = storage.wiki.get_page(hits[0]["slug"])
            elif hits:
                print("Ambiguous — matching pages:")
                for h in hits:
                    print(f"  {h.get('slug')}\t{h.get('title')}")
                raise SystemExit(1)
        if not page:
            print(f"Not found: {args.slug}")
            raise SystemExit(1)
        if getattr(args, "json", False):
            print(json.dumps(page, indent=2, ensure_ascii=False, default=str))
            return
        print(f"# {page.get('title')}")
        print(f"slug: {page.get('slug')}")
        if page.get("tags"):
            print(f"tags: {', '.join(page['tags'])}")
        if page.get("content_hash"):
            print(f"hash: {page['content_hash']}")
        print()
        print(page.get("body") or "")
        return

    if sub == "put":
        body = getattr(args, "body", None)
        if getattr(args, "file", None):
            body = Path(args.file).read_text(encoding="utf-8")
        elif body is None:
            if sys.stdin.isatty():
                print("Error: provide --body, --file, or pipe markdown on stdin")
                raise SystemExit(1)
            body = sys.stdin.read()
        page = storage.put_wiki_page(
            title=args.title,
            body=body or "",
            slug=getattr(args, "slug", None),
            tags=list(getattr(args, "tags", None) or []),
        )
        queued = " (queued — Mongo outbox)" if page.get("queued") else ""
        print(f"Saved wiki page: {page.get('slug')}{queued}")
        if page.get("content_hash"):
            print(f"hash: {page['content_hash']}")
        return

    if sub == "search":
        hits = storage.wiki.search(args.query, limit=int(getattr(args, "limit", 20) or 20))
        if getattr(args, "json", False):
            print(json.dumps(hits, indent=2, ensure_ascii=False, default=str))
            return
        if not hits:
            print("(no matches)")
            return
        for h in hits:
            tags = ",".join(h.get("tags") or []) or "—"
            print(f"{h.get('slug')}\t{h.get('title')}\t[{tags}]")
        return

    if sub == "delete":
        ok = storage.delete_wiki_page(args.slug)
        print("deleted" if ok else "not found / queued")
        if not ok:
            raise SystemExit(1)
        return

    print("usage: hermes wiki <list|show|put|search|delete>")
    raise SystemExit(2)
