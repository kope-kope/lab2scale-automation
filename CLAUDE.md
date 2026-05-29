# Lab2Scale Automation System

## What This Is

A multi-system agentic platform that monitors deep tech research and events, then sends a weekly intelligence report to team@lab-2-scale.com. Three systems, eight sub-agents, running 3x daily.

## Project Context

Lab2Scale helps deep tech founders with commercialization. This system automates their research monitoring across five focus areas (power generation, energy storage, power electronics, semiconductors, deep tech infrastructure) and event tracking across three cities (Boston, NYC, SF).

## Architecture

Read these docs in order:
1. **ARCHITECTURE.md** — System design, data flow, execution model
2. **IMPLEMENTATION_SPEC.md** — Database schema, module specs, build order, testing strategy

## Key Design Decisions

- **Three systems, not three scripts.** System 1 (research) has 5 domain sub-agents. System 2 (events) has 3 city sub-agents. System 3 (delivery) compiles and emails.
- **Sub-agents run in parallel** via asyncio.gather().
- **3x daily monitoring** (6am, 1pm, 8pm ET) with weekly report (Monday 9am ET).
- **Content-hash deduplication** prevents duplicate findings across 21 weekly sweeps.
- **Claude Haiku** for scoring/filtering (cost), **Claude Sonnet** for weekly summary (quality).
- **Source configs are YAML files** in `config/domains/` and `config/cities/`. Adding a new domain or city = adding a new YAML file.

## Build Order

Follow IMPLEMENTATION_SPEC.md Section 10 exactly. Build foundation libs first, then System 1, then System 2, then System 3, then integration.

**Phase 1** (Foundation): data_store → dedup → scraper → llm
**Phase 2** (System 1): base_agent → domain_agent → test one domain → orchestrator
**Phase 3** (System 2): city_agent → test one city → orchestrator  
**Phase 4** (System 3): email template → summarizer → email_sender → orchestrator
**Phase 5** (Integration): main.py → end-to-end test → Dockerfile → deploy

## Config Files

Source configs are already written:
- `config/domains/power_generation.yaml` — 102 URLs
- `config/domains/energy_storage.yaml` — 55 URLs
- `config/domains/power_electronics.yaml` — 87 URLs
- `config/domains/semiconductors.yaml` — 66 URLs
- `config/domains/deep_tech_infra.yaml` — 85 URLs
- `config/cities/boston.yaml` — 37 sources
- `config/cities/nyc.yaml` — 55 sources
- `config/cities/sf.yaml` — 52 sources

Total: ~395 research URLs + ~144 event sources.

## Tech Stack

- Python 3.11+, asyncio, httpx, beautifulsoup4, feedparser
- Claude API (anthropic SDK) — Haiku for filtering, Sonnet for summaries
- SQLite (dev) / PostgreSQL via Supabase (prod)
- Resend API for email
- Jinja2 for email templates
- Docker for deployment
- Railway or Google Cloud Run for hosting

## Environment Variables

See `.env.example` (create from IMPLEMENTATION_SPEC.md Section 2). Required:
- `ANTHROPIC_API_KEY`
- `RESEND_API_KEY`
- `REPORT_RECIPIENT` (default: team@lab-2-scale.com)

## Testing

After building each module, test it independently before moving on. The spec has specific test checkpoints at steps 7, 9, 11, 13, and 18.

## Error Handling

- Source failures: log and skip, never block the full agent
- LLM failures: retry 3x, fallback to saving raw item with score=0
- Email failures: retry 3x, save HTML to disk as fallback
- Rate limiting: respect Retry-After headers, 2 req/sec per domain max

## Working Agreement (review cadence)

- ALWAYS hold for the user's review before committing, pushing, merging, or
  starting the next task/day. Build → report → wait for an explicit "go".
- Never bundle commit + push + merge + next-branch into one step. Each is a
  separate checkpoint the user approves.
- Leave changes uncommitted in the working tree for review; do not assume that
  resolving one open question is approval to commit.
