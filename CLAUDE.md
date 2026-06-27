# GPU Compute Router — Phase 1 MVP

## What we're building

A **GPU compute router**: an asset-light aggregation layer that sits on top of existing GPU cloud providers. A developer submits a workload through one unified API; we route it to the optimal provider (cheapest / fastest / most reliable per their preference) across providers like RunPod, Vast.ai, Lambda, and a spot tier. The user never touches individual provider accounts.

We own **no hardware**. The product is software: a unified API, a routing engine, health/price polling, automatic failover, and usage logging.

## Why this exists (the thesis)

The same GPU sells at wildly different prices across providers — an H100 has been tracked at a ~13.8x spread ($0.80–$11.10/hr). No single provider wins every GPU type (H100 cheapest on RunPod, A100 on Lambda, consumer GPUs on Vast.ai), and prices move fast (down 64–75% in ~15 months). That fragmentation and volatility is hard for a human to shop continuously — which is the router's job.

**The moat is data**: every job teaches us which provider is actually cheap / fast / reliable right now. That price-and-reliability intelligence compounds with volume and is what a new entrant can't copy.

## Scope guardrails (READ BEFORE CODING)

This is a **thin MVP**. The goal is to prove automated routing produces measurable savings or reliability gains over a developer using one provider directly. Build only what's below.

**IN scope for Phase 1:**
- Integrate **2–3 providers only**. Start with RunPod + Vast.ai + one reliable baseline (Lambda).
- Unified job-submission API (accept a job spec, normalize, submit to a provider).
- **Routing v1 is intentionally dumb**: rules engine on live price + availability. No ML.
- Provider health/price polling with caching.
- Automatic failover (if a job fails to place or dies, retry on next-best provider).
- Usage + decision logging to a database. **This data is the future moat — capture it from day one.**
- A minimal usage view (cost per job, where it ran, savings vs. baseline). API-first; skip polished UI.

**OUT of scope (do NOT build yet):**
- ML-based routing
- Polished frontend / dashboard beyond a basic read view
- More than ~3 providers
- Enterprise compliance (SOC2/HIPAA), auth beyond a simple API key
- Our own compute supply
- Billing/payments integration (log cost; don't charge yet)

## Suggested architecture

A modular monolith. Keep provider logic behind a common interface so adding/removing providers is cheap.

```
[Client] -> [API layer] -> [Router] -> [Provider Adapter] -> [Provider Cloud]
                              |              ^
                              v              |
                        [Price/Health cache] |
                              |              |
                          [Job + decision store (Postgres)]
                              ^
                        [Poller (background)]
```

### Components

1. **API layer** — REST. Endpoints:
   - `POST /jobs` — submit a job spec, returns job id + chosen provider + reasoning
   - `GET /jobs/{id}` — status, provider, cost so far
   - `GET /jobs/{id}/logs` — pass-through logs if available
   - `GET /providers` — current cached price/health snapshot
   - `GET /usage` — aggregate: jobs, total cost, estimated savings vs. baseline
   - Auth: single static API key via header for now.

2. **Provider Adapter interface** — every provider implements the same contract:
   - `get_offers(gpu_class) -> [Offer]` (price, region, availability, gpu type)
   - `submit(job_spec) -> provider_job_id`
   - `status(provider_job_id) -> {state, cost_so_far}`
   - `cancel(provider_job_id)`
   - `logs(provider_job_id) -> str` (optional)
   Implement: `RunPodAdapter`, `VastAdapter`, `LambdaAdapter`. Read each provider's API docs for auth + endpoints; do not hardcode credentials (use env vars).

3. **Router (v1, rules-based)** — given a normalized job spec and a preference (`cheapest` | `fastest` | `reliable`):
   - Pull current offers from the price/health cache for the requested GPU class.
   - Filter to providers reporting availability and healthy status.
   - Rank: `cheapest` = lowest price/hr; `reliable` = highest recent success rate then price; `fastest` = lowest recent provision time then price.
   - Return ranked list; pick top, record the decision + the alternatives considered (this is moat data).

4. **Failover** — wrap submission: try top-ranked; on placement failure or early job death, advance to next-ranked, log the failover event. Cap retries (e.g. 3).

5. **Poller (background task)** — periodically (e.g. every 60–120s) refresh each provider's offers + health into the cache. Track rolling success rate and avg provision time per provider per GPU class — these feed the `reliable`/`fastest` rankings.

6. **Data store (Postgres)** — tables:
   - `providers` (static config + current health)
   - `price_snapshots` (provider, gpu_class, price, ts) — time series, never overwrite
   - `jobs` (id, spec, preference, chosen_provider, baseline_provider, status, actual_cost, created_at)
   - `routing_decisions` (job_id, ranked alternatives + prices at decision time, chosen, reason)
   - `failover_events` (job_id, from_provider, to_provider, reason, ts)

## Tech stack

- **Language**: Python (provider SDKs friendliest here). FastAPI for the API.
- **DB**: Postgres. SQLAlchemy + Alembic for migrations.
- **Background work**: start simple — APScheduler or a FastAPI startup task for the poller. Add a real queue (Redis/RQ, or Temporal) only if/when durability is needed.
- **Config**: pydantic-settings; all provider keys via env vars (`.env`, never committed).
- **Tests**: pytest. Mock provider HTTP calls — do not hit real provider APIs in unit tests.

## First milestones (build in this order)

1. **Project skeleton** — FastAPI app, config loading, Postgres connection, Alembic init, healthcheck endpoint, `.env.example`, README with run instructions, docker-compose for local Postgres.
2. **Data models + migrations** — the tables above.
3. **Provider adapter interface + one real adapter (RunPod)** — `get_offers` + `submit` + `status`, with mocked tests. Prove the contract end to end with one provider.
4. **Poller + price/health cache** — populate `price_snapshots`, expose `GET /providers`.
5. **Router v1 (cheapest only first)** — `POST /jobs` picks cheapest available, records a routing decision.
6. **Add second + third adapters** (Vast.ai, Lambda) behind the same interface.
7. **Failover** — wrap submission with ranked retry + `failover_events` logging.
8. **`reliable` / `fastest` preferences** — once the poller has rolling stats to rank on.
9. **`GET /usage`** — aggregate cost + savings vs. a configured baseline provider.

## Definition of done for Phase 1

- A developer can `POST /jobs` with a GPU class + preference and get a real job placed on the chosen provider, with the routing decision and alternatives logged.
- If the first provider fails, the job automatically retries on the next-best and the failover is recorded.
- `GET /usage` shows total cost and estimated savings vs. baseline.
- Every routing decision and price snapshot is persisted (the moat data).

## Critical risk to design around from day one

**Provider Terms of Service.** Some providers restrict reselling/wrapping their service. Before relying on any provider, check their ToS — ideally pursue an official reseller/affiliate arrangement (also improves margin). Keep each adapter isolated so a provider can be swapped or dropped without touching the router. (Not legal advice — get ToS reviewed before commercializing.)

## Conventions

- Keep provider-specific quirks inside the adapter; the router only ever sees normalized `Offer` / `JobSpec` objects.
- Never overwrite price history — append snapshots (the time series is an asset).
- Log the reasoning behind every routing decision, not just the outcome.
- No credentials in code or git. Ever.
