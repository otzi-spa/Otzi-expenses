(function () {
  "use strict";

  const DEBUGGER_PROTOCOL_VERSION = "1.3";

  function attachDebugger(tabId) {
    return new Promise((resolve, reject) => {
      console.info("[Otzi Rindegastos] attach debugger", { tabId });
      chrome.debugger.attach({ tabId }, DEBUGGER_PROTOCOL_VERSION, () => {
        const error = chrome.runtime.lastError;
        if (error && !String(error.message || "").includes("Another debugger is already attached")) {
          console.error("[Otzi Rindegastos] debugger attach failed", error.message);
          reject(new Error(error.message));
          return;
        }
        if (error) {
          console.warn("[Otzi Rindegastos] debugger already attached", error.message);
        }
        resolve();
      });
    });
  }

  function detachDebugger(tabId) {
    return new Promise((resolve) => {
      chrome.debugger.detach({ tabId }, () => resolve());
    });
  }

  function sendDebuggerCommand(tabId, method, params) {
    return new Promise((resolve, reject) => {
      chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
        const error = chrome.runtime.lastError;
        if (error) {
          reject(new Error(error.message));
          return;
        }
        resolve(result);
      });
    });
  }

  function keyDescriptor(char) {
    if (/^\d$/.test(char)) {
      const code = `Digit${char}`;
      return {
        key: char,
        code,
        windowsVirtualKeyCode: char.charCodeAt(0),
        nativeVirtualKeyCode: char.charCodeAt(0),
      };
    }
    if (char === ".") {
      return {
        key: ".",
        code: "Period",
        windowsVirtualKeyCode: 190,
        nativeVirtualKeyCode: 190,
      };
    }
    if (char === ",") {
      return {
        key: ",",
        code: "Comma",
        windowsVirtualKeyCode: 188,
        nativeVirtualKeyCode: 188,
      };
    }
    if (char === "/") {
      return {
        key: "/",
        code: "Slash",
        windowsVirtualKeyCode: 191,
        nativeVirtualKeyCode: 191,
      };
    }
    return {
      key: char,
      code: "",
      windowsVirtualKeyCode: char.charCodeAt(0),
      nativeVirtualKeyCode: char.charCodeAt(0),
    };
  }

  async function dispatchTyping(tabId, text) {
    const value = String(text || "");
    for (const char of value) {
      const descriptor = keyDescriptor(char);
      await sendDebuggerCommand(tabId, "Input.dispatchKeyEvent", {
        type: "keyDown",
        ...descriptor,
        text: char,
        unmodifiedText: char,
      });
      await sendDebuggerCommand(tabId, "Input.dispatchKeyEvent", {
        type: "keyUp",
        ...descriptor,
      });
    }
  }

  async function insertText(tabId, text) {
    console.info("[Otzi Rindegastos] debugger typing", { tabId, text: String(text || "") });
    await attachDebugger(tabId);
    try {
      await dispatchTyping(tabId, text);
    } finally {
      await detachDebugger(tabId);
      console.info("[Otzi Rindegastos] debugger detached", { tabId });
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || message.type !== "OTZI_DEBUGGER_INSERT_TEXT") {
      return false;
    }

    const tabId = sender.tab && sender.tab.id;
    if (!tabId) {
      console.error("[Otzi Rindegastos] sender tab missing");
      sendResponse({ ok: false, error: "No pude identificar la pestana activa." });
      return false;
    }

    insertText(tabId, message.text)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => {
        console.error("[Otzi Rindegastos] debugger typing failed", error.message || error);
        sendResponse({ ok: false, error: error.message || "No pude escribir con debugger." });
      });

    return true;
  });
})();
