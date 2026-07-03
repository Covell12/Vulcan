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
const overlaySvg = document.getElementById("overlay-shapes");
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
    const response = await fetch("/intents", { method: "POST", body: formData });
    if (!response.ok) throw new Error(await describeFetchError(response));
    currentIntent = await response.json();
    renderQuestions();
    renderResult();
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

  overlayImg.src = annotateImg.src || "";
  questionsList.innerHTML = "";
  overlaySvg.innerHTML = "";

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

  const renderedDims = new Set();
  for (const question of questions) {
    if (question.overlay && question.overlay.photo_index === 0) {
      drawOverlay(question.overlay);
    }
    if (question.dim_name) {
      if (renderedDims.has(question.dim_name)) continue;
      if (preferredByDim[question.dim_name] !== question) continue; // wait for the preferred one
      renderedDims.add(question.dim_name);
    }
    questionsList.appendChild(renderQuestionRow(question, dimsByName[question.dim_name]));
  }
}

function drawOverlay(overlay) {
  const ns = "http://www.w3.org/2000/svg";
  const points = overlay.points || [];
  if (points.length === 0) return;

  if (overlay.shape === "circle") {
    const [cx, cy] = points[0];
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", cx);
    circle.setAttribute("cy", cy);
    circle.setAttribute("r", 0.03);
    circle.setAttribute("class", "overlay-shape");
    overlaySvg.appendChild(circle);
    return;
  }

  // "arrow" and "line" both render as a polyline; arrow gets a marker.
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", points.map(([x, y]) => `${x},${y}`).join(" "));
  line.setAttribute("class", "overlay-shape");
  if (overlay.shape === "arrow") line.setAttribute("marker-end", "url(#overlay-arrowhead)");
  overlaySvg.appendChild(line);
}

// A measurement input with a mm/cm/in unit selector (default = remembered
// session unit) and a live "8 in = 203.2 mm" dual display. Internal units stay
// mm; the value is converted to mm at collection time (see collectMm).
function appendMeasureField(parent, questionId, placeholder) {
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
  const refresh = () => (dual.textContent = formatDual(input.value, unit.value));

  input.addEventListener("input", refresh);
  unit.addEventListener("refresh-dual", refresh);
  unit.addEventListener("change", () => {
    setSessionUnit(unit.value);
    // "Remembered per session": switch every unit selector to the new unit.
    for (const sel of document.querySelectorAll(".unit-select")) {
      sel.value = getSessionUnit();
      sel.dispatchEvent(new Event("refresh-dual"));
    }
  });

  wrap.appendChild(input);
  wrap.appendChild(unit);
  wrap.appendChild(dual);
  parent.appendChild(wrap);
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

    appendMeasureField(card, question.question_id, "enter a corrected value");

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
    appendMeasureField(row, question.question_id, placeholder);
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
    const response = await fetch(`/intents/${currentIntent.intent_id}/answers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers }),
    });
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
  for (const question of currentIntent.questions || []) {
    const input = document.getElementById(questionInputId(question.question_id));
    if (!input) continue; // deduped / already-confirmed questions render no input

    if (question.kind === "measure_mm") {
      const mm = collectMm(question.question_id); // converts cm/in -> mm
      if (mm !== null) answers.push({ question_id: question.question_id, measure_mm: mm });
    } else if (question.kind === "confirm" && input.checked) {
      answers.push({ question_id: question.question_id, confirm: true });
    } else if (question.kind === "choice" && input.value !== "") {
      answers.push({ question_id: question.question_id, choice: input.value });
    }
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

const SOURCE_BADGE = {
  measured: "measured ✓",
  chosen: "chosen ✓",
  suggested: "suggested ~",
  assumed: "estimate ~",
  default: "default",
};

function renderDesign(design) {
  designResult.hidden = false;

  // In-photo ghost first ("In your photo"), when the API produced one. The
  // cache-buster keeps a re-generated design from showing a stale image.
  const compositeUrl = design.files.composite;
  if (compositeUrl) {
    designCompositeImg.src = `${compositeUrl}?t=${Date.now()}`;
    compositeFigure.hidden = false;
  } else {
    designCompositeImg.removeAttribute("src");
    compositeFigure.hidden = true;
  }

  designPreviewImg.src = `${design.files.preview_png}?t=${Date.now()}`;

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

  // Download links.
  designDownloads.innerHTML = "";
  for (const [label, key] of [["STEP", "step"], ["3MF", "threemf"], ["STL", "stl"]]) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = design.files[key];
    a.download = "";
    a.textContent = `Download ${label}`;
    li.appendChild(a);
    designDownloads.appendChild(li);
  }
}

generatePartBtn.addEventListener("click", async () => {
  if (!currentIntent) return;
  generatePartBtn.disabled = true;
  setGenerateStatus("Generating your part…", false);
  try {
    const response = await fetch(`/intents/${currentIntent.intent_id}/design`, { method: "POST" });
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
