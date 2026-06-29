# Compute-Arr — GPU Compute Router

An asset-light aggregation layer that routes GPU workloads to the optimal provider (cheapest / fastest / most reliable) across RunPod, Vast.ai, and Lambda.

## Quick start

### 1. Prerequisites

- Docker + Docker Compose
- Python 3.11+

### 2. Start Postgres

```bash
docker compose up -d
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and add your provider API keys
```

### 5. Run migrations

```bash
alembic upgrade head
```

### 6. Start the API

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`.

Interactive docs: `http://localhost:8000/docs`

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness + DB check |
| POST | `/jobs` | Submit a GPU job |
| GET | `/jobs/{id}` | Job status + cost |
| GET | `/jobs/{id}/logs` | Provider logs |
| GET | `/providers` | Current price/health snapshot |
| GET | `/usage` | Aggregate cost + savings |

## Auth

Pass your API key in the `X-API-Key` header.

## Running tests

```bash
pytest
```
