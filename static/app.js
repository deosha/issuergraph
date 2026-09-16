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

// A fact is a button, not a styled span: it opens a panel, so it must be
// reachable by Tab, activated by Enter or Space, and announced as a control.
const fact = (claimId, label, cls = "") =>
  `<button type="button" class="fact ${cls}" data-claim="${claimId}"
     aria-label="Show the source evidence for ${String(label).replace(/<[^>]*>/g, "")}"
   >${label}</button>`;

const TABS = [
  ["debt", "Debt"],
  ["ratings", "Ratings"],
  ["conflicts", "Conflicts"],
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
             counts: o.counts, cutoff: o.document_cutoff, generated_at: o.generated_at };
  }
  const [issuer, debt, ratings, conflicts, corrob, changes] = await Promise.all([
    get("/api/issuer"), get("/api/debt"), get("/api/ratings"),
    get("/api/conflicts?include_resolved=true"), get("/api/corroborations"),
    get("/api/changes"),
  ]);
  return { issuer, debt, ratings, conflicts, corrob, changes };
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
  // Only open conflicts are counted. A resolved one is history, not a to-do.
  const open = conflicts.filter(c => c.status === "open").length;
  $("#issuer-name").textContent = issuer.name;
  $("#issuer-sub").textContent =
    `${issuer.documents.length} documents · ${issuer.claim_count} anchored claims · ` +
    `${open} open conflicts · CIN ${issuer.cin}`;
  const incomplete = issuer.documents.filter(d => d.extraction_status === "incomplete");
  if (incomplete.length) $("#issuer-sub").textContent +=
    ` · ⚠️ ${incomplete.length} document(s) incompletely extracted`;
  $("#tabs").innerHTML = TABS.map(([id, label]) =>
    `<button data-tab="${id}" role="tab" aria-selected="${id === state.tab}"
       aria-controls="view">${label}${id === "conflicts" && open
      ? ` <span class="pill bad">${open}</span>` : ""}</button>`).join("");
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
function agreeBadge(items) {
  const values = items.map(i => Number(i.value_numeric));
  const gap = Math.max(...values) - Math.min(...values);
  const label = `${items.length} sources agree`;
  return gap === 0
    ? `<span class="pill ok">${label}</span>`
    : `<span class="pill ok">${label} ±${inr(gap)}</span>`;
}

function viewDebt() {
  const { totals, instruments } = state.data.debt;
  const groups = {};
  for (const t of totals) (groups[`${t.basis}|${t.as_of_date}`] ||= []).push(t);

  const rows = Object.entries(groups).sort().reverse().map(([key, items]) => {
    const [basis, asOf] = key.split("|");
    const conflicted = items.find(i => i.conflict);
    return `<tr>
      <td>${esc(basis)}<div class="muted" style="font-size:12px">as at ${day(asOf)}</div></td>
      ${items.map(i => `<td class="num">${fact(i.claim_id, inr(i.value_numeric))}
        <div class="muted" style="font-size:11px">${esc(i.source_name)}</div></td>`).join("")}
      <td>${conflicted
        ? `<span class="pill bad">conflict ${Number(conflicted.conflict.spread_pct).toFixed(3)}%</span>`
        : items.length > 1 ? agreeBadge(items)
        : `<span class="pill">single source</span>`}</td></tr>`;
  }).join("");

  const instRows = instruments.map(i => `<tr>
      <td>${fact(i.claim_id, esc(i.instrument_name))}</td>
      <td><span class="pill">${esc(i.instrument_type)}</span></td>
      <td class="num">${inr(i.amount_cr)}</td>
      <td class="num">${day(i.maturity_date)}</td>
      <td class="muted">${esc(i.source_name)}</td></tr>`).join("");

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
  return r.effective_to
    ? `${day(r.effective_from)} → ${day(r.effective_to)}`
    : `${day(r.effective_from)} → current`;
}

function viewRatings() {
  const rows = state.data.ratings.map(r => `<tr>
    <td class="muted">${day(r.action_date)}</td>
    <td>${esc(r.agency)}</td>
    <td>${fact(r.claim_id, esc(r.instrument))}
      <div class="muted" style="font-size:11px">
        ${esc(CLASS_LABEL[r.instrument_class] ?? r.instrument_class)} ·
        ${r.term === "short_term" ? "short-term scale" : "long-term scale"}</div></td>
    <td class="num">${r.rated_amount_cr == null ? "—" : inr(r.rated_amount_cr)}</td>
    <td><b>${esc(r.rating)}</b>${r.outlook ? ` <span class="pill">${esc(r.outlook)}</span>` : ""}
        ${r.watch ? ` <span class="pill bad">${esc(r.watch)}</span>` : ""}
        <div class="muted" style="font-size:11px">${inForce(r)}</div></td>
    <td class="muted">${esc(r.action ?? "")}</td></tr>`).join("");
  return `<h2>Rating actions across agencies</h2>
    <div class="muted" style="font-size:12px;margin:-6px 0 10px">
      A rating stands from its action date until the same agency next acts on the
      same instrument class. Agencies are compared only where those periods overlap.</div>
    <table><thead><tr><th>Date</th><th>Agency</th><th>Instrument</th>
      <th class="num">Rated amount</th><th>Rating</th><th>Action</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function viewConflicts() {
  const open = state.data.conflicts.filter(c => c.status === "open");
  const resolved = state.data.conflicts.filter(c => c.status === "resolved");

  const card = c => `<div class="card conflict${c.status === "resolved" ? " resolved" : ""}"
    id="conflict-${c.id}">
    <h3>${c.status === "resolved" ? "✔️" : "⚠️"} ${esc(c.subject)}</h3>
    <div class="muted" style="font-size:12px;margin-bottom:6px">
      ${c.status === "resolved"
        ? `no longer detected as of ${day(c.resolved_at)} · first seen ${day(c.first_detected_at)}`
        : `first seen ${day(c.first_detected_at)} · still present ${day(c.last_seen_at)}`}</div>
    <div class="note">${esc(c.note)}</div>
    <div class="members">${c.members.map(m => `<div class="member">
      <span><b>${esc(m.source_name)}</b>
        <span class="muted">· ${esc(m.title)} · ${day(m.published_date)}</span>
        ${m.stated_value ? `<div class="muted" style="font-size:11px">
          quoting: ${esc(m.value_text ?? "")}</div>` : ""}</span>
      <span>${fact(m.claim_id, m.stated_value ? esc(m.stated_value)
        : (m.value_numeric != null ? inr(m.value_numeric) : esc(m.value_text)))}</span>
      </div>`).join("")}</div>
    <div class="muted" style="margin-top:8px;font-size:12px">
      IssuerGraph does not choose between these. <code>${esc(c.fact_key)}</code></div>
  </div>`;

  const agree = state.data.corrob.map(c => `<div class="card">
    <h3>${esc(c.subject)}</h3>
    ${Number(c.variance_cr) > 0 ? `<div class="muted" style="font-size:12px;margin-bottom:6px">
      agree within tolerance, residual variance ${inr(c.variance_cr)}
      (${Number(c.spread_pct).toFixed(3)}%)</div>` : ""}
    <div class="members">${c.members.map(m => `<div class="member">
      <span class="muted">${esc(m.source)}</span>
      <span>${fact(m.claim_id, inr(m.value))}</span></div>`).join("")}</div>
  </div>`).join("");

  return `<h2>Open conflicts (${open.length})</h2>
    ${open.map(card).join("") || '<div class="empty">None.</div>'}
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
        <span class="pill ${c.direction === "removed" ? "bad" : ""}">${c.direction}</span></h3>
        <div class="chg">
          <div>${c.from_claim_id
            ? fact(c.from_claim_id, esc(c.from_text))
            : '<span class="muted">not stated in the earlier report</span>'}</div>
          <div class="arrow" style="background:none;padding-top:8px">→</div>
          <div>${c.to_claim_id
            ? fact(c.to_claim_id, esc(c.to_text))
            : '<span class="muted">dropped from the later report</span>'}</div>
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

function viewSources() {
  return `<h2>Retrieved documents</h2>` + state.data.issuer.documents.map(d => `
    <div class="card"><h3>${esc(d.title)} ${coverageBadge(d)}</h3>
      ${(d.extraction_missing || []).length ? `<div class="note">
        Facts this document was expected to yield but did not:
        <ul style="margin:6px 0 0 16px">${d.extraction_missing
          .map(m => `<li>${esc(m)}</li>`).join("")}</ul>
        <div style="margin-top:6px">Absent facts below reflect parsing, not disclosure.</div>
      </div>` : ""}
      <div class="muted" style="font-size:12px">
        ${esc(d.source_name)} · ${esc(d.doc_type)} · ${d.page_count} pages ·
        published ${day(d.published_date)} · retrieved ${day(d.retrieved_at)}<br>
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
  const value = c.value_numeric != null
    ? `${inr(c.value_numeric)} <span class="muted">(${esc(c.value_unit ?? "")})</span>`
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

  const pageView = IG.demo
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

  const pdfLink = IG.demo ? "" :
    ` · <a href="/api/pdf/${c.document_id}#page=${primary.page_no}" target="_blank">open PDF</a>`;

  $("#evidence").innerHTML = `
    <div style="font-size:20px;font-weight:600;margin-bottom:2px">${value}</div>
    <div class="meta">${esc(c.subject)}</div>
    <div class="meta">
      <b>${esc(c.title)}</b> · ${esc(c.source_name)} · published ${day(c.published_date)}<br>
      Page ${primary.page_no} of ${c.page_count} ·
      chars ${primary.char_start}–${primary.char_end} ·
      basis ${esc(c.basis)} · as at ${day(c.as_of_date)}<br>
      <code>${esc(c.extractor)} v${esc(c.extractor_version)}</code> ·
      <code>sha256 ${esc(c.sha256).slice(0, 16)}…</code> ·
      retrieved ${day(c.retrieved_at)}<br>
      <a href="${esc(c.url)}" target="_blank" rel="noopener">source URL</a>${pdfLink}
    </div>
    ${c.anchors.map(a => `<blockquote>${esc(a.evidence_text)}</blockquote>`).join("")}
    ${pageView}`;

  // Announce for screen readers, and tell the host page (the demo tour and the
  // analytics shim listen for this).
  document.dispatchEvent(new CustomEvent("ig:evidence", {
    detail: { claim_id: c.id, claim_type: c.claim_type, source: c.source_name,
              page_no: primary.page_no, anchors: c.anchors.length },
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
