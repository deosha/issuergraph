/* IssuerGraph Assess — analyst workspace (vanilla JS, no build).
 *
 * Everything shown comes from GET /api/assess/cases/:id, which is computed on
 * the server from stored extractions + analyst input. This file never does
 * arithmetic on figures: after any change it re-fetches the case, so the
 * screen, the review list and the Excel export always agree.
 */
(function () {
  "use strict";
  const API = "/api/assess";
  // Public demo: the same workspace over a static snapshot of synthetic cases.
  const DEMO = !!(window.IGA && window.IGA.demo);
  const DEMO_BASE = "/static/demo/assess/";
  const BASE = DEMO ? "/demo/assess" : "/assess";
  const READ_ONLY = "Read-only public demo with synthetic data. Corrections, decisions and uploads work in a pilot.";
  let demoSnap = null;
  async function demoApi(path) {
    if (!demoSnap) demoSnap = await (await fetch(DEMO_BASE + "snapshot.json")).json();
    let m;
    if (path === "/cases") return Object.values(demoSnap.cases).map((c) => ({ id: c.case.id, label: c.case.label,
      is_synthetic: true, borrower_type: c.case.borrower_type }));
    if ((m = path.match(/^\/cases\/(\d+)$/))) return demoSnap.cases[m[1]];
    if ((m = path.match(/^\/fact\/(\d+)$/))) return demoSnap.facts[m[1]];
    if ((m = path.match(/^\/txn\/(\d+)$/))) return demoSnap.txns[m[1]];
    if ((m = path.match(/^\/json\/(\d+)\?path=(.*)$/))) return demoSnap.json[`${m[1]}|${decodeURIComponent(m[2])}`];
    if (path === "/status") return null;
    throw new Error(READ_ONLY);
  }
  // A page image: the server renders highlights live; the demo draws them over a
  // pre-rendered page from the stored rectangles, as percentages of the page.
  function pageImage(docId, pageNo, rects, alt) {
    if (!DEMO) return null;
    const pg = demoSnap.pages[`${docId}|${pageNo}`];
    if (!pg) return `<p class="note">Page image not included in the demo.</p>`;
    const hl = (rects || []).map(([x0, y0, x1, y1]) => `<span class="hl" style="left:${x0 / pg.width * 100}%;top:${y0 / pg.height * 100}%;
      width:${(x1 - x0) / pg.width * 100}%;height:${(y1 - y0) / pg.height * 100}%"></span>`).join("");
    return `<div class="pagewrap"><img class="ev-page" alt="${esc(alt)}" src="${DEMO_BASE + pg.file}">${hl}</div>`;
  }
  const exportUrl = (id) => (DEMO ? `${DEMO_BASE}case_${id}.xlsx` : `${API}/cases/${id}/export.xlsx`);
  const state = { cases: [], caseId: null, data: null, tab: "overview", sel: null, provider: null,
                  txnFilter: "all", hideDup: true, poll: null };
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const inr0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const money = (v) => (v === null || v === undefined || v === "" ? "" :
    (Number(v) < 0 ? "−₹" : "₹") + inr.format(Math.abs(Number(v))));
  const money0 = (v) => (v === null || v === undefined ? "" :
    (Number(v) < 0 ? "−₹" : "₹") + inr0.format(Math.abs(Number(v))));
  const ym = (k) => { const d = new Date(k + "-01T00:00:00");
    return isNaN(d) ? esc(k) : d.toLocaleDateString("en-GB", { month: "short", year: "2-digit" }); };
  const fmtDate = (s) => {
    if (!s) return "";
    const d = new Date(s.length === 10 ? s + "T00:00:00" : s);
    return isNaN(d) ? esc(s) : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  };
  const fmtWhen = (s) => (s ? new Date(s).toLocaleString("en-GB", { day: "2-digit", month: "short",
    year: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
  const label = (k) => (k || "").replace(/_/g, " ");
  const KIND = { salary_slip: "Salary slip", bank_statement: "Bank statement", credit_report: "Credit report",
                 form16_part_b: "Form 16 Part B", form16_part_a: "Form 16 Part A", itr_json: "ITR (JSON)",
                 itr_ack: "ITR acknowledgement", tax_statement: "AIS / 26AS", od_sanction: "OD sanction letter",
                 loan_sanction: "Loan sanction (other)", kyc: "KYC / identity",
                 gst_certificate: "GST certificate", udyam_certificate: "Udyam certificate",
                 financials: "P&L / balance sheet", itr_computation: "ITR computation",
                 business_proof: "Business proof", loan_soa: "Loan SOA", other: "Other" };
  const ORIGIN = { reported: "Reported", unverified: "Unverified", analyst: "Analyst", inferred: "Inferred",
                   calculated: "Calculated", suggested: "Suggested" };

  async function api(path, opts = {}) {
    if (DEMO) {
      if (opts.method && opts.method !== "GET") throw new Error(READ_ONLY);
      return demoApi(path);
    }
    const res = await fetch(API + path, opts);
    if (!res.ok) {
      let msg = res.status + "";
      try { msg = (await res.json()).detail || msg; } catch (_) { /* not JSON */ }
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return res.json();
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(body) });

  /* ---------- value chips ---------- */

  function fmtVal(v, kind, field) {
    if (v === null || v === undefined) return "";
    if (kind === "amount") return inr.format(Number(v));
    if (kind === "date" && field === "pay_month") {
      const d = new Date(String(v) + "T00:00:00");
      return isNaN(d) ? esc(v) : d.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    }
    if (kind === "date") return fmtDate(String(v));
    return esc(v);
  }

  // A fact view → clickable chip. Absent values say so; they never render as 0.
  function chip(view, opts = {}) {
    if (!view) return `<span class="absent">${esc(opts.absent || "not reported")}</span>`;
    const cls = view.status + (view.kind === "text" ? " text" : "");
    const body = view.reported_blank ? `<span class="v blank ${cls}" data-fact="${view.id}" title="The document prints no value here — not zero">blank (reported)</span>`
      : `<span class="v ${cls}" data-fact="${view.id}" title="${esc(ORIGIN[view.status])} — click for source">${
        view.value === null ? "—" : fmtVal(view.value, view.kind, view.field)}</span>`;
    const edit = opts.edit === false ? "" :
      `<button class="edit" data-action="correct-fact" data-key="${esc(view.key)}" data-kind="${view.kind}"
        data-current="${esc(view.value ?? "")}" title="Correct this value">✎</button>`;
    return `<span class="cell">${body}${edit}</span>`;
  }
  const calc = (text) => `<span class="v calculated">${text}</span>`;
  // A calculated figure that opens the transactions it was computed from.
  const src = (text, list, title) => (list && list.length
    ? `<span class="v calculated link" data-action="txns" data-ids="${esc(JSON.stringify(list))}" data-title="${esc(title)}" title="Show the ${list.length} transaction(s) behind this figure">${text}</span>`
    : calc(text));
  // An average that opens its arithmetic and its months.
  const avg = (text, acct, what) => `<span class="v calculated link" data-action="average" data-acct="${esc(acct)}"
    data-what="${what}" title="Show how this average is calculated">${text}</span>`;
  // A calculated figure that opens the extracted facts it was computed from.
  const fsrc = (text, facts, title) => (facts && facts.length
    ? `<span class="v calculated link" data-action="facts" data-ids="${esc(JSON.stringify(facts.map((f) => f.id)))}" data-title="${esc(title)}" title="Show the figures this is computed from">${text}</span>`
    : calc(text));
  const inferred = (text, attrs = "") => `<span class="v inferred" ${attrs}>${text}</span>`;

  function statusPill(s) {
    const cls = { complete: "ok", incomplete: "warn", failed: "bad", unsupported: "bad", processing: "info",
                  received: "info", identified: "info" }[s] || "";
    return `<span class="pill ${cls}">${esc(s)}</span>`;
  }

  /* ---------- load ---------- */

  async function loadProvider() {
    try { state.provider = await api("/status"); } catch (_) { state.provider = null; }
  }

  async function loadCases(selectId) {
    state.cases = await api("/cases");
    const sel = $("#case-select");
    sel.innerHTML = state.cases.map((c) =>
      `<option value="${c.id}">#${c.id} · ${esc(c.label)}${c.is_synthetic ? " (synthetic)" : ""}</option>`).join("");
    const fromUrl = Number((location.pathname.match(/\/(\d+)$/) || [])[1]) || null;
    const id = selectId || fromUrl || (state.cases[0] && state.cases[0].id);
    if (id) {
      sel.value = id;
      await openCase(id);
    } else {
      $("#view").innerHTML = `<div class="card"><h2>No case yet</h2><p>Create a case, then add documents.</p></div>`;
    }
  }

  async function openCase(id) {
    state.caseId = Number(id);
    history.replaceState(null, "", `${BASE}/${id}${location.hash}`);
    $("#export").href = exportUrl(id);
    await refresh();
  }

  async function refresh() {
    state.data = await api(`/cases/${state.caseId}`);
    render();
    const busy = state.data.documents.some((d) => d.status === "processing" || d.status === "received");
    clearTimeout(state.poll);
    if (busy) state.poll = setTimeout(refresh, 2000);
  }

  /* ---------- render ---------- */

  const TABS = [["overview", "Overview"], ["documents", "Documents"], ["income", "Income"],
                ["banking", "Banking"], ["liabilities", "Liabilities"], ["eligibility", "Eligibility"],
                ["review", "Review & Export"]];
  const GROUPS = [["missing", "Missing information"], ["discrepancy", "Possible discrepancy"],
                  ["confirm", "Needs confirmation"]];
  const NA = `<span class="absent">Not available</span>`;

  function render() {
    const c = state.data;
    const k = c.case;
    const h = c.headline;
    const cov = h.coverage;
    const f = h.findings;
    const openAll = f.missing + f.discrepancy + f.confirm;
    $("#case-head").innerHTML = `<div class="ch-main"><h1>${esc(k.applicant_name || k.label)}</h1>
      <span class="kv">Borrower <b>${esc(label(k.borrower_type))}</b></span>
      <span class="kv">Requested <b>${esc(k.requested_product || "not supplied")}${
        k.requested_amount ? " · " + money0(k.requested_amount) : ""}</b></span>
      <span class="kv">Case <b>#${k.id}</b></span></div>
      <div class="ch-status">
        <span class="pill ${cov.ok === cov.of ? "ok" : "warn"}" title="Checklist items received">Document coverage ${cov.ok}/${cov.of}</span>
        <span class="pill info">${cov.read}/${cov.documents} documents read${cov.incomplete ? ` · ${cov.incomplete} incomplete` : ""}${cov.not_read ? ` · ${cov.not_read} not read` : ""}</span>
        <span class="pill ${openAll ? "warn" : "ok"}">${openAll ? `Review: ${openAll} open finding${openAll === 1 ? "" : "s"}` : "Review: no open findings"}</span>
        <span class="pill">${esc({ computed: "Worksheet computed", provisional: "Worksheet provisional",
          needs_input: "Worksheet needs input", invalid: "Worksheet needs review" }[c.eligibility.status] || "")}</span>
      </div>`;
    const banner = $("#banner");
    if (DEMO) {
      banner.hidden = false;
      banner.className = "banner synthetic";
      banner.innerHTML = `<b>Public demo · synthetic data · read-only.</b> Every document and figure is fictional. Click any
        figure to see its source page; corrections, decisions and uploads are shown but work only in a pilot.
        <a href="/pilot?from=assess-demo">Discuss an assessment pilot →</a>`;
    } else if (k.is_synthetic) {
      banner.hidden = false;
      banner.className = "banner synthetic";
      banner.textContent = "Synthetic case: every document and figure here is fictional, generated for "
        + "testing." + (c.credit.reports.length ? " Its credit report is read from recorded fixture responses, not a live model call." : "");
    } else {
      banner.hidden = true;
    }
    const open = openAll;
    const biz = k.borrower_type !== "salaried";
    $("#tabs").innerHTML = TABS.map(([id, name]) => [id, id === "banking" && biz ? "Business & Banking" : name])
      .map(([id, name]) => [id, id === "income" && biz ? "Business income" : name])
      .map(([id, name]) => `<button role="tab" data-tab="${id}"
      class="${state.tab === id ? "on" : ""}">${name}${id === "review" && open ? `<span class="count">${open}</span>` : ""}</button>`).join("");
    const view = { overview, documents, income, banking: bankingAnalysis, liabilities, eligibility, review }[state.tab](c);
    $("#view").innerHTML = view;
    if (state.sel) document.querySelectorAll(`[data-fact="${state.sel}"]`).forEach((e) => e.classList.add("sel"));
  }

  /* documents */
  function documents(c) { return documentsTable(c) + banking(c); }

  function documentsTable(c) {
    const docs = c.documents;
    const rows = docs.map((d) => {
      const period = d.period_start ? `${fmtDate(d.period_start)} – ${fmtDate(d.period_end)}` : "";
      const usage = d.model_calls ? `${esc(d.model_id)} · ${d.model_calls} call(s) · ${d.input_tokens}/${d.output_tokens} tokens`
        : d.model_id ? esc(d.model_id) : "";
      return `<tr>
        <td><b>${esc(d.filename)}</b><div class="note">${(d.byte_size / 1024).toFixed(0)} KB · ${d.page_count || 0} page(s)${
          d.ocr_pages ? " · " + d.ocr_pages + " via OCR" : ""}</div></td>
        <td><select class="kind" data-action="set-kind" data-doc="${d.id}">${(KIND[d.kind] ? "" :
          `<option value="${esc(d.kind)}" selected>${esc(label(d.kind))}</option>`) + Object.entries(KIND).map(([v, n]) =>
          `<option value="${v}" ${v === d.kind ? "selected" : ""}>${n}</option>`).join("")}</select>
          ${d.kind_basis ? `<div class="note">${esc(d.kind_basis)}</div>` : ""}</td>
        <td>${esc(d.source_label || "")}</td>
        <td style="white-space:nowrap">${period || fmtDate(d.doc_date) || (d.summary && d.summary.assessment_year) || ""}</td>
        <td>${statusPill(d.status)}${(d.status_reasons || []).length ? `<div class="reasons">${
          d.status_reasons.map(esc).join("<br>")}</div>` : ""}
          <div class="note">${d.processed_at ? "Processed " + fmtWhen(d.processed_at) : "Not processed"}${
          d.runs > 1 ? ` · ${d.runs} runs` : ""}${usage ? " · " + usage : ""}
          · <button class="link" data-action="reprocess" data-doc="${d.id}">re-run</button></div></td></tr>`;
    }).join("");
    const cl = c.checklist.rows.map((r) => `<tr><td>${esc(r.item)}</td><td>${esc(r.received || "")}</td>
      <td>${r.missing ? `<span class="pill warn">missing</span> ${esc(r.missing)}` : `<span class="pill ok">received</span>`}</td></tr>`).join("");
    const byArea = {};
    c.review.filter((r) => !r.resolved).forEach((r) => {
      byArea[r.area] = byArea[r.area] || { n: 0, b: 0 };
      byArea[r.area].n++; if (r.blocking) byArea[r.area].b++;
    });
    const e = c.eligibility;
    return `
      <div class="card"><h2>Documents</h2>
        <p class="sub">Stored privately on this machine. A status is only “complete” when every declared
        field was read and verified; results are cached and reopen without repeating model or OCR calls.</p>
        <div class="tbl-wrap"><table><thead><tr><th>File</th><th>Type</th><th>Source</th><th>Period / date</th>
        <th>Extraction</th></tr></thead><tbody>${rows ||
          `<tr><td colspan="5" class="empty">No documents yet.</td></tr>`}</tbody></table></div>
        <div class="upload" style="margin-top:12px">
          <input type="file" id="files" multiple accept=".pdf,.json">
          <select id="upload-kind"><option value="">Identify from content</option>${Object.entries(KIND).filter(([v]) => v !== "other")
            .map(([v, n]) => `<option value="${v}">${n}</option>`).join("")}</select>
          <button class="primary small" data-action="upload">Add & process</button>
          <span class="note" id="upload-msg"></span>
        </div>
        <p class="note" style="margin-top:8px">${state.provider ? (state.provider.available
          ? `External processing on: model <b>${esc(state.provider.model)}</b> (${esc(state.provider.region)}) for credit reports, sanction letters and slips the parser cannot read${state.provider.textract ? "; Textract OCR for pages without text" : ""}.`
          : `Model / OCR: ${esc(state.provider.reason)} Bank statements, salary slips and ITR JSON are still read locally; credit reports will fail visibly.`) : ""}</p>
      </div>
      <div class="grid2">
        <div class="card"><h2>Received and missing</h2><p class="sub">${esc(c.checklist.note)}</p>
          <table><tbody>${cl}</tbody></table></div>
      </div>`;
  }

  /* income */
  function income(c) {
    if (c.case.borrower_type !== "salaried") return businessIncome(c);
    const inc = c.income;
    const slips = inc.slips.map((s) => `<tr>
      <td>${s.fields.pay_month ? chip(s.fields.pay_month, { edit: false }) : `<span class="absent">month not read</span>`}</td>
      <td>${chip(s.fields.employer, { absent: "not read" })}</td>
      <td class="num">${chip(s.fields.gross_pay)}</td>
      <td class="num">${chip(s.fields.total_deductions)}</td>
      <td class="num">${chip(s.fields.net_pay)}</td>
      <td>${s.arith ? (s.arith.ok ? `<span class="pill ok">adds up</span>` :
        `<span class="pill warn">differs by ${money(s.arith.difference)}</span>`) : ""}</td>
      <td>${statusPill(s.status)}</td></tr>`).join("");
    const cmp = inc.comparison.map((r) => `<tr>
      <td>${esc(r.label)}</td>
      <td class="num">${r.slip_net !== null ? money(r.slip_net) : `<span class="absent">no slip</span>`}</td>
      <td class="num">${r.bank_credits.length ? r.bank_credits.map((b) =>
        `<span class="v reported" data-txn="${b.id}">${inr.format(b.amount)}</span> <span class="note">${fmtDate(b.date)}</span>`).join("<br>")
        : `<span class="absent">none identified</span>`}</td>
      <td class="num">${r.difference !== null ? calc(money(r.difference)) : ""}</td>
      <td><span class="pill ${r.level === "ok" ? "ok" : r.level === "review" ? "warn" : "info"}">${
        r.level === "ok" ? "consistent" : r.level === "review" ? "review" : "note"}</span> ${esc(r.note)}</td></tr>`).join("");
    const f16 = (inc.form16 || []).map((x) => {
      const f = x.fields;
      const row = (k, n) => `<tr><td>${n}</td><td class="num">${chip(f[k], { absent: "not found" })}</td></tr>`;
      return `<div class="card"><h2>Form 16 Part B ${f.assessment_year ? esc(f.assessment_year.value) : ""} ${statusPill(x.status)}</h2>
        <p class="sub">Employer-certified figures for the financial year before the assessment year. Read by label; no model.</p>
        <table><tbody>${row("gross_salary", "Gross salary (total, s.17)")}${row("income_from_salary", "Income chargeable under “Salaries”")}
        ${row("gross_total_income", "Gross total income")}${row("total_taxable_income", "Total taxable income")}
        ${row("net_tax_payable", "Net tax payable")}</tbody></table></div>`;
    }).join("");
    const itr = inc.itr.map((i) => {
      const f = i.fields;
      const row = (k, n) => `<tr><td>${n}</td><td class="num">${chip(f[k], { edit: false, absent: "not in JSON" })}</td></tr>`;
      return `<div class="card"><h2>Income-tax return ${f.assessment_year ? esc(f.assessment_year.value) : ""}</h2>
        <p class="sub">${esc((i.summary || {}).filed_status || "")}. Parsed directly from the JSON; no model.</p>
        <table><tbody>${row("gross_salary", "Gross salary (as per s.17)")}${row("income_from_salary", "Income from salary (after s.16 deductions)")}
        ${row("gross_total_income", "Gross total income")}${row("total_income", "Total (taxable) income")}
        ${row("tax_payable", "Tax payable")}</tbody></table></div>`;
    }).join("");
    return `
      <div class="card"><h2>Salary slips</h2>
        <p class="sub">Gross pay, deductions and net pay as printed on each slip.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Month</th><th>Employer</th><th class="num">Gross</th>
        <th class="num">Deductions</th><th class="num">Net pay</th><th>Check</th><th>Extraction</th></tr></thead>
        <tbody>${slips || `<tr><td colspan="7" class="empty">No salary slips.</td></tr>`}</tbody></table></div></div>
      <div class="card"><h2>Net pay vs salary credits</h2>
        <p class="sub">Matching periods only. ${esc(inc.salary_credit_rule)} A difference is something to
        explain, not automatically an error.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Month</th><th class="num">Slip net pay</th><th class="num">Bank salary credit</th>
        <th class="num">Difference</th><th>Assessment</th></tr></thead><tbody>${cmp ||
          `<tr><td colspan="5" class="empty">Nothing to compare.</td></tr>`}</tbody></table></div></div>
      <div class="grid2">${f16}${itr || (f16 ? "" : `<div class="card"><h2>Income-tax return</h2><p class="empty">No ITR or Form 16 received.</p></div>`)}
        <div class="card"><h2>Annual figures</h2>
          ${inc.annualised_gross ? `<p><b>${fsrc(money0(inc.annualised_gross.value), inc.slips.slice(-3).map((x) => x.fields.gross_pay).filter(Boolean), "Gross pay used for the annualised figure")}</b> annualised gross</p>
          <div class="callout">${esc(inc.annualised_gross.basis)}</div>` : `<p class="empty">No verified slips to annualise.</p>`}
          <p class="note">The ITR covers a past financial year; slips cover recent months. Compare them as
          different periods.</p>
          ${inc.suggested_income ? `<p style="margin-top:10px">Suggested eligible income:
            ${fsrc(money(inc.suggested_income.value), inc.slips.slice(-3).map((x) => x.fields.net_pay).filter(Boolean), "Net pay averaged for the suggestion")}<br><span class="note">${esc(inc.suggested_income.basis)}</span></p>` : ""}
        </div></div>`;
  }

  // A proprietor has no salary: income is declared (ITR, financials) and banked.
  function businessIncome(c) {
    const docs = c.documents;
    const has = (...k) => docs.filter((d) => k.includes(d.kind));
    const row = (name, list, note) => `<tr><td>${name}</td><td>${list.length
      ? list.map((d) => `${esc(d.filename)} ${statusPill(d.status)}`).join("<br>") : `<span class="pill warn">not received</span>`}</td>
      <td class="note">${note}</td></tr>`;
    const ca = c.banking.analysis.find((a) => a.account_type === "current") || c.banking.analysis[0];
    return `<div class="card"><h2>Business income</h2>
        <p class="sub">A proprietor has no salary slip. Income is what the ITR and financial statements declare, checked
        against what the business account banks.</p>
        <table><thead><tr><th>Declared income</th><th>Received</th><th></th></tr></thead><tbody>
        ${row("ITR — last 2 years", has("itr_ack", "itr_json"), "Filed return or acknowledgement")}
        ${row("ITR computation", has("itr_computation"), "Business income, depreciation, deductions")}
        ${row("P&L and balance sheet", has("financials"), "Turnover, gross / net profit, liabilities")}
        </tbody></table>
        <p class="note">Reading the ITR computation and financial statements, and comparing their turnover with banking and
        GST, is pilot scope: it needs sample documents from your cases.</p></div>
      <div class="card"><h2>Banked turnover</h2>
        ${ca ? `<div class="stats">
          <div class="stat"><div class="k">Business credits / month</div><div class="val">${ca.avg_monthly_business_credits !== null
            ? avg(money0(ca.avg_monthly_business_credits), ca.account_key, "credits") : NA}</div>
            <div class="note">${esc(ca.account_key.replace("|", " "))} (${esc(ca.account_type || "type not stated")}), ${ca.month_count} months</div></div>
          <div class="stat"><div class="k">Business credits, total</div><div class="val">${src(money0(ca.business_credits), ca.business_ids,
            "Business credits")}</div><div class="note">Excludes returns and own-account transfers</div></div>
          <div class="stat"><div class="k">Average monthly balance</div><div class="val">${ca.amb !== null ? avg(money0(ca.amb), ca.account_key, "amb") : NA}</div></div>
        </div><p class="note"><button class="link" data-tab="banking">Full banking analysis →</button></p>`
          : `<p class="empty">No bank statement processed.</p>`}</div>`;
  }

  /* business & banking */
  const ids = (list) => esc(JSON.stringify(list || []));
  function tile(name, value, list, note, title, help) {
    const click = list && list.length ? ` data-action="txns" data-ids="${ids(list)}" data-title="${esc(title || name)}"` : "";
    return `<div class="stat${click ? " click" : ""}"${click}${help ? ` title="${help}"` : ""}><div class="k">${name}</div>
      <div class="val">${value}</div>${note ? `<div class="note">${note}</div>` : ""}</div>`;
  }
  const pct = (v) => (v === null || v === undefined ? NA : calc((v * 100).toFixed(0) + "%"));

  function bankingAnalysis(c) {
    const b = c.business;
    let html = "";
    if (c.case.borrower_type !== "salaried") {
      const v = b.vintage;
      const regs = b.registrations.map((r) => {
        const f = r.fields;
        const row = (k, n) => f[k] ? `<tr><td>${n}</td><td>${chip(f[k], { edit: false })}</td></tr>` : "";
        return `<div><h3 class="sm">${r.kind === "gst_certificate" ? "GST registration" : "Udyam registration"} ${statusPill(r.status)}</h3>
          <table><tbody>${row("registration_number", "Registration no.")}${row("constitution", "Constitution")}
          ${row("registration_type", "Type")}${row("enterprise_type", "Enterprise type")}${row("major_activity", "Major activity")}
          ${row("liability_date", "Date of liability")}${row("validity_from", "Valid from")}
          ${row("incorporation_date", "Incorporated / registered")}${row("commencement_date", "Commenced operations")}
          ${row("udyam_date", "Udyam registration")}</tbody></table></div>`;
      }).join("");
      html += `<div class="card"><h2>Business profile</h2>
        <p class="sub">Read from the registration certificates supplied. Nature of business, stability and customer
        dependence are for the analyst to judge from these and the banking below.</p>
        <div class="stats">
          <div class="stat"><div class="k">Business vintage</div><div class="val">${v.years !== null ? calc(v.years + " yrs") : NA}</div>
            <div class="note">${v.fact ? `since ${chip(v.fact, { edit: false })} — ${esc(v.basis)}` : "No registration date read"}</div>
            <div class="note">Checklist guide: ${v.guide_years} years; lender criteria vary.</div></div>
          <div class="stat"><div class="k">Current account</div><div class="val" style="font-size:13px">${b.has_current_account
            ? `<span class="pill ok">statement received</span>` : `<span class="pill warn">not received</span>`}</div>
            <div class="note">Account types: ${b.account_types.map(esc).join(", ") || "—"}</div></div>
        </div>
        <div class="grid2">${regs || `<p class="empty">No GST or Udyam certificate received.</p>`}</div></div>`;
    }
    for (const a of c.banking.analysis) {
      const name = a.account_key.replace("|", " ");
      const top = a.concentration.top;
      const months = a.months.map((m) => { const L = ym(m.month); return `<tr><td>${L}</td>
        <td class="num">${m.amb !== null ? src(money0(m.amb), m.ids.balance, `Balances, ${L}`) : NA}${m.days && m.days < 20 ? `<div class="note">${m.days} days</div>` : ""}</td>
        <td class="num">${src(money0(m.business_credits), m.ids.business, `Business credits, ${L}`)}<div class="note">${m.credit_n} credits</div></td>
        <td class="num">${src(money0(m.debits), m.ids.debit, `Debits, ${L}`)}<div class="note">${m.debit_n} debits</div></td>
        <td class="num">${m.cash_dep ? src(money0(m.cash_dep), m.ids.cash_dep, `Cash deposits, ${L}`) : "—"}</td>
        <td class="num">${m.cash_wdl ? src(money0(m.cash_wdl), m.ids.cash_wdl, `Cash withdrawals, ${L}`) : "—"}</td>
        <td class="num">${m.returns ? src(String(m.returns), m.ids.return, `Returns, ${L}`) : "—"}</td>
        <td class="num">${m.min_balance !== null ? src(money0(m.min_balance), m.ids.balance, `Balances, ${L}`) : NA}</td></tr>`; }).join("");
      html += `<div class="card"><h2>${esc(name)} <span class="pill">${esc(a.account_type || "type not stated")}</span></h2>
        <p class="sub">${fmtDate(a.from)} – ${fmtDate(a.to)} · ${a.month_count} month(s) · overlapping statements de-duplicated.
        Click a figure to list the transactions behind it.</p>
        <div class="stats">
          ${tile("Average monthly balance", a.amb !== null ? avg(money0(a.amb), a.account_key, "amb") : NA, null, "End-of-day average ⓘ", null, esc(a.amb_method))}
          ${tile(a.account_type === "savings" ? "Credits / month" : "Business credits / month", a.avg_monthly_business_credits !== null ? avg(money0(a.avg_monthly_business_credits), a.account_key, "credits") : NA,
            null, `Total ${src(money0(a.business_credits), a.business_ids, `Business credits — ${name}`)} — excludes returns and own-account transfers`)}
          ${tile("Cash deposits", calc(money0(a.cash_deposits.amount)), a.cash_deposits.ids,
            `${a.cash_deposits.count} entries · ${a.cash_deposits.share !== null ? (a.cash_deposits.share * 100).toFixed(0) + "% of business credits" : ""}`)}
          ${tile("Cash withdrawals", calc(money0(a.cash_withdrawals.amount)), a.cash_withdrawals.ids, `${a.cash_withdrawals.count} entries`)}
          ${tile("Returns / bounces", a.returns.count ? `<span class="pill bad">${a.returns.count}</span>` : calc("0"), a.returns.ids,
            "Cheque / ECS / NACH returns and their charges")}
          ${tile("Days below zero", calc(String(a.negative_days)), a.negative_ids, a.balances_available ? "From end-of-day balances" : "Balances not available", `Rows with a balance below zero — ${name}`)}
          ${tile("Moved out within 2 days", pct(a.retention.share), a.retention.ids, `${a.retention.quick_out} of ${a.retention.large_credits} large credits ⓘ`, null, esc(a.retention.rule))}
          ${tile("Largest single payer", top[0] ? pct(top[0].share) : NA, top[0] ? top[0].ids : null,
            top[0] ? esc(top[0].party) + " — share of non-cash business credits" : "", top[0] ? top[0].party : "")}
        </div>
        <div class="grid2">
          <div><h3 class="sm">Monthly pattern</h3><div class="tbl-wrap"><table><thead><tr><th>Month</th><th class="num">AMB</th>
            <th class="num">Business credits</th><th class="num">Debits</th><th class="num">Cash in</th><th class="num">Cash out</th>
            <th class="num">Returns</th><th class="num">Min balance</th></tr></thead><tbody>${months}</tbody></table></div></div>
          <div><h3 class="sm">Who pays in <span class="note">(inferred from narrations)</span></h3>
            <table><tbody>${top.map((t) => `<tr class="click" data-action="txns" data-ids="${ids(t.ids)}" data-title="${esc(t.party)}">
              <td>${esc(t.party)}</td><td class="num">${pct(t.share)}</td><td class="num">${calc(money0(t.amount))}</td>
              <td class="num note">${t.count}×</td></tr>`).join("") || `<tr><td class="empty">—</td></tr>`}</tbody></table>
            <p class="note">${esc(a.concentration.basis)}</p>
            ${a.returns.rows.length ? `<h3 class="sm">Returns and bounce entries</h3><table><tbody>${a.returns.rows.map((r) =>
              `<tr class="click" data-txn="${r.id}"><td>${fmtDate(r.date)}</td><td class="nar">${esc(r.narration)}</td>
              <td class="num">${money(r.amount)}</td></tr>`).join("")}</tbody></table>` : ""}</div>
        </div>
        <p class="note">Findings use review prompts, not lender rules: one payer ≥ ${(a.review_thresholds.concentration_share * 100).toFixed(0)}%,
        cash ≥ ${(a.review_thresholds.cash_share * 100).toFixed(0)}% of business credits, ≥ ${(a.review_thresholds.quick_out_share * 100).toFixed(0)}% of large credits moved out within 2 days.</p></div>`;
    }
    return html || `<div class="card"><p class="empty">No statements processed.</p></div>`;
  }

  // An average: show the arithmetic, then each month, each opening its rows.
  function showAverage(acctKey, what) {
    const a = state.data.banking.analysis.find((x) => x.account_key === acctKey);
    const name = acctKey.replace("|", " ");
    let rows, formula, title;
    if (what === "credits") {
      title = `Business credits per month — ${name}`;
      rows = a.months.filter((m) => m.credit_n || m.days).map((m) => ({ label: ym(m.month), value: m.business_credits,
        ids: m.ids.business, note: `${m.ids.business.length} credit(s)` }));
      formula = `${money(a.business_credits)} business credits ÷ ${a.month_count} month(s) = <b>${money(a.avg_monthly_business_credits)}</b>`;
    } else {
      title = `Average monthly balance — ${name}`;
      const used = a.months.filter((m) => m.amb !== null && m.days >= 20);
      rows = a.months.filter((m) => m.amb !== null).map((m) => ({ label: ym(m.month), value: m.amb, ids: m.ids.balance,
        note: `average of ${m.days} end-of-day balance(s)${m.days < 20 ? " — not used (fewer than 20 days)" : ""}` }));
      formula = `Average of ${used.length} monthly figure(s) = <b>${money(a.amb)}</b>. Each month is the average of its
        end-of-day balances; a day without a transaction carries the previous balance forward.`;
    }
    $("#evidence").innerHTML = `<div class="ev-meta"><b>${esc(title)}</b></div>
      <p class="calcline">${formula}</p>
      <p class="note">Click a month to list its transactions; each opens its row on the statement page.</p>
      <table><tbody>${rows.map((r) => `<tr class="click" data-action="txns" data-ids="${esc(JSON.stringify(r.ids))}"
        data-title="${esc(title + " — " + r.label)}"><td>${r.label}</td><td class="num">${money(r.value)}</td>
        <td class="note">${esc(r.note)}</td></tr>`).join("")}</tbody></table>`;
  }

  function showObligations() {
    const ob = state.data.obligations;
    const rows = ob.rows.filter((r) => r.included);
    $("#evidence").innerHTML = `<div class="ev-meta"><b>Included monthly obligations</b> · ${rows.length} row(s) · total ${money(ob.total_included)}</div>
      <p class="note">Each amount opens its source: the credit-report line, the matched bank debits, or the analyst's entry and reason.</p>
      <table><tbody>${rows.map((r) => {
        const v = r.origin === "reported" && r.facts.emi ? `<span class="v reported" data-fact="${r.facts.emi}">${money(r.amount)}</span>`
          : r.origin === "inferred" && r.observed ? src(money(r.amount), r.observed.txn_ids, `Bank debits — ${r.label}`)
          : `<span class="v analyst" title="${esc(r.basis || "")}">${money(r.amount)}</span>`;
        return `<tr><td>${esc(r.label)}<div class="note">${esc(r.basis || "")}</div></td><td class="num">${v}</td></tr>`;
      }).join("") || `<tr><td class="empty">No obligations included yet.</td></tr>`}</tbody></table>
      ${ob.unresolved ? `<p class="note">${ob.unresolved} row(s) awaiting a decision are not in this total.</p>` : ""}`;
  }

  function showFactList(list, title) {
    const all = {};
    const c = state.data;
    for (const g of [...c.income.slips.map((x) => x.fields), ...c.credit.reports.map((r) => r.fields),
      ...c.credit.tradelines.map((t) => t.fields), ...c.banking.statements.map((x) => x.fields),
      ...c.income.itr.map((i) => i.fields), ...(c.income.form16 || []).map((f) => f.fields)]) {
      for (const v of Object.values(g)) all[v.id] = v;
    }
    const rows = list.map((i) => all[i]).filter(Boolean);
    $("#evidence").innerHTML = `<div class="ev-meta"><b>${esc(title)}</b> · ${rows.length} figure(s)</div>
      <p class="note">Each figure opens the page it was read from.</p>
      <table><tbody>${rows.map((v) => `<tr><td>${esc(label(v.field))}</td><td class="num">${chip(v, { edit: false })}</td>
        <td class="note">p.${v.page_no ?? "—"}</td></tr>`).join("")}</tbody></table>`;
  }

  function showTxnList(list, title) {
    const byId = Object.fromEntries(state.data.banking.transactions.map((t) => [t.id, t]));
    const rows = list.map((i) => byId[i]).filter(Boolean);
    const total = (k) => rows.reduce((s_, t) => s_ + (t[k] || 0), 0);
    $("#evidence").innerHTML = `<div class="ev-meta"><b>${esc(title)}</b></div>
      <p class="calcline">${rows.length} transaction(s)${total("credit") ? ` · credits total <b>${money(total("credit"))}</b>` : ""}${total("debit") ? ` · debits total <b>${money(total("debit"))}</b>` : ""}</p>
      <p class="note">Click a row to open it on the statement page.</p>
      <table><tbody>${rows.map((t) => `<tr class="click" data-txn="${t.id}"><td>${fmtDate(t.txn_date)}</td>
        <td class="nar">${esc(t.narration)}</td><td class="num">${t.debit !== null ? "−" + money(t.debit) : money(t.credit)}</td></tr>`).join("")}</tbody></table>`;
  }

  /* liabilities */
  function liabilities(c) {
    return impactPanel(c) + credit(c) + banking(c, "series");
  }

  // The worksheet, next to the schedule, with what the last edit changed.
  function impactPanel(c) {
    const e = c.eligibility;
    const prev = state.prev;
    const cur = snapshot(c);
    const cell = (k, name, fmt) => {
      const now = cur[k];
      const was = prev ? prev[k] : undefined;
      const changed = prev && was !== now;
      return `<div class="stat ${changed ? "changed" : ""}"><div class="k">${name}</div>
        <div class="val">${now === null || now === undefined ? NA : calc(fmt(now))}</div>
        ${changed ? `<div class="note">was ${was === null || was === undefined ? "not available" : fmt(was)}</div>` : ""}</div>`;
    };
    return `<div class="card impact"><h2>Eligibility worksheet impact <span class="pill">${esc(label(e.status))}</span></h2>
      <p class="sub">Recalculated after every decision below. ${esc(e.label)}</p>
      <div class="result">
        ${cell("obligations", "Included monthly obligations", (v) => `<span class="v calculated link" data-action="obligations" title="Show what makes up this total">${money(v)}</span>`)}
        ${cell("existing_ratio_pct", "Obligation-to-income", (v) => v.toFixed(2) + "%")}
        ${cell("capacity_emi", "Additional EMI capacity", money)}
        ${cell("principal", "Indicative principal", money0)}
      </div></div>`;
  }

  function snapshot(c) {
    const e = c.eligibility;
    return { obligations: c.obligations.total_included, existing_ratio_pct: e.existing_ratio_pct,
             capacity_emi: e.capacity_emi, principal: e.principal };
  }

  function credit(c) {
    const reps = c.credit.reports.map((r) => {
      const f = r.fields;
      const score = f.score ? chip(f.score, { edit: false }) : f.score_code
        ? `${chip(f.score_code, { edit: false })} <span class="pill info">no score</span>`
        : `<span class="absent">score not found</span>`;
      return `<div class="card"><h2>Credit report ${statusPill(r.status)}</h2>
        <p class="sub"><span class="pill violet">Applicant-supplied</span> Read from the PDF supplied by the
        applicant. Not independently verified, and not a live bureau pull.</p>
        <div class="stats"><div class="stat"><div class="k">Bureau</div><div class="val" style="font-size:13px">${chip(f.bureau, { edit: false })}</div></div>
        <div class="stat"><div class="k">Report date</div><div class="val">${chip(f.report_date, { edit: false })}</div></div>
        <div class="stat"><div class="k">Score</div><div class="val">${score}</div></div>
        <div class="stat"><div class="k">Accounts read</div><div class="val">${c.credit.tradelines.filter((t) => t.document_id === r.document_id).length}</div></div></div></div>`;
    }).join("");
    const tl = c.credit.tradelines.map((t) => {
      const f = t.fields;
      return `<tr class="${t.closed ? "dim" : ""}">
        <td><b>${chip(f.lender, { edit: false })}</b><div class="note">${esc(t.account_type || "")} · ${chip(f.account_number, { edit: false })}
          · ${esc(t.ownership_role || "role not read")}</div></td>
        <td>${(!f.status || f.status.reported_blank) && f.section ? chip(f.section, { edit: false })
          : chip(f.status, { edit: false, absent: "—" })}${t.closed ? ` <span class="pill">closed</span>` : ""}
          <div class="note">reported ${chip(f.last_reported, { edit: false })}</div></td>
        <td class="num">${t.kind === "loan" ? chip(f.sanctioned_amount) : chip(f.credit_limit && !f.credit_limit.reported_blank ? f.credit_limit : f.sanctioned_amount)}</td>
        <td class="num">${chip(f.current_balance)}</td>
        <td class="num">${chip(f.emi)}</td>
        <td class="num">${chip(f.overdue)}</td>
        <td class="dpd">${f.payment_history ? chip(f.payment_history, { edit: false }) : `<span class="absent">—</span>`}</td></tr>`;
    }).join("");
    const ob = c.obligations;
    const obRows = ob.rows.map((r) => {
      const obs = r.observed ? `${inferred(money(r.observed.amount), `data-action="txns" data-ids="${esc(JSON.stringify(r.observed.txn_ids || r.txn_ids || []))}" data-title="Bank debits — ${esc(r.label)}"`)}<div class="note">${r.observed.fixed ? "fixed" : "varies"} · ${
        r.observed.months.length} month(s)${r.observed.match_status ? " · " + esc(r.observed.match_status) : ""}</div>` : `<span class="absent">none matched</span>`;
      const amt = r.included ? `<span class="v ${r.origin === "reported" ? "reported" : r.origin === "inferred" ? "inferred" : "analyst"} calc-like">${money(r.amount)}</span>`
        : r.unresolved ? `<span class="pill warn">needs decision</span>` : `<span class="pill">excluded</span>`;
      return `<tr class="${r.unresolved ? "warnrow" : r.included ? "" : "dim"}">
        <td><b>${esc(r.label)}</b>${r.ownership ? ` <span class="note">· ${esc(r.ownership)}</span>` : ""}
          <div class="note">${esc(r.unresolved || r.basis || "")}</div></td>
        <td class="num">${r.outstanding !== null ? `<span class="v reported" data-fact="${r.facts.current_balance}">${money(r.outstanding)}</span>` : NA}</td>
        <td class="num">${r.bureau_emi !== null ? `<span class="v reported" data-fact="${r.facts.emi}">${money(r.bureau_emi)}</span>`
          : r.facts.emi ? `<span class="v reported blank" data-fact="${r.facts.emi}">${esc(r.bureau_emi_state)}</span>` : `<span class="absent">${esc(r.bureau_emi_state)}</span>`}</td>
        <td class="num">${obs}</td>
        <td class="num">${amt}</td>
        <td><button class="small" data-action="treat" data-key="${esc(r.key)}">${r.unresolved ? "Decide" : "Change"}</button></td></tr>`;
    }).join("");
    const tls = Object.fromEntries(c.credit.tradelines.map((t) => [t.key, t]));
    const series = Object.fromEntries(c.banking.series.map((s) => [s.key, s]));
    const matches = c.matches.map((m) => {
      const s = series[m.series];
      const cls = { confirmed: "ok", rejected: "", proposed: "violet", ambiguous: "warn" }[m.status];
      return `<tr><td>${esc(tls[m.tradeline].label)}</td>
        <td>${esc(s.signature)}<div class="note">${s.count} debit(s) · typical ${money(s.amount)}</div></td>
        <td>${esc(m.explanation)}</td>
        <td><span class="pill ${cls}">${esc(m.status)}</span>${m.reason ? `<div class="note">${esc(m.reason)}</div>` : ""}</td>
        <td style="white-space:nowrap"><button class="small" data-action="decide" data-key="${esc(m.key)}" data-status="confirmed">Confirm</button>
        <button class="small" data-action="decide" data-key="${esc(m.key)}" data-status="rejected">Reject</button></td></tr>`;
    }).join("");
    const od = c.od.map((o) => `<div class="card"><h2>Overdraft — ${esc(o.lender || "")}</h2>
      <div class="callout rule">${esc(o.rule)}</div>
      <div class="stats">
        <div class="stat"><div class="k">Sanctioned limit</div><div class="val">${o.sanctioned_limit !== null ? money(o.sanctioned_limit) : "—"}</div></div>
        <div class="stat"><div class="k">Outstanding</div><div class="val">${chip(o.fields.current_balance, { edit: false })}</div></div>
        <div class="stat"><div class="k">Drawing power</div><div class="val" style="font-size:13px">${o.drawing_power !== null ? money(o.drawing_power)
          : `<span class="absent">${esc(o.drawing_power_state)}</span>`}</div></div>
        <div class="stat"><div class="k">Last reported</div><div class="val">${chip(o.fields.last_reported, { edit: false })}</div></div>
      </div>
      <p>${o.interest_series ? `Observed interest/servicing debits: ${inferred(money(o.interest_series.min))} – ${inferred(money(o.interest_series.max))}
        (avg ${money(o.interest_series.average)}) in ${o.interest_series.months.map(esc).join(", ")} — ${esc(o.interest_match || "")} match.`
        : "No interest or servicing debits identified in the statements received."}</p>
      <p><b>Monthly obligation used:</b> ${o.obligation.included ? `<span class="v analyst">${money(o.obligation.amount)}</span> — ${esc(o.obligation.basis)}`
        : o.obligation.unresolved ? `<span class="pill warn">not set</span> Enter an analyst assumption with its basis (e.g. observed average interest, or a stated policy) — or exclude it with a reason.`
        : `<span class="pill">excluded</span> ${esc(o.obligation.basis || "")}`}
        <button class="small" data-action="treat" data-key="${esc(o.key)}">Set OD assumption</button></p>
      ${o.sanction_documents.length ? "" : `<p class="note">No sanction letter received: terms and drawing power are unknown, not zero.</p>`}
    </div>`).join("");
    return `${reps || `<div class="card"><h2>Credit report</h2><p class="empty">No credit report received.</p></div>`}
      <div class="card"><h2>Liability schedule</h2>
        <p class="sub">One row per obligation. A bureau account and the bank debit that repays it are one row, never two.
        Unresolved rows are left out of the total until decided.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Obligation</th><th class="num">Outstanding</th><th class="num">Bureau EMI</th>
        <th class="num">Observed in bank</th><th class="num">Monthly used</th><th></th></tr></thead>
        <tbody>${obRows}</tbody><tfoot><tr><td colspan="4" class="num"><b>Total included monthly obligations</b></td>
        <td class="num"><b><span class="v calculated link" data-action="obligations" title="Show what makes up this total">${money(ob.total_included)}</span></b></td><td class="note">${ob.unresolved} awaiting decision</td></tr></tfoot></table></div></div>
      <div class="card"><h2>Bureau ↔ bank matches</h2>
        <p class="sub">Inferred from lender names, account digits, amounts and monthly recurrence. Confirm or reject; a rejected
        match stops the bank debit from standing in for the bureau EMI.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Bureau account</th><th>Bank debits</th><th>Why</th><th>Status</th><th></th></tr></thead>
        <tbody>${matches || `<tr><td colspan="5" class="empty">No candidate matches.</td></tr>`}</tbody></table></div></div>
      ${od}
      <div class="card"><h2>Accounts in the credit report</h2>
        <p class="sub">“blank (reported)” means the report prints no value — it is not zero. Amber values were proposed by the model but not found in the report.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Lender / account</th><th>Status</th>
        <th class="num">Sanctioned / limit</th><th class="num">Outstanding</th><th class="num">EMI</th><th class="num">Overdue</th>
        <th>Payment history</th></tr></thead><tbody>${tl || `<tr><td colspan="7" class="empty">No accounts.</td></tr>`}</tbody></table></div></div>`;
  }

  /* banking */
  function banking(c, part) {
    const b = c.banking;
    const sts = b.statements.map((s) => {
      const m = s.summary || {};
      const recon = m.recon_ok === true ? `<span class="pill ok">balances</span>` : m.recon_ok === false
        ? `<span class="pill bad">does not balance (${money(m.recon_difference)})</span>` : `<span class="pill warn">not checked</span>`;
      const breaks = (m.running_breaks || []).length;
      return `<div class="card"><h2>${esc(s.bank || "Bank")} ${esc(s.account || "")} · ${fmtDate(s.period_start)} – ${fmtDate(s.period_end)} ${statusPill(s.status)}</h2>
        <p class="sub">${esc(s.filename)}</p>
        <div class="stats">
          <div class="stat"><div class="k">Opening</div><div class="val">${s.fields.opening_balance ? chip(s.fields.opening_balance, { edit: false }) : money(m.opening_balance)}</div>
            ${m.opening_basis !== "reported" ? `<div class="note">${esc(m.opening_basis || "")}</div>` : ""}</div>
          <div class="stat"><div class="k">Credits (${m.credit_count ?? 0})</div><div class="val">${src(money(m.total_credits),
            c.banking.transactions.filter((t) => t.document_id === s.document_id && t.credit !== null).map((t) => t.id), `Credits — ${s.filename}`)}</div></div>
          <div class="stat"><div class="k">Debits (${m.debit_count ?? 0})</div><div class="val">${src(money(m.total_debits),
            c.banking.transactions.filter((t) => t.document_id === s.document_id && t.debit !== null).map((t) => t.id), `Debits — ${s.filename}`)}</div></div>
          <div class="stat"><div class="k">Closing</div><div class="val">${s.fields.closing_balance ? chip(s.fields.closing_balance, { edit: false }) : money(m.closing_balance)}</div></div>
        </div>
        <p>Opening + credits − debits = closing: ${recon} · Running balance: ${m.running_balance_rows ? (breaks
          ? `<span class="pill bad">${breaks} break(s)</span>` : `<span class="pill ok">consistent on ${m.running_balance_rows} rows</span>`) : `<span class="pill warn">no balance column</span>`}
          · ${m.rows} rows read${s.duplicates ? ` · ${s.duplicates} repeated in an earlier statement` : ""}</p>
        ${(m.issues || []).length ? `<div class="callout">${m.issues.map(esc).join("<br>")}</div>` : ""}
        <p class="note">Reconciling shows the rows were read consistently. It is not proof the statement is authentic.</p></div>`;
    }).join("");
    const accts = b.accounts.map((a) => `<tr><td>${esc(a.bank)} ${esc(a.account)}</td><td>${fmtDate(a.from)} – ${fmtDate(a.to)}</td>
      <td class="num">${a.unique_rows}</td><td class="num">${a.duplicate_rows}</td>
      <td>${a.gaps.length ? a.gaps.map((g) => `<span class="pill warn">gap</span> ${fmtDate(g.from)} – ${fmtDate(g.to)}`).join("<br>") : `<span class="pill ok">none</span>`}</td>
      <td>${a.overlaps.map((o) => `${fmtDate(o.from)} – ${fmtDate(o.to)}`).join("<br>") || "—"}</td></tr>`).join("");
    const series = b.series.map((s) => `<tr><td>${esc(s.signature)}<div class="note">${esc(s.narrations[0] || "")}</div></td>
      <td>${esc(label(s.category))}</td><td class="num">${inferred(money(s.amount), `data-action="txns" data-ids="${esc(JSON.stringify(s.txn_ids))}" data-title="${esc(s.signature)}"`)}${s.fixed_amount ? "" : `<div class="note">varies ${money(s.min)}–${money(s.max)}</div>`}</td>
      <td>${s.months.map(ym).join(", ")}</td><td class="num">${s.count}</td></tr>`).join("");
    const cats = ["salary", "loan_repayment", "card_payment", "od_interest", "other"];
    let txns = b.transactions;
    if (state.hideDup) txns = txns.filter((t) => !t.duplicate);
    if (state.txnFilter !== "all") txns = txns.filter((t) => t.category === state.txnFilter);
    const trs = txns.map((t) => `<tr class="click ${t.duplicate ? "dim" : ""}" data-txn="${t.id}">
      <td>${fmtDate(t.txn_date)}</td><td class="nar">${esc(t.narration)}</td>
      <td class="num">${t.debit !== null ? money(t.debit) : ""}</td><td class="num">${t.credit !== null ? money(t.credit) : ""}</td>
      <td class="num">${t.balance !== null ? money(t.balance) : ""}</td>
      <td><select data-action="set-cat" data-key="${esc(t.key)}" data-current="${t.category}">${cats.map((x) =>
        `<option value="${x}" ${x === t.category ? "selected" : ""}>${label(x)}</option>`).join("")}</select>
        <div class="note">${t.category_origin === "analyst" ? "analyst" : t.category !== "other" ? "inferred" : ""}${t.duplicate ? " · duplicate (overlap)" : ""}</div></td></tr>`).join("");
    if (part === "series") return `<div class="card"><h2>Candidate servicing debits</h2><p class="sub">Recurring debits that read as loan EMIs, card payments or OD interest — inferred, for matching to the credit report.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Debit</th><th>Reads as</th><th class="num">Typical</th><th>Months</th><th class="num">Count</th></tr></thead>
        <tbody>${series || `<tr><td colspan="5" class="empty">None identified.</td></tr>`}</tbody></table></div></div>`;
    return `${sts || `<div class="card"><h2>Bank statements</h2><p class="empty">No statements processed.</p></div>`}
      <div class="card"><h2>Coverage by account</h2><p class="sub">Overlapping statements are de-duplicated: a row repeated in
        a later statement is shown dimmed and counted once.</p>
        <table><thead><tr><th>Account</th><th>Covered</th><th class="num">Unique rows</th><th class="num">Duplicates</th><th>Missing periods</th><th>Overlaps</th></tr></thead>
        <tbody>${accts || `<tr><td colspan="6" class="empty">—</td></tr>`}</tbody></table></div>
      <div class="card"><h2>Transactions</h2>
        <div class="filters"><label>Show <select id="txn-filter"><option value="all">all</option>${cats.map((x) =>
          `<option value="${x}" ${state.txnFilter === x ? "selected" : ""}>${label(x)}</option>`).join("")}</select></label>
          <label><input type="checkbox" id="hide-dup" ${state.hideDup ? "checked" : ""}> hide duplicates</label>
          <span class="note">${txns.length} row(s). Click a row to see it on the statement page; change a category to reclassify (reason required).</span></div>
        <div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Narration</th><th class="num">Debit</th><th class="num">Credit</th>
        <th class="num">Balance</th><th>Category</th></tr></thead><tbody>${trs}</tbody></table></div></div>`;
  }

  /* eligibility */
  function eligibility(c) {
    const e = c.eligibility;
    const ins = e.inputs;
    const val = (x) => (x && x.value !== null && x.value !== undefined ? x.value : "");
    const statusText = { computed: ["ok", "Computed"], provisional: ["warn", "Provisional"], needs_input: ["warn", "Needs input"],
                         invalid: ["bad", "Needs review — invalid input"] }[e.status] || ["", e.status];
    const row = (key, name, x, step, hint) => `<label for="in-${key}">${name}</label>
      <input type="number" id="in-${key}" step="${step}" value="${esc(val(x))}">
      <input type="text" id="basis-${key}" placeholder="Basis (required)" value="${esc(x && x.origin !== "suggested" ? x.basis || "" : "")}">
      ${x && x.basis ? `<span></span><span class="note" style="grid-column: span 2">${x.origin === "suggested" ? "" : "Current basis: "}${esc(x.basis)}</span>` : hint ? `<span></span><span class="note" style="grid-column: span 2">${hint}</span>` : ""}`;
    return `<div class="disclaimer">${esc(e.label)}</div>
      <div class="card"><h2>Inputs</h2>
        <p class="sub">Every input is an analyst entry with a stated basis. There are no built-in lender thresholds.</p>
        <div class="form-grid">
          ${row("eligible_income", "Eligible monthly income (₹)", ins.eligible_income, "0.01")}
          <label>Included monthly obligations (₹)</label><span class="mono"><span class="v calculated link" data-action="obligations" title="Show what makes up this total">${money(ins.obligations.value)}</span></span>
          <span class="note">From the liability schedule${ins.obligations.unresolved_rows ? ` — ${ins.obligations.unresolved_rows} row(s) unresolved, not in this total` : ""}.
            <button class="link" data-tab="credit">Edit schedule</button></span>
          ${row("foir_max_pct", "Illustrative max obligation-to-income (%)", ins.foir_max_pct, "0.1", "An illustrative policy value, not any lender's threshold.")}
          ${row("annual_rate_pct", "Annual interest rate (%)", ins.annual_rate_pct, "0.01")}
          ${row("tenure_months", "Tenure (months)", ins.tenure_months, "1")}
        </div>
        <p style="margin-top:12px"><button class="primary" data-action="save-assumptions">Save inputs & recalculate</button>
        <span class="note" id="elig-msg"></span></p>
      </div>
      <div class="card"><h2>Result <span class="pill ${statusText[0]} status-big">${statusText[1]}</span></h2>
        ${e.errors.length ? `<div class="callout">${e.errors.map(esc).join("<br>")}</div>` : ""}
        ${e.missing.length ? `<div class="callout info">Enter: ${e.missing.map(esc).join(", ")}.</div>` : ""}
        ${e.provisional_reasons.length && e.status === "provisional" ? `<div class="callout">Provisional because: ${e.provisional_reasons.map(esc).join(" ")}${
          e.open_blocking ? ` ${e.open_blocking} blocking review item(s) open.` : ""}</div>` : ""}
        <div class="result">
          <div class="stat"><div class="k">Existing obligation-to-income</div><div class="val">${e.existing_ratio_pct !== null ? calc(e.existing_ratio_pct.toFixed(2) + "%") : "—"}</div></div>
          <div class="stat"><div class="k">Max obligations at ratio</div><div class="val">${e.max_obligation !== undefined && e.max_obligation !== null ? calc(money(e.max_obligation)) : "—"}</div></div>
          <div class="stat"><div class="k">Additional EMI capacity</div><div class="val">${e.capacity_emi !== null ? calc(money(e.capacity_emi)) : "—"}</div></div>
          <div class="stat"><div class="k">Indicative principal</div><div class="val">${e.principal !== null ? calc(money0(e.principal)) : "—"}</div></div>
        </div>
        ${(e.notes || []).map((n) => `<p class="note">${esc(n)}</p>`).join("")}
        <p class="note">capacity = max(0, ratio × income − obligations); principal = capacity × (1 − (1+i)<sup>−n</sup>) / i with
        i = annual rate ÷ 12 (capacity × n at 0%). Computed on the server in exact decimal arithmetic.</p>
      </div>`;
  }

  /* findings (shared by Overview and Review & Export) */
  function findingItem(r) {
    const controls = r.resolved ? "" :
      r.action === "obligation" ? `<button class="small primary" data-action="treat" data-key="${esc(r.target)}">Decide</button>`
      : r.action === "match" ? `<button class="small primary" data-action="decide" data-key="${esc(r.target)}" data-status="confirmed">Confirm</button>
         <button class="small" data-action="decide" data-key="${esc(r.target)}" data-status="rejected">Reject</button>`
      : r.action === "assumption" ? `<button class="small primary" data-tab="eligibility">Enter</button>`
      : r.action === "fact" ? `<button class="small primary" data-action="correct-fact" data-key="${esc(r.target)}">Edit / confirm</button>`
      : `<button class="small" data-action="ack" data-key="${esc(r.key)}">Resolve with note</button>`;
    return `<div class="review-item ${r.resolved ? "resolved" : ""}">
      <div class="what"><div class="area">${esc(r.area)}${r.blocking && !r.resolved ? ` · <span class="blocks">affects the worksheet</span>` : ""}</div>
        ${esc(r.text)}${r.resolution ? `<div class="note">Resolved: ${esc(r.resolution)}</div>` : ""}</div>
      <div class="ctl">${controls}${r.tab ? ` <button class="small ghost" data-tab="${r.tab}">View</button>` : ""}</div></div>`;
  }

  function findingGroups(c, limit) {
    return GROUPS.map(([g, name]) => {
      const open = c.review.filter((r) => r.group === g && !r.resolved);
      const shown = limit ? open.slice(0, limit) : open;
      return `<div class="fgroup fg-${g}"><h3>${name} <span class="count">${open.length}</span></h3>
        ${shown.map(findingItem).join("") || `<p class="empty">None open.</p>`}
        ${limit && open.length > limit ? `<button class="link" data-tab="review">${open.length - limit} more…</button>` : ""}</div>`;
    }).join("");
  }

  /* overview */
  function overview(c) {
    const h = c.headline;
    const inc = h.income;
    const d = h.debt;
    const el = inc.eligible || {};
    const od = c.od.filter((o) => !o.obligation.basis || !String(o.obligation.basis).startsWith("Closed"));
    const odRows = od.map((o) => `<tr><td><b>${esc(o.lender || "")}</b> overdraft</td>
      <td class="num">${o.sanctioned_limit !== null ? `<span class="v reported" data-fact="${(o.fields.sanctioned_amount || o.fields.credit_limit || {}).id}">${money(o.sanctioned_limit)}</span>` : NA}</td>
      <td class="num">${o.fields.current_balance ? chip(o.fields.current_balance, { edit: false }) : NA}</td>
      <td class="num">${o.drawing_power !== null ? money(o.drawing_power) : NA}</td>
      <td class="num">${o.interest_series ? inferred(`${money(o.interest_series.min)}–${money(o.interest_series.max)}`, `data-action="txns" data-ids="${esc(JSON.stringify(o.interest_series.txn_ids))}" data-title="OD servicing debits"`) : NA}</td>
      <td>${o.obligation.included ? `<span class="v analyst">${money(o.obligation.amount)}</span>` : `<span class="pill warn">not set</span>`}</td></tr>`).join("");
    const bizAcct = c.case.borrower_type !== "salaried" ? (c.banking.analysis.find((a) => a.account_type === "current") || c.banking.analysis[0]) : null;
    const incomeCard = bizAcct ? `<div class="hcard"><div class="k">Business banking</div>
        <div class="big">${bizAcct.avg_monthly_business_credits !== null ? avg(money0(bizAcct.avg_monthly_business_credits), bizAcct.account_key, "credits") : NA}</div>
        <div class="note">Business credits per month, ${esc(bizAcct.account_key.replace("|", " "))} (${esc(bizAcct.account_type || "")}), ${bizAcct.month_count} months</div>
        <div class="note">AMB ${bizAcct.amb !== null ? avg(money0(bizAcct.amb), bizAcct.account_key, "amb") : "not available"} · vintage ${c.business.vintage.years !== null
          ? `${c.business.vintage.years} yrs (since ${chip(c.business.vintage.fact, { edit: false })})` : "not available"}</div>
        <div class="note">ITR / financials: ${c.documents.some((d) => ["itr_ack", "itr_json", "itr_computation", "financials"].includes(d.kind)) ? "received" : "not available"}</div>
        <div class="note"><button class="link" data-tab="banking">Business & Banking</button></div></div>` : null;
    return `<div class="headline">
      ${incomeCard || `<div class="hcard"><div class="k">Income</div>
        <div class="big">${inc.net_pay ? chip(inc.net_pay, { edit: false }) : NA}</div>
        <div class="note">${inc.net_pay ? `Net pay, ${ym(inc.net_pay_month)} salary slip` : "No verified salary slip"}</div>
        <div class="note">${inc.form16_gross ? `Gross salary ${chip(inc.form16_gross, { edit: false })} — Form 16 ${esc(inc.form16_ay || "")}` : "Form 16: not available"}</div>
        <div class="note">Eligible income: ${el.value !== null && el.value !== undefined ? `${el.origin === "analyst" ? `<span class="v analyst">${money(el.value)}</span>` : inferred(money(el.value))} (${esc(el.origin)})` : "not set"}</div></div>`}
      <div class="hcard"><div class="k">Reported outstanding debt</div>
        <div class="big">${d.outstanding !== null ? fsrc(money(d.outstanding), d.parts.map((x) => x.fact), "Balances added up") : NA}</div>
        ${d.parts.length ? `<table class="parts"><tbody>${d.parts.map((x) => `<tr><td>${esc(x.label)}</td>
          <td class="num">${chip(x.fact, { edit: false })}</td></tr>`).join("")}</tbody></table>` : ""}
        ${!c.credit.reports.length ? `<div class="note">No credit report received.</div>` : ""}
        <div class="note" ${!c.credit.reports.length ? "hidden" : ""}>Sum of ${d.counted} of ${d.accounts} open account(s) held by the applicant${d.report_date ? `, credit report dated <span class="v reported" data-fact="${d.report_fact}">${fmtDate(d.report_date)}</span>` : ""}</div>
        ${d.not_counted.length ? `<div class="note">Not available for: ${d.not_counted.map(esc).join(", ")}</div>` : ""}
        ${d.excluded_roles.length ? `<div class="note">Not included (guarantor / authorised user): ${d.excluded_roles.map(esc).join(", ")}</div>` : ""}
        <div class="note">Includes the OD's drawn balance; an OD limit is not debt.</div></div>
      <div class="hcard"><div class="k">Monthly obligations used</div>
        <div class="big"><span class="v calculated link" data-action="obligations" title="Show what makes up this total">${money(h.obligations.total)}</span></div>
        <div class="note">${h.obligations.rows_included} included${h.obligations.unresolved ? ` · <b>${h.obligations.unresolved} awaiting a decision</b> (not in this total)` : ""}</div>
        <div class="note"><button class="link" data-tab="liabilities">Open liability schedule</button></div></div>
      <div class="hcard"><div class="k">Unresolved findings</div>
        <div class="fcounts">${GROUPS.map(([g, name]) => `<div><b>${h.findings[g]}</b> ${name}</div>`).join("")}</div>
        <div class="note"><button class="link" data-tab="review">Review & Export</button></div></div>
    </div>
    ${odRows ? `<div class="card"><h2>Overdraft — shown separately</h2>
      <p class="sub">The limit is not debt and not an EMI. Only the drawn balance is owed; a monthly obligation must be an analyst assumption.</p>
      <div class="tbl-wrap"><table><thead><tr><th>Facility</th><th class="num">Sanctioned limit</th><th class="num">Outstanding</th>
      <th class="num">Drawing power</th><th class="num">Observed servicing</th><th>Obligation used</th></tr></thead><tbody>${odRows}</tbody></table></div></div>` : ""}
    <div class="card"><h2>Findings</h2><p class="sub">Each finding has its control beside it. Items marked “affects the worksheet” keep the result provisional.</p>
      ${findingGroups(c, 3)}</div>`;
  }

  /* review & export */
  function review(c) {
    const done = c.review.filter((r) => r.resolved);
    const e = c.eligibility;
    return `<div class="card export"><div><h2>Review pack</h2>
        <p class="sub">An Excel workbook built from exactly what is on screen: case and documents, income, liability schedule,
        bank reconciliation, findings with their status, worksheet inputs with their basis, and the source page of every figure.</p>
        <p class="note">Worksheet: <b>${esc(label(e.status))}</b>${e.open_blocking ? ` — ${e.open_blocking} open finding(s) affect it, and the pack says so.` : "."}
        ${esc(e.label)}</p></div>
      <a class="btn primary big-btn" href="${exportUrl(state.caseId)}" download>${DEMO ? "Download sample review pack (.xlsx)" : "Export review pack (.xlsx)"}</a></div>
      <div class="card"><h2>Findings</h2>${findingGroups(c)}</div>
      ${done.length ? `<div class="card"><details><summary>${done.length} resolved finding(s)</summary>${done.map(findingItem).join("")}</details></div>` : ""}`;
  }

  /* ---------- evidence ---------- */

  async function showEvidence(kind, id) {
    const pane = $("#evidence");
    pane.innerHTML = `<p class="empty">Loading source…</p>`;
    try {
      const ev = await api(kind === "fact" ? `/fact/${id}` : `/txn/${id}`);
      const d = ev.document;
      let body = "";
      const head = `<div class="ev-meta"><b>${esc(d.filename)}</b> · ${esc(KIND[d.kind] || d.kind)}${
        ev.page_no ? ` · page ${ev.page_no} of ${d.page_count}` : ""}<br>${ev.item ? esc(ev.item) + " · " : ""}${
        ev.field ? esc(label(ev.field)) : "Statement row"} · ${ev.origin === "model" ? "proposed by model" :
        ev.origin === "json" ? "read from JSON" : "read by parser"}</div>`;
      if (d.media_type === "application/json" && ev.json_path) {
        const j = await api(`/json/${d.id}?path=${encodeURIComponent(ev.json_path)}`);
        const pretty = esc(JSON.stringify(j.parent, null, 2)).replace(
          new RegExp(`(&quot;${j.key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}&quot;: [^,\\n]*)`), "<mark>$1</mark>");
        body = `<p><span class="pill ok">verified</span> JSON path <code>${esc(ev.json_path)}</code></p>
          <div class="ev-json">${pretty}</div>`;
      } else if (ev.verified) {
        const q = kind === "fact" ? `fact_id=${id}` : `txn_id=${id}`;
        body = `<p><span class="pill ok">verified</span> The highlighted text is read back from the page at stored character offsets.</p>
          <div class="ev-quote">${esc(ev.evidence_text)}</div>
          ${pageImage(d.id, ev.page_no, ev.rects, `Page ${ev.page_no} of ${d.filename} with the source text highlighted`) ||
          `<img class="ev-page" alt="Page ${ev.page_no} of ${esc(d.filename)} with the source text highlighted"
            src="${API}/page.png?document_id=${d.id}&page_no=${ev.page_no}&${q}">`}`;
      } else {
        body = `<p><span class="pill warn">unverified</span> ${esc(ev.note || "")}</p>
          <p class="note">The model's quote is shown below. It was not found on the page, so nothing is highlighted
          and the value is not used until an analyst confirms or corrects it.</p>
          <div class="ev-quote">${esc(ev.evidence_text || "(no quote)")}</div>
          ${ev.page_no ? `<button class="small" data-action="show-page" data-doc="${d.id}" data-page="${ev.page_no}">Open page ${ev.page_no} as cited by the model (unconfirmed)</button>
          <div id="cited-page"></div>` : ""}`;
      }
      pane.innerHTML = head + body + (ev.note && ev.verified ? `<p class="note">${esc(ev.note)}</p>` : "");
    } catch (err) {
      pane.innerHTML = `<p class="callout">Could not load the source: ${esc(err.message)}</p>`;
    }
  }

  /* ---------- dialogs ---------- */

  function dialog(title, html, onSave) {
    const dlg = $("#dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-body").innerHTML = html;
    $("#dialog-err").hidden = true;
    dlg.showModal();
    const form = $("#dialog-form");
    form.onsubmit = async (ev) => {
      if (ev.submitter && ev.submitter.value === "cancel") return;
      ev.preventDefault();
      try {
        const before = snapshot(state.data);
        await onSave(dlg);
        dlg.close();
        state.prev = before;
        await refresh();
      } catch (err) {
        $("#dialog-err").textContent = err.message;
        $("#dialog-err").hidden = false;
      }
    };
  }
  const reasonField = (ph) => `<label>Reason (required, kept with the change)</label><textarea id="d-reason" required placeholder="${esc(ph || "")}"></textarea>`;
  const reasonVal = () => {
    const v = $("#d-reason").value.trim();
    if (!v) throw new Error("A reason is required.");
    return v;
  };

  function correctFact(key, kind, current) {
    const view = findFact(key);
    kind = kind || (view && view.kind) || "text";
    current = current ?? (view ? view.value ?? "" : "");
    const input = kind === "amount" ? `<input type="number" step="0.01" id="d-val" value="${esc(current)}">`
      : kind === "date" ? `<input type="text" id="d-val" placeholder="YYYY-MM-DD" value="${esc(current)}">`
      : `<input type="text" id="d-val" value="${esc(current)}">`;
    dialog("Correct or confirm value", `
      ${view ? `<p class="note">Original extraction: <b>${view.reported_blank ? "blank (reported)" : esc(view.original ?? "—")}</b> (${esc(ORIGIN[view.status] || view.status)}). The original is kept.</p>` : ""}
      <label>Value used</label>${input}
      <p class="note">Leave empty to record the value as absent (not zero).</p>
      ${reasonField("e.g. Checked against page 2 of the report")}`, async () => {
      const raw = $("#d-val").value.trim();
      const value = raw === "" ? null : kind === "amount" ? Number(raw) : raw;
      if (kind === "amount" && raw !== "" && !isFinite(value)) throw new Error("Not a number.");
      await post(`/cases/${state.caseId}/corrections`, { target: "fact", target_id: key, field: "value",
        value: { value }, reason: reasonVal() });
    });
  }

  function findFact(key) {
    const c = state.data;
    const groups = [...c.income.slips.map((s) => s.fields), ...c.credit.reports.map((r) => r.fields),
      ...c.credit.tradelines.map((t) => t.fields), ...c.banking.statements.map((s) => s.fields),
      ...c.income.itr.map((i) => i.fields)];
    for (const g of groups) for (const v of Object.values(g)) if (v.key === key) return v;
    return null;
  }

  function treat(key) {
    const row = state.data.obligations.rows.find((r) => r.key === key);
    const od = state.data.od.find((o) => o.key === key);
    const hint = od && od.interest_series ? `Observed OD interest debits average ${money(od.interest_series.average)} a month.` :
      row && row.observed ? `Observed bank debit ${money(row.observed.amount)} a month.` : "";
    dialog(`Treatment — ${row ? row.label : key}`, `
      ${od ? `<div class="callout rule">${esc(od.rule)}</div>` : ""}
      <label>Count this as a monthly obligation?</label>
      <select id="d-inc"><option value="1" ${row && row.included ? "selected" : ""}>Include</option>
      <option value="0" ${row && !row.included ? "selected" : ""}>Exclude</option></select>
      <label>Monthly amount (₹) — if included</label><input type="number" step="0.01" id="d-amt" value="${row && row.amount !== null ? row.amount : ""}">
      ${hint ? `<p class="note">${esc(hint)} Nothing is pre-filled from it.</p>` : ""}
      <label>Basis of the amount — if included</label><input type="text" id="d-basis" placeholder="${od ? "e.g. Average observed OD interest Jun–Sep" : "e.g. EMI per sanction letter"}">
      ${reasonField(od ? "e.g. Illustrative: servicing interest only, per analyst" : "e.g. Closing before disbursal per applicant")}`, async () => {
      const include = $("#d-inc").value === "1";
      const amtRaw = $("#d-amt").value.trim();
      const amount = amtRaw === "" ? null : Number(amtRaw);
      if (include && (amount === null || !isFinite(amount) || amount < 0)) throw new Error("Enter a monthly amount of 0 or more.");
      const basis = $("#d-basis").value.trim();
      if (include && !basis) throw new Error("Enter the basis of the amount.");
      await post(`/cases/${state.caseId}/corrections`, { target: "obligation", target_id: key, field: "treatment",
        value: { include, amount: include ? amount : null, basis }, reason: reasonVal() });
    });
  }

  function decide(key, status) {
    dialog(status === "confirmed" ? "Confirm match" : "Reject match",
      `<p class="note">${status === "confirmed" ? "The bank debits will be treated as repayments of this bureau account (one obligation, not two)."
        : "The bank debits will no longer stand in for this account; if they recur, they appear as a separate item to decide."}</p>${reasonField()}`,
      async () => post(`/cases/${state.caseId}/decisions`, { match_key: key, status, reason: reasonVal() }));
  }

  function ackItem(key) {
    dialog("Accept with note", `<label>Note (required)</label><textarea id="d-reason" required></textarea>`,
      async () => post(`/cases/${state.caseId}/acks`, { item_key: key, note: reasonVal() }));
  }

  function setCategory(sel) {
    const key = sel.dataset.key;
    const value = sel.value;
    dialog("Reclassify transaction", `<p class="note">New category: <b>${esc(label(value))}</b>. The automatic classification is kept alongside.</p>${reasonField()}`,
      async () => post(`/cases/${state.caseId}/corrections`, { target: "txn", target_id: key, field: "category",
        value: { value }, reason: reasonVal() }));
    $("#dialog").addEventListener("close", () => { sel.value = sel.dataset.current; }, { once: true });
  }

  function newCase() {
    dialog("New case", `<label>Case label</label><input type="text" id="d-label" required placeholder="e.g. Salaried — personal loan">
      <label>Applicant name</label><input type="text" id="d-name">
      <label>Borrower type</label><select id="d-type"><option value="salaried">Salaried</option>
        <option value="proprietorship">Proprietorship (business loan)</option></select>
      <label>Requested facility (optional)</label><input type="text" id="d-prod" placeholder="e.g. Personal loan">
      <label>Requested amount ₹ (optional)</label><input type="number" id="d-amt" step="1">
      <p class="note">Salaried and proprietorship cases in this version. The case is private to this machine.</p>`, async () => {
      const label_ = $("#d-label").value.trim();
      if (!label_) throw new Error("Enter a label.");
      const amt = $("#d-amt").value.trim();
      const r = await post("/cases", { label: label_, applicant_name: $("#d-name").value.trim() || null,
        requested_product: $("#d-prod").value.trim() || null, borrower_type: $("#d-type").value, requested_amount: amt ? Number(amt) : null });
      state.tab = "overview";
      await loadCases(r.id);
    });
  }

  async function upload() {
    const files = $("#files").files;
    const kind = $("#upload-kind").value;
    const msg = $("#upload-msg");
    if (!files.length) { msg.textContent = "Choose one or more files."; return; }
    msg.textContent = "Uploading…";
    const results = [];
    for (const f of files) {
      try {
        const q = `filename=${encodeURIComponent(f.name)}${kind ? "&kind=" + kind : ""}`;
        const r = await api(`/cases/${state.caseId}/documents?${q}`, { method: "POST", body: f });
        results.push(`${f.name}: ${r.status}`);
      } catch (err) {
        results.push(`${f.name}: ${err.message}`);
      }
    }
    msg.textContent = results.join(" · ");
    await refresh();
  }

  async function saveAssumptions() {
    const msg = $("#elig-msg");
    const keys = ["eligible_income", "foir_max_pct", "annual_rate_pct", "tenure_months"];
    const current = state.data.eligibility.inputs;
    const posts = [];
    for (const k of keys) {
      const raw = $(`#in-${k}`).value.trim();
      const basis = $(`#basis-${k}`).value.trim();
      const cur = current[k];
      const curVal = cur && cur.origin !== "suggested" ? cur.value : null;
      const changed = raw !== "" && (curVal === null || Number(raw) !== Number(curVal) || (basis && basis !== cur.basis));
      if (!changed) continue;
      if (!basis) { msg.textContent = `Enter a basis for ${label(k)}.`; return; }
      posts.push(post(`/cases/${state.caseId}/assumptions`, { key: k, value: Number(raw), basis }));
    }
    if (!posts.length) { msg.textContent = "Nothing changed."; return; }
    try { await Promise.all(posts); msg.textContent = ""; await refresh(); }
    catch (err) { msg.textContent = err.message; }
  }

  /* ---------- events ---------- */

  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-tab],[data-fact],[data-txn],[data-action]");
    if (!t) return;
    if (t.matches("select")) return;
    if (t.dataset.tab) {
      state.tab = t.dataset.tab; history.replaceState(null, "", `#tab=${state.tab}`);
      render(); $("#view").focus(); return;
    }
    if (t.dataset.fact) {
      document.querySelectorAll(".v.sel, tr.sel").forEach((e) => e.classList.remove("sel"));
      t.classList.add("sel"); state.sel = t.dataset.fact;
      return showEvidence("fact", t.dataset.fact);
    }
    if (t.dataset.txn && !t.dataset.action) {
      document.querySelectorAll(".v.sel, tr.sel").forEach((e) => e.classList.remove("sel"));
      t.classList.add("sel");
      return showEvidence("txn", t.dataset.txn);
    }
    const a = t.dataset.action;
    if (a === "correct-fact") correctFact(t.dataset.key, t.dataset.kind, t.dataset.current);
    else if (a === "treat") treat(t.dataset.key);
    else if (a === "decide") decide(t.dataset.key, t.dataset.status);
    else if (a === "ack") ackItem(t.dataset.key);
    else if (a === "txns") showTxnList(JSON.parse(t.dataset.ids), t.dataset.title);
    else if (a === "facts") showFactList(JSON.parse(t.dataset.ids), t.dataset.title);
    else if (a === "obligations") showObligations();
    else if (a === "average") showAverage(t.dataset.acct, t.dataset.what);
    else if (a === "upload") upload();
    else if (a === "save-assumptions") saveAssumptions();
    else if (a === "reprocess") { if (DEMO) return alert(READ_ONLY); await post(`/documents/${t.dataset.doc}/process`, {}); await refresh(); }
    else if (a === "show-page") {
      $("#cited-page").innerHTML = `<p class="note">Page as cited by the model — no highlight, location not confirmed.</p>
        ${pageImage(t.dataset.doc, Number(t.dataset.page), [], `Page ${t.dataset.page}`) ||
        `<img class="ev-page" alt="Page ${t.dataset.page}" src="${API}/page.png?document_id=${t.dataset.doc}&page_no=${t.dataset.page}">`}`;
    }
  });
  document.addEventListener("change", async (ev) => {
    const t = ev.target;
    if (t.id === "case-select") { state.tab = "overview"; return openCase(t.value); }
    if (t.id === "txn-filter") { state.txnFilter = t.value; return render(); }
    if (t.id === "hide-dup") { state.hideDup = t.checked; return render(); }
    if (t.dataset.action === "set-cat") return setCategory(t);
    if (t.dataset.action === "set-kind" && DEMO) { alert(READ_ONLY); return refresh(); }
    if (t.dataset.action === "set-kind") {
      await post(`/documents/${t.dataset.doc}/kind`, { kind: t.value });
      return refresh();
    }
  });
  $("#new-case").addEventListener("click", newCase);

  const hashTab = (location.hash.match(/tab=(\w+)/) || [])[1];
  if (hashTab && TABS.some(([id]) => id === hashTab)) state.tab = hashTab;
  const hashEv = location.hash.match(/ev=(fact|txn):(\d+)/);
  if (hashEv) showEvidence(hashEv[1], hashEv[2]);
  if (DEMO) document.body.classList.add("demo");
  (DEMO ? Promise.resolve() : loadProvider()).then(() => loadCases()).catch((err) => {
    $("#view").innerHTML = `<div class="callout">Could not load cases: ${esc(err.message)}</div>`;
  });
})();
