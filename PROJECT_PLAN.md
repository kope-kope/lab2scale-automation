# Lab2Scale Automation — Project Plan

## How to Use This Plan

Each day has 1-2 tasks scoped for a single Claude Code session. Each task maps to a git branch. The workflow:

1. Create a branch from `main` (branch name provided)
2. Open Claude Code in the `Automation/` directory
3. Give Claude Code the task prompt (provided below)
4. Review output, test, merge to `main`
5. Move to next task

Tasks are ordered by dependency — don't skip ahead. Each task builds on the previous day's merged work.

---

## Day 1 — Project Scaffold + Database

### Task 1.1: Initialize project and database layer

**Branch:** `feat/scaffold-and-database`

**Claude Code prompt:**
> Initialize the Lab2Scale Automation project. Read CLAUDE.md and IMPLEMENTATION_SPEC.md for full context. For this task:
>
> 1. Create the full project directory structure as specified in ARCHITECTURE.md (systems/, lib/, templates/, data/, etc.)
> 2. Create `requirements.txt` from IMPLEMENTATION_SPEC.md Section 3
> 3. Create `.env.example` from IMPLEMENTATION_SPEC.md Section 2
> 4. Create `lib/__init__.py` and all other `__init__.py` files
> 5. Implement `lib/data_store.py` with the full database schema from IMPLEMENTATION_SPEC.md Section 1
> 6. Implement `lib/dedup.py` — hash computation, seen-before checks, cleanup
> 7. Implement `main.py` with just the `init-db` command working
> 8. Write tests: `tests/test_data_store.py` and `tests/test_dedup.py` using in-memory SQLite
> 9. Run the tests and verify they pass
>
> Use async/await throughout. Use aiosqlite for SQLite. Follow the interfaces in IMPLEMENTATION_SPEC.md Section 4.3 and 4.4 exactly.

**Definition of done:**
- `python main.py init-db` creates all tables
- Unit tests pass for data_store and dedup modules
- All `__init__.py` files exist

---

## Day 2 — Scraper + LLM Wrapper

### Task 2.1: Build the scraper and LLM modules

**Branch:** `feat/scraper-and-llm`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Build the scraper and LLM modules:
>
> 1. Implement `lib/scraper.py` following the interface in IMPLEMENTATION_SPEC.md Section 4.1:
>    - Async HTTP client using httpx.AsyncClient with connection pooling
>    - `fetch_rss()` — parse RSS/Atom feeds via feedparser, return list of {title, link, summary, published}
>    - `fetch_page()` — fetch HTML, respect robots.txt (cache parsed robots.txt per domain)
>    - `fetch_api()` — fetch JSON from REST APIs
>    - `parse_html()` — extract data from HTML using CSS selectors via BeautifulSoup
>    - Rate limiting: max 2 requests/second per domain (use asyncio.Semaphore or token bucket)
>    - Retry: 3 attempts with exponential backoff using tenacity
>    - User-Agent: "Lab2Scale-Monitor/1.0"
>    - Timeout: 30 seconds
>
> 2. Implement `lib/llm.py` following IMPLEMENTATION_SPEC.md Section 4.2:
>    - `score_relevance()` — uses Claude Haiku, returns float 0-10. Use the exact scoring prompt template from the spec.
>    - `extract_structured_data()` — uses Claude Haiku with tool_use for structured output
>    - `generate_weekly_summary()` — uses Claude Sonnet
>    - Handle rate limits with retry
>    - Log token usage
>
> 3. Write tests:
>    - `tests/test_scraper.py` — test RSS parsing with a saved fixture file (create a sample RSS XML fixture). Test HTML parsing.
>    - `tests/test_llm.py` — test prompt formatting and response parsing with mocked API responses.
>
> Use the prompt templates from IMPLEMENTATION_SPEC.md Section 4.2 exactly.

**Definition of done:**
- Scraper can fetch and parse a live RSS feed (test with `https://rss.arxiv.org/rss/cond-mat.mtrl-sci`)
- LLM module formats prompts correctly and parses responses
- Tests pass

---

## Day 3 — Base Agent + First Domain Agent

### Task 3.1: Build base agent and energy storage domain agent

**Branch:** `feat/base-agent-and-first-domain`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Build the agent framework:
>
> 1. Implement `systems/base_agent.py` — abstract base class following IMPLEMENTATION_SPEC.md Section 5.1:
>    - Constructor takes config_path, scraper, llm, dedup, store
>    - Loads YAML config
>    - Abstract `run()` method
>    - Concrete `fetch_all_sources()`, `filter_and_score()`, `extract_and_store()` methods
>
> 2. Implement `systems/system1_research/domain_agent.py` — DomainAgent(BaseAgent) following Section 5.2:
>    - Reads sources from its domain YAML config
>    - Iterates through source categories (academic_labs, government_doe, arxiv, publications, news, etc.)
>    - Uses the appropriate scraper method (rss/scrape/api) based on each source's `method` field
>    - Scores each item via LLM
>    - Deduplicates via hash
>    - Saves relevant findings (score >= 6.0) to database
>    - Returns stats: {new_items, skipped, errors}
>
> 3. Test by running the energy_storage domain agent against ONLY the arXiv and journal RSS sources (skip scrape sources for now — we want to verify the pipeline works end-to-end with the easiest sources first):
>    - Load config from `config/domains/energy_storage.yaml`
>    - Filter to only `method: rss` sources
>    - Run the agent
>    - Verify findings appear in the SQLite database
>    - Print a summary of what was found
>
> Start with RSS sources only. Web scraping sources will be tested in a later task.

**Definition of done:**
- Energy storage agent runs against RSS sources
- Findings appear in the database with scores, summaries, and structured data
- No crashes on source fetch failures (graceful skip)

---

## Day 4 — System 1 Orchestrator (All 5 Domains)

### Task 4.1: Build System 1 orchestrator and test all domains

**Branch:** `feat/system1-orchestrator`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Build the System 1 orchestrator:
>
> 1. Implement `systems/system1_research/orchestrator.py` following Section 5.3:
>    - Creates a DomainAgent for each of the 5 domains
>    - Runs all 5 agents concurrently with asyncio.gather()
>    - Aggregates stats from all agents
>    - Handles individual agent failures gracefully (one agent crashing doesn't stop the others)
>
> 2. Add the `sweep` command to `main.py` — for now, just run System 1:
>    ```
>    python main.py sweep
>    ```
>
> 3. Test: Run a full System 1 sweep against RSS sources only.
>    - All 5 domain agents should run in parallel
>    - Check the database: should have findings across multiple focus areas
>    - Print a summary table: domain | sources_checked | items_found | items_saved
>
> 4. Fix any issues with the YAML config parsing — each domain's YAML has slightly different category names (e.g., `academic_labs` vs `government_doe` vs `government` vs `government_programs`). The agent should handle all category names and iterate through every source regardless of which category it's under.
>
> Only test with `method: rss` sources for now. Scraping comes later.

**Definition of done:**
- `python main.py sweep` runs all 5 domain agents in parallel
- Database has findings from multiple domains
- Summary stats printed to console
- No crashes from individual agent failures

---

## Day 5 — Web Scraping Support

### Task 5.1: Add web scraping capability to domain agents

**Branch:** `feat/web-scraping`

**Claude Code prompt:**
> The domain agents currently only handle `method: rss` sources. Extend them to handle `method: web_scrape` (called `scrape` or `web_scrape` in the YAML configs).
>
> 1. In `lib/scraper.py`, enhance `fetch_page()` and `parse_html()`:
>    - Fetch the HTML page
>    - Extract text content (strip navigation, headers, footers — focus on main content)
>    - For news/lab pages: extract article titles, links, and snippets
>    - Use a general-purpose extraction approach: find all `<a>` tags with surrounding text, or use the page's main content area
>
> 2. In `domain_agent.py`, add handling for scrape sources:
>    - Fetch the page HTML
>    - Extract article/item links and text
>    - Feed each item through the same LLM scoring pipeline
>
> 3. Test with a few specific scrape sources:
>    - An MIT news page (e.g., `https://news.mit.edu/topic/energy`)
>    - A DOE page (e.g., `https://www.energy.gov/science/fes/fusion-energy-sciences`)
>    - An ARPA-E page
>
> 4. Handle edge cases: pages that require JavaScript (log a warning and skip), pages that return 403/404, pages with unusual structures.
>
> The goal is not perfect extraction from every source — it's robust extraction from most sources with graceful failure on the rest.

**Definition of done:**
- Domain agents can process both RSS and scrape sources
- Test scrape sources produce findings in the database
- Sources that fail to scrape are logged and skipped, not crashed

---

## Day 6 — System 2 (Event Tracking)

### Task 6.1: Build city agent and System 2 orchestrator

**Branch:** `feat/system2-events`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Build System 2 (event tracking):
>
> 1. Implement `systems/system2_events/city_agent.py` following Section 5.4:
>    - Similar to DomainAgent but for events
>    - Reads city config from `config/cities/{city}.yaml`
>    - Extracts event data: name, date, time, venue, URL, description, cost, type
>    - Uses LLM to score relevance to Lab2Scale's focus areas
>    - Saves to the `events` table
>
> 2. The LLM scoring prompt for events should be different from research:
>    - Score based on: topic relevance to the 5 focus areas + whether it's a networking opportunity
>    - Extract relevance_tags (which focus areas does this event touch?)
>
> 3. Implement `systems/system2_events/orchestrator.py` following Section 5.5:
>    - Creates a CityAgent for each of the 3 cities
>    - Runs all 3 in parallel
>
> 4. Update `main.py` `sweep` command to run BOTH System 1 and System 2 in parallel.
>
> 5. Test: Run the Boston city agent against its RSS and Eventbrite sources. Verify events appear in the database.
>
> Use the events table schema from IMPLEMENTATION_SPEC.md Section 1.

**Definition of done:**
- `python main.py sweep` runs Systems 1 and 2 in parallel
- Events from at least one city appear in the database
- Events have relevance scores and tags

---

## Day 7 — Email Template + System 3

### Task 7.1: Build email template and report compiler

**Branch:** `feat/system3-delivery`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Build System 3 (report compilation and delivery):
>
> 1. Create `templates/weekly_report.html` — Jinja2 template following the layout in IMPLEMENTATION_SPEC.md Section 6:
>    - Mobile-responsive HTML email using tables and inline CSS
>    - Sections: Executive Summary, Top Research Findings (grouped by domain), Upcoming Events (grouped by city), Notable Contacts, Stats
>    - Max width 600px, system fonts, clean professional design
>    - Each finding/event is a clickable link
>    - Subtle color coding by domain and city
>
> 2. Implement `systems/system3_delivery/summarizer.py`:
>    - Takes lists of findings and events
>    - Calls Claude Sonnet to generate an executive summary (3-5 sentences)
>    - Ranks findings by relevance score
>    - Identifies notable contacts (researchers/founders mentioned in findings)
>
> 3. Implement `lib/email_sender.py` following Section 4.5:
>    - Send HTML email via Resend API
>    - Subject: "Lab2Scale Weekly Intelligence Brief — Week of {date}"
>    - From: configured sender address
>    - To: team@lab-2-scale.com
>
> 4. Implement `systems/system3_delivery/orchestrator.py` following Section 5.6:
>    - Query unreported findings and events
>    - Generate summary
>    - Render HTML template
>    - Send email
>    - Mark items as reported
>    - Log report
>
> 5. Add the `report` command to `main.py`.
>
> 6. Test: Seed the database with some test findings and events, then run `python main.py report`. Verify the email is sent and looks correct.

**Definition of done:**
- `python main.py report` compiles and sends a formatted email
- Email received at the configured address
- Items marked as reported in the database

---

## Day 8 — Integration + End-to-End Test

### Task 8.1: Full integration and end-to-end test

**Branch:** `feat/integration`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Do the full integration:
>
> 1. Implement the `full` command in `main.py`:
>    ```
>    python main.py full  # runs sweep then report
>    ```
>
> 2. Run a complete end-to-end test:
>    - `python main.py init-db` — fresh database
>    - `python main.py sweep` — run Systems 1 & 2
>    - Check database: verify findings and events exist
>    - `python main.py report` — run System 3
>    - Verify email sent, items marked as reported
>    - `python main.py sweep` — run again
>    - Verify deduplication works (no duplicate findings)
>
> 3. Add logging throughout (follow the format in IMPLEMENTATION_SPEC.md Section 13)
>
> 4. Add error handling improvements:
>    - Graceful handling of missing API keys (warn but don't crash)
>    - Source timeout handling
>    - Database connection error handling
>
> 5. Write a brief `README.md` with setup and usage instructions
>
> 6. Clean up any rough edges found during testing.

**Definition of done:**
- `python main.py full` runs the complete pipeline end-to-end
- Deduplication works across multiple runs
- Logging is clean and informative
- README exists with setup instructions

---

## Day 9 — Dockerize + Deploy

### Task 9.1: Containerize and deploy

**Branch:** `feat/deployment`

**Claude Code prompt:**
> Read CLAUDE.md and IMPLEMENTATION_SPEC.md. Containerize and prepare for deployment:
>
> 1. Create `Dockerfile` following IMPLEMENTATION_SPEC.md Section 8
>
> 2. Create `docker-compose.yml` following Section 9 (alternative deployment)
>
> 3. Create deployment configs:
>    - `railway.toml` for Railway deployment
>    - OR equivalent for Google Cloud Run
>
> 4. Create a `scripts/` directory with helper scripts:
>    - `scripts/run_sweep.sh` — runs the monitoring sweep
>    - `scripts/run_report.sh` — runs the weekly report
>    - `scripts/setup.sh` — installs deps, inits DB
>
> 5. Test the Docker build:
>    ```
>    docker build -t lab2scale-automation .
>    docker run --env-file .env lab2scale-automation python main.py init-db
>    docker run --env-file .env lab2scale-automation python main.py sweep
>    ```
>
> 6. Document the deployment process in README.md:
>    - How to deploy to Railway (cron schedule: `0 11,18,1 * * *` for 6am/1pm/8pm ET in UTC)
>    - How to set environment variables
>    - How to monitor logs

**Definition of done:**
- Docker build succeeds
- Container runs sweep and report commands
- Deployment docs in README
- Cron schedules documented

---

## Day 10 — Polish + Launch

### Task 10.1: Final polish and first production run

**Branch:** `feat/polish`

**Claude Code prompt:**
> Final polish pass on the Lab2Scale Automation System:
>
> 1. Review and improve the email template — make sure it looks professional in Gmail, Outlook, and Apple Mail
>
> 2. Calibrate LLM scoring:
>    - Run a sweep and review the top 20 findings by score
>    - Are the scores reasonable? Adjust the prompt if needed
>    - Are we catching enough findings or too many? Adjust the 6.0 threshold if needed
>
> 3. Add a `--dry-run` flag to the report command that generates the report HTML but doesn't send the email (saves to `data/latest_report.html` instead)
>
> 4. Add a `--sources` flag to the sweep command that lists all sources without fetching (for debugging)
>
> 5. Add cost tracking: log estimated Claude API costs per run based on token usage
>
> 6. Run the first production sweep and report. Review the output.

**Definition of done:**
- Email template renders well in email clients
- `--dry-run` and `--sources` flags work
- First real report generated and reviewed
- System is ready for daily automated runs

---

## Git Branching Strategy

```
main
 ├── feat/scaffold-and-database      (Day 1)
 ├── feat/scraper-and-llm            (Day 2)
 ├── feat/base-agent-and-first-domain (Day 3)
 ├── feat/system1-orchestrator       (Day 4)
 ├── feat/web-scraping               (Day 5)
 ├── feat/system2-events             (Day 6)
 ├── feat/system3-delivery           (Day 7)
 ├── feat/integration                (Day 8)
 ├── feat/deployment                 (Day 9)
 └── feat/polish                     (Day 10)
```

Each branch merges to `main` before the next day starts. No parallel branches — each day depends on the previous.

---

## After Launch — Ongoing Maintenance

- **Weekly:** Review the report quality. Are scores calibrated? Are we missing important sources?
- **Monthly:** Check for broken sources (sites that changed structure). Update YAML configs.
- **As needed:** Add new domains or cities by creating new YAML configs.
- **Cost monitoring:** Track Claude API spend. Target: under $75/month.
