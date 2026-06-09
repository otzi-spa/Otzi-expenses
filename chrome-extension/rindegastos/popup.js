(function () {
  "use strict";

  const BATCH_SIZE = 50;
  const DATA_HEADER = [
    "politica",
    "expenses_id",
    "proveedor",
    "total",
    "moneda",
    "impuesto",
    "valor_impuesto",
    "otros_impuestos",
    "fecha",
    "centro_costo_faena",
    "nombre_quien_rinde",
    "numero_documento",
    "rut_proveedor",
    "tipo_documento",
    "vehiculo_equipo",
    "km_carguio",
    "litros_combustible",
    "categoria_rindegastos",
    "nota",
  ];

  const REQUIRED_FIELDS = [
    "politica",
    "expenses_id",
    "proveedor",
    "total",
    "moneda",
    "fecha",
    "centro_costo_faena",
    "nombre_quien_rinde",
    "tipo_documento",
    "nota",
  ];

  const state = {
    rows: [],
    summary: new Map(),
    selectedPolicy: "",
    selectedBatchIndex: 0,
  };

  const els = {
    fileInput: document.getElementById("csvFileInput"),
    fileStatus: document.getElementById("fileStatus"),
    policySelect: document.getElementById("policySelect"),
    batchSelect: document.getElementById("batchSelect"),
    summaryBox: document.getElementById("summaryBox"),
    previewBody: document.getElementById("previewBody"),
    previewCount: document.getElementById("previewCount"),
    warningsBox: document.getElementById("warningsBox"),
    copyButton: document.getElementById("copyButton"),
    fillButton: document.getElementById("fillButton"),
    runStatus: document.getElementById("runStatus"),
  };

  function normalizeHeader(value) {
    return String(value || "").trim().replace(/^\uFEFF/, "").toLowerCase();
  }

  function parseCsv(text) {
    const rows = [];
    let row = [];
    let field = "";
    let inQuotes = false;

    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      const next = text[i + 1];

      if (inQuotes) {
        if (ch === '"' && next === '"') {
          field += '"';
          i += 1;
        } else if (ch === '"') {
          inQuotes = false;
        } else {
          field += ch;
        }
        continue;
      }

      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        row.push(field);
        field = "";
      } else if (ch === "\n") {
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
      } else if (ch !== "\r") {
        field += ch;
      }
    }

    if (field.length > 0 || row.length > 0) {
      row.push(field);
      rows.push(row);
    }
    return rows;
  }

  function findDataHeaderIndex(csvRows) {
    return csvRows.findIndex((row) => {
      const normalized = row.map(normalizeHeader);
      return DATA_HEADER.every((header, index) => normalized[index] === header);
    });
  }

  function readSummary(csvRows, dataHeaderIndex) {
    const summary = new Map();
    for (let i = 1; i < dataHeaderIndex; i += 1) {
      const row = csvRows[i] || [];
      const policy = String(row[0] || "").trim();
      const count = Number.parseInt(row[1], 10);
      if (policy && Number.isFinite(count)) {
        summary.set(policy, count);
      }
    }
    return summary;
  }

  function rowsFromCsv(csvRows) {
    const headerIndex = findDataHeaderIndex(csvRows);
    if (headerIndex === -1) {
      throw new Error("No se encontro el encabezado de datos del export Rindegastos.");
    }

    const header = csvRows[headerIndex].map(normalizeHeader);
    const dataRows = csvRows.slice(headerIndex + 1).filter((row) => row.some((cell) => String(cell || "").trim()));
    const rows = dataRows.map((row, index) => {
      const item = { __rowNumber: index + 1 };
      header.forEach((key, colIndex) => {
        item[key] = String(row[colIndex] || "").trim();
      });
      return item;
    });

    return {
      rows,
      summary: readSummary(csvRows, headerIndex),
    };
  }

  function getPolicies() {
    return Array.from(new Set(state.rows.map((row) => row.politica).filter(Boolean))).sort((a, b) => a.localeCompare(b));
  }

  function getSelectedRows() {
    if (!state.selectedPolicy) return [];
    const policyRows = state.rows.filter((row) => row.politica === state.selectedPolicy);
    const start = state.selectedBatchIndex * BATCH_SIZE;
    return policyRows.slice(start, start + BATCH_SIZE);
  }

  function getBatchCount(policy) {
    const count = state.rows.filter((row) => row.politica === policy).length;
    return Math.max(1, Math.ceil(count / BATCH_SIZE));
  }

  function renderPolicies() {
    const policies = getPolicies();
    els.policySelect.innerHTML = "";

    if (!policies.length) {
      els.policySelect.disabled = true;
      els.policySelect.append(new Option("Sin politicas", ""));
      return;
    }

    policies.forEach((policy) => {
      const count = state.rows.filter((row) => row.politica === policy).length;
      els.policySelect.append(new Option(`${policy} (${count})`, policy));
    });
    els.policySelect.disabled = false;
    state.selectedPolicy = policies[0];
    els.policySelect.value = state.selectedPolicy;
  }

  function renderBatches() {
    els.batchSelect.innerHTML = "";
    if (!state.selectedPolicy) {
      els.batchSelect.disabled = true;
      els.batchSelect.append(new Option("Sin tandas", ""));
      return;
    }

    const policyRows = state.rows.filter((row) => row.politica === state.selectedPolicy);
    const batchCount = getBatchCount(state.selectedPolicy);
    for (let batchIndex = 0; batchIndex < batchCount; batchIndex += 1) {
      const start = batchIndex * BATCH_SIZE + 1;
      const end = Math.min((batchIndex + 1) * BATCH_SIZE, policyRows.length);
      els.batchSelect.append(new Option(`${start}-${end} (${end - start + 1})`, String(batchIndex)));
    }
    els.batchSelect.disabled = false;
    state.selectedBatchIndex = Math.min(state.selectedBatchIndex, batchCount - 1);
    els.batchSelect.value = String(state.selectedBatchIndex);
  }

  function renderSummary() {
    els.summaryBox.innerHTML = "";
    const policies = getPolicies();
    if (!policies.length) {
      els.summaryBox.hidden = true;
      return;
    }

    policies.forEach((policy) => {
      const count = state.summary.get(policy) || state.rows.filter((row) => row.politica === policy).length;
      const item = document.createElement("div");
      item.className = "summaryItem";
      const name = document.createElement("span");
      name.textContent = policy;
      const countEl = document.createElement("span");
      countEl.className = "summaryCount";
      countEl.textContent = String(count);
      item.append(name, countEl);
      els.summaryBox.append(item);
    });
    els.summaryBox.hidden = false;
  }

  function renderPreview() {
    const rows = getSelectedRows();
    els.previewBody.innerHTML = "";
    els.previewCount.textContent = `${rows.length} fila${rows.length === 1 ? "" : "s"}`;

    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.textContent = "Sin datos.";
      tr.append(td);
      els.previewBody.append(tr);
      return;
    }

    rows.slice(0, 50).forEach((row) => {
      const tr = document.createElement("tr");
      [row.expenses_id, row.proveedor, row.total, row.fecha].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value || "-";
        tr.append(td);
      });
      els.previewBody.append(tr);
    });
  }

  function collectWarnings(rows) {
    const warnings = [];
    rows.forEach((row, index) => {
      const missing = REQUIRED_FIELDS.filter((field) => !row[field]);
      if ((row.politica || "").trim().toLowerCase() === "combustibles") {
        ["vehiculo_equipo", "km_carguio", "litros_combustible"].forEach((field) => {
          if (!row[field] && !missing.includes(field)) missing.push(field);
        });
      }
      if (missing.length) {
        warnings.push(`Fila ${index + 1}: faltan ${missing.join(", ")}`);
      }
    });
    return warnings;
  }

  function renderWarnings() {
    const warnings = collectWarnings(getSelectedRows());
    els.warningsBox.innerHTML = "";
    if (!warnings.length) {
      els.warningsBox.hidden = true;
      return;
    }

    const title = document.createElement("strong");
    title.textContent = "Advertencias";
    const ul = document.createElement("ul");
    warnings.slice(0, 8).forEach((warning) => {
      const li = document.createElement("li");
      li.textContent = warning;
      ul.append(li);
    });
    if (warnings.length > 8) {
      const li = document.createElement("li");
      li.textContent = `Y ${warnings.length - 8} advertencias mas.`;
      ul.append(li);
    }
    els.warningsBox.append(title, ul);
    els.warningsBox.hidden = false;
  }

  function renderActions() {
    const hasRows = getSelectedRows().length > 0;
    els.copyButton.disabled = !hasRows;
    els.fillButton.disabled = !hasRows;
  }

  function renderAll() {
    renderPolicies();
    renderBatches();
    renderSummary();
    renderPreview();
    renderWarnings();
    renderActions();
  }

  function renderSelectionOnly() {
    renderBatches();
    renderPreview();
    renderWarnings();
    renderActions();
  }

  function toTsv(rows) {
    const header = DATA_HEADER;
    const lines = [header.join("\t")];
    rows.forEach((row) => {
      lines.push(header.map((key) => String(row[key] || "").replace(/\t/g, " ").replace(/\r?\n/g, " ")).join("\t"));
    });
    return lines.join("\n");
  }

  async function handleFile(file) {
    const text = await file.text();
    const parsed = rowsFromCsv(parseCsv(text));
    state.rows = parsed.rows;
    state.summary = parsed.summary;
    state.selectedBatchIndex = 0;
    els.fileStatus.textContent = `${file.name}: ${state.rows.length} filas cargadas.`;
    renderAll();
  }

  async function getActiveTab() {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    return tabs[0];
  }

  async function ensureContentScript(tabId) {
    try {
      await chrome.tabs.sendMessage(tabId, { type: "OTZI_RINDEGASTOS_PING" });
      return;
    } catch (error) {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["content-script.js"],
      });
    }
  }

  async function sendBatchToActiveTab(rows) {
    const tab = await getActiveTab();
    if (!tab || !tab.id) {
      throw new Error("No hay pestana activa.");
    }
    if (!/^https:\/\/app\.rindegastos\.com\//.test(tab.url || "")) {
      throw new Error("Abre la pestana de app.rindegastos.com antes de rellenar.");
    }
    await ensureContentScript(tab.id);
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "OTZI_FILL_RINDEGASTOS",
      rows,
      metadata: {
        policy: state.selectedPolicy,
        batchIndex: state.selectedBatchIndex,
        batchSize: BATCH_SIZE,
      },
    });
    return response;
  }

  els.fileInput.addEventListener("change", async () => {
    const file = els.fileInput.files && els.fileInput.files[0];
    if (!file) return;
    try {
      els.runStatus.textContent = "";
      await handleFile(file);
    } catch (error) {
      state.rows = [];
      state.summary = new Map();
      els.fileStatus.textContent = error.message || "No se pudo leer el CSV.";
      renderAll();
    }
  });

  els.policySelect.addEventListener("change", () => {
    state.selectedPolicy = els.policySelect.value;
    state.selectedBatchIndex = 0;
    renderSelectionOnly();
  });

  els.batchSelect.addEventListener("change", () => {
    state.selectedBatchIndex = Number.parseInt(els.batchSelect.value || "0", 10) || 0;
    renderPreview();
    renderWarnings();
    renderActions();
  });

  els.copyButton.addEventListener("click", async () => {
    try {
      const rows = getSelectedRows();
      await navigator.clipboard.writeText(toTsv(rows));
      els.runStatus.textContent = `${rows.length} filas copiadas al portapapeles.`;
    } catch (error) {
      els.runStatus.textContent = error.message || "No se pudo copiar al portapapeles.";
    }
  });

  els.fillButton.addEventListener("click", async () => {
    try {
      const rows = getSelectedRows();
      els.runStatus.textContent = "Enviando tanda a Rindegastos...";
      const response = await sendBatchToActiveTab(rows);
      if (!response || !response.ok) {
        throw new Error((response && response.error) || "Rindegastos no respondio.");
      }
      const failed = response.results.filter((item) => item.errors.length).length;
      els.runStatus.textContent = `${response.results.length} filas procesadas. ${failed} con advertencias.`;
    } catch (error) {
      els.runStatus.textContent = error.message || "No se pudo rellenar Rindegastos.";
    }
  });
})();
