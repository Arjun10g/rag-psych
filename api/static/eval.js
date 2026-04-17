// /eval dashboard: fetches /eval/data and renders the Chart.js panels.
//
// We rely on the browser already having auth'd via HTTP Basic for the
// /eval HTML page, so the /eval/data fetch below carries the same
// credentials automatically. No manual token handling needed.

const SOURCE_COLORS = {
  mtsamples: "#22d3ee",  // cyan-400
  pubmed:    "#e879f9",  // fuchsia-400
  icd11:     "#34d399",  // emerald-400
};
const SLATE_400 = "#94a3b8";
const SLATE_300 = "#cbd5e1";
const SLATE_600 = "#475569";
const SLATE_800 = "#1e293b";

// Chart.js global defaults matched to the dark theme.
Chart.defaults.color = SLATE_400;
Chart.defaults.borderColor = SLATE_800;
Chart.defaults.font.family = '-apple-system, "Inter", "Segoe UI", sans-serif';

const PCT = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const MS = (v) => (v == null ? "—" : `${Math.round(v)} ms`);

(async function init() {
  try {
    const res = await fetch("/eval/data", { credentials: "include" });
    if (!res.ok) throw new Error(`/eval/data returned ${res.status}`);
    const data = await res.json();
    render(data);
  } catch (err) {
    const box = document.getElementById("eval-error");
    document.getElementById("eval-error-msg").textContent =
      `Couldn't load eval data: ${err.message}. ` +
      `Make sure eval/run_eval.py has been run at least once.`;
    box.classList.remove("hidden");
    console.error(err);
  }
})();

function render(data) {
  const runs = data.runs || [];
  const corpus = data.corpus || { docs: {}, chunks: {}, sections: [] };
  const latest = runs.length ? runs[runs.length - 1] : null;

  renderAggregateCards(latest, runs);
  if (latest) {
    renderPerQuery(latest);
    renderSourceMix(latest);
    renderLatency(latest);
  }
  if (runs.length > 1) {
    document.getElementById("run-history-section").classList.remove("hidden");
    renderRunHistory(runs);
  }
  renderCorpus(corpus);
  renderSections(corpus);
}

// ─── Aggregate headline cards ──────────────────────────────────────────────
function renderAggregateCards(latest, runs) {
  const host = document.getElementById("agg-cards");
  const agg = latest ? latest.aggregate : {};
  const cards = [
    { label: "queries", value: (latest?.n_queries ?? "—"), color: "cyan" },
    { label: "runs",    value: runs.length,                color: "slate" },
    { label: "routing top-1",  value: PCT(agg.source_routing_top1_rate), color: "cyan" },
    { label: "keyword recall", value: PCT(agg.mean_keyword_recall),      color: "fuchsia" },
    { label: "citation valid", value: PCT(agg.mean_citation_validity),   color: "emerald" },
    { label: "off-topic refusal", value: PCT(agg.off_topic_refusal_rate), color: "amber" },
    { label: "negation pass", value: PCT(agg.negation_pass_rate),        color: "emerald" },
    { label: "mean total",    value: MS(agg.mean_total_ms),              color: "slate" },
  ];
  host.innerHTML = cards.map(c => `
    <div class="agg-card agg-${c.color}">
      <div class="text-2xl font-light text-slate-100">${c.value}</div>
      <div class="text-[10px] uppercase tracking-widest text-slate-500 mt-1">${c.label}</div>
    </div>
  `).join("");
}

// ─── Per-query source-recall + keyword-recall grouped bars ─────────────────
function renderPerQuery(run) {
  const rows = run.per_query.filter(r => !r.off_topic);
  new Chart(document.getElementById("chart-per-query"), {
    type: "bar",
    data: {
      labels: rows.map(r => r.id),
      datasets: [
        {
          label: "source-recall@5",
          data: rows.map(r => (r.source_recall_top5 ?? 0) * 100),
          backgroundColor: "rgba(34,211,238,0.7)",   // cyan
          borderRadius: 3,
        },
        {
          label: "keyword recall",
          data: rows.map(r => (r.keyword_recall ?? 0) * 100),
          backgroundColor: "rgba(232,121,249,0.7)",  // fuchsia
          borderRadius: 3,
        },
        {
          label: "citation validity",
          data: rows.map(r => (r.citation_validity ?? 0) * 100),
          backgroundColor: "rgba(52,211,153,0.7)",   // emerald
          borderRadius: 3,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: SLATE_300 } } },
      scales: {
        y: { beginAtZero: true, max: 100,
             ticks: { callback: v => v + "%" } },
      },
    },
  });
}

// ─── Top-5 source mix per query (stacked horizontal bars) ──────────────────
function renderSourceMix(run) {
  const rows = run.per_query.filter(r => !r.off_topic);
  const sources = ["mtsamples", "pubmed", "icd11"];
  const datasets = sources.map(src => ({
    label: src,
    data: rows.map(r => (r.sources_top5 || []).filter(s => s === src).length),
    backgroundColor: SOURCE_COLORS[src],
    borderRadius: 2,
    stack: "top5",
  }));
  new Chart(document.getElementById("chart-source-mix"), {
    type: "bar",
    data: { labels: rows.map(r => r.id), datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      indexAxis: "y",
      plugins: { legend: { labels: { color: SLATE_300 } } },
      scales: {
        x: { stacked: true, min: 0, max: 5,
             ticks: { stepSize: 1 } },
        y: { stacked: true },
      },
    },
  });
}

// ─── Latency (retrieval vs generation) per query ───────────────────────────
function renderLatency(run) {
  const rows = run.per_query;
  new Chart(document.getElementById("chart-latency"), {
    type: "bar",
    data: {
      labels: rows.map(r => r.id),
      datasets: [
        {
          label: "retrieval ms",
          data: rows.map(r => r.retrieval_ms ?? 0),
          backgroundColor: "rgba(34,211,238,0.8)",
          stack: "t",
          borderRadius: 2,
        },
        {
          label: "generation ms",
          data: rows.map(r => r.generation_ms ?? 0),
          backgroundColor: "rgba(232,121,249,0.8)",
          stack: "t",
          borderRadius: 2,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: SLATE_300 } } },
      scales: {
        x: { stacked: true, ticks: { maxRotation: 45, minRotation: 45 } },
        y: { stacked: true, beginAtZero: true,
             ticks: { callback: v => v + " ms" } },
      },
    },
  });
}

// ─── Aggregate metrics across all runs ─────────────────────────────────────
function renderRunHistory(runs) {
  const labels = runs.map(r => (r.timestamp || "").replace("T", " ").replace("Z", ""));
  const series = [
    { key: "source_routing_top1_rate", label: "routing top-1", color: "rgba(34,211,238,0.9)" },
    { key: "mean_source_recall_top5",  label: "source recall@5", color: "rgba(232,121,249,0.9)" },
    { key: "mean_keyword_recall",      label: "keyword recall", color: "rgba(52,211,153,0.9)" },
    { key: "mean_citation_validity",   label: "citation valid", color: "rgba(251,191,36,0.9)" },
  ];
  new Chart(document.getElementById("chart-run-history"), {
    type: "line",
    data: {
      labels,
      datasets: series.map(s => ({
        label: s.label,
        data: runs.map(r => (r.aggregate?.[s.key] ?? null) * 100),
        borderColor: s.color,
        backgroundColor: s.color,
        tension: 0.3, pointRadius: 4,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: SLATE_300 } } },
      scales: {
        y: { beginAtZero: true, max: 100,
             ticks: { callback: v => v + "%" } },
      },
    },
  });
}

// ─── Corpus stats: docs + chunks per source ────────────────────────────────
function renderCorpus(corpus) {
  const sources = Object.keys(corpus.chunks || {});
  new Chart(document.getElementById("chart-corpus"), {
    type: "bar",
    data: {
      labels: sources,
      datasets: [
        {
          label: "documents",
          data: sources.map(s => corpus.docs?.[s] ?? 0),
          backgroundColor: sources.map(s => SOURCE_COLORS[s] || SLATE_400),
          borderRadius: 3,
          yAxisID: "y",
        },
        {
          label: "chunks",
          data: sources.map(s => corpus.chunks?.[s] ?? 0),
          backgroundColor: sources.map(s => (SOURCE_COLORS[s] || SLATE_400) + "55"),
          borderRadius: 3,
          yAxisID: "y",
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: SLATE_300 } } },
      scales: { y: { beginAtZero: true } },
    },
  });
}

// ─── Top sections per source (horizontal bar) ──────────────────────────────
function renderSections(corpus) {
  const sections = (corpus.sections || []).slice(0, 15);
  new Chart(document.getElementById("chart-sections"), {
    type: "bar",
    data: {
      labels: sections.map(s => `${s.source_type} / ${s.section}`),
      datasets: [{
        label: "chunks",
        data: sections.map(s => s.n),
        backgroundColor: sections.map(s => SOURCE_COLORS[s.source_type] || SLATE_400),
        borderRadius: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true } },
    },
  });
}
