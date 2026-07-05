// The ONE place the Vulcan site talks to the Vulcan API.
//
// Every network call AND every API-served asset URL (images, STEP/3MF/STL
// downloads, the STL the 3D viewer fetches) is routed through here. That keeps
// the frontend a clean, portable client: change web/config.js `apiBase` and the
// whole site follows the API to a new origin, no other edits needed. Depends on
// config.js (window.VULCAN_CONFIG).

// Shared, defensive error rendering for failed fetches. Works whether the
// server sent a JSON body ({detail: ...}), plain text, or nothing at all, and
// always includes the HTTP status — so an empty "Error:" can never be shown.
// (Lives here, not in app.js, so both the photo flow and the template studio
// share one implementation.)
async function describeFetchError(response) {
  const statusPart = `HTTP ${response.status}${response.statusText ? " " + response.statusText : ""}`;
  let detail = "";
  const raw = await response.text().catch(() => "");
  if (raw) {
    try {
      const body = JSON.parse(raw);
      if (body && body.detail !== undefined && body.detail !== null) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } else {
        detail = raw;
      }
    } catch {
      detail = raw; // not JSON — show the raw text
    }
  }
  detail = (detail || "").toString().trim();
  if (detail.length > 600) detail = detail.slice(0, 600) + "…";
  return detail ? `${statusPart}: ${detail}` : statusPart;
}

// Never let a caught error render as an empty string.
function errorText(err) {
  if (err && err.message) return err.message;
  const s = String(err || "");
  return s || "request failed";
}

window.VulcanAPI = (function () {
  const JSON_HEADERS = { "Content-Type": "application/json" };

  function apiBase() {
    return (window.VULCAN_CONFIG && window.VULCAN_CONFIG.apiBase) || "";
  }

  // Turn an API path (a "/intents" endpoint, or a "/exports/…/part.stl" URL from
  // a files.* field) into a full URL against the configured API origin. Absolute
  // URLs and blob:/data: URLs pass through untouched (local object URLs, etc.).
  function url(path) {
    if (!path) return path;
    if (/^(https?:|blob:|data:)/i.test(path)) return path;
    const b = apiBase();
    if (!b) return path; // same origin — leave root-relative paths as-is
    return b.replace(/\/+$/, "") + (path[0] === "/" ? "" : "/") + path;
  }

  function req(path, opts) {
    return fetch(url(path), opts || {});
  }

  return {
    // URL helpers. `asset` is the same as `url` but named for intent at call
    // sites that turn a files.* value into an <img src>/<a href>/STL fetch URL.
    url,
    asset: url,
    describeFetchError,
    errorText,

    // Template studio ("From a template").
    getTemplates: () => req("/templates"),
    createDesign: (body) =>
      req("/designs", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) }),

    // Photo flow ("Start with a photo").
    createIntent: (formData) => req("/intents", { method: "POST", body: formData }),
    submitAnswers: (intentId, body) =>
      req(`/intents/${intentId}/answers`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(body),
      }),
    // Freeform is async: POST starts a job ({job_id, status_url}); poll the job.
    freeform: (intentId) => req(`/intents/${intentId}/freeform`, { method: "POST" }),
    freeformJob: (statusUrl) => req(statusUrl),
    joinDesign: (intentId, body) =>
      req(`/intents/${intentId}/design`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(body || {}),
      }),

    // Founder review dashboard. The token is a request header (no cookies).
    reviewQueue: (status) => req(`/review?status=${encodeURIComponent(status)}`),
    submitVerdict: (designId, body, token) =>
      req(`/review/${designId}`, {
        method: "POST",
        headers: Object.assign({}, JSON_HEADERS, { "X-Review-Token": token }),
        body: JSON.stringify(body),
      }),

    // Fetch an API-served file (optionally with the founder token) — used for
    // authorized blob downloads on the dashboard.
    fetchFile: (fileUrl, token) => req(fileUrl, token ? { headers: { "X-Review-Token": token } } : {}),
  };
})();

// describeFetchError/errorText are top-level function declarations (globals in a
// classic script), but export them explicitly too so app.js/intents.js's bare
// calls are unambiguous regardless of load nuance.
window.describeFetchError = describeFetchError;
window.errorText = errorText;
