/* The pilot form.
 *
 * Two rules the UI has to respect, because they are the difference between a
 * lead-capture form and a lie:
 *
 *   - success is shown only when the server says the request was stored;
 *   - when storage fails, the visitor gets a way to reach us that does not
 *     depend on our database, not a cheerful confirmation.
 *
 * Validation runs on the server; this file mirrors the required fields for
 * immediate feedback and renders whatever the server sends back per field.
 */
const form = document.getElementById("form");
const statusBox = document.getElementById("status");
const submit = document.getElementById("submit");
const FIELDS = ["name", "email", "organisation", "role", "issuers", "workflow", "notes"];

document.addEventListener("ig:config", (e) => {
  const cfg = e.detail;
  document.querySelectorAll("[data-cfg]").forEach(el => {
    if (cfg[el.dataset.cfg]) el.textContent = cfg[el.dataset.cfg];
  });
  const mail = `mailto:${cfg.contact_email}?subject=IssuerGraph%20pilot`;
  ["contact-email", "footer-email"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.href = mail;
  });
});

if (window.track) track("pilot_form_viewed", { page: "pilot" });

function clearErrors() {
  FIELDS.forEach(name => {
    const input = document.getElementById(name);
    if (!input) return;
    input.closest(".field").classList.remove("invalid");
    input.removeAttribute("aria-invalid");
    document.getElementById(`${name}-err`).textContent = "";
  });
}

function showFieldErrors(errors) {
  let first = null;
  for (const [name, message] of Object.entries(errors || {})) {
    const input = document.getElementById(name);
    if (!input) continue;
    input.closest(".field").classList.add("invalid");
    input.setAttribute("aria-invalid", "true");
    document.getElementById(`${name}-err`).textContent = message;
    first = first || input;
  }
  if (first) first.focus();          // land the keyboard where the problem is
  return Object.keys(errors || {}).length;
}

function setStatus(kind, html) {
  statusBox.className = `form-status show ${kind}`;
  statusBox.innerHTML = html;
  statusBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearErrors();
  statusBox.className = "form-status";

  const payload = Object.fromEntries(
    FIELDS.map(name => [name, (document.getElementById(name).value || "").trim()]));
  payload.source = new URLSearchParams(location.search).get("from") || "pilot_page";

  // A cheap client-side pass so an obviously empty form does not need a round
  // trip. The server validates independently and is the authority.
  const local = {};
  for (const name of ["name", "email", "organisation", "role", "issuers", "workflow"]) {
    if (!payload[name]) local[name] = "This field is required.";
  }
  if (payload.email && !/^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/.test(payload.email))
    local.email = "Enter a valid email address.";
  if (Object.keys(local).length) {
    showFieldErrors(local);
    setStatus("bad", "Please complete the highlighted fields.");
    if (window.track) track("pilot_submit_failed",
      { outcome: "client_validation", field_errors: Object.keys(local).length });
    return;
  }

  submit.disabled = true;
  submit.textContent = "Sending…";

  let response, body;
  try {
    response = await fetch("/api/pilot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    body = await response.json();
  } catch {
    submit.disabled = false;
    submit.textContent = "Send request";
    setStatus("bad", "The request did not reach us — check your connection and try again.");
    if (window.track) track("pilot_submit_failed", { outcome: "network" });
    return;
  }

  submit.disabled = false;
  submit.textContent = "Send request";

  if (response.ok && body.stored) {
    form.querySelectorAll("input, textarea").forEach(el => { el.disabled = true; });
    submit.disabled = true;
    setStatus("ok", `<b>${escapeHtml(body.message)}</b>`);
    if (window.track) track("pilot_submitted",
      { outcome: "stored", duplicate: Boolean(body.duplicate) });
    return;
  }

  if (response.status === 422) {
    const count = showFieldErrors(body.fields);
    setStatus("bad", escapeHtml(body.error || "Please check the highlighted fields."));
    if (window.track) track("pilot_submit_failed",
      { outcome: "validation", field_errors: count });
    return;
  }

  // 429, 503 and anything unexpected: say what happened and give a route that
  // does not depend on the thing that just failed.
  const fallback = body.fallback_email
    ? ` <a href="mailto:${escapeHtml(body.fallback_email)}?subject=IssuerGraph%20pilot">Email us instead</a>.`
    : "";
  setStatus("bad", escapeHtml(body.error || "Something went wrong. Please try again.") + fallback);
  if (window.track) track("pilot_submit_failed",
    { outcome: response.status === 429 ? "rate_limited" : "server" });
});

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
