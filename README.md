# Compute-Arr — GPU Compute Router

One API for renting GPUs across several clouds. You submit a job with a GPU class and a preference (`cheapest`, `reliable`, or `fastest`); Compute-Arr picks a provider from live prices, launches the job there, and fails over to the next-best provider if the job can't be placed or dies shortly after launch. It owns no hardware — it is a routing layer on top of RunPod, Vast.ai, and Lambda.

Every price poll, routing decision, and failover is stored in Postgres, so you can see what each job cost and what it would have cost on a baseline provider.

**Status:** Phase 1 MVP. Routing is a rules engine (no ML), auth is a single static API key, and costs are logged but nothing is billed. See [Known limitations](#known-limitations).

> **Jobs launch real, billable instances** on your provider accounts. Cancel them with `POST /jobs/{id}/cancel` when you are done.

## How it works

```
[Client] -> [API] -> [Router] -> [Placement + failover] -> [Provider adapter] -> [Provider cloud]
                        |                                          ^
                 [Offer cache] <- [Poller, every ~90s]             |
                        |                                          |
                    [Postgres] <- [Job monitor, every ~30s] -------+
```

1. A background **poller** asks each configured provider for current offers per GPU class. It appends every offer to `price_snapshots` and refreshes an in-memory cache.
2. `POST /jobs` reads the cache, drops unavailable offers and unhealthy providers, and **ranks** the rest by your preference. The ranking and the reasoning are saved as a **routing decision**.
3. The job is submitted to the top-ranked provider. If that provider rejects it, the next-best provider is tried, and each hop is recorded as a **failover event**.
4. A background **job monitor** keeps each active job's status and cost in sync with its provider. If a job fails within a few minutes of launch, the monitor releases the dead instance and re-places the job on the next provider that hasn't been tried.

A job is attempted on at most `MAX_PLACEMENT_ATTEMPTS` providers (default 3), counting both kinds of failover.

| Preference | Ranking |
|------------|---------|
| `cheapest` (default) | Lowest price per hour |
| `reliable` | Highest availability rate seen by the poller, then price |
| `fastest` | Lowest recent time from placement to running, then price |

`fastest` learns from jobs the router has already placed: providers with no provision history rank last, and with no history at all it behaves like `cheapest`. An unrecognized preference also falls back to `cheapest`.

## Quick start

Prerequisites: Python 3.11+, Docker with Docker Compose, and an API key for at least one provider.

```bash
# 1. Start Postgres
docker compose up -d

# 2. Install dependencies (a virtual environment is recommended)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env             # then edit .env — see Configuration below

# 4. Create the tables
alembic upgrade head

# 5. Run the API
uvicorn app.main:app --reload
```

The API is served at `http://localhost:8000`, with interactive docs at `http://localhost:8000/docs`.

## Configuration

All settings come from environment variables or `.env`. Never commit `.env`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `API_KEY` | `changeme` | Key clients send in the `X-API-Key` header. Change it. |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/compute_arr` | Postgres connection string (matches `docker-compose.yml`). |
| `POLL_INTERVAL_SECONDS` | `90` | How often the poller refreshes prices. |
| `GPU_CLASSES` | `["H100","A100","A40","RTX4090","RTX3090","RTX3080"]` | GPU classes the poller tracks (JSON list). |
| `JOB_SYNC_INTERVAL_SECONDS` | `30` | How often the job monitor syncs active jobs with their providers. |
| `MAX_PLACEMENT_ATTEMPTS` | `3` | Maximum providers tried per job. |
| `EARLY_DEATH_WINDOW_SECONDS` | `300` | A job that fails within this long after placement is retried on another provider. `0` turns this off. |
| `RUNPOD_API_KEY` | empty | Enables the RunPod adapter. |
| `VAST_API_KEY` | empty | Enables the Vast.ai adapter. |
| `LAMBDA_API_KEY` | empty | Enables the Lambda adapter. |
| `LAMBDA_SSH_KEY_NAMES` | `[]` | SSH key names registered in your Lambda account (JSON list). Lambda requires one to launch an instance. |
| `BASELINE_PROVIDER` | `lambda` | Provider that savings in `GET /usage` are measured against. |

A provider is only used when its API key is set. With no keys set, the poller and job monitor do not start and `GET /providers` returns an empty list.

## API

Every endpoint except `/health` requires the key in an `X-API-Key` header.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness and database check (no key needed) |
| GET | `/providers` | Latest cached prices and health per provider |
| POST | `/jobs` | Route and submit a job |
| GET | `/jobs/{id}` | Job status, provider, price, and cost so far |
| POST | `/jobs/{id}/cancel` | Stop the job at its provider and record the final cost |
| GET | `/jobs/{id}/logs` | Provider logs, where the provider exposes them |
| GET | `/usage` | Totals, cost, and estimated savings vs. the baseline |

### Submit a job

```bash
curl -X POST http://localhost:8000/jobs \
  -H "X-API-Key: changeme" \
  -H "Content-Type: application/json" \
  -d '{
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "pytorch/pytorch:latest",
        "command": "python train.py",
        "env_vars": {"EPOCHS": "3"}
      }'
```

Only `gpu_class` and `image` are required. `gpu_class` must match a tracked class exactly, including case (`H100`, not `h100`).

```json
{
  "job_id": "3f0c1c2e-8a54-4d0b-9a55-0f6f6f0f8f11",
  "status": "pending",
  "chosen_provider": "runpod",
  "price_per_hr": 2.49,
  "reasoning": "preference=cheapest; selected runpod at $2.49/hr from 3 candidate(s)"
}
```

The job starts as `pending` and moves to `running`, then `succeeded`, `failed`, or `cancelled` as the monitor syncs it. If placement needed a failover, `chosen_provider` is where the job landed and `reasoning` says so.

A `503` means no provider currently has that GPU class available, or every attempt failed. Check `GET /providers`, or wait for the next poll.

### Cancel a job

```bash
curl -X POST http://localhost:8000/jobs/3f0c1c2e-8a54-4d0b-9a55-0f6f6f0f8f11/cancel \
  -H "X-API-Key: changeme"
```

Returns the job with `status: "cancelled"` and its final cost. Cancelling a job that has already finished returns `409`.

### Usage summary

```bash
curl http://localhost:8000/usage -H "X-API-Key: changeme"
```

```json
{
  "total_jobs": 12,
  "completed_jobs": 9,
  "failed_jobs": 1,
  "total_cost_usd": 41.87,
  "by_provider": {"runpod": 7, "vast": 4},
  "estimated_savings_usd": 18.2,
  "savings_pct": 30.3
}
```

`total_cost_usd` covers every job that ran, including cancelled and failed ones. Savings compare what each job cost with what the baseline provider was charging at the moment the job was routed, over the same duration.

## Project layout

```
app/
  main.py            FastAPI app; builds the adapter registry and starts the scheduler
  config.py          Settings loaded from the environment / .env
  database.py        SQLAlchemy engine and session
  auth.py            X-API-Key check
  dependencies.py    FastAPI dependency that exposes the adapter registry
  router.py          Rules-based ranking (cheapest / reliable / fastest)
  placement.py       Submits a job down the ranking, one provider at a time, logging failovers
  poller.py          Background price and health polling
  monitor.py         Background job sync and failover for jobs that die after launch
  scheduler.py       Runs the poller and the monitor on their intervals
  cache.py           In-memory offer cache
  stats.py           In-memory availability and provision-time stats
  registry.py        Builds adapters for providers that have an API key
  models.py          SQLAlchemy models
  api/               Route handlers: health, jobs, providers, usage
  providers/
    base.py          ProviderAdapter interface and the Offer / JobSpec / JobStatus types
    runpod.py        RunPod (GraphQL)
    vast.py          Vast.ai (REST)
    lambda_labs.py   Lambda (REST)
alembic/             Database migrations
tests/               pytest suite
```

Provider-specific behavior stays inside each adapter; the router only sees normalized `Offer` and `JobSpec` objects. To add a provider, implement `ProviderAdapter` and register it in `app/registry.py`.

### Database tables

| Table | Contents |
|-------|----------|
| `providers` | One row per provider, with current health |
| `price_snapshots` | Append-only price and availability time series |
| `jobs` | Job spec, where it ran, the hourly price it was placed at, status, and cost |
| `routing_decisions` | Ranked alternatives and prices at decision time, the router's pick, and the reason |
| `failover_events` | Each move from one provider to the next, and why |

## Tests

```bash
pytest
```

The suite runs against in-memory SQLite with mocked provider HTTP calls, so it needs neither Postgres nor provider API keys.

## Known limitations

- **Adapters are tested against mocked HTTP only.** There is no integration suite that runs against the live provider APIs.
- **Run a single server process.** The poller and job monitor run inside the API process, so several workers would each poll and monitor.
- **Reliability stats, provision times, and the offer cache live in memory** and reset on restart. Price history, jobs, and decisions in Postgres are unaffected.
- **Job state can lag by one sync interval** (30 seconds by default), and provision times are measured at that resolution.
- **Lambda** reports neither running cost nor logs. Its cost is estimated from the hourly price and elapsed time, and its instances run until cancelled.
- **Cost from an attempt that was failed over is not added** to the job's total.
- **Single static API key** — no users, scopes, or rate limiting.

## Provider terms of service

Some GPU providers restrict reselling or wrapping their service. Review each provider's terms before using this commercially. Adapters are isolated so a provider can be dropped without touching the router.
