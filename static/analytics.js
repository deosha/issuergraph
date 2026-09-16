/* Optional product analytics.
 *
 * Off unless POSTHOG_PROJECT_KEY is set: /api/config reports whether it is, and
 * nothing is loaded when it is not — no key, no network call, no cookie.
 *
 * What may be sent is fixed by ALLOWED below. Events carry counts, page numbers
 * and which tab was opened; they never carry a name, an email address, an
 * organisation, a free-text field or any document content. The pilot form sends
 * "submitted", not what was typed into it. That rule is enforced here rather
 * than left to each call site: track() drops any key it was not told about.
 */
(function () {
  const ALLOWED = new Set([
    "page", "tab", "claim_type", "source", "page_no", "anchors", "conflict_kind",
    "status", "step", "steps", "outcome", "duplicate", "field_errors", "demo",
  ]);

  let posthog = null;
  const queue = [];

  function clean(props) {
    const out = {};
    for (const [key, value] of Object.entries(props || {})) {
      if (!ALLOWED.has(key)) continue;
      if (value === null || value === undefined) continue;
      // Scalars only: an object or array could smuggle a free-text field in.
      if (typeof value === "object") continue;
      if (typeof value === "string" && value.length > 60) continue;
      out[key] = value;
    }
    return out;
  }

  window.track = function track(event, props) {
    const payload = clean(props);
    if (posthog) posthog.capture(event, payload);
    else queue.push([event, payload]);
    if (window.IG_DEBUG_ANALYTICS) console.debug("[track]", event, payload);
  };

  fetch("/api/config").then(r => r.json()).then(cfg => {
    window.SITE_CONFIG = cfg;
    document.dispatchEvent(new CustomEvent("ig:config", { detail: cfg }));
    if (!cfg.analytics || !cfg.analytics.enabled) return;

    // Minimal PostHog loader; only reached when a key is configured.
    const script = document.createElement("script");
    script.src = cfg.analytics.host.replace(/\/$/, "") + "/static/array.js";
    script.async = true;
    script.onload = () => {
      window.posthog.init(cfg.analytics.key, {
        api_host: cfg.analytics.host,
        capture_pageview: false,       // sent explicitly, with our own properties
        autocapture: false,            // autocapture would hoover up form text
        disable_session_recording: true,
person_profiles: "never",
      });
      posthog = window.posthog;
      queue.splice(0).forEach(([event, props]) => posthog.capture(event, props));
    };
    document.head.appendChild(script);
  }).catch(() => { /* analytics must never break the page */ });
})();
