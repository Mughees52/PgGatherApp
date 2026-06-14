// --- Toast notifications ----------------------------------------------------
function toast(title, body, kind) {
  const wrap = document.getElementById("toasts");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.innerHTML = `<div class="t-title">${title}</div>` + (body ? `<div class="t-body">${body}</div>` : "");
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4000);
}

// --- Poll a report's status until terminal, then reload ---------------------
function pollStatus(id) {
  const tick = async () => {
    try {
      const res = await fetch(`/reports/${id}/status`);
      const data = await res.json();
      const badge = document.getElementById("status-badge");
      if (badge) badge.textContent = data.status;
      if (["done", "failed", "missing"].includes(data.status)) { window.location.reload(); return; }
    } catch (e) { /* keep trying */ }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
}

// --- Test-connection buttons (connections page) -----------------------------
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".test-btn");
  if (!btn) return;
  ev.preventDefault();
  const cid = btn.dataset.cid;
  const name = btn.dataset.name || "connection";
  btn.disabled = true; btn.textContent = "…";
  try {
    const res = await fetch(`/connections/${cid}/test`, { method: "POST" });
    const data = await res.json();
    if (data.ok) toast("✓ " + name, "Connection successful", "ok");
    else toast("✗ " + name, data.error || "Connection failed", "bad");
  } catch (e) {
    toast("✗ " + name, "Request error", "bad");
  } finally { btn.disabled = false; btn.textContent = "▶"; }
});

// --- Compare picker: build /compare/A/B from two radios ---------------------
document.addEventListener("submit", (ev) => {
  const form = ev.target.closest(".compare-form");
  if (!form) return;
  ev.preventDefault();
  const a = form.querySelector('input[name="a"]:checked');
  const b = form.querySelector('input[name="b"]:checked');
  if (!a || !b) { toast("Pick two", "Choose one report in each column (A and B).", "bad"); return; }
  if (a.value === b.value) { toast("Pick two", "Choose two different reports.", "bad"); return; }
  window.location.href = `/compare/${a.value}/${b.value}`;
});

// --- Compare view: toggle unchanged rows ------------------------------------
function setupDiffFilter() {
  const cb = document.getElementById("only-changed");
  if (!cb) return;
  const apply = () => document.querySelectorAll('tr[data-same="1"]').forEach((tr) => {
    tr.style.display = cb.checked ? "none" : "";
  });
  cb.addEventListener("change", apply); apply();
}

// --- Parameter live filter (detail page) ------------------------------------
function setupParamSearch() {
  const input = document.getElementById("param-search");
  if (!input) return;
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    document.querySelectorAll("#param-list tr[data-name]").forEach((tr) => {
      tr.style.display = tr.dataset.name.includes(q) ? "" : "none";
    });
    document.querySelectorAll(".param-group").forEach((g) => {
      const any = [...g.querySelectorAll("tr[data-name]")].some((tr) => tr.style.display !== "none");
      g.style.display = any ? "" : "none";
    });
  });
}

// --- Reveal-on-scroll animation ---------------------------------------------
function revealOnScroll() {
  const els = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.08 });
  els.forEach((e) => io.observe(e));
}

// --- Collapsible section toggles ---------------------------------------------
function setupSectionToggles() {
  document.querySelectorAll(".toggle-section").forEach((btn) => {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    const update = () => { btn.textContent = target.classList.contains("collapsed") ? "▸" : "▾"; };
    btn.addEventListener("click", () => { target.classList.toggle("collapsed"); update(); });
    update();
  });
}

// --- Click to expand/collapse query cells -------------------------------------
function expandQuery(cell) {
  const full = cell.dataset.full;
  if (!full) return;
  if (cell.classList.contains("query-expanded")) {
    // Collapse back
    cell.classList.remove("query-expanded");
    cell.textContent = full.slice(0, 100) + (full.length > 100 ? "…" : "");
  } else {
    // Expand
    cell.classList.add("query-expanded");
    cell.textContent = full;
  }
}

// --- Double-click to copy table detail (like original pg_gather) -------------
document.addEventListener("dblclick", (ev) => {
  const cell = ev.target.closest("[data-copy]");
  if (!cell) return;
  ev.preventDefault();
  // Clear the text selection that double-click creates
  window.getSelection()?.removeAllRanges();
  const text = cell.dataset.copy;
  if (!text) return;
  const name = cell.querySelector("strong")?.textContent || "Info";
  navigator.clipboard.writeText(text).then(() => {
    toast("Copied", name + " — details copied to clipboard", "ok");
  }).catch(() => {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta);
    toast("Copied", name + " — details copied to clipboard", "ok");
  });
});

// --- Parameter Recommendations calculator ------------------------------------
document.addEventListener("click", async (ev) => {
  const calcBtn = ev.target.closest("#rec-calc");
  if (!calcBtn) return;
  const rid = calcBtn.dataset.rid;
  calcBtn.disabled = true; calcBtn.textContent = "Calculating…";
  try {
    const body = new FormData();
    body.append("cpus", document.getElementById("rec-cpus").value);
    body.append("memory_gb", document.getElementById("rec-mem").value);
    body.append("storage", document.getElementById("rec-strg").value);
    body.append("workload", document.getElementById("rec-wrkld").value);
    body.append("filesystem", document.getElementById("rec-flsys").value);
    const res = await fetch(`/reports/${rid}/recommend`, { method: "POST", body });
    const data = await res.json();
    const results = document.getElementById("rec-results");
    const copyBtn = document.getElementById("rec-copy");
    if (data.recommendations && data.recommendations.length > 0) {
      let html = '<table class="rec-table"><thead><tr><th>Parameter</th><th>Current</th><th>Recommended</th><th>Reason</th></tr></thead><tbody>';
      for (const r of data.recommendations) {
        html += `<tr><td class="mono">${r.param}</td><td class="mono current">${r.current}</td><td class="mono suggest">${r.suggest}</td><td class="muted small">${r.reason}</td></tr>`;
      }
      html += '</tbody></table>';
      results.innerHTML = html;
      copyBtn.style.display = "";
      copyBtn.onclick = () => {
        const text = data.recommendations.map(r => `${r.param} = ${r.suggest}  # ${r.reason}`).join("\n");
        navigator.clipboard.writeText(text).then(() => toast("Copied", `${data.recommendations.length} recommendations`, "ok"));
      };
    } else {
      results.innerHTML = '<div class="rec-empty">✓ All parameters look optimal for your configuration</div>';
      copyBtn.style.display = "none";
    }
  } catch (e) {
    toast("Error", "Failed to calculate recommendations", "bad");
  } finally { calcBtn.disabled = false; calcBtn.textContent = "Calculate"; }
});

// Auto-calculate on input change
["rec-cpus","rec-mem","rec-strg","rec-wrkld","rec-flsys"].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", () => document.getElementById("rec-calc")?.click());
});

// --- Column sorting on detail tables -----------------------------------------
function setupTableSort() {
  document.querySelectorAll("table.detail-tbl th, table.grid.diff th, table.rec-table th").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const table = th.closest("table");
      const tbody = table.querySelector("tbody");
      if (!tbody) return;
      const idx = Array.from(th.parentNode.children).indexOf(th);
      const rows = Array.from(tbody.rows);
      const cur = th.dataset.sortDir || "";
      const dir = cur === "asc" ? "desc" : "asc";
      // Reset all headers in this table
      th.parentNode.querySelectorAll("th").forEach(h => { h.dataset.sortDir = ""; h.classList.remove("sort-asc","sort-desc"); });
      th.dataset.sortDir = dir;
      th.classList.add("sort-" + dir);
      rows.sort((a, b) => {
        let va = a.cells[idx]?.textContent.trim() || "";
        let vb = b.cells[idx]?.textContent.trim() || "";
        // Try numeric comparison (strip %, commas, units)
        const na = parseFloat(va.replace(/[,%KMGBT ]/g, ""));
        const nb = parseFloat(vb.replace(/[,%KMGBT ]/g, ""));
        if (!isNaN(na) && !isNaN(nb)) {
          return dir === "asc" ? na - nb : nb - na;
        }
        // Fallback to string
        return dir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}

// --- Upload dropzone --------------------------------------------------------
function setupDropzone() {
  const dz = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const label = document.getElementById("dz-file");
  const form = document.getElementById("upload-form");
  const btn = document.getElementById("upload-btn");
  if (!dz || !input) return;
  const show = () => { if (input.files.length) label.textContent = "📄 " + input.files[0].name; };
  ["dragover", "dragenter"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((e) => dz.addEventListener(e, () => dz.classList.remove("drag")));
  dz.addEventListener("drop", (ev) => { ev.preventDefault(); if (ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; show(); } });
  input.addEventListener("change", show);
  if (form) form.addEventListener("submit", () => { if (input.files.length) { btn.textContent = "Uploading…"; btn.disabled = true; } });
}
