# Hermes Agent — MongoDB fork

> ⚠️ **Alpha.** Основные сценарии работают, но гарантий стабильности,
> совместимости и сохранности данных нет. Не используйте в production без
> резервных копий и собственной проверки.

Форк [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent):
«мозг» агента в **self-hosted MongoDB** на сервере; на ПК — `bootstrap.yaml` +
сертификаты (и локальный runtime Hermes).

Полные доки Hermes: [оригинал](https://github.com/NousResearch/hermes-agent) /
[docs](https://hermes-agent.nousresearch.com/docs/).

## Install

### 1) DB-сервер (Ubuntu / Debian)

Нужны: `sudo`, `systemd`, `openssl`, `python3`.

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash
```

Поставит:
- MongoDB Community (apt) + single-node replica set
- systemd user: `hermes-mongod`, `hermes-enroll` (:8743), `hermes-orchestrator` (:8744 mTLS)

После установки на сервере создайте одноразовый код для нового ПК:

```bash
agent-add <agent-name>
```

Пример: `agent-add home-pc`. Команда выведет адрес control plane и готовую
команду подключения. `agent-add` и `agents` добавляются в `PATH` для новых
установок.

### 2) Агент-ПК

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex
```

Подключите установленный агент кодом с DB-сервера:

```bash
hermes db connect --host <DB_SERVER_IP>:8743 --code <ONE_TIME_CODE>
```

Установщик также предлагает это подключение интерактивно.

### 3) Проверка

```bash
# на DB
systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator

# на агенте — сразу после install / db connect
hermes mongo status

# mTLS к orchestrator
hermes cluster status
```

Открой фаервол при необходимости: `27017`, `8743`, `8744`.

## Update

Данные (`HERMES_HOME` на агенте, Mongo data / certs / `.env` на DB) при
обновлении сохраняются. Меняется runtime и скрипты control plane.

### Агент-ПК

Повторно запустите тот же установщик. Если найден `bootstrap.yaml`, повторный
`db connect` **не нужен** — по умолчанию установщик предлагает **n** (пропустить
коннект). На замену runtime отвечайте **Y**.

Linux (без вопросов — заменить runtime, не трогать коннект):

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh \
  | HERMES_YES=1 HERMES_SKIP_CONNECT=1 bash
```

Windows PowerShell:

```powershell
$env:HERMES_YES = "1"; $env:HERMES_SKIP_CONNECT = "1"
irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex
```

После обновления:

```bash
hermes mongo status
hermes cluster status
```

### DB-сервер

Снова выполните `installDB.sh` (тот же `curl | bash`). Скрипт обновит файлы
control plane и unit’ы; каталоги `data/`, `certs/`, `bundles/` и `.env` не
затираются. Затем проверьте сервисы:

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash
systemctl --user daemon-reload
systemctl --user restart hermes-mongod hermes-enroll hermes-orchestrator
systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator
```

Новых агентов по-прежнему добавляйте так:

```bash
agent-add <agent-name>
```

## License

MIT — как у [upstream](https://github.com/NousResearch/hermes-agent).
