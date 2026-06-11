# Context — Knobs & Configuration

A complete map of every knob that affects runtime behavior, where its default
lives, and how to set it differently for test vs. live deployments.

Read this before deploying — it's the surface area you need to think about.

---

## Required environment

| Variable | Required for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | scoring, extraction, summary | All LLM calls fail soft (score=0 / empty dict / "" summary) without it, so missing key won't crash a sweep — it just produces nothing. |
| `RESEND_API_KEY` | sending the weekly report (Day 7+) | The report `--dry-run` flag writes HTML to disk and skips Resend. |

---

## Production-relevant environment variables (`.env`)

### Database
| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/lab2scale.db` | For prod use `postgresql://user:pass@host:5432/lab2scale`. Schema is dialect-agnostic. |

### Email (System 3 / Day 7+)
| Variable | Default | Notes |
|---|---|---|
| `REPORT_RECIPIENT` | `team@lab-2-scale.com` | Where the weekly brief goes. |
| `REPORT_FROM` | `reports@lab-2-scale.com` | Must be a domain verified in Resend. |

### LLM models
| Variable | Default | Notes |
|---|---|---|
| `LLM_SCORING_MODEL` | `claude-haiku-4-5` | Used for relevance scoring + structured extraction. |
| `LLM_SUMMARY_MODEL` | `claude-sonnet-4-6` | Used once per weekly report. |

### Sweep behavior
| Variable | Default | Notes |
|---|---|---|
| `SWEEP_METHODS` | `rss` | Comma-separated list for **System 1 (research)**. Set `rss,scrape` to also fetch `web_scrape` sources. Scrape **roughly triples** per-sweep cost on the first run. System 2 (events) ignores this — it uses Tavily search. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. |

### Optional / future
| Variable | Used by | Notes |
|---|---|---|
| `TAVILY_API_KEY` | System 2 (events) | **Required for event discovery.** System 2 finds events via Tavily web search (one query per focus area × city). Without it, events are skipped (research still runs). Free tier: 1000 searches/month; a full sweep uses 15. |
| `CRUNCHBASE_API_KEY` | startup tracking | Not yet wired. |
| `RUN_SCHEDULE`, `REPORT_DAY`, `REPORT_TIME` | scheduler | Documentation only — actual scheduling is configured at the platform level (Railway cron, Cloud Scheduler, etc.). |

---

## Code-level defaults

These live in source. Override via constructor args (for one-off runs in
scripts or tests) or by changing the default in code.

### `BaseAgent` (parent of `DomainAgent`, System 1 research)
| Param | Default | What it does |
|---|---|---|
| `threshold` | `RELEVANCE_THRESHOLD = 8.0` | Items scoring below this are filtered out. `lib/llm.py:RELEVANCE_THRESHOLD`. |
| `methods` | `None` (all) | When set (`{"rss"}`, `{"scrape"}`, etc.), restricts which source methods are fetched. |
| `max_items` | `None` (no cap) | Cap on items scored per agent per run. Cost guard. |
| `week_window_days` | `7` | Rolling lookback for the `published` date filter. `None` disables. |

### `DomainAgent` (research, System 1)
- Inherits all of the above; `week_window_days=7` filters by paper/article publish date.

### `SearchCityAgent` (events, System 2)
Discovers events via **Tavily web search** — no feed configs, no scraping. Pipeline:
`search (5 domain queries/city) → dedup by URL → score (Haiku, snippet) → extract (Haiku, full page text) → date + locality + cross-source filters → store`.

| Param | Default | What it does |
|---|---|---|
| `threshold` | `6.0` | Event relevance keep-threshold (Haiku `score_event_relevance`). |
| `future_horizon_days` | **`30`** | `event_date` must be within today → today+N days. Missing/unparseable dates dropped. `None` disables. |
| `max_results_per_query` | `10` | Tavily results per focus-area query (× 5 domains = up to 50/city). |

Quality filters (in `systems/system2_events/search_city_agent.py`):
- **Locality** (`CITY_LOCALITY`): drops events with no evidence of being in the city's metro. Token-based match on the *extracted* venue/name + snippet (not the raw page, which on aggregator pages lists other cities).
- **Date extraction** uses Tavily `raw_content` (full page text) — snippets rarely contain the date.
- **Cross-source dedup**: collapses the same event from different URLs by `(normalized name, date)`.

### `Scraper` (`lib/scraper.py`)
| Param | Default | What it does |
|---|---|---|
| `user_agent` | `"Lab2Scale-Monitor/1.0 (+https://lab-2-scale.com)"` | Sent with every request. |
| `timeout` | `30.0` seconds | Per-request timeout. |
| `rate_limit` | `2` req/sec **per domain** | Politeness rate limit. |
| `max_attempts` | `3` | Tenacity retry with exponential backoff + `Retry-After` awareness. Only retries 5xx/429/transport errors. |
| `respect_robots` | `True` | Applies to `fetch_page` (HTML scraping). RSS / API fetches skip the check. |

### `LLMFilter` (`lib/llm.py`)
| Param / constant | Default | What it does |
|---|---|---|
| `max_retries` | `3` | SDK-level retry for 429/5xx. |
| `_MAX_CONTENT_CHARS` | `6000` | Content truncated before sending to the model. Cost guard. |
| `RELEVANCE_THRESHOLD` | `6.0` | Imported and used by `BaseAgent`. |

---

## Demo-script env vars (gitignored `scripts/try_*.py`)

These never affect production — they only matter when running the local try scripts.

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `DEMO_DB` | all try scripts | `data/lab2scale.db` | Path to the database. Use `:memory:` for a throwaway run. |
| `DEMO_THRESHOLD` | all | `6.0` | Override the keep-threshold for this run. |
| `DEMO_MAX_ITEMS` | research scripts | `15` | Per-agent cap. Bump to `50`+ for richer runs. |
| `DEMO_DOMAINS` | try_rss / try_scrape | (all 5) | Comma-separated subset. |
| `DEMO_CITIES` | try_event_search | (all 3) | Comma-separated subset of cities. |
| `DEMO_MAX_RESULTS` | try_event_search | `10` | Tavily results per domain query. |
| `DEMO_FUTURE_HORIZON` | try_event_search | `30` | Days. Use `0` to disable the future-horizon filter. |

`scripts/try_event_search.py` is the events smoke test (Tavily search mode). Requires `TAVILY_API_KEY` + `ANTHROPIC_API_KEY`.

---

## Test vs. Live configurations

### Local development / testing
```bash
# .env
DATABASE_URL=sqlite:///data/lab2scale.db
ANTHROPIC_API_KEY=sk-ant-...
LOG_LEVEL=DEBUG
```
- Use try scripts: `DEMO_DB=:memory: DEMO_MAX_ITEMS=15 python scripts/try_rss.py`
- Iterate cheaply — in-memory DB + small caps keep cost under a cent per run
- Run `pytest tests/` for the full offline suite (no API key needed)

### Staging / production
```bash
# .env (production)
DATABASE_URL=postgresql://user:pass@host:5432/lab2scale
ANTHROPIC_API_KEY=sk-ant-...
RESEND_API_KEY=re_...
REPORT_RECIPIENT=team@lab-2-scale.com
REPORT_FROM=reports@lab-2-scale.com
SWEEP_METHODS=rss,scrape
LOG_LEVEL=INFO
```
- Cron / scheduled job runs:
  - **Sweep**: `python main.py sweep` at 6am, 1pm, 8pm ET (= 11:00, 18:00, 01:00 UTC)
  - **Report**: `python main.py report` at 9am ET Monday (= 14:00 UTC Monday)
- Use a verified Resend sender domain
- Monitor: `findings` and `events` row counts per sweep, Anthropic spend, Resend deliverability

---

## Cost notes

| Operation | Model | Approx unit cost |
|---|---|---|
| Score one item | Haiku | ~$0.0005 |
| Extract one item | Haiku (tool_use) | ~$0.001 |
| Weekly summary | Sonnet | ~$0.02–0.05 |

Per-sweep cost is dominated by **how many items survive the date filter**. With
`week_window_days=7` we typically drop ~25–30% of fetched items before any LLM
call, which is the primary cost guard. Dedup (`seen_hashes`) means repeated
sweeps cost near zero.

Rough monthly estimates:
- 21 sweeps/week × ~50 items kept/sweep × Haiku = ~$25–60/month
- 4 weekly reports × Sonnet = ~$0.20–1/month
- Resend (free tier covers < 100 emails/month)

---

## Date semantics — important to internalize

- **Research findings** are filtered by `published` (when the paper/article came out). A finding's `published` must be in the rolling last `week_window_days` (default 7) window.
- **Events** are filtered by `event_date` (when the event happens) *after* LLM extraction. An event's `event_date` must be in the next `future_horizon_days` (default 30) window.
- **Missing dates always drop.** The principle: undated content can't be bucketed into a weekly brief, so it's ignored entirely.
- `dropped_old` counter in stats counts both kinds of drop.

---

## Schedule (per `ARCHITECTURE.md`)

- Sweeps: **3× daily** — 6am, 1pm, 8pm ET
- Report: **weekly** — Monday 9am ET
- Both are stateless — safe to re-run; dedup prevents double-saving.
