const form = document.getElementById("bracket-form");
const statusEl = document.getElementById("status");
const previewImg = document.getElementById("preview-img");
const downloadsEl = document.getElementById("downloads");

function paramsFromForm(formEl) {
  const data = new FormData(formEl);
  return {
    span_mm: Number(data.get("span_mm")),
    depth_mm: Number(data.get("depth_mm")),
    thickness_mm: Number(data.get("thickness_mm")),
    screw_size: data.get("screw_size"),
    screw_count: Number(data.get("screw_count")),
    load_hint: data.get("load_hint"),
  };
}

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", Boolean(isError));
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
        template_id: "bracket_shelf_l",
        params: paramsFromForm(form),
      }),
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
    }

    const design = await response.json();
    previewImg.src = `${design.files.preview_png}?t=${Date.now()}`;
    previewImg.classList.add("visible");
    renderDownloads(design.files);
    setStatus(`Design ${design.design_id} ready.`, false);
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    submitButton.disabled = false;
  }
});
