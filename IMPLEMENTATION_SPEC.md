# Implementation Specification

This document contains everything needed to build the Lab2Scale Automation System. Read ARCHITECTURE.md first for the high-level design, then use this spec to build each component.

---

## 1. Database Schema

Use SQLite for local dev, PostgreSQL (Supabase free tier) for production. The schema is identical for both — use SQLAlchemy or raw SQL with dialect-agnostic types.

### Tables

```sql
-- Findings from System 1 (research monitoring)
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,                    -- SHA-256 hash of (source_url + title)
    system TEXT NOT NULL DEFAULT 'research',
    focus_area TEXT NOT NULL,               -- power_generation | energy_storage | power_electronics | semiconductors | deep_tech_infra
    agent TEXT NOT NULL,                    -- e.g. "power_generation_agent"
    title TEXT NOT NULL,
    summary TEXT,                           -- LLM-generated 2-3 sentence summary
    relevance_score REAL,                   -- 0-10, from LLM scoring
    researchers TEXT,                       -- JSON array of names
    affiliation TEXT,
    contact_info TEXT,
    source_url TEXT NOT NULL,
    source_type TEXT,                       -- preprint | journal | news | patent | lab_page | startup
    trl_estimate TEXT,                      -- e.g. "TRL 3-4"
    raw_content TEXT,                       -- original scraped text (for dedup and re-scoring)
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported BOOLEAN NOT NULL DEFAULT FALSE,
    report_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Events from System 2 (event tracking)
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,                    -- SHA-256 hash of (event_name + date + venue)
    system TEXT NOT NULL DEFAULT 'events',
    city TEXT NOT NULL,                     -- boston | nyc | sf
    agent TEXT NOT NULL,                    -- e.g. "boston_events_agent"
    event_name TEXT NOT NULL,
    event_date DATE,
    event_time TEXT,                        -- e.g. "18:00-20:00"
    venue TEXT,
    url TEXT NOT NULL,
    description TEXT,
    cost TEXT,                              -- "Free" | "$50" | "TBD"
    event_type TEXT,                        -- conference | seminar | meetup | workshop | demo_day
    relevance_tags TEXT,                    -- JSON array of focus area tags
    relevance_score REAL,                   -- 0-10
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported BOOLEAN NOT NULL DEFAULT FALSE,
    report_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Content hashes for deduplication
CREATE TABLE IF NOT EXISTS seen_hashes (
    hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,                   -- source name from YAML config
    first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Report log
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    findings_count INTEGER,
    events_count INTEGER,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',    -- sent | failed
    error_message TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_findings_reported ON findings(reported);
CREATE INDEX IF NOT EXISTS idx_findings_focus_area ON findings(focus_area);
CREATE INDEX IF NOT EXISTS idx_findings_score ON findings(relevance_score);
CREATE INDEX IF NOT EXISTS idx_events_reported ON events(reported);
CREATE INDEX IF NOT EXISTS idx_events_city ON events(city);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
```

---

## 2. Environment Variables

Create a `.env.example` file with these:

```bash
# Claude API
ANTHROPIC_API_KEY=sk-ant-...

# Email (Resend)
RESEND_API_KEY=re_...
REPORT_RECIPIENT=team@lab-2-scale.com
REPORT_FROM=reports@lab-2-scale.com

# Database
DATABASE_URL=sqlite:///data/lab2scale.db
# For production: postgresql://user:pass@host:5432/lab2scale

# Optional APIs
EVENTBRITE_API_KEY=           # For event tracking
MEETUP_API_KEY=               # For Meetup events
CRUNCHBASE_API_KEY=           # For startup tracking (paid)

# Scheduling
RUN_SCHEDULE=3x_daily         # 3x_daily | daily | weekly
REPORT_DAY=monday             # Day of week for weekly report
REPORT_TIME=09:00             # ET

# Logging
LOG_LEVEL=INFO
```

---

## 3. Python Package Requirements

```
# requirements.txt
httpx>=0.27.0                 # Async HTTP client
beautifulsoup4>=4.12.0        # HTML parsing
feedparser>=6.0.0             # RSS feed parsing
anthropic>=0.40.0             # Claude API
resend>=2.0.0                 # Email sending
pyyaml>=6.0                   # YAML config parsing
python-dotenv>=1.0.0          # Environment variable loading
jinja2>=3.1.0                 # HTML email templating
sqlalchemy>=2.0.0             # Database ORM (optional, can use raw SQL)
aiosqlite>=0.20.0             # Async SQLite support
apscheduler>=3.10.0           # Job scheduling (if running as daemon)
tenacity>=8.2.0               # Retry logic for flaky scrapes
lxml>=5.0.0                   # Faster HTML/XML parsing
```

---

## 4. Module Specifications

### 4.1 `lib/scraper.py` — Shared Scraping Utilities

```python
# Interface specification (not implementation)

class Scraper:
    """Async HTTP client with retry logic, rate limiting, and robots.txt respect."""

    async def fetch_rss(self, url: str) -> list[dict]:
        """Fetch and parse an RSS feed. Returns list of {title, link, summary, published}."""

    async def fetch_page(self, url: str) -> str:
        """Fetch a web page. Returns HTML string. Respects robots.txt."""

    async def fetch_api(self, url: str, params: dict = None, headers: dict = None) -> dict:
        """Fetch from a REST API. Returns parsed JSON."""

    def parse_html(self, html: str, selectors: dict) -> list[dict]:
        """Extract structured data from HTML using CSS selectors."""

# Key behaviors:
# - Use httpx.AsyncClient with connection pooling
# - Retry up to 3 times with exponential backoff (tenacity)
# - Respect robots.txt (cache parsed robots.txt per domain)
# - Use If-Modified-Since / ETag headers where supported
# - Rate limit: max 2 requests/second per domain
# - User-Agent: "Lab2Scale-Monitor/1.0 (+https://lab-2-scale.com)"
# - Timeout: 30 seconds per request
```

### 4.2 `lib/llm.py` — Claude API Wrapper

```python
# Interface specification

class LLMFilter:
    """Uses Claude API for relevance scoring and data extraction."""

    async def score_relevance(self, content: str, focus_area: str) -> float:
        """Score content 0-10 for relevance to a focus area.
        Uses Claude Haiku for cost efficiency.
        Returns float score. Threshold for inclusion: 6.0"""

    async def extract_structured_data(self, content: str, focus_area: str) -> dict:
        """Extract structured finding data from raw content.
        Uses Claude Haiku.
        Returns: {title, summary, researchers, affiliation, contact_info, trl_estimate}"""

    async def generate_weekly_summary(self, findings: list[dict], events: list[dict]) -> str:
        """Generate executive summary for the weekly report.
        Uses Claude Sonnet for quality.
        Returns: markdown string with sections."""

# Key behaviors:
# - Batch scoring calls where possible (send multiple items in one prompt)
# - Use structured output (tool_use) for extraction
# - Cache prompt templates as constants
# - Handle rate limits with retry
# - Log token usage for cost tracking
```

**Scoring Prompt Template (for Haiku):**
```
You are a research analyst for Lab2Scale, a deep tech commercialization firm.
Score the following content for relevance to {focus_area} on a scale of 0-10.

Scoring criteria:
- 9-10: Breakthrough discovery, new prototype, major funding for commercialization-ready tech
- 7-8: Significant research advance, new startup, notable partnership
- 5-6: Incremental progress, interesting but not actionable
- 3-4: Tangentially related, low novelty
- 0-2: Not relevant to {focus_area}

Content: {content}

Return ONLY a JSON object: {"score": <float>, "reason": "<one sentence>"}
```

**Extraction Prompt Template (for Haiku):**
```
Extract structured data from this research finding. Return a JSON object with these fields:
- title: concise title (max 100 chars)
- summary: 2-3 sentence summary of the finding and why it matters
- researchers: array of researcher/founder names mentioned
- affiliation: university, lab, or company name
- contact_info: any email addresses or contact links found
- trl_estimate: estimated Technology Readiness Level (e.g. "TRL 2-3")
- source_type: one of [preprint, journal, news, patent, lab_page, startup]

If a field is not found in the content, use null.

Content: {content}
```

### 4.3 `lib/dedup.py` — Deduplication

```python
class Deduplicator:
    """Content-hash based deduplication."""

    def compute_hash(self, url: str, title: str) -> str:
        """SHA-256 hash of normalized (url + title). This is the finding/event ID."""

    async def is_seen(self, hash: str) -> bool:
        """Check if this hash exists in seen_hashes table."""

    async def mark_seen(self, hash: str, source: str) -> None:
        """Add hash to seen_hashes table."""

    async def cleanup(self, days: int = 90) -> int:
        """Remove hashes older than N days. Returns count removed."""
```

### 4.4 `lib/data_store.py` — Database Operations

```python
class DataStore:
    """Read/write operations for findings and events tables."""

    async def save_finding(self, finding: dict) -> bool:
        """Insert a finding. Returns False if duplicate (hash collision)."""

    async def save_event(self, event: dict) -> bool:
        """Insert an event. Returns False if duplicate."""

    async def get_unreported_findings(self) -> list[dict]:
        """Get all findings where reported=False, ordered by relevance_score DESC."""

    async def get_unreported_events(self) -> list[dict]:
        """Get all events where reported=False, ordered by event_date ASC."""

    async def mark_reported(self, ids: list[str], table: str) -> None:
        """Set reported=True and report_date=now() for given IDs."""

    async def log_report(self, findings_count: int, events_count: int, recipient: str, status: str) -> None:
        """Insert a record into the reports table."""
```

### 4.5 `lib/email_sender.py` — Email Delivery

```python
class EmailSender:
    """Send HTML emails via Resend API."""

    async def send_report(self, html: str, subject: str, to: str) -> dict:
        """Send the weekly report email. Returns Resend API response."""

# Subject line format: "Lab2Scale Weekly Intelligence Brief — Week of {date}"
# From: reports@lab-2-scale.com (or whatever is configured)
# Reply-To: team@lab-2-scale.com
```

---

## 5. Agent Specifications

### 5.1 `systems/base_agent.py`

```python
from abc import ABC, abstractmethod

class BaseAgent(ABC):
    """Abstract base class for all sub-agents."""

    def __init__(self, config_path: str, scraper: Scraper, llm: LLMFilter, dedup: Deduplicator, store: DataStore):
        self.config = load_yaml(config_path)
        self.scraper = scraper
        self.llm = llm
        self.dedup = dedup
        self.store = store

    @abstractmethod
    async def run(self) -> dict:
        """Execute the agent's pipeline. Returns {new_items: int, skipped: int, errors: int}."""

    async def fetch_all_sources(self) -> list[dict]:
        """Fetch from all sources in config. Returns raw items."""

    async def filter_and_score(self, items: list[dict]) -> list[dict]:
        """Run LLM scoring. Returns items with score >= 6.0."""

    async def extract_and_store(self, items: list[dict]) -> int:
        """Extract structured data and save to DB. Returns count saved."""
```

### 5.2 `systems/system1_research/domain_agent.py`

```python
class DomainAgent(BaseAgent):
    """Configurable research monitoring agent for a specific focus area."""

    # Config comes from config/domains/{focus_area}.yaml
    # Each source in the YAML has: name, url, method (rss/scrape/api), and optional fields

    async def run(self) -> dict:
        # 1. Iterate through all source categories in config
        # 2. For each source, use the appropriate fetch method
        # 3. Score each item for relevance to this domain's focus area
        # 4. Dedup against seen_hashes
        # 5. Extract structured data from items scoring >= 6.0
        # 6. Save to findings table
        # 7. Return stats
```

### 5.3 `systems/system1_research/orchestrator.py`

```python
class ResearchOrchestrator:
    """Spins up all domain agents in parallel."""

    DOMAINS = [
        "power_generation",
        "energy_storage",
        "power_electronics",
        "semiconductors",
        "deep_tech_infra",
    ]

    async def run(self) -> dict:
        # 1. Create a DomainAgent for each domain
        # 2. Run all 5 agents concurrently with asyncio.gather()
        # 3. Aggregate and return stats
```

### 5.4 `systems/system2_events/city_agent.py`

```python
class CityAgent(BaseAgent):
    """Configurable event tracking agent for a specific city."""

    # Config comes from config/cities/{city}.yaml

    async def run(self) -> dict:
        # 1. Iterate through all source categories
        # 2. Fetch events from each source
        # 3. Score relevance to Lab2Scale focus areas
        # 4. Dedup
        # 5. Extract structured event data
        # 6. Save to events table
        # 7. Return stats
```

### 5.5 `systems/system2_events/orchestrator.py`

```python
class EventsOrchestrator:
    """Spins up all city agents in parallel."""

    CITIES = ["boston", "nyc", "sf"]

    async def run(self) -> dict:
        # Same pattern as ResearchOrchestrator
```

### 5.6 `systems/system3_delivery/orchestrator.py`

```python
class DeliveryOrchestrator:
    """Compiles weekly report and sends email."""

    async def run(self) -> dict:
        # 1. Query unreported findings (ordered by score)
        # 2. Query unreported events (ordered by date)
        # 3. If no new items, send a brief "nothing new" email or skip
        # 4. Generate executive summary via LLM (Claude Sonnet)
        # 5. Render HTML email from Jinja2 template
        # 6. Send via Resend
        # 7. Mark all included items as reported
        # 8. Log report to reports table
        # 9. Return stats
```

---

## 6. Email Template Specification

The weekly report email (`templates/weekly_report.html`) should be a Jinja2 template with these sections:

```
┌──────────────────────────────────────────────┐
│  Lab2Scale Weekly Intelligence Brief         │
│  Week of May 25, 2026                        │
├──────────────────────────────────────────────┤
│                                              │
│  EXECUTIVE SUMMARY                           │
│  {{ executive_summary }}                     │
│  (3-5 sentences, LLM-generated)              │
│                                              │
├──────────────────────────────────────────────┤
│                                              │
│  TOP RESEARCH FINDINGS                       │
│                                              │
│  ⚡ Power Generation ({{ count }})            │
│  • Finding title — summary. Score: 8.5       │
│    Researchers: Dr. X, Prof. Y | Lab: MIT    │
│    → Link                                    │
│                                              │
│  🔋 Energy Storage ({{ count }})              │
│  • ...                                       │
│                                              │
│  (repeat for all 5 domains)                  │
│                                              │
├──────────────────────────────────────────────┤
│                                              │
│  UPCOMING EVENTS                             │
│                                              │
│  📍 Boston ({{ count }})                      │
│  • Event name — June 15, MIT Media Lab       │
│    Description snippet. Free.                │
│    → Link                                    │
│                                              │
│  📍 New York City ({{ count }})               │
│  • ...                                       │
│                                              │
│  📍 San Francisco ({{ count }})               │
│  • ...                                       │
│                                              │
├──────────────────────────────────────────────┤
│                                              │
│  NOTABLE CONTACTS                            │
│  (researchers/founders worth reaching out to)│
│  • Name — Affiliation — Context — Contact    │
│                                              │
├──────────────────────────────────────────────┤
│                                              │
│  Stats: {{ findings_count }} findings from   │
│  {{ sweep_count }} sweeps | {{ events_count}}│
│  events tracked                              │
│                                              │
│  Lab2Scale Automation System v1.0            │
└──────────────────────────────────────────────┘
```

**Design requirements:**
- Mobile-responsive HTML email (use tables for layout, inline CSS)
- Clean, professional appearance — no heavy graphics
- Each finding/event is a clickable link
- Color coding by domain/city (subtle, not garish)
- Max width: 600px
- Font: system fonts (Arial, Helvetica, sans-serif)

---

## 7. `main.py` — Top-Level Orchestrator

```python
"""
Lab2Scale Automation System — Main Orchestrator

Usage:
    python main.py sweep          # Run Systems 1 & 2 (monitoring sweep)
    python main.py report         # Run System 3 (compile & send report)
    python main.py full           # Run all three systems
    python main.py init-db        # Initialize database tables
"""

import asyncio
import argparse
from systems.system1_research.orchestrator import ResearchOrchestrator
from systems.system2_events.orchestrator import EventsOrchestrator
from systems.system3_delivery.orchestrator import DeliveryOrchestrator

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sweep", "report", "full", "init-db"])
    args = parser.parse_args()

    if args.command == "init-db":
        # Create all tables
        ...
    elif args.command == "sweep":
        # Run Systems 1 & 2 in parallel
        await asyncio.gather(
            ResearchOrchestrator().run(),
            EventsOrchestrator().run(),
        )
    elif args.command == "report":
        # Run System 3 only
        await DeliveryOrchestrator().run()
    elif args.command == "full":
        # Run all three in sequence (sweep then report)
        await asyncio.gather(
            ResearchOrchestrator().run(),
            EventsOrchestrator().run(),
        )
        await DeliveryOrchestrator().run()
```

---

## 8. Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Initialize database
RUN python main.py init-db

# Default: run a full sweep
CMD ["python", "main.py", "sweep"]
```

---

## 9. Deployment Configuration

### Railway (recommended)

```toml
# railway.toml
[build]
builder = "DOCKERFILE"

[deploy]
startCommand = "python main.py sweep"

# Cron jobs are configured in Railway dashboard:
# Sweep: 0 6,13,20 * * *     (6am, 1pm, 8pm ET — adjust for UTC)
# Report: 0 9 * * 1           (9am ET Monday)
```

### Alternative: Docker Compose (for VPS)

```yaml
# docker-compose.yml
version: '3.8'
services:
  sweep:
    build: .
    command: python main.py sweep
    env_file: .env
    volumes:
      - ./data:/app/data
    # Use system cron to trigger: docker compose run sweep

  report:
    build: .
    command: python main.py report
    env_file: .env
    volumes:
      - ./data:/app/data
    # Use system cron to trigger: docker compose run report
```

---

## 10. Build Order

Follow this exact order. Each step should be testable independently before moving to the next.

### Phase 1: Foundation
1. **`lib/data_store.py`** + database schema — create tables, test CRUD
2. **`lib/dedup.py`** — hash computation, seen-before checks
3. **`lib/scraper.py`** — HTTP client with retry, RSS parsing, HTML parsing
4. **`lib/llm.py`** — Claude API wrapper with scoring and extraction prompts

### Phase 2: System 1
5. **`systems/base_agent.py`** — abstract base class
6. **`systems/system1_research/domain_agent.py`** — single domain agent
7. **Test**: Run ONE domain agent (e.g., energy_storage) against 2-3 RSS sources. Verify findings land in DB.
8. **`systems/system1_research/orchestrator.py`** — parallel execution of all 5
9. **Test**: Run full System 1. Check DB has findings across all 5 domains.

### Phase 3: System 2
10. **`systems/system2_events/city_agent.py`** — single city agent
11. **Test**: Run ONE city agent (e.g., boston) against 2-3 sources. Verify events land in DB.
12. **`systems/system2_events/orchestrator.py`** — parallel execution of all 3
13. **Test**: Run full System 2. Check DB has events across all 3 cities.

### Phase 4: System 3
14. **`templates/weekly_report.html`** — Jinja2 email template
15. **`systems/system3_delivery/summarizer.py`** — LLM summary generation
16. **`lib/email_sender.py`** — Resend integration
17. **`systems/system3_delivery/orchestrator.py`** — full pipeline
18. **Test**: Run System 3 with test data. Verify email is received.

### Phase 5: Integration
19. **`main.py`** — CLI orchestrator
20. **End-to-end test**: `python main.py full` — sweep + report
21. **Dockerfile** + deployment config
22. **Deploy** to Railway/Cloud Run

---

## 11. Testing Strategy

### Unit Tests
- `test_dedup.py`: Hash computation, collision handling
- `test_scraper.py`: RSS parsing, HTML extraction (use saved fixtures)
- `test_llm.py`: Prompt formatting, response parsing (mock API)
- `test_data_store.py`: CRUD operations on in-memory SQLite

### Integration Tests
- `test_domain_agent.py`: Run against 1-2 live RSS feeds, check DB
- `test_city_agent.py`: Run against 1-2 live event sources, check DB
- `test_report.py`: Generate report from seeded DB, check HTML output

### Manual Tests
- Review email rendering in multiple email clients
- Verify dedup across multiple runs (no duplicates)
- Check LLM scoring calibration (are scores reasonable?)

---

## 12. Error Handling

- **Source fetch failures**: Log and skip. Never let one broken source stop the entire agent.
- **LLM API errors**: Retry 3x with exponential backoff. If still failing, skip scoring and save raw item with score=0.
- **Email send failure**: Retry 3x. If still failing, log error and save report HTML to disk for manual review.
- **Database errors**: Fatal. Stop execution and alert.
- **Rate limiting**: Honor Retry-After headers. Back off per-domain.

---

## 13. Logging

Use Python `logging` module. Log format:
```
[2026-05-28 06:05:23] [INFO] [system1.energy_storage] Fetched 45 items from Nature Energy RSS
[2026-05-28 06:05:25] [INFO] [system1.energy_storage] Scored 45 items: 12 relevant (>= 6.0), 33 filtered
[2026-05-28 06:05:26] [INFO] [system1.energy_storage] Saved 8 new findings (4 duplicates skipped)
[2026-05-28 06:05:26] [ERROR] [system1.power_generation] Failed to fetch https://arpa-e.energy.gov/... (timeout)
```

Each agent should log:
- Sources fetched (count, time)
- Items scored (relevant vs filtered)
- Items saved (new vs duplicate)
- Errors (with source URL and error type)
