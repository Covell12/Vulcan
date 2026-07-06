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
    const resp = await VulcanAPI.fetchFile(url, founderToken());
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

// --- Per-card view: dimensioned render, in-photo composite (with/without the
// model), and an interactive 3D viewer (loaded WITH the founder token so a
// pending design's STL works). At most one inline card viewer + one modal are
// live at a time, to keep WebGL contexts bounded.
let dashViewer = null;
let dashModal = null;

function buildCardView(record) {
  const f = record.files;
  const wrap = el("div", { className: "review-view" });
  const box = el("div", { className: "review-view-box" });
  const img = el("img", { alt: "Part view" });
  const viewer = el("div", { className: "viewer3d" });
  viewer.style.display = "none";
  box.append(img, viewer);
  const bar = el("div", { className: "view-toggle" });

  // Image srcs are used directly (not via fetchFile), so resolve them against
  // the configured API origin with VulcanAPI.asset().
  const sources = [];
  if (f.composite) sources.push(["With part", `${VulcanAPI.asset(f.composite)}?t=${Date.now()}`]);
  if (f.photo) sources.push(["Photo only", `${VulcanAPI.asset(f.photo)}?t=${Date.now()}`]);
  if (f.preview_png) sources.push(["Dimensions", `${VulcanAPI.asset(f.preview_png)}?t=${Date.now()}`]);
  img.src = (sources[0] || [null, ""])[1];

  const clearActive = () => [...bar.querySelectorAll(".seg")].forEach((b) => b.classList.remove("active"));
  const flatBtns = sources.map(([label, src], i) => {
    const b = el("button", { type: "button", className: "seg", textContent: label });
    b.addEventListener("click", () => {
      if (dashViewer) { dashViewer.dispose(); dashViewer = null; }
      viewer.style.display = "none";
      img.style.display = "";
      img.src = src;
      clearActive();
      b.classList.add("active");
    });
    if (i === 0) b.classList.add("active");
    bar.append(b);
    return b;
  });

  const viewStl = f.view_stl || f.stl; // ungated coarse preview mesh when present
  if (viewStl || (f.parts && f.parts.length)) {
    const b3d = el("button", { type: "button", className: "seg", textContent: "3D" });
    b3d.addEventListener("click", () => {
      if (dashViewer) dashViewer.dispose();
      img.style.display = "none";
      viewer.style.display = "";
      clearActive();
      b3d.classList.add("active");
      mountRecordViewer(viewer, f).then((v) => (dashViewer = v));
    });
    bar.append(b3d);
    const exp = el("button", { type: "button", className: "expand-btn", textContent: "⛶ Expand" });
    exp.addEventListener("click", () => openCardModal(f));
    bar.append(exp);
  }

  wrap.append(bar, box);
  return wrap;
}

// A multi-part record -> [{url,colorIndex,name}] for createAssembly, else null.
function recordParts(f) {
  const parts = f.parts || [];
  if (parts.length <= 1) return null;
  return parts.map((p, i) => ({
    url: VulcanAPI.asset(p.view_stl || p.stl),
    colorIndex: p.color_index != null ? p.color_index : i,
    name: p.name,
  }));
}

function mountRecordViewer(target, f) {
  const opts = { token: founderToken(), fallbackImg: VulcanAPI.asset(f.preview_png) };
  const parts = recordParts(f);
  return parts
    ? Vulcan3D.createAssembly(target, parts, opts)
    : Vulcan3D.create(target, VulcanAPI.asset(f.view_stl || f.stl), opts);
}

function openCardModal(f) {
  const modal = document.getElementById("viewer-modal");
  modal.hidden = false;
  if (dashModal) dashModal.dispose();
  mountRecordViewer(document.getElementById("viewer-modal-stage"), f).then((v) => {
    dashModal = v;
  });
}

document.getElementById("viewer-modal-close").addEventListener("click", () => {
  document.getElementById("viewer-modal").hidden = true;
  if (dashModal) { dashModal.dispose(); dashModal = null; }
});

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
  if (dfm.connected !== undefined) {
    bits.push(dfm.connected ? "1 body ✓" : `${dfm.body_count} PIECES ✗`);
  }
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

// One-line summary of the winning candidate's visual critique (M10a Feature 1).
// Distinguishes a real score from "disabled" vs "enabled but skipped" so a 0.7-
// default score isn't mistaken for a genuine critique (audit finding 2).
function critiqueLine(record) {
  const critique = record.critique;
  const bits = [];
  if (critique && critique.matches_request !== undefined && critique.matches_request !== null) {
    const pct = Math.round(Number(critique.matches_request) * 100);
    bits.push(`visual match ${pct}%`);
    const defects = (critique.defects || []).filter(Boolean);
    if (defects.length) bits.push(`defects: ${defects.join("; ")}`);
  } else if (record.critique_enabled === false) {
    bits.push("visual critique: disabled");
  } else {
    bits.push("visual critique: enabled but not scored");
  }
  if (record.score !== undefined && record.score !== null) {
    bits.push(`overall score ${Number(record.score).toFixed(2)}`);
  }
  return bits.join(" · ");
}

// Dimensional-contract summary: which length params were verified to actually
// drive the geometry (M10a Feature 3).
function dimContractLine(dim) {
  if (!dim) return null;
  if (dim.dead && dim.dead.length) return `dead params: ${dim.dead.join(", ")} ✗`;
  const checked = (dim.checked || []).length;
  if (!checked) return "no length params probed";
  return `all ${checked} length param(s) verified to drive geometry ✓`;
}

// Best-of-N provenance: every evaluated candidate with its stage + scores, the
// winner marked (M10a Feature 2).
function candidatesBlock(candidates) {
  if (!candidates || candidates.length <= 1) return null;
  const rows = candidates.map((c, i) => {
    const crit =
      c.critique && c.critique.matches_request != null
        ? `${Math.round(Number(c.critique.matches_request) * 100)}%`
        : "—";
    return el("tr", { className: c.winner ? "candidate-winner" : "" }, [
      el("td", { textContent: c.winner ? `#${i + 1} ★` : `#${i + 1}` }),
      el("td", { textContent: c.stage }),
      el("td", { textContent: c.score != null ? Number(c.score).toFixed(2) : "—" }),
      el("td", { textContent: crit }),
    ]);
  });
  const table = el("table", { className: "review-candidates" }, [
    el("tr", {}, [
      el("th", {}, "cand"),
      el("th", {}, "stage"),
      el("th", {}, "score"),
      el("th", {}, "visual"),
    ]),
    ...rows,
  ]);
  return el("details", {}, [
    el("summary", {}, `Best-of-${candidates.length} candidates`),
    table,
  ]);
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

  // How the customer wants it delivered (chosen at submit, before this review).
  const deliver = record.fulfillment === "ship" ? "🚚 Ship it to the customer" : "⬇ Send the files";
  card.append(
    el("p", { className: "review-fulfillment" }, [el("strong", {}, "Deliver: "), deliver]),
  );

  if (record.assumptions && record.assumptions.length) {
    card.append(
      el("p", { className: "review-assumptions" }, [
        el("strong", {}, "Assumptions: "),
        record.assumptions.join(" "),
      ])
    );
  }

  card.append(el("p", { className: "review-dfm" }, [el("strong", {}, "DFM: "), dfmLine(record.dfm)]));

  // Generation quality (M10a): visual critique + dimensional contract.
  card.append(
    el("p", { className: "review-critique" }, [
      el("strong", {}, "Quality: "),
      critiqueLine(record),
    ])
  );
  const dimLine = dimContractLine(record.dim_contract);
  if (dimLine) {
    card.append(
      el("p", { className: "review-dimcontract" }, [el("strong", {}, "Dimensions: "), dimLine])
    );
  }
  const cands = candidatesBlock(record.candidates);
  if (cands) card.append(cands);

  // The part: dimensioned render, the in-photo composite (with/without model),
  // and an interactive 3D viewer — the founder can fly around the model.
  if (record.files) card.append(buildCardView(record));

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
  // freeform design — pending (to inspect before approving) or approved. A
  // multi-part assembly offers each piece's own STEP/3MF/STL.
  if (record.files) {
    const f = record.files;
    // [{name, {step,threemf,stl urls}}] — one group per part (or one for a
    // single-part design using the flat top-level keys).
    const groups =
      f.parts && f.parts.length > 1
        ? f.parts.map((p) => ({ label: p.name, files: p }))
        : [{ label: null, files: f }];
    const wrap = el("div", { className: "review-downloads" }, []);
    for (const g of groups) {
      if (g.label) wrap.append(el("span", { className: "review-part-name" }, `${g.label}: `));
      for (const [label, key] of [["STEP", "step"], ["3MF", "threemf"], ["STL", "stl"]]) {
        const url = g.files[key];
        if (!url) continue;
        const stem = g.label ? `${record.design_id}-${g.label}` : record.design_id;
        const name = `${stem}-${key}${url.slice(url.lastIndexOf("."))}`;
        const btn = el("button", { type: "button", className: "download-btn", textContent: label }, []);
        btn.addEventListener("click", () => downloadFile(url, name));
        wrap.append(btn);
      }
    }
    if (wrap.childNodes.length) {
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
    const resp = await VulcanAPI.submitVerdict(designId, { verdict, note: note || null }, founderToken());
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
    const resp = await VulcanAPI.reviewQueue(filterEl.value);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const records = await resp.json();
    if (dashViewer) { dashViewer.dispose(); dashViewer = null; } // cards are about to be replaced
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
