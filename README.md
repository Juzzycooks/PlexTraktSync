# PlexTraktSync

Sync your Plex watch history to Trakt.tv with a clean web UI. Built for Unraid but runs anywhere Docker does.

![Dashboard](https://img.shields.io/badge/Web_UI-Dashboard-7b68ee)
![Docker](https://img.shields.io/docker/pulls/juzzycooks/plextraktsync)

## Features

- **One-time setup wizard** – walks you through connecting Plex and Trakt step by step
- **Movies & TV shows** – syncs watched status with timestamps and external IDs (IMDb, TMDb, TVDb)
- **Flexible scheduling** – interval (every X hours), daily at a specific time, or custom cron
- **Encrypted secrets** – all credentials stored with Fernet (AES-128-CBC) encryption at rest
- **Sync history** – full log of past syncs with stats and error tracking
- **Connection testing** – verify Plex and Trakt connections from the UI
- **Trakt device auth** – no redirect URL needed, works great in Docker/headless setups

## Quick Start

```bash
docker run -d \
  --name plextraktsync \
  -p 5088:5000 \
  -v /path/to/config:/config \
  -e FLASK_SECRET_KEY=$(openssl rand -hex 32) \
  --restart unless-stopped \
  juzzycooks/plextraktsync:latest
```

Then open `http://localhost:5088` and follow the setup wizard.

## Docker Compose

```yaml
services:
  plextraktsync:
    image: juzzycooks/plextraktsync:latest
    container_name: plextraktsync
    restart: unless-stopped
    ports:
      - "5088:5000"
    volumes:
      - ./config:/config
    environment:
      - FLASK_SECRET_KEY=your-random-secret-here
```

## Unraid

### Option 1: Community Applications XML Template
1. In Unraid, go to **Docker** → **Add Container** → **Template Repositories**
2. Add: `https://github.com/juzzycooks/PlexTraktSync`
3. The template will appear in Community Applications

### Option 2: Manual Install
1. Go to **Docker** → **Add Container**
2. Repository: `juzzycooks/plextraktsync:latest`
3. Add port mapping: Container `5000` → Host `5088`
4. Add path mapping: Container `/config` → Host `/mnt/user/appdata/plextraktsync`
5. Add variable: `FLASK_SECRET_KEY` = any random string

## Setup

1. **Plex**: Enter your server URL and token
   - Find your token: Plex Web → any media → Get Info → View XML → `X-Plex-Token` in the URL
2. **Trakt**: Create an app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)
   - Set redirect URI to `urn:ietf:wg:oauth:2.0:oob`
   - Copy the Client ID and Client Secret
3. **Authorize**: The app uses device code flow – you'll get a code to enter at trakt.tv/activate
4. **Schedule**: Set up automatic syncing (interval, daily, or cron) in Settings

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | Yes | Random string for Flask session security |
| `ENCRYPTION_KEY` | No | Fernet key for encrypting secrets. Auto-generated if not set |
| `CONFIG_DIR` | No | Config directory path (default: `/config`) |

## License

MIT
