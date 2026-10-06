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
  traceRowsById = {};
}

function appendBubble(channel, text, sttDetail) {
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

function handleStage(p) {
  const meta = STAGE_CATALOG[p.stage] || { code: p.stage.slice(0, 5).toUpperCase(), label: p.stage, system: "", source: "local", business: "", technical: "" };
  let entry = traceRowsById[p.instance_id];
  if (!entry) {
    if (traceEl.querySelector(".empty")) clearChildren(traceEl);
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
      <details>
        <summary>technical detail</summary>
        <div class="trace-technical">${meta.technical}</div>
        <div class="trace-eh-note"></div>
        <pre class="trace-raw" style="display:none"></pre>
      </details>`;
    traceEl.appendChild(row);
    entry = { el: row };
    traceRowsById[p.instance_id] = entry;
  }
  const row = entry.el;
  if (p.status === "done") {
    row.querySelector(".trace-status").className = "trace-status trace-status-done";
    row.querySelector(".trace-status").textContent = "done";
    const ms = typeof p.ms === "number" ? p.ms : 0;
    row.querySelector(".trace-ms").textContent = `${ms.toFixed(0)} ms`;
    row.querySelector(".trace-bar-fill").style.width = Math.min(100, Math.round((ms / TRACE_SCALE_MS) * 100)) + "%";
    if (p.detail && Object.keys(p.detail).length) {
      const raw = row.querySelector(".trace-raw");
      raw.style.display = "block";
      raw.textContent = JSON.stringify(p.detail, null, 2);
    }
  } else if (p.status === "error") {
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

async function runScript(scriptId, buttons, activeCard) {
  buttons.forEach((b) => (b.disabled = true));
  if (activeCard) activeCard.classList.add("running");
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
    if (activeCard) activeCard.classList.remove("running");
  }
}

function dispatch(evt) {
  const p = evt.payload || {};
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
  const cards = [];
  for (const s of scripts) {
    const cat = SCENARIO_CATEGORY[s.id] || { tag: "SCENARIO", color: "var(--muted)" };
    const card = document.createElement("button");
    card.className = "scenario-card";
    card.innerHTML = `
      <div class="scenario-card-top">
        <span class="scenario-tag" style="background:${cat.color}">${cat.tag}</span>
        <span class="scenario-customer">${s.customer_id || ""}</span>
      </div>
      <div class="scenario-title">${s.title}</div>`;
    cards.push(card);
    card.addEventListener("click", () => runScript(s.id, cards, card));
    controls.appendChild(card);
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

resetPanels();
loadMode();
loadScripts();
setupAssistant();
