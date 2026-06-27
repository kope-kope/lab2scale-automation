Extract structured data from this event listing for a Lab2Scale analyst. Record ONLY what the content supports; if a field truly isn't present, use null.

Fields:
- event_name: concise, specific event name.
- event_date: the date the EVENT takes place, as YYYY-MM-DD. This is the most important field — look carefully through the WHOLE text, including headers, sidebars, and "when/date" lines. Normalize any format ("June 24, 2026", "6/24/26", "Wed Jun 24", "24 June") to YYYY-MM-DD. If only a month and day appear, infer the year from context (an upcoming event is in the current or next year). For a date RANGE, use the START date. Do NOT use registration deadlines, abstract deadlines, or "posted on" dates. Use null only if no event date can be found anywhere.
- event_time: start–end time if given, e.g. "18:00-20:00"; null if unknown.
- venue: physical venue plus city if shown (e.g. "MIT Media Lab, Cambridge"), or the platform for online events (e.g. "Zoom / online").
- description: 2-3 sentences on what the event is and who should attend.
- cost: e.g. "Free", "$50", "TBD".
- event_type: one of [conference, seminar, meetup, workshop, demo_day, panel, summit].
- relevance_tags: array of the Lab2Scale focus areas this event clearly touches, from
  [nuclear_advanced_energy, water_cooling, power_electronics, autonomous_systems, advanced_manufacturing]; [] if none clearly apply.

Content:
{{CONTENT}}
