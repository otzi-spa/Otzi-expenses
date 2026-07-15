(function () {
  "use strict";

  if (window.__OTZI_RINDEGASTOS_PAGE_BRIDGE__) {
    return;
  }
  window.__OTZI_RINDEGASTOS_PAGE_BRIDGE__ = true;

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function setRawValue(element, value) {
    const descriptor =
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value") ||
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value") ||
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value");
    if (descriptor && descriptor.set) {
      descriptor.set.call(element, value);
    } else {
      element.value = value;
    }
    element.setAttribute("value", value);
  }

  function dispatchInputEvents(element, data) {
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: data || element.value || "" }));
    element.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "Unidentified" }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
    element.dispatchEvent(new Event("focusout", { bubbles: true }));
  }

  async function typeValueLikeUser(element, value) {
    element.focus();
    element.click();
    setRawValue(element, "");
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward", data: "" }));
    await sleep(20);

    const text = String(value || "");
    for (const char of text) {
      element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: char }));
      setRawValue(element, `${element.value || ""}${char}`);
      element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: char }));
      element.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: char }));
      await sleep(6);
    }

    element.setAttribute("value", element.value || text);
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
    element.dispatchEvent(new Event("focusout", { bubbles: true }));
  }

  async function setInputValue(payload) {
    const element = document.querySelector(payload.selector);
    if (!element) {
      throw new Error("input no encontrado en pagina");
    }

    if (payload.fieldName === "fecha") {
      element.removeAttribute("readonly");
      element.readOnly = false;
    }

    if (payload.mode === "type") {
      await typeValueLikeUser(element, payload.value);
    } else {
      element.focus();
      element.click();
      setRawValue(element, payload.value);
      dispatchInputEvents(element);
    }

    if (payload.fieldName === "fecha") {
      element.dispatchEvent(new Event("dateInput", { bubbles: true }));
      element.dispatchEvent(new Event("dateChange", { bubbles: true }));
    }

    await sleep(30);
    return {
      value: element.value || "",
      attrValue: element.getAttribute("value") || "",
      className: element.className || "",
    };
  }

  async function prepareDebuggerInput(payload) {
    const element = document.querySelector(payload.selector);
    if (!element) {
      throw new Error("input no encontrado en pagina");
    }

    if (payload.fieldName === "fecha") {
      element.removeAttribute("readonly");
      element.readOnly = false;
    }
    element.focus();
    element.click();
    element.select();
    element.value = "";
    element.setAttribute("value", "");
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward", data: "" }));
    await sleep(20);
    return {
      value: element.value || "",
      attrValue: element.getAttribute("value") || "",
      className: element.className || "",
    };
  }

  async function finalizeDebuggerInput(payload) {
    const element = document.querySelector(payload.selector);
    if (!element) {
      throw new Error("input no encontrado en pagina");
    }

    element.setAttribute("value", element.value || "");
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
    element.dispatchEvent(new Event("focusout", { bubbles: true }));
    await sleep(30);
    return {
      value: element.value || "",
      attrValue: element.getAttribute("value") || "",
      className: element.className || "",
    };
  }

  window.addEventListener("message", async (event) => {
    if (event.source !== window) return;
    const message = event.data || {};
    if (message.source !== "OTZI_RINDEGASTOS_EXTENSION" || !message.id) return;

    try {
      let result;
      if (message.action === "setInputValue") {
        result = await setInputValue(message.payload || {});
      } else if (message.action === "prepareDebuggerInput") {
        result = await prepareDebuggerInput(message.payload || {});
      } else if (message.action === "finalizeDebuggerInput") {
        result = await finalizeDebuggerInput(message.payload || {});
      } else {
        throw new Error(`accion no soportada: ${message.action || ""}`);
      }
      window.postMessage({
        source: "OTZI_RINDEGASTOS_PAGE_BRIDGE",
        id: message.id,
        ok: true,
        result,
      }, "*");
    } catch (error) {
      window.postMessage({
        source: "OTZI_RINDEGASTOS_PAGE_BRIDGE",
        id: message.id,
        ok: false,
        error: error.message || "error desconocido",
      }, "*");
    }
  });
})();
