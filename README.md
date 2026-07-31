# Hermes Agent — MongoDB fork

> ⚠️ **Alpha.** Основные сценарии работают, но гарантий стабильности,
> совместимости и сохранности данных нет. Не используйте в production без
> резервных копий и собственной проверки.

Форк [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent): «мозг» агента в **self-hosted MongoDB** на обычном сервере (**без Docker**), на ПК — только `bootstrap.yaml` + сертификаты.

Полные доки Hermes: [оригинал](https://github.com/NousResearch/hermes-agent) / [docs](https://hermes-agent.nousresearch.com/docs/).

## Install

### 1) DB-сервер (Ubuntu / Debian)

Нужны: `sudo`, `systemd`, `openssl`, `python3`. **Docker не нужен.**

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash
```

Поставит:
- MongoDB Community (apt) + single-node replica set
- systemd user: `hermes-mongod`, `hermes-enroll` (:8743), `hermes-orchestrator` (:8744 mTLS)

После установки на сервере создайте одноразовый код для нового ПК:

```bash
agent-add hermes-windows
```

Команда выведет адрес control plane и готовую команду подключения. `agent-add`
и `agents` автоматически добавляются в `PATH` для новых установок.

### 2) Агент-ПК

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash

# non-interactive upgrade (replace existing install, keep HERMES_HOME data):
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh \
  | HERMES_YES=1 HERMES_SKIP_CONNECT=1 bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex
```

Подключите установленный агент кодом, выданным на DB-сервере:

```bash
hermes db connect --host <DB_SERVER_IP>:8743 --code <ONE_TIME_CODE>
```

Установщик также предлагает это подключение интерактивно.

### 3) Проверка

```bash
# на DB
systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator

# на агенте — штатная команда (сразу после install / db connect)
hermes mongo status

# проверка mTLS-подключения к orchestrator
hermes cluster status
```

Открой фаервол при необходимости: `27017`, `8743`, `8744`.

## License

MIT — как у [upstream](https://github.com/NousResearch/hermes-agent).
