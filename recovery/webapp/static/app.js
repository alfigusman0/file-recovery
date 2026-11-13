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
    flash_repair: "Perbaikan sistem file", capacity: "Uji kapasitas", capacity_test: "Uji kapasitas",
    flash_capacity: "Uji kapasitas",
  };
  // Job names onJobDone knows; any other name falls back to the handler that started the job.
  const KNOWN_JOBS = new Set(["trash_restore", "fs_scan", "fs_recover", "fs_preview", "carve", "image",
    "repair", "flash_diag", "flash_surface", "flash_repair", "capacity"]);
  const CAPACITY_ALIASES = new Set(["capacity", "capacity_test", "flash_capacity"]);
  const MAX_ROWS = 2000;  // rows drawn per table; filters still count everything
  const CONFIDENCE = {
    high: { label: "Tinggi", tone: "ok",
      tip: "Keyakinan tinggi: struktur file lengkap dan cocok dari awal sampai akhir. Kemungkinan besar file bisa dibuka normal." },
    medium: { label: "Sedang", tone: "warn",
      tip: "Keyakinan sedang: awal file dan sebagian strukturnya cocok, tetapi ada bagian yang tidak bisa dipastikan. File mungkin terbuka dengan sedikit kerusakan." },
    low: { label: "Rendah", tone: "bad",
      tip: "Keyakinan rendah: hanya tanda awal file yang cocok. Isinya bisa terpotong atau tercampur data lain." },
  };
  const STUCK_TIP = "Perangkat tidak merespons. Cabut drive, colok kembali, lalu muat ulang daftar disk.";
  // source input -> operation buttons that must stay disabled while that source is a stuck device
  const STUCK_TARGETS = {
    fs: { src: "fs-src", buttons: ["fs-scan"] },
    carve: { src: "carve-src", buttons: ["carve-start"] },
    image: { src: "image-src", buttons: ["image-start"] },
    flash: { src: "flash-src", buttons: ["flash-diag", "flash-surface", "flash-image", "flash-carve", "flash-check", "flash-fix"] },
  };
  // Buttons that start a server job: all disabled while any job runs.
  const JOB_BUTTONS = ["trash-restore", "fs-scan", "fs-recover", "fs-preview", "carve-start", "image-start",
    "repair-start", "flash-diag", "flash-surface", "flash-check", "flash-fix", "cap-start"];
  const BUSY_TIP = "Pekerjaan lain sedang berjalan. Tunggu sampai selesai atau batalkan dulu.";

  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const state = {
    page: "trash", job: null, pollTimer: null, debugOn: false, debugTimer: null, lastSeq: 0,
    trash: [], trashSel: new Set(), fs: [], fsSel: new Set(), fsTotal: 0,
    exts: new Set(EXTS), repairRows: [], disks: [], stuck: new Set(),
    carve: [], lastHandler: null, filters: {},
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
  function baseName(p) { return String(p || "").split(/[\\/]/).filter(Boolean).pop() || String(p || ""); }

  // Per-item problems reported by the server ("errors" / "problems"): first few messages plus a count.
  function problemText(list, max = 3) {
    if (!Array.isArray(list) || !list.length) return "";
    const one = (x) => {
      if (x === null || x === undefined) return "";
      if (typeof x !== "object") return String(x);
      const who = x.name || x.path || x.file || x.original || (x.id !== undefined ? `#${x.id}` : "");
      const why = x.error || x.message || x.reason || x.problem || "";
      return who && why ? `${baseName(who)}: ${why}` : String(who || why || JSON.stringify(x));
    };
    const head = list.slice(0, max).map(one).filter(Boolean).join("; ");
    const rest = list.length - Math.min(list.length, max);
    return head + (rest > 0 ? ` (+${rest} masalah lain)` : "");
  }

  // ---------- result filters (client-side) ----------

  // Derived fields used by the filters, cached on the item.
  function facts(it) {
    if (it._f) return it._f;
    const path = it.path || it.output || it.file || it.name || "";
    const name = it.name || baseName(path);
    let ext = String(it.ext || it.extension || "").toLowerCase().replace(/^\./, "");
    if (!ext) { const m = /\.([a-z0-9]{1,8})$/i.exec(name); ext = m ? m[1].toLowerCase() : ""; }
    let status = null;
    if (typeof it.complete === "boolean") status = it.complete ? "utuh" : "sebagian";
    else if (typeof it.partial === "boolean") status = it.partial ? "sebagian" : "utuh";
    else if (it.status) {
      const s = String(it.status).toLowerCase();
      if (["utuh", "complete", "full", "ok"].includes(s)) status = "utuh";
      else if (["sebagian", "partial", "truncated"].includes(s)) status = "sebagian";
    }
    const rawSize = it.size ?? it.length ?? it.bytes;
    const size = rawSize === null || rawSize === undefined || rawSize === "" ? null : Number(rawSize);
    const conf = String(it.confidence || "").toLowerCase();
    Object.defineProperty(it, "_f", {
      value: { path, name, ext, status, size: Number.isFinite(size) ? size : null,
               confidence: CONFIDENCE[conf] ? conf : null, hay: (name + "\n" + path).toLowerCase() },
      enumerable: false,
    });
    return it._f;
  }

  function statusTag(status) {
    if (status === "utuh") return `<span class="tag ok">Utuh</span>`;
    if (status === "sebagian") return `<span class="tag warn">Sebagian</span>`;
    return `<span class="muted">-</span>`;
  }
  function confidenceBadge(level) {
    const c = CONFIDENCE[level];
    if (!c) return "";
    return `<span class="tag ${c.tone} conf" title="${esc(c.tip)}" aria-label="${esc(c.tip)}" tabindex="0">${c.label}</span>`;
  }

  // Builds a filter bar into #<key>-filters. onChange() is called (debounced for typing) on any change.
  function makeFilter(key, onChange) {
    const root = $(`${key}-filters`);
    const id = (n) => `${key}-f-${n}`;
    root.innerHTML = `
      <div class="field f-search"><label for="${id("q")}">Cari nama / path</label>
        <input type="search" id="${id("q")}" placeholder="Ketik nama file atau folder…" autocomplete="off"></div>
      <div class="field"><label for="${id("type")}">Jenis file</label>
        <select id="${id("type")}"><option value="">Semua jenis</option></select></div>
      <div class="field"><label for="${id("min")}">Ukuran min (MB)</label>
        <input type="number" id="${id("min")}" min="0" step="any" inputmode="decimal" placeholder="0"></div>
      <div class="field"><label for="${id("max")}">Ukuran maks (MB)</label>
        <input type="number" id="${id("max")}" min="0" step="any" inputmode="decimal" placeholder="tanpa batas"></div>
      <div class="field" id="${id("status-wrap")}"><label for="${id("status")}">Status</label>
        <select id="${id("status")}"><option value="">Semua status</option>
          <option value="utuh">Utuh</option><option value="sebagian">Sebagian</option></select></div>
      <div class="f-foot"><span class="small f-count" id="${id("count")}" aria-live="polite">menampilkan 0 dari 0</span>
        <button type="button" class="btn btn-sm" id="${id("reset")}" disabled>Reset filter</button></div>`;
    let timer = null;
    const fire = () => { clearTimeout(timer); onChange(); };
    const later = () => { clearTimeout(timer); timer = setTimeout(onChange, 180); };
    $(id("q")).addEventListener("input", later);
    $(id("min")).addEventListener("input", later);
    $(id("max")).addEventListener("input", later);
    $(id("type")).addEventListener("change", fire);
    $(id("status")).addEventListener("change", fire);
    $(id("reset")).addEventListener("click", () => {
      ["q", "min", "max", "type", "status"].forEach((n) => { $(id(n)).value = ""; });
      fire();
    });

    const read = () => {
      const mb = (n) => { const v = $(id(n)).value.trim(); const x = Number(v); return v === "" || !Number.isFinite(x) ? null : x * 1024 * 1024; };
      return { q: $(id("q")).value.trim().toLowerCase(), type: $(id("type")).value,
               min: mb("min"), max: mb("max"), status: $(id("status")).value };
    };
    return {
      // Refresh the type/status options from the full list (keeps the current choice if still valid).
      setItems(items) {
        const types = [...new Set(items.map((it) => facts(it).ext))].sort();
        const sel = $(id("type")), prev = sel.value;
        sel.innerHTML = `<option value="">Semua jenis</option>` + types.map((t) =>
          `<option value="${esc(t)}">${t ? esc(t.toUpperCase()) : "(tanpa ekstensi)"}</option>`).join("");
        sel.value = types.includes(prev) ? prev : "";
        const hasStatus = items.some((it) => facts(it).status);
        $(id("status-wrap")).hidden = !hasStatus;
        if (!hasStatus) $(id("status")).value = "";
        return hasStatus;
      },
      apply(items) {
        const f = read();
        const active = !!(f.q || f.type || f.min !== null || f.max !== null || f.status);
        const out = active ? items.filter((it) => {
          const x = facts(it);
          if (f.q && !x.hay.includes(f.q)) return false;
          if (f.type && x.ext !== f.type) return false;
          if (f.min !== null && (x.size === null || x.size < f.min)) return false;
          if (f.max !== null && (x.size === null || x.size > f.max)) return false;
          if (f.status && x.status !== f.status) return false;
          return true;
        }) : items;
        $(id("count")).textContent = `menampilkan ${out.length.toLocaleString("id-ID")} dari ${items.length.toLocaleString("id-ID")}`;
        $(id("reset")).disabled = !active;
        return out;
      },
    };
  }

  function limitNote(elId, shown) {
    const el = $(elId);
    el.hidden = shown <= MAX_ROWS;
    el.textContent = `Tabel hanya menampilkan ${MAX_ROWS.toLocaleString("id-ID")} baris pertama. Persempit filter untuk melihat sisanya.`;
  }

  // ---------- modal ----------

  let modalSeq = 0;
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function modal(title, bodyHtml, footerHtml = "") {
    const opener = document.activeElement;
    const titleId = `modal-title-${++modalSeq}`;
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="${titleId}" tabindex="-1">
      <header><h3 id="${titleId}">${esc(title)}</h3><button type="button" class="btn btn-sm btn-icon" data-close aria-label="Tutup">✕</button></header>
      <div class="body">${bodyHtml}</div>
      ${footerHtml ? `<footer>${footerHtml}</footer>` : ""}</div>`;
    const dialog = back.querySelector(".modal");
    const focusables = () => $$(FOCUSABLE, dialog).filter((el) => el.getClientRects().length > 0);
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      back.remove();
      document.removeEventListener("keydown", onKey, true);
      if (opener && opener !== document.body && opener.isConnected && typeof opener.focus === "function") opener.focus();
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (e.key !== "Tab") return;
      // keep Tab / Shift+Tab inside the dialog
      const items = focusables();
      if (!items.length) { e.preventDefault(); dialog.focus(); return; }
      const first = items[0], last = items[items.length - 1], cur = document.activeElement;
      const inside = dialog.contains(cur);
      if (e.shiftKey) {
        if (!inside || cur === first || cur === dialog) { e.preventDefault(); last.focus(); }
      } else if (!inside || cur === last) { e.preventDefault(); first.focus(); }
    };
    back.addEventListener("click", (e) => { if (e.target === back || e.target.closest("[data-close]")) close(); });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(back);
    (focusables()[0] || dialog).focus();
    return { root: back, close, isOpen: () => !closed };
  }

  // Built-in file/folder picker (works without native dialogs)
  function openBrowser({ mode = "any", title = "Pilih", initial = "", onPick }) {
    let current = initial || "";
    const m = modal(title, `<div class="crumbs" id="br-path"></div>
      <div class="row" style="margin-bottom:10px"><button class="btn btn-sm" id="br-up">⬆ Naik</button>
      <div id="br-roots" class="chips"></div></div>
      <div class="list" id="br-list"><div class="empty">Memuat…</div></div>`,
      `${mode === "save" ? `<label for="br-name" class="small">Nama file</label><input type="text" id="br-name" value="hasil-pemulihan" style="max-width:260px">` : ""}
       ${mode !== "file" ? `<button type="button" class="btn btn-primary" id="br-choose">Pilih folder ini</button>` : ""}`);
    const q = (id) => m.root.querySelector("#" + id);
    let reqSeq = 0;  // only the newest browse response may update the dialog
    const load = async (path) => {
      const seq = ++reqSeq;
      const fresh = () => seq === reqSeq && m.isOpen();
      const choose = q("br-choose");
      if (choose) choose.disabled = true;  // re-enabled once this folder is shown
      try {
        const data = await api("/api/browse?path=" + encodeURIComponent(path || ""));
        if (!fresh()) return;
        const pathEl = q("br-path"), up = q("br-up"), roots = q("br-roots"), list = q("br-list");
        if (!pathEl || !up || !roots || !list) return;
        current = data.path || "";
        pathEl.textContent = current || "Pilih drive atau lokasi";
        up.disabled = !data.parent;
        up.onclick = () => load(data.parent || "");
        roots.innerHTML = (data.roots || []).map((r) =>
          `<button type="button" class="chip" data-root="${esc(r.path)}">${esc(r.name)}</button>`).join("");
        $$("[data-root]", roots).forEach((b) => b.onclick = () => load(b.dataset.root));
        if (choose) choose.disabled = false;
        if (!data.entries || !data.entries.length) {
          list.innerHTML = `<div class="empty">${current ? "Folder kosong" : "Pilih drive di atas"}</div>`;
          return;
        }
        list.innerHTML = data.entries.map((e) => `
          <button type="button" data-path="${esc(e.path)}" data-dir="${e.is_dir ? 1 : 0}">
            <span>${e.is_dir ? "📁" : "📄"}</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(e.name)}</span>
            <span class="muted small">${e.is_dir ? "" : fmtSize(e.size)}</span></button>`).join("");
        $$("button[data-path]", list).forEach((b) => b.onclick = () => {
          if (b.dataset.dir === "1") return load(b.dataset.path);
          if (mode === "file" || mode === "any") { m.close(); onPick(b.dataset.path); }
        });
      } catch (err) {
        if (!fresh()) return;
        const list = q("br-list");
        if (list) list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
        if (choose) choose.disabled = false;
      }
    };
    const choose = q("br-choose");
    if (choose) choose.onclick = () => {
      if (!m.isOpen()) return;
      const nameEl = q("br-name");
      if (mode === "save") { m.close(); onPick(current.replace(/[\\/]$/, "") + "/" + ((nameEl && nameEl.value) || "hasil")); }
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
    refreshStuck();  // sources may have been filled in programmatically (e.g. flashdisk -> carving)
    history.replaceState(null, "", "#" + page);
  }

  async function startJob(path, body, handlerName) {
    if (state.job) {
      toast("Pekerjaan lain berjalan", "Tunggu sampai selesai atau batalkan dulu.", "err");
      return false;
    }
    try {
      await api(path, { method: "POST", body });
      state.job = { name: handlerName };
      state.lastHandler = handlerName;
      syncRunning();
      showJobBar(handlerName, "Memulai…", 0);
      poll();
      return true;
    } catch (err) {
      toast("Tidak bisa memulai", err.message, "err");
      return false;
    }
  }

  // Controls that must not start a second job while one runs.
  // A button is disabled when a job runs, when its source is a stuck device (data-stuck, set by
  // refreshStuck) or when it needs a selection that is missing.
  function selectionBlocks(id) {
    if (id === "trash-restore") return state.trashSel.size === 0;
    if (id === "fs-recover") return state.fsSel.size === 0;
    if (id === "fs-preview") return state.fsSel.size !== 1;
    return false;
  }
  function syncRunning() {
    const busy = !!state.job;
    setBusyButton($("cap-start"), busy, "Pekerjaan berjalan…");
    JOB_BUTTONS.forEach((id) => {
      const b = $(id);
      if (!b) return;
      b.disabled = busy || !!b.dataset.stuck || selectionBlocks(id);
      if (b.dataset.stuck) b.title = STUCK_TIP;
      else if (busy) {
        if (b.dataset.busyTitle === undefined) b.dataset.busyTitle = b.title || "";
        b.title = BUSY_TIP;
      } else if (b.dataset.busyTitle !== undefined) {
        b.title = b.dataset.busyTitle;
        delete b.dataset.busyTitle;
      }
    });
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
        state.pollFailures = 0;
        if (job.state === "idle") {
          state.job = null;
          $("jobbar").hidden = true;
          syncRunning();
          return;
        }
        showJobBar(job.name, job.phase + (job.text ? ` — ${job.text}` : ""), job.percent);
        const parts = [];
        if (job.status_line) parts.push(`<span><b>${esc(job.status_line)}</b></span>`);
        if (job.eta_seconds !== null && job.eta_seconds !== undefined) parts.push(`<span>Sisa waktu: <b>${fmtEta(job.eta_seconds)}</b></span>`);
        parts.push(`<span>Berjalan: <b>${fmtEta(Math.round(job.elapsed))}</b></span>`);
        $("job-meta").innerHTML = parts.join("");
        if (job.state === "running") { poll(); return; }
        $("jobbar").hidden = true;
        state.job = null;
        syncRunning();
        onJobDone(job);
      } catch (err) {
        // A single failed request (busy server, brief network blip) must not drop the job.
        // Keep polling and give up only after several failures in a row.
        state.pollFailures = (state.pollFailures || 0) + 1;
        if (state.pollFailures < 5) {
          $("job-phase").textContent = `Menunggu server… (percobaan ${state.pollFailures})`;
          poll();
          return;
        }
        toast("Koneksi putus", err.message, "err");
        state.pollFailures = 0;
        state.job = null;
        syncRunning();
        $("jobbar").hidden = true;
      }
    }, 500);
  }

  function onJobDone(job) {
    const r = job.result || {};
    if (job.state === "error") { toast("Gagal", job.error || "error tidak diketahui", "err"); return; }
    if (job.state === "cancelled") toast("Dibatalkan", JOB_TITLES[job.name] || job.name);
    let name = job.name;
    if (CAPACITY_ALIASES.has(name)) name = "capacity";
    else if (!KNOWN_JOBS.has(name) && state.lastHandler) name = state.lastHandler;
    switch (name) {
      case "trash_restore": {
        const issues = problemText(r.errors);
        toast("Selesai", `${r.ok} dari ${r.total} file dipulihkan ke ${r.dest}` + (issues ? `. Masalah: ${issues}` : ""),
          issues ? "err" : "ok");
        loadTrash(); break;
      }
      case "fs_scan":
        toast("Pemindaian selesai", `${r.count} file dihapus ditemukan`, "ok"); loadFs(); break;
      case "fs_recover": {
        const issues = problemText(r.problems);
        toast("Pemulihan selesai", `${r.ok} dari ${r.total} file ke ${r.dest}` + (issues ? `. Masalah: ${issues}` : ""),
          issues ? "err" : "ok");
        break;
      }
      case "fs_preview":
        showPreview(r); break;
      case "carve":
        $("carve-recovered").textContent = r.recovered ?? 0;
        $("carve-complete").textContent = r.complete ?? 0;
        $("carve-skipped").textContent = r.skipped_by_date ?? 0;
        $("carve-result").textContent = `${job.state === "cancelled" ? "Dibatalkan. " : ""}Hasil di ${r.out || "-"}.`;
        toast(job.state === "cancelled" ? "Dibatalkan" : "Carving selesai", `${r.recovered ?? 0} file, ${r.complete ?? 0} utuh`, "ok");
        setCarveFiles(r);
        break;
      case "capacity":
        renderCapacity(r, job.state === "cancelled"); break;
      case "image": {
        const cancelled = job.state === "cancelled" || r.cancelled === true;
        $("image-result").innerHTML = `${cancelled ? "<b>Salin disk dibatalkan.</b> " : ""}Disalin ${fmtSize(r.bytes)} dari ${fmtSize(r.total)}.<br>
          SHA-256: <code>${esc(r.sha256)}</code>${r.bad_blocks ? `<br><span class="tag warn">${r.bad_blocks} blok rusak diisi nol</span>` : ""}`;
        // job.state "cancelled" already raised a "Dibatalkan" toast above
        if (job.state !== "cancelled") toast(cancelled ? "Salin disk dibatalkan" : "Salin disk selesai", fmtSize(r.bytes), cancelled ? "info" : "ok");
        break;
      }
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
    $("trash-body").innerHTML = state.trash.length ? state.trash.slice(0, MAX_ROWS).map((it) => `
      <tr class="${state.trashSel.has(it.id) ? "selected" : ""}" data-id="${it.id}">
        <td><input type="checkbox" data-id="${it.id}" aria-label="Pilih ${esc(it.name)}" ${state.trashSel.has(it.id) ? "checked" : ""}></td>
        <td>${esc(it.name)}</td><td class="num">${fmtSize(it.size)}</td>
        <td>${fmtDate(it.deleted)}</td><td class="path" title="${esc(it.original || "")}">${esc(it.original || "-")}</td></tr>`).join("")
      : `<tr><td colspan="5" class="empty">Tidak ada item. Coba ubah filter tanggal.</td></tr>`;
    limitNote("trash-limit", state.trash.length);
    syncTrashSelection();
  }
  // Counters, buttons and select-all state after a selection change (no table rebuild).
  function syncTrashSelection() {
    $("trash-selected").textContent = state.trashSel.size;
    const all = $("trash-all");
    all.checked = state.trash.length > 0 && state.trashSel.size === state.trash.length;
    all.indeterminate = state.trashSel.size > 0 && state.trashSel.size < state.trash.length;
    syncRunning();
  }
  // Sets every drawn row's checkbox / row class from the selection set (used by select-all).
  function syncRows(bodyId, sel) {
    $$(`#${bodyId} input[type=checkbox][data-id]`).forEach((cb) => {
      const on = sel.has(Number(cb.dataset.id));
      cb.checked = on;
      cb.closest("tr").classList.toggle("selected", on);
    });
  }

  // ---------- File system (names) ----------

  async function loadFs() {
    try {
      const { from, to } = dateRange("fs-from", "fs-to");
      // name/path search runs client-side in the filter bar
      const data = await api("/api/fs/entries?" + new URLSearchParams({ from, to }).toString());
      state.fs = data.items || []; state.fsTotal = data.total;
      $("fs-total").textContent = data.total;
      const ids = new Set(state.fs.map((e) => e.id));
      state.fsSel = new Set([...state.fsSel].filter((id) => ids.has(id)));
      state.fsHasStatus = state.filters.fs.setItems(state.fs);
      renderFs();
    } catch (err) { toast("Gagal memuat daftar", err.message, "err"); }
  }
  function renderFs() {
    const shown = state.filters.fs.apply(state.fs);
    state.fsShown = shown;
    const withStatus = !!state.fsHasStatus;
    $$(".fs-status-col").forEach((th) => th.hidden = !withStatus);
    const cols = withStatus ? 6 : 5;
    $("fs-shown").textContent = shown.length;
    $("fs-body").innerHTML = shown.length ? shown.slice(0, MAX_ROWS).map((e) => `
      <tr class="${state.fsSel.has(e.id) ? "selected" : ""}">
        <td><input type="checkbox" data-id="${e.id}" aria-label="Pilih ${esc(facts(e).name || e.path)}" ${state.fsSel.has(e.id) ? "checked" : ""}></td>
        <td class="path" title="${esc(e.path)}">${esc(e.path)}</td><td class="num">${fmtSize(e.size)}</td>
        <td><span class="tag">${esc(e.fs)}</span></td>${withStatus ? `<td>${statusTag(facts(e).status)}</td>` : ""}<td>${fmtDate(e.deleted)}</td></tr>`).join("")
      : `<tr><td colspan="${cols}" class="empty">${state.fs.length ? "Tidak ada file yang cocok dengan filter." : "Belum ada hasil. Jalankan pencarian dulu."}</td></tr>`;
    limitNote("fs-limit", shown.length);
    syncFsSelection();
  }
  function syncFsSelection() {
    $("fs-selected").textContent = state.fsSel.size;
    syncRunning();  // fs-recover / fs-preview depend on the selection and on a running job
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
      const pvText = () => m.root.querySelector("#pv-text");
      fetch(r.url).then(async (res) => {
        if (!res.ok) {
          let msg = `HTTP ${res.status}`;
          try { const d = await res.json(); if (d && d.error) msg = d.error; } catch (_) { /* not JSON */ }
          throw new Error(msg);
        }
        return res.arrayBuffer();
      }).then((buf) => {
        const el = pvText();
        if (!el) return;  // dialog closed meanwhile
        const bytes = new Uint8Array(buf).slice(0, 4096);
        const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
        const printable = /^[\x09\x0a\x0d\x20-\x7e -￿]*$/.test(text.slice(0, 512));
        let out = text;
        if (!printable || ![".txt", ".csv", ".log", ".xml", ".json", ".md", ".html", ".py", ".ini", ".cfg"].includes(kind)) {
          out = Array.from(bytes.slice(0, 512)).map((b, i) => (i % 16 === 0 ? (i ? "\n" : "") + i.toString(16).padStart(8, "0") + "  " : "") + b.toString(16).padStart(2, "0") + " ").join("");
        }
        el.textContent = out || "(kosong)";
      }).catch((err) => {
        const el = pvText();
        if (el) el.textContent = "Pratinjau tidak bisa dimuat: " + err.message;
        toast("Pratinjau gagal", err.message, "err");
      });
    }
    m.root.querySelector(".modal").dataset.kind = kind;
  }

  // ---------- Carving ----------

  // File-type toggles: real buttons with aria-pressed, built once and updated in place (keeps focus).
  function renderChips() {
    $("ext-chips").innerHTML = EXTS.map((e) =>
      `<button type="button" class="chip" data-ext="${e}" aria-pressed="false">${e.toUpperCase()}</button>`).join("");
    $$("#ext-chips .chip").forEach((c) => c.onclick = () => {
      const e = c.dataset.ext;
      state.exts.has(e) ? state.exts.delete(e) : state.exts.add(e);
      syncChips();
    });
    syncChips();
  }
  function syncChips() {
    $$("#ext-chips .chip").forEach((c) => {
      const on = state.exts.has(c.dataset.ext);
      c.classList.toggle("on", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // The carve result may carry a per-file list (with an optional 'confidence' field). The current
  // server sends counts only, so the file card stays hidden unless real rows arrive.
  function setCarveFiles(r) {
    const list = [r.files, r.items, r.results, r.recovered_files, r.recovered].find(Array.isArray);
    state.carve = (list || []).filter((x) => x && typeof x === "object");
    state.filters.carve.setItems(state.carve);
    renderCarve();
  }
  function renderCarve() {
    const has = state.carve.length > 0;
    $("carve-files-card").hidden = !has;
    $("carve-filters").hidden = !has;
    if (!has) {
      $$(".carve-conf-col").forEach((el) => el.hidden = true);
      $("carve-body").innerHTML = "";
      $("carve-limit").hidden = true;
      return;
    }
    const shown = state.filters.carve.apply(state.carve);
    const hasConf = state.carve.some((it) => facts(it).confidence);
    $$(".carve-conf-col").forEach((el) => el.hidden = !hasConf);
    const cols = hasConf ? 5 : 4;
    $("carve-body").innerHTML = shown.length ? shown.slice(0, MAX_ROWS).map((it) => {
      const x = facts(it);
      return `<tr><td class="path" title="${esc(x.path)}">${esc(x.path || x.name)}</td>
        <td>${x.ext ? `<span class="tag">${esc(x.ext.toUpperCase())}</span>` : "-"}</td>
        <td class="num">${fmtSize(x.size)}</td><td>${statusTag(x.status)}</td>
        ${hasConf ? `<td>${confidenceBadge(x.confidence)}</td>` : ""}</tr>`;
    }).join("") : `<tr><td colspan="${cols}" class="empty">Tidak ada file yang cocok dengan filter.</td></tr>`;
    limitNote("carve-limit", shown.length);
  }

  // ---------- capacity test ----------

  function capacitySizeMb() {
    const v = $("cap-size").value;
    if (v !== "custom") return Number(v);
    const n = Number($("cap-custom").value);
    return Number.isFinite(n) && n >= 1 ? Math.floor(n) : null;
  }
  function firstOf(r, keys) {
    for (const k of keys) if (r[k] !== undefined && r[k] !== null && r[k] !== "") return [k, r[k]];
    return [null, null];
  }
  function fmtSpeed([key, v]) {
    if (v === null) return "-";
    if (typeof v === "string") return v;
    const n = Number(v);
    if (!Number.isFinite(n)) return "-";
    return `${n.toFixed(1)} MB/s`;
  }
  function renderCapacity(r, cancelled) {
    const verdict = r.verdict ? String(r.verdict) : (cancelled ? "Uji dibatalkan." : "Tidak ada kesimpulan dari server.");
    let tone = "";
    if (r.ok === true || r.genuine === true || r.passed === true) tone = "ok";
    else if (r.ok === false || r.genuine === false || r.passed === false || r.fake === true) tone = "bad";
    else if (/palsu|fake|rusak|gagal|tidak cocok|error/i.test(verdict)) tone = "bad";
    const [, written] = firstOf(r, ["bytes_written", "written_bytes", "written"]);
    const [, verified] = firstOf(r, ["bytes_verified_ok", "bytes_verified", "verified_bytes", "verified", "bytes_ok"]);
    // Server contract: write_mb_per_s / read_mb_per_s are always MB/s
    const write = [null, r.write_mb_per_s ?? null];
    const read = [null, r.read_mb_per_s ?? null];
    const size = (v) => (typeof v === "number" || /^\d+$/.test(String(v ?? ""))) ? fmtSize(Number(v)) : (v ?? "-");
    $("cap-result").className = "";
    $("cap-result").innerHTML = `
      <div class="verdict ${tone}">${cancelled ? `<span class="tag warn">dibatalkan</span> ` : ""}${esc(verdict)}</div>
      <div class="stats" style="margin-top:12px">
        <div class="stat"><div class="label">Ditulis</div><div class="value">${esc(size(written))}</div></div>
        <div class="stat"><div class="label">Terverifikasi</div><div class="value">${esc(size(verified))}</div></div>
        <div class="stat"><div class="label">Kecepatan tulis</div><div class="value">${esc(fmtSpeed(write))}</div></div>
        <div class="stat"><div class="label">Kecepatan baca</div><div class="value">${esc(fmtSpeed(read))}</div></div>
      </div>`;
    toast(cancelled ? "Uji kapasitas dibatalkan" : "Uji kapasitas selesai", verdict, tone === "bad" ? "err" : "ok");
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
    if (r.stuck) {
      // the scan stopped because the device stopped answering: say so, not "healthy" / "N bad ranges"
      $("flash-result").innerHTML = `<p><span class="tag warn">TIDAK MERESPONS</span> <span class="muted">${esc(r.message || STUCK_TIP)}</span></p>`;
      toast("Uji permukaan berhenti", r.message || STUCK_TIP, "err");
      loadDisks();  // pick up the server's stuck flag so the buttons for this device lock
      return;
    }
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
      state.disks = data.disks || [];
      state.stuck = new Set(state.disks.filter((d) => d.stuck === true).map((d) => normPath(d.path)));
      ["fs-disk", "carve-disk", "image-disk", "flash-disk"].forEach((id) => {
        const sel = $(id), prev = sel.value;
        sel.innerHTML = `<option value="">— pilih disk —</option>` + state.disks.map((d) => d.stuck === true
          ? `<option value="${esc(d.path)}" class="opt-stuck" title="${esc(STUCK_TIP)}">⚠ ${esc(d.desc)} — tidak merespons</option>`
          : `<option value="${esc(d.path)}">${esc(d.desc)}</option>`).join("");
        if (state.disks.some((d) => d.path === prev)) sel.value = prev;
        sel.onchange = () => { const target = { "fs-disk": "fs-src", "carve-disk": "carve-src", "image-disk": "image-src", "flash-disk": "flash-src" }[id]; $(target).value = sel.value; refreshStuck(); };
      });
    } catch (err) { toast("Daftar disk tidak tersedia", err.message, "err"); }
    refreshStuck();
  }
  function normPath(p) { return String(p || "").trim().toLowerCase(); }

  // Disable operation buttons when the chosen source is a device that stopped responding.
  function refreshStuck() {
    Object.entries(STUCK_TARGETS).forEach(([key, t]) => {
      const stuck = state.stuck.has(normPath($(t.src).value));
      const note = document.querySelector(`[data-stuck-note="${key}"]`);
      if (note) {
        note.hidden = !stuck;
        if (stuck && !note.childElementCount) {
          note.innerHTML = `<span class="tag warn" title="${esc(STUCK_TIP)}">tidak merespons</span>
            <span>Cabut lalu colok kembali drive ini.</span>
            <button type="button" class="btn btn-sm btn-ghost" data-reload-disks>Muat ulang daftar disk</button>`;
          note.querySelector("[data-reload-disks]").onclick = () => loadDisks();
        }
      }
      t.buttons.forEach((bid) => {
        const b = $(bid);
        if (!b) return;
        if (stuck) {
          if (!b.dataset.stuck) { b.dataset.stuck = "1"; b.dataset.prevTitle = b.title || ""; }
          b.disabled = true;
          b.title = STUCK_TIP;
        } else if (b.dataset.stuck) {
          b.disabled = false;
          b.title = b.dataset.prevTitle || "";
          delete b.dataset.stuck; delete b.dataset.prevTitle;
        }
      });
    });
    syncRunning();  // a device that is no longer stuck must still stay locked while a job runs
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
      syncRows("trash-body", state.trashSel);
      syncTrashSelection();
    };
    // row checkboxes: one delegated listener, update the row and counters in place
    $("trash-body").addEventListener("change", (e) => {
      const cb = e.target.closest("input[type=checkbox][data-id]");
      if (!cb) return;
      const id = Number(cb.dataset.id);
      cb.checked ? state.trashSel.add(id) : state.trashSel.delete(id);
      cb.closest("tr").classList.toggle("selected", cb.checked);
      syncTrashSelection();
    });
    $("fs-body").addEventListener("change", (e) => {
      const cb = e.target.closest("input[type=checkbox][data-id]");
      if (!cb) return;
      const id = Number(cb.dataset.id);
      cb.checked ? state.fsSel.add(id) : state.fsSel.delete(id);
      cb.closest("tr").classList.toggle("selected", cb.checked);
      syncFsSelection();
    });
    $("trash-restore").onclick = () => openBrowser({
      mode: "dir", title: "Pilih folder tujuan",
      onPick: (dest) => startJob("/api/trash/restore", { ids: [...state.trashSel], dest }, "trash_restore"),
    });

    // deleted
    $("fs-scan").onclick = () => {
      const source = $("fs-src").value.trim();
      if (!source) return toast("Sumber kosong", "Pilih disk atau file image dulu.", "err");
      startJob("/api/fs/scan", { source, ...dateRange("fs-from", "fs-to") }, "fs_scan").then((ok) => {
        // drop the old selection only once the server accepted the scan (a 409 keeps it)
        if (ok) { state.fsSel.clear(); syncRows("fs-body", state.fsSel); syncFsSelection(); }
      });
    };
    $("fs-from").onchange = loadFs;
    $("fs-to").onchange = loadFs;
    $("fs-select-all").onclick = () => {
      // selects (or clears) the rows the current filter shows
      const ids = (state.fsShown || state.fs).map((e) => e.id);
      const allOn = ids.length > 0 && ids.every((id) => state.fsSel.has(id));
      ids.forEach((id) => allOn ? state.fsSel.delete(id) : state.fsSel.add(id));
      syncRows("fs-body", state.fsSel);
      syncFsSelection();
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
    $("ext-all").onclick = () => { state.exts = new Set(EXTS); syncChips(); };
    $("ext-none").onclick = () => { state.exts = new Set(); syncChips(); };
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

    // capacity test
    $("cap-size").onchange = () => {
      const custom = $("cap-size").value === "custom";
      $("cap-custom-wrap").hidden = !custom;
      if (custom) $("cap-custom").focus();
    };
    $("cap-start").onclick = () => {
      if (state.job) return toast("Pekerjaan lain berjalan", "Tunggu sampai selesai atau batalkan dulu.", "err");
      const path = $("cap-path").value.trim();
      if (!path) return toast("Pilih drive atau folder", "Isi lokasi drive atau folder yang akan diuji.", "err");
      const sizeMb = capacitySizeMb();
      if (!sizeMb) return toast("Ukuran tidak valid", "Isi ukuran uji dalam MB (bilangan bulat minimal 1).", "err");
      $("cap-result").className = "muted small";
      $("cap-result").textContent = `Menguji ${sizeMb.toLocaleString("id-ID")} MB di ${path}…`;
      startJob("/api/capacity", { path, size_mb: sizeMb }, "capacity").then((ok) => {
        if (!ok) $("cap-result").textContent = "Uji tidak dimulai.";
      });
    };

    // stuck devices: re-check when a source path is typed or picked
    Object.values(STUCK_TARGETS).forEach((t) => {
      $(t.src).addEventListener("input", refreshStuck);
      $(t.src).addEventListener("change", refreshStuck);
    });

    // result filters
    state.filters.fs = makeFilter("fs", renderFs);
    state.filters.carve = makeFilter("carve", renderCarve);

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
    const gen = (state.debugGen || 0) + 1;
    state.debugGen = gen;
    const tick = async () => {
      if (gen !== state.debugGen) return;
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
      if (gen === state.debugGen) state.debugTimer = setTimeout(tick, 800);
    };
    tick();
  }
  function stopDebug() {
    state.debugGen = (state.debugGen || 0) + 1;   // invalidates any tick still awaiting fetches
    clearTimeout(state.debugTimer);
    state.debugTimer = null;
  }

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
    // level filtering is a data attribute on the container + CSS, so new lines need no per-node work
    if ($("dbg-follow").checked) box.scrollTop = box.scrollHeight;
    $("dbg-count").textContent = `${box.children.length} baris`;
  }
  // Minimum level lives on the container (data-min); app.css hides lower-level lines.
  function renderLogFilter() {
    const box = $("dbg-log");
    const level = $("dbg-level").value;
    if (level === "ALL") delete box.dataset.min; else box.dataset.min = level;
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
      if (job.state === "running") { state.job = { name: job.name }; syncRunning(); poll(); }
    } catch (_) { /* ignore */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
