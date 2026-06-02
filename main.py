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


async def init_db() -> None:
    path = _db_path()
    log.info("Initializing database at %s", path)
    async with DataStore(path) as store:
        await store.init_db()
    log.info("Database initialized — all tables created")


def _print_sweep_summary(result: dict) -> None:
    """Print a per-domain summary table for a System 1 sweep."""
    domains = result.get("domains", {})
    totals = result.get("totals", {})
    print("\nSystem 1 — Research sweep")
    header = f"{'domain':<20}{'sources':>9}{'fetched':>9}{'filtered':>9}{'saved':>7}{'errors':>8}"
    print(header)
    print("-" * len(header))
    for domain, s in domains.items():
        if "error" in s:
            print(f"{domain:<20}  CRASHED: {s['error']}")
            continue
        print(f"{domain:<20}{s.get('sources', 0):>9}{s.get('fetched', 0):>9}"
              f"{s.get('filtered', 0):>9}{s.get('new_items', 0):>7}{s.get('errors', 0):>8}")
    print("-" * len(header))
    print(f"{'TOTAL':<20}{totals.get('sources', 0):>9}{totals.get('fetched', 0):>9}"
          f"{totals.get('filtered', 0):>9}{totals.get('new_items', 0):>7}{totals.get('errors', 0):>8}\n")


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
    """Run System 1 (research monitoring) and persist findings."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning(
            "ANTHROPIC_API_KEY is not set — LLM scoring will fail and no findings "
            "will be saved. Set it in .env to get real results."
        )
    methods = _sweep_methods()
    log.info("Sweep methods: %s", ", ".join(sorted(methods)))
    # Imported here so `init-db` works even before System 2/3 modules exist.
    from systems.system1_research.orchestrator import ResearchOrchestrator

    result = await ResearchOrchestrator(methods=methods).run()
    _print_sweep_summary(result)


async def run(command: str) -> None:
    if command == "init-db":
        await init_db()
    elif command == "sweep":
        await sweep()
    elif command == "report":
        # System 3 — implemented in a later task (Day 7).
        log.warning("'report' is not implemented yet")
    elif command == "full":
        log.warning("'full' is not implemented yet")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lab2Scale Automation System")
    parser.add_argument("command", choices=["sweep", "report", "full", "init-db"])
    args = parser.parse_args()
    asyncio.run(run(args.command))


if __name__ == "__main__":
    main()
