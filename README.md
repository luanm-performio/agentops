# AgentOps

## Run with Docker Compose

The Compose stack runs database migrations, the Django web UI, and the Django-Q background worker. The existing `tools/config.yaml` is mounted read-only and is never copied into the image.

```bash
docker compose up --build
```

Open <http://localhost:8000>. To create the first login:

```bash
docker compose exec web python manage.py createsuperuser
```

The SQLite database is stored on the host at `data/db.sqlite3`, so it survives container rebuilds and removal.

### Back up SQLite

For a consistent backup while the app is running, use SQLite's backup API inside the web container:

```bash
docker compose exec web python -c "import sqlite3; source = sqlite3.connect('/app/data/db.sqlite3'); backup = sqlite3.connect('/app/data/db-backup.sqlite3'); source.backup(backup); backup.close(); source.close()"
```

The backup will be available as `data/db-backup.sqlite3` on the host. Move or rename it after creation so the next backup does not overwrite it.

### Configuration

Optional environment variables:

- `AGENTOPS_PORT` — host port, default `8000`.
- `DJANGO_SECRET_KEY` — change this outside local development.
- `DJANGO_DEBUG` — defaults to `true`.
- `DJANGO_ALLOWED_HOSTS` — comma-separated hostnames.
- `LOCK_MONITOR_ALERT_AGENT_ID` — optional agent id used to send lock monitor Slack alerts.
- `LOCK_MONITOR_ALERT_AGENT_NAME` — optional agent name fallback when no id is set.
- `LOCK_MONITOR_ALERT_RECIPIENT` — Slack recipient wording for the alert prompt, default `me`.

SSH keys or CA files referenced by `tools/config.yaml` must also be accessible inside the container. Mount them in `docker-compose.yml` and use their container paths in the configuration.

Lock monitor command parameters also support per-run or per-schedule alert overrides:

```json
{
  "tenant_host": "tenant.performio.com",
  "long_running_seconds": 300,
  "alert_threshold_seconds": 7200,
  "alert_agent_id": "1",
  "alert_agent_name": "",
  "alert_recipient": "me"
}
```

Set `alert_threshold_seconds` to `0` to disable alerting for a run or schedule.
