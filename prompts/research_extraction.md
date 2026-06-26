Extract structured data from this item for a Lab2Scale deal-flow scout. We are logging a potential Incubator client, so the COMPANY and the people to reach out to matter most. Record ONLY what the content states; never invent names, emails, or affiliations. If a field isn't present, use null.

Fields:
- title: the company/team and what they're building, concise (max 100 chars). Prefer "Company — one-line of what they do."
- summary: 2-3 sentences — what they're building, their stage, and why they're a fit to incubate (the commercial path we could help build).
- researchers: array of named founders or researchers — these are the people to reach out to. Capture every named person; [] if none are named.
- affiliation: the company name, or the university / lab it spun out of.
- contact_info: an email, LinkedIn, or company / lab page ONLY if it appears verbatim in the content.
- trl_estimate: rough stage as "TRL X" or "TRL X-Y" (1-3 = lab proof-of-concept; 4-6 = prototype/pilot; 7-9 = deployment). Use null if unclear.
- source_type: one of [preprint, journal, news, patent, lab_page, startup].

Content:
{{CONTENT}}
