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

/* demo-only dense PO (never shipped in the product sample): the bundled
 * sample invoice's three items billed against a five-item PO, so the budget
 * view shows over-budget, fully billed and not-billed rows. */
const DENSE_PO_JSON = {
  po_number: "PO-2024-0117",
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
    {
      description: "Webcam 1080p with Mic",
      sku: "CAM-WEB-01",
      quantity: 6,
      quantity_unit: "pcs",
      unit_price: 2200.0,
    },
    {
      description: "Monitor Riser Stand",
      sku: "RIS-STD-02",
      quantity: 8,
      quantity_unit: "pcs",
      unit_price: 950.0,
    },
  ],
};

function demoSamplePo() {
  const tag = new URLSearchParams(location.search).get("demo");
  return tag === "2" ? DENSE_PO_JSON : SAMPLE_PO_JSON;
}

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
  statusSub: $("status-sub"),
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

  els.poJson.value = JSON.stringify(demoSamplePo(), null, 2);
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
  if (els.statusSub) {
    els.statusSub.textContent =
      report.status === "MATCHED"
        ? "Deterministic validation passed"
        : report.status === "DISCREPANCY_FOUND"
          ? "Settle or credit the flagged amounts"
          : "Human review required";
  }

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
    const cells = [];
    if (d.line_ref != null && String(d.invoice_value || "").indexOf(String(d.line_ref)) !== 0) {
      cells.push(["Item", esc(trunc(d.line_ref, 18))]);
    }
    if (d.invoice_value != null) cells.push(["Invoice value", esc(d.invoice_value)]);
    if (d.po_value != null) cells.push(["PO value", esc(d.po_value)]);
    if (d.difference != null) cells.push(["Difference", esc(d.difference)]);
    if (d.potential_excess_amount != null) {
      div.classList.add("has-excess");
      cells.push(["Potential excess", `<span class="alert">${fmtAmount(d.potential_excess_amount)}</span>`]);
    }
    div.innerHTML = `
      <div class="disc-title"><span class="sev-token ${esc((d.severity || "").toLowerCase())}">${esc(d.severity)}</span> ${esc(prettify(d.type))}</div>
      ${cells.length ? `<div class="disc-grid">${cells.map(([label, val]) => `<span class="g-cell"><span class="g-label">${label}</span><span class="g-val">${val}</span></span>`).join("")}</div>` : ""}
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

/* budget utilization: one row per PO item (billed vs its budget), then a
 * total strip covering ALL PO items, billed or not. */
function renderTotalsChart(report) {
  if (!els.totalsChart) return;
  const currency = report.effective_currency || report.invoice_currency || report.po_currency || "INR";
  const matches = report.matches || [];
  const poItems = report.po_items || [];

  const billedByPo = {};
  matches.forEach((m) => {
    if (m.matched && m.po_item_index != null && !billedByPo[m.po_item_index]) {
      billedByPo[m.po_item_index] = m;
    }
  });
  const unbilledByPo = {};
  (report.po_items_without_invoice || []).forEach((p) => {
    unbilledByPo[p.po_item_index] = true;
  });

  /* one row per PO item, in PO order */
  const rows = poItems.map((p) => {
    const budget = poLineTotal(p);
    const m = billedByPo[p.po_item_index];
    const billed = m && m.invoice_item && m.invoice_item.line_total != null ? Number(m.invoice_item.line_total) : null;
    return { name: p.description, budget, billed, state: billed == null ? "notbilled" : "billed" };
  });
  /* invoice lines with no PO line at all */
  matches.forEach((m) => {
    if (!m.matched && m.invoice_item) {
      const value = m.invoice_item.line_total != null ? Number(m.invoice_item.line_total) : null;
      rows.push({ name: m.invoice_item.description, budget: null, billed: value, state: "nobudget" });
    }
  });

  const values = [];
  rows.forEach((r) => {
    if (r.budget != null) values.push(r.budget);
    if (r.billed != null) values.push(r.billed);
  });
  const scale = values.length ? Math.max(...values) : 1;
  const shownName = (s) => esc(trunc(s, 34));

  let html = "";
  rows.forEach((r) => {
    const poPct = r.budget != null ? Math.max(4, Math.min(100, (r.budget / scale) * 100)) : 0;
    const invPctOfBudget = r.budget && r.billed != null ? (r.billed / r.budget) * 100 : 0;
    const invPct = r.billed != null ? Math.max(4, Math.min(100, (r.billed / scale) * 100)) : 0;
    const over = r.budget && r.billed != null && r.billed > r.budget + 0.005;
    const overPct = over ? ((r.billed - r.budget) / scale) * 100 : 0;

    let nums;
    if (r.state === "notbilled") {
      nums = `<span class="b-num"><span class="b-po-label">${moneyIn(r.budget, currency)}</span></span><span class="row-tag">not billed</span>`;
    } else if (r.state === "nobudget") {
      nums = `<span class="b-num"><span class="b-inv-label">${moneyIn(r.billed, currency)}</span></span><span class="row-tag bad">no PO line</span>`;
    } else {
      const flag =
        over ? `<span class="row-flag">+${Math.round(invPctOfBudget - 100)}%</span>` : "";
      nums =
        `<span class="b-num"><span class="b-inv-label">${moneyIn(r.billed, currency)}</span>` +
        ` <span class="b-slash">/</span> <span class="b-po-label">PO ${moneyIn(r.budget, currency)}</span></span>${flag}`;
    }

    html += `
      <div class="budget-row">
        <span class="b-name" title="${esc(r.name)}">${shownName(r.name)}</span>
        <span class="b-track">
          <span class="b-fill b-po" style="width:${poPct}%"></span>
          <span class="b-fill b-inv" style="width:${over ? Math.min(100, poPct) : invPct}%"></span>
          ${over ? `<span class="b-over" style="left:${poPct}%;width:${2 + overPct}%"></span>` : ""}
        </span>
        <span class="b-cells">${nums}</span>
      </div>`;
  });

  /* total strip: invoice total vs the full PO budget (all items) */
  let invoiceTotal = report.invoice_total != null ? Number(report.invoice_total) : null;
  if (invoiceTotal == null) {
    let sum = 0;
    let any = false;
    rows.forEach((r) => {
      if (r.billed != null) { sum += r.billed; any = true; }
    });
    invoiceTotal = any ? sum : null;
  }
  let poTotal = 0;
  let poCounted = false;
  poItems.forEach((p) => {
    const t = poLineTotal(p);
    if (t != null) { poTotal += t; poCounted = true; }
  });
  const billedCount = rows.filter((r) => r.state === "billed").length;
  const notBilledCount = rows.filter((r) => r.state === "notbilled").length;

  let badge = "";
  if (invoiceTotal != null && poCounted && poTotal > 0) {
    const util = (invoiceTotal / poTotal) * 100;
    const cls = util > 100.05 ? "bad" : util > 60 ? "warn" : "ok";
    badge = `<span class="chip ${cls}" title="Invoice total as a share of the PO budget">${util < 1 ? util.toFixed(1) : Math.round(util)}% utilized</span>`;
  }

  const noteBits = [];
  if (poCounted) {
    noteBits.push(`PO budget covers all ${poItems.length} item${poItems.length === 1 ? "" : "s"}`);
    if (notBilledCount) noteBits.push(`${notBilledCount} not billed yet (partial shipment is normal)`);
  }
  if (invoiceTotal != null && poCounted && Math.abs(invoiceTotal - poTotal) > 0.005) {
    const diff = invoiceTotal - poTotal;
    if (diff > 0) noteBits.push(`billing ${moneyIn(diff, currency)} above the full PO budget`);
    else noteBits.push(`${moneyIn(-diff, currency)} of the PO budget still open`);
  }
  if (invoiceTotal == null) noteBits.push("invoice total not extracted");

  html += `
    <div class="budget-total">
      <span class="bt-label">Total</span>
      <span class="bt-note">${esc(noteBits.join(" · "))}</span>
      <span class="bt-right"><span class="bt-nums">${invoiceTotal != null ? `<span class="b-inv-label">${esc(moneyIn(invoiceTotal, currency))}</span>` : "-"}${poCounted ? ` <span class="b-slash">/</span> <span class="b-po-label">PO ${esc(moneyIn(poTotal, currency))}</span>` : ""}</span>
      ${badge}</span>
    </div>`;

  els.totalsChart.innerHTML = html;
}

function trunc(text, max) {
  const s = String(text);
  return s.length > max ? s.slice(0, max - 1).trimEnd() + "…" : s;
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
  const okLen = matched > 0 ? (matched / total) * C : 0;
  /* dedicated arcs per state so a zero share never draws a stray dot */
  const greenCircle = matched > 0
    ? `<circle cx="70" cy="70" r="${R}" fill="none" stroke="#16a34a" stroke-width="15"
            stroke-dasharray="${okLen.toFixed(2)} ${(C - okLen).toFixed(2)}"
            stroke-dashoffset="${(okLen / 2).toFixed(2)}" transform="rotate(-90 70 70)" stroke-linecap="round"/>`
    : "";
  const grayCircle = matched < total
    ? `<circle cx="70" cy="70" r="${R}" fill="none" stroke="#cbd5e1" stroke-width="15"
            stroke-dasharray="${(C - okLen).toFixed(2)} ${C.toFixed(2)}"
            stroke-dashoffset="${(-okLen / 2).toFixed(2)}" transform="rotate(-90 70 70)"/>`
    : "";
  const svg = `
    <div class="donut-wrap">
      <svg viewBox="0 0 140 140" role="img" aria-label="Matched share of invoice lines">
        <circle cx="70" cy="70" r="${R}" fill="none" stroke="#eef2f7" stroke-width="15"/>
        ${grayCircle}
        ${greenCircle}
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
/* landing only: product tour carousel, auto-advance + manual controls, no deps */
(function tourCarousel() {
  const viewer = document.getElementById("tour-viewer");
  if (!viewer) return;
  const slides = Array.from(viewer.querySelectorAll(".tour-slide"));
  const dots = Array.from(viewer.querySelectorAll(".tour-dot"));
  const caption = viewer.querySelector(".tour-caption");
  const zoom = viewer.querySelector(".tour-zoom");
  const prevBtn = viewer.querySelector(".tour-prev");
  const nextBtn = viewer.querySelector(".tour-next");
  if (slides.length < 2 || dots.length !== slides.length || !caption) return;

  const INTERVAL = 5200;
  const reducedMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let index = 0;
  let timer = null;
  let hovered = false;

  function show(next, manual) {
    index = (next + slides.length) % slides.length;
    slides.forEach((slide, n) => slide.classList.toggle("is-active", n === index));
    dots.forEach((dot, n) => dot.classList.toggle("is-active", n === index));
    const active = slides[index];
    caption.innerHTML =
      "<strong>" + esc(active.dataset.title || "") + "</strong><span>" +
      esc(active.dataset.sub || "") + "</span>";
    const img = active.querySelector("img");
    if (zoom && img) zoom.setAttribute("href", img.getAttribute("src"));
    if (manual) restartTimer();
  }

  function startTimer() {
    if (timer || reducedMotion || hovered || document.hidden) return;
    timer = setInterval(() => {
      if (document.hidden) return;
      /* autoplay only while the card is actually on screen */
      const rect = viewer.getBoundingClientRect();
      const vh = window.innerHeight || document.documentElement.clientHeight;
      const visible = rect.bottom > 140 && rect.top < vh - 80;
      if (visible) show(index + 1);
    }, INTERVAL);
  }
  function stopTimer() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }
  function restartTimer() {
    stopTimer();
    startTimer();
  }

  prevBtn.addEventListener("click", () => show(index - 1, true));
  nextBtn.addEventListener("click", () => show(index + 1, true));
  dots.forEach((dot, n) => dot.addEventListener("click", () => show(n, true)));

  ["pointerenter", "focusin"].forEach((ev) =>
    viewer.addEventListener(ev, () => { hovered = true; stopTimer(); }, true)
  );
  ["pointerleave", "focusout"].forEach((ev) =>
    viewer.addEventListener(ev, () => { hovered = false; startTimer(); }, true)
  );

  let touchX = null;
  viewer.addEventListener("touchstart", (e) => {
    touchX = e.touches[0].clientX;
    hovered = true;
    stopTimer();
  }, { passive: true });
  viewer.addEventListener("touchend", (e) => {
    if (touchX != null) {
      const delta = e.changedTouches[0].clientX - touchX;
      if (Math.abs(delta) > 48) show(index + (delta < 0 ? 1 : -1), true);
    }
    touchX = null;
    setTimeout(() => { hovered = false; startTimer(); }, 500);
  }, { passive: true });

  viewer.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { e.preventDefault(); show(index - 1, true); }
    if (e.key === "ArrowRight") { e.preventDefault(); show(index + 1, true); }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopTimer();
    else startTimer();
  });

  show(0);
  startTimer();
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

/* ------------------------------------------------ landing: copy-code buttons */
(function enableCopyButtons() {
  const buttons = document.querySelectorAll(".copy-btn[data-copy]");
  if (!buttons.length) return;

  async function writeText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    /* insecure-context fallback: temporary textarea + execCommand */
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }

  buttons.forEach((btn) => {
    const idleLabel = btn.getAttribute("data-copy") || "Copy to clipboard";
    btn.addEventListener("click", async () => {
      const wrap = btn.closest(".code-wrap") || btn.closest(".run-card");
      const code = wrap && (wrap.querySelector("pre code") || wrap.querySelector("code"));
      if (!code) return;
      const text = code.textContent.replace(/^\n+/, "").replace(/\s+$/, "");
      try {
        await writeText(text);
        btn.classList.add("copied");
        btn.setAttribute("aria-label", "Copied");
        btn.setAttribute("title", "Copied!");
        setTimeout(() => {
          btn.classList.remove("copied");
          btn.setAttribute("aria-label", idleLabel);
          btn.setAttribute("title", "Copy");
        }, 1600);
      } catch {
        /* clipboard unavailable (denied permission): fail silently */
      }
    });
  });
})();

/* ------------------------------------------------ landing: scrollspy chips */
(function scrollSpy() {
  const links = Array.from(document.querySelectorAll(".nav-chips a[href^='#']"));
  if (!links.length || !("IntersectionObserver" in window)) return;
  const sections = links
    .map((a) => document.getElementById(a.getAttribute("href").slice(1)))
    .filter(Boolean);
  if (!sections.length) return;

  function setActive(id) {
    links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + id));
  }

  setActive(sections[0].id);
  /* a section "owns" the nav chip while it crosses the middle band of the viewport */
  const io = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) setActive(visible[0].target.id);
    },
    { rootMargin: "-35% 0px -55% 0px", threshold: 0 }
  );
  sections.forEach((s) => io.observe(s));
})();
