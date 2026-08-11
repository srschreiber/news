You are filling in a historical gap: our RSS feeds missed real news for this
topic on a specific past date. The input JSON has `topic`, `date` (the day to
cover), `scoring_context` (how to judge importance for this topic),
`search_results` (Google News hits: title, url, snippet), and `pages`
(fetched article text: url, content).

Find genuinely newsworthy, SPECIFIC stories that were published on or very
close to `date` — real events, findings, or announcements, not evergreen
content and not stories that actually broke on a different day. Be skeptical:
a thin or ambiguously-dated result should be left out entirely rather than
stretched into a story. Return as many distinct stories as you can confidently
support with the given sources — this may be zero, one, or several.

Return JSON only, matching the schema: `{"events": [...]}`.

## Each event

- **`title`** — a short factual headline (not clickbait).
- **`summary`** — 1–2 sentences: what happened and why it matters.
- **`takeaways`** — 2–5 short bullet facts (numbers, names, dates). Empty
  array only if there's truly nothing concrete beyond the summary. `pages`
  may contain material for several DIFFERENT stories at once — keep each
  event's takeaways strictly to facts about that event's own `title`. Never
  let a number, spec, or claim from one story's source drift into another
  story's takeaways. If a source qualifies a stat (e.g. "response rate" vs.
  "success rate"), keep that qualifier rather than compressing it away.
- **`importance`** — 1–5, scored using `scoring_context`.
- **`keywords`** — 3–6 short tags for this story.
- **`sources`** — ONLY pages you actually drew facts from (from `pages`), as
  `{label, url}`. Use the real outlet name (e.g. "The Register"), never
  "Google News". Empty list if you found nothing usable in `pages`.

JSON only. No narration.
