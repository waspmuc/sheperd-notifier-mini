# shepherd-notifier-mini

Minimaler HTTP→Telegram Notification-Sidecar für [Shepherd](https://github.com/containrrr/shepherd) in Docker Swarm.

Shepherd v1.5+ unterstützt keine direkte Telegram-Integration mehr — stattdessen sendet es HTTP-POSTs an eine `APPRISE_SIDECAR_URL`. Dieses Projekt implementiert genau diesen Endpunkt mit ~80 Zeilen Python (stdlib only, keine externen Dependencies).

## Features

- Kein Flask, kein gunicorn, kein nginx — reines Python `http.server`
- ~15 MB RAM statt ~128 MB (caronc/apprise)
- Formatierte Nachrichten mit Emoji (🚧 Staging / 🚀 Prod / ❌ Fehler)
- Zeigt die letzten 3 Commit-Messages via GitHub API
- HTML-Formatierung in Telegram

## Beispiel-Nachrichten

**Erfolgreiches Update:**
```
🚀 Prod — backend aktualisiert
Tag: latest
ID: 5663b638

Commits:
• feat: add reservation endpoint
• fix: null pointer in booking service
• chore: update dependencies
```

**Fehlgeschlagenes Update:**
```
❌ Prod — backend fehlgeschlagen
boxenplatz-prod_boxenplatz-prod-backend
Rollback wurde eingeleitet.
```

## Docker Image

```
ghcr.io/waspmuc/shepherd-notifier:latest
```

## Konfiguration

| Env-Variable       | Beschreibung                                      | Pflicht |
|--------------------|---------------------------------------------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot-Token von BotFather                         | ✅      |
| `TELEGRAM_CHAT_ID`   | Ziel-Chat-ID (User oder Gruppe)                 | ✅      |
| `GITHUB_TOKEN`       | PAT mit `repo`-Scope für Commit-Messages        | ❌      |
| `PORT`               | HTTP-Port (default: `8000`)                     | ❌      |

## Einbindung in Docker Swarm (Shepherd)

```yaml
services:
  shepherd-notifier:
    image: ghcr.io/waspmuc/shepherd-notifier:latest
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      GITHUB_TOKEN: ${GITHUB_TOKEN}
    networks:
      - proxy-network
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.role == manager
      resources:
        limits:
          memory: 32M

  shepherd:
    image: containrrr/shepherd:v1.8.1
    environment:
      APPRISE_SIDECAR_URL: "http://shepherd-notifier:8000/notify"
      # ... weitere Shepherd-Konfiguration
```
