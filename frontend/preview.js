/* InvoiceGuard AI: in-platform file previewer for the analyzer page.
 *
 * Standalone on purpose: it listens to the same file inputs the analyzer
 * uses and needs nothing from app.js, so it cannot interfere with the
 * analysis flow.
 *
 * Rendering rules:
 *   - PDFs: browser-native viewer in an <iframe> over a blob URL.
 *   - Images: plain <img>.
 *   - JSON/CSV purchase orders: pretty-printed when parseable, raw text
 *     otherwise; the analyzer still validates a not-yet-parseable file.
 *   - All file-derived text goes through textContent (never innerHTML), so
 *     a crafted invoice or PO file cannot inject markup.
 *   - Previous blob URLs are revoked before a file is replaced.
 */
"use strict";

(function filePreview() {
  const panes = [
    {
      label: "Invoice",
      input: document.getElementById("invoice-file"),
      card: document.getElementById("invoice-card"),
      slot: document.getElementById("invoice-preview"),
      meta: document.getElementById("invoice-preview-meta"),
      open: document.getElementById("invoice-preview-open"),
      download: document.getElementById("invoice-preview-download"),
      url: null,
    },
    {
      label: "Purchase Order",
      input: document.getElementById("po-file"),
      card: document.getElementById("po-card"),
      slot: document.getElementById("po-preview"),
      meta: document.getElementById("po-preview-meta"),
      open: document.getElementById("po-preview-open"),
      download: document.getElementById("po-preview-download"),
      url: null,
    },
  ].filter((pane) => pane.input && pane.slot);

  if (!panes.length) return;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function formatSize(bytes) {
    const kb = 1024;
    const mb = kb * kb;
    bytes = Number(bytes) || 0;
    if (bytes >= mb) return (bytes / mb).toFixed(bytes >= 10 * mb ? 0 : 1) + " MB";
    if (bytes >= kb) return Math.round(bytes / kb) + " KB";
    return bytes + " B";
  }

  function kindOf(file) {
    const name = (file.name || "").toLowerCase();
    if (file.type === "application/pdf" || name.endsWith(".pdf")) return "pdf";
    if (file.type.startsWith("image/") || /\.(png|jpe?g|webp)$/.test(name)) return "image";
    if (name.endsWith(".json") || file.type === "application/json") return "text-json";
    if (name.endsWith(".csv") || file.type === "text/csv") return "text-csv";
    return null;
  }

  function emptyState(pane, hint) {
    pane.slot.replaceChildren(el("p", "preview-empty muted", hint));
    if (pane.meta) pane.meta.replaceChildren(el("span", "muted small", "nothing loaded"));
    if (pane.open) {
      pane.open.hidden = true;
      pane.open.removeAttribute("href");
    }
    if (pane.download) {
      pane.download.hidden = true;
      pane.download.removeAttribute("href");
    }
  }

  function actionsFor(pane, file) {
    if (pane.open) {
      pane.open.href = pane.url;
      pane.open.hidden = false;
    }
    if (pane.download) {
      pane.download.href = pane.url;
      pane.download.download = file.name || "invoiceguard-file";
      pane.download.hidden = false;
    }
  }

  function showFile(pane, file) {
    const kind = kindOf(file);
    if (!kind) {
      emptyState(
        pane,
        "This file type can't be previewed here: pick a PDF or image for the invoice, or a .json / .csv for the Purchase Order."
      );
      return;
    }

    /* revoke the previous blob URL before creating a new one */
    if (pane.url) {
      URL.revokeObjectURL(pane.url);
      pane.url = null;
    }
    pane.url = URL.createObjectURL(file);

    if (pane.meta) {
      pane.meta.replaceChildren(
        el("span", "preview-name mono", file.name),
        el("span", "muted small", formatSize(file.size))
      );
    }
    actionsFor(pane, file);

    if (kind === "pdf") {
      const frame = el("iframe", "preview-frame");
      frame.src = pane.url;
      frame.title = "Preview of the uploaded " + pane.label.toLowerCase() + " file";
      pane.slot.replaceChildren(frame);
      return;
    }

    if (kind === "image") {
      const img = el("img", "preview-image");
      img.src = pane.url;
      img.alt = "Preview of the uploaded " + pane.label.toLowerCase() + " file";
      pane.slot.replaceChildren(img);
      return;
    }

    /* json / csv: read as text */
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      let body = text;
      if (kind === "text-json") {
        try {
          body = JSON.stringify(JSON.parse(text), null, 2);
        } catch {
          body = text; /* not-yet-valid JSON: show it raw */
        }
      }
      const pre = el("pre", "preview-text mono", body.replace(/\s+$/, ""));
      pre.setAttribute("aria-label", "Contents of the uploaded " + pane.label.toLowerCase() + " file");
      pane.slot.replaceChildren(pre);
    };
    reader.onerror = () =>
      emptyState(pane, "This file could not be read for preview; the analyzer can still process it.");
    reader.readAsText(file);
  }

  panes.forEach((pane) => {
    emptyState(
      pane,
      "No " +
        pane.label.toLowerCase() +
        " file yet — pick one above and it renders here. Both can be previewed in parallel."
    );

    /* picker selections (and programmatic re-selections) always fire change */
    pane.input.addEventListener("change", () => {
      const list = pane.input.files;
      if (list && list[0]) showFile(pane, list[0]);
      else emptyState(pane, "No " + pane.label.toLowerCase() + " file loaded.");
    });

    /* dropped files: landing in whichever card received them, read straight
     * from the drag payload so app.js's own drop handling stays untouched */
    const within = pane.card || pane.input.closest(".card");
    document.addEventListener(
      "drop",
      (ev) => {
        if (!ev.dataTransfer || !ev.dataTransfer.files || !ev.dataTransfer.files.length) return;
        if (!within || !(ev.target === within || within.contains(ev.target))) return;
        showFile(pane, ev.dataTransfer.files[0]);
      },
      false
    );
  });
})();
