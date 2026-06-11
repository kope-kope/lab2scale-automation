Extract structured data from this event listing. Return a JSON object with these fields:
- event_name: concise event name
- event_date: date in ISO format YYYY-MM-DD if known
- event_time: time range, e.g. "18:00-20:00"
- venue: location or platform (e.g. "MIT Media Lab" or "Zoom")
- description: 2-3 sentence summary of what the event is about
- cost: e.g. "Free", "$50", "TBD"
- event_type: one of [conference, seminar, meetup, workshop, demo_day, panel, summit]
- relevance_tags: array of Lab2Scale focus areas this event touches, from
  [power_generation, energy_storage, power_electronics, semiconductors, deep_tech_infra]

If a field is not found in the content, use null.

Content: {{CONTENT}}
