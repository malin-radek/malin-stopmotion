const state = { accountId: "default", projectId: "default", frames: [] };
const $ = (id) => document.getElementById(id);

function status(text) {
  $("status").textContent = text;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function loadAccounts() {
  const data = await api("/accounts");
  $("accountSelect").innerHTML = data.accounts.map(a => `<option value="${a.id}">${a.name}</option>`).join("");
  state.accountId = $("accountSelect").value || "default";
  await loadProjects();
}

async function loadProjects() {
  const data = await api(`/accounts/${state.accountId}/projects`);
  if (!data.projects.length) {
    const created = await api(`/accounts/${state.accountId}/projects`, {
      method: "POST",
      body: JSON.stringify({ name: "default", local_id: "default", fps: 12, resolution: "1920x1080", orientation: "landscape" }),
    });
    data.projects.push({ id: created.id, name: created.project, frames: 0, fps: created.fps, resolution: created.resolution, orientation: created.orientation });
  }
  $("projectSelect").innerHTML = data.projects.map(p => `<option value="${p.id}">${p.name} (${p.frames})</option>`).join("");
  state.projectId = $("projectSelect").value;
  await loadFrames();
}

async function loadFrames() {
  const data = await api(`/frames?account_id=${encodeURIComponent(state.accountId)}&project=${encodeURIComponent(state.projectId)}`);
  state.frames = data.frames || [];
  $("projectName").value = data.project || state.projectId;
  $("fps").value = data.fps || 12;
  $("resolution").value = data.resolution || "1920x1080";
  $("orientation").value = data.orientation || "landscape";
  $("frames").innerHTML = state.frames.map((f, idx) => `
    <div class="frame">
      <img src="/frame/${f.id}/thumb.jpg?account_id=${encodeURIComponent(state.accountId)}&project=${encodeURIComponent(state.projectId)}">
      <span>${idx + 1}. ${f.id}</span>
    </div>
  `).join("");
  status(`Projekt ma ${state.frames.length} klatek.`);
}

$("accountSelect").addEventListener("change", async (e) => {
  state.accountId = e.target.value;
  await loadProjects();
});

$("projectSelect").addEventListener("change", async (e) => {
  state.projectId = e.target.value;
  await loadFrames();
});

$("newAccount").addEventListener("click", async () => {
  const name = prompt("Nazwa konta");
  if (!name) return;
  await api("/accounts", { method: "POST", body: JSON.stringify({ name }) });
  await loadAccounts();
});

$("newProject").addEventListener("click", async () => {
  const name = prompt("Nazwa projektu");
  if (!name) return;
  const created = await api(`/accounts/${state.accountId}/projects`, {
    method: "POST",
    body: JSON.stringify({ name, fps: 12, resolution: "1920x1080", orientation: "landscape" }),
  });
  state.projectId = created.id;
  await loadProjects();
  $("projectSelect").value = state.projectId;
  await loadFrames();
});

$("saveProject").addEventListener("click", async () => {
  await api(`/projects/${state.projectId}?account_id=${encodeURIComponent(state.accountId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      name: $("projectName").value || state.projectId,
      fps: Number($("fps").value || 12),
      resolution: $("resolution").value,
      orientation: $("orientation").value,
    }),
  });
  await loadProjects();
});

$("pairPhone").addEventListener("click", async () => {
  const data = await api(`/accounts/${state.accountId}/pairing`, { method: "POST", body: "{}" });
  $("pairing").classList.remove("hidden");
  $("pairing").innerHTML = `
    <strong>Skanuj w aplikacji telefonu</strong>
    <img src="${data.qr_url}" alt="QR parowania">
    <code>${data.token}</code>
  `;
});

$("capture").addEventListener("click", async () => {
  await api(`/frame/take?account_id=${encodeURIComponent(state.accountId)}&project=${encodeURIComponent(state.projectId)}`, { method: "POST", body: "{}" });
  await loadFrames();
});

$("undo").addEventListener("click", async () => {
  const last = state.frames[state.frames.length - 1];
  if (!last) return;
  await api(`/frames/${last.id}?account_id=${encodeURIComponent(state.accountId)}&project=${encodeURIComponent(state.projectId)}`, { method: "DELETE" });
  await loadFrames();
});

$("render").addEventListener("click", async () => {
  status("Renderuje MP4...");
  const data = await api(`/projects/${state.projectId}/render?account_id=${encodeURIComponent(state.accountId)}`, { method: "POST", body: "{}" });
  status("Gotowe.");
  window.open(data.download_url, "_blank");
});

window.addEventListener("keydown", (event) => {
  if (event.code === "Space" && event.target === document.body) {
    event.preventDefault();
    $("capture").click();
  }
});

loadAccounts().catch(err => status(err.message));
