"""Lab2Scale Automation System — Main Orchestrator

Usage:
    python main.py sweep          # Run Systems 1 & 2 (monitoring sweep)
    python main.py report         # Run System 3 (compile & send report)
    python main.py full           # Run all three systems
    python main.py init-db        # Initialize database tables
"""

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv

from lib.data_store import DataStore, db_path_from_url

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


def _db_path() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///data/lab2scale.db")
    return db_path_from_url(url)


def _check_env(command: str, dry_run: bool = False) -> None:
    """Warn about missing keys with copy specific to the command being run.

    Never exits — the LLM and email layers fail soft on their own. This is
    just so the user gets a single clear heads-up instead of cryptic
    downstream failures.
    """
    needs: list[str] = []
    if command in ("sweep", "report", "full"):
        if not os.getenv("ANTHROPIC_API_KEY"):
            needs.append(
                "ANTHROPIC_API_KEY — required for LLM scoring/extraction/summary"
            )
    if command in ("report", "full") and not dry_run:
        if not os.getenv("RESEND_API_KEY"):
            needs.append(
                "RESEND_API_KEY — required to send the brief (use --dry-run "
                "to preview without sending)"
            )
    if needs:
        log.warning(
            "Missing required env for `%s`:\n  - %s\nSet these in .env. The "
            "command will still run but produce no useful output.",
            command, "\n  - ".join(needs),
        )


async def init_db() -> None:
    path = _db_path()
    log.info("Initializing database at %s", path)
    async with DataStore(path) as store:
        await store.init_db()
    log.info("Database initialized — all tables created")


def _print_sweep_summary(result: dict) -> None:
    """Print a per-domain or per-city summary table for a sweep result."""
    system = result.get("system", "research")
    if system == "events":
        title = "System 2 — Events sweep"
        row_label = "city"
        rows = result.get("cities", {})
    else:
        title = "System 1 — Research sweep"
        row_label = "domain"
        rows = result.get("domains", {})
    totals = result.get("totals", {})
    print(f"\n{title}")
    header = (f"{row_label:<20}{'sources':>9}{'fetched':>9}{'dropped':>9}"
              f"{'filtered':>9}{'saved':>7}{'errors':>8}")
    print(header)
    print("-" * len(header))
    for name, s in rows.items():
        if "error" in s:
            print(f"{name:<20}  CRASHED: {s['error']}")
            continue
        print(f"{name:<20}{s.get('sources', 0):>9}{s.get('fetched', 0):>9}"
              f"{s.get('dropped_old', 0):>9}{s.get('filtered', 0):>9}"
              f"{s.get('new_items', 0):>7}{s.get('errors', 0):>8}")
    print("-" * len(header))
    print(f"{'TOTAL':<20}{totals.get('sources', 0):>9}{totals.get('fetched', 0):>9}"
          f"{totals.get('dropped_old', 0):>9}{totals.get('filtered', 0):>9}"
          f"{totals.get('new_items', 0):>7}{totals.get('errors', 0):>8}\n")


def _sweep_methods() -> set[str]:
    """Which source methods this sweep should fetch.

    Default is ``{"rss"}`` — cost-safe. To include web-scrape sources (which
    are large in number and can ~triple the per-sweep cost on the first run),
    set ``SWEEP_METHODS=rss,scrape`` in the environment.
    """
    raw = os.getenv("SWEEP_METHODS", "rss")
    methods = {m.strip().lower() for m in raw.split(",") if m.strip()}
    return methods or {"rss"}


async def sweep() -> None:
    """Run System 1 (research) and System 2 (events) concurrently and persist."""
    methods = _sweep_methods()
    log.info("Sweep methods: %s", ", ".join(sorted(methods)))

    # Imports deferred so `init-db` doesn't pull in the system modules.
    from systems.system1_research.orchestrator import ResearchOrchestrator
    from systems.system2_events.orchestrator import EventsOrchestrator

    # Share a single DataStore + Scraper + LLM across both systems so they
    # share dedup state and avoid two open SQLite connections to the same file.
    store = DataStore(_db_path())
    await store.connect()
    await store.init_db()
    from lib.dedup import Deduplicator
    from lib.llm import LLMFilter
    from lib.scraper import Scraper

    scraper = Scraper()
    llm = LLMFilter()
    dedup = Deduplicator(store)

    research = ResearchOrchestrator(
        scraper=scraper, llm=llm, dedup=dedup, store=store, methods=methods,
    )
    events = EventsOrchestrator(
        scraper=scraper, llm=llm, dedup=dedup, store=store, methods=methods,
    )

    try:
        research_result, events_result = await asyncio.gather(
            research.run(), events.run()
        )
    finally:
        await scraper.close()
        await store.close()

    _print_sweep_summary(research_result)
    _print_sweep_summary(events_result)
    log_fn = getattr(llm, "log_usage_summary", None)
    if callable(log_fn):
        log_fn()


async def report(dry_run: bool = False) -> None:
    """Run System 3 — compile and deliver the weekly intelligence brief."""
    from systems.system3_delivery.orchestrator import DeliveryOrchestrator
    result = await DeliveryOrchestrator().run(dry_run=dry_run)
    log.info("Delivery result: %s", result)


async def run(command: str, dry_run: bool = False) -> None:
    _check_env(command, dry_run=dry_run)
    if command == "init-db":
        await init_db()
    elif command == "sweep":
        await sweep()
    elif command == "report":
        await report(dry_run=dry_run)
    elif command == "full":
        await sweep()
        await report(dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lab2Scale Automation System")
    parser.add_argument("command", choices=["sweep", "report", "full", "init-db"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For 'report' / 'full': render HTML to data/latest_report.html "
             "instead of sending via Resend.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.command, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
