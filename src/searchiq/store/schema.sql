-- searchiq analytics store.
--
-- A read-optimised copy of the slices of the Spinneys production database that
-- search quality depends on, plus the tables this system owns: the suggestion
-- review queue and generated digests.
--
-- It is deliberately separate from the source database. Analysis never touches
-- production tables, the whole store rebuilds from one command, and the source
-- is an 828 MB dump that nothing should have to re-read on every query.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ETL provenance: what was loaded, from where, when. Surfaced in the UI so a
-- reader always knows which extract a number came from.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Catalogue

CREATE TABLE IF NOT EXISTS product (
    id            INTEGER PRIMARY KEY,
    sku           TEXT,
    url_key       TEXT,
    default_price REAL
);

CREATE TABLE IF NOT EXISTS product_name (
    product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    lang       TEXT    NOT NULL CHECK (lang IN ('ar', 'en')),
    name       TEXT    NOT NULL,  -- as merchandised, for display
    norm_name  TEXT    NOT NULL,  -- text.normalize()d, for every comparison
    PRIMARY KEY (product_id, lang)
);
CREATE INDEX IF NOT EXISTS idx_product_name_lang ON product_name(lang);

CREATE TABLE IF NOT EXISTS category (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES category(id)
);

CREATE TABLE IF NOT EXISTS category_name (
    category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    lang        TEXT    NOT NULL CHECK (lang IN ('ar', 'en')),
    name        TEXT    NOT NULL,
    norm_name   TEXT    NOT NULL,
    PRIMARY KEY (category_id, lang)
);

CREATE TABLE IF NOT EXISTS product_category (
    product_id  INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_product_category_cat ON product_category(category_id);

-- The catalogue vocabulary: every term that appears in a product name, with the
-- number of products using it. This is the dictionary spelling corrections are
-- validated against — a correction is only proposed towards a term that real
-- merchandise actually uses.
CREATE TABLE IF NOT EXISTS catalog_term (
    term          TEXT    NOT NULL,
    lang          TEXT    NOT NULL CHECK (lang IN ('ar', 'en')),
    product_count INTEGER NOT NULL,
    PRIMARY KEY (term, lang)
);
CREATE INDEX IF NOT EXISTS idx_catalog_term_count ON catalog_term(product_count DESC);

-- Search log

CREATE TABLE IF NOT EXISTS search_event (
    id               INTEGER PRIMARY KEY,
    raw_query        TEXT    NOT NULL,  -- exactly what the shopper typed
    norm_query       TEXT    NOT NULL,
    script           TEXT    NOT NULL,  -- arabic | latin | digits | mixed | other
    result_count     INTEGER NOT NULL,
    results_ar       TEXT    NOT NULL,  -- returned titles, newline-separated
    results_en       TEXT    NOT NULL,
    result_signature TEXT    NOT NULL,  -- digest of the result set; detects ranking drift
    occurred_at      TEXT    NOT NULL,  -- ISO-8601
    session_id       TEXT,              -- derived at ETL from inter-search gaps

    -- Engagement columns. The source database records no click stream and no
    -- order stream, so these are NULL for every row in the shipped dataset.
    -- They exist because the metrics layer prefers them whenever they are
    -- populated, and falls back to behavioural proxies when they are not; a
    -- deployment that logs clicks gets true engagement with no schema change.
    clicked_rank     INTEGER,
    converted        INTEGER CHECK (converted IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_search_event_norm ON search_event(norm_query);
CREATE INDEX IF NOT EXISTS idx_search_event_time ON search_event(occurred_at);
CREATE INDEX IF NOT EXISTS idx_search_event_session ON search_event(session_id, occurred_at);

-- Lifetime query counters maintained by the production search service. Kept
-- because they cover traffic older than the retained event log.
CREATE TABLE IF NOT EXISTS query_count (
    query      TEXT    PRIMARY KEY,
    norm_query TEXT    NOT NULL,
    count      INTEGER NOT NULL CHECK (count >= 0)
);

-- Review queue — owned by this system, not by the source database

-- Discovered synonyms and misspellings land here as proposals. Nothing in this
-- system writes to the production search configuration; approving a suggestion
-- marks it for export, and a human still ships the exported file.
CREATE TABLE IF NOT EXISTS suggestion (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('misspelling', 'synonym', 'partial_query')),
    source_term TEXT    NOT NULL,  -- what shoppers type
    target_term TEXT    NOT NULL,  -- what it should also match
    lang        TEXT    NOT NULL,
    confidence  REAL    NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rationale   TEXT    NOT NULL,  -- one sentence, shown to the reviewer
    evidence    TEXT    NOT NULL,  -- JSON: the numbers behind the rationale
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    reviewer    TEXT,
    review_note TEXT,
    UNIQUE (kind, source_term, target_term)
);
CREATE INDEX IF NOT EXISTS idx_suggestion_status ON suggestion(status, confidence DESC);

-- Generated digests

CREATE TABLE IF NOT EXISTS digest (
    id               INTEGER PRIMARY KEY,
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    generated_at     TEXT NOT NULL,
    narrative_source TEXT NOT NULL CHECK (narrative_source IN ('model', 'deterministic')),
    body_md          TEXT NOT NULL,
    metrics          TEXT NOT NULL,  -- JSON snapshot the narrative was written from
    UNIQUE (period_start, period_end)
);
