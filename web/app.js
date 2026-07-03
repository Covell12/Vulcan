// Tab switching between the two top-level flows. Both tab panels' scripts
// (this file and intents.js) run unconditionally on page load regardless of
// which tab is active — only the DOM visibility toggles.
for (const btn of document.querySelectorAll(".tab-btn")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".tab-btn")) b.classList.remove("active");
    for (const p of document.querySelectorAll(".tab-panel")) p.classList.remove("active");
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
}

const templateSelect = document.getElementById("template-select");
const templateDescription = document.getElementById("template-description");
const paramFields = document.getElementById("param-fields");
const form = document.getElementById("design-form");
const statusEl = document.getElementById("status");
const previewImg = document.getElementById("preview-img");
const downloadsEl = document.getElementById("downloads");

let templates = [];
let currentFields = [];

// Shared, defensive error rendering for failed fetches. Works whether the
// server sent a JSON body ({detail: ...}), plain text, or nothing at all, and
// always includes the HTTP status. Guarantees a non-empty string so an empty
// "Error:" can never be shown. Used by both app.js and intents.js.
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

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", Boolean(isError));
}

function fieldInputId(name) {
  return `field-${name}`;
}

// Builds the parameter form straight from a template's field list (as
// returned by GET /templates) — nothing about any specific template's
// fields is hardcoded here.
function renderFields(fields) {
  currentFields = fields;
  paramFields.innerHTML = "";

  for (const field of fields) {
    const label = document.createElement("label");
    label.title = field.description || "";

    if (field.type === "boolean") {
      label.classList.add("checkbox-field");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = fieldInputId(field.name);
      input.name = field.name;
      input.checked = Boolean(field.default);
      label.appendChild(input);
      label.appendChild(document.createTextNode(field.label));
    } else if (field.type === "choice") {
      label.appendChild(document.createTextNode(field.label));
      const select = document.createElement("select");
      select.id = fieldInputId(field.name);
      select.name = field.name;
      for (const choice of field.choices || []) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        if (choice === field.default) option.selected = true;
        select.appendChild(option);
      }
      label.appendChild(select);
    } else {
      label.appendChild(document.createTextNode(field.label));
      const input = document.createElement("input");
      input.type = "number";
      input.step = field.type === "integer" ? "1" : "any";
      if (field.minimum !== null && field.minimum !== undefined) input.min = field.minimum;
      if (field.maximum !== null && field.maximum !== undefined) input.max = field.maximum;
      if (field.default !== null && field.default !== undefined) input.value = field.default;
      input.id = fieldInputId(field.name);
      input.name = field.name;
      input.required = true;
      label.appendChild(input);
    }

    paramFields.appendChild(label);
  }
}

function paramsFromFields() {
  const params = {};
  for (const field of currentFields) {
    const input = document.getElementById(fieldInputId(field.name));
    if (field.type === "boolean") {
      params[field.name] = input.checked;
    } else if (field.type === "integer") {
      params[field.name] = parseInt(input.value, 10);
    } else if (field.type === "number") {
      params[field.name] = Number(input.value);
    } else {
      params[field.name] = input.value;
    }
  }
  return params;
}

function renderDownloads(files) {
  downloadsEl.innerHTML = "";
  const links = [
    { label: "STEP", href: files.step },
    { label: "3MF", href: files.threemf },
    { label: "STL", href: files.stl },
  ];
  for (const { label, href } of links) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = href;
    a.download = "";
    a.textContent = `Download ${label}`;
    li.appendChild(a);
    downloadsEl.appendChild(li);
  }
}

function selectedTemplate() {
  return templates.find((t) => t.template_id === templateSelect.value);
}

function onTemplateChange() {
  const template = selectedTemplate();
  if (!template) return;
  templateDescription.textContent = `template_id: ${template.template_id}`;
  renderFields(template.fields);
  previewImg.classList.remove("visible");
  previewImg.removeAttribute("src");
  downloadsEl.innerHTML = "";
  setStatus("", false);
}

async function loadTemplates() {
  setStatus("Loading templates…", false);
  try {
    const response = await fetch("/templates");
    if (!response.ok) throw new Error(await describeFetchError(response));
    templates = await response.json();
  } catch (err) {
    setStatus(`Error loading templates: ${errorText(err)}`, true);
    return;
  }

  templateSelect.innerHTML = "";
  for (const template of templates) {
    const option = document.createElement("option");
    option.value = template.template_id;
    option.textContent = template.label;
    templateSelect.appendChild(option);
  }

  onTemplateChange();
  setStatus("", false);
}

templateSelect.addEventListener("change", onTemplateChange);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = form.querySelector("button[type=submit]");
  submitButton.disabled = true;
  setStatus("Generating…", false);
  previewImg.classList.remove("visible");
  downloadsEl.innerHTML = "";

  try {
    const response = await fetch("/designs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        template_id: templateSelect.value,
        params: paramsFromFields(),
      }),
    });

    if (!response.ok) throw new Error(await describeFetchError(response));

    const design = await response.json();
    previewImg.src = `${design.files.preview_png}?t=${Date.now()}`;
    previewImg.classList.add("visible");
    renderDownloads(design.files);
    setStatus(`Design ${design.design_id} ready.`, false);
  } catch (err) {
    setStatus(`Error: ${errorText(err)}`, true);
  } finally {
    submitButton.disabled = false;
  }
});

loadTemplates();
