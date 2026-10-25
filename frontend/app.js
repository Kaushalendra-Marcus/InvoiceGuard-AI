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
  statLines: $("stat-lines"),
  statMatched: $("stat-matched"),
  statFindings: $("stat-findings"),
  statExcess: $("stat-excess"),
  totalsChart: $("totals-chart"),
  totalsNote: $("totals-note"),
  matchChart: $("match-chart"),
  pdfBtn: $("btn-pdf"),
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
      const detail = payload.detail || {};
      if (detail.usage) renderUsage(detail.usage);
      setError(
        detail.message ||
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
  window.__IG_REPORT__ = report;

  const style = STATUS_STYLE[report.status] || "review";
  els.statusBanner.className = `status-banner ${style}`;
  els.statusValue.textContent = report.status;
  els.statusSummary.textContent = report.summary;
  els.statusAction.textContent = "→ " + report.recommended_action;

  renderSummaryStats(report);
  renderTotalsChart(report);
  renderMatchDonut(report);

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
      ${scoreBar(m)}
      <div class="match-chips">${chips.join("")}</div>
      ${lineNumbers(m)}
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

/* ----------------------------------------------- summary stats + visuals */
function renderSummaryStats(report) {
  if (!els.statLines) return;
  const matches = report.matches || [];
  const discrepancies = report.discrepancies || [];
  const matchedCount = matches.filter((m) => m.matched).length;
  let excess = 0;
  discrepancies.forEach((d) => {
    if (d.potential_excess_amount != null) excess += Number(d.potential_excess_amount);
  });
  els.statLines.textContent = String(matches.length);
  els.statMatched.textContent = `${matchedCount} / ${matches.length}`;
  els.statFindings.textContent = String(discrepancies.length);
  els.statExcess.textContent = excess > 0 ? fmtAmount(excess.toFixed(2)) : "None";
  els.statExcess.classList.toggle("alert", excess > 0);
  els.statExcess.classList.toggle("amount", true);
}

function poLineTotal(poItem) {
  if (!poItem) return null;
  if (poItem.line_total != null) return Number(poItem.line_total);
  if (poItem.quantity != null && poItem.unit_price != null) {
    return Number(poItem.quantity) * Number(poItem.unit_price);
  }
  return null;
}

function lineNumbers(m) {
  const inv = m.invoice_item || {};
  const poItem = m.po_item;
  const cells = [];
  if (inv.quantity != null || poItem) {
    const invQty = inv.quantity != null ? Number(inv.quantity) : null;
    const poQty = poItem ? Number(poItem.quantity) : null;
    cells.push(
      `<span><span class="num-label">qty</span> ${invQty != null ? fadeNumber(invQty, poQty) : "-"}` +
        `${poItem ? `<span class="num-label"> / po ${poQty}</span>` : ""}</span>`
    );
  }
  if (inv.unit_price != null || poItem) {
    const invPrice = inv.unit_price != null ? Number(inv.unit_price) : null;
    const poPrice = poItem ? Number(poItem.unit_price) : null;
    cells.push(
      `<span><span class="num-label">unit</span> ${invPrice != null ? fadeMoney(invPrice, poPrice) : "-"}` +
        `${poItem ? `<span class="num-label"> / po ${fmtMoney(poPrice)}</span>` : ""}</span>`
    );
  }
  const invTotal = inv.line_total != null ? Number(inv.line_total) : null;
  const poTotal = poLineTotal(poItem);
  if (invTotal != null || poTotal != null) {
    let html = `<span><span class="num-label">line</span> ${invTotal != null ? fmtMoney(invTotal) : "-"}`;
    if (poTotal != null) {
      html += `<span class="num-label"> / po ${fmtMoney(poTotal)}</span>`;
      if (invTotal != null && Math.abs(invTotal - poTotal) > 0.005) {
        html += ` <span class="num-diff">(+${fmtMoney(invTotal - poTotal)})</span>`;
      }
    }
    html += "</span>";
    cells.push(html);
  }
  return cells.length ? `<div class="line-nums">${cells.join("")}</div>` : "";
}

function fadeNumber(invoiceValue, poValue) {
  if (poValue != null && Math.abs(invoiceValue - poValue) > 0.0001) {
    return `<span class="num-diff">${invoiceValue}</span>`;
  }
  return String(invoiceValue);
}

function fadeMoney(invoiceValue, poValue) {
  if (poValue != null && Math.abs(invoiceValue - poValue) > 0.005) {
    return `<span class="num-diff">${fmtMoney(invoiceValue)}</span>`;
  }
  return fmtMoney(invoiceValue);
}

function fmtMoney(n) {
  const v = Number(n);
  if (!isFinite(v)) return "-";
  return `₹${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/* thin similarity bar: best of fuzzy (0-100) and semantic (0-1) */
function scoreBar(m) {
  let pct = null;
  if (m.fuzzy_score != null) pct = m.fuzzy_score;
  if (m.semantic_score != null) pct = Math.max(pct ?? 0, m.semantic_score * 100);
  const bucket = !m.matched ? "off" : pct >= 85 ? "high" : pct >= 60 ? "mid" : "low";
  const label = pct == null ? "n/a" : `${pct.toFixed(0)}%`;
  return `
    <div class="score-line">
      <span class="score-track"><span class="score-fill ${bucket}" style="width:${pct == null ? 0 : Math.max(2, Math.min(100, pct))}%"></span></span>
      <span class="score-val">${label}</span>
    </div>`;
}

/* horizontal bars: invoice total vs PO line total */
function renderTotalsChart(report) {
  if (!els.totalsChart) return;
  const invoiceTotal = report.invoice_total != null ? Number(report.invoice_total) : null;
  let poTotal = null;
  (report.po_items || []).forEach((p) => {
    const t = poLineTotal(p);
    if (t != null) poTotal = (poTotal || 0) + t;
  });
  const currency = report.effective_currency || report.invoice_currency || report.po_currency || "INR";
  const scale = Math.max(invoiceTotal ?? 0, poTotal ?? 0) || 1;

  let html = "";
  html += amtRow("Invoice", invoiceTotal, scale, (v) => moneyIn(v, currency));
  html += amtRow("PO budget", poTotal, scale, (v) => moneyIn(v, currency));
  els.totalsChart.innerHTML = html;

  const note = [];
  if (invoiceTotal == null) note.push("Invoice total was not extracted from the document.");
  if (poTotal == null) note.push("No PO line totals available.");
  if (invoiceTotal != null && poTotal != null) {
    const diff = invoiceTotal - poTotal;
    if (Math.abs(diff) < 0.005) {
      note.push("Invoice total matches the PO line total.");
    } else {
      const pct = (diff / poTotal) * 100;
      const direction = diff > 0 ? "above" : "below";
      note.push(`Invoice is ${fmtMoney(Math.abs(diff))} (${Math.abs(pct).toFixed(1)}%) ${direction} the PO total.`);
    }
  }
  if (els.totalsNote) els.totalsNote.textContent = note.join(" ");
}

function amtRow(label, value, scale, fmt) {
  const pct = value == null ? 0 : Math.max(2, Math.min(100, (value / scale) * 100));
  const fill = label.toLowerCase().includes("po") ? "po" : "invoice";
  return `
    <div class="amt-row">
      <span class="amt-label">${label}</span>
      <span class="amt-track"><span class="amt-fill ${fill}" style="width:${pct}%"></span></span>
      <span class="amt-value">${value == null ? "-" : fmt(value)}</span>
    </div>`;
}

function moneyIn(value, currency) {
  const s = Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency === "INR" ? `₹${s}` : `${currency} ${s}`;
}

/* donut: matched share of invoice lines + confidence mix legend */
function renderMatchDonut(report) {
  if (!els.matchChart) return;
  const matches = report.matches || [];
  const total = matches.length;
  const matched = matches.filter((m) => m.matched).length;
  const unmatched = total - matched;
  const conf = { HIGH: 0, MEDIUM: 0, LOW: 0 };
  matches.forEach((m) => {
    if (m.matched && m.confidence) conf[m.confidence] = (conf[m.confidence] || 0) + 1;
  });

  const matchedPct = total ? (matched / total) * 100 : 0;
  const R = 54;
  const C = 2 * Math.PI * R;
  const okLen = total ? (matched / total) * C : 0;
  const svg = `
    <div class="donut-wrap">
      <svg viewBox="0 0 140 140" role="img" aria-label="Matched share of invoice lines">
        <circle cx="70" cy="70" r="${R}" fill="none" stroke="#eef2f7" stroke-width="15"/>
        <circle cx="70" cy="70" r="${R}" fill="none" stroke="#cbd5e1" stroke-width="15"
                stroke-dasharray="${(C - okLen).toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-okLen / 2).toFixed(2)}" transform="rotate(-90 70 70)"/>
        <circle cx="70" cy="70" r="${R}" fill="none" stroke="#16a34a" stroke-width="15"
                stroke-dasharray="${okLen.toFixed(2)} ${(C - okLen).toFixed(2)}" stroke-dashoffset="${(okLen / 2).toFixed(2)}" transform="rotate(-90 70 70)" stroke-linecap="round"/>
        <text x="70" y="66" text-anchor="middle" font-size="24" font-weight="800" fill="#0f172a">${Math.round(matchedPct)}%</text>
        <text x="70" y="84" text-anchor="middle" font-size="10" font-weight="600" fill="#64748b">items matched</text>
      </svg>
      <div class="donut-legend">
        <span><span class="dot" style="background:#16a34a"></span>Matched: <strong>${matched}</strong></span>
        <span><span class="dot" style="background:#cbd5e1"></span>Unmatched: <strong>${unmatched}</strong></span>
        <span style="color:var(--muted)"><span class="dot" style="background:#eef2f7"></span>High ${conf.HIGH} · Medium ${conf.MEDIUM} · Low ${conf.LOW}</span>
      </div>
    </div>`;
  els.matchChart.innerHTML = svg;
}

function fmtAmount(value) {
  const n = Number(value);
  return `₹${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function prettify(type) {
  return (type || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

refreshUsage();

/* ------------------------------------------------------- PDF report button */
if (els.pdfBtn) {
  els.pdfBtn.addEventListener("click", () => {
    const report = window.__IG_REPORT__;
    if (!report) return;
    if (!window.InvoiceGuardPDF || typeof window.InvoiceGuardPDF.buildReportPdf !== "function") {
      setError("The report generator failed to load; please refresh the page.");
      return;
    }
    try {
      const bytes = window.InvoiceGuardPDF.buildReportPdf(report, sessionId);
      const blob = new Blob([bytes], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download =
        "InvoiceGuard-AI_report_" +
        String(report.status || "report").toLowerCase() +
        "_" +
        new Date().toISOString().slice(0, 10) +
        ".pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (err) {
      setError("Could not generate the PDF report: " + err.message);
    }
  });
}

/* hidden demo path: /app.html?demo=1 loads the bundled sample and runs one
 * analysis so the result layout is reachable without manual steps. */
(function demoAutoload() {
  const params = new URLSearchParams(location.search);
  if (!params.has("demo") || !els.sampleBtn || !els.analyzeBtn) return;
  window.addEventListener("load", () => {
    setTimeout(() => {
      els.sampleBtn.click();
      setTimeout(() => {
        if (!els.analyzeBtn.disabled) els.analyzeBtn.click();
      }, 400);
    }, 200);
  });
})();
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
