"use strict";

const ui = {
  form: document.getElementById("form"),
  unsupported: document.getElementById("unsupported"),
  host: document.getElementById("host"),
  port: document.getElementById("port"),
  mode: document.getElementById("mode"),
  group: document.getElementById("group"),
  secret: document.getElementById("secret"),
  keyState: document.getElementById("key-state"),
  save: document.getElementById("save"),
  test: document.getElementById("test"),
  forget: document.getElementById("forget"),
  status: document.getElementById("status"),
};

let storedKey = null;

function setStatus(text, kind) {
  ui.status.textContent = text;
  ui.status.classList.toggle("is-ok", kind === "ok");
  ui.status.classList.toggle("is-error", kind === "error");
}

function setBusy(busy) {
  for (const button of [ui.save, ui.test, ui.forget]) button.disabled = busy;
  if (!busy) ui.forget.disabled = !storedKey;
}

function showKeyState() {
  ui.keyState.textContent = storedKey
    ? "Schlüssel ist in diesem Browser gespeichert. Zum Ändern einen neuen eintragen."
    : "Kein Schlüssel gespeichert.";
  ui.keyState.classList.toggle("is-ok", Boolean(storedKey));
  ui.forget.disabled = !storedKey;
}

function fillSelect(select, values) {
  select.replaceChildren(
    ...values.map((value) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      return option;
    }),
  );
}

function selectValue(select, value) {
  const option = Array.from(select.options).find((item) => item.value === String(value));
  if (option) select.value = option.value;
  return Boolean(option);
}

function syncMode() {
  ui.group.disabled = ui.mode.value === "open";
}

function currentSettings() {
  return {
    host: ui.host.value,
    port: Number(ui.port.value),
    mode: ui.mode.value === "open" ? "open" : "upload",
    groupShapes: ui.group.checked,
  };
}

async function init() {
  const support = LightBurn.support();
  if (!support.ok) {
    ui.unsupported.textContent = support.reason;
    ui.unsupported.hidden = false;
    for (const control of ui.form.elements) control.disabled = true;
    return;
  }

  try {
    const targets = await LightBurn.targets();
    fillSelect(ui.host, Array.isArray(targets.hosts) ? targets.hosts : []);
    fillSelect(ui.port, Array.isArray(targets.ports) ? targets.ports : []);
  } catch (error) {
    setStatus(error.message, "error");
  }

  try {
    const stored = await LightBurn.load();
    if (stored) {
      storedKey = stored.key;
      const hostKnown = !stored.host || selectValue(ui.host, stored.host);
      const portKnown = selectValue(ui.port, stored.port);
      ui.mode.value = stored.mode;
      ui.group.checked = stored.groupShapes;
      if (!hostKnown || !portKnown) {
        setStatus("Der gespeicherte Host oder Port ist auf dem Server nicht mehr freigegeben. Bitte neu wählen und speichern.", "error");
      }
    }
  } catch (error) {
    setStatus(error.message, "error");
  }
  syncMode();
  showKeyState();
}

ui.mode.addEventListener("change", syncMode);

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!ui.host.value || !ui.port.value) {
    setStatus("Bitte Host und Port wählen.", "error");
    return;
  }
  setBusy(true);
  try {
    const typed = ui.secret.value.trim();
    const key = typed ? await LightBurn.importSecret(typed) : null;
    const record = await LightBurn.save(currentSettings(), key);
    ui.secret.value = "";
    storedKey = record.key;
    setStatus(storedKey ? "Gespeichert." : "Gespeichert – es fehlt noch der Schlüssel.", storedKey ? "ok" : "error");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
    showKeyState();
  }
});

ui.test.addEventListener("click", async () => {
  setBusy(true);
  setStatus("Verbindung wird getestet …");
  try {
    const typed = ui.secret.value.trim();
    // A typed but unsaved secret is tested with a temporary in-memory key; nothing is stored.
    const key = typed ? await LightBurn.importSecret(typed) : storedKey;
    if (!key) throw new Error("Bitte zuerst einen Schlüssel eintragen.");
    const result = await LightBurn.test(currentSettings(), key);
    const note = typed ? " (Schlüssel noch nicht gespeichert)" : "";
    setStatus(String(result.message || "") + (result.ok ? note : ""), result.ok ? "ok" : "error");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
  }
});

ui.forget.addEventListener("click", async () => {
  setBusy(true);
  try {
    await LightBurn.remove();
    storedKey = null;
    setStatus("Schlüssel und LightBurn-Einstellungen aus diesem Browser gelöscht.", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
    showKeyState();
  }
});

init();
