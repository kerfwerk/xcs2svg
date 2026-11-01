"use strict";

const state = { files: [], items: [], urls: [], pending: 0, maxUploadMb: 25, lightburn: null, sending: 0 };

const el = {
  drop: document.getElementById("drop"),
  pick: document.getElementById("pick"),
  input: document.getElementById("file"),
  limit: document.getElementById("limit"),
  toolbar: document.getElementById("toolbar"),
  summary: document.getElementById("summary"),
  clear: document.getElementById("clear"),
  downloadAll: document.getElementById("download-all"),
  sendAll: document.getElementById("send-all"),
  lbHint: document.getElementById("lb-hint"),
  lbHintText: document.getElementById("lb-hint-text"),
  results: document.getElementById("results"),
  cardTemplate: document.getElementById("card-template"),
  svgTemplate: document.getElementById("svg-template"),
};

function objectUrl(blob) {
  const url = URL.createObjectURL(blob);
  state.urls.push(url);
  return url;
}

function svgSize(svg) {
  const match = /<svg[^>]*\swidth="([\d.]+)mm"[^>]*\sheight="([\d.]+)mm"/.exec(svg);
  if (!match) return "";
  const format = (value) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: 1 });
  return `${format(match[1])} × ${format(match[2])} mm`;
}

function updateToolbar() {
  const done = state.files.length;
  el.toolbar.hidden = el.results.children.length === 0;
  el.downloadAll.disabled = done === 0 || state.pending > 0;
  el.sendAll.hidden = !state.lightburn || state.lightburn.mode !== "upload" || done < 2;
  el.sendAll.disabled = state.pending > 0 || state.sending > 0;
  // One send at a time: LightBurn's import result names no file, so overlapping sends could mix them up.
  for (const { item } of state.items) item.querySelector(".send").disabled = state.sending > 0;
  const parts = [];
  if (state.pending > 0) parts.push(`${state.pending} in Arbeit`);
  parts.push(done === 1 ? "1 SVG fertig" : `${done} SVGs fertig`);
  el.summary.textContent = parts.join(" · ");
}

function createCard(name) {
  const card = el.cardTemplate.content.firstElementChild.cloneNode(true);
  card.querySelector(".card-title").textContent = name;
  setStatus(card, "Wird umgewandelt …");
  el.results.prepend(card);
  return card;
}

function setStatus(card, text, kind) {
  const status = card.querySelector(".status");
  status.textContent = text;
  status.classList.toggle("is-ok", kind === "ok");
  status.classList.toggle("is-error", kind === "error");
}

function showWarnings(card, warnings) {
  const list = card.querySelector(".warnings");
  list.replaceChildren(...warnings.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  list.hidden = warnings.length === 0;
}

function showError(card, message) {
  setStatus(card, "Fehler", "error");
  const text = document.createElement("p");
  text.className = "error-text";
  text.textContent = message;
  card.querySelector(".card-body").replaceChildren(text);
}

function showResult(card, result) {
  const body = card.querySelector(".card-body");
  for (const file of result.files) {
    const url = objectUrl(new Blob([file.svg], { type: "image/svg+xml" }));
    const item = el.svgTemplate.content.firstElementChild.cloneNode(true);
    const image = item.querySelector("img");
    image.src = url;
    image.alt = `Vorschau von ${file.name}`;
    item.querySelector(".svg-name").textContent = file.name;
    item.querySelector(".svg-size").textContent = svgSize(file.svg);
    const link = item.querySelector("a");
    link.href = url;
    link.download = file.name;
    const send = item.querySelector(".send");
    if (state.lightburn) {
      send.hidden = false;
      send.addEventListener("click", () => sendToLightBurn(file, item));
    }
    body.append(item);
    state.files.push(file);
    state.items.push({ file, item });
  }
  const count = result.files.length;
  setStatus(card, count === 1 ? "Fertig" : `Fertig · ${count} Arbeitsflächen`, "ok");
  showWarnings(card, result.warnings || []);
}

async function convert(file) {
  const card = createCard(file.name);
  if (file.size > state.maxUploadMb * 1024 * 1024) {
    showError(card, `Die Datei ist zu groß (maximal ${state.maxUploadMb} MB).`);
    return;
  }
  state.pending += 1;
  updateToolbar();
  try {
    const response = await fetch("api/convert", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showError(card, payload.error || `Der Server hat mit Status ${response.status} geantwortet.`);
    } else {
      showResult(card, payload);
    }
  } catch {
    showError(card, "Keine Verbindung zum Server.");
  } finally {
    state.pending -= 1;
    updateToolbar();
  }
}

async function handleFiles(fileList) {
  const files = Array.from(fileList);
  if (files.length === 0) return;
  el.toolbar.hidden = false;
  for (const file of files) {
    await convert(file);
  }
}

// Minimal ZIP writer (stored, no compression) so "Alle herunterladen" yields one file.
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

const DOS_DATE = (1 << 5) | 1; // 1980-01-01, the earliest date ZIP can store

function zip(entries) {
  const encoder = new TextEncoder();
  const chunks = [];
  const central = [];
  let offset = 0;
  const used = new Set();
  for (const entry of entries) {
    let name = entry.name;
    for (let i = 2; used.has(name); i += 1) name = entry.name.replace(/\.svg$/, `_${i}.svg`);
    used.add(name);
    const nameBytes = encoder.encode(name);
    const data = encoder.encode(entry.svg);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true); // UTF-8 file names
    local.setUint16(12, DOS_DATE, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, nameBytes.length, true);
    chunks.push(local, nameBytes, data);

    const record = new DataView(new ArrayBuffer(46));
    record.setUint32(0, 0x02014b50, true);
    record.setUint16(4, 20, true);
    record.setUint16(6, 20, true);
    record.setUint16(8, 0x0800, true);
    record.setUint16(14, DOS_DATE, true);
    record.setUint32(16, crc, true);
    record.setUint32(20, data.length, true);
    record.setUint32(24, data.length, true);
    record.setUint16(28, nameBytes.length, true);
    record.setUint32(42, offset, true);
    central.push(record, nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const centralSize = central.reduce((sum, part) => sum + part.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...chunks, ...central, end], { type: "application/zip" });
}

// --- LightBurn: settings and key come from this browser's IndexedDB (see lightburn.js) ---

function setSendStatus(item, text, kind, detail) {
  const status = item.querySelector(".send-status");
  const label = document.createElement("span");
  label.textContent = text;
  status.replaceChildren(label);
  if (detail) {
    const more = document.createElement("span");
    more.className = "send-detail";
    more.textContent = detail;
    status.append(more);
  }
  status.hidden = false;
  status.classList.toggle("is-ok", kind === "ok");
  status.classList.toggle("is-error", kind === "error");
}

async function sendToLightBurn(file, item) {
  if (state.sending > 0) return;
  state.sending += 1;
  updateToolbar();
  setSendStatus(item, "LightBurn: wird gesendet …");
  try {
    const result = await LightBurn.send(state.lightburn, file.svg, file.name);
    if (result.accepted && result.confirmed === true) {
      setSendStatus(item, "LightBurn: in LightBurn importiert", "ok");
    } else if (result.accepted && result.confirmed === null) {
      setSendStatus(item, "LightBurn: übergeben, Bestätigung nicht möglich", "", result.message);
    } else {
      setSendStatus(item, `LightBurn: Fehler: ${result.message || "unbekannt"}`, "error");
    }
  } catch (error) {
    setSendStatus(item, `LightBurn: Fehler: ${error.message}`, "error");
  } finally {
    state.sending -= 1;
    updateToolbar();
  }
}

async function initLightBurn() {
  const support = LightBurn.support();
  let hint = "";
  if (!support.ok) {
    hint = "An LightBurn senden geht nur über http://localhost:8080 auf diesem PC oder über HTTPS.";
  } else {
    try {
      const settings = await LightBurn.load();
      if (settings && settings.key && settings.host) state.lightburn = settings;
      else hint = "Tipp: Die SVGs lassen sich auch direkt an LightBurn senden – einmalig einrichten unter";
    } catch (error) {
      hint = `${error.message} LightBurn-Versand einrichten unter`;
    }
  }
  el.lbHintText.textContent = hint;
  el.lbHint.hidden = !hint;
  updateToolbar();
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

el.downloadAll.addEventListener("click", () => {
  if (state.files.length === 1) {
    const [file] = state.files;
    download(new Blob([file.svg], { type: "image/svg+xml" }), file.name);
  } else if (state.files.length > 1) {
    download(zip(state.files), "svg-dateien.zip");
  }
});

el.sendAll.addEventListener("click", async () => {
  for (const { file, item } of [...state.items]) {
    if (!item.isConnected) continue;
    await sendToLightBurn(file, item);
  }
});

el.clear.addEventListener("click", () => {
  state.urls.forEach((url) => URL.revokeObjectURL(url));
  state.urls = [];
  state.files = [];
  state.items = [];
  el.results.replaceChildren();
  updateToolbar();
});

el.pick.addEventListener("click", () => el.input.click());
el.input.addEventListener("change", () => {
  handleFiles(el.input.files);
  el.input.value = "";
});

let dragDepth = 0;
el.drop.addEventListener("dragenter", (event) => {
  event.preventDefault();
  dragDepth += 1;
  el.drop.classList.add("is-over");
});
el.drop.addEventListener("dragover", (event) => {
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
el.drop.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) el.drop.classList.remove("is-over");
});
el.drop.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  el.drop.classList.remove("is-over");
  handleFiles(event.dataTransfer.files);
});
// Dropping next to the zone must not navigate away from the page.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

fetch("api/limits")
  .then((response) => (response.ok ? response.json() : null))
  .then((limits) => {
    if (limits && Number.isFinite(limits.maxUploadMb)) {
      state.maxUploadMb = limits.maxUploadMb;
      el.limit.textContent = `Mehrere Dateien möglich, je bis ${limits.maxUploadMb} MB.`;
    }
  })
  .catch(() => {});

initLightBurn();
