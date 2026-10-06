const controls = document.getElementById("controls");
const transcriptEl = document.getElementById("transcript");
const sentimentValueEl = document.getElementById("sentiment-value");
const sentimentFillEl = document.getElementById("sentiment-fill");
const themesEl = document.getElementById("themes");
const statusAreaEl = document.getElementById("status-area");
const suggestionAreaEl = document.getElementById("suggestion-area");
const logEl = document.getElementById("log");
const answerEl = document.getElementById("answer");
const quickEl = document.getElementById("quick");
const modeBadgeEl = document.getElementById("mode-badge");
const latencyPanelEl = document.getElementById("latency-panel");
const latencyRowsEl = document.getElementById("latency-rows");

function clearChildren(el) { el.innerHTML = ""; }

function logLine(evt) {
  const div = document.createElement("div");
  div.textContent = `[${evt.sequence_number ?? "-"}] ${evt.event_type}  ${JSON.stringify(evt.payload || {})}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function resetPanels() {
  clearChildren(transcriptEl);
  sentimentValueEl.textContent = "–";
  sentimentFillEl.style.width = "50%";
  sentimentFillEl.style.background = "var(--muted)";
  clearChildren(themesEl);
  const empty = document.createElement("span");
  empty.className = "empty";
  empty.textContent = "No themes yet.";
  themesEl.appendChild(empty);
  statusAreaEl.innerHTML = '<span class="empty">No decision yet.</span>';
  suggestionAreaEl.innerHTML = "";
  logEl.innerHTML = "";
  latencyRowsEl.innerHTML = '<span class="empty">No cloud calls yet.</span>';
}

function appendBubble(channel, text) {
  if (transcriptEl.querySelector(".empty")) clearChildren(transcriptEl);
  const b = document.createElement("div");
  b.className = `bubble ${channel}`;
  const ch = document.createElement("span");
  ch.className = "ch";
  ch.textContent = channel;
  b.appendChild(ch);
  b.appendChild(document.createTextNode(text));
  transcriptEl.appendChild(b);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function updateSentiment(rolling) {
  sentimentValueEl.textContent = (rolling >= 0 ? "+" : "") + rolling.toFixed(2);
  const pct = Math.round(((rolling + 1) / 2) * 100);
  sentimentFillEl.style.width = pct + "%";
  sentimentFillEl.style.background = rolling <= -0.4 ? "var(--bad)" : rolling >= 0.3 ? "var(--ok)" : "var(--warn)";
}

function updateThemes(active) {
  clearChildren(themesEl);
  if (!active.length) {
    const empty = document.createElement("span");
    empty.className = "empty";
    empty.textContent = "No themes yet.";
    themesEl.appendChild(empty);
    return;
  }
  for (const t of active) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = t;
    themesEl.appendChild(chip);
  }
}

function updateDecision(payload) {
  const outcome = payload.outcome;
  statusAreaEl.innerHTML = "";
  const banner = document.createElement("div");
  banner.className = `status-banner status-${outcome}`;
  banner.textContent = outcome.replace(/_/g, " ").toUpperCase();
  statusAreaEl.appendChild(banner);
  const rules = document.createElement("div");
  rules.className = "citelist";
  rules.textContent = `policy: ${(payload.policy_decisions || []).join(", ") || "(none fired)"}`;
  statusAreaEl.appendChild(rules);
  if (payload.degraded && payload.degraded.length) {
    const deg = document.createElement("div");
    deg.className = "citelist";
    deg.textContent = `degraded: ${payload.degraded.join(", ")}`;
    statusAreaEl.appendChild(deg);
  }
}

function showSuggestion(payload) {
  suggestionAreaEl.innerHTML = "";
  const badge = document.createElement("span");
  badge.className = `gen-badge gen-${payload.generated_by}`;
  badge.textContent = payload.generated_by === "llm" ? "narrator" : "template fallback";
  suggestionAreaEl.appendChild(badge);

  const text = document.createElement("div");
  text.className = "suggestion-text";
  text.textContent = payload.explanation;
  suggestionAreaEl.appendChild(text);

  const cites = document.createElement("div");
  cites.className = "citelist";
  cites.textContent = `cites: ${(payload.cited_document_ids || []).join(", ") || "none"}`;
  suggestionAreaEl.appendChild(cites);

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `evidence (${(payload.evidence || []).length})`;
  details.appendChild(summary);
  for (const e of payload.evidence || []) {
    const row = document.createElement("div");
    row.className = "evidence-row";
    row.textContent = `${e.kind} → ${e.source_system}:${e.source_id}${e.source_version ? "@" + e.source_version : ""} ${e.detail || ""}`;
    details.appendChild(row);
  }
  suggestionAreaEl.appendChild(details);
}

const LATENCY_SCALE_MS = 12000;   // bar width reference -- narrator calls run several seconds

function upsertLatencyRow(label, ms, detail) {
  if (latencyRowsEl.querySelector(".empty")) clearChildren(latencyRowsEl);
  let row = latencyRowsEl.querySelector(`[data-stage="${label}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "latency-row";
    row.dataset.stage = label;
    row.innerHTML = `<div><div>${label}</div><div class="latency-bar-track"><div class="latency-bar-fill" style="width:0%"></div></div></div><div class="latency-ms"></div>`;
    latencyRowsEl.appendChild(row);
  }
  const pct = Math.min(100, Math.round((ms / LATENCY_SCALE_MS) * 100));
  row.querySelector(".latency-bar-fill").style.width = pct + "%";
  row.querySelector(".latency-ms").textContent = detail || `${ms.toFixed(0)} ms`;
}

function showSpanLatencies(spans) {
  if (!spans || !spans.length) return;
  latencyPanelEl.style.display = "block";
  for (const s of spans) {
    if (s.duration_ms > 1) upsertLatencyRow(s.name, s.duration_ms);
  }
}

function showPostcall(payload) {
  const banner = document.createElement("div");
  banner.className = "citelist";
  banner.style.marginTop = "10px";
  banner.textContent = `post-call record saved — ${payload.summary}`;
  statusAreaEl.appendChild(banner);
}

async function runScript(scriptId, buttons) {
  buttons.forEach((b) => (b.disabled = true));
  resetPanels();
  try {
    const runResp = await fetch(`/api/run/${scriptId}`, { method: "POST" });
    const { call_id } = await runResp.json();
    const streamResp = await fetch(`/api/calls/${call_id}/stream`);
    const reader = streamResp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const evt = JSON.parse(line);
        logLine(evt);
        dispatch(evt);
      }
    }
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function dispatch(evt) {
  const p = evt.payload || {};
  if (typeof p._eh_publish_ms === "number") {
    latencyPanelEl.style.display = "block";
    upsertLatencyRow("Event Hub publish", p._eh_publish_ms);
  }
  if (typeof p._redis_ms === "number") {
    latencyPanelEl.style.display = "block";
    upsertLatencyRow("Redis round trip", p._redis_ms);
  }
  switch (evt.event_type) {
    case "transcript.utterance_final":
      appendBubble(p.channel, p.text);
      break;
    case "sentiment.updated":
      updateSentiment(p.rolling_sentiment);
      break;
    case "theme.detected":
      updateThemes(p.active_themes || []);
      break;
    case "commercial.decision_made":
      updateDecision(p);
      showSpanLatencies(p.spans);
      break;
    case "copilot.suggestion_generated":
      showSuggestion(p);
      break;
    case "postcall.enrichment_completed":
      showPostcall(p);
      break;
    default:
      break;
  }
}

async function loadScripts() {
  const resp = await fetch("/api/scripts");
  const scripts = await resp.json();
  clearChildren(controls);
  const buttons = [];
  for (const s of scripts) {
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = `Run: ${s.title}`;
    buttons.push(btn);
    btn.addEventListener("click", () => runScript(s.id, buttons));
    controls.appendChild(btn);
  }
}

const QUICK_QUESTIONS = [
  "how many calls were suppressed?",
  "what's the average sentiment?",
  "what can I say about Savings Plus?",
];

function setupAssistant() {
  for (const q of QUICK_QUESTIONS) {
    const btn = document.createElement("button");
    btn.textContent = q;
    btn.addEventListener("click", () => askAssistant(q));
    quickEl.appendChild(btn);
  }
  document.getElementById("ask-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = document.getElementById("question");
    if (input.value.trim()) askAssistant(input.value.trim());
  });
}

async function askAssistant(question) {
  document.getElementById("question").value = question;
  answerEl.innerHTML = '<span class="empty">Thinking&hellip;</span>';
  const resp = await fetch("/api/assistant/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  const data = await resp.json();
  answerEl.innerHTML = "";
  const text = document.createElement("div");
  text.textContent = data.answer;
  answerEl.appendChild(text);
  if (data.citations && data.citations.length) {
    const cites = document.createElement("div");
    cites.className = "citelist";
    cites.textContent = `source: ${data.source} · cites: ${data.citations.join(", ")}`;
    answerEl.appendChild(cites);
  }
  const note = document.createElement("div");
  note.className = "note";
  note.textContent = data.note;
  answerEl.appendChild(note);
}

async function loadMode() {
  try {
    const resp = await fetch("/api/mode");
    const data = await resp.json();
    if (data.mode === "azure-live") {
      modeBadgeEl.textContent = "LIVE AZURE -- real Event Hubs/Redis/AI Search/OpenAI";
      modeBadgeEl.style.background = "color-mix(in srgb, var(--ok) 20%, transparent)";
      modeBadgeEl.style.color = "var(--ok)";
      latencyPanelEl.style.display = "block";
    } else {
      modeBadgeEl.textContent = "local demo (no cloud calls)";
    }
  } catch {
    modeBadgeEl.textContent = "local demo (no cloud calls)";
  }
}

loadMode();
loadScripts();
setupAssistant();
