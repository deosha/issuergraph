/* Landing-page behaviour: configuration, the live preview, and CTA tracking.
 *
 * The preview is deliberately not a screenshot of a table. It fetches the same
 * snapshot the demo serves and renders real rows from it, so the landing page
 * cannot drift from the product or quote a count the data does not support.
 */

// --- configuration ----------------------------------------------------------
document.addEventListener("ig:config", (e) => applyConfig(e.detail));
if (window.SITE_CONFIG) applyConfig(window.SITE_CONFIG);

function applyConfig(cfg) {
  document.querySelectorAll("[data-cfg]").forEach(el => {
    const value = cfg[el.dataset.cfg];
    if (value) { el.textContent = value; el.hidden = false; }
    else if (el.hasAttribute("hidden") || el.dataset.cfg === "founder_bio") el.hidden = true;
  });

  const mail = `mailto:${cfg.contact_email}?subject=IssuerGraph`;
  ["contact-email", "footer-email"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.href = mail;
    if (id === "contact-email") el.textContent = cfg.contact_email;
  });

  // A LinkedIn profile is optional: with none configured, no link is rendered.
  const linkedin = document.getElementById("founder-linkedin");
  if (linkedin && cfg.founder_linkedin) {
    linkedin.href = cfg.founder_linkedin; linkedin.hidden = false;
  }

  // A booking link is optional: with none configured, the button never appears
  // rather than pointing at a placeholder.
  const booking = document.getElementById("booking-link");
  if (booking && cfg.booking_url) { booking.href = cfg.booking_url; booking.hidden = false; }

  // Pricing is configurable and defaults to asking for scope.
  const pricing = document.getElementById("pricing-line");
  if (pricing && cfg.pilot_price) {
    pricing.innerHTML = `<b>Pilot price: ${escapeHtml(cfg.pilot_price)}</b> — fixed, over ` +
      `${escapeHtml(cfg.pilot_length || "the pilot")}. Tell us your issuers and the ` +
      `workflow and you will get a schedule with it.`;
  }
}

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const inr = (v) => v == null ? "—" : "₹" + Number(v).toLocaleString("en-IN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " Cr";
const day = (d) => d ? new Date(d).toLocaleDateString("en-GB",
  { day: "2-digit", month: "short", year: "numeric" }) : "—";

// --- the live preview -------------------------------------------------------
fetch("/api/demo/overview")
  .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
  .then(renderPreview)
  .catch(() => {
    const mount = document.getElementById("preview-mount");
    if (mount) mount.innerHTML = `<p class="muted small">The sample is not loaded in this
      environment. <a href="/demo">Open the demo</a> to see it.</p>`;
  });

function renderPreview(data) {
  const mount = document.getElementById("preview-mount");
  if (!mount) return;

  const cutoff = document.getElementById("cutoff-inline");
  if (cutoff && data.document_cutoff) cutoff.textContent = day(data.document_cutoff);
  // Counts in prose come from the snapshot too, so the page cannot drift from it.
  document.querySelectorAll("[data-count]").forEach(el => {
    const value = data.counts[el.dataset.count];
    if (value != null) el.textContent = value;
  });

  const c = data.counts;
  const totals = (data.debt.totals || [])
    .filter(t => t.fact_key && t.fact_key.startsWith("total_borrowings"))
    .slice(0, 4);

  const conflict = (data.conflicts || []).find(k => k.kind === "numeric_disagreement")
    || (data.conflicts || [])[0];

  mount.innerHTML = `
    <div class="grid four" style="margin-bottom:22px">
      ${statTile(c.claims, "anchored facts")}
      ${statTile(c.documents, "public documents")}
      ${statTile(c.conflicts_open, "open differences")}
      ${statTile(c.changes, "tracked changes")}
    </div>

    <div class="table-scroll">
      <table>
        <caption class="muted small" style="text-align:left;padding:11px 14px">
          Total borrowings, as stated by each source — sample dataset
        </caption>
        <thead><tr>
          <th scope="col">Basis</th><th scope="col">As at</th>
          <th scope="col" class="num">Figure</th><th scope="col">Source</th>
          <th scope="col">Evidence</th>
        </tr></thead>
        <tbody>${totals.map(t => `<tr>
          <td>${escapeHtml(t.basis)}</td>
          <td class="muted">${day(t.as_of_date)}</td>
          <td class="num"><b>${inr(t.value_numeric)}</b></td>
          <td>${escapeHtml(t.source_name)}</td>
          <td><a href="/demo#claim-${t.claim_id}" data-cta="demo" data-where="preview-row">
            page + highlighted text →</a></td>
        </tr>`).join("")}</tbody>
      </table>
    </div>

    ${conflict ? `<div class="card attention" style="margin-top:20px">
      <h3><span class="pill warn">Difference · ${escapeHtml(conflict.status)}</span>
        ${escapeHtml(conflict.subject)}</h3>
      <p class="small attention-text">${escapeHtml(conflict.note)}</p>
      <div class="small muted">
        First detected ${day(conflict.first_detected_at)} ·
        still present ${day(conflict.last_seen_at)}
      </div>
      <p class="small" style="margin:12px 0 0">
        Both figures are kept with their own evidence.
        <a href="/demo" data-cta="demo" data-where="preview-conflict">Compare them in the demo →</a>
      </p>
    </div>` : ""}

    <p class="small muted" style="margin-top:14px">
      Sample dataset · documents published up to ${day(data.document_cutoff)} ·
      ${c.evidenced_claims} of these facts have their source page bundled for offline viewing.
    </p>`;
}

function statTile(value, label) {
  return `<div class="card" style="padding:18px">
    <div style="font:650 27px/1.1 var(--mono);color:var(--text)">${escapeHtml(value)}</div>
    <div class="small muted" style="margin-top:4px">${escapeHtml(label)}</div>
  </div>`;
}

// --- tracking ---------------------------------------------------------------
if (window.track) track("landing_viewed", { page: "landing" });

document.addEventListener("click", (e) => {
  const cta = e.target.closest("[data-cta]");
  if (!cta || !window.track) return;
  const map = { demo: "demo_cta_clicked", pilot: "pilot_cta_clicked",
                booking: "booking_cta_clicked", email: "email_cta_clicked" };
  track(map[cta.dataset.cta] || "cta_clicked", { page: "landing" });
});
