"use strict";

const byId = (id) => document.getElementById(id);
const [login, token, loginError, workspace, connection, statusButton, logout, sessions,
  messages, chat, message, send, cancel] = [
  "login", "token", "login-error", "workspace", "connection", "status", "logout", "sessions",
  "messages", "chat", "message", "send", "cancel",
].map(byId);
const modes = { ephemeral: "Temporal", persistent: "Persistente" };
const controls = [login.querySelector("button"), statusButton, sessions, send, ...Object.keys(modes).map(byId)];
const unavailable = "PC no disponible";
let busy = false;
let job = null;

function syncControls() {
  for (const control of controls) control.disabled = busy || !!job;
  send.disabled ||= !sessions.value;
  logout.disabled = cancel.disabled = busy;
  cancel.hidden = !job;
}

function addMessage(text, kind = "reply", url) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  bubble.textContent = text;
  if (typeof url === "string" && url.startsWith("https://")) {
    let target;
    try { target = new URL(url); } catch { /* Invalid links remain plain text. */ }
    if (target?.protocol === "https:") {
      const link = document.createElement("a");
      link.href = target.href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      link.textContent = target.href;
      bubble.append("\n", link);
    }
  }
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function renderResult(result, showNotices = true) {
  if (showNotices) for (const notice of result.notices) addMessage(notice, "notice");
  addMessage(result.text, result.kind === "error" ? "error" : "reply", result.url);
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method, credentials: "same-origin", cache: "no-store", redirect: "error",
    ...(body && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  }).catch(() => null);
  if (!response || [502, 503, 504].includes(response.status)) {
    connection.textContent = unavailable;
    throw new Error("La PC no responde. Comprueba Tailscale y que ADA esté en ejecución.");
  }
  connection.textContent = job ? "Pensando" : "Conectado";
  if (!response.ok) {
    const error = new Error(response.status === 409 ? "Ya hay una navegación en curso."
      : `No se pudo completar la solicitud (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return { status: response.status, data: response.status === 204 ? null : await response.json() };
}

function stopTracking() {
  if (!job) return;
  job.stream.close();
  if (job.retry) clearTimeout(job.retry);
  job = null;
}

function setSignedIn(signedIn) {
  workspace.hidden = statusButton.hidden = logout.hidden = !signedIn;
  login.hidden = signedIn;
  if (signedIn) return;
  stopTracking();
  messages.replaceChildren();
  sessions.replaceChildren();
  message.value = "";
}

function showError(error, context) {
  if (error.status === 401) {
    setSignedIn(false);
    connection.textContent = "Conectado";
    loginError.textContent = context === "login" ? "El token no es válido."
      : context === "startup" ? "" : "Tu sesión caducó. Vuelve a entrar.";
  } else if (workspace.hidden) loginError.textContent = error.message;
  else addMessage(error.message, "error");
}

async function run(action, context) {
  if (busy) return;
  busy = true;
  syncControls();
  try { await action(); } catch (error) { showError(error, context); }
  finally { busy = false; syncControls(); }
}

async function refreshSessions(selected = "") {
  const { data } = await api("/sessions");
  const options = data.map((session) => {
    const option = document.createElement("option");
    option.value = session.session_id;
    option.textContent = session.title ? `${modes[session.mode]}: ${session.title}` : modes[session.mode];
    return option;
  });
  sessions.replaceChildren(...options);
  if (selected) sessions.value = selected;
}

function finishJob(snapshot) {
  if (!["completed", "failed", "cancelled"].includes(snapshot.state)) return false;
  stopTracking();
  connection.textContent = "Conectado";
  if (snapshot.state === "completed") renderResult(snapshot.result, false);
  else if (snapshot.state === "failed") addMessage(snapshot.error || "La navegación falló.", "error");
  else addMessage("Navegación cancelada.", "notice");
  syncControls();
  return true;
}

function connectJob(tracked) {
  const stream = tracked.stream = new EventSource(`${tracked.path}/events`);
  stream.onopen = () => { connection.textContent = "Pensando"; };
  function listen(name, render) {
    stream.addEventListener(name, (event) => {
      if (job !== tracked || tracked.stream !== stream) return;
      const seq = Number(event.lastEventId);
      // A fresh EventSource replays from the beginning.
      if (seq > 0) {
        if (seq <= tracked.lastSeq) return;
        tracked.lastSeq = seq;
      }
      render(JSON.parse(event.data));
    });
  }
  listen("job.started", () => addMessage("Navegación en curso…", "notice"));
  listen("job.notice", (data) => addMessage(data.text, "notice"));
  for (const state of ["completed", "failed", "cancelled"]) {
    listen(`job.${state}`, finishJob);
  }
  stream.onerror = async () => {
    if (job !== tracked || tracked.stream !== stream) return;
    connection.textContent = unavailable;
    if (stream.readyState !== EventSource.CLOSED) return; // The browser reconnects with Last-Event-ID.
    try {
      const { data } = await api(tracked.path);
      if (job === tracked) finishJob(data);
    } catch (error) {
      if (job === tracked && [401, 404].includes(error.status)) {
        stopTracking();
        connection.textContent = "Conectado";
        showError(error);
        syncControls();
      }
    }
    if (job === tracked) {
      tracked.retry = setTimeout(() => { if (job === tracked) connectJob(tracked); }, 3000);
    }
  };
}

function watchJob(id) {
  job = { path: `/jobs/${encodeURIComponent(id)}`, lastSeq: 0 };
  connection.textContent = "Pensando";
  addMessage("Navegación solicitada…", "notice");
  connectJob(job);
}

login.onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    loginError.textContent = "";
    const credential = token.value;
    token.value = "";
    await api("/auth/login", "POST", { token: credential });
    await refreshSessions();
    setSignedIn(true);
  }, "login");
};
logout.onclick = () => run(async () => { await api("/auth/logout", "POST"); setSignedIn(false); });
statusButton.onclick = () => run(async () => renderResult((await api("/status")).data));
sessions.onchange = () => { messages.replaceChildren(); syncControls(); };
for (const mode of Object.keys(modes)) {
  byId(mode).onclick = () => run(async () => {
    const { data } = await api("/sessions", "POST", { mode });
    await refreshSessions(data.session_id);
    messages.replaceChildren();
  });
}
chat.onsubmit = (event) => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || send.disabled) return;
  run(async () => {
    message.value = "";
    addMessage(text, "user");
    connection.textContent = "Pensando";
    const { status, data } = await api(`/sessions/${encodeURIComponent(sessions.value)}/chat`, "POST", { text });
    if (status === 202) watchJob(data.job_id);
    else renderResult(data);
  });
};
cancel.onclick = () => run(async () => {
  const tracked = job;
  const { data } = await api(`${tracked.path}/cancel`, "POST");
  if (job === tracked && !finishJob(data)) addMessage("Cancelación solicitada…", "notice");
});

run(async () => { await api("/auth/me"); await refreshSessions(); setSignedIn(true); }, "startup");
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(console.warn);
