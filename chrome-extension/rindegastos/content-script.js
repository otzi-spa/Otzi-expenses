(function () {
  "use strict";

  if (window.__OTZI_RINDEGASTOS_CONTENT_SCRIPT_LOADED__) {
    return;
  }
  window.__OTZI_RINDEGASTOS_CONTENT_SCRIPT_LOADED__ = true;

  const FALLBACK_FIELD_ORDER = [
    "selector",
    "proveedor",
    "total",
    "moneda",
    "impuesto",
    "valor_impuesto",
    "otros_impuestos",
    "fecha",
    "centro_costo_faena",
    "nombre_quien_rinde",
    "rut_proveedor",
    "tipo_documento",
    "numero_documento",
    "vehiculo_equipo",
    "categoria_rindegastos",
    "nota",
  ];

  const HEADER_FIELD_MAP = new Map([
    ["proveedor", "proveedor"],
    ["total", "total"],
    ["moneda", "moneda"],
    ["impuesto", "impuesto"],
    ["valor del impuesto", "valor_impuesto"],
    ["otros impuestos", "otros_impuestos"],
    ["fecha", "fecha"],
    ["centro de costo / faena", "centro_costo_faena"],
    ["centro de costo / fae", "centro_costo_faena"],
    ["nombre quien rinde", "nombre_quien_rinde"],
    ["numero de documento", "numero_documento"],
    ["número de documento", "numero_documento"],
    ["rut proveedor", "rut_proveedor"],
    ["tipo de documento", "tipo_documento"],
    ["vehiculo o equipo", "vehiculo_equipo"],
    ["vehículo o equipo", "vehiculo_equipo"],
    ["km.carguío", "km_carguio"],
    ["km carguío", "km_carguio"],
    ["litros combustible", "litros_combustible"],
    ["categoria", "categoria_rindegastos"],
    ["categoría", "categoria_rindegastos"],
    ["nota", "nota"],
    ["archivo", "archivo"],
  ]);

  const NG_SELECT_FIELDS = new Set([
    "moneda",
    "impuesto",
    "centro_costo_faena",
    "nombre_quien_rinde",
    "tipo_documento",
    "vehiculo_equipo",
    "categoria_rindegastos",
  ]);

  const VALUE_FIELDS = new Set([
    "proveedor",
    "total",
    "valor_impuesto",
    "otros_impuestos",
    "fecha",
    "rut_proveedor",
    "numero_documento",
    "km_carguio",
    "litros_combustible",
    "nota",
  ]);

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }

  function collectVisualFields(container) {
    if (!container) return [];
    const fields = [];
    Array.from(container.children).forEach((child) => {
      if (child.classList && child.classList.contains("field")) {
        fields.push(child);
        return;
      }
      fields.push(
        ...Array.from(child.querySelectorAll(":scope > .field")).filter(
          (field) => field.classList && field.classList.contains("field"),
        ),
      );
    });
    return fields;
  }

  function dispatchInputEvents(element) {
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: element.value || "" }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function setNativeValue(element, value) {
    element.focus();
    element.click();
    const prototype = Object.getPrototypeOf(element);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor && descriptor.set) {
      descriptor.set.call(element, value);
    } else {
      element.value = value;
    }
    dispatchInputEvents(element);
  }

  function findSheet() {
    return document.querySelector(".rgFieldSheet form") || document.querySelector("form");
  }

  function getExpenseRows() {
    return Array.from(document.querySelectorAll(".bodySheet[formarrayname='expenses']"));
  }

  function headerKeyForField(fieldEl, index) {
    if (index === 0) return "selector";
    const label = normalizeText(fieldEl.textContent || "");
    if (!label) return null;
    for (const [needle, key] of HEADER_FIELD_MAP.entries()) {
      if (label.includes(normalizeText(needle))) return key;
    }
    return null;
  }

  function getHeaderFieldOrder() {
    const header = document.querySelector(".headerSheet");
    const headerFields = collectVisualFields(header);
    const order = headerFields.map(headerKeyForField).filter(Boolean);
    return order.length >= 8 ? order : FALLBACK_FIELD_ORDER;
  }

  function closestField(element) {
    return element ? element.closest(".field") : null;
  }

  function firstMatch(container, selectors) {
    for (const selector of selectors) {
      const element = container.querySelector(selector);
      if (element) return element;
    }
    return null;
  }

  function getRowFields(row) {
    const fields = collectVisualFields(row);
    const fieldOrder = getHeaderFieldOrder();
    const mapped = {};
    fieldOrder.forEach((key, index) => {
      mapped[key] = fields[index] || null;
    });
    const semanticFields = {
      proveedor: [
        'input[formcontrolname="merchant"]',
        'input#merchant',
        'input[placeholder*="proveedor" i]',
      ],
      total: [
        'input[formcontrolname="originalTotal"]',
        'input[placeholder="Total" i]',
      ],
      fecha: [
        'input[formcontrolname="date"]',
        'input[placeholder="Fecha" i]',
      ],
      rut_proveedor: [
        'input[placeholder*="RUT proveedor" i]',
        'input[placeholder*="rut proveedor" i]',
      ],
      numero_documento: [
        'input[placeholder*="Número de Documento" i]',
        'input[placeholder*="Numero de Documento" i]',
        'input[placeholder*="documento" i]',
      ],
      nota: [
        'input[formcontrolname="note"]',
        'textarea[formcontrolname="note"]',
        'input[placeholder*="nota" i]',
        'input[placeholder*="comentario" i]',
      ],
    };
    Object.entries(semanticFields).forEach(([key, selectors]) => {
      const field = closestField(firstMatch(row, selectors));
      if (field) mapped[key] = field;
    });
    return mapped;
  }

  function findAddExpenseButton() {
    const buttons = Array.from(document.querySelectorAll("button"));
    return buttons.find((button) => normalizeText(button.textContent).includes("agregar otro gasto"));
  }

  async function ensureRowCount(targetCount) {
    let rows = getExpenseRows();
    let guard = 0;
    while (rows.length < targetCount && guard < targetCount + 5) {
      const button = findAddExpenseButton();
      if (!button) {
        throw new Error(`No encontre boton para agregar filas. Hay ${rows.length}, necesito ${targetCount}.`);
      }
      button.click();
      await sleep(350);
      rows = getExpenseRows();
      guard += 1;
    }
    if (rows.length < targetCount) {
      throw new Error(`No se pudieron crear suficientes filas. Hay ${rows.length}, necesito ${targetCount}.`);
    }
    return rows;
  }

  function findInput(fieldEl) {
    if (!fieldEl) return null;
    return fieldEl.querySelector("input:not([type='hidden']), textarea");
  }

  function findInputForField(fieldEl, fieldName) {
    if (!fieldEl) return null;
    if (fieldName === "proveedor") {
      return (
        fieldEl.querySelector('input[formcontrolname="merchant"]') ||
        fieldEl.querySelector('input#merchant') ||
        fieldEl.querySelector('input[placeholder*="proveedor" i]') ||
        findInput(fieldEl)
      );
    }
    if (fieldName === "total") {
      return fieldEl.querySelector('input[formcontrolname="originalTotal"]') || findInput(fieldEl);
    }
    if (fieldName === "fecha") {
      return fieldEl.querySelector('input[formcontrolname="date"]') || findInput(fieldEl);
    }
    if (fieldName === "nota") {
      return fieldEl.querySelector('input[formcontrolname="note"], textarea[formcontrolname="note"]') || findInput(fieldEl);
    }
    return findInput(fieldEl);
  }

  function findNgSelect(fieldEl) {
    if (!fieldEl) return null;
    return fieldEl.querySelector("ng-select");
  }

  function parseMoney(value) {
    return String(value || "").replace(/[^\d]/g, "");
  }

  function valueForField(rowData, fieldName) {
    const map = {
      proveedor: rowData.proveedor,
      total: parseMoney(rowData.total),
      moneda: rowData.moneda,
      impuesto: rowData.impuesto,
      valor_impuesto: parseMoney(rowData.valor_impuesto),
      otros_impuestos: parseMoney(rowData.otros_impuestos),
      fecha: rowData.fecha,
      centro_costo_faena: rowData.centro_costo_faena,
      nombre_quien_rinde: rowData.nombre_quien_rinde,
      rut_proveedor: rowData.rut_proveedor,
      tipo_documento: rowData.tipo_documento,
      numero_documento: rowData.numero_documento,
      vehiculo_equipo: rowData.vehiculo_equipo,
      km_carguio: rowData.km_carguio,
      litros_combustible: rowData.litros_combustible,
      categoria_rindegastos: rowData.categoria_rindegastos,
      nota: rowData.nota,
    };
    return map[fieldName] || "";
  }

  function shouldSkipEmptyNgSelect(fieldName, value) {
    return !value && (fieldName === "impuesto" || fieldName === "vehiculo_equipo" || fieldName === "categoria_rindegastos");
  }

  function visibleOptions() {
    return Array.from(document.querySelectorAll(".ng-dropdown-panel .ng-option")).filter((option) => {
      const rect = option.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });
  }

  function optionMatches(option, value) {
    const optionText = normalizeText(option.textContent);
    const target = normalizeText(value);
    return optionText === target || optionText.includes(target) || target.includes(optionText);
  }

  async function selectNgOption(ngSelect, value) {
    if (!value) return { ok: true, skipped: true };

    const container = ngSelect.querySelector(".ng-select-container") || ngSelect;
    container.click();
    await sleep(120);

    const input = ngSelect.querySelector("input[role='combobox'], .ng-input input");
    if (!input) {
      return { ok: false, error: "ng-select sin input interno" };
    }

    input.focus();
    setNativeValue(input, value);
    await sleep(350);

    let options = visibleOptions();
    let match = options.find((option) => optionMatches(option, value));

    if (!match && value.includes(" - ")) {
      const shortValue = value.split(" - ")[0].trim();
      setNativeValue(input, shortValue);
      await sleep(350);
      options = visibleOptions();
      match = options.find((option) => optionMatches(option, shortValue));
    }

    if (!match) {
      document.body.click();
      return { ok: false, error: `No encontre opcion "${value}"` };
    }

    match.click();
    await sleep(160);
    const selectedText = ngSelect.querySelector(".ng-value-label");
    if (!selectedText) {
      return { ok: false, error: `No pude confirmar seleccion "${value}"` };
    }
    return { ok: true };
  }

  function setInputField(fieldEl, fieldName, value) {
    if (!value && fieldName !== "valor_impuesto" && fieldName !== "otros_impuestos") {
      return { ok: true, skipped: true };
    }

    const input = findInputForField(fieldEl, fieldName);
    if (!input) {
      return { ok: false, error: "campo sin input" };
    }

    if (input.disabled) {
      return { ok: true, skipped: true, warning: "input deshabilitado" };
    }

    if (fieldName === "fecha") {
      input.removeAttribute("readonly");
    }
    setNativeValue(input, value);
    if (value && fieldName === "proveedor" && normalizeText(input.value) !== normalizeText(value)) {
      return { ok: false, error: `no se pudo escribir proveedor "${value}"` };
    }
    return { ok: true };
  }

  async function setField(rowFields, fieldName, value) {
    const fieldEl = rowFields[fieldName];
    if (!fieldEl) {
      return { ok: false, error: "campo no encontrado" };
    }

    if (NG_SELECT_FIELDS.has(fieldName)) {
      if (shouldSkipEmptyNgSelect(fieldName, value)) return { ok: true, skipped: true };
      const ngSelect = findNgSelect(fieldEl);
      if (!ngSelect) return { ok: false, error: "ng-select no encontrado" };
      return selectNgOption(ngSelect, value);
    }

    if (VALUE_FIELDS.has(fieldName)) {
      return setInputField(fieldEl, fieldName, value);
    }

    return { ok: true, skipped: true };
  }

  async function fillRow(rowEl, rowData) {
    const fields = getRowFields(rowEl);
    const result = {
      expenses_id: rowData.expenses_id || "",
      errors: [],
      warnings: [],
    };

    const orderedFields = [
      "proveedor",
      "total",
      "moneda",
      "impuesto",
      "valor_impuesto",
      "otros_impuestos",
      "fecha",
      "centro_costo_faena",
      "nombre_quien_rinde",
      "rut_proveedor",
      "tipo_documento",
      "numero_documento",
      "vehiculo_equipo",
      "km_carguio",
      "litros_combustible",
      "categoria_rindegastos",
      "nota",
    ];

    for (const fieldName of orderedFields) {
      const value = valueForField(rowData, fieldName);
      const fieldResult = await setField(fields, fieldName, value);
      if (!fieldResult.ok) {
        result.errors.push(`${fieldName}: ${fieldResult.error}`);
      }
      if (fieldResult.warning) {
        result.warnings.push(`${fieldName}: ${fieldResult.warning}`);
      }
    }

    return result;
  }

  async function fillRindegastos(rows) {
    if (!findSheet()) {
      throw new Error("No encontre el formulario de Rindegastos en esta pagina.");
    }
    const sheetRows = await ensureRowCount(rows.length);
    const results = [];

    for (let index = 0; index < rows.length; index += 1) {
      const rowResult = await fillRow(sheetRows[index], rows[index]);
      results.push(rowResult);
    }

    return results;
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message && message.type === "OTZI_RINDEGASTOS_PING") {
      sendResponse({ ok: true });
      return false;
    }

    if (!message || message.type !== "OTZI_FILL_RINDEGASTOS") {
      return false;
    }

    fillRindegastos(message.rows || [])
      .then((results) => {
        sendResponse({ ok: true, results });
      })
      .catch((error) => {
        sendResponse({ ok: false, error: error.message || "Error desconocido" });
      });

    return true;
  });
})();
