// Cyrene UI — data layer
// Loads real data from the FastAPI backend before the React tree mounts.
// Static fallback values keep the UI usable if the backend is unreachable.

const APP_VERSION = "—";

const DATA = {
  user: { name: "loading…", handle: "loading", initials: "…" },
  assistantName: "Cyrene",
  appVersion: APP_VERSION,
  dashboard: {
    today: { learned: [], learned_count: 0, memory_count: 0, archive_days: 0 },
    soul: { path: "", updated_at: "", recent_items: [], section_count: 0 },
    topic_cloud: [],
    emotion: [],
    usage: { requests: 0, tokens: "—", spend: "—", prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cache_hit_tokens: 0, cache_miss_tokens: 0, timeline: [] },
    reminders: [],
    recent_memories: [],
    recent_archive: [],
    activity_heatmap: { days: [], rows: [] },
  },

  sessions: [
    {
      id: "run_loading",
      title: "loading…",
      status: "queued",
      started: "—",
      dur: "—",
      preview: "Fetching session data from backend…",
      model: "—",
      summary: { tokens: "—", spend: "—", toolCalls: 0 },
      chat: {
        contextChips: [{ icon: "⌛", label: "loading" }],
        messages: [],
      },
      liveRounds: [],
      shells: [],
      subagents: [],
      flow: {
        nodes: [
          {
            id: "n_main", kind: "main", x: 200, y: 80,
            title: "main agent", subtitle: "loading", status: "queued",
            model: "—",
            detail: {
              systemPrompt: "Loading…",
              reasoning: "Fetching live state from /api/ui-data.",
              tokensIn: 0, tokensOut: 0, model: "—", temp: 0.2,
            },
          },
        ],
        edges: [],
      },
    },
  ],

  status: {
    metrics: [
      { label: "Subagents",    value: "—", unit: "", sub: "—", delta: null },
      { label: "Session msgs", value: "—", unit: "", sub: "—", delta: null },
      { label: "Short-term",   value: "—", unit: "", sub: "—", delta: null },
      { label: "Scheduled",    value: "—", unit: "", sub: "—", delta: null },
    ],
    sparkData: [1, 2, 3, 2, 3, 4, 3, 4, 5, 4, 5, 6, 5, 6, 7, 6, 7, 8, 7, 8],
    workers: [],
    logs: [],
    services: [],
  },

  skills: [],

  onboarding: {
    needsOnboarding: false,
    isAbsoluteFreshStart: false,
    activeStep: "done",
    completedAt: "",
    llm: {
      configured: false,
      hasApiKey: false,
      baseUrl: "https://api.deepseek.com/v1",
      model: "deepseek-chat",
      completedAt: "",
    },
    personality: {
      configured: false,
      completedAt: "",
      mode: "",
      label: "",
      isDefaultSoul: true,
      path: "",
      currentContent: "",
    },
  },

  settings: {
    sections: [
      { id: "general", label: "General" },
      { id: "models", label: "Models" },
      { id: "agents", label: "Agent flow" },
      { id: "tools", label: "Tools" },
      { id: "search", label: "Search" },
      { id: "keys", label: "API keys" },
      { id: "appearance", label: "Appearance" },
      { id: "danger", label: "Danger zone" },
    ],
    models: [],
  },
};

// Force a React re-render when fresh data lands.
let __dataVersion = 0;
const __dataSubscribers = new Set();
function bumpData() {
  __dataVersion += 1;
  __dataSubscribers.forEach((fn) => fn(__dataVersion));
}
function useDataVersion() {
  const [v, setV] = React.useState(__dataVersion);
  React.useEffect(() => {
    __dataSubscribers.add(setV);
    return () => __dataSubscribers.delete(setV);
  }, []);
  return v;
}

window.DATA = DATA;
window.bumpData = bumpData;
window.useDataVersion = useDataVersion;

async function bootstrapData() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    const r = await fetch("/api/ui-data?tz=" + encodeURIComponent(tz));
    if (!r.ok) throw new Error("ui-data fetch failed: " + r.status);
    const fresh = await r.json();
    if (fresh.user) DATA.user = fresh.user;
    if (fresh.assistantName) DATA.assistantName = fresh.assistantName;
    if (fresh.appVersion) DATA.appVersion = fresh.appVersion;
    if (fresh.dashboard) DATA.dashboard = fresh.dashboard;
    if (Array.isArray(fresh.sessions) && fresh.sessions.length) DATA.sessions = fresh.sessions;
    if (fresh.status) DATA.status = fresh.status;
    if (Array.isArray(fresh.skills)) DATA.skills = fresh.skills;
    if (fresh.settings) DATA.settings = { ...DATA.settings, ...fresh.settings };
    if (fresh.onboarding) DATA.onboarding = fresh.onboarding;
    bumpData();
  } catch (e) {
    console.warn("Cyrene: failed to load /api/ui-data, using fallback data", e);
  }
}

let __sessionsRequestSeq = 0;
let __statusRequestSeq = 0;
let __refreshTimer = null;

function sessionsFingerprint(sessions) {
  if (!Array.isArray(sessions)) return "";
  return sessions.map(function (s) {
    var flow = s.flow || {};
    var nodes = Array.isArray(flow.nodes) ? flow.nodes.length : 0;
    var edges = Array.isArray(flow.edges) ? flow.edges.length : 0;
    var messages = s.chat && Array.isArray(s.chat.messages) ? s.chat.messages : [];
    var lastMessage = messages.length ? messages[messages.length - 1] : null;
    var lastMessageId = String(lastMessage && (lastMessage.messageId || lastMessage.id) || "");
    var lastMessageBody = String(lastMessage && lastMessage.body || "").slice(0, 120);
    var pendingQuestion = s.pendingQuestion || {};
    var liveRounds = Array.isArray(s.liveRounds) ? s.liveRounds : [];
    var liveRoundFingerprint = liveRounds.map(function (round) {
      return [
        round.id || "",
        round.status || "",
        round.pendingGuidance || 0,
        round.runningSubagents || 0,
        round.updatedAt || "",
      ].join(":");
    }).join(",");
    return [
      s.id || "",
      s.status || "",
      nodes,
      edges,
      s.preview || "",
      s.currentRoundId || "",
      messages.length,
      lastMessageId,
      lastMessageBody,
      pendingQuestion.id || "",
      pendingQuestion.text || "",
      liveRoundFingerprint,
    ].join("|");
  }).join(",");
}

async function refreshSessions() {
  const seq = ++__sessionsRequestSeq;
  try {
    const r = await fetch("/api/sessions");
    if (!r.ok) return;
    const { sessions } = await r.json();
    if (seq !== __sessionsRequestSeq) return;
    if (Array.isArray(sessions) && sessions.length) {
      var prev = sessionsFingerprint(DATA.sessions);
      var next = sessionsFingerprint(sessions);
      DATA.sessions = sessions;
      if (prev !== next) bumpData();
    }
  } catch (e) { /* swallow */ }
}

async function refreshStatus() {
  const seq = ++__statusRequestSeq;
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const status = await r.json();
    if (seq !== __statusRequestSeq) return;
    var prev = JSON.stringify(DATA.status);
    var next = JSON.stringify(status);
    DATA.status = status;
    if (prev !== next) bumpData();
  } catch (e) { /* swallow */ }
}

function scheduleRealtimeRefresh() {
  if (__refreshTimer) return;
  __refreshTimer = window.setTimeout(() => {
    __refreshTimer = null;
    void refreshSessions();
    void refreshStatus();
  }, 60);
}

window.refreshSessions = refreshSessions;
window.refreshStatus = refreshStatus;
window.reloadUiData = bootstrapData;

// ── Global SSE event bus for real-time chat progress ──
// Stores recent events so chat.jsx can display live progress during sending.
window.__sseEvents = [];
window.__sseHandlers = new Set();

// Subscribe to SSE so live events bump UI state.
function connectEvents() {
  try {
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === "heartbeat") return;

        // Store recent events for real-time display (ring buffer, max 200)
        window.__sseEvents.push(data);
        if (window.__sseEvents.length > 200) window.__sseEvents.shift();
        // Notify subscribers (used by chat.jsx during sending)
        window.__sseHandlers.forEach(function (fn) { fn(data); });

        if ([
          "chat_message",
          "guidance_acknowledged",
          "user_question",
          "user_question_answered",
          "tool_call",
          "llm_call",
          "phase_transition",
          "subagent_update",
          "session_update",
          "shell_update",
          "cc_learning",
          "round_guidance_update",
          "agent_comm",
          "assistant_message",
        ].includes(data.type)) {
          scheduleRealtimeRefresh();
        }
      } catch (e) { /* swallow */ }
    };
    window.__cyreneEventSource = es;
  } catch (e) {
    console.warn("SSE connection failed", e);
  }
}

bootstrapData().then(() => connectEvents());
setInterval(() => { refreshSessions(); refreshStatus(); }, 15000);
