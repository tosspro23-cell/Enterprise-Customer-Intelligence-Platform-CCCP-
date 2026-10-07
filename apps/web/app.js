const controls = document.getElementById("controls");
const transcriptEl = document.getElementById("transcript");
const sentimentValueEl = document.getElementById("sentiment-value");
const sentimentFillEl = document.getElementById("sentiment-fill");
const themesEl = document.getElementById("themes");
const statusAreaEl = document.getElementById("status-area");
const suggestionAreaEl = document.getElementById("suggestion-area");
const traceEl = document.getElementById("trace");
const logEl = document.getElementById("log");
const answerEl = document.getElementById("answer");
const quickEl = document.getElementById("quick");
const modeBadgeEl = document.getElementById("mode-badge");
const playbackBarEl = document.getElementById("playback-bar");
const pbToggleEl = document.getElementById("pb-toggle");
const pbNextEl = document.getElementById("pb-next");
const pbStatusEl = document.getElementById("pb-status");
const traceCurrentEl = document.getElementById("trace-current");
const traceCurrentTextEl = document.getElementById("trace-current-text");

// What each pipeline stage is, which system backs it, and -- just as
// important -- whether that system call is real (hits Azure) or a local
// stand-in (synthetic data, no network call). Mirrors docs/architecture.md
// and README.md; the point of writing it here too is that someone watching
// a call run shouldn't have to go read those to understand what they're
// looking at.
const STAGE_CATALOG = {
  stt: { code: "STT", label: "Speech-to-Text", system: "Azure AI Speech", source: "cloud",
    business: "Turns the customer's spoken words into text the system can act on.",
    technical: "Real-time streaming STT (PushAudioInputStream). Audio is synthesised by Azure TTS for this demo -- there is no live phone call behind it." },
  sentiment: { code: "SENT", label: "Sentiment Scoring", system: "Azure AI Language", source: "cloud",
    business: "Scores how positive or negative the customer sounds, right now.",
    technical: "analyze_sentiment API; positive_confidence minus negative_confidence, mapped to [-1, 1]." },
  theme: { code: "THEME", label: "Theme Tagging", system: "keyword match", source: "local",
    business: "Flags which topic the customer is discussing (fees, savings, etc.).",
    technical: "Keyword match against the controlled taxonomy. NOT a trained classifier in this build -- production would use one (docs/architecture.md §9.9)." },
  redis: { code: "STATE", label: "Hot Call State", system: "Azure Managed Redis", source: "cloud",
    business: "Remembers this call's mood and topics while it's still in progress.",
    technical: "Redis HSET/RPUSH per utterance, 24h TTL, EnterpriseCluster policy." },
  gate: { code: "GATE", label: "Policy Gate", system: "deterministic rules", source: "local",
    business: "Checks hard business rules BEFORE any product is even considered -- e.g. never sell to a customer with an open complaint.",
    technical: "commercial-policy-v1.0; customer gates R0-R3 run before scoring, most-restrictive-wins. Customer/interaction data is a local synthetic fixture, not a real warehouse." },
  ml: { code: "ML", label: "Propensity Scoring", system: "existing ML model", source: "local",
    business: "Asks the existing prediction model which product this customer is likely to want.",
    technical: "PredictiveModelPort.score_products(); fixed feature schema, no live-call features injected. Synthetic fixture data in this build, not a real model endpoint." },
  search: { code: "SEARCH", label: "Guidance Retrieval", system: "Azure AI Search", source: "cloud",
    business: "Finds the company-approved script for recommending this product.",
    technical: "Hybrid search filtered by product + situation + active version." },
  narrator: { code: "LLM", label: "Narrator", system: "Azure OpenAI", source: "cloud",
    business: "Turns the decision into a sentence the agent can actually say to the customer.",
    technical: "LLM call; output validated against grounded context (citations, numbers, product match) before use; template fallback on any violation." },
  evidence: { code: "EVID", label: "Evidence Assembly", system: "", source: "local",
    business: "Collects the proof behind every claim -- which model, which rule, which guidance version -- so the recommendation can be audited.",
    technical: "Evidence objects attached to the result; see the expandable list on the recommendation above." },
};

function clearChildren(el) { el.innerHTML = ""; }
function emptySpan(text) { const s = document.createElement("span"); s.className = "empty"; s.textContent = text; return s; }

function logLine(evt) {
  const div = document.createElement("div");
  div.textContent = `[${evt.sequence_number ?? "-"}] ${evt.event_type}  ${JSON.stringify(evt.payload || {})}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

let traceRowsById = {};

function resetPanels() {
  resetPlayback();
  clearChildren(transcriptEl);
  transcriptEl.appendChild(emptySpan("Run a call to see it here."));
  sentimentValueEl.textContent = "–";
  sentimentFillEl.style.width = "50%";
  sentimentFillEl.style.background = "var(--muted)";
  clearChildren(themesEl);
  themesEl.appendChild(emptySpan("No themes yet."));
  statusAreaEl.innerHTML = "";
  statusAreaEl.appendChild(emptySpan("No decision yet."));
  suggestionAreaEl.innerHTML = "";
  logEl.innerHTML = "";
  clearChildren(traceEl);
  traceEl.appendChild(emptySpan("Run a call against LIVE AZURE to see every stage here -- this fills in automatically, nothing to switch on."));
  traceRowsById = {};
  currentGroupBody = null;
  currentRunningRow = null;
  setCurrentStep("Not running", true);
}

function showTypingIndicator() {
  if (transcriptEl.querySelector(".bubble.typing")) return;
  if (transcriptEl.querySelector(".empty")) clearChildren(transcriptEl);
  const b = document.createElement("div");
  b.className = "bubble typing";
  b.innerHTML = '<span class="ch">working</span><span class="dots"><span>&#9679;</span><span>&#9679;</span><span>&#9679;</span></span>';
  transcriptEl.appendChild(b);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}
function hideTypingIndicator() {
  const b = transcriptEl.querySelector(".bubble.typing");
  if (b) b.remove();
}

function appendBubble(channel, text, sttDetail) {
  hideTypingIndicator();
  if (transcriptEl.querySelector(".empty")) clearChildren(transcriptEl);
  const b = document.createElement("div");
  b.className = `bubble ${channel}`;
  const ch = document.createElement("span");
  ch.className = "ch";
  ch.textContent = channel;
  b.appendChild(ch);
  b.appendChild(document.createTextNode(text));
  if (sttDetail && sttDetail.recognized_text && sttDetail.recognized_text !== text) {
    const diff = document.createElement("span");
    diff.className = "stt-diff";
    diff.textContent = `Azure STT heard: "${sttDetail.recognized_text}"`;
    b.appendChild(diff);
  }
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
  if (!active.length) return themesEl.appendChild(emptySpan("No themes yet."));
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

function showPostcall(payload) {
  const banner = document.createElement("div");
  banner.className = "citelist";
  banner.style.marginTop = "10px";
  banner.textContent = `post-call record saved — ${payload.summary}`;
  statusAreaEl.appendChild(banner);
}

const TRACE_SCALE_MS = 10000; // bar width reference -- the narrator stage runs several seconds
let currentGroupBody = null;   // where new stage rows get appended -- set by handleGroup()
let currentRunningRow = null;  // the one row currently highlighted as "happening now"

// A group is purely organisational (which conversation turn, or "the
// decision" a trigger kicked off) -- it's never a real fact about the call,
// so like the "running" stage marker it's browser-only, not published.
function handleGroup(p) {
  if (traceEl.querySelector(".empty")) clearChildren(traceEl);
  const group = document.createElement("div");
  group.className = `trace-group kind-${p.kind}`;
  group.innerHTML = `<div class="trace-group-header"><span class="trace-group-dot"></span>${p.label}</div>
    <div class="trace-group-body"></div>`;
  traceEl.appendChild(group);
  currentGroupBody = group.querySelector(".trace-group-body");
  traceEl.scrollTop = traceEl.scrollHeight;
}

// Most `detail` payloads are now {label: [readable strings]} on purpose
// (built server-side from real evidence/scores/policy decisions) -- rendered
// as a plain bullet list so "what did AI Search actually match" is a glance,
// not a JSON blob to parse. Anything that doesn't fit that shape still
// degrades to raw JSON further down.
function renderDetailList(container, detail) {
  let any = false;
  for (const [key, val] of Object.entries(detail)) {
    if (!Array.isArray(val)) continue;
    any = true;
    const wrap = document.createElement("div");
    const label = document.createElement("div");
    label.style.cssText = "font-size:11px;color:var(--muted);margin-top:6px;text-transform:uppercase;letter-spacing:.02em;";
    label.textContent = key.replace(/_/g, " ");
    wrap.appendChild(label);
    const ul = document.createElement("ul");
    ul.className = "trace-detail-list";
    for (const item of val) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
    container.appendChild(wrap);
  }
  return any;
}

function setCurrentStep(text, idle) {
  traceCurrentTextEl.textContent = text;
  traceCurrentEl.classList.toggle("idle", !!idle);
}

function handleStage(p) {
  const meta = STAGE_CATALOG[p.stage] || { code: p.stage.slice(0, 5).toUpperCase(), label: p.stage, system: "", source: "local", business: "", technical: "" };
  let entry = traceRowsById[p.instance_id];
  if (!entry) {
    const row = document.createElement("div");
    row.className = `trace-row source-${meta.source}`;
    row.innerHTML = `
      <div class="trace-row-main">
        <span class="trace-code">${meta.code}</span>
        <div class="trace-row-text">
          <div class="trace-label">${meta.label}<span class="trace-system">${meta.system}</span></div>
          <div class="trace-business">${meta.business}</div>
        </div>
        <span class="trace-status trace-status-running">running</span>
        <span class="trace-ms"></span>
      </div>
      <div class="trace-bar-track"><div class="trace-bar-fill" style="width:0%"></div></div>
      <div class="trace-detail-rendered"></div>
      <details>
        <summary>technical detail</summary>
        <div class="trace-technical">${meta.technical}</div>
        <div class="trace-eh-note"></div>
        <pre class="trace-raw" style="display:none"></pre>
      </details>`;
    (currentGroupBody || traceEl).appendChild(row);
    entry = { el: row };
    traceRowsById[p.instance_id] = entry;
  }
  const row = entry.el;
  if (p.status === "running") {
    if (currentRunningRow) currentRunningRow.classList.remove("is-current");
    row.classList.add("is-current");
    currentRunningRow = row;
    setCurrentStep(`${meta.label} — ${meta.system || "local"}`);
  } else if (p.status === "done") {
    row.classList.remove("is-current");
    if (currentRunningRow === row) { currentRunningRow = null; setCurrentStep("Idle, waiting for next step", true); }
    row.querySelector(".trace-status").className = "trace-status trace-status-done";
    row.querySelector(".trace-status").textContent = "done";
    const ms = typeof p.ms === "number" ? p.ms : 0;
    row.querySelector(".trace-ms").textContent = `${ms.toFixed(0)} ms`;
    row.querySelector(".trace-bar-fill").style.width = Math.min(100, Math.round((ms / TRACE_SCALE_MS) * 100)) + "%";
    if (p.detail && Object.keys(p.detail).length) {
      const rendered = row.querySelector(".trace-detail-rendered");
      const hadList = renderDetailList(rendered, p.detail);
      const leftover = Object.fromEntries(Object.entries(p.detail).filter(([, v]) => !Array.isArray(v)));
      if (!hadList || Object.keys(leftover).length) {
        const raw = row.querySelector(".trace-raw");
        raw.style.display = "block";
        raw.textContent = JSON.stringify(hadList ? leftover : p.detail, null, 2);
      }
    }
  } else if (p.status === "error") {
    row.classList.remove("is-current");
    if (currentRunningRow === row) { currentRunningRow = null; setCurrentStep("Failed -- see detail below", true); }
    row.querySelector(".trace-status").className = "trace-status trace-status-error";
    row.querySelector(".trace-status").textContent = "failed";
    if (p.detail && p.detail.error) {
      const raw = row.querySelector(".trace-raw");
      raw.style.display = "block";
      raw.textContent = p.detail.error;
    }
  }
  if (typeof p._eh_publish_ms === "number") {
    row.querySelector(".trace-eh-note").textContent = `+ published to Event Hubs in ${p._eh_publish_ms.toFixed(0)} ms`;
  }
  traceEl.scrollTop = traceEl.scrollHeight;
}

// -------------------------------------------------------------- paced reveal
//
// The backend streams events the instant they're real (an STT call might
// take 6s, a narrator call 9s, a Redis round trip 500ms) -- piping that
// straight into the UI makes the conversation feel like it's stuttering:
// long dead air, then a burst, at a pace that tracks Azure's response time
// instead of a human one. This decouples the two: the real backend still
// runs (and still produces real timing, shown on each trace row once it
// reveals), but what's actually DISPLAYED is drained from a queue at a
// steady, human pace -- with a visible "still working" gap when the
// backend is genuinely still going, instead of either freezing or lying
// about how long something took.
const PACE_MS = { "transcript.utterance_final": 1100, "pipeline.stage": 550 };
const DEFAULT_PACE_MS = 450;

let revealQueue = [];
let revealTimer = null;
let streamDone = false;
let autoPlay = true;
let onFullyRevealed = null;

function resetPlayback() {
  revealQueue = [];
  streamDone = false;
  autoPlay = true;
  clearTimeout(revealTimer);
  revealTimer = null;
  onFullyRevealed = null;
  hideTypingIndicator();
  playbackBarEl.style.display = "none";
  pbToggleEl.textContent = "⏸ Pause";
  pbStatusEl.textContent = "";
}

function updatePlaybackStatus() {
  pbNextEl.disabled = revealQueue.length === 0;
  pbStatusEl.textContent = revealQueue.length
    ? `${revealQueue.length} step${revealQueue.length > 1 ? "s" : ""} queued`
    : streamDone ? "finished" : "backend still working…";
}

function enqueueEvent(evt) {
  revealQueue.push(evt);
  playbackBarEl.style.display = "flex";
  updatePlaybackStatus();
  if (autoPlay && !revealTimer) revealTimer = setTimeout(revealNext, 1);
}

function revealNext() {
  revealTimer = null;
  hideTypingIndicator();
  const evt = revealQueue.shift();
  if (evt) {
    logLine(evt);
    dispatch(evt);
  }
  updatePlaybackStatus();
  if (!revealQueue.length && streamDone) {
    onFullyRevealed?.();
    return;
  }
  if (autoPlay) {
    if (revealQueue.length) {
      revealTimer = setTimeout(revealNext, PACE_MS[evt?.event_type] ?? DEFAULT_PACE_MS);
    } else {
      showTypingIndicator();  // backend's still going; say so instead of looking stuck
    }
  }
}

pbToggleEl.addEventListener("click", () => {
  autoPlay = !autoPlay;
  pbToggleEl.textContent = autoPlay ? "⏸ Pause" : "▶ Play";
  if (autoPlay) {
    hideTypingIndicator();
    if (revealQueue.length && !revealTimer) revealTimer = setTimeout(revealNext, 1);
  } else {
    clearTimeout(revealTimer);
    revealTimer = null;
  }
});
pbNextEl.addEventListener("click", () => {
  clearTimeout(revealTimer);
  revealTimer = null;
  revealNext();
});

async function runScript(scriptId, buttons, activeCard) {
  buttons.forEach((b) => (b.disabled = true));
  if (activeCard) activeCard.classList.add("running");
  resetPanels();
  // The backend's first real call (an Event Hubs connection, then the first
  // Speech round trip) can take well over 10s before anything is enqueued --
  // without this, the whole page just sits there looking broken for that
  // stretch. Show that something is happening from the first click, not
  // from the first event.
  playbackBarEl.style.display = "flex";
  pbStatusEl.textContent = "starting call…";
  showTypingIndicator();
  const revealed = new Promise((resolve) => { onFullyRevealed = resolve; });
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
        enqueueEvent(JSON.parse(line));
      }
    }
    streamDone = true;
    updatePlaybackStatus();
    if (!revealQueue.length) onFullyRevealed?.();
    await revealed;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    if (activeCard) activeCard.classList.remove("running");
  }
}

function dispatch(evt) {
  const p = evt.payload || {};
  if (evt.event_type === "pipeline.group") {
    handleGroup(p);
    return;
  }
  if (evt.event_type === "pipeline.stage") {
    handleStage(p);
    return;
  }
  if (evt.event_type === "pipeline.audio") {
    const entry = traceRowsById[p.instance_id];
    if (entry && p.audio_base64) {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.style.cssText = "width:100%;height:28px;margin-top:6px;";
      audio.src = `data:audio/wav;base64,${p.audio_base64}`;
      entry.el.querySelector(".trace-row-main").insertAdjacentElement("afterend", audio);
    }
    return;
  }
  if (evt.event_type === "pipeline.error") {
    statusAreaEl.innerHTML = "";
    const banner = document.createElement("div");
    banner.className = "status-banner status-unavailable";
    banner.textContent = "PIPELINE ERROR";
    statusAreaEl.appendChild(banner);
    const detail = document.createElement("div");
    detail.className = "citelist";
    detail.textContent = p.error;
    statusAreaEl.appendChild(detail);
    return;
  }
  switch (evt.event_type) {
    case "transcript.utterance_final":
      appendBubble(p.channel, p.text, p._stt);
      break;
    case "sentiment.updated":
      updateSentiment(p.rolling_sentiment);
      break;
    case "theme.detected":
      updateThemes(p.active_themes || []);
      break;
    case "commercial.decision_made":
      updateDecision(p);
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

// Same categories the eval suite (evals/cases.json) already uses to group
// scenarios, so the label on a card means the same thing it means there.
const SCENARIO_CATEGORY = {
  golden_savings: { tag: "DECISION", color: "var(--accent)" },
  already_held_switch: { tag: "DECISION", color: "var(--accent)" },
  region_restriction: { tag: "DECISION", color: "var(--accent)" },
  recent_decline_cooldown: { tag: "DECISION", color: "var(--accent)" },
  suppressed_complaint: { tag: "SAFETY", color: "var(--bad)" },
  deteriorating_trend: { tag: "SAFETY", color: "var(--bad)" },
  vulnerable_handoff: { tag: "SAFETY", color: "var(--handoff)" },
  below_threshold: { tag: "ROBUSTNESS", color: "var(--warn)" },
  no_history: { tag: "ANALYSIS", color: "var(--local)" },
  injection_safety: { tag: "SECURITY", color: "var(--handoff)" },
  unknown_and_missing_guidance: { tag: "GOVERNANCE", color: "var(--ok)" },
};

async function loadScripts() {
  const resp = await fetch("/api/scripts");
  const scripts = await resp.json();
  clearChildren(controls);
  const rows = [];
  for (const s of scripts) {
    const cat = SCENARIO_CATEGORY[s.id] || { tag: "SCENARIO", color: "var(--muted)" };
    const row = document.createElement("button");
    row.className = "scenario-row";
    row.title = s.title;  // full text on hover via native tooltip too, for a long sidebar list
    row.innerHTML = `
      <span class="scenario-tag" style="background:${cat.color}">${cat.tag}</span>
      <div class="scenario-row-text">
        <span class="scenario-customer">${s.customer_id || ""}</span>
        <div class="scenario-title">${s.title}</div>
      </div>`;
    rows.push(row);
    row.addEventListener("click", () => runScript(s.id, rows, row));
    controls.appendChild(row);
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
      modeBadgeEl.textContent = "LIVE AZURE -- real Speech/Language/Event Hubs/Redis/AI Search/OpenAI";
      modeBadgeEl.style.background = "color-mix(in srgb, var(--ok) 20%, transparent)";
      modeBadgeEl.style.color = "var(--ok)";
    } else {
      modeBadgeEl.textContent = "local demo (no cloud calls)";
    }
  } catch {
    modeBadgeEl.textContent = "local demo (no cloud calls)";
  }
}

// -------------------------------------------------------------- view switch
const viewAgentEl = document.getElementById("view-agent");
const viewSupervisorEl = document.getElementById("view-supervisor");
const tabAgentEl = document.getElementById("tab-agent");
const tabSupervisorEl = document.getElementById("tab-supervisor");

function switchView(view) {
  const toSupervisor = view === "supervisor";
  viewAgentEl.style.display = toSupervisor ? "none" : "";
  viewSupervisorEl.style.display = toSupervisor ? "" : "none";
  tabAgentEl.classList.toggle("active", !toSupervisor);
  tabSupervisorEl.classList.toggle("active", toSupervisor);
  if (toSupervisor) loadSupervisor();
}
tabAgentEl.addEventListener("click", () => switchView("agent"));
tabSupervisorEl.addEventListener("click", () => switchView("supervisor"));

// -------------------------------------------------------------- supervisor view
const svSummaryEl = document.getElementById("sv-summary");
const svCallsEl = document.getElementById("sv-calls");
const svDetailEl = document.getElementById("sv-detail");
const svDetailHintEl = document.getElementById("sv-detail-hint");

const OUTCOME_COLOR = {
  recommended: "var(--ok)", deferred: "var(--warn)", suppressed: "var(--bad)",
  specialist_handoff: "var(--handoff)", no_recommendation: "var(--muted)",
  unavailable: "var(--muted)", no_decision: "var(--muted)",
};

async function loadSupervisor() {
  const [summary, calls] = await Promise.all([
    fetch("/api/supervisor/summary").then((r) => r.json()),
    fetch("/api/supervisor/calls").then((r) => r.json()),
  ]);
  renderSvSummary(summary);
  renderSvCalls(calls);
}

function renderSvSummary(s) {
  svSummaryEl.innerHTML = "";
  const stat = (label, value, sub) => {
    const el = document.createElement("div");
    el.className = "sv-stat";
    el.innerHTML = `<div class="sv-stat-label">${label}</div><div class="sv-stat-value">${value}</div>${sub ? `<div class="sv-stat-sub">${sub}</div>` : ""}`;
    return el;
  };
  svSummaryEl.appendChild(stat("Total calls", s.total_calls ?? 0));
  const avg = s.avg_final_sentiment;
  svSummaryEl.appendChild(stat("Avg. final sentiment", avg == null ? "–" : (avg >= 0 ? "+" : "") + avg.toFixed(2)));
  const gen = s.generated_by_counts || {};
  const genTotal = (gen.llm || 0) + (gen.template || 0);
  const catchRate = genTotal ? Math.round(((gen.template || 0) / genTotal) * 100) : null;
  svSummaryEl.appendChild(stat("Narrator used real LLM", genTotal ? `${gen.llm || 0}/${genTotal}` : "–",
    catchRate !== null ? `${catchRate}% fell back to template` : ""));
  const outcomeEl = stat("Outcome mix", Object.values(s.outcome_counts || {}).reduce((a, b) => a + b, 0) || 0);
  const bar = document.createElement("div");
  bar.className = "sv-outcome-bar";
  const total = Object.values(s.outcome_counts || {}).reduce((a, b) => a + b, 0) || 1;
  for (const [outcome, n] of Object.entries(s.outcome_counts || {})) {
    const seg = document.createElement("span");
    seg.style.width = `${(n / total) * 100}%`;
    seg.style.background = OUTCOME_COLOR[outcome] || "var(--muted)";
    seg.title = `${outcome}: ${n}`;
    bar.appendChild(seg);
  }
  outcomeEl.appendChild(bar);
  svSummaryEl.appendChild(outcomeEl);
}

function renderSvCalls(calls) {
  clearChildren(svCallsEl);
  if (!calls.length) return svCallsEl.appendChild(emptySpan("No calls recorded yet -- run a scenario in Agent view first."));
  for (const c of calls) {
    const row = document.createElement("div");
    row.className = "sv-call-row";
    const outcomeBadge = `<span class="sv-call-outcome" style="background:${OUTCOME_COLOR[c.outcome] || "var(--muted)"}">${c.outcome.replace(/_/g, " ")}</span>`;
    row.innerHTML = `
      ${outcomeBadge}
      <span class="sv-call-meta">${c.scenario_id || c.call_id} · ${c.customer_id}${c.product_id ? " → " + c.product_id : ""}</span>
      <span class="sv-call-time">${(c.ended_at || "").replace("T", " ").slice(0, 19)}</span>`;
    row.addEventListener("click", () => {
      svCallsEl.querySelectorAll(".sv-call-row.selected").forEach((r) => r.classList.remove("selected"));
      row.classList.add("selected");
      loadSvDetail(c.call_id);
    });
    svCallsEl.appendChild(row);
  }
}

async function loadSvDetail(callId) {
  svDetailHintEl.textContent = callId;
  svDetailEl.innerHTML = '<span class="empty">Loading&hellip;</span>';
  const resp = await fetch(`/api/supervisor/calls/${callId}/events`);
  if (!resp.ok) {
    svDetailEl.innerHTML = '<span class="empty">No stored trace for this call.</span>';
    return;
  }
  const events = await resp.json();
  renderReplay(events);
}

function renderReplay(events) {
  svDetailEl.innerHTML = "";
  const transcript = document.createElement("div");
  transcript.style.cssText = "display:flex;flex-direction:column;gap:6px;margin-bottom:12px;";
  const stageList = document.createElement("div");
  stageList.style.cssText = "display:flex;flex-direction:column;gap:4px;";
  let decisionHtml = "";

  for (const evt of events) {
    const p = evt.payload || {};
    if (evt.event_type === "transcript.utterance_final") {
      const b = document.createElement("div");
      b.className = `bubble ${p.channel}`;
      b.style.maxWidth = "100%";
      b.innerHTML = `<span class="ch">${p.channel}</span>${p.text}`;
      transcript.appendChild(b);
    } else if (evt.event_type === "commercial.decision_made") {
      decisionHtml = `<div class="status-banner status-${p.outcome}" style="margin:10px 0 4px">${p.outcome.replace(/_/g, " ").toUpperCase()}</div>
        <div class="citelist">policy: ${(p.policy_decisions || []).join(", ") || "(none fired)"}</div>`;
    } else if (evt.event_type === "copilot.suggestion_generated") {
      decisionHtml += `<div class="suggestion-text">${p.explanation}</div><div class="citelist">cites: ${(p.cited_document_ids || []).join(", ") || "none"}</div>`;
    } else if (evt.event_type === "pipeline.stage" && p.status === "done") {
      const meta = STAGE_CATALOG[p.stage] || { code: p.stage.toUpperCase(), source: "local" };
      const row = document.createElement("div");
      row.style.cssText = "font-size:11.5px;display:flex;gap:8px;align-items:center;";
      row.innerHTML = `<span class="trace-code" style="background:${meta.source === "cloud" ? "var(--cloud)" : "var(--local)"}">${meta.code}</span>
        <span style="color:var(--muted)">${(p.ms ?? 0).toFixed(0)} ms</span>`;
      stageList.appendChild(row);
    }
  }
  if (transcript.children.length) svDetailEl.appendChild(transcript);
  if (decisionHtml) {
    const d = document.createElement("div");
    d.innerHTML = decisionHtml;
    svDetailEl.appendChild(d);
  }
  if (stageList.children.length) {
    const details = document.createElement("details");
    details.open = false;
    const summary = document.createElement("summary");
    summary.textContent = `pipeline stages (${stageList.children.length})`;
    details.appendChild(summary);
    details.appendChild(stageList);
    svDetailEl.appendChild(details);
  }
  if (!svDetailEl.children.length) svDetailEl.appendChild(emptySpan("Nothing to show for this call."));
}

resetPanels();
loadMode();
loadScripts();
setupAssistant();
