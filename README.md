# Permit Route server

Reads oversize-permit PDFs and builds the route on real roads. This is the part that goes online.
The app people use is in `../frontend` and only needs this server's address.

## Run it on your PC

1. Python 3.11+ from python.org (Windows: tick "Add python.exe to PATH").
2. In this folder (`server`):
   - Windows: `python -m venv venv`, then `venv\Scripts\activate`, then `pip install -r requirements.txt`
   - Mac: `python3 -m venv venv`, then `source venv/bin/activate`, then `pip install -r requirements.txt`
3. Start: `python -m uvicorn app:app --port 8000 --reload` (Ctrl+C stops it).
   Check: http://localhost:8000/health shows `{"ok":true,"auth":false}`.

## Put it online

See `../RELEASE.md`, step 1. Short version: any host that runs a `Dockerfile` (Render, Railway, Fly.io,
Google Cloud Run, a VPS) with this folder as the root, HTTPS on, and these settings:

| Environment variable | What it does |
|---|---|
| `APP_PASSWORD` | the password users type. **Required on a public server**; without it anyone can use it |
| `APP_SECRET` | signs logins; a long random text: `python -c "import secrets;print(secrets.token_hex(32))"` |
| `WEB_ORIGINS` | optional: only these web addresses of the frontend may call the server, e.g. `https://routes-app.example.com` (comma-separated). Unset = any address (the password still protects it) |
| `OVERPASS_ENDPOINTS` | optional: your own / paid map-data server, e.g. `https://…/api/interpreter` (free public ones are used otherwise) |
| `OVERPASS_HEADER` | optional: key header for that server, e.g. `Authorization: Bearer <key>` |
| `ANTHROPIC_API_KEY` | optional: paid reader for permits from new states (see below) |
| `OLLAMA_URL`, `OLLAMA_MODEL` | optional: a free local AI reader for new states, if you run Ollama somewhere the server can reach |
| `OVERPASS_CACHE_DIR` | where downloaded road data is kept (the Dockerfile uses `/data/overpass_cache`; give the host a persistent disk at `/data`) |

## Permits from new states

The 7 states seen so far (SC GA AL MS LA TX NM) are read by built-in rules. Other states need an AI reader:
rules → Ollama → paid Claude API (only if `ANTHROPIC_API_KEY` is set). Force one with `EXTRACTOR=rules` / `ollama` / `claude`.
Ollama on your PC: install from ollama.com, then `ollama pull qwen3:8b` (several GB, needs about 8 GB of free memory).

## API

| | |
|---|---|
| `GET /health` | `{ok, auth}` |
| `POST /v1/login` | `{password}` → `{token}`; send it as `Authorization: Bearer <token>` |
| `POST /v1/extract` | one permit PDF (multipart, field `file`) → `{leg, problems, method, needs_review}` |
| `POST /v1/order` | `{legs}` → `{legs, confident}` (travel order) |
| `POST /v1/route` | `{legs}` → `{geojson, warnings, needs_review}` |

## Command line check

`python inspect_permits.py "C:\path\to\permits" --golden golden_trip.json --route`
prints, per permit: which reader was used, the checks, a comparison with the hand-checked sample, and the route.

Tests: `python test_route_builder.py`
