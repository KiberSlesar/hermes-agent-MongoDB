---
name: hermes-wiki
description: Store and recall fleet technical reference in Mongo wiki.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [wiki, knowledge, reference, mongo, fleet]
    category: productivity
    related_skills: [llm-wiki]
---

# Hermes Fleet Wiki Skill

Store technical reference in the fleet Mongo wiki — hostnames, nginx addresses,
service URLs, runbooks. Procedures stay in skills; personal prefs stay in MEMORY/USER.

## When to Use

- User says "сохрани в вики…", "запомни адреса…", "wiki…", "справочник…"
- You need a durable lookup (IPs, vhosts, ports) shared across machines
- A skill is filling up with static addresses — move facts to wiki, keep steps in the skill

## Prerequisites

Mongo mode on (`bootstrap.yaml` / `hermes db connect`). Wiki lives in shared DB.

## How to Run

Use `terminal` with `hermes wiki` (do not invent a local markdown wiki under HERMES_HOME).

```bash
# Save / update a page
hermes wiki put --title "Nginx addresses" --tag nginx --tag network --body "$(cat <<'EOF'
## edge
- edge-01: 10.0.0.11
- edge-02: 10.0.0.12
EOF
)"

# Search
hermes wiki search "nginx"

# Show full page
hermes wiki show nginx-addresses

# List
hermes wiki list
hermes wiki list --tag nginx
```

Pipe body from stdin when convenient:

```bash
printf '%s\n' '## prod' '- api.example.com → 10.1.2.3' | hermes wiki put --title "API hosts" --tag api
```

## Quick Reference

| Goal | Command |
|------|---------|
| Save facts | `hermes wiki put --title "…" --tag x --body "…"` |
| Find | `hermes wiki search "query"` |
| Read | `hermes wiki show <slug>` |
| List | `hermes wiki list [--tag t]` |
| Delete | `hermes wiki delete <slug>` |

Slug defaults from the title (`Nginx addresses` → `nginx-addresses`).

## Procedure

1. Extract the technical facts the user wants stored (no secrets/passwords).
2. Choose a clear title + tags (`nginx`, `dns`, `comfyui`, …).
3. Run `hermes wiki put` with markdown body.
4. Confirm with `hermes wiki show <slug>` and report the slug + hash to the user.
5. When answering later, `hermes wiki search` first; `show` only the matching page.

## Pitfalls

- Do **not** put API keys/passwords in wiki — use secrets.
- Do **not** replace a skill with wiki text: skill = how; wiki = what/where.
- Existing `llm-wiki` skill is a local markdown KB (`WIKI_PATH`). Fleet shared facts belong here via `hermes wiki`.
- If Mongo is down, put may queue to outbox — tell the user and suggest `hermes storage flush-outbox` after reconnect.

## Verification

```bash
hermes wiki search "<keyword>"
hermes wiki show <slug>
hermes mongo status   # Wiki: N pages
```
