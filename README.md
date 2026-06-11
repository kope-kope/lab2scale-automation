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
| `REPORT_RECIPIENT` | Where the brief goes (primary `to`) | `team@lab-2-scale.com` |
| `REPORT_CC` | Extra CC recipients, comma/semicolon list — change anytime, no redeploy | _(none)_ |
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

### Email render verification across clients

Before a real send, preview the rendered email with representative data:

```bash
# Seeds the DB with diverse mock findings + events, then dry-runs the report.
python scripts/try_seed.py
open data/latest_report.html
```

To verify rendering across actual mail clients:

1. Open `data/latest_report.html` in Chrome / Safari / Firefox
2. Save the page or copy the HTML and email it to test inboxes (Gmail, Outlook, Apple Mail)
3. For a comprehensive check, paste the HTML into [Litmus](https://litmus.com) or [Email on Acid](https://www.emailonacid.com)

The template uses table-based layout with inline CSS and stays under 600px,
which is the broadly-compatible pattern.

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
│   ├── prompts.py      — loads editable LLM prompts from prompts/*.md
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

## Deployment — Railway (ephemeral model)

Auto-deploy on push to main: connect this repo to Railway once, and every
merge to `main` triggers a rebuild and redeploy.

**The deployment is intentionally ephemeral.** Each weekly cron run is a
fresh container: init-db creates a clean SQLite, the sweep finds whatever
is new in the past week, the report goes out, the container exits and the
DB is discarded. The next week starts from scratch — no volume, no
Postgres, no cross-week state.

Why this works:
- The rolling 7-day date filter in `BaseAgent` already drops anything
  older than a week, so `seen_hashes` adds nothing meaningful across runs.
- Lab2Scale's brief is "what's new this week" — by definition fresh.
- One container, one run, one email. Simplest possible operational shape.

### One-time setup (3 minutes)

1. **Create a Railway project** at https://railway.app → New Project → Deploy from GitHub repo → pick `lab2scale-automation`. Railway reads `Dockerfile` + `railway.toml` and builds.

2. **Set environment variables** in the service's Variables tab (paste these in; see `context.md` for the full reference):
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   RESEND_API_KEY=re_...
   REPORT_RECIPIENT=team@lab-2-scale.com
   REPORT_FROM=reports@lab-2-scale.com
   SWEEP_METHODS=rss
   LOG_LEVEL=INFO
   ```

3. **Verify the cron schedule.** `railway.toml` declares one weekly cron — Monday 14:00 UTC (9am ET) running `python main.py full`. That's both the sweep and the report in one shot. If you want to adjust, the schedule lives in `railway.toml` → `[deploy] cronSchedule`.

4. **Confirm auto-deploy.** In the service settings → Source, make sure the branch is set to `main`. Railway shows recent deploys in the dashboard — push something small to `main` and watch a rebuild kick off.

### Verifying the first run

- The first Monday after setup, Railway will trigger the cron at 14:00 UTC.
- Logs live in the Railway dashboard under the service's Deployments tab.
- The email lands in `REPORT_RECIPIENT`'s inbox.
- The SQLite DB persists on the volume; subsequent runs reuse it.

### Future: 3×-daily sweeps (not currently planned)

The original architecture imagined 3×-daily sweeps accumulating into a
single weekly report. That would require persistent shared state across
runs (Postgres + a shared DB), which adds operational complexity. The
ephemeral weekly model above gets you ~90% of the value at ~10% of the
complexity. If you ever want to bring it back: add a Railway Postgres
plugin, swap `aiosqlite` for an `asyncpg` driver in `lib/data_store.py`,
add a second cron service for `sweep`, leave the existing service to run
`report` weekly.

### Cron timezone reference

```
Sweep (3×/day):    0 11,18,1 * * *   UTC   = 6am, 1pm, 8pm ET
Report (weekly):   0 14 * * 1         UTC   = 9am ET Monday
Full (weekly):     0 14 * * 1         UTC   = 9am ET Monday  ← current default
```

### Local testing of the image

```bash
docker build -t lab2scale-automation .
docker run --rm --env-file .env -v "$(pwd)/data:/app/data" \
  lab2scale-automation python main.py full --dry-run
```

### Helper scripts

`scripts/setup.sh` — one-shot local setup (venv, install, init-db).
`scripts/run_sweep.sh` — wrapper for local cron / systemd / manual.
`scripts/run_report.sh` — same, passes args through (e.g. `--dry-run`).

---

## License

Internal — Lab2Scale.
