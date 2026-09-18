# Search Pulse — published snapshot

> **This snapshot runs on generated data, not the real catalogue.**
> It was built with `searchiq sample-data`, which plants every pattern the
> analytics layer claims to detect — zero-result queries, misspellings, partial
> queries, machine-like repeats — in known proportions, then feeds them through
> the same ETL the real dump uses. 240 products, 300 searches, 168 sessions.
> The findings quoted in the project README come from the real 828 MB Spinneys
> dump, which stays on the author's machine and is not published here.

A static copy of the Search Pulse dashboard. Every figure here was computed from
the analytics store at export time and written to `data/*.json`; the page reads
those files instead of calling an API.

## Publishing it

Drag this whole folder onto https://app.netlify.com/drop — no account needed.
Any static host works the same way (GitHub Pages, Cloudflare Pages, Vercel).

## What works, and what does not

Working: the overview, every query and its drill-down, the terms rollup, the
review queue as a read-only list, and the digest.

Not working, because each needs a running server: approving or rejecting a
proposal, asking the agent a question, re-running discovery, and changing the
reporting period. The page states this rather than showing dead controls.

Run `searchiq serve` locally for the interactive version.

## Refreshing it

    searchiq export-site --output site

Then re-upload. The snapshot does not update on its own.
