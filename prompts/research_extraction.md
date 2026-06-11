Extract structured data from this research finding. Return a JSON object with these fields:
- title: concise title (max 100 chars)
- summary: 2-3 sentence summary of the finding and why it matters
- researchers: array of researcher/founder names mentioned
- affiliation: university, lab, or company name
- contact_info: any email addresses or contact links found
- trl_estimate: estimated Technology Readiness Level (e.g. "TRL 2-3")
- source_type: one of [preprint, journal, news, patent, lab_page, startup]

If a field is not found in the content, use null.

Content: {{CONTENT}}
