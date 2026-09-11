Project 7 — Agentic Dashboard Intelligence

An AI-driven dashboard for **search-quality intelligence** over the Spinneys
catalog and search logs. It surfaces bad searches, discovers synonyms and
misspellings from the query log as reviewable suggestions, and answers questions
about search performance in natural language with a weekly digest.

What it does (the three deliverables)

1. Search-quality dashboard** — zero-result and low-engagement queries, and the
   terms driving them. Built from `recommendations_searches` and
   `recommendations_querycount`.
2. Synonym + misspelling discovery** — mined from the query log and presented as
   Pending → Approved/Rejected suggestions for human review, never applied
   silently. Persisted so decisions survive restarts.
3. Natural-language agent + weekly digest** — answers questions about search
   performance and produces a weekly summary of what changed and what needs
   attention, enriched with catalog context (category + price for returned
   products).


