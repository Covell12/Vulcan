// "Start with a photo" flow (the complete single loop): upload photo(s) +
// optional freehand annotation + text -> POST /intents -> render questions as
// overlays on the photo (with mm/cm/in unit selectors) -> POST
// /intents/{id}/answers -> once ready_for_design, "Generate my part" calls
// POST /intents/{id}/design (the join) and renders the annotated preview, a
// param summary table (value + source), and the STEP/3MF/STL downloads — the
// user never touches the raw template form.

const photoInput = document.getElementById("photo-input");
const annotateWrap = document.getElementById("annotate-wrap");
const clearAnnotationBtn = document.getElementById("clear-annotation-btn");
const intentText = document.getElementById("intent-text");
const submitIntentBtn = document.getElementById("submit-intent-btn");
const intentStatusEl = document.getElementById("intent-status");

const questionsPanel = document.getElementById("questions-panel");
const intentDescriptionEl = document.getElementById("intent-description");
const overlayWrap = document.getElementById("overlay-wrap");
const overlayImg = document.getElementById("overlay-img");
const overlaySvg = document.getElementById("overlay-svg");
const questionUnits = document.getElementById("question-units");
const questionsList = document.getElementById("questions-list");
const submitAnswersBtn = document.getElementById("submit-answers-btn");
const answersStatusEl = document.getElementById("answers-status");

const resultPanel = document.getElementById("result-panel");
const resultStatusLine = document.getElementById("result-status-line");
const intentJsonEl = document.getElementById("intent-json");
const generatePartBtn = document.getElementById("generate-part-btn");
const regenerateBtn = document.getElementById("regenerate-btn");
const generateStatusEl = document.getElementById("generate-status");
const designResult = document.getElementById("design-result");
const designPreviewImg = document.getElementById("design-preview-img");
const compositeFigure = document.getElementById("composite-figure");
const designCompositeImg = document.getElementById("design-composite-img");
const designParamsTable = document.getElementById("design-params-table");
const designDownloads = document.getElementById("design-downloads");

const viewer3dPart = document.getElementById("viewer3d-part");
const partToggle3d = document.getElementById("part-toggle-3d");
const partToggleDims = document.getElementById("part-toggle-dims");
const partExpandBtn = document.getElementById("part-expand");
const compositeToggleWith = document.getElementById("composite-toggle-with");
const compositeToggleWithout = document.getElementById("composite-toggle-without");
const viewerModal = document.getElementById("viewer-modal");
const viewerModalStage = document.getElementById("viewer-modal-stage");
const viewerModalClose = document.getElementById("viewer-modal-close");

let partViewer = null; // the inline 3D viewer handle (disposed on re-render)
let modalViewer = null; // the expanded 3D viewer handle
let currentStlUrl = null; // STL url for the current design (for the 3D viewer)
let compositeSources = { with: null, without: null }; // composite vs plain photo

const freeformPanel = document.getElementById("freeform-panel");
const freeformBtn = document.getElementById("freeform-btn");
const freeformStatusEl = document.getElementById("freeform-status");
const reviewBanner = document.getElementById("review-banner");
const reviewBannerText = document.getElementById("review-banner-text");

const freeformOverride = document.getElementById("freeform-override");
const freeformOverrideBtn = document.getElementById("freeform-override-btn");
const freeformOverrideStatus = document.getElementById("freeform-override-status");
const freeformRecommendNote = document.getElementById("freeform-recommend-note");

let selectedPhotos = [];
let annItems = []; // one per uploaded photo: { index, canvas, img, url, points }
let firstPhotoUrl = null; // object URL of photo 0 (drives the overlay/composite)
let drawing = null; // the annotate record currently being drawn on
let currentIntent = null;

function setIntentStatus(message, isError) {
  intentStatusEl.textContent = message;
  intentStatusEl.classList.toggle("error", Boolean(isError));
}

function setAnswersStatus(message, isError) {
  answersStatusEl.textContent = message;
  answersStatusEl.classList.toggle("error", Boolean(isError));
}

function setGenerateStatus(message, isError) {
  generateStatusEl.textContent = message;
  generateStatusEl.classList.toggle("error", Boolean(isError));
}

// --- Photo upload + freehand annotation on ANY photo -------------------
// Every uploaded photo is shown and drawable; each keeps its own strokes. The
// part is only placed into the FIRST photo (the composite), but drawing on the
// others still gives the vision model context about what you mean.

function revokePhotoUrls() {
  for (const it of annItems) if (it.url) URL.revokeObjectURL(it.url);
}

photoInput.addEventListener("change", () => {
  revokePhotoUrls();
  selectedPhotos = Array.from(photoInput.files || []).slice(0, 3);
  annItems = [];
  firstPhotoUrl = null;
  annotateWrap.innerHTML = "";
  questionsPanel.hidden = true;
  resultPanel.hidden = true;

  if (selectedPhotos.length === 0) {
    annotateWrap.hidden = true;
    clearAnnotationBtn.hidden = true;
    return;
  }

  selectedPhotos.forEach((photo, index) => {
    const url = URL.createObjectURL(photo);
    if (index === 0) firstPhotoUrl = url;

    const item = document.createElement("div");
    item.className = "annotate-item";
    const badge = document.createElement("span");
    badge.className = "annotate-badge";
    badge.textContent = index === 0 ? "① part goes here" : `photo ${index + 1}`;
    const img = document.createElement("img");
    img.className = "annotate-photo";
    img.alt = `Uploaded photo ${index + 1}`;
    const canvas = document.createElement("canvas");
    canvas.className = "annotate-canvas";

    const rec = { index, canvas, img, url, points: [] };
    img.onload = () => {
      canvas.width = img.clientWidth;
      canvas.height = img.clientHeight;
      drawStroke(rec);
    };
    img.src = url;
    attachDrawing(rec);
    item.append(badge, img, canvas);
    annotateWrap.appendChild(item);
    annItems.push(rec);
  });
  annotateWrap.hidden = false;
  clearAnnotationBtn.hidden = false;
});

function drawStroke(rec) {
  if (!rec.canvas.width || !rec.canvas.height) return; // not sized (pre-load) yet
  const ctx = rec.canvas.getContext("2d");
  ctx.clearRect(0, 0, rec.canvas.width, rec.canvas.height);
  if (!rec.points.length) return;
  ctx.strokeStyle = "#ff8b34";
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = "rgba(255, 106, 26, 0.9)";
  ctx.shadowBlur = 6;
  ctx.beginPath();
  rec.points.forEach(([nx, ny], i) => {
    const x = nx * rec.canvas.width;
    const y = ny * rec.canvas.height;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function attachDrawing(rec) {
  // Normalized [0,1] point, or null if the canvas isn't laid out yet (before the
  // image loads its box has zero size — dividing by it would give NaN/Infinity).
  const pt = (e) => {
    const r = rec.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return [
      Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    ];
  };
  rec.canvas.addEventListener("pointerdown", (e) => {
    const p = pt(e);
    if (!p) return;
    drawing = rec;
    rec.pointerId = e.pointerId;
    rec.points = [p];
    drawStroke(rec);
    rec.canvas.setPointerCapture && rec.canvas.setPointerCapture(e.pointerId);
  });
  rec.canvas.addEventListener("pointermove", (e) => {
    if (drawing !== rec) return;
    const p = pt(e);
    if (!p) return;
    rec.points.push(p);
    drawStroke(rec);
  });
}
window.addEventListener("pointerup", () => {
  if (drawing && drawing.pointerId != null && drawing.canvas.releasePointerCapture) {
    try {
      drawing.canvas.releasePointerCapture(drawing.pointerId);
    } catch (e) {
      /* already released */
    }
  }
  drawing = null;
});

clearAnnotationBtn.addEventListener("click", () => {
  for (const it of annItems) {
    it.points = [];
    drawStroke(it);
  }
});

// --- Global unit toggle (mm / cm / in) for the whole question set ------
function syncUnitToggle() {
  if (!questionUnits) return;
  for (const b of questionUnits.querySelectorAll("[data-unit]")) {
    b.classList.toggle("active", b.dataset.unit === getSessionUnit());
  }
}
function applySessionUnit(u) {
  setSessionUnit(u);
  for (const sel of document.querySelectorAll(".unit-select")) {
    sel.value = getSessionUnit();
    sel.dispatchEvent(new Event("refresh-dual"));
  }
  syncUnitToggle();
}
if (questionUnits) {
  questionUnits.addEventListener("click", (e) => {
    const b = e.target.closest("[data-unit]");
    if (b) applySessionUnit(b.dataset.unit);
  });
}

// --- Zoom + pan for an image (wheel to zoom toward the cursor, drag to pan
// when zoomed, double-click to reset). Transforms every passed element so an
// image + its SVG overlay stay locked together. ---
function makeZoomable(container, elements) {
  if (!container || container._zoomable) return;
  container._zoomable = true;
  const els = (Array.isArray(elements) ? elements : [elements]).filter(Boolean);
  let scale = 1;
  let tx = 0;
  let ty = 0;
  let drag = null;
  container.classList.add("zoomable");
  const apply = () => {
    const t = `translate(${tx}px, ${ty}px) scale(${scale})`;
    for (const e of els) {
      e.style.transformOrigin = "0 0";
      e.style.transform = t;
    }
    container.classList.toggle("zoomed", scale > 1.01);
  };
  const clamp = () => {
    const w = container.clientWidth;
    const h = container.clientHeight;
    tx = Math.min(0, Math.max(w - w * scale, tx));
    ty = Math.min(0, Math.max(h - h * scale, ty));
  };
  container.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const r = container.getBoundingClientRect();
      const px = e.clientX - r.left;
      const py = e.clientY - r.top;
      const ns = Math.min(6, Math.max(1, scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      tx = px - (px - tx) * (ns / scale);
      ty = py - (py - ty) * (ns / scale);
      scale = ns;
      if (scale <= 1.01) {
        scale = 1;
        tx = 0;
        ty = 0;
      }
      clamp();
      apply();
    },
    { passive: false },
  );
  container.addEventListener("pointerdown", (e) => {
    if (scale <= 1.01) return; // let clicks (e.g. chips) through at 1x
    drag = { x: e.clientX - tx, y: e.clientY - ty };
    container.setPointerCapture && container.setPointerCapture(e.pointerId);
  });
  container.addEventListener("pointermove", (e) => {
    if (!drag) return;
    tx = e.clientX - drag.x;
    ty = e.clientY - drag.y;
    clamp();
    apply();
  });
  container.addEventListener("pointerup", (e) => {
    if (container.releasePointerCapture) {
      try {
        container.releasePointerCapture(e.pointerId);
      } catch (err) {
        /* already released */
      }
    }
    drag = null;
  });
  container.addEventListener("dblclick", () => {
    scale = 1;
    tx = 0;
    ty = 0;
    apply();
  });
}

// --- Submit photo(s) + text -> POST /intents ---------------------------

submitIntentBtn.addEventListener("click", async () => {
  if (selectedPhotos.length === 0) {
    setIntentStatus("Choose at least one photo first.", true);
    return;
  }
  if (!intentText.value.trim()) {
    setIntentStatus("Describe what you need first.", true);
    return;
  }

  submitIntentBtn.disabled = true;
  setIntentStatus("Analyzing…", false);
  questionsPanel.hidden = true;
  resultPanel.hidden = true;

  const formData = new FormData();
  for (const photo of selectedPhotos) formData.append("photos", photo);
  formData.append("text", intentText.value.trim());
  const annotation = annItems
    .map((it) => ({ photo_index: it.index, points: it.points }))
    .filter((a) => a.points.length > 1);
  if (annotation.length) formData.append("annotation", JSON.stringify(annotation));

  try {
    const response = await VulcanAPI.createIntent(formData);
    if (!response.ok) throw new Error(await describeFetchError(response));
    currentIntent = await response.json();
    if (currentIntent.freeform_available && !currentIntent.template_id) {
      // No template fits — offer the custom-design path instead of empty questions.
      questionsPanel.hidden = true;
      resultPanel.hidden = true;
      renderFreeform();
    } else {
      freeformPanel.hidden = true;
      renderQuestions();
      renderResult();
    }
    setIntentStatus(`Got it — intent ${currentIntent.intent_id}.`, false);
  } catch (err) {
    setIntentStatus(`Error: ${errorText(err)}`, true);
  } finally {
    submitIntentBtn.disabled = false;
  }
});

// --- Questions panel: overlays + inputs ---------------------------------

function questionInputId(questionId) {
  return `intent-q-${questionId}`;
}

function renderQuestions() {
  questionsPanel.hidden = false;
  intentDescriptionEl.textContent = currentIntent.description || "";
  renderFreeformOptions();

  overlayImg.src = firstPhotoUrl || "";
  questionsList.innerHTML = "";
  overlaySvg.innerHTML = "";
  dimChips = {};

  // Show the global unit toggle when there's at least one measurement to enter,
  // reflecting the remembered session unit.
  const hasMeasure = (currentIntent.questions || []).some((q) => q.kind === "measure_mm");
  questionUnits.hidden = !hasMeasure;
  syncUnitToggle();

  const dimsByName = Object.fromEntries((currentIntent.dimensions || []).map((d) => [d.name, d]));
  const questions = currentIntent.questions || [];

  // A dim can have more than one question targeting it (e.g. its original
  // question plus a server-added "reask-<dim>", or a stray "confirm"). Render
  // only ONE input row per dim so cards/inputs don't duplicate — and prefer the
  // measure_mm question, so a confirm that happens to sort first never hides the
  // field the user needs to enter a real measurement.
  const preferredByDim = {};
  for (const q of questions) {
    if (!q.dim_name) continue;
    const cur = preferredByDim[q.dim_name];
    if (!cur || (q.kind === "measure_mm" && cur.kind !== "measure_mm")) {
      preferredByDim[q.dim_name] = q;
    }
  }

  // Collect the overlays to draw (one per dim, the preferred question's).
  overlayDrawList = [];
  const overlaidDims = new Set();
  for (const q of questions) {
    if (!q.overlay || q.overlay.photo_index !== 0) continue;
    if (q.dim_name) {
      if (overlaidDims.has(q.dim_name)) continue;
      if (preferredByDim[q.dim_name] && preferredByDim[q.dim_name] !== q) continue;
      overlaidDims.add(q.dim_name);
    }
    overlayDrawList.push({ q, dim: dimsByName[q.dim_name] });
  }
  scheduleOverlayDraw();
  makeZoomable(overlayWrap, [overlayImg, overlaySvg]);

  const renderedDims = new Set();
  for (const question of questions) {
    if (question.dim_name) {
      if (renderedDims.has(question.dim_name)) continue;
      if (preferredByDim[question.dim_name] !== question) continue; // wait for the preferred one
      renderedDims.add(question.dim_name);
    }
    questionsList.appendChild(renderQuestionRow(question, dimsByName[question.dim_name]));
  }
}

// --- Dimension-line overlays with live label chips (Part B) --------------
// The photo becomes a live dimension drawing: each critical measurement is a
// dimension line / ellipse over the photo with a label chip ON it, whose state
// (?, ~estimate, measured ✓) mirrors the dimension and updates as the user types.
const SVG_NS = "http://www.w3.org/2000/svg";
let dimChips = {}; // question_id -> { group, rect, text, mid:[x,y], dimension }
let overlayDrawList = [];

function svgEl(name, attrs) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function fmtMm(v) {
  if (v == null || !Number.isFinite(Number(v))) return "";
  return (Math.round(Number(v) * 10) / 10).toString();
}

// HONESTY (Part B rule 6): "?" for unanswered, "~X" for a vision/depth estimate
// (never without the ~), "X ✓" ONLY for user_measured, mismatch shows both.
function dimChipState(dimension) {
  if (!dimension) return { text: "?", cls: "chip-unanswered" };
  const cc = dimension.cross_check;
  if (cc && cc.status === "mismatch_reask") {
    return { text: `${fmtMm(dimension.value_mm)} vs ~${fmtMm(cc.depth_value_mm)}mm`, cls: "chip-mismatch" };
  }
  if (dimension.source === "user_measured" && dimension.value_mm != null) {
    return { text: `${fmtMm(dimension.value_mm)}mm ✓`, cls: "chip-measured" };
  }
  if (dimension.value_mm != null) {
    return { text: `~${fmtMm(dimension.value_mm)}mm`, cls: "chip-estimate" };
  }
  return { text: "?", cls: "chip-unanswered" };
}

function scheduleOverlayDraw() {
  if (overlayImg.complete && overlayImg.naturalWidth) drawAllOverlays();
  else overlayImg.addEventListener("load", drawAllOverlays, { once: true });
}

// A short human name for a measurement — the SAME label shown on the question
// form, so an on-photo line and its form field read as the same thing. Prefers
// a backend-supplied label, else prettifies the template param name.
function dimLabel(question) {
  if (question && question.label) return question.label;
  const n = question && question.dim_name;
  if (!n) return "";
  return n
    .replace(/_(mm|deg|cm|in)$/i, "")
    .replace(/_/g, " ")
    .trim()
    .replace(/^\w/, (c) => c.toUpperCase());
}

// defs for the overlays: a soft Gaussian blur used for cast shadows (so a line
// looks like it sits ON the surface), plus outward-pointing arrow caps. Markers
// are in userSpaceOnUse px so they don't scale with stroke width.
const OVERLAY_DEFS = `
  <filter id="dim-cast" x="-50%" y="-50%" width="200%" height="200%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="2.4"/>
  </filter>
  <marker id="dim-arrow-end" markerUnits="userSpaceOnUse" markerWidth="16" markerHeight="16" refX="11" refY="8" orient="auto">
    <path d="M2,2 L13,8 L2,14 Z" class="dim-arrowhead"/>
  </marker>
  <marker id="dim-arrow-start" markerUnits="userSpaceOnUse" markerWidth="16" markerHeight="16" refX="5" refY="8" orient="auto-start-reverse">
    <path d="M2,2 L13,8 L2,14 Z" class="dim-arrowhead"/>
  </marker>`;

function drawAllOverlays() {
  const W = overlayImg.clientWidth || overlayImg.naturalWidth || 1;
  const H = overlayImg.clientHeight || overlayImg.naturalHeight || 1;
  overlaySvg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const defs = svgEl("defs");
  defs.innerHTML = OVERLAY_DEFS;
  overlaySvg.innerHTML = "";
  overlaySvg.appendChild(defs);
  dimChips = {};
  for (const { q, dim } of overlayDrawList) drawDimOverlay(q, dim, W, H);
  // Sync each chip with any value already typed (e.g. after a re-render).
  for (const { q } of overlayDrawList) updateChipFromInput(q.question_id);
}

// --- 3D-looking overlays: lines drawn INTO the photo -----------------------
// Rather than a flat ruler, each dimension is rendered like it's painted ONTO
// the surface: a gently bowed path (so it curves with the object), a layered
// "tube" stroke (dark base + molten body + bright highlight) for roundness, a
// soft cast shadow so it sits on the surface, foreshortened ticks, and pin-like
// end nubs. Diameters wrap the round feature like a band around a cylinder.

// A gently bowed quadratic path between two points, so the line reads as lying
// on a (curved) surface instead of floating flat over the photo.
function bowedPath(p0, p1, bow) {
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1], len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len, ny = dx / len; // unit perpendicular
  const cx = (p0[0] + p1[0]) / 2 + nx * len * bow;
  const cy = (p0[1] + p1[1]) / 2 + ny * len * bow;
  return { d: `M ${p0[0]} ${p0[1]} Q ${cx} ${cy} ${p1[0]} ${p1[1]}`, ctrl: [cx, cy], len };
}

// Position + unit tangent on a quadratic bezier at parameter t (for ticks).
function quadAt(p0, c, p1, t) {
  const u = 1 - t;
  const x = u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0];
  const y = u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1];
  let tx = 2 * u * (c[0] - p0[0]) + 2 * t * (p1[0] - c[0]);
  let ty = 2 * u * (c[1] - p0[1]) + 2 * t * (p1[1] - c[1]);
  const tl = Math.hypot(tx, ty) || 1;
  return { x, y, tx: tx / tl, ty: ty / tl };
}

// Layered strokes that read as a rounded, glowing 3D tube on the surface, with
// a soft blurred cast shadow offset down-right (as if lit from the upper left).
function drawTube(g, d, dashed) {
  g.appendChild(svgEl("path", { d, class: "dim-cast", filter: "url(#dim-cast)", transform: "translate(2.4 3.2)" }));
  g.appendChild(svgEl("path", { d, class: dashed ? "dim-tube-base dim-depth" : "dim-tube-base" }));
  g.appendChild(svgEl("path", { d, class: dashed ? "dim-tube-body dim-depth" : "dim-tube-body" }));
  g.appendChild(svgEl("path", { d, class: "dim-tube-hi", transform: "translate(-0.6 -1)" }));
}

// A little "pin" stuck into the surface at an endpoint (shadow + bead + glint).
function drawNub(g, p) {
  g.appendChild(svgEl("ellipse", { cx: p[0] + 1.6, cy: p[1] + 3, rx: 4.6, ry: 2.4, class: "dim-nub-shadow", filter: "url(#dim-cast)" }));
  g.appendChild(svgEl("circle", { cx: p[0], cy: p[1], r: 3.6, class: "dim-nub" }));
  g.appendChild(svgEl("circle", { cx: p[0] - 0.9, cy: p[1] - 1.2, r: 1.3, class: "dim-nub-hi" }));
}

// A full dimension line between two pixel points, drawn as a 3D tube on the
// surface with foreshortened ticks, outward arrow caps and end nubs.
function drawDimLine(g, p0, p1, dashed) {
  const bow = dashed ? 0.12 : 0.06; // depth lines recede harder, so bow more
  const path = bowedPath(p0, p1, bow);
  const n = Math.min(24, Math.max(6, Math.round(path.len / 22)));
  for (let i = 1; i < n; i++) {
    const t = i / n;
    const s = quadAt(p0, path.ctrl, p1, t);
    // ticks perpendicular to the local tangent; shrink toward the far end of a
    // depth line so they read as receding along the surface.
    const h = (i % 5 === 0 ? 6.5 : 3.5) * (dashed ? 1 - 0.45 * t : 1);
    g.appendChild(svgEl("line", { x1: s.x + s.ty * h, y1: s.y - s.tx * h, x2: s.x - s.ty * h, y2: s.y + s.tx * h, class: "dim-tick" }));
  }
  drawTube(g, path.d, dashed);
  // Transparent stroke carrying the arrow markers (oriented along the curve).
  g.appendChild(svgEl("path", { d: path.d, class: "dim-arrowline", "marker-start": "url(#dim-arrow-start)", "marker-end": "url(#dim-arrow-end)" }));
  drawNub(g, p0);
  drawNub(g, p1);
}

// A diameter, drawn like a band wrapping a cylinder: a faint dashed FAR arc
// (behind the object) + a bright NEAR arc + the measured diameter as a tube.
function drawDimRing(g, cx, cy, rx, ry, rot) {
  const ring = svgEl("g", { transform: `rotate(${rot} ${cx} ${cy})` });
  const l = [cx - rx, cy], r = [cx + rx, cy];
  const far = `M ${l[0]} ${l[1]} A ${rx} ${ry} 0 0 1 ${r[0]} ${r[1]}`; // top: behind
  const near = `M ${l[0]} ${l[1]} A ${rx} ${ry} 0 0 0 ${r[0]} ${r[1]}`; // bottom: in front
  ring.appendChild(svgEl("path", { d: far, class: "dim-ring-back" }));
  ring.appendChild(svgEl("path", { d: near, class: "dim-cast", filter: "url(#dim-cast)", transform: "translate(2 3)" }));
  ring.appendChild(svgEl("path", { d: near, class: "dim-ring-front" }));
  g.appendChild(ring);
  // The measured diameter across the front (endpoints rotated with the ellipse).
  const a = (rot * Math.PI) / 180, ca = Math.cos(a), sa = Math.sin(a);
  drawDimLine(g, [cx - rx * ca, cy - rx * sa], [cx + rx * ca, cy + rx * sa], false);
}

function drawDimOverlay(question, dimension, W, H) {
  const ov = question.overlay;
  const kind = ov.kind || (ov.shape === "circle" ? "dim_ellipse" : "dim_line");
  const g = svgEl("g", { class: "dim-overlay" });
  let mid;
  let prefix = "";

  if (kind === "dim_ellipse") {
    const c = ov.center || (ov.points && ov.points[0]) || [0.5, 0.5];
    const cx = c[0] * W, cy = c[1] * H;
    const rx = (ov.rx != null ? ov.rx : 0.04) * W;
    const ry = (ov.ry != null ? ov.ry : ov.rx != null ? ov.rx : 0.04) * W;
    drawDimRing(g, cx, cy, rx, ry, ov.rotation || 0);
    mid = [cx, cy - ry - 18];
    prefix = "⌀ "; // diameter symbol
  } else {
    const pts = ov.points && ov.points.length >= 2 ? ov.points : [[0.4, 0.5], [0.6, 0.5]];
    const p0 = [pts[0][0] * W, pts[0][1] * H];
    const p1 = [pts[pts.length - 1][0] * W, pts[pts.length - 1][1] * H];
    drawDimLine(g, p0, p1, kind === "dim_depth");
    const dx = p1[0] - p0[0], dy = p1[1] - p0[1], len = Math.hypot(dx, dy) || 1;
    // Lift the chip off the (bowed) line so it doesn't cover the ticks.
    mid = [(p0[0] + p1[0]) / 2 + (-dy / len) * 22, (p0[1] + p1[1]) / 2 + (dx / len) * 22];
  }

  // Two-line chip: the measurement NAME (from the form) + its live value/state.
  const chipG = svgEl("g", { class: "dim-chip" });
  const rect = svgEl("rect", { rx: 6, ry: 6, class: "dim-chip-rect" });
  const nameText = svgEl("text", { class: "dim-chip-name", "text-anchor": "middle" });
  const valueText = svgEl("text", { class: "dim-chip-text", "text-anchor": "middle" });
  chipG.appendChild(rect);
  chipG.appendChild(nameText);
  chipG.appendChild(valueText);
  g.appendChild(chipG);
  overlaySvg.appendChild(g);

  const chip = { group: chipG, rect, nameText, valueText, mid, dimension, prefix, name: dimLabel(question) };
  dimChips[question.question_id] = chip;
  // Click a chip to focus (edit) its input — the other half of the two-way bind.
  chipG.addEventListener("click", () => {
    const input = document.getElementById(questionInputId(question.question_id));
    if (input) {
      input.focus();
      input.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });

  const state = dimChipState(dimension);
  setChip(chip, state.text, state.cls);
}

// Size + place the two-line chip (name over value) and color it by state.
function setChip(chip, valueStr, cls) {
  chip.nameText.textContent = chip.name || "";
  chip.valueText.textContent = (chip.prefix || "") + (valueStr || "?");
  chip.group.setAttribute("class", `dim-chip ${cls || ""}`);
  const padX = 9, padY = 6, gap = 3;
  const nb = chip.name ? chip.nameText.getBBox() : { width: 0, height: 0 };
  const vb = chip.valueText.getBBox();
  const nameH = chip.name ? nb.height : 0;
  const w = Math.max(nb.width, vb.width, 10) + padX * 2;
  const h = nameH + (chip.name ? gap : 0) + vb.height + padY * 2;
  const [mx, my] = chip.mid;
  const top = my - h / 2;
  chip.rect.setAttribute("x", mx - w / 2);
  chip.rect.setAttribute("y", top);
  chip.rect.setAttribute("width", w);
  chip.rect.setAttribute("height", h);
  if (chip.name) {
    chip.nameText.setAttribute("x", mx);
    chip.nameText.setAttribute("y", top + padY + nameH * 0.78);
  }
  chip.valueText.setAttribute("x", mx);
  chip.valueText.setAttribute("y", top + padY + nameH + (chip.name ? gap : 0) + vb.height * 0.78);
}

// Live-update a chip from what's typed in its input (pending, not yet submitted).
function updateChipFromInput(questionId) {
  const chip = dimChips[questionId];
  if (!chip) return;
  const mm = collectMm(questionId); // converts cm/in -> mm; null if empty
  if (mm == null) {
    const s = dimChipState(chip.dimension);
    setChip(chip, s.text, s.cls);
  } else {
    // Pending: show the typed value with NO ✓ (it isn't user_measured yet).
    setChip(chip, `${fmtMm(mm)}mm`, "chip-pending");
  }
}

// The bounds (hard min/max + soft recommended range + hard_reason) the template
// exposes for a question's dimension, or null.
function boundsFor(question) {
  if (!question || !question.dim_name) return null;
  return (currentIntent.param_bounds || {})[question.dim_name] || null;
}

// The soft "recommended" range (falls back to the hard range if none was given).
function recRange(b) {
  if (!b) return { lo: null, hi: null };
  return {
    lo: b.recommended_min != null ? b.recommended_min : b.minimum,
    hi: b.recommended_max != null ? b.recommended_max : b.maximum,
  };
}

// Why a value CAN'T be built (a hard limit / rule), or null if it's allowed.
// Values merely outside the *recommended* range return null — they're allowed,
// just nudged; only the hard min/max (with the template's reason) block.
function hardBlockReasonForBounds(b, mm) {
  if (mm == null) return null;
  if (mm <= 0) return "must be greater than 0.";
  if (!b) return null;
  if (b.minimum != null && mm < b.minimum - 1e-9) {
    return b.hard_reason || `can't go below ${trimNumber(b.minimum)} mm.`;
  }
  if (b.maximum != null && mm > b.maximum + 1e-9) {
    return (
      b.hard_reason ||
      `this template builds up to ${trimNumber(b.maximum)} mm — for something bigger, use “Design this custom instead”.`
    );
  }
  return null;
}
function hardBlockReason(question, mm) {
  return hardBlockReasonForBounds(boundsFor(question), mm);
}

// A measurement input with a mm/cm/in unit selector (default = remembered
// session unit), a live "8 in = 203.2 mm" dual display, and — when the template
// constrains this dimension — a RECOMMENDED range you can expand past (soft
// nudge), while genuinely-hard limits/rules stay blocked with their reason.
function appendMeasureField(parent, questionId, placeholder, bounds) {
  const wrap = document.createElement("div");
  wrap.className = "measure-field";

  const input = document.createElement("input");
  input.type = "number";
  input.step = "any";
  input.id = questionInputId(questionId);
  if (placeholder) input.placeholder = placeholder;

  const unit = document.createElement("select");
  unit.className = "unit-select";
  unit.id = `intent-unit-${questionId}`;
  for (const u of ["mm", "cm", "in"]) {
    const option = document.createElement("option");
    option.value = u;
    option.textContent = u;
    if (u === getSessionUnit()) option.selected = true;
    unit.appendChild(option);
  }

  const dual = document.createElement("span");
  dual.className = "dual-display";

  const rec = recRange(bounds);
  const baseHint =
    bounds && rec.lo != null && rec.hi != null
      ? `recommended ${trimNumber(rec.lo)}–${trimNumber(rec.hi)} mm — you can go beyond`
      : "";
  const hint = document.createElement("span");
  hint.className = "range-hint";
  hint.textContent = baseHint;

  // Native min/max = the HARD limits (so the browser only stops truly-hard
  // values), then classify: within recommended = quiet; past it but buildable =
  // a soft amber nudge (still allowed); past the hard limit = red + the reason.
  const applyBounds = () => {
    if (!bounds) return;
    const u = unit.value;
    if (bounds.minimum != null) input.min = trimNumber(fromMm(bounds.minimum, u));
    else input.removeAttribute("min");
    if (bounds.maximum != null) input.max = trimNumber(fromMm(bounds.maximum, u));
    else input.removeAttribute("max");

    const mm = collectMm(questionId);
    hint.classList.remove("error", "soft");
    input.classList.remove("out-of-range");
    if (mm == null) {
      hint.textContent = baseHint;
      return;
    }
    const hard = hardBlockReasonForBounds(bounds, mm);
    if (hard) {
      hint.textContent = hard;
      hint.classList.add("error");
      input.classList.add("out-of-range");
      return;
    }
    const soft =
      (rec.lo != null && mm < rec.lo - 1e-9) || (rec.hi != null && mm > rec.hi + 1e-9);
    if (soft) {
      hint.textContent = `${trimNumber(mm)} mm is outside the typical range — we'll still try to build it.`;
      hint.classList.add("soft");
    } else {
      hint.textContent = baseHint;
    }
  };

  // On every keystroke / unit change: dual display, the photo's dimension chip
  // (two-way binding), and range validation.
  const refresh = () => {
    dual.textContent = formatDual(input.value, unit.value);
    updateChipFromInput(questionId);
    applyBounds();
  };

  input.addEventListener("input", refresh);
  unit.addEventListener("refresh-dual", refresh);
  // Changing any field's unit changes the session unit for ALL fields (and the
  // global toggle), so the whole question set stays in one unit.
  unit.addEventListener("change", () => applySessionUnit(unit.value));

  wrap.appendChild(input);
  wrap.appendChild(unit);
  wrap.appendChild(dual);
  if (bounds) wrap.appendChild(hint);
  parent.appendChild(wrap);
  applyBounds();
}

// Read a measure_mm question's entered value, converted to mm via its unit
// selector. Returns null when empty/invalid.
function collectMm(questionId) {
  const input = document.getElementById(questionInputId(questionId));
  if (!input || input.value === "") return null;
  const unitSel = document.getElementById(`intent-unit-${questionId}`);
  const mm = toMm(input.value, unitSel ? unitSel.value : "mm");
  return Number.isFinite(mm) ? mm : null;
}

function renderQuestionRow(question, dimension) {
  const row = document.createElement("div");
  row.className = "question-row";

  // The measurement's NAME (same label as its on-photo chip), so the form field
  // and the line drawn into the image read as the same thing.
  const name = dimLabel(question);
  if (name) {
    const nameEl = document.createElement("p");
    nameEl.className = "question-name";
    nameEl.textContent = name;
    row.appendChild(nameEl);
  }

  const prompt = document.createElement("p");
  prompt.className = "question-prompt";
  prompt.textContent = question.prompt;
  row.appendChild(prompt);

  if (dimension && dimension.source === "user_measured") {
    const answered = document.createElement("p");
    answered.className = "question-answered";
    answered.textContent = `✓ confirmed: ${dimension.value_mm}mm`;
    row.appendChild(answered);
    return row;
  }

  // Cross-check mismatch: the typed value disagrees with the depth prior by
  // >20%. Show both values and a one-click "my measurement is right" override,
  // plus a field to enter a corrected value instead.
  const crossCheck = dimension && dimension.cross_check;
  if (crossCheck && crossCheck.status === "mismatch_reask") {
    const card = document.createElement("div");
    card.className = "mismatch-card";

    const msg = document.createElement("p");
    msg.className = "mismatch-msg";
    msg.textContent =
      `You entered ${dimension.value_mm}mm, but the photo suggests about ` +
      `${crossCheck.depth_value_mm}mm. Did you use the wrong units?`;
    card.appendChild(msg);

    appendMeasureField(card, question.question_id, "enter a corrected value", boundsFor(question));

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reconfirm-btn";
    btn.textContent = "Yes, my measurement is right";
    btn.addEventListener("click", () =>
      submitAnswers([{ question_id: question.question_id, measure_mm: dimension.value_mm }]),
    );
    card.appendChild(btn);

    row.appendChild(card);
    return row;
  }

  if (question.kind === "measure_mm") {
    let placeholder = "";
    if (dimension && dimension.source === "depth_inferred" && dimension.value_mm != null) {
      placeholder = `looks like ~${dimension.value_mm}mm — measure to confirm`;
    } else if (dimension && dimension.value_mm !== null && dimension.value_mm !== undefined) {
      placeholder = `assumed: ${dimension.value_mm}mm`;
    }
    appendMeasureField(row, question.question_id, placeholder, boundsFor(question));
  } else if (question.kind === "confirm") {
    const label = document.createElement("label");
    label.className = "checkbox-field";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = questionInputId(question.question_id);
    label.appendChild(input);
    const assumedValue = dimension ? `${dimension.value_mm}mm` : "this";
    label.appendChild(document.createTextNode(`Confirm ${assumedValue} is correct`));
    row.appendChild(label);
  } else if (question.kind === "choice") {
    const select = document.createElement("select");
    select.id = questionInputId(question.question_id);
    // Blank = accept the provider's suggestion (join uses suggested_value);
    // picking a value overrides it (recorded as the user's choice).
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = question.suggested_value
      ? `— use suggested: ${question.suggested_value}`
      : "—";
    select.appendChild(blank);
    for (const choice of question.choices || []) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      if (choice === question.chosen_value) option.selected = true; // reflect an earlier answer
      select.appendChild(option);
    }
    row.appendChild(select);
  } else {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "Please retake the photo and start over.";
    row.appendChild(note);
  }

  return row;
}

// Shared submit path for both the "Submit answers" button and the one-click
// mismatch re-confirm button.
async function submitAnswers(answers) {
  if (!currentIntent || answers.length === 0) {
    setAnswersStatus("Fill in at least one answer first.", true);
    return;
  }
  submitAnswersBtn.disabled = true;
  setAnswersStatus("Submitting…", false);
  try {
    const response = await VulcanAPI.submitAnswers(currentIntent.intent_id, { answers });
    if (!response.ok) throw new Error(await describeFetchError(response));
    currentIntent = await response.json();
    renderQuestions();
    renderResult();
    setAnswersStatus("Saved.", false);
  } catch (err) {
    setAnswersStatus(`Error: ${errorText(err)}`, true);
  } finally {
    submitAnswersBtn.disabled = false;
  }
}

submitAnswersBtn.addEventListener("click", () => {
  if (!currentIntent) return;

  const answers = [];
  const blocked = [];
  for (const question of currentIntent.questions || []) {
    const input = document.getElementById(questionInputId(question.question_id));
    if (!input) continue; // deduped / already-confirmed questions render no input

    if (question.kind === "measure_mm") {
      const mm = collectMm(question.question_id); // converts cm/in -> mm
      if (mm === null) continue;
      // Only HARD limits/rules block; a value past the recommended range is
      // allowed (the user can expand it). hardBlockReason returns null unless
      // there's a real reason it can't build.
      const reason = hardBlockReason(question, mm);
      if (reason) {
        blocked.push(`${dimLabel(question) || question.dim_name}: ${reason}`);
        continue;
      }
      answers.push({ question_id: question.question_id, measure_mm: mm });
    } else if (question.kind === "confirm" && input.checked) {
      answers.push({ question_id: question.question_id, confirm: true });
    } else if (question.kind === "choice" && input.value !== "") {
      answers.push({ question_id: question.question_id, choice: input.value });
    }
  }

  if (blocked.length) {
    setAnswersStatus(`Can't use these values — ${blocked.join("; ")}`, true);
    return;
  }

  submitAnswers(answers);
});

// --- Result panel: "Generate my part" -> the join endpoint --------------

// Delivery choice (files vs shipped), captured when the user submits — before
// review/fulfillment. Defaults to "files".
let fulfillment = "files";
const fulfillmentChoice = document.getElementById("fulfillment-choice");
if (fulfillmentChoice) {
  fulfillmentChoice.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fulfillment]");
    if (!b) return;
    fulfillment = b.dataset.fulfillment;
    for (const seg of fulfillmentChoice.querySelectorAll("[data-fulfillment]")) {
      seg.classList.toggle("active", seg === b);
    }
  });
}

function renderResult() {
  resultPanel.hidden = false;
  resultStatusLine.textContent =
    currentIntent.status === "ready_for_design"
      ? "All set — every critical dimension is confirmed."
      : `status: ${currentIntent.status}`;
  intentJsonEl.textContent = JSON.stringify(currentIntent, null, 2);
  generatePartBtn.disabled = currentIntent.status !== "ready_for_design" || !currentIntent.template_id;
  if (currentIntent.status !== "ready_for_design") designResult.hidden = true;
}

// --- Freeform (Track B): the custom-design path + the always-on override --

function renderFreeform() {
  // The PRIMARY freeform panel shows only when the request is in scope but no
  // template matched (freeform is then the only real path).
  const offer = currentIntent.freeform_available && !currentIntent.template_id;
  freeformPanel.hidden = !offer;
  if (currentIntent.freeform_error) {
    setStatusText(freeformStatusEl, currentIntent.freeform_error, true);
  } else if (!offer) {
    setStatusText(freeformStatusEl, "", false);
  }
}

// The always-available "Design this custom instead" override, shown inside the
// questions panel whenever a template DID match — with a recommend note when the
// router flagged the template as a poor fit (Part A).
function renderFreeformOptions() {
  const showOverride = currentIntent.freeform_available && !!currentIntent.template_id;
  freeformOverride.hidden = !showOverride;
  setStatusText(freeformOverrideStatus, "", false);

  const recommend = currentIntent.freeform_recommended && !!currentIntent.template_id;
  if (recommend) {
    const feats = currentIntent.unsupported_features || [];
    freeformRecommendNote.textContent = feats.length
      ? `Heads up: the closest template (${currentIntent.template_id}) can't do: ${feats.join(", ")}. A custom design is recommended.`
      : "Heads up: this is only a rough template match — a custom design may fit better.";
    freeformRecommendNote.hidden = false;
  } else {
    freeformRecommendNote.hidden = true;
  }
}

function setStatusText(el, message, isError) {
  el.textContent = message;
  el.classList.toggle("error", Boolean(isError));
}

async function runFreeform(statusEl, btn) {
  if (!currentIntent) return;
  btn.disabled = true;
  setStatusText(statusEl, "Designing your custom part… this can take a moment (generating code, building, and safety-checking it).", false);
  try {
    const response = await VulcanAPI.freeform(currentIntent.intent_id);
    if (!response.ok) throw new Error(await describeFetchError(response));
    currentIntent = await response.json();
    if (currentIntent.template_id) {
      setStatusText(statusEl, "Custom design ready — confirm the measurements below.", false);
      freeformPanel.hidden = true;
      renderQuestions();
      renderResult();
    } else {
      // Generation failed (logged to the demand log); show the honest message.
      setStatusText(statusEl, currentIntent.freeform_error || "We couldn't design this automatically.", true);
    }
  } catch (err) {
    setStatusText(statusEl, `Error: ${errorText(err)}`, true);
  } finally {
    btn.disabled = false;
  }
}

freeformBtn.addEventListener("click", () => runFreeform(freeformStatusEl, freeformBtn));
freeformOverrideBtn.addEventListener("click", () => runFreeform(freeformOverrideStatus, freeformOverrideBtn));

const SOURCE_BADGE = {
  measured: "measured ✓",
  chosen: "chosen ✓",
  suggested: "suggested ~",
  assumed: "estimate ~",
  default: "default",
};

// --- 3D model viewer + photo/part toggles -------------------------------

function setCompositeView(which) {
  const src = compositeSources[which];
  if (src) designCompositeImg.src = src;
  compositeToggleWith.classList.toggle("active", which === "with");
  compositeToggleWithout.classList.toggle("active", which === "without");
}

function mountPartViewer() {
  if (partViewer) {
    partViewer.dispose();
    partViewer = null;
  }
  if (!currentStlUrl || !window.Vulcan3D) return;
  Vulcan3D.create(viewer3dPart, currentStlUrl, {
    fallbackImg: designPreviewImg.src, // pending freeform STL is gated -> show the render
  }).then((v) => {
    partViewer = v;
  });
}

function showPartView(which) {
  const is3d = which === "3d";
  viewer3dPart.hidden = !is3d;
  designPreviewImg.hidden = is3d;
  partToggle3d.classList.toggle("active", is3d);
  partToggleDims.classList.toggle("active", !is3d);
  partExpandBtn.hidden = !is3d;
  if (is3d) mountPartViewer();
  else if (partViewer) {
    partViewer.dispose();
    partViewer = null;
  }
}

compositeToggleWith.addEventListener("click", () => setCompositeView("with"));
compositeToggleWithout.addEventListener("click", () => setCompositeView("without"));
partToggle3d.addEventListener("click", () => showPartView("3d"));
partToggleDims.addEventListener("click", () => showPartView("dims"));

partExpandBtn.addEventListener("click", () => {
  if (!currentStlUrl) return;
  viewerModal.hidden = false;
  if (modalViewer) modalViewer.dispose();
  Vulcan3D.create(viewerModalStage, currentStlUrl, { fallbackImg: designPreviewImg.src }).then((v) => {
    modalViewer = v;
  });
});
function closeModalViewer() {
  viewerModal.hidden = true;
  if (modalViewer) {
    modalViewer.dispose();
    modalViewer = null;
  }
}
viewerModalClose.addEventListener("click", closeModalViewer);
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !viewerModal.hidden) closeModalViewer();
});

function renderDesign(design) {
  designResult.hidden = false;

  // Freeform designs land in the founder review queue; CAD downloads are locked
  // until approved. Show an honest banner + the model's assumptions.
  if (design.review_status === "pending_review") {
    const notes = (design.assumptions || []).join(" ");
    reviewBannerText.textContent =
      ` A person needs to check this custom part before it can ship, so the STEP/STL/3MF downloads are locked for now. ${notes}`;
    reviewBanner.hidden = false;
  } else {
    reviewBanner.hidden = true;
  }

  // In-photo ghost ("In your photo"), with a with/without-part toggle when the
  // API produced both the composite and the plain photo. Cache-buster avoids a
  // stale image after a re-generate.
  const t = Date.now();
  // Files come back as API paths; VulcanAPI.asset() resolves them against the
  // configured API origin (a no-op when the site is served same-origin).
  const compositeUrl = design.files.composite;
  const photoUrl = design.files.photo;
  if (compositeUrl) {
    compositeSources = {
      with: `${VulcanAPI.asset(compositeUrl)}?t=${t}`,
      without: photoUrl ? `${VulcanAPI.asset(photoUrl)}?t=${t}` : null,
    };
    setCompositeView("with");
    compositeFigure.hidden = false;
    makeZoomable(designCompositeImg.parentElement, designCompositeImg); // scroll to zoom in
    document.getElementById("composite-toggle-without").hidden = !photoUrl;
  } else {
    compositeSources = { with: null, without: null };
    designCompositeImg.removeAttribute("src");
    compositeFigure.hidden = true;
  }

  // "The part": interactive 3D model (orbit/zoom/pan) + a dimensioned image.
  designPreviewImg.src = `${VulcanAPI.asset(design.files.preview_png)}?t=${t}`;
  // Prefer the ungated coarse preview mesh so 3D works even for a pending
  // freeform part (whose real STL is download-gated → would 403 in the viewer).
  currentStlUrl = VulcanAPI.asset(design.files.view_stl || design.files.stl);
  showPartView("3d");

  // Param summary table (value + source), built with DOM nodes.
  designParamsTable.innerHTML = "";
  const header = document.createElement("tr");
  for (const h of ["Parameter", "Value", "Source"]) {
    const th = document.createElement("th");
    th.textContent = h;
    header.appendChild(th);
  }
  designParamsTable.appendChild(header);
  for (const p of design.params || []) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = p.label;
    const value = document.createElement("td");
    value.textContent = p.unit ? `${p.value} ${p.unit}` : `${p.value}`;
    const source = document.createElement("td");
    source.textContent = SOURCE_BADGE[p.source] || p.source;
    source.className = `src-${p.source}`;
    tr.append(name, value, source);
    designParamsTable.appendChild(tr);
  }

  // Download links — locked (server returns 403) while a freeform design is
  // pending review, so present them as locked rather than dead links.
  const locked = design.review_status === "pending_review";
  designDownloads.innerHTML = "";
  for (const [label, key] of [["STEP", "step"], ["3MF", "threemf"], ["STL", "stl"]]) {
    const li = document.createElement("li");
    if (locked) {
      li.textContent = `${label} — 🔒 locked until approved`;
      li.className = "download-locked";
    } else {
      const a = document.createElement("a");
      a.href = VulcanAPI.asset(design.files[key]);
      a.download = "";
      a.textContent = `Download ${label}`;
      li.appendChild(a);
    }
    designDownloads.appendChild(li);
  }
}

// Build the design from the confirmed intent (the join). Shared by the first
// "Forge my part" click and the "Regenerate" button.
async function doGenerate(verb) {
  if (!currentIntent) return;
  generatePartBtn.disabled = true;
  if (regenerateBtn) regenerateBtn.disabled = true;
  setGenerateStatus(`${verb} your part…`, false);
  try {
    const response = await VulcanAPI.joinDesign(currentIntent.intent_id, { fulfillment });
    if (!response.ok) throw new Error(await describeFetchError(response));
    const design = await response.json();
    renderDesign(design);
    const how =
      (design.fulfillment || fulfillment) === "ship"
        ? "we'll print and ship it to you"
        : "download your files below";
    setGenerateStatus(`Design ${design.design_id} ready — ${how}.`, false);
    if (regenerateBtn) regenerateBtn.hidden = false; // offer a re-roll now
  } catch (err) {
    setGenerateStatus(`Error: ${errorText(err)}`, true);
  } finally {
    generatePartBtn.disabled = false;
    if (regenerateBtn) regenerateBtn.disabled = false;
  }
}

generatePartBtn.addEventListener("click", () => doGenerate("Forging"));

// Regenerate: for a CUSTOM (freeform) part, re-roll the design — the model
// authors a fresh attempt (useful if the last one had floating pieces or missed
// the intent); you then re-confirm the measurements and forge it. For a template
// part the geometry is deterministic, so it just rebuilds/re-renders in place.
if (regenerateBtn) {
  regenerateBtn.addEventListener("click", () => {
    if (!currentIntent) return;
    if (currentIntent.freeform) {
      designResult.hidden = true;
      regenerateBtn.hidden = true;
      runFreeform(generateStatusEl, regenerateBtn); // new custom-design attempt
    } else {
      doGenerate("Regenerating");
    }
  });
}
