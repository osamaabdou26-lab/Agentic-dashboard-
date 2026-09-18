// Search Pulse dashboard.
//
// Plain ES modules-free JavaScript with no build step and no dependencies: the
// dashboard is served by the same FastAPI process as the API, and a static file
// that needs no toolchain is one less thing to keep working.
//
// Every value shown here comes from the API. Nothing is computed client-side,
// so the dashboard and the agent can never disagree about a number. The only
// exceptions are sorting and text filtering, which reorder what the API already
// returned without changing any figure.

'use strict';

const state = {
  view: 'overview',
  queries: [],
  selectedQuery: null,
  suggestionStatus: 'pending',

  /* Sorting is a view preference, not data: the API always ranks by impact,
     and this reorders that same payload. */
  sort: { key: 'impact', direction: 'desc' },

  /* Reporting period. `all` means no filter. Any other value is a number of
     days counted back from the newest search in the log — not from today,
     because the shipped extract is historical and a wall-clock window would
     report an empty week against it. */
  period: 'all',
  logStart: null,
  logEnd: null,
};

const THEME_KEY = 'searchpulse-theme';

/* Dark is the default and the first state in the cycle: the palette is built
   for a black ground, and that is what an unconfigured visitor should see.
   `system` stays available as an explicit third choice rather than as the
   default, so someone who wants the dashboard to follow their machine still
   can. */
const THEME_ORDER = ['dark', 'light', 'system'];
const THEME_ICON = { dark: '☾', light: '☀', system: '◐' };

/* Utilities */

async function api(path, options) {
  if (window.SEARCHPULSE_STATIC) return staticApi(path, options);

  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

// Published snapshots have no API behind them. `searchiq export-site` writes
// every answer the dashboard asks for to data/*.json, and this maps the request
// onto the file holding it. Anything that would change state has no file,
// because a static host cannot honour it.
async function staticApi(path, options) {
  if (options && options.method && options.method !== 'GET') {
    throw new Error('This is a published snapshot — run the app locally to make changes.');
  }

  const [route, search] = path.split('?');
  const params = new URLSearchParams(search || '');

  // A query drill-down is looked up inside one map rather than fetched per
  // query: Arabic terms make awkward file names.
  if (route.startsWith('/api/queries/')) {
    const wanted = decodeURIComponent(route.slice('/api/queries/'.length));
    const details = await loadStaticFile('query-details');
    if (!details[wanted]) throw new Error(`No query “${wanted}” in this snapshot.`);
    return details[wanted];
  }

  const files = {
    '/api/status': 'status',
    '/api/overview': 'overview',
    '/api/terms': 'terms',
    '/api/queries': params.get('problems_only') === 'true' ? 'queries-problems' : 'queries-all',
    '/api/suggestions': `suggestions-${params.get('status') || 'pending'}`,
    '/api/digest': `digest-${params.get('days') || '7'}`,
  };

  const name = files[route];
  if (!name) throw new Error(`Not available in a published snapshot: ${route}`);
  return loadStaticFile(name);
}

const staticCache = new Map();

async function loadStaticFile(name) {
  if (!staticCache.has(name)) {
    staticCache.set(name, fetch(`data/${name}.json`).then((response) => {
      if (!response.ok) throw new Error(`Missing snapshot file: data/${name}.json`);
      return response.json();
    }));
  }
  return staticCache.get(name);
}

/**
 * Strip the controls a published snapshot cannot honour.
 *
 * Removing them beats leaving buttons that look live and do nothing; the note
 * explains where the working version is.
 */
function applyStaticMode() {
  if (!window.SEARCHPULSE_STATIC) return;

  const period = document.getElementById('period');
  period.disabled = true;
  period.title = 'Fixed in a published snapshot';

  document.getElementById('refresh-discovery').remove();
  document.getElementById('export-rules').href = 'data/rules-export.json';
  const download = document.getElementById('download-digest');
  if (download) download.remove();

  const ask = document.getElementById('ask-form');
  ask.hidden = true;
  document.getElementById('ask-examples').innerHTML =
    '<p class="empty">The agent needs a running server. Start the app locally ' +
    'with <code>searchiq serve</code> to ask questions.</p>';

  const exported = window.SEARCHPULSE_EXPORTED_AT
    ? ` Exported ${String(window.SEARCHPULSE_EXPORTED_AT).slice(0, 16).replace('T', ' ')}.`
    : '';
  const note = document.getElementById('period-note');
  note.hidden = false;
  note.textContent =
    'Published snapshot — figures are fixed as exported, and the review queue is ' +
    `read-only.${exported}`;
}

/** Escape text before it ever reaches innerHTML. Product names and query terms
 *  are shopper-supplied data and are treated as untrusted throughout. */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

const percent = (value) => (value == null ? '–' : `${Math.round(value * 100)}%`);
const number = (value) => (value == null ? '–' : value.toLocaleString());

/** Severity band. Every use is paired with a text label so colour is never the
 *  only carrier of meaning. */
function band(severity) {
  if (severity >= 0.5) return { key: 'severe', label: 'Severe' };
  if (severity >= 0.3) return { key: 'warn', label: 'Needs attention' };
  return { key: 'ok', label: 'Healthy' };
}

function toast(message) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 3200);
}

/** Fill a container with shimmer rows while its data is in flight. */
function showSkeleton(container, rows = 3) {
  container.innerHTML =
    `<div class="skeleton">${'<div class="skeleton-row"></div>'.repeat(rows)}</div>`;
}

/* Reporting period */

/**
 * Query-string fragment for the selected period, anchored to the log.
 *
 * Returns an empty string for "all logged data", so the unfiltered call stays
 * exactly what it was before this control existed.
 */
function periodQuery() {
  if (state.period === 'all' || !state.logEnd) return '';

  const end = new Date(state.logEnd.replace(' ', 'T'));
  if (Number.isNaN(end.getTime())) return '';

  const start = new Date(end);
  start.setDate(start.getDate() - Number(state.period));

  const stamp = (date) => date.toISOString().slice(0, 19).replace('T', ' ');
  return `&since=${encodeURIComponent(stamp(start))}&until=${encodeURIComponent(stamp(end))}`;
}

/** Same fragment, for an endpoint that has no other parameters. */
function periodQueryFirst() {
  const fragment = periodQuery();
  return fragment ? `?${fragment.slice(1)}` : '';
}

/**
 * Say on screen which slice of the log the figures were computed from.
 *
 * A filtered number that looks like a total is the easiest way for a dashboard
 * to mislead, so the banner appears whenever a filter is active and disappears
 * when it is not.
 */
function renderPeriodNote() {
  // A published snapshot owns this banner and says something more important.
  if (window.SEARCHPULSE_STATIC) return;

  const note = document.getElementById('period-note');
  if (state.period === 'all' || !state.logEnd) {
    note.hidden = true;
    return;
  }
  const label = document.querySelector(`#period option[value="${state.period}"]`);
  note.hidden = false;
  note.textContent =
    `Showing ${(label ? label.textContent : state.period).toLowerCase()} — ` +
    `every figure below is computed from that window, ending ${state.logEnd}.`;
}

/* Theme */

function readTheme() {
  try {
    return localStorage.getItem(THEME_KEY) || 'dark';
  } catch (error) {
    return 'dark';
  }
}

function applyTheme(theme) {
  /* Dark is what the bare `:root` already carries, so it is expressed by
     stamping nothing. `system` is a real attribute rather than an absent one,
     because the stylesheet has to redefine the tokens for it in both
     directions. */
  if (theme === 'dark') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', theme);
  }
  try {
    if (theme === 'dark') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch (error) {
    /* storage blocked; the choice simply does not persist */
  }
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = THEME_ICON[theme];
  const button = document.getElementById('theme-toggle');
  if (button) button.title = `Colour theme: ${theme}`;
}

function cycleTheme() {
  const next = THEME_ORDER[(THEME_ORDER.indexOf(readTheme()) + 1) % THEME_ORDER.length];
  applyTheme(next);
  toast(`Theme: ${next}`);
}

/* Navigation */

const loaders = {
  overview: loadOverview,
  queries: loadQueries,
  terms: loadTerms,
  suggestions: loadSuggestions,
  ask: loadAskExamples,
  digest: () => {},
};

function showView(name, { updateHash = true } = {}) {
  if (!loaders[name]) name = 'overview';
  state.view = name;

  document.querySelectorAll('.tab').forEach((tab) => {
    const active = tab.dataset.view === name;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
    // Roving tabindex: the tablist is one stop, arrow keys move within it.
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.view').forEach((view) => {
    view.classList.toggle('is-active', view.dataset.view === name);
  });

  if (updateHash) {
    const target = `#${name}`;
    if (window.location.hash !== target) {
      history.pushState(null, '', target);
    }
  }

  // Loaders are async. Without this catch a failed fetch would leave the panel
  // silently blank, which reads as "no problems found" — the most misleading
  // thing this dashboard could possibly show.
  Promise.resolve(loaders[name]()).catch((error) => {
    toast(`Could not load ${name}: ${error.message}`);
    console.error(error);
  });
}

/**
 * Restore the view, and any selected query, from the address bar.
 *
 * Deep links matter here: "look at this query" is the most common thing one
 * analyst says to another, and it should survive being pasted into a message.
 */
function routeFromHash({ updateHash = false } = {}) {
  const raw = decodeURIComponent(window.location.hash.replace(/^#/, ''));
  const [view, query] = raw.split('/');
  showView(view || 'overview', { updateHash });
  if (view === 'queries' && query) {
    state.selectedQuery = query;
  }
}

/* Status */

async function loadStatus() {
  const element = document.getElementById('provenance');
  try {
    const status = await api('/api/status');
    if (!status.ready) {
      element.textContent = status.message;
      return;
    }
    const meta = status.meta || {};
    const mode = status.agent_mode === 'model'
      ? `agent: ${status.model}`
      : 'agent: offline planner';
    element.textContent =
      `${number(Number(meta['rows.search_event'] || 0))} searches · ` +
      `${number(Number(meta['rows.product'] || 0))} products · ` +
      `loaded ${(meta.loaded_at || '').slice(0, 16).replace('T', ' ')} · ${mode}`;
  } catch (error) {
    element.textContent = `Cannot reach the API: ${error.message}`;
  }
}

/* Overview */

async function loadOverview() {
  const overview = await api(`/api/overview${periodQueryFirst()}`);

  // The very first unfiltered response defines the log window every relative
  // period is measured back from.
  if (state.period === 'all' && overview.period_end) {
    state.logStart = overview.period_start;
    state.logEnd = overview.period_end;
  }
  renderPeriodNote();

  renderDial(overview.health_score);

  document.getElementById('health-score').textContent =
    overview.total_searches ? overview.health_score : '–';
  document.getElementById('health-headline').textContent =
    overview.total_searches
      ? `${number(overview.problem_queries)} of ${number(overview.distinct_queries)} queries need attention`
      : 'No searches in this period';
  document.getElementById('health-period').textContent =
    overview.period_start
      ? `${overview.period_start} to ${overview.period_end} · ${number(overview.total_searches)} searches across ${number(overview.sessions)} sessions`
      : '';

  document.getElementById('health-notes').innerHTML =
    (overview.notes || []).map((note) => `<li>${escapeHtml(note)}</li>`).join('');

  renderKpis(overview);

  const chart = document.getElementById('impact-chart');
  const cards = document.getElementById('overview-problems');
  showSkeleton(chart, 5);
  showSkeleton(cards, 3);

  const queries = await api(`/api/queries?problems_only=true&limit=50${periodQuery()}`);
  renderImpactChart(queries.slice(0, 8));
  renderProblemCards(queries.slice(0, 5), 'overview-problems');
  renderHeroStats(overview);
}

/**
 * Fill the hero's headline figures.
 *
 * These are live values, not copy: if the ETL has not run, the hero says so by
 * showing dashes rather than claiming numbers the store cannot back.
 */
async function renderHeroStats(overview) {
  const set = (name, value) => {
    const element = document.querySelector(`#hero-stats [data-stat="${name}"]`);
    if (element) element.textContent = value;
  };

  set('searches', number(overview.total_searches));
  set('problems', number(overview.problem_queries));

  // Product count and proposal count are not in the overview payload, so they
  // are fetched alongside it. A failure here leaves the dashes in place rather
  // than blanking the hero.
  try {
    const [status, proposals] = await Promise.all([
      api('/api/status'),
      api('/api/suggestions?status=pending&limit=500'),
    ]);
    set('products', number(Number(status.meta?.['rows.product'] || 0)));
    set('proposals', number(proposals.length));
  } catch (error) {
    /* leave the dashes in place rather than claim a number */
  }
}

function renderDial(score) {
  const circumference = 327;
  const clamped = Math.max(0, Math.min(100, Number(score) || 0));
  const dial = document.getElementById('dial-value');
  dial.style.strokeDashoffset = String(circumference * (1 - clamped / 100));
  dial.style.stroke =
    clamped >= 80 ? 'var(--ok)' : clamped >= 60 ? 'var(--warn)' : 'var(--severe)';
}

function renderKpis(overview) {
  const engagementNote = overview.engagement_source === 'behavioural proxy'
    ? 'inferred from repeat searches; no click data exists'
    : 'measured from click-through';

  const cards = [
    {
      value: percent(overview.lexical_miss_rate),
      label: 'Irrelevant results',
      note: 'searches where nothing returned mentions the query',
    },
    {
      value: percent(overview.zero_result_rate),
      label: 'Zero results',
      note: 'the engine always returns its nearest neighbours',
    },
    {
      value: percent(overview.under_filled_rate),
      label: 'Under-filled',
      note: 'fewer results than the engine is allowed to return',
    },
    {
      value: number(overview.retrieval_gap_queries),
      label: 'Retrieval gaps',
      note: 'stocked in the catalogue, not surfaced by search',
    },
    {
      value: percent(overview.dissatisfaction_rate),
      label: 'Retried or reworded',
      note: engagementNote,
    },
    {
      value: percent(overview.automated_traffic_rate),
      label: 'Machine-like traffic',
      note: 'identical repeats under two seconds, excluded from engagement',
    },
  ];

  document.getElementById('kpis').innerHTML = cards.map((card) => `
    <div class="kpi">
      <div class="kpi-value">${escapeHtml(card.value)}</div>
      <div class="kpi-label">${escapeHtml(card.label)}</div>
      <div class="kpi-note">${escapeHtml(card.note)}</div>
    </div>
  `).join('');
}

function renderImpactChart(queries) {
  const container = document.getElementById('impact-chart');
  if (!queries.length) {
    container.innerHTML = '<p class="empty">No query is failing badly enough to chart.</p>';
    return;
  }
  const peak = Math.max(...queries.map((query) => query.impact)) || 1;
  container.innerHTML = queries.map((query) => {
    const tone = band(query.severity);
    return `
      <div class="bar-row">
        <span class="bar-label" dir="auto" title="${escapeHtml(query.display_query)}">
          ${escapeHtml(query.display_query)}
        </span>
        <span class="bar-track">
          <span class="bar-fill"
                style="width:${(query.impact / peak) * 100}%;background:var(--${tone.key})"></span>
        </span>
        <span class="bar-value">${query.searches} searches</span>
      </div>`;
  }).join('');
}

function renderProblemCards(queries, targetId) {
  const container = document.getElementById(targetId);
  if (!queries.length) {
    container.innerHTML = '<p class="empty">Nothing is currently flagged.</p>';
    return;
  }
  container.innerHTML = queries.map((query) => {
    const tone = band(query.severity);
    return `
      <article class="card sev-${tone.key}">
        <div class="card-head">
          <span class="card-title" dir="auto">${escapeHtml(query.display_query)}</span>
          <span>
            <span class="badge badge-${tone.key}">${tone.label}</span>
            <span class="card-meta">${query.searches} searches · severity ${query.severity.toFixed(2)}</span>
          </span>
        </div>
        <ul>${(query.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>
      </article>`;
  }).join('');
}

/* Queries */

async function loadQueries() {
  const problemsOnly = document.getElementById('problems-only').checked;
  const body = document.querySelector('#queries-table tbody');
  body.innerHTML = '<tr><td colspan="8"><div class="skeleton-row"></div></td></tr>';

  state.queries = await api(
    `/api/queries?problems_only=${problemsOnly}&limit=500${periodQuery()}`
  );
  renderQueryTable();

  // A deep link names a query; open it once the rows exist.
  if (state.selectedQuery) {
    const wanted = state.selectedQuery;
    const row = body.querySelector(`tr[data-query="${CSS.escape(wanted)}"]`);

    if (row) {
      state.selectedQuery = null;
      selectQuery(wanted, row, { updateHash: false });
    } else if (problemsOnly) {
      // A shared link can name a perfectly healthy query, which the default
      // filter hides. Widen the view and try once more rather than leave the
      // recipient staring at a table that does not contain what they opened.
      document.getElementById('problems-only').checked = false;
      await loadQueries();
    } else {
      state.selectedQuery = null;
      toast(`No query “${wanted}” in this period.`);
    }
  }
}

/** Reorder the rows the API returned. No figure is recomputed. */
function sortQueries(rows) {
  const { key, direction } = state.sort;
  const sign = direction === 'asc' ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = left[key];
    const b = right[key];
    if (a == null && b == null) return 0;
    if (a == null) return 1;          // unmeasured values sort last either way
    if (b == null) return -1;
    if (typeof a === 'string') return sign * a.localeCompare(b);
    return sign * (a - b);
  });
}

function renderQueryTable() {
  const filter = document.getElementById('query-filter').value.trim().toLowerCase();
  // Two independent narrowings: severity, and whether the query ever came
  // back empty. The brief names zero-result queries as their own class of
  // problem, and on an engine that always returns something they would
  // otherwise be invisible in a list ranked by severity.
  const emptyOnly = document.getElementById('empty-only').checked;
  const matched = state.queries.filter(
    (query) =>
      (!filter || query.display_query.toLowerCase().includes(filter)) &&
      (!emptyOnly || query.zero_result_rate > 0)
  );
  const rows = sortQueries(matched);
  const body = document.querySelector('#queries-table tbody');

  document.querySelectorAll('#queries-table .sort').forEach((button) => {
    if (button.dataset.sort === state.sort.key) {
      button.setAttribute('aria-sort', state.sort.direction === 'asc' ? 'ascending' : 'descending');
    } else {
      button.removeAttribute('aria-sort');
    }
  });

  if (!rows.length) {
    // An empty table has to say *why* it is empty. "No queries in this period"
    // in front of an active filter reads as "we checked and search is fine",
    // which is the most misleading thing this dashboard could show — and on
    // this engine, no zero-result query is a real finding, not an absence.
    let reason;
    if (filter) {
      reason = 'No queries match that filter.';
    } else if (emptyOnly) {
      reason =
        'No query ever returned zero results. This engine is embedding-based ' +
        'and always hands back its nearest neighbours, so failures here look ' +
        'like wrong results rather than empty ones — check the Irrelevant column.';
    } else {
      reason = 'No queries in this period.';
    }
    body.innerHTML = `<tr><td colspan="9" class="empty">${escapeHtml(reason)}</td></tr>`;
    return;
  }

  body.innerHTML = rows.map((query) => {
    const tone = band(query.severity);
    // `<bdi>` isolates the Arabic term so it cannot drag the surrounding
    // English words and digits into right-to-left order.
    const diagnosis = query.retrieval_gap && query.intended_term
      ? `Misspelling of <bdi>${escapeHtml(query.intended_term)}</bdi> — ${number(query.intended_coverage)} products missed`
      : escapeHtml((query.reasons || [])[0] || 'Performing normally');
    // `data-label` is what lets the table restack as cards on a phone.
    return `
      <tr data-query="${escapeHtml(query.norm_query)}" tabindex="0">
        <td dir="auto">${escapeHtml(query.display_query)}</td>
        <td class="num" data-label="Searches">${query.searches}</td>
        <td class="num" data-label="Severity"><span class="badge badge-${tone.key}">${query.severity.toFixed(2)}</span></td>
        <td class="num" data-label="Impact">${query.impact.toFixed(3)}</td>
        <td class="num" data-label="Avg results">${query.mean_results}</td>
        <td class="num" data-label="Empty">${percent(query.zero_result_rate)}</td>
        <td class="num" data-label="Irrelevant">${percent(query.lexical_miss_rate)}</td>
        <td class="num" data-label="Retried">${percent(query.dissatisfaction_rate)}</td>
        <td>${diagnosis}</td>
      </tr>`;
  }).join('');

  body.querySelectorAll('tr[data-query]').forEach((row) => {
    const open = () => selectQuery(row.dataset.query, row);
    row.addEventListener('click', open);
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });
  });
}

async function selectQuery(normQuery, row, { updateHash = true } = {}) {
  document.querySelectorAll('#queries-table tbody tr').forEach((other) => {
    other.classList.toggle('is-selected', other === row);
  });

  if (updateHash) {
    history.replaceState(null, '', `#queries/${encodeURIComponent(normQuery)}`);
  }

  const panel = document.getElementById('query-detail');
  panel.hidden = false;
  showSkeleton(panel, 2);

  try {
    const detail = await api(`/api/queries/${encodeURIComponent(normQuery)}`);
    const results = detail.sample_results || [];
    panel.innerHTML = `
      <div class="card-head">
        <span class="card-title" dir="auto">${escapeHtml(detail.display_query)}</span>
        <span class="card-meta">
          ${detail.searches} searches · ${detail.sessions} sessions ·
          catalogue holds ${number(detail.catalog_coverage)} matching products
        </span>
      </div>
      <ul>${(detail.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>
      <h3>Most recently returned</h3>
      ${results.length ? `<ol class="result-list">${results.map((result, index) => `
        <li>
          <span class="result-rank">${index + 1}</span>
          <span>
            <span dir="auto">${escapeHtml(result.en || result.ar)}</span>
            ${result.en && result.ar ? `<span class="result-ar" dir="auto">${escapeHtml(result.ar)}</span>` : ''}
          </span>
        </li>`).join('')}</ol>` : '<p class="muted">No results recorded.</p>'}
    `;
  } catch (error) {
    panel.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

/* Terms */

const VERDICT_TONE = {
  'spelling': 'warn',
  'retrieval gap': 'severe',
  'assortment gap': 'info',
  'partial query': 'info',
  'relevance': 'warn',
  'healthy': 'ok',
};

async function loadTerms() {
  const container = document.getElementById('terms-list');
  showSkeleton(container, 4);

  const drivers = await api(`/api/terms?limit=25${periodQuery()}`);

  if (!drivers.length) {
    container.innerHTML = '<p class="empty">No terms to report in this period.</p>';
    return;
  }

  container.innerHTML = drivers.map((driver) => {
    const tone = VERDICT_TONE[driver.verdict] || 'info';
    const cardTone = tone === 'info' ? 'ok' : tone;
    // Spellings are the evidence for the verdict, so they open from the card
    // rather than sitting in a separate view.
    const spellings = driver.distinct_spellings > 1
      ? `<details>
           <summary>Spelled ${driver.distinct_spellings} ways</summary>
           <div class="evidence" dir="auto">${driver.spellings.map(escapeHtml).join(' · ')}</div>
         </details>`
      : '';
    return `
      <article class="card sev-${cardTone}">
        <div class="card-head">
          <span class="card-title" dir="auto">${escapeHtml(driver.term)}</span>
          <span>
            <span class="badge badge-${tone}">${escapeHtml(driver.verdict)}</span>
            <span class="card-meta">
              ${driver.searches} searches · ${driver.failing_searches} failing ·
              ${number(driver.catalog_coverage)} products
            </span>
          </span>
        </div>
        <p>${escapeHtml(driver.headline)}</p>
        ${spellings}
      </article>`;
  }).join('');
}

/* Review queue */

const KIND_LABEL = {
  misspelling: 'Misspelling',
  synonym: 'Synonym',
  partial_query: 'Partial query',
};

const STATUS_LABEL = {
  pending: 'Awaiting review',
  approved: 'Approved',
  rejected: 'Rejected',
};

async function loadSuggestions() {
  const container = document.getElementById('suggestions-list');
  showSkeleton(container, 4);

  const suggestions = await api(`/api/suggestions?status=${state.suggestionStatus}&limit=200`);
  renderSuggestionCounts(suggestions);

  if (!suggestions.length) {
    container.innerHTML =
      `<p class="empty">Nothing ${escapeHtml(state.suggestionStatus)}. Re-run discovery to look again.</p>`;
    return;
  }

  container.innerHTML = suggestions.map(renderSuggestionCard).join('');

  container.querySelectorAll('[data-decide]').forEach((button) => {
    button.addEventListener('click', () =>
      decide(button.dataset.id, button.dataset.decide));
  });
}

/**
 * One proposal, with the controls that apply to it in its current state.
 *
 * A pending proposal offers Approve and Reject and nothing else; a decided one
 * shows who decided and offers Undo. Rendering the controls from the status,
 * rather than always drawing all three, is what makes "no proposal is ever
 * applied without a decision" visible rather than merely true.
 */
function renderSuggestionCard(suggestion) {
  const tone = suggestion.confidence >= 0.8 ? 'ok' : suggestion.confidence >= 0.6 ? 'warn' : 'info';
  const decided = suggestion.status !== 'pending';

  // A snapshot has no API behind it, so it states the position instead of
  // offering controls that could not be honoured.
  let actions;
  if (window.SEARCHPULSE_STATIC) {
    actions = `<div class="evidence">${decided
      ? `${escapeHtml(STATUS_LABEL[suggestion.status] || suggestion.status)} by ` +
        `${escapeHtml(suggestion.reviewer || 'unknown')}`
      : 'Awaiting review — open the local app to decide on it.'}</div>`;
  } else if (decided) {
    actions = `
      <div class="review-state">
        <span class="badge badge-${suggestion.status === 'approved' ? 'ok' : 'info'}">
          ${escapeHtml(STATUS_LABEL[suggestion.status] || suggestion.status)}
        </span>
        by ${escapeHtml(suggestion.reviewer || 'unknown')}
        on ${escapeHtml((suggestion.updated_at || '').slice(0, 16).replace('T', ' '))}
        ${suggestion.review_note ? `— “${escapeHtml(suggestion.review_note)}”` : ''}
      </div>
      <div class="card-actions">
        <button class="button" data-decide="pending" data-id="${suggestion.id}">
          Undo — return to queue
        </button>
      </div>`;
  } else {
    // The note travels with the decision. Why a proposal was turned down is the
    // part a later reviewer most needs and the part most easily lost.
    actions = `
      <input class="review-note" type="text" data-note="${suggestion.id}"
             placeholder="Reason (optional) — recorded with your decision"
             aria-label="Reason for your decision">
      <div class="card-actions">
        <button class="button button-primary" data-decide="approved" data-id="${suggestion.id}">
          Approve
        </button>
        <button class="button" data-decide="rejected" data-id="${suggestion.id}">
          Reject
        </button>
      </div>`;
  }

  return `
    <article class="card sev-${tone}">
      <div class="card-head">
        <span class="rule">
          <code dir="auto">${escapeHtml(suggestion.source_term)}</code>
          <span class="arrow">&rarr;</span>
          <code dir="auto">${escapeHtml(suggestion.target_term)}</code>
        </span>
        <span>
          <span class="badge badge-info">${escapeHtml(KIND_LABEL[suggestion.kind] || suggestion.kind)}</span>
          <span class="badge badge-${tone}">${suggestion.confidence.toFixed(2)}</span>
        </span>
      </div>
      <p>${escapeHtml(suggestion.rationale)}</p>
      <details>
        <summary>Evidence</summary>
        <div class="evidence">${escapeHtml(JSON.stringify(suggestion.evidence, null, 1))}</div>
      </details>
      ${actions}
    </article>`;
}

function renderSuggestionCounts(suggestions) {
  const line = document.getElementById('suggestions-counts');
  if (!line) return;
  const byKind = suggestions.reduce((totals, suggestion) => {
    totals[suggestion.kind] = (totals[suggestion.kind] || 0) + 1;
    return totals;
  }, {});
  const parts = Object.keys(byKind).sort()
    .map((kind) => `${byKind[kind]} ${(KIND_LABEL[kind] || kind).toLowerCase()}`);
  line.textContent = suggestions.length
    ? `${suggestions.length} ${state.suggestionStatus} — ${parts.join(', ')}.`
    : '';
}

/**
 * Show how many proposals are waiting, on the tab itself.
 *
 * A review queue nobody can see the size of is a review queue that stops being
 * reviewed, and this system's whole claim is that a person decides.
 */
async function refreshPendingBadge() {
  const badge = document.getElementById('pending-badge');
  if (!badge) return;
  try {
    const summary = await api('/api/suggestions/summary');
    const pending = summary.by_status.pending || 0;
    badge.textContent = String(pending);
    badge.hidden = pending === 0;
    badge.title = `${pending} proposal(s) awaiting a decision`;
  } catch (error) {
    badge.hidden = true;
  }
}

async function decide(id, decision) {
  const field = document.querySelector(`[data-note="${id}"]`);
  const note = field && field.value.trim() ? field.value.trim() : null;

  try {
    await api(`/api/suggestions/${id}/${decision}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reviewer: 'dashboard', note }),
    });
    toast(decision === 'pending'
      ? 'Decision undone. The proposal is back in the queue and out of the export.'
      : `Proposal ${decision}. Nothing is live until the rules are exported.`);
    loadSuggestions();
    refreshPendingBadge();
  } catch (error) {
    toast(error.message);
  }
}

/* Ask */

const EXAMPLE_QUESTIONS = [
  'How is search doing?',
  'Which queries need attention?',
  'What synonyms did you find?',
  'Do we sell strawberries?',
  'How much data is loaded?',
];

function loadAskExamples() {
  const container = document.getElementById('ask-examples');
  if (container.childElementCount) return;
  container.innerHTML = EXAMPLE_QUESTIONS
    .map((question) => `<button class="chip" type="button">${escapeHtml(question)}</button>`)
    .join('');
  container.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.getElementById('ask-input').value = chip.textContent.trim();
      document.getElementById('ask-form').requestSubmit();
    });
  });
}

async function submitQuestion(event) {
  event.preventDefault();
  const input = document.getElementById('ask-input');
  const question = input.value.trim();
  if (!question) return;

  const panel = document.getElementById('ask-answer');
  panel.hidden = false;
  showSkeleton(panel, 3);

  try {
    const answer = await api('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const trace = (answer.tool_calls || [])
      .map((call) => `<code>${escapeHtml(call.name)}</code>`)
      .join(' &rarr; ') || 'none';
    const source = answer.source === 'model'
      ? `answered by ${escapeHtml(answer.model)}`
      : 'answered by the offline planner (no API key configured)';

    panel.innerHTML = `
      <div class="answer-body" dir="auto">${escapeHtml(answer.answer)}</div>
      <div class="trace">Tools called: ${trace} · ${source}</div>`;
  } catch (error) {
    panel.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

/* Digest */

async function generateDigest() {
  const button = document.getElementById('generate-digest');
  const days = document.getElementById('digest-days').value;
  const body = document.getElementById('digest-body');

  button.disabled = true;
  showSkeleton(body, 6);
  try {
    const digest = await api(`/api/digest?days=${days}`);
    body.innerHTML = renderMarkdown(digest.body_md);
    const download = document.getElementById('download-digest');
    if (download) download.href = `/api/digest?days=${days}&format=markdown`;
  } catch (error) {
    body.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  } finally {
    button.disabled = false;
  }
}

/**
 * Render the subset of Markdown the digest generator emits: headings, tables,
 * lists, bold, italics and inline code.
 *
 * Input is escaped before any markup is added, so digest content — which
 * includes shopper-typed query terms — can never inject HTML.
 */
function renderMarkdown(markdown) {
  const lines = escapeHtml(markdown).split('\n');
  const html = [];
  let list = null;
  let table = null;

  const closeBlocks = () => {
    if (list) { html.push(`</${list}>`); list = null; }
    if (table) { html.push('</tbody></table>'); table = null; }
  };

  /* The digest marks each period-over-period move as (better) or (worse). In a
     column of signed numbers that word is the part a reader actually wants, and
     it is the slowest thing to find. Colouring it costs nothing and the word
     itself stays, so the meaning does not depend on the colour. */
  const inline = (text) => text
    .replace(/`([^`]+)`/g, '<code dir="auto">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\((better|worse)\)/g, '<span class="delta delta-$1">($1)</span>');

  for (const line of lines) {
    const trimmed = line.trim();

    if (!trimmed) { closeBlocks(); continue; }

    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeBlocks();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (trimmed.startsWith('|')) {
      const cells = trimmed.slice(1, -1).split('|').map((cell) => cell.trim());
      if (cells.every((cell) => /^-{1,}:?$|^:?-{1,}:?$/.test(cell))) continue; // separator
      if (!table) {
        html.push('<table><thead><tr>' + cells.map((cell) => `<th>${inline(cell)}</th>`).join('') + '</tr></thead><tbody>');
        table = true;
        continue;
      }
      html.push('<tr>' + cells.map((cell) => `<td dir="auto">${inline(cell)}</td>`).join('') + '</tr>');
      continue;
    }

    const bullet = trimmed.match(/^[-*]\s+(.*)$/);
    if (bullet) {
      if (!list) { html.push('<ul>'); list = 'ul'; }
      html.push(`<li dir="auto">${inline(bullet[1])}</li>`);
      continue;
    }

    closeBlocks();
    html.push(`<p dir="auto">${inline(trimmed)}</p>`);
  }

  closeBlocks();
  return html.join('\n');
}

/* Wiring */

/** Arrow-key movement within the tablist, as a tablist is expected to behave. */
function onTabKeydown(event) {
  const keys = { ArrowRight: 1, ArrowLeft: -1, Home: 'first', End: 'last' };
  if (!(event.key in keys)) return;

  const tabs = [...document.querySelectorAll('.tab')];
  const current = tabs.indexOf(event.currentTarget);
  let next;
  if (keys[event.key] === 'first') next = 0;
  else if (keys[event.key] === 'last') next = tabs.length - 1;
  else next = (current + keys[event.key] + tabs.length) % tabs.length;

  event.preventDefault();
  tabs[next].focus();
  showView(tabs[next].dataset.view);
}

document.addEventListener('DOMContentLoaded', () => {
  applyTheme(readTheme());
  document.getElementById('theme-toggle').addEventListener('click', cycleTheme);

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => showView(tab.dataset.view));
    tab.addEventListener('keydown', onTabKeydown);
  });

  // Back and forward should move between views, not out of the dashboard.
  window.addEventListener('popstate', () => routeFromHash());

  document.getElementById('period').addEventListener('change', (event) => {
    state.period = event.target.value;
    renderPeriodNote();
    // Only the data views depend on the period; reloading the active one is
    // enough, and the others refresh when they are next opened.
    Promise.resolve(loaders[state.view]()).catch((error) => toast(error.message));
  });

  document.getElementById('problems-only').addEventListener('change', loadQueries);
  document.getElementById('empty-only').addEventListener('change', renderQueryTable);
  document.getElementById('query-filter').addEventListener('input', renderQueryTable);

  document.querySelectorAll('#queries-table .sort').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      state.sort = state.sort.key === key
        ? { key, direction: state.sort.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: key === 'display_query' ? 'asc' : 'desc' };
      renderQueryTable();
    });
  });

  document.querySelectorAll('#suggestion-filters .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#suggestion-filters .chip')
        .forEach((other) => other.classList.toggle('is-active', other === chip));
      state.suggestionStatus = chip.dataset.status;
      loadSuggestions();
    });
  });

  document.getElementById('refresh-discovery').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const report = await api('/api/suggestions/refresh', { method: 'POST' });
      toast(`${report.proposed} new, ${report.updated} re-scored, ${report.withdrawn} withdrawn, ${report.unchanged_by_decision} left as reviewed.`);
      loadSuggestions();
      refreshPendingBadge();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll('[data-goto]').forEach((link) => {
    link.addEventListener('click', () => {
      showView(link.dataset.goto);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  document.getElementById('ask-form').addEventListener('submit', submitQuestion);
  document.getElementById('generate-digest').addEventListener('click', generateDigest);

  const toTop = document.getElementById('to-top');
  toTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  window.addEventListener('scroll', () => {
    toTop.hidden = window.scrollY < 500;
  }, { passive: true });

  applyStaticMode();
  loadStatus();
  refreshPendingBadge();
  routeFromHash({ updateHash: false });
});
