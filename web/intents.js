// "Start with a photo" flow: upload photo(s) + optional freehand annotation
// + text -> POST /intents -> render questions as overlays on the photo ->
// POST /intents/{id}/answers -> once ready_for_design, feed the resulting
// dimensions into the existing /designs flow (see app.js's `templates` and
// `renderDownloads`, reused here rather than duplicated).

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

    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.id = questionInputId(question.question_id);
    input.placeholder = "enter a corrected value (mm)";
    card.appendChild(input);

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
    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.id = questionInputId(question.question_id);
    if (dimension && dimension.source === "depth_inferred" && dimension.value_mm != null) {
      // Depth prior: a suggestion, not a measurement — say so.
      input.placeholder = `looks like ~${dimension.value_mm}mm — measure to confirm`;
    } else if (dimension && dimension.value_mm !== null && dimension.value_mm !== undefined) {
      input.placeholder = `assumed: ${dimension.value_mm}mm`;
    }
    row.appendChild(input);
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
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "—";
    select.appendChild(blank);
    for (const choice of question.choices || []) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
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

    if ((question.kind === "measure_mm") && input.value !== "") {
      answers.push({ question_id: question.question_id, measure_mm: Number(input.value) });
    } else if (question.kind === "confirm" && input.checked) {
      answers.push({ question_id: question.question_id, confirm: true });
    } else if (question.kind === "choice" && input.value !== "") {
      answers.push({ question_id: question.question_id, choice: input.value });
    }
  }

  submitAnswers(answers);
});

// --- Result panel + "Generate part" -------------------------------------

function renderResult() {
  resultPanel.hidden = false;
  resultStatusLine.textContent = `status: ${currentIntent.status}`;
  intentJsonEl.textContent = JSON.stringify(currentIntent, null, 2);
  generatePartBtn.disabled = currentIntent.status !== "ready_for_design" || !currentIntent.template_id;
}

generatePartBtn.addEventListener("click", async () => {
  if (!currentIntent || !currentIntent.template_id) return;

  const template = templates.find((t) => t.template_id === currentIntent.template_id);
  if (!template) {
    setGenerateStatus(`Error: template '${currentIntent.template_id}' not loaded yet — try again.`, true);
    return;
  }

  const dimsByName = Object.fromEntries((currentIntent.dimensions || []).map((d) => [d.name, d]));
  const params = {};
  for (const field of template.fields) {
    const dim = dimsByName[field.name];
    params[field.name] = dim && dim.value_mm !== null && dim.value_mm !== undefined ? dim.value_mm : field.default;
  }

  generatePartBtn.disabled = true;
  setGenerateStatus("Generating…", false);

  try {
    const response = await fetch("/designs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: currentIntent.template_id, params }),
    });
    if (!response.ok) throw new Error(await describeFetchError(response));
    const design = await response.json();

    // Reuse the "Direct template params" tab's preview UI (app.js globals)
    // instead of duplicating preview/download rendering here — sync its
    // template dropdown + fields first so it reflects what was just built.
    document.querySelector('.tab-btn[data-tab="direct"]').click();
    templateSelect.value = currentIntent.template_id;
    onTemplateChange();
    renderFields(template.fields.map((f) => ({ ...f, default: params[f.name] })));
    previewImg.src = `${design.files.preview_png}?t=${Date.now()}`;
    previewImg.classList.add("visible");
    renderDownloads(design.files);
    setGenerateStatus(`Design ${design.design_id} ready — see the Direct template params tab.`, false);
  } catch (err) {
    setGenerateStatus(`Error: ${errorText(err)}`, true);
  } finally {
    generatePartBtn.disabled = false;
  }
});
