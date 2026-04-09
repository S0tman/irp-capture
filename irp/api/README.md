# IRP REST API v0

Local-first HTTP interface to the IRP decision substrate.

## Install

```bash
pip install irp-capture[api]
```

## Run

```bash
python3 -m irp.api.server --project-root /path/to/your/project
```

Starts on `http://127.0.0.1:3100` by default.

```bash
# Custom port
python3 -m irp.api.server --project-root /path/to/project --port 4000
```

The server logs the project root on startup — always verify it before use:
```
[irp-api] Project root: /Users/yourname/myproject
[irp-api] Starting on http://127.0.0.1:3100
```

---

## Endpoints

### `GET /health`
```bash
curl http://localhost:3100/health
```
```json
{
  "status": "ok",
  "project_root": "/Users/yourname/myproject",
  "irp_dir": "/Users/yourname/myproject/.irp",
  "substrate": { "ledger": true, "current": true }
}
```

---

### `GET /decisions`
Returns active decisions from `current.json` (last 10).
```bash
curl http://localhost:3100/decisions
```
```json
{
  "count": 3,
  "decisions": [...]
}
```

---

### `GET /decisions/{id}`
Returns a specific decision by ID from the full ledger.
```bash
curl http://localhost:3100/decisions/IRP-2026-04-09-002
```

---

### `POST /capture`
Write a new decision to the ledger.
```bash
curl -X POST http://localhost:3100/capture \
  -H "Content-Type: application/json" \
  -d '{
    "what": "Use dark background for all sensor cards",
    "why": "Consistent with IRP brand, better contrast for coloured dots",
    "confidence": "high",
    "source": "api"
  }'
```
```json
{
  "ok": true,
  "id": "IRP-2026-04-10-001",
  "entry": { ... }
}
```

---

### `POST /check`
Check a proposal against active decisions for conflicts.
```bash
curl -X POST http://localhost:3100/check \
  -H "Content-Type: application/json" \
  -d '{"proposal": "introduce a database for caching"}'
```
```json
{
  "command": "check",
  "status": "conflict",
  "match_id": "IRP-2026-03-22-001",
  "matched_on": ["database"],
  ...
}
```

---

## Interactive docs

FastAPI auto-generates interactive docs at:
- `http://localhost:3100/docs` — Swagger UI
- `http://localhost:3100/redoc` — ReDoc

---

## Design notes

- **localhost only by default** — `--host 0.0.0.0` to expose on network (not recommended without auth)
- **No auth in v0** — API key auth is planned for v1
- **Same store as CLI** — reads/writes the same `.irp/` as `irp capture`, `irp why`, the Figma bridge, and the git hook
- **`--project-root` is required** — defaults to cwd, but always pass it explicitly to avoid writing to the wrong ledger
