// Where this instance gets its data. /app talks to the live database; /demo
// talks to the committed snapshot, which has the same shape so every view
// below is shared rather than reimplemented for marketing.
const IG = Object.assign({ demo: false, api: "/api" }, window.IG || {});

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const inr = (v) => v == null ? "—" : "₹" + Number(v).toLocaleString("en-IN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " Cr";
const day = (d) => d ? new Date(d).toLocaleDateString("en-GB",
  { day: "2-digit", month: "short", year: "numeric" }) : "—";
// Units as a reader says them, not as they are stored.
const UNIT_LABEL = { INR_CRORE: "₹ crore", PERCENT: "%", TIMES: "times" };
const unitLabel = (u) => UNIT_LABEL[u] ?? u ?? "";
// A document that states no publication date says so, rather than showing "—".
// A publisher's name as it writes it ("Crisil"), not the key we store it under.
const PUBLISHER = { CRISIL: "Crisil" };
const pub = (name) => PUBLISHER[name] ?? name;
const published = (d) => d ? `published ${day(d)}` : "publication date not stated";
// Internal keys are for debugging, not for readers: ?debug=1 shows them.
const DEBUG = new URLSearchParams(location.search).has("debug");

// A fact is a button, not a styled span: it opens a panel, so it must be
// reachable by Tab, activated by Enter or Space, and announced as a control.
const fact = (claimId, label, cls = "") =>
  `<button type="button" class="fact ${cls}" data-claim="${claimId}"
     aria-label="Show the source evidence for ${String(label).replace(/<[^>]*>/g, "")}"
   >${label}</button>`;

const TABS = [
  ["debt", "Debt"],
  ["ratings", "Ratings"],
  ["conflicts", "Differences"],
  ["changes", "What changed"],
  ["sources", "Sources"],
];
let state = { tab: "debt", data: {} };

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${path}`);
  return r.json();
}

async function load() {
  if (IG.demo) {
    // One curated document, already assembled. Counts come with it and are
    // computed from the snapshot, never written down by hand.
    const o = await get("/api/demo/overview");
    return { issuer: o.issuer, debt: o.debt, ratings: o.ratings, timeline: o.timeline,
             conflicts: o.conflicts, corrob: o.corroborations, changes: o.changes,
             gaps: o.gaps, counts: o.counts, cutoff: o.document_cutoff,
             generated_at: o.generated_at };
  }
  const [issuer, debt, ratings, conflicts, corrob, changes, gaps] = await Promise.all([
    get("/api/issuer"), get("/api/debt"), get("/api/ratings"),
    get("/api/conflicts?include_resolved=true"), get("/api/corroborations"),
    get("/api/changes"), get("/api/corpus-gaps"),
  ]);
  return { issuer, debt, ratings, conflicts, corrob, changes, gaps };
}

async function boot() {
  try {
    state.data = await load();
  } catch (err) {
    $("#view").innerHTML = `<div class="empty">Could not load this dataset.
      <div class="muted" style="margin-top:8px">${esc(err.message)}</div></div>`;
    return;
  }
  const { issuer, conflicts } = state.data;
  // Only open differences are counted. A resolved one is history, not a to-do.
  const open = conflicts.filter(c => c.status === "open").length;
  $("#issuer-name").textContent = issuer.name;
  $("#issuer-sub").textContent =
    `${issuer.documents.length} documents · ${issuer.claim_count} facts · ` +
    `${open} open difference${open === 1 ? "" : "s"} · CIN ${issuer.cin}`;
  const incomplete = issuer.documents.filter(d => d.extraction_status === "incomplete");
  if (incomplete.length) $("#issuer-sub").textContent +=
    ` · ⚠️ ${incomplete.length} document(s) incompletely extracted`;
  $("#tabs").innerHTML = TABS.map(([id, label]) =>
    `<button data-tab="${id}" role="tab" aria-selected="${id === state.tab}"
       aria-controls="view">${label}${id === "conflicts" && open
      ? ` <span class="pill diff">${open}</span>` : ""}</button>`).join("");
  render();
  document.dispatchEvent(new CustomEvent("ig:loaded", { detail: state.data }));
}

function render() {
  document.querySelectorAll("#tabs button").forEach(b => {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", String(on));
  });
  $("#view").innerHTML = ({
    debt: viewDebt, ratings: viewRatings, conflicts: viewConflicts,
    changes: viewChanges, sources: viewSources,
  })[state.tab]();
}

// Agreement within tolerance is not the same claim as an exact match. If the
// sources differ at all, say by how much rather than rendering a bare "agree".
// Agreement is decided by the server over distinct publishers — three
// Brickwork rationales restating one figure are one source, not three.
function agreeBadge(a) {
  const cls = a.kind === "difference" ? "diff" : a.kind === "agree" ? "ok" : "";
  return `<span class="pill ${cls}">${esc(a.label)}</span>`;
}

function debtCell(c) {
  const more = c.statements.length > 1;
  return `<td class="num">${fact(c.claim_id, inr(c.value_numeric))}
    <div class="muted" style="font-size:11px">${esc(pub(c.source_name))}${more
      ? ` · ${c.statements.length} rationales` : ""}${c.changed_within
      ? ` · <span title="This publisher stated different figures; see What changed">restated</span>` : ""}</div>
    ${more ? `<details class="restated"><summary>each rationale</summary>
      ${c.statements.map(st => `<div>${fact(st.claim_id, inr(st.value_numeric))}
        <span class="muted">${esc(st.title)} · ${published(st.published_date)}</span></div>`)
        .join("")}</details>` : ""}</td>`;
}

function viewDebt() {
  const { rows: grouped, instruments } = state.data.debt;
  const rows = grouped.map(r => `<tr>
      <td>${esc(r.basis)}<div class="muted" style="font-size:12px">as at ${day(r.as_of_date)}</div></td>
      ${r.cells.map(debtCell).join("")}
      <td>${agreeBadge(r.agreement)}</td></tr>`).join("");

  const instRows = instruments.map(i => `<tr>
      <td>${fact(i.claim_id, esc(i.instrument_name))}</td>
      <td><span class="pill">${esc(i.instrument_type)}</span></td>
      <td class="num">${inr(i.amount_cr)}</td>
      <td class="num">${day(i.maturity_date)}</td>
      <td class="muted">${esc(pub(i.source_name))}</td></tr>`).join("");

  return `<h2>Total borrowings — every figure is a link to its source</h2>
    <table><thead><tr><th>Basis</th><th colspan="9"></th></tr></thead>
    <tbody>${rows}</tbody></table>
    <h2>Rated instruments with stated maturity (${instruments.length})</h2>
    <table><thead><tr><th>Instrument</th><th>Type</th><th class="num">Amount</th>
      <th class="num">Maturity</th><th>Source</th></tr></thead>
    <tbody>${instRows}</tbody></table>`;
}

// The instrument class, not the agency's wording, is what two ratings are
// compared on — so the table shows it next to the wording it was derived from.
const CLASS_LABEL = {
  ncd: "NCD", subordinated_debt: "sub. debt", perpetual_debt: "perpetual",
  bank_facility: "bank", mld: "MLD", commercial_paper: "CP",
  debt_securities: "debt sec.", other: "other",
};

function inForce(r) {
  if (!r.effective_from) return "";
  if (r.withdrawn) return `withdrawn ${day(r.effective_from)} · no longer rated`;
  if (r.end_basis === "history")
    return `${day(r.effective_from)} → superseded ${day(r.effective_to)}
      (primary document not loaded)`;
  return r.effective_to
    ? `${day(r.effective_from)} → ${day(r.effective_to)}`
    : `${day(r.effective_from)} → current`;
}

function viewRatings() {
  const rows = state.data.ratings.map(r => `<tr>
    <td class="muted">${day(r.action_date)}</td>
    <td>${esc(pub(r.agency))}</td>
    <td>${fact(r.claim_id, esc(r.display_name))}
      <div class="muted" style="font-size:11px">
        ${esc(CLASS_LABEL[r.instrument_class] ?? r.instrument_class)} ·
        ${r.term === "short_term" ? "short-term scale" : "long-term scale"}</div></td>
    <td class="num">${r.rated_amount_cr == null ? "—" : inr(r.rated_amount_cr)}</td>
    <td><b>${esc(r.rating ?? "—")}</b>${r.outlook ? ` <span class="pill">${esc(r.outlook)}</span>` : ""}
        ${r.watch ? ` <span class="pill bad">${esc(r.watch)}</span>` : ""}
        <div class="muted" style="font-size:11px">${inForce(r)}</div></td>
    <td class="muted">${esc(r.action_text)}</td></tr>`).join("");
  return `<h2>Rating actions across agencies</h2>
    <div class="muted" style="font-size:12px;margin:-6px 0 10px">
      A rating stands from its action date until the same agency next acts on the
      same instrument class, or is withdrawn on that tranche. Agencies are compared
      only where those periods overlap.</div>
    <table><thead><tr><th>Date</th><th>Agency</th><th>Instrument</th>
      <th class="num">Rated amount</th><th>Rating</th><th>Action</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function viewConflicts() {
  const open = state.data.conflicts.filter(c => c.status === "open");
  const ended = state.data.conflicts.filter(c => c.status === "ended");
  const resolved = state.data.conflicts.filter(c => c.status === "resolved");

  const STATUS = { open: "Open", ended: "Ended", resolved: "Resolved" };
  // One row per agency document: its value, and each distinct quote with the
  // dates it was stated on and the instruments it covers (listed on demand).
  const quote = (q, many) => `<div class="muted" style="font-size:11px">
      “${esc(q.text)}”${q.dates.length > 1 || many
        ? ` · ${q.dates.map(day).join(", ")}` : ""}
      ${q.instruments.length > 1 ? `<details class="covers"><summary>covers
        ${q.instruments.length} instruments</summary>${q.instruments.map(esc).join("<br>")}</details>`
        : q.instruments.length ? ` · ${esc(q.instruments[0])}` : ""}</div>`;
  const action = a => `<div class="member">
      <span><b>${esc(pub(a.source_name))}</b>
        <span class="muted">· ${esc(a.title)} · ${published(a.published_date)}${
          a.provenance === "history_annexure" ? " · from its rating-history annexure" : ""}${
          a.restated_in > 1 ? ` · latest of ${a.restated_in} rationales` : ""}</span>
        ${a.quotes.map(q => quote(q, a.quotes.length > 1)).join("")}</span>
      <span>${fact(a.claim_id, esc(a.stated_value))}</span>
    </div>`;
  // Per agency: the action in force when the difference began, then its later
  // actions in date order; older or superseded ones behind "history".
  const evidence = c => `<div class="members">${(c.evidence || []).map(sd =>
      sd.actions.map(action).join("") + (sd.history.length
        ? `<details class="history"><summary>${esc(pub(sd.agency))} history
            (${sd.history.length})</summary><div class="members">
            ${sd.history.map(action).join("")}</div></details>` : "")).join("")}</div>`;
  const card = c => `<div class="card conflict${c.status !== "open" ? " resolved" : ""}"
    id="conflict-${c.id}" data-fact-key="${esc(c.fact_key)}">
    <h3><span class="pill ${c.status === "open" ? "diff" : "ok"}">${STATUS[c.status]}</span>
      ${c.material_gap ? `<span class="pill warn">missing document</span>`
        : c.incomplete_corpus ? `<span class="pill">documents missing, no change of view</span>` : ""}
      ${esc(c.subject)}</h3>
    ${c.material_gap ? `<div class="note">Computed over a period in which an agency
      changed its view in an action we hold only from its rating-history annexure,
      not its own rationale. See Sources.</div>`
      : c.incomplete_corpus ? `<div class="muted" style="font-size:12px;margin-bottom:6px">
      Some of this period's actions are known only from rating-history annexures;
      each reaffirmed the view shown. See Sources.</div>` : ""}
    <div class="muted" style="font-size:12px;margin-bottom:6px">
      ${c.status === "resolved"
        ? `no longer detected as of ${day(c.resolved_at)} · first seen ${day(c.first_detected_at)}`
        : c.status === "ended"
        ? `the sources ended it on ${day(c.ended_on)} · first seen ${day(c.first_detected_at)}`
        : `first seen ${day(c.first_detected_at)} · still present ${day(c.last_seen_at)}`}</div>
    <div class="note">${esc(c.note)}</div>
    ${evidence(c)}
    <div class="muted" style="margin-top:8px;font-size:12px">
      IssuerGraph does not choose between these.${DEBUG
        ? ` <code>${esc(c.fact_key)}</code>` : ""}</div>
  </div>`;

  const agree = state.data.corrob.map(c => `<div class="card">
    <h3>${esc(c.subject)}</h3>
    ${Number(c.variance_cr) > 0 ? `<div class="muted" style="font-size:12px;margin-bottom:6px">
      agree within tolerance, residual variance ${inr(c.variance_cr)}
      (${Number(c.spread_pct).toFixed(3)}%)</div>` : ""}
    <div class="members">${c.members.map(m => `<div class="member">
      <span class="muted">${esc(pub(m.source))}</span>
      <span>${fact(m.claim_id, inr(m.value))}</span></div>`).join("")}</div>
  </div>`).join("");

  return `<h2>Open differences (${open.length})</h2>
    ${open.map(card).join("") || '<div class="empty">None.</div>'}
    ${ended.length ? `<h2>Ended (${ended.length})</h2>
      <div class="muted" style="font-size:12px;margin:-6px 0 10px">
        Both views were in force together for a period that has closed — one
        agency has since acted. Historical, not current.</div>
      ${ended.map(card).join("")}` : ""}
    ${resolved.length ? `<h2>Resolved (${resolved.length})</h2>
      <div class="muted" style="font-size:12px;margin:-6px 0 10px">
        Detected previously, not seen in the latest run. Kept because a
        disagreement disappearing is itself a signal.</div>
      ${resolved.map(card).join("")}` : ""}
    <h2>Independently corroborated (${state.data.corrob.length})</h2>
    ${agree || '<div class="empty">None.</div>'}`;
}

function viewChanges() {
  const byPair = {};
  for (const c of state.data.changes)
    (byPair[`${c.agency}|${c.from_date}|${c.to_date}`] ||= []).push(c);

  return Object.entries(byPair).map(([key, items]) => {
    const [agency, from, to] = key.split("|");
    return `<h2>${esc(agency)} · ${day(from)} → ${day(to)}</h2>` + items.map(c => `
      <div class="card"><h3>${esc(c.section)}
        <span class="pill ${c.direction === "removed" ? "bad" : ""}">${c.direction}</span>
        ${c.certainty === "unconfirmed"
          ? '<span class="pill warn" title="The report it is absent from did not meet its coverage declaration">unconfirmed</span>'
          : ""}</h3>
        <div class="chg">
          <div>${c.from_claim_id
            ? fact(c.from_claim_id, esc(c.from_text))
            : `<span class="muted">${c.certainty === "unconfirmed"
                ? "not found in the earlier report (extraction incomplete)"
                : "not stated in the earlier report"}</span>`}</div>
          <div class="arrow" style="background:none;padding-top:8px">→</div>
          <div>${c.to_claim_id
            ? fact(c.to_claim_id, esc(c.to_text))
            : `<span class="muted">${c.certainty === "unconfirmed"
                ? "not found in the later report (extraction incomplete)"
                : "dropped from the later report"}</span>`}</div>
        </div></div>`).join("");
  }).join("") || '<div class="empty">No successive reports to compare.</div>';
}

// Extraction coverage is shown per document because "we did not parse this"
// must never be readable as "the issuer did not report this".
function coverageBadge(d) {
  if (d.extraction_status === "complete")
    return `<span class="pill ok">extraction complete
      ${d.extraction_found}/${d.extraction_expected}</span>`;
  if (d.extraction_status === "incomplete")
    return `<span class="pill bad">extraction incomplete
      ${d.extraction_found}/${d.extraction_expected}</span>`;
  return `<span class="pill">coverage not checked</span>`;
}

// "History lists N actions since <date>; M loaded; gaps: <dates>", with each
// missing date saying whether it changed the agency's view.
function viewHistoryCoverage() {
  const g = state.data.gaps;
  if (!g || !g.agencies.length) return "";
  const missing = g.gaps.reduce((by, x) => ((by[x.agency] ??= []).push(x), by), {});
  const gapDates = a => (a.missing_within_detail || []).map(m => {
    const at = (missing[a.agency] || []).find(x => x.action_date === m.date);
    const label = `${day(m.date)}${m.changes_view ? "" : " (reaffirmation, no change)"}`;
    return at ? fact(at.claim_id, label) : label;
  }).join(", ");
  return `<h2>Rating-history coverage</h2>
    <div class="muted" style="font-size:12px;margin:-6px 0 10px">
      Each agency's rationale lists that agency's own past actions. An action it
      lists that we hold no rationale for is a document we are missing.</div>` +
    g.agencies.map(a => `<div class="card"><h3>${esc(pub(a.agency))}
      ${a.missing_material ? `<span class="pill warn">${a.missing_material} missing, view changed</span>`
        : a.missing_within ? `<span class="pill">${a.missing_within} missing, reaffirmations</span>` : ""}</h3>
      History lists ${a.listed} action${a.listed === 1 ? "" : "s"} since
      ${day(a.listed_since)}; ${a.loaded} loaded;
      ${a.missing_within ? `gaps: ${gapDates(a)}.` : "no gaps in the period our documents cover."}
      ${a.missing_before ? `<div class="muted" style="font-size:12px;margin-top:4px">
        ${a.missing_before} earlier action${a.missing_before === 1 ? "" : "s"} predate
        our earliest ${esc(pub(a.agency))} document.</div>` : ""}
    </div>`).join("");
}

// A web page has no pages; a PDF says how many it has.
const extent = d => d.media_type === "text/html" ? "web page"
  : `${d.page_count} page${d.page_count === 1 ? "" : "s"}`;

function viewSources() {
  return viewHistoryCoverage() +
    `<h2>Retrieved documents</h2>` + state.data.issuer.documents.map(d => `
    <div class="card"><h3>${esc(d.title)} ${coverageBadge(d)}</h3>
      ${(d.extraction_missing || []).length ? `<div class="note">
        Facts this document was expected to yield but did not:
        <ul style="margin:6px 0 0 16px">${d.extraction_missing
          .map(m => `<li>${esc(m)}</li>`).join("")}</ul>
        <div style="margin-top:6px">Absent facts below reflect parsing, not disclosure.</div>
      </div>` : ""}
      <div class="muted" style="font-size:12px">
        ${esc(pub(d.source_name))} · ${esc(d.doc_type.replace(/_/g, " "))} · ${extent(d)} ·
        ${published(d.published_date)}${d.source_updated_on
          ? ` · updated by the publisher ${day(d.source_updated_on)}` : ""} ·
        retrieved ${day(d.retrieved_at)}<br>
        ${d.source_changed_at ? `<div class="note">Changed at source since retrieval
          (noticed ${day(d.source_changed_at)}). The stored copy, with the hash below,
          remains the evidence of record.</div>` : ""}
        <code>sha256 ${esc(d.sha256)}</code><br>
        <a href="${esc(d.url)}" target="_blank" rel="noopener">original URL</a>${
          IG.demo ? "" : ` · <a href="/api/pdf/${d.id}" target="_blank">stored copy</a>`}
      </div></div>`).join("");
}

async function showClaim(claimId) {
  document.querySelectorAll(".fact.sel").forEach(e => e.classList.remove("sel"));
  document.querySelectorAll(`.fact[data-claim="${claimId}"]`)
    .forEach(e => e.classList.add("sel"));

  let c;
  try {
    c = await get(IG.demo ? `/api/demo/claim/${claimId}` : `/api/claim/${claimId}`);
  } catch (err) {
    $("#evidence").innerHTML =
      `<div class="empty">Evidence unavailable: ${esc(err.message)}</div>`;
    return;
  }
  const primary = c.anchors[0];
  // quote_and_link: the publisher's terms restrict redistribution, so the
  // server sends a short quote and a link to their page — never a page image,
  // an iframe or their HTML. Everything else shows the highlighted page.
  const quoteOnly = c.evidence_policy === "quote_and_link";
  const value = c.value_numeric != null
    ? `${c.value_unit === "INR_CRORE" ? inr(c.value_numeric)
        : `${Number(c.value_numeric).toLocaleString("en-IN")} ${esc(unitLabel(c.value_unit))}`}
       <span class="muted">(${esc(unitLabel(c.value_unit))})</span>`
    : esc(c.value_text);

  // The demo ships the page as a plain image and draws the highlights over it
  // from the stored rectangles; the live product renders them into the PNG.
  // Both use the same geometry the pipeline recorded — neither re-searches the
  // page, which is what would let a highlight land on the wrong occurrence.
  const pageKey = `${c.document_id}-${primary.page_no}`;
  const page = IG.demo ? (c.pages || {})[pageKey] : null;
  const rects = c.anchors
    .filter(a => a.page_no === primary.page_no)
    .flatMap(a => a.bbox_rects || []);

  const pageView = quoteOnly
    ? `<div class="note">${esc(c.policy_note)}
        <div style="margin-top:6px"><a href="${esc(c.source_link)}" target="_blank"
          rel="noopener noreferrer">Open the quoted passage</a> ·
        <a href="${esc(c.plain_url)}" target="_blank" rel="noopener noreferrer">open the page</a>
        <span class="muted">(if your browser does not jump to the passage)</span></div></div>`
    : IG.demo
    ? (page ? `<div class="pagewrap" id="pagewrap">
        <img class="pageimg" id="pageimg" alt="Page ${primary.page_no} of ${esc(c.title)}"
             src="/demo/${page.image}">
        ${rects.map(r => `<span class="hl" style="
            left:${(r[0] / page.width) * 100}%; top:${(r[1] / page.height) * 100}%;
            width:${((r[2] - r[0]) / page.width) * 100}%;
            height:${((r[3] - r[1]) / page.height) * 100}%"></span>`).join("")}
      </div>` : `<div class="empty">No page image in this snapshot.</div>`)
    : `<img class="pageimg" id="pageimg" alt="Page ${primary.page_no} of ${esc(c.title)}"
           src="/api/page.png?document_id=${c.document_id}&page_no=${primary.page_no}&claim_id=${c.id}">`;

  const pdfLink = IG.demo || quoteOnly ? "" :
    ` · <a href="/api/pdf/${c.document_id}#page=${primary.page_no}" target="_blank">open PDF</a>`;

  $("#evidence").innerHTML = `
    <div style="font-size:20px;font-weight:600;margin-bottom:2px">${value}</div>
    <div class="meta">${esc(c.subject)}</div>
    <div class="meta">
      ${c.panel_lines.map((line, i) => i === 0 ? `<b>${esc(line)}</b>` : esc(line)).join("<br>")}
      ${DEBUG && primary.kind === "html" ? `<br><code>${esc(primary.node_path)}</code>` : ""}
      ${quoteOnly ? "" : `<br><a href="${esc(c.source_link ?? c.url)}" target="_blank"
         rel="noopener noreferrer">source URL</a>${pdfLink}`}
    </div>
    ${c.anchors.map(a => `<blockquote>${esc(a.evidence_text)}${a.truncated
      ? ` <span class="muted">(quote shortened)</span>` : ""}</blockquote>`).join("")}
    ${pageView}`;

  // Announce for screen readers, and tell the host page (the demo tour and the
  // analytics shim listen for this).
  document.dispatchEvent(new CustomEvent("ig:evidence", {
    detail: { claim_id: c.id, claim_type: c.claim_type, source: c.source_name,
              page_no: primary.page_no, anchors: c.anchors.length,
              policy: c.evidence_policy },
  }));

  const img = $("#pageimg");
  if (!img) return;
  const scrollToHighlight = () => {
    const rect = rects[0];
    if (!rect) return;
    const host = IG.demo ? $("#pagewrap") : img;
    const scale = img.clientWidth / (IG.demo ? page.width : img.naturalWidth / 2.0);
    const top = host.offsetTop + rect[1] * scale - 120;
    $(".right").scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  };
  if (img.complete) scrollToHighlight(); else img.onload = scrollToHighlight;
}

function selectTab(tab) {
  if (!tab || tab === state.tab) return;
  state.tab = tab;
  render();
  document.dispatchEvent(new CustomEvent("ig:tab", { detail: { tab } }));
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest("#tabs button");
  if (tab) { selectTab(tab.dataset.tab); return; }
  const f = e.target.closest(".fact");
  if (f) showClaim(f.dataset.claim);
});

// Left/right arrows move between tabs, which is what a tablist is expected to do.
document.addEventListener("keydown", (e) => {
  if (!e.target.closest("#tabs")) return;
  const order = TABS.map(([id]) => id);
  const at = order.indexOf(state.tab);
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    const next = order[(at + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length];
    selectTab(next);
    document.querySelector(`#tabs button[data-tab="${next}"]`).focus();
  }
});

window.IGApp = { showClaim, selectTab, state };

boot();
