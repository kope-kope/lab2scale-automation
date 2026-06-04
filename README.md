# Lab2Scale Automation System

A multi-agent platform that monitors deep tech research and events three
times a day across five focus areas and three cities, then emails a weekly
intelligence brief to the team.

- **System 1** — 5 domain agents (power generation, energy storage, power
  electronics, semiconductors, deep tech infrastructure) scoring papers,
  news, and patents.
- **System 2** — 3 city agents (Boston, NYC, SF) tracking conferences,
  seminars, and meetups.
- **System 3** — weekly delivery: queries everything unreported, has Claude
  Sonnet write the executive summary, renders the email, ships via Resend.

All three share one SQLite/Postgres data store, one shared `Scraper`, one
shared `LLMFilter`, and a unified dedup layer (`seen_hashes`).

For the design rationale read **`ARCHITECTURE.md`**; for every knob you can
turn at deploy time read **`context.md`**.

---

## Prerequisites

- Python 3.11+ (3.12 tested)
- An Anthropic API key (Claude Haiku + Sonnet)
- A Resend API key (only required when actually sending email; previews work
  without one)

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/kope-kope/lab2scale-automation.git Automation
cd Automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY=sk-ant-...
# For email later: RESEND_API_KEY=re_..., REPORT_RECIPIENT, REPORT_FROM

# 3. Create the database
python main.py init-db

# 4. Run a research sweep (RSS only — cost-safe default ~$0.10–0.50)
python main.py sweep

# 5. Preview the weekly brief — no email is sent, HTML is written to disk
python main.py report --dry-run
open data/latest_report.html       # macOS; xdg-open on Linux
```

---

## Commands

```bash
python main.py init-db                 # Create tables (idempotent)
python main.py sweep                   # System 1 + 2 monitoring sweep
python main.py report [--dry-run]      # System 3 weekly brief
python main.py full [--dry-run]        # Sweep, then report
```

`--dry-run` on `report`/`full` writes the rendered HTML to
`data/latest_report.html` instead of calling Resend. It still marks the
included items as reported — preview = published, from the system's POV.

### Environment toggles

| Variable | What it does | Default |
|---|---|---|
| `SWEEP_METHODS` | `rss`, `scrape`, or `rss,scrape` | `rss` |
| `DATABASE_URL` | `sqlite:///...` or `postgresql://...` | `sqlite:///data/lab2scale.db` |
| `LLM_SCORING_MODEL` | Haiku model for scoring + extraction | `claude-haiku-4-5` |
| `LLM_SUMMARY_MODEL` | Sonnet model for the weekly summary | `claude-sonnet-4-6` |
| `REPORT_RECIPIENT` | Where the brief goes | `team@lab-2-scale.com` |
| `REPORT_FROM` | Verified Resend sender | `reports@lab-2-scale.com` |
| `LOG_LEVEL` | Standard Python log level | `INFO` |

Full reference, including code-level defaults and a test-vs-live config
sheet, lives in **`context.md`**.

---

## Cost discipline

- **`SWEEP_METHODS=rss`** is the cost-safe default — fetches the ~85 RSS
  sources, drops anything older than the rolling window before any LLM
  call.
- **`SWEEP_METHODS=rss,scrape`** unlocks the ~200 `web_scrape` sources;
  roughly doubles per-sweep cost on the first run, near-zero thereafter
  (dedup).
- Dedup via `seen_hashes` means re-running `sweep` minutes apart costs
  essentially nothing.

Rough numbers:
- One full RSS sweep: ~$0.30–1.50 (scales with how many items are inside
  the date window).
- One weekly Sonnet summary: ~$0.02–0.05.
- Empty-week heartbeat brief: $0 (no Sonnet call).

---

## Schedule (production)

Per `ARCHITECTURE.md`:

```
Daily 06:00 / 13:00 / 20:00 ET  →  python main.py sweep
Monday 09:00 ET                 →  python main.py report
```

Translation to UTC for cron: `0 11,18,1 * * *` (sweep) and `0 14 * * 1`
(report).

---

## Testing

```bash
pytest tests/                 # offline, no API keys required
pytest tests/ -q --tb=short   # quiet
```

Each module has unit tests; orchestrator-level tests use fake scrapers and
fake LLMs against an in-memory SQLite. Live verification scripts live in
`scripts/try_*.py` (gitignored).

---

## Project layout

```
Automation/
├── README.md, ARCHITECTURE.md, CLAUDE.md, IMPLEMENTATION_SPEC.md,
│   PROJECT_PLAN.md, context.md       — docs
├── config/
│   ├── domains/{power_generation,...}.yaml   — System 1 source configs
│   └── cities/{boston,nyc,sf}.yaml             — System 2 source configs
├── lib/
│   ├── data_store.py   — async SQLite (aiosqlite)
│   ├── dedup.py        — content hashing + seen_hashes
│   ├── scraper.py      — httpx + feedparser + generic article extractor
│   ├── llm.py          — Anthropic SDK wrapper (Haiku + Sonnet)
│   └── email_sender.py — Resend wrapper
├── systems/
│   ├── base_agent.py                 — shared pipeline + date filter + hooks
│   ├── system1_research/{domain_agent.py, orchestrator.py}
│   ├── system2_events/{city_agent.py, orchestrator.py}
│   └── system3_delivery/{summarizer.py, orchestrator.py}
├── templates/weekly_report.html      — Jinja2 email
├── tests/                            — offline test suite
├── scripts/                          — gitignored try_*.py demos
├── data/                             — runtime: lab2scale.db, latest_report.html
└── main.py                           — CLI entry point
```

---

## Deployment

See **`context.md` § Test vs Live configurations** for the recommended
env-var values per environment. Container build (Day 9) wraps this whole
thing into a single image runnable on Railway or Google Cloud Run with two
cron triggers.

---

## License

Internal — Lab2Scale.
