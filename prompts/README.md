# Prompts

Every prompt the system sends to Claude lives here as a `.md` file. **Edit
these to change how the LLM behaves — no code changes needed.** Changes take
effect on the next run (each cron run is a fresh process).

## The files

| File | Used by | What it controls |
|---|---|---|
| `research_scoring.md` | `score_relevance` (Haiku) | How research papers/news are scored 0–10 for a focus area. |
| `research_extraction.md` | `extract_structured_data` (Haiku) | What fields to pull from a research finding. |
| `event_scoring.md` | `score_event_relevance` (Haiku) | How events are scored 0–10 for our focus areas. |
| `event_extraction.md` | `extract_event_data` (Haiku) | What fields to pull from an event listing. |
| `summary_system.md` | `generate_weekly_summary` (Sonnet) | The analyst persona/voice for the weekly brief. |
| `summary_user.md` | `generate_weekly_summary` (Sonnet) | The brief's framing + formatting instructions. |

## Placeholders

Some prompts contain `{{UPPERCASE}}` markers — the code fills these in at
runtime. **Keep them as-is** (don't delete or rename them) or the model won't
see the data:

| Marker | Filled with |
|---|---|
| `{{CONTENT}}` | The article/event text being scored or extracted. |
| `{{FOCUS_AREA}}` | The research focus area (e.g. `energy_storage`). |
| `{{FINDINGS_COUNT}}`, `{{FINDINGS_LIST}}` | The week's research findings (summary prompt). |
| `{{EVENTS_COUNT}}`, `{{EVENTS_LIST}}` | The week's upcoming events (summary prompt). |

Everything **else** is literal — write JSON examples like `{"score": 8.5}`,
dollar amounts like `$50`, and any other braces normally. No escaping needed.

## Tips

- The scoring prompts must keep asking for a JSON object with a numeric
  `score` — the code reads `score` back out. You can freely reword the
  criteria, add focus areas, or change the tone.
- The extraction prompts pair with a tool schema in `lib/llm.py` (the list of
  fields and allowed values). If you add a brand-new field, it also needs to be
  added there to be captured — ping an engineer for that part.
- To preview a prompt change end-to-end without sending email:
  `python main.py report --dry-run` (after a sweep).
