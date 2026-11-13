/* File Recovery front end: plain JavaScript, no framework, no external requests. */
(() => {
  "use strict";

  const TOKEN = window.APP_TOKEN || "";
  const EXTS = ["jpg", "png", "gif", "bmp", "pdf", "zip", "rar", "7z", "gz", "sqlite",
                "mp4", "mov", "avi", "wav", "mp3", "exe"];
  const JOB_TITLES = {
    trash_restore: "Pulihkan dari trash", fs_scan: "Mencari file dihapus", fs_recover: "Memulihkan file",
    fs_preview: "Menyiapkan pratinjau", carve: "Pemindaian carving", image: "Menyalin disk",
    repair: "Memperbaiki file", flash_diag: "Diagnosa perangkat", flash_surface: "Uji permukaan",
    flash_repair: "Perbaikan sistem file",
  };

  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const state = {
    page: "trash", job: null, pollTimer: null, debugOn: false, debugTimer: null, lastSeq: 0,
    trash: [], trashSel: new Set(), fs: [], fsSel: new Set(), fsTotal: 0,
    exts: new Set(EXTS), repairRows: [], disks: [],
  };

  // ---------- helpers ----------

  async function api(path, { method = "GET", body } = {}) {
    const opts = { method, headers: { "X-Token": TOKEN } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function fmtSize(n) {
    if (n === null || n === undefined) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
  }
  function fmtDate(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" });
  }
  function fmtEta(sec) {
    if (sec === null || sec === undefined) return "-";
    if (sec < 60) return `${sec} dtk`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m} mnt ${String(s).padStart(2, "0")} dtk`;
    return `${Math.floor(m / 60)} jam ${String(m % 60).padStart(2, "0")} mnt`;
  }
  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function toast(title, message = "", kind = "info") {
    const el = document.createElement("div");
    el.className = `toast ${kind === "ok" ? "ok" : kind === "err" ? "err" : ""}`;
    el.innerHTML = `<b>${esc(title)}</b><span class="muted small">${esc(message)}</span>`;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 9000 : 5000);
  }
  function setBusyButton(btn, busy, label) {
    if (!btn) return;
    btn.disabled = busy;
    if (label) btn.dataset.label ??= btn.textContent;
    btn.textContent = busy ? (label || "Berjalan…") : (btn.dataset.label || btn.textContent);
  }
  function dateRange(fromId, toId) {
    return { from: $(fromId).value || "", to: $(toId).value || "" };
  }

  // ---------- modal ----------

  function modal(title, bodyHtml, footerHtml = "") {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
      <header><h3>${esc(title)}</h3><button class="btn btn-sm btn-icon" data-close>✕</button></header>
      <div class="body">${bodyHtml}</div>
      ${footerHtml ? `<footer>${footerHtml}</footer>` : ""}</div>`;
    const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    back.addEventListener("click", (e) => { if (e.target === back || e.target.dataset.close !== undefined) close(); });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    return { root: back, close };
  }

  // Built-in file/folder picker (works without native dialogs)
  function openBrowser({ mode = "any", title = "Pilih", initial = "", onPick }) {
    let current = initial || "";
    const m = modal(title, `<div class="crumbs" id="br-path"></div>
      <div class="row" style="margin-bottom:10px"><button class="btn btn-sm" id="br-up">⬆ Naik</button>
      <div id="br-roots" class="chips"></div></div>
      <div class="list" id="br-list"><div class="empty">Memuat…</div></div>`,
      `${mode === "save" ? `<input type="text" id="br-name" value="hasil-pemulihan" style="max-width:260px">` : ""}
       ${mode !== "file" ? `<button class="btn btn-primary" id="br-choose">Pilih folder ini</button>` : ""}`);
    const load = async (path) => {
      try {
        const data = await api("/api/browse?path=" + encodeURIComponent(path || ""));
        current = data.path || "";
        $("br-path").textContent = current || "Pilih drive atau lokasi";
        $("br-up").disabled = !data.parent;
        $("br-up").onclick = () => load(data.parent || "");
        $("br-roots").innerHTML = (data.roots || []).map((r) =>
          `<button class="chip" data-root="${esc(r.path)}">${esc(r.name)}</button>`).join("");
        $$("[data-root]", $("br-roots")).forEach((b) => b.onclick = () => load(b.dataset.root));
        const list = $("br-list");
        if (!data.entries || !data.entries.length) {
          list.innerHTML = `<div class="empty">${current ? "Folder kosong" : "Pilih drive di atas"}</div>`;
          return;
        }
        list.innerHTML = data.entries.map((e) => `
          <button data-path="${esc(e.path)}" data-dir="${e.is_dir ? 1 : 0}">
            <span>${e.is_dir ? "📁" : "📄"}</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(e.name)}</span>
            <span class="muted small">${e.is_dir ? "" : fmtSize(e.size)}</span></button>`).join("");
        $$("button[data-path]", list).forEach((b) => b.onclick = () => {
          if (b.dataset.dir === "1") return load(b.dataset.path);
          if (mode === "file" || mode === "any") { m.close(); onPick(b.dataset.path); }
        });
      } catch (err) {
        $("br-list").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
      }
    };
    const choose = $("br-choose");
    if (choose) choose.onclick = () => {
      if (mode === "save") { m.close(); onPick(current.replace(/[\\/]$/, "") + "/" + ($("br-name").value || "hasil")); }
      else { m.close(); onPick(current); }
    };
    load(current);
  }

  // ---------- job handling ----------

  function setNav(page) {
    state.page = page;
    $$(".nav-item[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    $$(".page").forEach((s) => s.hidden = s.id !== `page-${page}`);
    if (page === "trash") loadTrash();
    if (page === "deleted") loadFs();
    if (page === "flash") { /* nothing to load */ }
    history.replaceState(null, "", "#" + page);
  }

  async function startJob(path, body, handlerName) {
    try {
      await api(path, { method: "POST", body });
      state.job = { name: handlerName };
      showJobBar(handlerName, "Memulai…", 0);
      poll();
      return true;
    } catch (err) {
      toast("Tidak bisa memulai", err.message, "err");
      return false;
    }
  }

  function showJobBar(name, phase, percent) {
    $("jobbar").hidden = false;
    $("job-title").textContent = JOB_TITLES[name] || name;
    $("job-phase").textContent = phase || "";
    $("job-percent").textContent = percent === null || percent === undefined ? "" : `${percent.toFixed(1)}%`;
    const bar = $("job-progress");
    if (percent === null || percent === undefined) bar.classList.add("indeterminate");
    else { bar.classList.remove("indeterminate"); bar.firstElementChild.style.width = Math.min(100, percent) + "%"; }
  }

  function poll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try {
        const job = await api("/api/job");
        if (job.state === "idle") return;
        showJobBar(job.name, job.phase + (job.text ? ` — ${job.text}` : ""), job.percent);
        const parts = [];
        if (job.status_line) parts.push(`<span><b>${esc(job.status_line)}</b></span>`);
        if (job.eta_seconds !== null && job.eta_seconds !== undefined) parts.push(`<span>Sisa waktu: <b>${fmtEta(job.eta_seconds)}</b></span>`);
        parts.push(`<span>Berjalan: <b>${fmtEta(Math.round(job.elapsed))}</b></span>`);
        $("job-meta").innerHTML = parts.join("");
        if (job.state === "running") { poll(); return; }
        $("jobbar").hidden = true;
        state.job = null;
        onJobDone(job);
      } catch (err) {
        toast("Koneksi putus", err.message, "err");
        state.job = null;
        $("jobbar").hidden = true;
      }
    }, 500);
  }

  function onJobDone(job) {
    const r = job.result || {};
    if (job.state === "error") { toast("Gagal", job.error || "error tidak diketahui", "err"); return; }
    if (job.state === "cancelled") toast("Dibatalkan", JOB_TITLES[job.name] || job.name);
    switch (job.name) {
      case "trash_restore":
        toast("Selesai", `${r.ok} dari ${r.total} file dipulihkan ke ${r.dest}`, "ok"); loadTrash(); break;
      case "fs_scan":
        toast("Pemindaian selesai", `${r.count} file dihapus ditemukan`, "ok"); loadFs(); break;
      case "fs_recover":
        toast("Pemulihan selesai", `${r.ok} dari ${r.total} file ke ${r.dest}`, "ok"); break;
      case "fs_preview":
        showPreview(r); break;
      case "carve":
        $("carve-recovered").textContent = r.recovered ?? 0;
        $("carve-complete").textContent = r.complete ?? 0;
        $("carve-skipped").textContent = r.skipped_by_date ?? 0;
        $("carve-result").textContent = `${job.state === "cancelled" ? "Dibatalkan. " : ""}Hasil di ${r.out || "-"}.`;
        toast(job.state === "cancelled" ? "Dibatalkan" : "Carving selesai", `${r.recovered ?? 0} file, ${r.complete ?? 0} utuh`, "ok");
        break;
      case "image":
        $("image-result").innerHTML = `${job.state === "cancelled" ? "<b>Dibatalkan.</b> " : ""}Disalin ${fmtSize(r.bytes)} dari ${fmtSize(r.total)}.<br>
          SHA-256: <code>${esc(r.sha256)}</code>${r.bad_blocks ? `<br><span class="tag warn">${r.bad_blocks} blok rusak diisi nol</span>` : ""}`;
        toast("Salin disk selesai", fmtSize(r.bytes), "ok"); break;
      case "repair":
        renderRepair(r); toast("Perbaikan selesai", `${r.ok} dari ${r.total} file`, "ok"); break;
      case "flash_diag":
        renderDiag(r); break;
      case "flash_surface":
        renderSurface(r); break;
      case "flash_repair":
        $("flash-result").innerHTML = `<div class="tag ${r.code === 0 ? "ok" : "warn"}">kode keluar ${r.code}</div>
          <pre class="code">$ ${esc(r.command)}\n${esc(r.output || "(tidak ada keluaran)")}</pre>`;
        toast("Perintah selesai", `kode ${r.code}`, r.code === 0 ? "ok" : "err"); break;
      default: break;
    }
  }

  // ---------- Recycle Bin ----------

  async function loadTrash() {
    try {
      const { from, to } = dateRange("trash-from", "trash-to");
      const qs = new URLSearchParams({ from, to }).toString();
      const data = await api("/api/trash?" + qs);
      state.trash = data.items; state.trashSel.clear();
      renderTrash();
    } catch (err) { toast("Gagal membaca trash", err.message, "err"); }
  }
  function renderTrash() {
    $("trash-count").textContent = state.trash.length;
    $("trash-selected").textContent = state.trashSel.size;
    $("trash-restore").disabled = state.trashSel.size === 0;
    $("trash-body").innerHTML = state.trash.length ? state.trash.map((it) => `
      <tr class="${state.trashSel.has(it.id) ? "selected" : ""}" data-id="${it.id}">
        <td><input type="checkbox" data-id="${it.id}" ${state.trashSel.has(it.id) ? "checked" : ""}></td>
        <td>${esc(it.name)}</td><td class="num">${fmtSize(it.size)}</td>
        <td>${fmtDate(it.deleted)}</td><td class="path" title="${esc(it.original || "")}">${esc(it.original || "-")}</td></tr>`).join("")
      : `<tr><td colspan="5" class="empty">Tidak ada item. Coba ubah filter tanggal.</td></tr>`;
    $$("#trash-body input[type=checkbox]").forEach((cb) => cb.onchange = () => {
      const id = Number(cb.dataset.id);
      cb.checked ? state.trashSel.add(id) : state.trashSel.delete(id);
      renderTrash();
    });
    $("trash-all").checked = state.trash.length > 0 && state.trashSel.size === state.trash.length;
  }

  // ---------- File system (names) ----------

  async function loadFs() {
    try {
      const { from, to } = dateRange("fs-from", "fs-to");
      const match = $("fs-match").value.trim();
      const data = await api("/api/fs/entries?" + new URLSearchParams({ from, to, match }).toString());
      state.fs = data.items; state.fsTotal = data.total;
      $("fs-total").textContent = data.total;
      renderFs();
    } catch (err) { toast("Gagal memuat daftar", err.message, "err"); }
  }
  function renderFs() {
    $("fs-shown").textContent = state.fs.length;
    $("fs-selected").textContent = state.fsSel.size;
    $("fs-recover").disabled = state.fsSel.size === 0;
    $("fs-preview").disabled = state.fsSel.size !== 1;
    $("fs-body").innerHTML = state.fs.length ? state.fs.map((e) => `
      <tr class="${state.fsSel.has(e.id) ? "selected" : ""}">
        <td><input type="checkbox" data-id="${e.id}" ${state.fsSel.has(e.id) ? "checked" : ""}></td>
        <td class="path" title="${esc(e.path)}">${esc(e.path)}</td><td class="num">${fmtSize(e.size)}</td>
        <td><span class="tag">${esc(e.fs)}</span></td><td>${fmtDate(e.deleted)}</td></tr>`).join("")
      : `<tr><td colspan="5" class="empty">Belum ada hasil. Jalankan pencarian dulu.</td></tr>`;
    $$("#fs-body input[type=checkbox]").forEach((cb) => cb.onchange = () => {
      const id = Number(cb.dataset.id);
      cb.checked ? state.fsSel.add(id) : state.fsSel.delete(id);
      renderFs();
    });
  }

  async function exportFs(format) {
    try {
      const res = await fetch(`/api/fs/export?format=${format}`, { headers: { "X-Token": TOKEN } });
      if (!res.ok) throw new Error((await res.json()).error);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = format === "json" ? "daftar-file-dihapus.json" : "daftar-file-dihapus.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (err) { toast("Ekspor gagal", err.message, "err"); }
  }

  function showPreview(r) {
    const kind = r.kind || "";
    let body;
    if ([".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp"].includes(kind)) {
      body = `<img class="preview-img" src="${esc(r.url)}" alt="${esc(r.name)}">`;
    } else {
      body = `<div class="muted small">Pratinjau teks atau heksadesimal.</div><pre class="code" id="pv-text">Memuat…</pre>`;
    }
    const m = modal(`Pratinjau: ${r.name}`, `${r.partial ? `<span class="tag warn">data tidak lengkap</span> ` : ""}
      <span class="muted small">${fmtSize(r.size)}</span><div style="margin-top:12px">${body}</div>`);
    if (!body.includes("<img")) {
      fetch(r.url).then((res) => res.arrayBuffer()).then((buf) => {
        const bytes = new Uint8Array(buf).slice(0, 4096);
        const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
        const printable = /^[\x09\x0a\x0d\x20-\x7e -￿]*$/.test(text.slice(0, 512));
        let out = text;
        if (!printable || ![".txt", ".csv", ".log", ".xml", ".json", ".md", ".html", ".py", ".ini", ".cfg"].includes(kind)) {
          out = Array.from(bytes.slice(0, 512)).map((b, i) => (i % 16 === 0 ? (i ? "\n" : "") + i.toString(16).padStart(8, "0") + "  " : "") + b.toString(16).padStart(2, "0") + " ").join("");
        }
        $("pv-text").textContent = out || "(kosong)";
      }).catch((err) => { $("pv-text").textContent = "Gagal memuat: " + err.message; });
    }
    m.root.querySelector(".modal").dataset.kind = kind;
  }

  // ---------- Carving ----------

  function renderChips() {
    $("ext-chips").innerHTML = EXTS.map((e) =>
      `<span class="chip ${state.exts.has(e) ? "on" : ""}" data-ext="${e}">${e.toUpperCase()}</span>`).join("");
    $$("#ext-chips .chip").forEach((c) => c.onclick = () => {
      const e = c.dataset.ext;
      state.exts.has(e) ? state.exts.delete(e) : state.exts.add(e);
      renderChips();
    });
  }

  // ---------- Flashdisk ----------

  function renderDiag(r) {
    const tone = r.status === "ok" ? "ok" : r.status === "raw" || r.status === "unreadable" ? "bad" : "warn";
    $("flash-result").innerHTML = `
      <div class="stats" style="margin-bottom:12px">
        <div class="stat"><div class="label">Status</div><div class="value"><span class="tag ${tone}">${esc(r.status.toUpperCase())}</span></div></div>
        <div class="stat"><div class="label">Ukuran</div><div class="value">${fmtSize(r.size_bytes)}</div></div>
        <div class="stat"><div class="label">Sistem file</div><div class="value">${esc(r.file_system)}</div></div>
      </div>
      <p>${esc(r.message)}</p>
      ${r.partitions && r.partitions.length ? `<div class="muted small">Partisi: ${esc(r.partitions.join(", "))}</div>` : ""}`;
  }
  function renderSurface(r) {
    const body = r.healthy
      ? `<span class="tag ok">SEHAT</span> <span class="muted">${fmtSize(r.bytes_read)} dibaca, tidak ada sektor rusak.</span>`
      : `<span class="tag bad">${r.bad_count} RENTANG RUSAK</span> <span class="muted">${fmtSize(r.bad_bytes)} tidak terbaca. Salin ke image sebelum perbaikan.</span>`;
    $("flash-result").innerHTML = `<p>${body}</p>` + (r.cancelled ? `<p class="muted">Uji dibatalkan di tengah jalan.</p>` : "");
  }

  // ---------- Repair ----------

  function renderRepair(r) {
    $("repair-ok").textContent = r.ok ?? 0;
    $("repair-total").textContent = r.total ?? 0;
    $("repair-body").innerHTML = (r.results || []).map((x) => `
      <tr><td>${esc(x.file)}</td><td><span class="tag ${x.ok ? "ok" : "bad"}">${x.ok ? "OK" : "GAGAL"}</span></td>
      <td class="small muted" style="white-space:normal">${esc((x.actions || []).join("; ") || "-")}</td></tr>`).join("")
      || `<tr><td colspan="3" class="empty">Belum ada hasil.</td></tr>`;
  }

  // ---------- disks ----------

  async function loadDisks() {
    try {
      const data = await api("/api/disks");
      state.disks = data.disks;
      ["fs-disk", "carve-disk", "image-disk", "flash-disk"].forEach((id) => {
        const sel = $(id);
        sel.innerHTML = `<option value="">— pilih disk —</option>` + state.disks.map((d) =>
          `<option value="${esc(d.path)}">${esc(d.desc)}</option>`).join("");
        sel.onchange = () => { const target = { "fs-disk": "fs-src", "carve-disk": "carve-src", "image-disk": "image-src", "flash-disk": "flash-src" }[id]; $(target).value = sel.value; };
      });
    } catch (err) { toast("Daftar disk tidak tersedia", err.message, "err"); }
  }

  // ---------- events ----------

  function bind() {
    $$(".nav-item[data-page]").forEach((b) => b.onclick = () => setNav(b.dataset.page));
    window.addEventListener("hashchange", () => {
      const page = location.hash.replace("#", "");
      if (["trash", "deleted", "carve", "image", "flash", "repair"].includes(page) && page !== state.page) setNav(page);
    });

    // theme
    const root = document.documentElement;
    try { const t = localStorage.getItem("fr-theme"); if (t) root.dataset.theme = t; } catch (_) {}
    $("theme-toggle").onclick = () => {
      const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("fr-theme", root.dataset.theme); } catch (_) {}
    };

    // trash
    $("trash-apply").onclick = loadTrash;
    $("trash-clear").onclick = () => { $("trash-from").value = ""; $("trash-to").value = ""; loadTrash(); };
    $("trash-all").onchange = (e) => {
      state.trashSel = e.target.checked ? new Set(state.trash.map((t) => t.id)) : new Set();
      renderTrash();
    };
    $("trash-restore").onclick = () => openBrowser({
      mode: "dir", title: "Pilih folder tujuan",
      onPick: (dest) => startJob("/api/trash/restore", { ids: [...state.trashSel], dest }, "trash_restore"),
    });

    // deleted
    $("fs-scan").onclick = () => {
      const source = $("fs-src").value.trim();
      if (!source) return toast("Sumber kosong", "Pilih disk atau file image dulu.", "err");
      state.fsSel.clear();
      startJob("/api/fs/scan", { source, ...dateRange("fs-from", "fs-to") }, "fs_scan");
    };
    $("fs-match").oninput = () => loadFs();
    $("fs-from").onchange = loadFs;
    $("fs-to").onchange = loadFs;
    $("fs-select-all").onclick = () => {
      const ids = state.fs.map((e) => e.id);
      state.fsSel = state.fsSel.size === ids.length ? new Set() : new Set(ids);
      renderFs();
    };
    $("fs-preview").onclick = () => {
      const [id] = [...state.fsSel];
      startJob("/api/fs/preview", { id }, "fs_preview");
    };
    $("fs-recover").onclick = () => openBrowser({
      mode: "dir", title: "Pilih folder tujuan (bukan di disk yang dipindai)",
      onPick: async (dest) => {
        try {
          await api("/api/check", { method: "POST", body: { source: $("fs-src").value.trim(), dest } });
        } catch (err) { return toast("Tujuan tidak aman", err.message, "err"); }
        startJob("/api/fs/recover", { ids: [...state.fsSel], dest }, "fs_recover");
      },
    });
    $("fs-export-csv").onclick = (e) => { e.preventDefault(); exportFs("csv"); };
    $("fs-export-json").onclick = (e) => { e.preventDefault(); exportFs("json"); };

    // carving
    renderChips();
    $("ext-all").onclick = () => { state.exts = new Set(EXTS); renderChips(); };
    $("ext-none").onclick = () => { state.exts = new Set(); renderChips(); };
    $("carve-start").onclick = () => {
      const source = $("carve-src").value.trim(), out = $("carve-out").value.trim();
      if (!source || !out) return toast("Lengkapi isian", "Sumber dan folder hasil wajib diisi.", "err");
      if (!state.exts.size) return toast("Pilih jenis file", "Minimal satu jenis.", "err");
      startJob("/api/carve", { source, out, exts: [...state.exts], ...dateRange("carve-from", "carve-to") }, "carve");
    };

    // image
    $("image-start").onclick = () => {
      const source = $("image-src").value.trim(), out = $("image-out").value.trim();
      if (!source || !out) return toast("Lengkapi isian", "Sumber dan file tujuan wajib diisi.", "err");
      startJob("/api/image", { source, out }, "image");
    };

    // flash
    const dev = () => $("flash-src").value.trim();
    const needDev = () => { if (!dev()) { toast("Pilih perangkat", "Pilih disk atau file image dulu.", "err"); return false; } return true; };
    $("flash-diag").onclick = () => needDev() && startJob("/api/flash/diagnose", { device: dev() }, "flash_diag");
    $("flash-surface").onclick = () => needDev() && startJob("/api/flash/surface", { device: dev() }, "flash_surface");
    $("flash-image").onclick = () => { if (!needDev()) return; $("image-src").value = dev(); setNav("image"); toast("Salin ke image", "Pilih lokasi file tujuan, lalu tekan Mulai salin."); };
    $("flash-carve").onclick = () => { if (!needDev()) return; $("carve-src").value = dev(); setNav("carve"); toast("Carving dari image", "Atur folder hasil lalu tekan Mulai pemindaian."); };
    $("flash-check").onclick = () => needDev() && startJob("/api/flash/repair", { device: dev(), mode: "check" }, "flash_repair");
    $("flash-fix").onclick = () => {
      if (!needDev()) return;
      const m = modal("Perbaiki sistem file?", `<p>Perbaikan <b>menulis ke perangkat</b> dan bisa mengubah data yang belum disalin.</p>
        <p class="muted small">Pastikan Anda sudah menyalin ke image. Perangkat sistem tidak akan diperbaiki.</p>
        <label class="row" style="gap:8px"><input type="checkbox" id="fix-ok"> Saya sudah menyalin dan mengerti risikonya</label>`,
        `<button class="btn" data-close>Batal</button><button class="btn btn-danger" id="fix-go" disabled>Perbaiki</button>`);
      const ok = m.root.querySelector("#fix-ok"), go = m.root.querySelector("#fix-go");
      ok.onchange = () => go.disabled = !ok.checked;
      go.onclick = () => { m.close(); startJob("/api/flash/repair", { device: dev(), mode: "fix" }, "flash_repair"); };
    };

    // repair
    $("repair-start").onclick = () => {
      const source = $("repair-src").value.trim(), out = $("repair-out").value.trim();
      if (!source || !out) return toast("Lengkapi isian", "File/folder dan folder hasil wajib diisi.", "err");
      startJob("/api/repair", { source, out }, "repair");
    };

    // browse buttons
    $$("[data-browse]").forEach((btn) => btn.onclick = () => {
      const target = $(btn.dataset.browse);
      openBrowser({
        mode: btn.dataset.mode, title: btn.dataset.mode === "file" ? "Pilih file" : "Pilih lokasi",
        initial: target.value, onPick: (p) => { target.value = p; target.dispatchEvent(new Event("change")); },
      });
    });

    $("job-cancel").onclick = async () => {
      try { await api("/api/job/cancel", { method: "POST", body: {} }); toast("Membatalkan…", "menunggu langkah berikutnya selesai"); }
      catch (err) { toast("Gagal membatalkan", err.message, "err"); }
    };

    bindDebug();
  }

  // ---------- debug ----------

  function bindDebug() {
    $("debug-toggle").onchange = async (e) => {
      state.debugOn = e.target.checked;
      try { await api("/api/debug", { method: "POST", body: { enabled: state.debugOn } }); }
      catch (err) { toast("Mode debug gagal", err.message, "err"); }
      $("debug").hidden = !state.debugOn;
      document.body.classList.toggle("has-debug", state.debugOn);
      if (state.debugOn) startDebug(); else stopDebug();
    };
    $("dbg-close").onclick = () => { $("debug-toggle").checked = false; $("debug-toggle").onchange({ target: { checked: false } }); };
    $("dbg-level").onchange = renderLogFilter;
    $("dbg-clear").onclick = () => { $("dbg-log").innerHTML = ""; $("dbg-count").textContent = ""; };
    $("dbg-copy").onclick = async () => {
      const text = Array.from($("dbg-log").children).map((d) => d.dataset.raw).join("\n");
      try { await navigator.clipboard.writeText(text); toast("Log disalin", `${$("dbg-log").children.length} baris`, "ok"); }
      catch (_) { toast("Tidak bisa menyalin", "Pilih teks log secara manual.", "err"); }
    };
  }

  function startDebug() {
    stopDebug();
    const tick = async () => {
      try {
        const [logs, dbg] = await Promise.all([
          api("/api/logs?since=" + state.lastSeq),
          api("/api/debug"),
        ]);
        $("dbg-state").textContent = dbg.job && dbg.job.state === "running" ? "berjalan" : "idle";
        $("dbg-state").className = "tag " + (dbg.job && dbg.job.state === "running" ? "warn" : "");
        renderDebugStatus(dbg);
        if (logs.lines.length) appendLogs(logs.lines);
        state.lastSeq = logs.last;
      } catch (err) { $("dbg-state").textContent = "terputus"; }
      state.debugTimer = setTimeout(tick, 800);
    };
    tick();
  }
  function stopDebug() { clearTimeout(state.debugTimer); state.debugTimer = null; }

  function renderDebugStatus(dbg) {
    const j = dbg.job || {};
    const rows = [
      ["Pekerjaan", j.label || "-"], ["Status", j.state || "idle"], ["Tahap", j.phase || "-"],
      ["Keterangan", j.text || "-"], ["Progres", j.status_line || "-"],
      ["Persen", j.percent !== null && j.percent !== undefined ? j.percent + " %" : "-"],
      ["Sisa waktu", fmtEta(j.eta_seconds)], ["Berjalan", fmtEta(Math.round(j.elapsed || 0))],
      ["Thread aktif", dbg.threads], ["File log", dbg.log || "-"],
    ];
    $("dbg-status").innerHTML = "<dl>" + rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") + "</dl>";
  }

  function lineHtml(r) {
    const raw = `${r.time} ${r.level} [${r.logger}] ${r.message}`;
    return { raw, html: `<span class="t">${esc(r.time)}</span><span class="lvl">${esc(r.level)}</span><span class="lg">${esc(r.logger)}</span>${esc(r.message)}` };
  }
  function appendLogs(rows) {
    const box = $("dbg-log");
    const frag = document.createDocumentFragment();
    rows.forEach((r) => {
      const { raw, html } = lineHtml(r);
      const div = document.createElement("div");
      div.className = r.level;
      div.dataset.level = r.level;
      div.dataset.raw = raw;
      div.innerHTML = html;
      frag.appendChild(div);
    });
    box.appendChild(frag);
    while (box.children.length > 2000) box.removeChild(box.firstChild);
    renderLogFilter();
    if ($("dbg-follow").checked) box.scrollTop = box.scrollHeight;
    $("dbg-count").textContent = `${box.children.length} baris`;
  }
  function renderLogFilter() {
    const level = $("dbg-level").value;
    Array.from($("dbg-log").children).forEach((d) => {
      const order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
      d.hidden = level !== "ALL" && order.indexOf(d.dataset.level) < order.indexOf(level);
    });
  }

  // ---------- start ----------

  async function init() {
    bind();
    try {
      const info = await api("/api/info");
      $("version").textContent = "v" + info.version;
      $("pill-admin").textContent = `${info.os} · ${info.admin ? "Administrator" : "bukan admin"}`;
      $("dot-admin").className = "dot " + (info.admin ? "ok" : "warn");
    } catch (err) { toast("Server tidak merespons", err.message, "err"); }
    loadDisks();
    const hash = location.hash.replace("#", "");
    setNav(["trash", "deleted", "carve", "image", "flash", "repair"].includes(hash) ? hash : "trash");
    // resume a running job after reload
    try {
      const job = await api("/api/job");
      if (job.state === "running") { state.job = { name: job.name }; poll(); }
    } catch (_) { /* ignore */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
