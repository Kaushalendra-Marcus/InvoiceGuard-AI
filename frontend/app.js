/* InvoiceGuard AI: single-page analyzer.
 * Anonymous session: random ID generated once, stored in localStorage,
 * sent with every request; the *server* enforces the daily limit.
 */
"use strict";

const SAMPLE_PO_JSON = {
  po_number: "PO-2024-0042",
  currency: "INR",
  items: [
    {
      description: "USB-C Cable, 1 Meter",
      sku: "CBL-USB-C-1M",
      quantity: 100,
      quantity_unit: "pcs",
      unit_price: 150.0,
    },
    {
      description: "Laptop Stand",
      sku: "STD-LAP-001",
      quantity: 50,
      quantity_unit: "pcs",
      unit_price: 800.0,
    },
    {
      description: "HDMI Adapter",
      sku: "HDMI-ADP-001",
      quantity: 10,
      quantity_unit: "pcs",
      unit_price: 450.0,
    },
  ],
};

const $ = (id) => document.getElementById(id);
const els = {
  invoiceFile: $("invoice-file"),
  dropzone: $("invoice-dropzone"),
  fileName: $("invoice-file-name"),
  poFile: $("po-file"),
  poFileInput: $("po-file-shell"),
  poFileBtn: $("po-file-btn"),
  poFileName: $("po-file-name"),
  poJson: $("po-json"),
  poCsv: $("po-csv"),
  analyzeBtn: $("btn-analyze"),
  sampleBtn: $("btn-sample"),
  loading: $("loading"),
  error: $("error-banner"),
  hint: $("form-hint"),
  sessionPreview: $("session-preview"),
  usageText: $("usage-text"),
  usagePill: $("usage-pill"),
  resultSection: $("result-section"),
  statusBanner: $("status-banner"),
  statusValue: $("status-value"),
  statusSummary: $("status-summary"),
  statusAction: $("status-action"),
  matchesList: $("matches-list"),
  discrepancyList: $("discrepancy-list"),
  rawJson: $("raw-json"),
};

/* -------------------------------------------------- anonymous session id */
function getSessionId() {
  let sid = localStorage.getItem("invoiceguard_session_id");
  if (!sid) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    sid =
      "anon-" +
      Array.from(bytes)
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
    localStorage.setItem("invoiceguard_session_id", sid);
  }
  return sid;
}
const sessionId = getSessionId();
if (els.sessionPreview) els.sessionPreview.textContent = sessionId.slice(0, 13) + "…";

/* ------------------------------------------------------------ usage pill */
function renderUsage(usage) {
  if (!usage) return;
  els.usageText.textContent = `${usage.remaining_validations} / ${usage.daily_limit} analyses left`;
  const exhausted = usage.remaining_validations === 0;
  els.usagePill.classList.toggle("exhausted", exhausted);
  if (els.analyzeBtn) {
    els.analyzeBtn.disabled = exhausted;
    els.analyzeBtn.title = exhausted
      ? "Daily limit reached for this anonymous session, resets at midnight IST."
      : "";
  }
}

async function refreshUsage() {
  try {
    const res = await fetch(`/api/v1/usage?session_id=${encodeURIComponent(sessionId)}`);
    if (res.ok) renderUsage(await res.json());
  } catch (_) {
    /* the analysis flow will surface usage anyway */
  }
}

/* ------------------------------------------------------------- PO input */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const mode = tab.dataset.tab; // "json" | "csv" | "file"
    els.poJson.classList.toggle("hidden", mode !== "json");
    els.poCsv.classList.toggle("hidden", mode !== "csv");
    if (mode === "file") {
      els.poFileInput.classList.remove("hidden");
      els.poFileName.classList.toggle("hidden", !els.poFile.files.length);
    } else {
      els.poFileInput.classList.add("hidden");
      els.poFileName.classList.add("hidden");
    }
  });
});

/* PO file shell: explicit button opens the (hidden) input; the chosen file
 * name is mirrored next to it so the user sees what was selected. */
function syncPoFileState() {
  const has = els.poFile.files.length > 0;
  els.poFileInput.classList.toggle("has-file", has);
  els.poFileName.textContent = has ? `\uD83D\uDCC4 ${els.poFile.files[0].name}` : "";
  els.poFileName.classList.toggle("hidden", !has);
}
if (els.poFile) els.poFile.addEventListener("change", syncPoFileState);
const poFileBtn = $("po-file-btn");
if (poFileBtn && els.poFile) poFileBtn.addEventListener("click", () => els.poFile.click());

function currentPoSource() {
  const jsonHidden = els.poJson.classList.contains("hidden");
  const csvHidden = els.poCsv.classList.contains("hidden");
  const activeTab = document.querySelector(".tab.active");
  const fileTab = activeTab && activeTab.dataset.tab === "file";
  if (fileTab && els.poFile.files && els.poFile.files.length > 0) {
    return { file: els.poFile.files[0] };
  }
  if (!jsonHidden && els.poJson.value.trim()) return { text: els.poJson.value };
  if (!csvHidden && els.poCsv.value.trim()) return { text: els.poCsv.value, csv: true };
  return null;
}

/* ------------------------------------------------------ invoice handling */
function styleDropzone() {
  const has = els.invoiceFile.files.length > 0;
  els.dropzone.classList.toggle("has-file", has);
  els.fileName.textContent = has
    ? els.invoiceFile.files[0].name
    : "Choose a PDF or image, or drop it here";
}
if (els.invoiceFile) els.invoiceFile.addEventListener("change", styleDropzone);
["dragover", "dragenter"].forEach((ev) =>
  els.dropzone && els.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  els.dropzone && els.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("drag");
  })
);
if (els.dropzone) {
  els.dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) {
      const dt = new DataTransfer();
      dt.items.add(e.dataTransfer.files[0]);
      els.invoiceFile.files = dt.files;
      styleDropzone();
    }
  });
}

/* ------------------------------------------------------------ sample load */
if (els.sampleBtn) {
els.sampleBtn.addEventListener("click", async () => {
  // Activate the JSON tab the same way a click would, so state stays honest.
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  const jsonTab = document.querySelector('.tab[data-tab="json"]');
  if (jsonTab) jsonTab.classList.add("active");
  els.poJson.classList.remove("hidden");
  els.poCsv.classList.add("hidden");
  els.poFileInput.classList.add("hidden");
  els.poFileName.classList.add("hidden");

  els.poJson.value = JSON.stringify(SAMPLE_PO_JSON, null, 2);
  els.poFile.value = "";
  syncPoFileState();
  try {
    const res = await fetch("/assets/invoice_sample.pdf");
    if (res.ok) {
      const blob = await res.blob();
      const file = new File([blob], "invoice_sample.pdf", { type: "application/pdf" });
      const dt = new DataTransfer();
      dt.items.add(file);
      els.invoiceFile.files = dt.files;
      styleDropzone();
      return;
    }
  } catch (_) { /* fall through to message */ }
  setError(
    'The bundled sample invoice (served at <code>/assets/invoice_sample.pdf</code>) is not ' +
      "available; start the backend or open an issue if the file is missing from your copy."
  );
});
}

/* ----------------------------------------------------------------- submit */
function setError(message, recoverable = false) {
  els.error.innerHTML = message;
  els.error.classList.remove("hidden");
  if (recoverable) setTimeout(() => els.error.classList.add("hidden"), 8000);
}
function clearError() {
  els.error.classList.add("hidden");
  els.error.innerHTML = "";
}
function setLoading(on) {
  els.loading.classList.toggle("hidden", !on);
  els.analyzeBtn.disabled = on;
}

els.analyzeBtn &&
els.analyzeBtn.addEventListener("click", async () => {
  clearError();
  els.resultSection.classList.add("hidden");

  if (!els.invoiceFile.files.length) {
    setError("Please attach the invoice PDF or image.");
    return;
  }
  const po = currentPoSource();
  if (!po) {
    setError("Please insert the Purchase Order as JSON or CSV (or load the sample input).");
    return;
  }

  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("invoice", els.invoiceFile.files[0]);
  if (po.file) form.append("purchase_order_file", po.file);
  else if (po.csv) form.append("po_csv", po.text);
  else form.append("po_json", po.text);

  setLoading(true);
  try {
    const res = await fetch("/api/v1/analyze", { method: "POST", body: form });
    const payload = await res.json().catch(() => ({}));
    if (res.status === 429) {
      const usage = payload.detail && payload.detail.usage;
      renderUsage(usage);
      setError(
        "Daily limit of 10 successful analyses reached for this anonymous session. " +
          "The limit resets at midnight IST (Asia/Kolkata)."
      );
      return;
    }
    if (!res.ok) {
      const detail = payload.detail;
      const message = detail && detail.message ? detail.message : `Request failed (HTTP ${res.status}).`;
      setError(message);
      renderUsage(usageFromResponse(res, payload));
      return;
    }
    renderResult(payload);
    renderUsage(payload.usage);
    refreshUsage();
  } catch (err) {
    setError(`Could not reach the backend: ${err.message}. Is the server running?`);
  } finally {
    setLoading(false);
  }
});

function usageFromResponse(res, payload) {
  return payload.usage || null;
}

/* --------------------------------------------------------------- render */
const STATUS_STYLE = {
  MATCHED: "matched",
  DISCREPANCY_FOUND: "discrepancy",
  NEEDS_REVIEW: "review",
};

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function renderResult(report) {
  els.resultSection.classList.remove("hidden");
  els.resultSection.scrollIntoView({ behavior: "smooth", block: "start" });

  const style = STATUS_STYLE[report.status] || "review";
  els.statusBanner.className = `status-banner ${style}`;
  els.statusValue.textContent = report.status;
  els.statusSummary.textContent = report.summary;
  els.statusAction.textContent = "→ " + report.recommended_action;

  /* matches */
  els.matchesList.innerHTML = "";
  (report.matches || []).forEach((m) => {
    const safe = esc;
    const inv = m.invoice_item || {};
    const poItem = m.po_item;
    const div = document.createElement("div");
    div.className = "match-item";
    const chips = [];
    if (m.matched) {
      chips.push(`<span class="chip ok">${safe(m.method)}</span>`);
      chips.push(`<span class="chip">${safe(m.confidence)} confidence</span>`);
    } else {
      chips.push(`<span class="chip bad">UNMATCHED</span>`);
    }
    if (m.sku_match) chips.push(`<span class="chip ok">SKU ✓</span>`);
    if (m.ambiguous) chips.push(`<span class="chip warn">AMBIGUOUS</span>`);
    if (m.unit_compatible === false) chips.push(`<span class="chip bad">UNIT CONFLICT</span>`);
    if (m.semantic_score != null) {
      chips.push(`<span class="chip">semantic ${(m.semantic_score * 100).toFixed(0)}%</span>`);
    }
    if (m.fuzzy_score != null) chips.push(`<span class="chip">fuzzy ${m.fuzzy_score.toFixed(0)}%</span>`);

    const notes = [...(m.contradictions || []), ...(m.notes || [])].map((n) => `• ${safe(n)}`);
    div.innerHTML = `
      <div class="pair">${safe(inv.description)} ${m.matched ? '<span class="arrow">→</span> ' + safe(poItem ? poItem.description : "") : '<span class="arrow">→</span> <em>(no PO line)</em>'}</div>
      <div class="match-chips">${chips.join("")}</div>
      ${(notes.length ? `<div class="match-notes">${notes.join("<br/>")}</div>` : "")}
    `;
    els.matchesList.appendChild(div);
  });

  /* discrepancies (sorted CRITICAL first) */
  els.discrepancyList.innerHTML = "";
  const SEVERITY_ORDER = { CRITICAL: 0, WARNING: 1, INFO: 2 };
  const discrepancies = (report.discrepancies || []).slice().sort(
    (a, b) =>
      (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9)
  );
  discrepancies.forEach((d) => {
    const div = document.createElement("div");
    div.className = `disc-item ${esc((d.severity || "").toLowerCase())}`;
    const values = [];
    if (d.line_ref) values.push(`<strong>${esc(d.line_ref)}</strong>`);
    if (d.invoice_value != null) values.push(`invoice: ${esc(d.invoice_value)}`);
    if (d.po_value != null) values.push(`PO: ${esc(d.po_value)}`);
    if (d.difference != null) values.push(`difference: ${esc(d.difference)}`);
    if (d.potential_excess_amount != null) {
      div.classList.add("critical");
      values.push(`<strong>potential excess: ${fmtAmount(d.potential_excess_amount)}</strong>`);
    }
    div.innerHTML = `
      <div class="disc-title"><span class="sev-token ${esc((d.severity || "").toLowerCase())}">${esc(d.severity)}</span> ${esc(prettify(d.type))}</div>
      <div class="disc-values">${values.join(" · ") || ""}</div>
      <div class="disc-expl">${esc(d.message)} ${esc(d.explanation)}</div>
    `;
    els.discrepancyList.appendChild(div);
  });
  if (!discrepancies.length) {
    els.discrepancyList.innerHTML = "<p class='muted'>No discrepancies detected.</p>";
  }

  els.rawJson.textContent = JSON.stringify(report, null, 2);
}

function fmtAmount(value) {
  const n = Number(value);
  return `₹${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function prettify(type) {
  return (type || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

refreshUsage();

/* ------------------------------------------------- pricing billing toggle */
const billingButtons = document.querySelectorAll(".billing-toggle [data-billing]");
if (billingButtons.length) {
  const proPrice = $("pro-price");
  const proNote = $("pro-note");
  billingButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      billingButtons.forEach((b) => b.classList.toggle("active", b === btn));
      const annual = btn.dataset.billing === "annual";
      if (proPrice) proPrice.textContent = annual ? "23" : "29";
      if (proNote) proNote.textContent = annual ? "billed annually, save 20%" : "billed monthly";
    });
  });
}

/* ------------------------------------------------------------ contact form */
const contactForm = $("contact-form");
const contactNote = $("contact-note");
if (contactForm && contactNote) {
  contactForm.addEventListener("submit", (event) => {
    event.preventDefault();
    contactNote.innerHTML =
      "This prototype build does not send e-mail. Please open a " +
      '<a href="https://github.com/Kaushalendra-Marcus/InvoiceGuard-AI/issues" target="_blank" rel="noopener">GitHub issue</a> ' +
      "and the maintainer will get back to you.";
    contactNote.classList.remove("hidden");
    contactNote.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  contactForm.addEventListener("input", () => contactNote.classList.add("hidden"));
}

/* -------------------------------------------------------
 * production polish: reveal-on-scroll, header elevation, stat count-up */
(function polish() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function activate(el) { el.classList.add("in"); }

  function observe(el) {
    if (!("IntersectionObserver" in window)) { activate(el); return; }
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          activate(entry.target);
          io.unobserve(entry.target);
        }
      }
    }, { threshold: 0.1, rootMargin: "0px 0px -6% 0px" });
    io.observe(el);
  }

  const revealTargets = document.querySelectorAll("[data-reveal], [data-reveal-stagger]");
  revealTargets.forEach((el) => {
    if (reduced) { activate(el); } else { observe(el); }
  });

  /* sticky header gets a shadow once the page scrolls */
  const header = document.querySelector(".site-header");
  const onScroll = () => {
    if (header) header.classList.toggle("scrolled", window.scrollY > 8);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* hero stats count up the first time they are seen */
  function countUp(el) {
    const target = Number(el.dataset.count || "0");
    const start = performance.now();
    const dur = 900;
    function frame(t) {
      const k = Math.min(1, (t - start) / dur);
      const eased = 1 - Math.pow(1 - k, 3);
      el.textContent = String(Math.round(target * eased));
      if (k < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  const statValues = document.querySelectorAll("[data-count]");
  if (reduced || !("IntersectionObserver" in window)) {
    statValues.forEach((el) => { el.textContent = String(Number(el.dataset.count || "0")); });
  } else {
    const co = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          countUp(entry.target);
          co.unobserve(entry.target);
        }
      }
    }, { threshold: 0.5 });
    statValues.forEach((el) => co.observe(el));
  }
})();
