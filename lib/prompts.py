"""Load and render LLM prompts from editable markdown files.

Every prompt the system sends to Claude lives in ``prompts/*.md`` so it can be
edited without touching Python. To change how the LLM scores, extracts, or
writes the brief, edit the relevant ``.md`` file — no code change, no redeploy
beyond the next run.

Placeholder convention
----------------------
Prompts use ``{{UPPERCASE}}`` markers for values the code fills in, e.g.
``{{CONTENT}}`` or ``{{FOCUS_AREA}}``. Everything else is passed through
literally — JSON examples like ``{"score": 8.5}``, dollar amounts like
``$50``, and any other braces are safe to write as-is.

Caching
-------
Prompt files are read once per process (``lru_cache``). The system runs as a
fresh container per cron run, so editing a ``.md`` file takes effect on the
next run. In a long-lived process, call ``load_prompt.cache_clear()`` to pick
up edits without a restart.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# prompts/ sits at the repo root, alongside templates/ and config/.
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Return the text of ``prompts/{name}.md`` (trailing whitespace stripped).

    Raises ``FileNotFoundError`` with a clear pointer if the file is missing.
    """
    path = PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Prompt file not found: {path}\n"
            f"LLM prompts live in {PROMPTS_DIR}/ as <name>.md files."
        ) from exc


def render_prompt(name: str, **values: object) -> str:
    """Load prompt ``name`` and fill its ``{{UPPERCASE}}`` placeholders.

    ``render_prompt("research_scoring", content=..., focus_area=...)`` replaces
    ``{{CONTENT}}`` and ``{{FOCUS_AREA}}``. Keys are uppercased to form the
    marker. Substitution is single-pass, so a value that itself contains a
    ``{{...}}`` marker is never re-substituted. Placeholders with no matching
    value are left untouched (so a typo in the .md fails loud-ish — the literal
    marker shows up in the prompt rather than crashing the run).
    """
    text = load_prompt(name)
    upper = {key.upper(): str(val) for key, val in values.items()}

    def _replace(match: re.Match) -> str:
        return upper.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, text)
