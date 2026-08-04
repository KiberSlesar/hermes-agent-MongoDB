# Hermes Agent — MongoDB fork

> ⚠️ **Alpha.** Основные сценарии работают, но гарантий стабильности,
> совместимости и сохранности данных нет. Не используйте в production без
> резервных копий и собственной проверки.

Форк [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent):
агентский runtime остаётся на домашних ПК, а **долговечное состояние**
(конфиг, память, скиллы, сессии, флот) живёт в **MongoDB** на
сервере. На ПК после enroll нужны в основном `bootstrap.yaml` + сертификаты.

Полные доки Hermes: [оригинал](https://github.com/NousResearch/hermes-agent) /
[docs](https://hermes-agent.nousresearch.com/docs/).

## Что меняется относительно классического Hermes

| | Классика (`~/.hermes`) | Этот форк (Mongo) |
|--|----------------------|-------------------|
| Источник правды | файлы на диске ПК | MongoDB на сервере |
| Несколько ПК | независимые копии | один профиль / флот, handoff messaging |
| Память / soul / secrets | `MEMORY.md`, `SOUL.md`, `.env` | коллекции профиля в Mongo |
| Skills | только локальная папка | shared Mongo + GridFS, кэш на ПК |
| Справочник инфраструктуры | часто пихали в skills / MEMORY | отдельный **fleet wiki** |
| Веб | `hermes dashboard` на том же ПК, что и агент | **control-plane UI у Mongo** + чат на активном агенте |

Локально в Mongo-режиме не правят `config.yaml` / `MEMORY.md` «как SoT» —
они либо кэш, либо путь миграции. Пишите через CLI / tools / веб; при обрыве
Mongo durable-записи могут уйти в локальный outbox и догрузиться позже.

## Где что хранится

### Mongo

| Что | Где | Зачем |
|-----|-----|--------|
| Общие настройки флота | `hermes_shared.settings` | модель, политики, то что общее для ПК |
| Конфиг профиля | `hermes_profile_<name>.config` | профиль агента |
| Secrets / API keys | profile `secrets` | не в git, не в wiki |
| Личность | profile `soul` (SOUL) | кто агент |
| Личная память | profile `memories` (MEMORY / USER) | предпочтения пользователя, личные факты |
| Skills (как делать) | `hermes_shared` + GridFS | процедуры; на ПК — кэш `cache/skills` |
| Wiki (что где лежит) | `hermes_shared.wiki_pages` | адреса, nginx, хосты, runbooks флота |
| Сессии / сообщения | profile `sessions` / `messages` | история чатов |
| Per-PC overlay | `machine_<id>` | cwd, docker, browser, локальный MCP |
| Состояние флота | `hermes_shared` cluster | кто online, `messaging_owner`, `api_base` |

Эффективный конфиг:

```
shared.settings ⊕ profile.config ⊕ machine_<id> overlay
```

### Роли «памяти» — не смешивать

| Слой | Хранит | Не хранит |
|------|--------|-----------|
| **MEMORY / USER** | личные предпочтения, факты о пользователе | IP/nginx/флот-справочник |
| **Skills** | *как* делать (процедуры, шаги) | длинные списки адресов |
| **Wiki** | *что/где* (хосты, URL, порты, runbooks) | пароли, личные секреты |
| **SOUL** | характер / роль агента | операционные факты инфраструктуры |

CLI для wiki: `hermes wiki list|show|put|search|delete`.

### На агент-ПК (локально)

```
$HERMES_HOME/bootstrap.yaml     # URI / enroll
$HERMES_HOME/certs/             # ca.crt, agent.pem
$HERMES_HOME/cache/             # skills cache, mongo outbox, …
$HERMES_HOME/logs/              # логи
```

## Веб-интерфейс (control plane)

Один веб рядом с Mongo — **пульт флота**, а не второй агентский loop на сервере.

- **System / cluster:** статус узлов, activate (messaging owner)
- **Wiki:** просмотр / сохранение справочных страниц
- **Chat:** JSON-RPC через прокси `/api/fleet/ws` → `hermes serve` **активного**
  агента (`messaging_owner`). После handoff UI переподключается сам.

На DB-сервере (после установки агентского runtime на эту же машину или с
доступа к `hermes`):

```bash
export HERMES_FLEET_PROXY_SECRET='<shared-secret>'
hermes control-plane --host 0.0.0.0 --port 9119
```

На каждом агент-ПК (чат должен быть доступен с control plane по сети/VPN):

```bash
export HERMES_API_BASE='http://<agent-lan-ip>:9119'
export HERMES_FLEET_PROXY_SECRET='<shared-secret>'   # тот же секрет
hermes serve --host 0.0.0.0 --port 9119
```

Браузер → `http://<db-server>:9119`. Без `api_base` узел может владеть
Telegram после activate, но веб-чат покажет «not ready».

Порты control plane: `27017` Mongo, `8743` enroll, `8744` orchestrator (mTLS),
`9119` веб (по умолчанию).

## Install

### 1) DB-сервер (Ubuntu / Debian)

Нужны: `sudo`, `systemd`, `openssl`, `python3`.

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash
```

Поставит:
- MongoDB Community (apt) + single-node replica set
- systemd user: `hermes-mongod`, `hermes-enroll` (:8743), `hermes-orchestrator` (:8744 mTLS)

После установки создайте одноразовый код для нового ПК:

```bash
agent-add <agent-name>
```

Пример: `agent-add home-pc`. Команда выведет адрес control plane и команду
подключения. `agent-add` / `agents` попадают в `PATH`.

### 2) Агент-ПК

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex
```

Подключение кодом с DB-сервера:

```bash
hermes db connect --host <DB_SERVER_IP>:8743 --code <ONE_TIME_CODE>
```

Установщик может предложить это интерактивно.

### 3) Проверка

```bash
# на DB
systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator

# на агенте
hermes mongo status
hermes cluster status
```

Фаервол при необходимости: `27017`, `8743`, `8744` (+ `9119` для веб).

## Update

Данные не трогаем: на агенте `HERMES_HOME`, на DB — Mongo `data/` / `certs/` /
`.env`. Меняется runtime агентов (и при полном `installDB` — софт control plane).

### Обычный путь (рекомендуется)

**1. DB-сервер — `hermes cluster update`**

Скачивает клиентский tarball, обновляет scripts control plane, публикует
`fleet_release` в Mongo. Данные Mongo не трогает.

```bash
# на DB (нужен hermes + Mongo bootstrap / доступ к hermes_shared)
export HERMES_FLEET_VERSION=0.19.11   # или --version
hermes cluster update --version 0.19.11 --ref main
```

Полный reinstall control plane по-прежнему через `installDB.sh` при необходимости;
после него тоже можно вызвать `hermes cluster update`.

**2. На каждом агенте — `hermes update`**

На Mongo-агентах штатный `hermes update` **не** тянет Nous ZIP: читает
`fleet_release` с оркестратора/Mongo и ставит tarball форка (как install-agent).

```bash
hermes update          # применить
hermes update --check  # только сравнить версии
```

Автообновление по heartbeat **отключено**.

**3. Проверка**

```bash
hermes update --check
hermes cluster status
```

В веб UI (System): у нод версия / `in_sync` или `stale`, плюс Fleet release.

### Запасной путь: install-agent

Если `hermes update` не смог (Windows lock и т.п.):

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh \
  | HERMES_YES=1 HERMES_SKIP_CONNECT=1 bash
```

Windows (PowerShell):

```powershell
$env:HERMES_YES = "1"; $env:HERMES_SKIP_CONNECT = "1"
irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex
```

### Activate и рассинхрон

`cluster activate` **не блокируется**. При смене owner в чат уходит notice; если
версия ≠ флота — предупреждение: выполните **`hermes update`** на этой машине.

### Чего не делать

- На Mongo-агентах не ждать auto-update и не рассчитывать на upstream Nous ZIP —
  только `hermes update` / install-agent.
- Агенты не обновляют Mongo/DB — DB: `installDB.sh` и/или `hermes cluster update`.
- Новый ПК: `agent-add <agent-name>` + install-agent + `db connect`.
- Низкоуровнево: `hermes fleet release set|show` (предпочтительнее `cluster update`).

## License

MIT — как у [upstream](https://github.com/NousResearch/hermes-agent).
