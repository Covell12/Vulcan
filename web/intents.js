// "Start with a photo" flow (the complete single loop): upload photo(s) +
// optional freehand annotation + text -> POST /intents -> render questions as
// overlays on the photo (with mm/cm/in unit selectors) -> POST
// /intents/{id}/answers -> once ready_for_design, "Generate my part" calls
// POST /intents/{id}/design (the join) and renders the annotated preview, a
// param summary table (value + source), and the STEP/3MF/STL downloads — the
// user never touches the raw template form.

const photoInput = document.getElementById("photo-input");
const annotateWrap = document.getElementById("annotate-wrap");
const annotateImg = document.getElementById("annotate-img");
const annotateCanvas = document.getElementById("annotate-canvas");
const clearAnnotationBtn = document.getElementById("clear-annotation-btn");
const intentText = document.getElementById("intent-text");
const submitIntentBtn = document.getElementById("submit-intent-btn");
const intentStatusEl = document.getElementById("intent-status");

const questionsPanel = document.getElementById("questions-panel");
const intentDescriptionEl = document.getElementById("intent-description");
const overlayImg = document.getElementById("overlay-img");
const overlaySvg = document.getElementById("overlay-svg");
const questionsList = document.getElementById("questions-list");
const submitAnswersBtn = document.getElementById("submit-answers-btn");
const answersStatusEl = document.getElementById("answers-status");

const resultPanel = document.getElementById("result-panel");
const resultStatusLine = document.getElementById("result-status-line");
const intentJsonEl = document.getElementById("intent-json");
const generatePartBtn = document.getElementById("generate-part-btn");
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
let annotationPoints = []; // normalized [x, y] pairs on photo 0
let drawing = false;
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

// --- Photo upload + freehand annotation on photo 0 --------------------

photoInput.addEventListener("change", () => {
  selectedPhotos = Array.from(photoInput.files || []).slice(0, 3);
  annotationPoints = [];
  questionsPanel.hidden = true;
  resultPanel.hidden = true;

  if (selectedPhotos.length === 0) {
    annotateWrap.hidden = true;
    clearAnnotationBtn.hidden = true;
    return;
  }

  annotateImg.src = URL.createObjectURL(selectedPhotos[0]);
  annotateImg.onload = () => {
    annotateCanvas.width = annotateImg.clientWidth;
    annotateCanvas.height = annotateImg.clientHeight;
    clearCanvas();
  };
  annotateWrap.hidden = false;
  clearAnnotationBtn.hidden = false;
});

function clearCanvas() {
  const ctx = annotateCanvas.getContext("2d");
  ctx.clearRect(0, 0, annotateCanvas.width, annotateCanvas.height);
}

function canvasPoint(event) {
  const rect = annotateCanvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
}

function drawStroke() {
  const ctx = annotateCanvas.getContext("2d");
  clearCanvas();
  ctx.strokeStyle = "#2f5fdb";
  ctx.lineWidth = 2;
  ctx.beginPath();
  annotationPoints.forEach(([nx, ny], i) => {
    const x = nx * annotateCanvas.width;
    const y = ny * annotateCanvas.height;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

annotateCanvas.addEventListener("pointerdown", (event) => {
  drawing = true;
  annotationPoints = [canvasPoint(event)];
  drawStroke();
});
annotateCanvas.addEventListener("pointermove", (event) => {
  if (!drawing) return;
  annotationPoints.push(canvasPoint(event));
  drawStroke();
});
window.addEventListener("pointerup", () => {
  drawing = false;
});

clearAnnotationBtn.addEventListener("click", () => {
  annotationPoints = [];
  clearCanvas();
});

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
  if (annotationPoints.length > 1) {
    formData.append("annotation", JSON.stringify([{ photo_index: 0, points: annotationPoints }]));
  }

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

  overlayImg.src = annotateImg.src || "";
  questionsList.innerHTML = "";
  overlaySvg.innerHTML = "";
  dimChips = {};

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

// Arrowhead markers for the dimension lines, sized in pixels (userSpaceOnUse) so
// they don't scale with stroke width. Both ends point OUTWARD like a real
// engineering dimension.
const OVERLAY_DEFS = `
  <marker id="dim-arrow-end" markerUnits="userSpaceOnUse" markerWidth="14" markerHeight="14" refX="10" refY="7" orient="auto">
    <path d="M2,2 L11,7 L2,12 Z" class="dim-arrowhead"/>
  </marker>
  <marker id="dim-arrow-start" markerUnits="userSpaceOnUse" markerWidth="14" markerHeight="14" refX="4" refY="7" orient="auto-start-reverse">
    <path d="M2,2 L11,7 L2,12 Z" class="dim-arrowhead"/>
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

// Draw a dimension line between two pixel points, styled like a ruler laid on
// the photo: a white legibility halo, evenly-spaced graduation ticks, end serifs
// (witness marks), and outward arrowheads.
function drawRulerLine(g, p0, p1, dashed) {
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1], len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len, ny = dx / len; // unit perpendicular

  g.appendChild(svgEl("line", { x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1], class: "dim-halo" }));

  // Ruler graduation ticks (every 5th a little longer, like real gradations).
  const ticks = Math.min(28, Math.max(6, Math.round(len / 20)));
  for (let i = 1; i < ticks; i++) {
    const px = p0[0] + dx * (i / ticks), py = p0[1] + dy * (i / ticks);
    const h = i % 5 === 0 ? 6 : 3;
    g.appendChild(svgEl("line", { x1: px - nx * h, y1: py - ny * h, x2: px + nx * h, y2: py + ny * h, class: "dim-grad" }));
  }

  const main = svgEl("line", {
    x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1],
    class: dashed ? "dim-line dim-depth" : "dim-line",
    "marker-start": "url(#dim-arrow-start)",
    "marker-end": "url(#dim-arrow-end)",
  });
  g.appendChild(main);

  const s = 12; // end serifs (witness marks)
  for (const p of [p0, p1]) {
    g.appendChild(svgEl("line", { x1: p[0] - nx * s, y1: p[1] - ny * s, x2: p[0] + nx * s, y2: p[1] + ny * s, class: "dim-witness" }));
  }
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
    const rot = ov.rotation || 0;
    const xf = `rotate(${rot} ${cx} ${cy})`;
    // Full circle/ellipse outline: a white halo under a crisp colored ring.
    g.appendChild(svgEl("ellipse", { cx, cy, rx, ry, class: "dim-ellipse-halo", transform: xf }));
    g.appendChild(svgEl("ellipse", { cx, cy, rx, ry, class: "dim-ellipse", transform: xf }));
    // The measured diameter drawn as a ruler line across it (rotated endpoints).
    const a = (rot * Math.PI) / 180, ca = Math.cos(a), sa = Math.sin(a);
    drawRulerLine(g, [cx - rx * ca, cy - rx * sa], [cx + rx * ca, cy + rx * sa], false);
    mid = [cx, cy - ry - 15];
    prefix = "⌀ "; // diameter symbol
  } else {
    const pts = ov.points && ov.points.length >= 2 ? ov.points : [[0.4, 0.5], [0.6, 0.5]];
    const p0 = [pts[0][0] * W, pts[0][1] * H];
    const p1 = [pts[pts.length - 1][0] * W, pts[pts.length - 1][1] * H];
    drawRulerLine(g, p0, p1, kind === "dim_depth");
    const dx = p1[0] - p0[0], dy = p1[1] - p0[1], len = Math.hypot(dx, dy) || 1;
    // Lift the chip slightly off the line so it doesn't cover the gradations.
    mid = [(p0[0] + p1[0]) / 2 + (-dy / len) * 16, (p0[1] + p1[1]) / 2 + (dx / len) * 16];
  }

  const chipG = svgEl("g", { class: "dim-chip" });
  const rect = svgEl("rect", { rx: 5, ry: 5, class: "dim-chip-rect" });
  const text = svgEl("text", { class: "dim-chip-text", "text-anchor": "middle", "dominant-baseline": "central" });
  chipG.appendChild(rect);
  chipG.appendChild(text);
  g.appendChild(chipG);
  overlaySvg.appendChild(g);

  const chip = { group: chipG, rect, text, mid, dimension, prefix };
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

function setChip(chip, textStr, cls) {
  chip.text.textContent = (chip.prefix || "") + (textStr || "?");
  chip.group.setAttribute("class", `dim-chip ${cls || ""}`);
  const pad = 6;
  const bb = chip.text.getBBox();
  const w = Math.max(bb.width, 8) + pad * 2;
  const h = bb.height + pad * 2;
  const [mx, my] = chip.mid;
  chip.text.setAttribute("x", mx);
  chip.text.setAttribute("y", my);
  chip.rect.setAttribute("x", mx - w / 2);
  chip.rect.setAttribute("y", my - h / 2);
  chip.rect.setAttribute("width", w);
  chip.rect.setAttribute("height", h);
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

// The min/max (mm) the template accepts for a question's dimension, or null.
function boundsFor(question) {
  if (!question || !question.dim_name) return null;
  return (currentIntent.param_bounds || {})[question.dim_name] || null;
}

// A measurement input with a mm/cm/in unit selector (default = remembered
// session unit), a live "8 in = 203.2 mm" dual display, and — when the template
// constrains this dimension — the allowed range plus out-of-range flagging so a
// too-large value is caught HERE, not with a confusing 422 at the join.
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

  const hint = document.createElement("span");
  hint.className = "range-hint";
  if (bounds) {
    const lo = bounds.minimum != null ? trimNumber(bounds.minimum) : "…";
    const hi = bounds.maximum != null ? trimNumber(bounds.maximum) : "…";
    hint.textContent = `allowed range: ${lo}–${hi} mm`;
  }

  // Keep the native min/max in the CURRENT unit; flag out-of-range values.
  const applyBounds = () => {
    if (!bounds) return;
    const u = unit.value;
    if (bounds.minimum != null) input.min = trimNumber(fromMm(bounds.minimum, u));
    else input.removeAttribute("min");
    if (bounds.maximum != null) input.max = trimNumber(fromMm(bounds.maximum, u));
    else input.removeAttribute("max");
    const mm = collectMm(questionId);
    const bad =
      mm != null &&
      ((bounds.minimum != null && mm < bounds.minimum - 1e-9) ||
        (bounds.maximum != null && mm > bounds.maximum + 1e-9));
    input.classList.toggle("out-of-range", bad);
    hint.classList.toggle("error", bad);
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
  unit.addEventListener("change", () => {
    setSessionUnit(unit.value);
    for (const sel of document.querySelectorAll(".unit-select")) {
      sel.value = getSessionUnit();
      sel.dispatchEvent(new Event("refresh-dual"));
    }
  });

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
  const outOfRange = [];
  for (const question of currentIntent.questions || []) {
    const input = document.getElementById(questionInputId(question.question_id));
    if (!input) continue; // deduped / already-confirmed questions render no input

    if (question.kind === "measure_mm") {
      const mm = collectMm(question.question_id); // converts cm/in -> mm
      if (mm === null) continue;
      // Catch a value outside the template's range HERE, so the join can't 422.
      const b = boundsFor(question);
      if (b && ((b.minimum != null && mm < b.minimum - 1e-9) || (b.maximum != null && mm > b.maximum + 1e-9))) {
        outOfRange.push(`${question.dim_name} (${trimNumber(mm)}mm; allowed ${b.minimum ?? "…"}–${b.maximum ?? "…"}mm)`);
        continue;
      }
      answers.push({ question_id: question.question_id, measure_mm: mm });
    } else if (question.kind === "confirm" && input.checked) {
      answers.push({ question_id: question.question_id, confirm: true });
    } else if (question.kind === "choice" && input.value !== "") {
      answers.push({ question_id: question.question_id, choice: input.value });
    }
  }

  if (outOfRange.length) {
    setAnswersStatus(
      `These are outside this design's allowed range — adjust them (or use "Design this custom instead" for a part that fits): ${outOfRange.join("; ")}.`,
      true,
    );
    return;
  }

  submitAnswers(answers);
});

// --- Result panel: "Generate my part" -> the join endpoint --------------

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
    document.getElementById("composite-toggle-without").hidden = !photoUrl;
  } else {
    compositeSources = { with: null, without: null };
    designCompositeImg.removeAttribute("src");
    compositeFigure.hidden = true;
  }

  // "The part": interactive 3D model (orbit/zoom/pan) + a dimensioned image.
  designPreviewImg.src = `${VulcanAPI.asset(design.files.preview_png)}?t=${t}`;
  currentStlUrl = VulcanAPI.asset(design.files.stl);
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

generatePartBtn.addEventListener("click", async () => {
  if (!currentIntent) return;
  generatePartBtn.disabled = true;
  setGenerateStatus("Forging your part…", false);
  try {
    const response = await VulcanAPI.joinDesign(currentIntent.intent_id);
    if (!response.ok) throw new Error(await describeFetchError(response));
    const design = await response.json();
    renderDesign(design);
    setGenerateStatus(`Design ${design.design_id} ready — download below.`, false);
  } catch (err) {
    setGenerateStatus(`Error: ${errorText(err)}`, true);
  } finally {
    generatePartBtn.disabled = false;
  }
});
