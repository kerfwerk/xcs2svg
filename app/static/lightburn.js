"use strict";

// LightBurn settings live only in this browser: IndexedDB "xcs2svg", store "settings".
// The secret is imported as a NON-extractable WebCrypto HMAC key and only that CryptoKey object is
// stored, so even script on this page can sign with it but never read it back. The raw secret is
// never written anywhere. The server only receives a token valid for about one minute:
// hex(HMAC-SHA256(secret, String(floor(unix_time / 60)))), as in the LightBurn REST docs.
const LightBurn = (() => {
  const DB_NAME = "xcs2svg";
  const STORE = "settings";
  const RECORD = "lightburn";
  const SECRET_PATTERN = /^[\x21-\x7e]{8,256}$/;

  function support() {
    if (!window.isSecureContext || !window.crypto || !crypto.subtle) {
      return {
        ok: false,
        reason:
          "Der Browser erlaubt die sichere Schlüsselablage (WebCrypto) nur über http://localhost oder HTTPS. " +
          "Bitte die App auf dem PC selbst über http://localhost:8080 öffnen oder hinter HTTPS betreiben.",
      };
    }
    if (!window.indexedDB) {
      return {
        ok: false,
        reason: "Dieser Browser bietet keinen lokalen Speicher (IndexedDB) an – z. B. in einem privaten Fenster.",
      };
    }
    return { ok: true, reason: "" };
  }

  function request(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function withStore(mode, action) {
    let db;
    try {
      const open = indexedDB.open(DB_NAME, 1);
      open.onupgradeneeded = () => {
        if (!open.result.objectStoreNames.contains(STORE)) open.result.createObjectStore(STORE);
      };
      db = await request(open);
    } catch {
      throw new Error("Der lokale Speicher des Browsers (IndexedDB) ist nicht verfügbar.");
    }
    try {
      const tx = db.transaction(STORE, mode);
      const done = new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
      const result = await request(action(tx.objectStore(STORE)));
      await done;
      return result;
    } finally {
      db.close();
    }
  }

  // -> {host, port, mode, groupShapes, key: CryptoKey | null} or null
  async function load() {
    let record;
    try {
      record = await withStore("readonly", (store) => store.get(RECORD));
    } catch {
      throw new Error("Die LightBurn-Einstellungen konnten nicht aus dem Browser gelesen werden.");
    }
    if (!record || typeof record !== "object") return null;
    const key = record.key instanceof CryptoKey ? record.key : null;
    return {
      host: typeof record.host === "string" ? record.host : "",
      port: Number.isInteger(record.port) ? record.port : 19520,
      mode: record.mode === "open" ? "open" : "upload",
      groupShapes: record.groupShapes === true,
      key,
    };
  }

  async function importSecret(secret) {
    const text = String(secret || "").trim();
    if (!SECRET_PATTERN.test(text)) {
      throw new Error("Der Schlüssel muss 8 bis 256 Zeichen lang sein (ohne Leerzeichen).");
    }
    return crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(text),
      { name: "HMAC", hash: "SHA-256" },
      false, // non-extractable: exportKey() rejects, the secret cannot be read back
      ["sign"],
    );
  }

  // Saves the non-secret settings; with a CryptoKey also replaces the stored key, otherwise keeps it.
  async function save(settings, key) {
    try {
      const current = await load();
      const record = {
        host: settings.host,
        port: settings.port,
        mode: settings.mode === "open" ? "open" : "upload",
        groupShapes: settings.groupShapes === true,
        key: key || (current && current.key) || null,
        savedAt: Date.now(),
      };
      await withStore("readwrite", (store) => store.put(record, RECORD));
      return record;
    } catch {
      throw new Error("Die LightBurn-Einstellungen konnten nicht im Browser gespeichert werden.");
    }
  }

  async function remove() {
    try {
      await withStore("readwrite", (store) => store.delete(RECORD));
    } catch {
      throw new Error("Der Schlüssel konnte nicht aus dem Browser gelöscht werden.");
    }
  }

  async function tokenForMinute(key, minute) {
    const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(String(minute)));
    return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function token(key) {
    return tokenForMinute(key, Math.floor(Date.now() / 60000)); // fresh right before each request
  }

  async function headers(settings, key) {
    return {
      "X-Requested-With": "xcs2svg",
      "X-LightBurn-Host": settings.host,
      "X-LightBurn-Port": String(settings.port),
      "X-LightBurn-Token": await token(key),
    };
  }

  async function post(path, init) {
    let response;
    try {
      response = await fetch(path, { method: "POST", credentials: "same-origin", ...init });
    } catch {
      throw new Error("Keine Verbindung zum Server.");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Der Server hat mit Status ${response.status} geantwortet.`);
    return payload;
  }

  // -> {ok, message}
  async function test(settings, key) {
    return post("api/lightburn/test", { headers: await headers(settings, key) });
  }

  // -> {accepted, confirmed: true | false | null, message}
  async function send(settings, svg, filename) {
    return post("api/lightburn/send", {
      headers: {
        ...(await headers(settings, settings.key)),
        "Content-Type": "image/svg+xml",
        "X-Filename": encodeURIComponent(filename),
        "X-LightBurn-Mode": settings.mode,
        "X-LightBurn-Group": settings.groupShapes ? "true" : "false",
      },
      body: svg,
    });
  }

  async function targets() {
    const response = await fetch("api/lightburn/targets").catch(() => null);
    if (!response || !response.ok) throw new Error("Die erlaubten LightBurn-Ziele konnten nicht geladen werden.");
    return response.json();
  }

  return { support, load, save, remove, importSecret, token, tokenForMinute, test, send, targets };
})();
