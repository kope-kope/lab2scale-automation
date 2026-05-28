# Lab2Scale Automation System — Architecture Plan

## Overview

A multi-system agentic platform that continuously monitors deep tech research, tracks industry events, and delivers a weekly intelligence report to team@lab-2-scale.com. Designed for horizontal scalability — new domains and cities are added by registering new sub-agents, not rewriting code.

---

## System Architecture

```
                         ┌──────────────────────┐
                         │   Agentic Orchestrator│
                         │   (main.py)           │
                         └──────────┬───────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
         ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
         │   SYSTEM 1   │  │   SYSTEM 2   │  │    SYSTEM 3      │
         │  Research &   │  │   Event      │  │  Summary &       │
         │  Prototype    │  │   Tracking   │  │  Delivery        │
         │  Monitoring   │  │              │  │                  │
         └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘
                │                 │                    │
    ┌───────────┼───────────┐    │               Reads from
    │     5 Domain Agents   │    │               both systems
    │  (run in parallel)    │    │                    │
    ├───────────────────────┤    ├──────────────┐     │
    │ Power Generation      │    │ 3 City Agents│     │
    │ Energy Storage        │    │ (parallel)   │     │
    │ Power Electronics     │    ├──────────────┤     │
    │ Semiconductors        │    │ Boston       │     │
    │ Deep Tech Infra       │    │ NYC          │     │
    └───────────┬───────────┘    │ San Francisco│     │
                │                └──────┬───────┘     │
                ▼                       ▼             │
    ┌───────────────────────────────────────┐         │
    │          Shared Data Store            │◄────────┘
    │  (PostgreSQL / SQLite + dedup layer)  │
    └───────────────────┬───────────────────┘
                        │
                        ▼
               ┌────────────────┐
               │  Email Delivery │
               │  (Resend API)   │
               └────────────────┘
```

---

## Design Principles

1. **Systems, not scripts.** Each system is a self-contained module with its own orchestration logic. Systems can be developed, tested, and scaled independently.

2. **Sub-agent parallelism.** Within each system, sub-agents run concurrently. System 1's five domain agents don't wait for each other. System 2's three city agents don't wait for each other.

3. **Pluggable sub-agents.** Adding a new focus area (e.g., "quantum computing") means creating one new config file and registering it — no changes to the system code. Same for adding a new city.

4. **Continuous monitoring with weekly delivery.** Systems 1 and 2 can run on a more frequent cadence (daily or even multiple times per week) to accumulate findings. System 3 fires once a week to compile and deliver. The monitoring loop is decoupled from the reporting loop.

5. **Shared data store as the contract.** Systems communicate only through the data store. No direct coupling. This means you could swap System 3 for a Slack bot or a dashboard without touching Systems 1 or 2.

---

## System 1: Research & Prototype Monitoring

### Purpose
Continuously scan academic, startup, and industry sources for breakthroughs and commercialization-ready research in Lab2Scale's focus areas.

### Sub-Agents (run in parallel)

Each sub-agent is a specialized instance of the same base agent class, configured with domain-specific sources, keywords, and scoring criteria.

| Sub-Agent                   | Focus Area                                                          | Example Sources                                                                            |
| --------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **Power Generation Agent**  | Fusion, advanced fission, next-gen solar, thermoelectrics           | arXiv (physics.plasm-ph), DOE Office of Nuclear Energy, Fusion Industry Association, IRENA |
| **Energy Storage Agent**    | Batteries (solid-state, flow, metal-air), hydrogen, thermal storage | arXiv (cond-mat.mtrl-sci), Battery Archive, DOE EERE, Electrek                             |
| **Power Electronics Agent** | GaN/SiC devices, inverters, converters, WBG semiconductors          | IEEE PELS, APEC proceedings, Power Electronics News                                        |
| **Semiconductor Agent**     | Advanced packaging, chiplets, photonics, compound semis             | SEMI, IEEE IEDM, Semiconductor Engineering, AnandTech                                      |
| **Deep Tech Infra Agent**   | Advanced manufacturing, materials science, compute infrastructure   | ARPA-E, DARPA, NSF awards, MIT Lincoln Lab, Hax accelerator                                |

### Sub-Agent Pipeline (each agent runs this)

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────┐
│ Fetch from   │────▶│ LLM Filter   │────▶│ Extract      │────▶│ Dedup &    │
│ sources      │     │ (relevance   │     │ structured   │     │ Store      │
│ (RSS/scrape/ │     │  scoring)    │     │ data         │     │            │
│  API)        │     │              │     │              │     │            │
└─────────────┘     └──────────────┘     └──────────────┘     └────────────┘
                     Claude Haiku          - Title              Hash-based
                     Score 0-10            - Summary            dedup against
                     Threshold: 6+         - Researchers        previously
                                           - Lab/Company        seen items
                                           - Contact info
                                           - Source URL
                                           - TRL estimate
                                           - Focus area tag
```

### Data Output Schema (per finding)

```json
{
  "id": "sha256-hash",
  "system": "research",
  "focus_area": "energy_storage",
  "agent": "energy_storage_agent",
  "title": "Solid-state lithium battery achieves 1000 cycle stability",
  "summary": "Researchers at MIT demonstrated...",
  "relevance_score": 8.5,
  "researchers": ["Dr. Jane Smith", "Prof. John Doe"],
  "affiliation": "MIT Department of Materials Science",
  "contact_info": "jsmith@mit.edu",
  "source_url": "https://arxiv.org/abs/...",
  "source_type": "preprint",
  "trl_estimate": "TRL 3-4",
  "discovered_at": "2026-05-28T07:15:00Z",
  "reported": false
}
```

---

## System 2: Event Tracking

### Purpose
Track relevant conferences, seminars, meetups, and networking events in three target cities.

### Sub-Agents (run in parallel)

Each city agent knows its local sources and can handle city-specific event platforms.

| Sub-Agent | City | Key Local Sources |
|---|---|---|
| **Boston Events Agent** | Boston / Cambridge | MIT Energy Initiative, Harvard SEAS, MassCEC events, Boston tech calendar, Greentown Labs |
| **NYC Events Agent** | New York City | Columbia SIPA energy events, NYU Tandon, NYC tech meetups, Luma NYC, PowerBridgeNY |
| **SF Events Agent** | San Francisco / Bay Area | Stanford PEEC, Berkeley labs, Cyclotron Road, BEIA, CalCEF, Bay Area tech calendar |

### Sub-Agent Pipeline

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────┐
│ Query event   │────▶│ LLM Filter   │────▶│ Extract      │────▶│ Dedup &    │
│ sources       │     │ (topic +     │     │ event data   │     │ Store      │
│ (Eventbrite,  │     │  location    │     │              │     │            │
│  Luma, scrape)│     │  relevance)  │     │              │     │            │
└──────────────┘     └──────────────┘     └──────────────┘     └────────────┘
                      Claude Haiku          - Event name
                      Is this relevant      - Date/time
                      to Lab2Scale's        - Venue/location
                      focus areas?          - URL
                                            - Description
                                            - Cost (free/paid)
                                            - Event type
                                            - City tag
```

### Data Output Schema (per event)

```json
{
  "id": "sha256-hash",
  "system": "events",
  "city": "boston",
  "agent": "boston_events_agent",
  "event_name": "MIT Energy Night: Next-Gen Power Electronics",
  "date": "2026-06-15",
  "time": "18:00-20:00",
  "venue": "MIT Media Lab, Cambridge MA",
  "url": "https://...",
  "description": "Panel discussion on...",
  "cost": "Free",
  "event_type": "seminar",
  "relevance_tags": ["power_electronics", "semiconductors"],
  "discovered_at": "2026-05-28T07:20:00Z",
  "reported": false
}
```

---

## System 3: Summary & Delivery

### Purpose
Compile findings from Systems 1 and 2 into a weekly intelligence report and deliver it via email.

### Pipeline

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────┐
│ Query data    │────▶│ LLM Summary  │────▶│ Format HTML  │────▶│ Send Email │
│ store for     │     │ Generation   │     │ email from   │     │ via Resend │
│ unreported    │     │ (Claude      │     │ template     │     │ to team@   │
│ findings      │     │  Sonnet)     │     │              │     │ lab-2-     │
│               │     │              │     │              │     │ scale.com  │
└──────────────┘     └──────────────┘     └──────────────┘     └────────────┘
                      - Executive summary    Sections:
                      - Highlight top 5      1. Executive Summary
                        findings             2. Top Research (by domain)
                      - Rank by novelty      3. Upcoming Events (by city)
                        and TRL              4. Notable Contacts
                      - Flag actionable      5. All Sources & Links
                        opportunities
```

### Report Sections

1. **Executive Summary** — 3-5 sentence overview of the week's most significant findings
2. **Research & Prototypes** — grouped by focus area, ranked by relevance score
3. **Upcoming Events** — grouped by city (Boston / NYC / SF), sorted by date
4. **Notable Contacts** — researchers or founders worth reaching out to, with context
5. **Source Index** — full links to all referenced materials

### Post-delivery
- Mark all included findings as `reported: true`
- Log report metadata (date sent, item count, etc.)

---

## Execution Model

### 3x Daily Monitoring + Weekly Report

Systems 1 and 2 run **three times per day**, every day. Each cycle scans all sources, deduplicates against the data store, and accumulates only net-new findings. System 3 fires **once per week** to compile everything into the report.

This means **21 monitoring cycles per week** feeding into one dense, high-quality report. Nothing slips through the cracks — a paper posted Tuesday afternoon gets caught by the evening sweep, not missed because the weekly scrape already ran.

```
DAILY SCHEDULE (every day, 7 days/week):

    06:00 AM ET — Morning Sweep
    ├── System 1: 5 domain agents run in parallel ──── ~10-15 min
    │       └── New findings → data store
    └── System 2: 3 city agents run in parallel ──── ~5-10 min
            └── New events → data store

    01:00 PM ET — Midday Sweep
    ├── System 1: 5 domain agents run in parallel
    └── System 2: 3 city agents run in parallel

    08:00 PM ET — Evening Sweep
    ├── System 1: 5 domain agents run in parallel
    └── System 2: 3 city agents run in parallel


WEEKLY SCHEDULE:

    Monday 09:00 AM ET — Weekly Report
    └── System 3 runs
            ├── Reads all unreported findings (accumulated over 7 days)
            ├── Generates executive summary via Claude Sonnet
            ├── Formats HTML email
            ├── Sends to team@lab-2-scale.com
            └── Marks all included findings as reported
```

### Why 3x Daily Works

| Concern | How It's Handled |
|---|---|
| **Duplicate findings** | Content-hash dedup — same paper/event seen twice is ignored |
| **API rate limits** | Staggered sub-agent execution with backoff; RSS feeds have no limits |
| **Cost** | Haiku filtering is ~$0.001 per item; 21 cycles ≈ $5-15/week in LLM costs |
| **Source politeness** | Respect `robots.txt`, use `If-Modified-Since` headers, cache RSS ETags |
| **Data store size** | Weekly cleanup marks reported items; archive after 90 days |

### Estimated Weekly Volume

| System | Items per sweep | Sweeps/week | Net new (after dedup) |
|---|---|---|---|
| System 1 (Research) | ~50-100 raw → ~10-20 relevant | 21 | ~30-80 unique findings |
| System 2 (Events) | ~20-40 raw → ~5-10 relevant | 21 | ~15-30 unique events |
| **Weekly report** | | | **~45-110 items total** |

This gives System 3 enough material to produce a genuinely rich report — not a thin list of 5-6 links, but a curated intelligence brief.

---

## Technology Stack

| Component     | Choice                                                 | Why                                     |
| ------------- | ------------------------------------------------------ | --------------------------------------- |
| Language      | Python 3.11+                                           | Best ecosystem for scraping, LLM, async |
| Async runtime | `asyncio` + `httpx`                                    | Parallel sub-agent execution            |
| LLM           | Claude API (Haiku for filtering, Sonnet for summaries) | Cost-effective + high quality           |
| Web scraping  | `httpx` + `beautifulsoup4` + `feedparser`              | Lightweight, reliable                   |
| Data store    | PostgreSQL (Supabase free tier) or SQLite              | Persistent, queryable                   |
| Email         | Resend API                                             | Already connected                       |
| Scheduling    | Cloud scheduler (see deployment)                       | No local dependency                     |
| Config        | YAML files per agent                                   | Easy to add/modify sources              |

---

## Scalability Path

| Scale Move | What Changes |
|---|---|
| Add focus area (e.g., quantum) | Create `config/domains/quantum.yaml`, register in System 1 |
| Add city (e.g., Austin) | Create `config/cities/austin.yaml`, register in System 2 |
| Add delivery channel (Slack) | Add new delivery adapter in System 3, no other changes |
| Increase frequency | Change cron schedule, data store handles dedup |
| Add a client | New config profile with different focus areas + email |

---

## Project Structure

```
Automation/
├── ARCHITECTURE.md              ← this file
│
├── config/
│   ├── settings.yaml            ← global settings (API keys, email, schedule)
│   ├── domains/                 ← System 1 sub-agent configs
│   │   ├── power_generation.yaml
│   │   ├── energy_storage.yaml
│   │   ├── power_electronics.yaml
│   │   ├── semiconductors.yaml
│   │   └── deep_tech_infra.yaml
│   └── cities/                  ← System 2 sub-agent configs
│       ├── boston.yaml
│       ├── nyc.yaml
│       └── sf.yaml
│
├── systems/
│   ├── __init__.py
│   ├── base_agent.py            ← abstract base class for all sub-agents
│   ├── system1_research/
│   │   ├── __init__.py
│   │   ├── orchestrator.py      ← spins up domain agents in parallel
│   │   └── domain_agent.py      ← configurable research agent class
│   ├── system2_events/
│   │   ├── __init__.py
│   │   ├── orchestrator.py      ← spins up city agents in parallel
│   │   └── city_agent.py        ← configurable event agent class
│   └── system3_delivery/
│       ├── __init__.py
│       ├── orchestrator.py      ← reads data, calls summarizer, sends email
│       ├── summarizer.py        ← LLM-powered report generation
│       └── email_sender.py      ← Resend integration
│
├── lib/
│   ├── scraper.py               ← HTTP client, RSS parser, scraping utils
│   ├── llm.py                   ← Claude API wrapper (filter + summarize)
│   ├── dedup.py                 ← content hashing, seen-before checks
│   └── data_store.py            ← DB read/write (findings + events tables)
│
├── templates/
│   └── weekly_report.html       ← HTML email template (Jinja2)
│
├── main.py                      ← top-level orchestrator
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## Cost Estimate (Monthly)

| Component | Estimate | Notes |
|---|---|---|
| Claude API (Haiku — filtering) | $20-60 | ~21 sweeps/week × ~100 items × scoring |
| Claude API (Sonnet — weekly summary) | $2-5 | 4 reports/month, ~100 items each |
| Cloud hosting (Railway/Cloud Run) | $5-10 | Cron jobs, no always-on server |
| Resend email | $0 (free tier) | Under 100 emails/month |
| **Total** | **~$27-75/mo** | Scales linearly with sources added |

---

## Open Questions for Tosin

1. **Your sources:** What specific websites, labs, or publications do you already track? I'll merge them into the domain/city YAML configs.
2. **Deployment platform:** Railway, Google Cloud Run, or AWS Lambda?
3. **Data visibility:** Also write to a Google Sheet so the team can browse findings between reports?
4. **Scoring criteria:** What makes a finding "high priority"? TRL level? Funding stage? Specific researchers or labs?

---

## Next Steps

1. **You** share your existing sources + answer open questions
2. **I** build domain and city YAML configs with merged sources
3. **I** scaffold the project: base agent, data store, LLM wrapper
4. **I** build System 1 (research monitoring) + test with a dry run
5. **I** build System 2 (event tracking) + test
6. **I** build System 3 (summary + email delivery) + test
7. **Full end-to-end test** — one complete weekly cycle
8. **Dockerize + deploy** to cloud with cron schedule
