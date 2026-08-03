# Hermes Control Plane

Remote MongoDB (replica set) + certificate authority that **authorizes agent PCs**.

There are **two installers** in this project:

| Script | Where | Purpose |
|--------|-------|---------|
| [`scripts/install-control-plane.sh`](../../scripts/install-control-plane.sh) / `.ps1` | Always-on server | Mongo RS + CA + enroll |
| [`scripts/install-agent.sh`](../../scripts/install-agent.sh) / `.ps1` | Each PC | Hermes binary + bootstrap/certs |

## Ports on the control plane

| Port | Service | Auth |
|------|---------|------|
| 27017–27019 | MongoDB RS | X.509 (or SCRAM lab fallback) |
| **8744** | **Orchestrator API** | **mTLS — client cert required or connection dropped** |
| 8743 | Enroll (one-time code) | Code only (pre-cert bootstrap) |
| **9119** | **Fleet web UI** (`hermes control-plane`) | Dashboard auth; chat WS proxied to active agent |

### Fleet web UI (chat follows active agent)

On the Mongo-adjacent server:

```bash
export HERMES_FLEET_PROXY_SECRET='long-random-shared-secret'
# same secret in Mongo secrets / agent env
hermes control-plane --host 0.0.0.0 --port 9119
```

On each agent PC (always-on):

```bash
export HERMES_API_BASE='http://192.168.1.10:9119'   # reachable from control plane
export HERMES_FLEET_PROXY_SECRET='long-random-shared-secret'
hermes serve --host 0.0.0.0 --port 9119
```

Browser opens the control-plane UI; Chat uses `/api/fleet/ws` → messaging owner's serve
(short-lived HMAC tickets over `Authorization: Bearer`, never the dashboard loopback token).
Activate agents on System page as before (`hermes cluster activate` / dashboard buttons).

Agents without `api_base` still own Telegram after activate, but web chat shows “not ready”.
After activate / handoff, the Chat tab polls `messaging_owner` and reconnects the proxy automatically.

### Fleet updates (manual)

Step-by-step (Russian): root [`README.md`](../../README.md) → **Update**.

1. On DB: `hermes cluster update --version … --ref main` (download client
   tarball, refresh control-plane scripts, publish `fleet_release`).
2. On each agent: `hermes update` (Mongo installs follow fleet_release; not
   upstream Nous ZIP). Idle auto-apply is **off**.
3. Check: `hermes update --check` / `hermes cluster status` / System UI.
4. Fallback: install-agent with `HERMES_YES=1` + `HERMES_SKIP_CONNECT=1`.

Activate stays unblocked; version skew notice tells the user to run `hermes update`.


Agents call `https://<server>:8744/cluster/*` with the same `agent.pem` used for Mongo.
Without that cert the TLS handshake fails — no anonymous orchestrator access.

**On the server** (after `install-control-plane.sh`):

```bash
hermes agent add
# or: hermes agent add --name home-pc --ttl 300 --hosts 192.168.1.10:27017,192.168.1.10:27018,192.168.1.10:27019
```

You get a one-time code (e.g. `K7Q2-M9XB`). The server waits ~5 minutes.

**On the agent PC:**

```bash
hermes db connect
```

Enter the server address (`IP:8743`) and the code. The PC downloads its X.509
bundle, writes `bootstrap.yaml` + `certs/`, and verifies Mongo.

Legacy offline bundles (`./scripts/enroll-agent.sh --name …`) still work.

## Agent authorization (X.509)

Preferred model: **mutual TLS with client certificates**.

1. Control plane generates a private CA (`certs/ca.key` — never leave the server).
2. `enroll-agent.sh --name home-pc` issues a client cert:
   - Subject: `CN=home-pc,OU=hermes-agents,O=Hermes`
   - Registers that subject as a MongoDB `$external` user with read/write on
     `hermes_shared` + `hermes_profile_<profile>`
3. The PC receives a **bundle**:
   - `bootstrap.yaml` (URI with `authMechanism=MONGODB-X509`, no password)
   - `certs/ca.crt`
   - `certs/agent.pem` (certificate + private key)
4. Hermes connects with TLS; Mongo authenticates the cert. Compromising one PC
   does not reveal other agents’ keys. Revoke by dropping the `$external` user.

### Fallback: SCRAM user/password

Lab/dev only: credentials in `certs/app-credentials.txt` and a URI like
`mongodb://hermesApp:***@hosts/?replicaSet=rs0`. Prefer X.509 for real fleets.

### Optional online enroll API

```bash
# on control plane
HERMES_ENROLL_TOKEN=... docker compose --profile enroll up -d
# on agent PC
./scripts/install-agent.sh --enroll-url http://cp:8743/enroll --token "$TOKEN" --name home-pc
```

## Quick start

```bash
# Server
./scripts/install-control-plane.sh
# Edit deploy/control-plane/.env → HERMES_MONGO_HOSTS=your.lan.ip:27017,...
cd deploy/control-plane && ./scripts/enroll-agent.sh --name home-pc

# PC (copy bundles/home-pc or home-pc.zip first)
./scripts/install-agent.sh --bundle ./home-pc
hermes storage status
hermes cluster status
```

Windows: `.\scripts\install-control-plane.ps1` and `.\scripts\install-agent.ps1`.

## Lab (no TLS, for integration tests)

```bash
docker compose -f deploy/control-plane/docker-compose.lab.yml up -d
export HERMES_MONGO_URI='mongodb://127.0.0.1:27017/?directConnection=true'
pytest tests/hermes_storage/test_mongo_integration.py -m integration -q
```

## Layout

```
deploy/control-plane/
  docker-compose.yml
  .env.example
  certs/                 # CA, server PEM, keyfile (gitignored secrets)
  bundles/<name>/        # per-agent enrollment output
  scripts/
    gen-ca.sh
    init-replica.sh
    enroll-agent.sh
    enroll_server.py
```
