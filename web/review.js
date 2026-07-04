// Founder review queue (Track B). Lists freeform design records, shows the
// request, the generated code (lightly highlighted), the render, the params, and
// the automated DFM/manifold results, and lets the founder approve or reject
// with a note. Verdicts drive the download gate (api/review.py).

const listEl = document.getElementById("review-list");
const statusEl = document.getElementById("review-status");
const countEl = document.getElementById("review-count");
const filterEl = document.getElementById("review-filter");
const refreshBtn = document.getElementById("review-refresh");
const tokenEl = document.getElementById("review-token");

// The founder token authorizes both recording a verdict and downloading a
// pending design's files. Remembered locally; blank is fine in local dev (the
// server treats any presented token as founder when none is configured).
const TOKEN_KEY = "vulcan_review_token";
tokenEl.value = localStorage.getItem(TOKEN_KEY) || "";
tokenEl.addEventListener("change", () => localStorage.setItem(TOKEN_KEY, tokenEl.value));
function founderToken() {
  return tokenEl.value || "founder"; // non-empty so the header is always sent
}

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", Boolean(isError));
}

// Fetch a file WITH the founder token header (so a pending design's CAD files
// download from the dashboard) and save it, keeping the token out of the URL.
async function downloadFile(url, filename) {
  setStatus(`Downloading ${filename}…`, false);
  try {
    const resp = await fetch(url, { headers: { "X-Review-Token": founderToken() } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus("", false);
  } catch (err) {
    setStatus(`Download failed: ${err.message || err}`, true);
  }
}

// Escape only the tag-injection characters, then color a few token classes. Not
// escaping quotes keeps the string regex simple; <>& are escaped so untrusted
// code can never inject markup into the founder's browser.
function highlight(code) {
  const esc = String(code).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .split("\n")
    .map((line) => {
      let out = line;
      // Comments first (rest of line).
      out = out.replace(/(#.*)$/g, '<span class="tok-comment">$1</span>');
      // Strings (single/double quoted, no escapes handling — fine for display).
      out = out.replace(/('[^']*'|"[^"]*")/g, '<span class="tok-string">$1</span>');
      // Keywords.
      out = out.replace(
        /\b(def|return|import|from|as|for|in|if|elif|else|while|and|or|not|None|True|False|class|with|lambda)\b/g,
        '<span class="tok-kw">$1</span>'
      );
      // Numbers.
      out = out.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="tok-num">$1</span>');
      return out;
    })
    .join("\n");
}

function dfmLine(dfm) {
  if (!dfm) return "no DFM data";
  const bits = [];
  bits.push(dfm.manifold ? "manifold ✓" : "NOT manifold ✗");
  bits.push(
    dfm.within_size
      ? `size OK (${dfm.max_extent_mm}mm ≤ ${dfm.size_ceiling_mm}mm)`
      : `OVER SIZE (${dfm.max_extent_mm}mm > ${dfm.size_ceiling_mm}mm)`
  );
  if (dfm.bbox_mm) bits.push(`bbox ${dfm.bbox_mm.join(" × ")} mm`);
  return bits.join(" · ");
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const c of [].concat(children)) {
    node.append(c instanceof Node ? c : document.createTextNode(c));
  }
  return node;
}

function renderCard(record) {
  const card = el("div", { className: "review-card" });

  const status = record.status || "pending_review";
  card.append(
    el("div", { className: "review-head" }, [
      el("span", { className: `review-status-badge status-${status}`, textContent: status }),
      el("span", { className: "review-id", textContent: record.design_id }),
    ])
  );

  card.append(el("p", { className: "review-request" }, [el("strong", {}, "Request: "), record.request || "(none)"]));

  if (record.assumptions && record.assumptions.length) {
    card.append(
      el("p", { className: "review-assumptions" }, [
        el("strong", {}, "Assumptions: "),
        record.assumptions.join(" "),
      ])
    );
  }

  card.append(el("p", { className: "review-dfm" }, [el("strong", {}, "DFM: "), dfmLine(record.dfm)]));

  // Render (preview is always viewable, even while pending).
  if (record.files && record.files.preview_png) {
    card.append(
      el("div", { className: "review-render" }, [
        el("img", { src: `${record.files.preview_png}?t=${Date.now()}`, alt: "Generated part render" }),
      ])
    );
  }

  // Params.
  if (record.params && record.params.length) {
    const rows = record.params.map((p) =>
      el("tr", {}, [
        el("td", { textContent: p.label || p.name }),
        el("td", { textContent: p.unit ? `${p.value} ${p.unit}` : `${p.value}` }),
        el("td", { textContent: p.source || "" }),
      ])
    );
    card.append(el("table", { className: "review-params" }, rows));
  }

  // Generated code, highlighted.
  const codePre = el("pre", { className: "review-code" });
  const codeEl = el("code");
  codeEl.innerHTML = highlight(record.code || "");
  codePre.append(codeEl);
  const details = el("details", {}, [el("summary", {}, "Generated CadQuery code"), codePre]);
  card.append(details);

  // Downloads: the founder can pull the files from their dashboard for ANY
  // freeform design — pending (to inspect before approving) or approved.
  if (record.files) {
    const btns = [];
    for (const [label, key] of [["STEP", "step"], ["3MF", "threemf"], ["STL", "stl"]]) {
      const url = record.files[key];
      if (!url) continue;
      const name = `${record.design_id}-${key}${url.slice(url.lastIndexOf("."))}`;
      btns.push(
        el("button", { type: "button", className: "download-btn", textContent: `Download ${label}` }, []),
      );
      btns[btns.length - 1].addEventListener("click", () => downloadFile(url, name));
    }
    if (btns.length) {
      const wrap = el("div", { className: "review-downloads" }, btns);
      if (status === "pending_review") {
        wrap.prepend(el("span", { className: "review-download-note" }, "Founder preview (not yet approved): "));
      }
      card.append(wrap);
    }
  }

  // Verdict controls (only for pending).
  if (status === "pending_review") {
    const note = el("input", { type: "text", placeholder: "note (why / what to templatize)", className: "review-note" });
    const approve = el("button", { type: "button", textContent: "Approve" });
    const reject = el("button", { type: "button", className: "reject-btn", textContent: "Reject" });
    approve.addEventListener("click", () => submitVerdict(record.design_id, "approve", note.value));
    reject.addEventListener("click", () => submitVerdict(record.design_id, "reject", note.value));
    card.append(el("div", { className: "review-actions" }, [note, approve, reject]));
  } else if (record.review_note) {
    card.append(el("p", { className: "review-note-shown" }, [el("strong", {}, "Note: "), record.review_note]));
  }

  return card;
}

async function submitVerdict(designId, verdict, note) {
  setStatus(`Recording ${verdict}…`, false);
  try {
    const resp = await fetch(`/review/${designId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Review-Token": founderToken() },
      body: JSON.stringify({ verdict, note: note || null }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    setStatus(`${verdict === "approve" ? "Approved" : "Rejected"} ${designId}.`, false);
    load();
  } catch (err) {
    setStatus(`Error: ${err.message || err}`, true);
  }
}

async function load() {
  setStatus("Loading…", false);
  try {
    const resp = await fetch(`/review?status=${filterEl.value}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const records = await resp.json();
    listEl.innerHTML = "";
    countEl.textContent = `${records.length} item(s)`;
    if (!records.length) {
      listEl.append(el("p", { className: "review-empty" }, "Nothing here."));
    } else {
      for (const r of records) listEl.append(renderCard(r));
    }
    setStatus("", false);
  } catch (err) {
    setStatus(`Error loading review queue: ${err.message || err}`, true);
  }
}

refreshBtn.addEventListener("click", load);
filterEl.addEventListener("change", load);
load();
