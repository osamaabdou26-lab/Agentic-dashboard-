"""Search Pulse — Agentic Dashboard Intelligence.

A Streamlit front end over the real searchiq analytics engine (../src/searchiq):
the same catalogue-aware search-quality metrics, misspelling/synonym discovery,
and digest builder the CLI and API use — plus a Gemini-powered natural-language
agent and AI digest summary.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

# ── Make the real searchiq analytics engine importable ─────────────────────
# web/ ships its own minimal requirements.txt (streamlit, google-genai,
# python-dotenv, pandas) rather than the full package, so we reach the engine
# via sys.path instead of an editable install. Everything imported below —
# analytics, discovery, store, the tool registry, the digest builder — is
# pure stdlib + python-dotenv; none of it needs anthropic or fastapi.
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from searchiq.agent.digest import generate_digest as searchiq_generate_digest  # noqa: E402
from searchiq.agent.tools import TOOLS as SEARCHIQ_TOOLS  # noqa: E402
from searchiq.agent.tools import run_tool as searchiq_run_tool  # noqa: E402
from searchiq.analytics.metrics import compute_overview, compute_query_quality  # noqa: E402
from searchiq.analytics.terms import compute_term_drivers  # noqa: E402
from searchiq.discovery.review import decide as decide_suggestion  # noqa: E402
from searchiq.discovery.review import export_approved  # noqa: E402
from searchiq.discovery.review import list_suggestions  # noqa: E402
from searchiq.discovery.review import refresh as refresh_suggestions  # noqa: E402
from searchiq.store.db import connect as store_connect  # noqa: E402
from searchiq.store.db import read_meta  # noqa: E402

load_dotenv(APP_DIR / ".env")

# ── Trust the operating system's certificate store ───────────────────────
# Interception proxies — corporate middleboxes, and consumer antivirus like
# Avast's Web/Mail Shield — re-sign HTTPS with their own root. Windows trusts
# that root, so browsers and pip are fine, but Python's HTTPS stack verifies
# against certifi's bundle instead and every Gemini call dies with
# "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".
#
# truststore points verification at the OS store, which is where the
# intercepting root actually lives. It is best-effort: where it is missing or
# unsupported, behaviour is exactly what it was before.
try:  # pragma: no cover - depends on the machine, not on the code
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - never let a TLS tweak stop the app booting
    pass

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────
DB_PATH = PROJECT_ROOT / "data" / "searchiq.db"

# Google retires model ids from under you: gemini-2.5-flash now answers new keys
# with "no longer available to new users", naming 3.6-flash as its replacement.
# Overridable from the environment so the next retirement is a .env edit rather
# than a code change — `GEMINI_MODEL=gemini-3.7-flash` and restart.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"

# A fixed status palette, used consistently across every chart and badge in
# this app: identity/verdict colour is never re-derived per chart.
COLOR_GOOD = "#2E7D32"
COLOR_WARN = "#B8860B"
COLOR_SEVERE = "#C0392B"
COLOR_NEUTRAL = "#4472C4"
COLOR_MUTED = "#8A8F98"

VERDICT_COLOR = {
    "healthy": COLOR_GOOD,
    "relevance": COLOR_WARN,
    "spelling": COLOR_WARN,
    "retrieval gap": COLOR_SEVERE,
    "assortment gap": COLOR_MUTED,
    "partial query": COLOR_NEUTRAL,
}
KIND_LABEL = {
    "misspelling": "🔤 Misspelling",
    "synonym": "🔀 Synonym",
    "partial_query": "⌨️ Partial query",
}
STATUS_BADGE = {
    "pending": "🟡 Pending",
    "approved": "🟢 Approved",
    "rejected": "🔴 Rejected",
}

st.set_page_config(page_title="Search Pulse", page_icon="🔍", layout="wide")


# ── Connection guard ─────────────────────────────────────────────────────
# A deployed copy has no store: `data/*.db` is a build artefact and is not in
# the repository, and a hosted app has no terminal to run `searchiq etl` from.
# So when the store is missing, build one from the sample generator — the same
# path `searchiq sample-data` takes, through the same ETL the real dump uses.
# Locally this never fires, because `searchiq etl` has already run.
@st.cache_resource(show_spinner=False)
def ensure_store() -> str:
    """Return how the store got here, building a sample one if there is none."""
    if DB_PATH.exists():
        return "existing"

    from searchiq.ingest.loader import load as _load
    from searchiq.ingest.sample_data import generate_sample_dump as _generate

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    dump = DB_PATH.parent / "sample_dump.sql"
    _generate(dump)
    _load(db_path=DB_PATH, dump_path=dump, source_label="generated sample data (first run)")
    # Without this the review queue is empty and the app looks half-built.
    with store_connect(DB_PATH) as conn:
        refresh_suggestions(conn)
    return "generated"


try:
    with st.spinner("First run: building a sample analytics store…"):
        STORE_ORIGIN = ensure_store()
except Exception as exc:  # noqa: BLE001 - show the reason rather than a stack trace
    st.error("No analytics store, and building a sample one failed.")
    st.markdown(
        f"Expected the analytics database at `{DB_PATH}`.\n\n"
        f"Automatic setup failed with: `{exc}`\n\n"
        "From the project root, run one of:\n"
        "```bash\nsearchiq sample-data     # no data to hand: generate a mock dataset\n"
        "searchiq etl              # load the real mysqldump extract\n```\n"
        "then refresh this page."
    )
    st.stop()


# ── Gemini client ────────────────────────────────────────────────────────
def _secret(name: str) -> str:
    """Read a setting from the environment, then from Streamlit's secrets.

    Locally the value comes from `web/.env`. A hosted deployment has no .env
    file — Streamlit Community Cloud takes secrets through its own TOML box —
    so fall back to `st.secrets`. Reading it raises when no secrets exist at
    all, which is the normal local case, hence the guard.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets[name]).strip()
    except Exception:  # noqa: BLE001 - no secrets configured is not an error
        return ""


def _key_hint() -> str:
    """Say where the missing key belongs — which is not the same in both places.

    Telling someone looking at a deployed copy to edit `web/.env` sends them
    after a file that is not there: a host keeps its configuration in
    environment variables or a secrets store. The presence of the .env file is
    what separates a working copy from a deployment.
    """
    if (APP_DIR / ".env").is_file():
        return "Add it to `web/.env`, then restart the app."
    return (
        "This looks like a deployment, so set it where this host keeps its "
        "configuration — a service variable on Railway, Render or Fly, or the "
        "secrets box on Streamlit Community Cloud. The app picks it up on the "
        "redeploy that follows. (Running locally, it goes in `web/.env`.)"
    )


@st.cache_resource(show_spinner=False)
def get_gemini_client() -> genai.Client | None:
    api_key = _secret("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None


gemini_client = get_gemini_client()


# ── Cached reads over the analytics store ───────────────────────────────
@st.cache_data(ttl=30, show_spinner=False)
def cached_meta() -> dict[str, str]:
    with store_connect(DB_PATH, read_only=True) as conn:
        return read_meta(conn)


@st.cache_data(ttl=30, show_spinner="Crunching search-quality metrics...")
def cached_overview(since: str | None = None, until: str | None = None) -> dict[str, Any]:
    with store_connect(DB_PATH, read_only=True) as conn:
        return compute_overview(conn, since=since, until=until).to_dict()


@st.cache_data(ttl=30, show_spinner="Scoring every query...")
def cached_query_quality(since: str | None = None, until: str | None = None) -> pd.DataFrame:
    with store_connect(DB_PATH, read_only=True) as conn:
        rows = [q.to_dict() for q in compute_query_quality(conn, since=since, until=until)]
    return pd.DataFrame(rows)


@st.cache_data(ttl=30, show_spinner=False)
def cached_term_drivers(limit: int = 25) -> pd.DataFrame:
    with store_connect(DB_PATH, read_only=True) as conn:
        rows = [d.to_dict() for d in compute_term_drivers(conn, limit=limit)]
    return pd.DataFrame(rows)


@st.cache_data(ttl=30, show_spinner=False)
def cached_result_count_histogram() -> pd.DataFrame:
    with store_connect(DB_PATH, read_only=True) as conn:
        return pd.read_sql_query(
            "SELECT result_count, COUNT(*) AS n FROM search_event "
            "GROUP BY result_count ORDER BY result_count",
            conn,
        )


@st.cache_data(ttl=30, show_spinner=False)
def cached_daily_trend() -> pd.DataFrame:
    with store_connect(DB_PATH, read_only=True) as conn:
        return pd.read_sql_query(
            "SELECT date(occurred_at) AS day, COUNT(*) AS searches, "
            "SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END) AS zero_result "
            "FROM search_event GROUP BY day ORDER BY day",
            conn,
        )


@st.cache_data(ttl=15, show_spinner=False)
def cached_suggestions(status: str | None = None, kind: str | None = None) -> pd.DataFrame:
    with store_connect(DB_PATH, read_only=True) as conn:
        rows = [
            s.to_dict()
            for s in list_suggestions(conn, status=status, kind=kind, limit=500)
        ]
    return pd.DataFrame(rows)


@st.cache_data(ttl=15, show_spinner=False)
def cached_suggestion_counts() -> dict[str, int]:
    with store_connect(DB_PATH, read_only=True) as conn:
        rows = list_suggestions(conn, limit=10_000)
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for s in rows:
        counts[s.status] = counts.get(s.status, 0) + 1
    return counts


# ── Small UI helpers ─────────────────────────────────────────────────────
def kpi_row(values: dict[str, Any]) -> None:
    for col, (label, value) in zip(st.columns(len(values)), values.items()):
        col.metric(label, value)


def top_queries_chart(qdf: pd.DataFrame, n: int = 15) -> go.Figure | None:
    if qdf.empty:
        return None
    top = qdf.sort_values("searches", ascending=False).head(n).sort_values("searches")
    fig = go.Figure(
        go.Bar(
            x=top["searches"],
            y=top["display_query"],
            orientation="h",
            marker_color=COLOR_NEUTRAL,
            hovertemplate="%{y}: %{x} searches<extra></extra>",
        )
    )
    fig.update_layout(
        height=max(320, 28 * len(top)),
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title="Searches",
        yaxis_title=None,
        showlegend=False,
    )
    return fig


def result_count_chart(hist: pd.DataFrame) -> go.Figure | None:
    if hist.empty:
        return None
    fig = go.Figure(
        go.Bar(
            x=hist["result_count"],
            y=hist["n"],
            marker_color=COLOR_NEUTRAL,
            hovertemplate="%{x} results: %{y} searches<extra></extra>",
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title="Results returned",
        yaxis_title="Searches",
        showlegend=False,
    )
    return fig


def daily_trend_chart(trend: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=trend["day"],
            y=trend["searches"],
            mode="lines+markers",
            line=dict(color=COLOR_NEUTRAL, width=2),
            marker=dict(size=8),
            hovertemplate="%{x}: %{y} searches<extra></extra>",
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title="Day",
        yaxis_title="Searches",
        showlegend=False,
    )
    return fig


def term_drivers_chart(term_df: pd.DataFrame) -> go.Figure | None:
    if term_df.empty:
        return None
    ordered = term_df.sort_values("total_impact", ascending=True)
    fig = go.Figure()
    for verdict, color in VERDICT_COLOR.items():
        sub = ordered[ordered["verdict"] == verdict]
        if sub.empty:
            continue
        fig.add_trace(
            go.Bar(
                x=sub["total_impact"],
                y=sub["term"],
                orientation="h",
                name=verdict,
                marker_color=color,
                hovertemplate="%{y}: impact %{x:.3f}<extra>" + verdict + "</extra>",
            )
        )
    fig.update_layout(
        height=max(320, 30 * len(ordered)),
        margin=dict(l=10, r=10, t=20, b=10),
        legend_title_text="Verdict",
        xaxis_title="Total impact (severity × traffic share)",
        yaxis_title=None,
    )
    return fig


# ── Gemini: weekly-digest narrative ─────────────────────────────────────
_DIGEST_PROMPT = """\
You are a search-quality analyst writing the opening of a weekly digest for an \
online grocery's product and engineering team. Below is the complete, \
already-computed set of figures for the period, as JSON. Write two to four \
plain sentences that say what the period looked like and what most deserves \
attention. Use ONLY figures that appear in the JSON — never invent a number, \
never add a heading, never use bullet points, and do not restate every metric. \
If "has_baseline" is false, say the period stands alone rather than describing \
a trend.

JSON:
{payload}
"""


def gemini_digest_narrative(client: genai.Client, metrics: dict[str, Any]) -> str | None:
    try:
        payload = json.dumps(metrics, ensure_ascii=False, indent=2, default=str)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_DIGEST_PROMPT.format(payload=payload),
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=400),
        )
        text = (response.text or "").strip()
        return text or None
    except Exception:
        return None


# ── Gemini: the search-performance agent ────────────────────────────────
_AGENT_SYSTEM_PROMPT = """\
You are the search-quality analyst for Search Pulse, an Egyptian online grocery. \
Shoppers search in both Arabic and English. The search engine is \
embedding-based and always returns its five nearest products, so it almost \
never returns zero results — a real failure usually shows up as WRONG results, \
not an empty page. Never call a query healthy just because it returned five \
results; check what those results actually are.

Answer only using the tools you are given, and never invent a number — every \
figure in your answer must come from a tool result. When a query looks like a \
misspelling, use check_catalogue_coverage or get_query_detail to tell a \
retrieval gap (the catalogue stocks it, search missed it) from an assortment \
gap (nobody stocks it). Cite concrete figures. Keep answers concise.\
"""


def _build_agent_tool() -> types.Tool:
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=tool.input_schema,
            )
            for tool in SEARCHIQ_TOOLS
        ]
    )


def ask_agent(
    client: genai.Client,
    question: str,
    history: list[types.Content],
    *,
    max_iterations: int = 6,
) -> tuple[str, list[dict[str, Any]], list[types.Content]]:
    """Run one turn of the tool-calling loop, reading through the real searchiq tools."""
    config = types.GenerateContentConfig(
        system_instruction=_AGENT_SYSTEM_PROMPT,
        tools=[_build_agent_tool()],
    )
    contents = list(history)
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
    trace: list[dict[str, Any]] = []

    with store_connect(DB_PATH, read_only=True) as conn:
        for _ in range(max_iterations):
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
            candidate = response.candidates[0]
            contents.append(candidate.content)

            calls = [p.function_call for p in candidate.content.parts if p.function_call]
            if not calls:
                text = "".join(p.text or "" for p in candidate.content.parts)
                return text or "(no answer)", trace, contents

            response_parts = []
            for fc in calls:
                args = dict(fc.args or {})
                result = searchiq_run_tool(conn, fc.name, args)
                trace.append(
                    {
                        "name": fc.name,
                        "args": args,
                        "result_preview": json.dumps(result, ensure_ascii=False, default=str)[:500],
                    }
                )
                response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=fc.id, name=fc.name, response={"result": result}
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=response_parts))

    return (
        "I couldn't settle on an answer within the tool-call budget — try a narrower question.",
        trace,
        contents,
    )


# ── Sidebar ──────────────────────────────────────────────────────────────
meta = cached_meta()
RESULT_CAP = int(meta.get("result_cap", 5))

st.sidebar.title("🔍 Search Pulse")
st.sidebar.caption("Agentic Dashboard Intelligence")
if meta.get("loaded_at"):
    st.sidebar.caption(f"Data loaded {meta['loaded_at'][:19].replace('T', ' ')}")
if STORE_ORIGIN == "generated":
    # A deployed copy seeds itself. Say so plainly: these numbers are shaped by
    # the generator, and nobody should read them as production figures.
    st.sidebar.info("Generated sample data — not the real catalogue.", icon="🧪")

page = st.sidebar.radio(
    "Section",
    ["Overview", "Search Quality", "Suggestions Review", "Weekly Digest", "Ask the Agent"],
)

st.sidebar.markdown("---")
if gemini_client is None:
    st.sidebar.warning(
        "`GEMINI_API_KEY` is not set — the agent chat and the AI digest summary are "
        "disabled. Search-quality analytics and the suggestion queue still work fully.\n\n"
        + _key_hint()
    )
else:
    st.sidebar.success(f"Gemini connected · {GEMINI_MODEL}")

if st.sidebar.button("🔄 Refresh analytics cache"):
    st.cache_data.clear()
    st.rerun()


# ═════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═════════════════════════════════════════════════════════════════════════
if page == "Overview":
    st.title("Search-Quality Overview")

    overview = cached_overview()
    counts = cached_suggestion_counts()

    kpi_row(
        {
            "Health score": f"{overview['health_score']:.1f}/100",
            "Total searches": overview["total_searches"],
            "Distinct queries": overview["distinct_queries"],
            "Failing queries": overview["problem_queries"],
            "Retrieval gaps": overview["retrieval_gap_queries"],
            "Pending suggestions": counts.get("pending", 0),
        }
    )

    if overview.get("notes"):
        with st.expander("ℹ️ How to read these numbers", expanded=True):
            for note in overview["notes"]:
                st.markdown(f"- {note}")

    st.divider()
    qdf = cached_query_quality()

    col_l, col_r = st.columns([1.2, 1])
    with col_l:
        st.subheader("Top queries")
        st.caption("The demand ranking — where shoppers spend their attention.")
        fig = top_queries_chart(qdf)
        if fig:
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No search traffic loaded yet.")

    with col_r:
        st.subheader("Result-count distribution")
        st.caption(f"The engine caps results at {RESULT_CAP}; shorter bars mean under-filled searches.")
        fig2 = result_count_chart(cached_result_count_histogram())
        if fig2:
            st.plotly_chart(fig2, width="stretch")

    with st.expander("Data provenance"):
        st.json(meta)


# ═════════════════════════════════════════════════════════════════════════
# SEARCH QUALITY
# ═════════════════════════════════════════════════════════════════════════
elif page == "Search Quality":
    st.title("Search Quality Detail")

    overview = cached_overview()
    qdf = cached_query_quality()

    tab_zero, tab_engagement, tab_problems, tab_terms, tab_trend = st.tabs(
        [
            "Zero-result queries",
            "Low-engagement queries",
            "All problem queries",
            "Terms driving failures",
            "Daily trend",
        ]
    )

    with tab_zero:
        if overview["zero_result_rate"] == 0:
            st.caption(
                "No distinct query returned zero results in this period. That is a "
                "property of the engine, not a clean bill of health: it is "
                "embedding-based and always returns its nearest neighbours, so a "
                "relevance failure looks like WRONG results, never an empty page. "
                "Check the **Terms driving failures** tab and the agent for failures "
                "a result count alone can't see."
            )
        else:
            st.caption(f"{overview['zero_result_rate']:.1%} of all searches returned nothing.")
        zero_df = qdf[qdf["zero_result_rate"] > 0].sort_values("searches", ascending=False)
        st.metric("Zero-result queries", len(zero_df))
        if not zero_df.empty:
            st.dataframe(
                zero_df[
                    ["display_query", "script", "searches", "zero_result_rate", "catalog_coverage", "retrieval_gap"]
                ],
                width="stretch",
                hide_index=True,
            )

    with tab_engagement:
        st.caption(
            "This dataset logs no click stream and no order stream, so engagement is "
            "a behavioural proxy: a search followed by another search in the same "
            "session — and not a machine-speed identical repeat — counts as a sign "
            "the shopper was not satisfied. A search that ends its session is "
            "ambiguous and excluded rather than assumed successful."
        )
        threshold = st.slider("Dissatisfaction-rate threshold", 0.0, 1.0, 0.5, 0.05)
        eng_df = qdf[qdf["dissatisfaction_rate"].notna() & (qdf["dissatisfaction_rate"] >= threshold)]
        eng_df = eng_df.sort_values(["dissatisfaction_rate", "searches"], ascending=False)
        st.metric("Low-engagement queries", len(eng_df))
        if eng_df.empty:
            st.info("No query crosses this dissatisfaction threshold.")
        else:
            st.dataframe(
                eng_df[
                    [
                        "display_query",
                        "script",
                        "searches",
                        "dissatisfaction_rate",
                        "rapid_repeat_rate",
                        "mean_results",
                        "severity",
                    ]
                ],
                width="stretch",
                hide_index=True,
            )

    with tab_problems:
        prob_df = qdf[qdf["is_problem"]].sort_values("impact", ascending=False)
        st.metric("Problem queries", len(prob_df))
        if prob_df.empty:
            st.success("No query crosses the severity threshold this period.")
        else:
            st.dataframe(
                prob_df[
                    ["display_query", "searches", "severity", "impact", "retrieval_gap", "dominant_category"]
                ],
                width="stretch",
                hide_index=True,
            )
            st.markdown("**Why, for the top 5 by impact:**")
            for _, row in prob_df.head(5).iterrows():
                with st.expander(
                    f"“{row['display_query']}” — {row['searches']} searches, severity {row['severity']:.2f}"
                ):
                    for reason in row["reasons"]:
                        st.markdown(f"- {reason}")

    with tab_terms:
        st.caption(
            "Every spelling of a word folded onto one row, with a verdict naming who "
            "owns the fix: spelling wants a dictionary, a retrieval gap wants "
            "relevance tuning, an assortment gap is a buying question."
        )
        limit = st.slider("How many terms", 5, 25, 15)
        term_df = cached_term_drivers(limit=limit)
        if term_df.empty:
            st.info("Not enough traffic to roll up terms yet.")
        else:
            fig = term_drivers_chart(term_df)
            if fig:
                st.plotly_chart(fig, width="stretch")
            show_df = term_df.copy()
            show_df["spellings"] = show_df["spellings"].apply(", ".join)
            st.dataframe(
                show_df[
                    ["term", "verdict", "searches", "failing_searches", "catalog_coverage", "spellings", "headline"]
                ],
                width="stretch",
                hide_index=True,
            )

    with tab_trend:
        trend = cached_daily_trend()
        if len(trend) < 2:
            st.info(
                "Not enough distinct days in the loaded log to show a trend — this "
                "dataset spans a single day."
            )
            st.dataframe(trend, width="stretch", hide_index=True)
        else:
            st.plotly_chart(daily_trend_chart(trend), width="stretch")
            st.dataframe(trend, width="stretch", hide_index=True)


# ═════════════════════════════════════════════════════════════════════════
# SUGGESTIONS REVIEW
# ═════════════════════════════════════════════════════════════════════════
elif page == "Suggestions Review":
    st.title("Synonym & Misspelling Suggestions")
    st.caption(
        "Proposals are mined automatically from the query log and the bilingual "
        "catalogue (edit-distance spelling correction, cross-language term "
        "alignment, and shopper self-corrections). **Nothing is ever applied "
        "silently** — every proposal needs a human Accept or Reject before it can "
        "be exported to search configuration."
    )

    col_run, col_export, _ = st.columns([1.3, 1.3, 3])
    with col_run:
        if st.button("🔎 Run discovery", type="primary"):
            with st.spinner("Mining query logs and the catalogue for candidates..."):
                with store_connect(DB_PATH) as conn:
                    report = refresh_suggestions(conn)
                st.cache_data.clear()
            st.success(
                f"{report.proposed} new · {report.updated} re-scored · "
                f"{report.withdrawn} withdrawn (no longer supported) · "
                f"{report.unchanged_by_decision} already reviewed, left untouched"
            )

    with store_connect(DB_PATH, read_only=True) as _conn:
        approved_doc = export_approved(_conn)
    with col_export:
        st.download_button(
            "📤 Export approved rules",
            data=json.dumps(approved_doc, ensure_ascii=False, indent=2),
            file_name="search-pulse-rules.json",
            mime="application/json",
            disabled=approved_doc["approved_count"] == 0,
        )

    counts = cached_suggestion_counts()
    kpi_row(
        {
            "Pending": counts.get("pending", 0),
            "Approved": counts.get("approved", 0),
            "Rejected": counts.get("rejected", 0),
        }
    )

    st.divider()
    f1, f2 = st.columns([1, 3])
    with f1:
        kind_filter = st.selectbox("Kind", ["All", "misspelling", "synonym", "partial_query"])
    with f2:
        status_filter = st.radio("Status", ["pending", "approved", "rejected", "all"], horizontal=True)

    kind_arg = None if kind_filter == "All" else kind_filter
    status_arg = None if status_filter == "all" else status_filter
    df = cached_suggestions(status=status_arg, kind=kind_arg)

    if df.empty:
        st.info("No suggestions match this filter. Run discovery above to populate the queue.")
    else:
        st.caption(f"{len(df)} suggestion(s), strongest first.")
        overview_cols = ["kind", "source_term", "target_term", "lang", "confidence", "status"]
        st.dataframe(
            df[overview_cols].sort_values(["status", "confidence"], ascending=[True, False]),
            width="stretch",
            hide_index=True,
        )

        st.markdown("#### Review queue")
        for _, row in df.sort_values(["status", "confidence"], ascending=[True, False]).iterrows():
            c = st.columns([1.3, 2.6, 0.8, 1, 1.4])
            c[0].markdown(f"**{KIND_LABEL.get(row['kind'], row['kind'])}**")
            c[0].caption(row["lang"].upper())
            c[1].markdown(f"`{row['source_term']}` → `{row['target_term']}`")
            c[1].caption(row["rationale"])
            c[2].markdown(f"**{row['confidence']:.2f}**")
            c[3].markdown(STATUS_BADGE.get(row["status"], row["status"]))
            with c[4]:
                if row["status"] == "pending":
                    a, r = st.columns(2)
                    if a.button("✓ Accept", key=f"acc_{row['id']}"):
                        with store_connect(DB_PATH) as conn:
                            decide_suggestion(conn, int(row["id"]), decision="approved", reviewer="dashboard")
                        st.cache_data.clear()
                        st.rerun()
                    if r.button("✗ Reject", key=f"rej_{row['id']}"):
                        with store_connect(DB_PATH) as conn:
                            decide_suggestion(conn, int(row["id"]), decision="rejected", reviewer="dashboard")
                        st.cache_data.clear()
                        st.rerun()
                else:
                    if st.button("↺ Reset to pending", key=f"reset_{row['id']}"):
                        with store_connect(DB_PATH) as conn:
                            decide_suggestion(conn, int(row["id"]), decision="pending", reviewer="dashboard")
                        st.cache_data.clear()
                        st.rerun()
            st.divider()


# ═════════════════════════════════════════════════════════════════════════
# WEEKLY DIGEST
# ═════════════════════════════════════════════════════════════════════════
elif page == "Weekly Digest":
    st.title("Weekly Performance Digest")
    st.caption(
        "Every figure and table below is computed straight from the analytics "
        "store. Gemini only writes the opening paragraph from those "
        "already-computed numbers — it is never in a position to invent a metric."
    )

    days = st.number_input("Period length (days)", min_value=1, max_value=90, value=7)
    if st.button("📅 Generate digest", type="primary"):
        with st.spinner("Assembling the digest..."):
            with store_connect(DB_PATH) as conn:
                digest = searchiq_generate_digest(conn, days=int(days), use_model=False, store=True)
            ai_summary = gemini_digest_narrative(gemini_client, digest.metrics) if gemini_client else None
        st.session_state["digest"] = digest
        st.session_state["digest_ai_summary"] = ai_summary

    digest = st.session_state.get("digest")
    if digest is None:
        st.info("Click **Generate digest** to build the current period's report.")
    else:
        ai_summary = st.session_state.get("digest_ai_summary")
        if ai_summary:
            st.markdown(f"##### 🤖 Gemini summary · {GEMINI_MODEL}")
            st.info(ai_summary)
        elif gemini_client is None:
            st.caption("Set `GEMINI_API_KEY` to also get an AI-written summary paragraph here.")
        else:
            st.caption("Gemini did not return a summary for this digest; showing the computed report only.")

        st.markdown(digest.body_md)

        download_text = digest.body_md
        if ai_summary:
            download_text = f"> 🤖 Gemini summary: {ai_summary}\n\n" + download_text
        st.download_button(
            "📥 Download digest (Markdown)",
            data=download_text,
            file_name=f"digest-{digest.period_end[:10]}.md",
            mime="text/markdown",
        )


# ═════════════════════════════════════════════════════════════════════════
# ASK THE AGENT
# ═════════════════════════════════════════════════════════════════════════
elif page == "Ask the Agent":
    st.title("Ask the Agent")
    st.caption(f"Natural-language Q&A over your search-quality data, via Gemini ({GEMINI_MODEL}) tool-calling.")

    if gemini_client is None:
        st.error(f"`GEMINI_API_KEY` is not set. {_key_hint()}")
        st.stop()

    if "chat_contents" not in st.session_state:
        st.session_state.chat_contents = []
        st.session_state.chat_display = []

    for turn in st.session_state.chat_display:
        with st.chat_message(turn["role"]):
            st.markdown(turn["text"])
            if turn.get("trace"):
                with st.expander(f"🔧 {len(turn['trace'])} tool call(s)"):
                    for tc in turn["trace"]:
                        st.code(f"{tc['name']}({tc['args']})\n→ {tc['result_preview']}")

    with st.expander("💡 Suggested questions"):
        st.markdown(
            "- What are the biggest search problems this period?\n"
            "- Which queries look like misspellings, and what should they correct to?\n"
            "- Do we actually stock strawberries?\n"
            "- What's pending in the review queue?\n"
            "- What should the search team fix first?"
        )

    if prompt := st.chat_input("Ask about search performance..."):
        st.session_state.chat_display.append({"role": "user", "text": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    answer, trace, new_contents = ask_agent(
                        gemini_client, prompt, st.session_state.chat_contents
                    )
                except Exception as exc:  # a bad key, a network hiccup, a quota error
                    answer, trace, new_contents = (
                        f"The agent hit an error calling Gemini: {exc}",
                        [],
                        st.session_state.chat_contents,
                    )
            st.markdown(answer)
            if trace:
                with st.expander(f"🔧 {len(trace)} tool call(s)"):
                    for tc in trace:
                        st.code(f"{tc['name']}({tc['args']})\n→ {tc['result_preview']}")
        st.session_state.chat_contents = new_contents
        st.session_state.chat_display.append({"role": "assistant", "text": answer, "trace": trace})

    if st.button("Clear conversation"):
        st.session_state.chat_contents = []
        st.session_state.chat_display = []
        st.rerun()
