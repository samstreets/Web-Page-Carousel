# 📺 Web Page Carousel

A Docker-based webpage carousel for TV displays. Cycles through a list of URLs at a configurable interval and keeps the screen awake.

[![Docker Hub](https://img.shields.io/docker/pulls/samuelstreets/tv-carousel)](https://hub.docker.com/r/samuelstreets/tv-carousel)

---

## 🚀 Quick Start

```bash
docker compose up -d
```

Open **http://localhost:8080** on your browser.

---

## ⚙️ Configuration

Edit `docker-compose.yml` to customise:

```yaml
environment:
  # Comma-separated URLs to cycle through
  PAGES: >-
    https://example.com,
    https://wikipedia.org,
    https://news.ycombinator.com

  # Seconds per page
  INTERVAL: 30
```

| Variable   | Default                | Description                              |
|------------|------------------------|------------------------------------------|
| `PAGES`    | `https://example.com`  | Comma-separated list of URLs to display  |
| `INTERVAL` | `30`                   | Seconds each page is displayed           |

---

## 🛡️ Keep-Awake

The app uses two strategies to prevent TV sleep:

1. **Screen Wake Lock API** — requests a browser wake lock when supported
2. **Canvas pixel flicker** — periodically mutates a 1×1 invisible canvas to signal activity

---

## 🐳 Docker Hub

Image is auto-published to [`samuelstreets/tv-carousel`](https://hub.docker.com/r/samuelstreets/tv-carousel) on every push to `main`.

---

## 🔧 GitHub Actions Setup

Add these secrets to your GitHub repo (`Settings → Secrets → Actions`):

| Secret               | Value                          |
|----------------------|--------------------------------|
| `DOCKERHUB_USERNAME` | `samuelstreets`                |
| `DOCKERHUB_TOKEN`    | Your Docker Hub access token   |

Generate a token at: **Docker Hub → Account Settings → Security → New Access Token**

---

## 📦 Run Without Compose

```bash
docker run -d \
  -p 8080:80 \
  -e PAGES="https://example.com,https://wikipedia.org" \
  -e INTERVAL=45 \
  --restart unless-stopped \
  samuelstreets/tv-carousel:latest
```
