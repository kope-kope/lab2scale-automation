Extract structured data from this research finding for a Lab2Scale analyst. Record ONLY what the content actually states — never invent names, emails, or affiliations. If a field isn't present, use null.

Fields:
- title: concise, specific title (max 100 chars). Prefer the real finding over a generic headline.
- summary: 2-3 sentences — what was achieved and why it matters for commercialization (the "so what").
- researchers: array of named researchers or founders explicitly mentioned; [] if none are named.
- affiliation: the primary university, lab, or company behind the work.
- contact_info: an email address or contact link ONLY if it appears verbatim in the content.
- trl_estimate: rough Technology Readiness Level as "TRL X" or "TRL X-Y", inferred from maturity cues:
  TRL 1-3 = basic research / lab proof-of-concept; TRL 4-6 = validated prototype / pilot;
  TRL 7-9 = demonstrated at scale / commercial deployment. Use null if maturity is unclear.
- source_type: one of [preprint, journal, news, patent, lab_page, startup].

Content:
{{CONTENT}}
