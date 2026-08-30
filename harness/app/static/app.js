/* home-harness PWA -- push-to-talk client for phone and laptop. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const log = $("log");
  const mic = $("mic");
  const micLabel = $("mic-label");
  const banner = $("banner");
  const settings = $("settings");

  const store = {
    get url()  { return localStorage.getItem("hh.url") || location.origin; },
    set url(v) { localStorage.setItem("hh.url", v); },
    get key()  { return localStorage.getItem("hh.key") || ""; },
    set key(v) { localStorage.setItem("hh.key", v); },
    get speak()  { return localStorage.getItem("hh.speak") !== "0"; },
    set speak(v) { localStorage.setItem("hh.speak", v ? "1" : "0"); },
    get session() {
      let s = localStorage.getItem("hh.session");
      if (!s) { s = Math.random().toString(36).slice(2, 14); localStorage.setItem("hh.session", s); }
      return s;
    },
    reset() { localStorage.removeItem("hh.session"); },
  };

  /* ---- UI helpers ---------------------------------------------------- */

  function warn(message) {
    banner.textContent = message;
    banner.hidden = !message;
  }

  function bubble(text, kind, meta, tools) {
    $("empty")?.remove();
    const el = document.createElement("div");
    el.className = `msg ${kind}`;
    el.textContent = text;
    if (tools && tools.length) {
      const t = document.createElement("span");
      t.className = "tools";
      t.textContent = tools.map((c) => `${c.ok ? "✓" : "✗"} ${c.name}`).join("  ");
      el.appendChild(t);
    }
    if (meta) {
      const m = document.createElement("span");
      m.className = "meta";
      m.textContent = meta;
      el.appendChild(m);
    }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function spinner() {
    const el = document.createElement("div");
    el.className = "thinking";
    el.innerHTML = "<i></i><i></i><i></i>";
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function headers(extra) {
    const h = Object.assign({}, extra || {});
    if (store.key) h["X-API-Key"] = store.key;
    return h;
  }

  async function play(base64, mime) {
    if (!base64) return;
    const audio = new Audio(`data:${mime || "audio/mpeg"};base64,${base64}`);
    try {
      await audio.play();
    } catch (err) {
      // iOS blocks playback that is not tied to a gesture; the text is still there.
      console.warn("autoplay blocked:", err);
    }
  }

  function describe(data) {
    const bits = [];
    if (data.model) bits.push(data.model);
    if (data.elapsed_ms) bits.push(`${(data.elapsed_ms / 1000).toFixed(1)}s`);
    return bits.join(" · ");
  }

  /* ---- talking to the harness ---------------------------------------- */

  async function send(path, init) {
    const resp = await fetch(`${store.url}${path}`, init);
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch (_) { /* not JSON */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  async function sendText(message) {
    bubble(message, "user");
    const busy = spinner();
    try {
      const data = await send("/v1/chat", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          message,
          session_id: store.session,
          speak: store.speak,
        }),
      });
      busy.remove();
      bubble(data.text || "(no reply)", "bot", describe(data), data.tool_calls);
      if (store.speak) play(data.audio, data.audio_mime);
    } catch (err) {
      busy.remove();
      bubble(err.message, "err");
    }
  }

  async function sendAudio(blob, filename) {
    const busy = spinner();
    const form = new FormData();
    form.append("audio", blob, filename);
    form.append("session_id", store.session);
    form.append("speak", String(store.speak));
    try {
      const data = await send("/v1/voice", { method: "POST", headers: headers(), body: form });
      busy.remove();
      if (data.transcript) bubble(data.transcript, "user");
      bubble(data.text || "(no reply)", "bot", describe(data), data.tool_calls);
      if (data.tts_error) warn(`Voice reply unavailable: ${data.tts_error}`);
      play(data.audio, data.audio_mime);
    } catch (err) {
      busy.remove();
      bubble(err.message, "err");
    }
  }

  /* ---- recording ------------------------------------------------------ */

  let recorder = null;
  let chunks = [];
  let stream = null;
  let armed = false;

  function pickMimeType() {
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",   // Safari, including iOS
    ];
    return candidates.find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || "";
  }

  async function startRecording() {
    if (recorder) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      warn(
        window.isSecureContext
          ? "This browser has no microphone API."
          : "Microphone needs HTTPS. Open the site over its https:// address."
      );
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      warn(`Microphone unavailable: ${err.message}`);
      return;
    }

    const mimeType = pickMimeType();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
      const type = recorder.mimeType || mimeType || "audio/webm";
      recorder = null;
      const blob = new Blob(chunks, { type });
      // Anything this short is a mis-tap, not speech.
      if (blob.size < 2000) { warn("That was too short to hear. Hold the button while you speak."); return; }
      warn("");
      const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      sendAudio(blob, `speech.${ext}`);
    };
    recorder.start();
    mic.classList.add("recording");
    micLabel.textContent = "Listening… release to send";
  }

  function stopRecording() {
    mic.classList.remove("recording");
    micLabel.textContent = "Hold to talk";
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  /* ---- events --------------------------------------------------------- */

  // Pointer events cover mouse, touch and pen with one path.
  mic.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    armed = true;
    mic.setPointerCapture?.(e.pointerId);
    startRecording();
  });
  const release = () => { if (armed) { armed = false; stopRecording(); } };
  mic.addEventListener("pointerup", release);
  mic.addEventListener("pointercancel", release);
  mic.addEventListener("contextmenu", (e) => e.preventDefault());

  // Space bar is push-to-talk on a laptop, as long as you are not typing.
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || e.target.tagName === "INPUT") return;
    e.preventDefault();
    armed = true;
    startRecording();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && armed) { e.preventDefault(); release(); }
  });

  $("text-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("text-input");
    const value = input.value.trim();
    if (!value) return;
    input.value = "";
    sendText(value);
  });

  $("settings-btn").addEventListener("click", () => {
    $("cfg-url").value = store.url;
    $("cfg-key").value = store.key;
    $("cfg-speak").checked = store.speak;
    $("cfg-session").textContent = store.session;
    settings.showModal();
  });

  settings.addEventListener("close", () => {
    if (settings.returnValue === "reset") {
      store.reset();
      log.innerHTML = "";
      bubble("Started a new conversation.", "bot");
      return;
    }
    if (settings.returnValue !== "save") return;
    store.url = $("cfg-url").value.trim().replace(/\/$/, "");
    store.key = $("cfg-key").value.trim();
    store.speak = $("cfg-speak").checked;
    checkHealth();
  });

  /* ---- startup -------------------------------------------------------- */

  async function checkHealth() {
    try {
      const data = await send("/health", { headers: headers() });
      const missing = [];
      if (!data.tools?.some((t) => t.startsWith("ha_"))) missing.push("Home Assistant");
      if (!data.tools?.some((t) => t.startsWith("calendar_"))) missing.push("Calendar");
      warn(missing.length ? `Not configured: ${missing.join(", ")}` : "");
    } catch (err) {
      warn(`Cannot reach the harness: ${err.message}`);
    }
  }

  if (!window.isSecureContext) {
    warn("Not a secure context — the microphone will not work. Use the https:// address.");
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => { /* offline shell is optional */ });
  }
  checkHealth();
})();
