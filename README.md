# Hermes Agent — MongoDB (fork)

> ⚠️ **НЕ ГОТОВО.** Эксперимент / WIP. На прод не ставить. Ломается. Тестируй на свой страх и риск.

Форк [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent): «мозг» агента в **self-hosted MongoDB** на обычном сервере (**без Docker**), на ПК — только `bootstrap.yaml` + сертификаты.

Полные доки Hermes: [оригинал](https://github.com/NousResearch/hermes-agent) / [docs](https://hermes-agent.nousresearch.com/docs/).

## Install

### 1) DB-сервер (Ubuntu / Debian)

Нужны: `sudo`, `systemd`, `openssl`, `python3`. **Docker не нужен.**

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/installDB.sh | bash
```

Поставит:
- MongoDB Community (apt) + single-node replica set
- systemd user: `hermes-mongod`, `hermes-enroll` (:8743), `hermes-orchestrator` (:8744 mTLS)

Спросит про подключение агента → one-time code. Позже: `~/hermes-db/agent-add`.

### 2) Агент-ПК

```bash
curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/install-agent.sh | bash
```

Введи адрес `IP:8743` и код с DB-сервера.

### 3) Проверка

```bash
# на DB
systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator

# на агенте
hermes storage status
hermes cluster status
```

Открой фаервол при необходимости: `27017`, `8743`, `8744`.

## License

MIT — как у [upstream](https://github.com/NousResearch/hermes-agent).
