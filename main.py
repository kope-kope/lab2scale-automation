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


async def run(command: str) -> None:
    if command == "init-db":
        await init_db()
    elif command == "sweep":
        # Systems 1 & 2 — implemented in later tasks (Days 3-6).
        log.warning("'sweep' is not implemented yet")
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
