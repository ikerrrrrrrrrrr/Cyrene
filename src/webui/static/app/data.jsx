// Cyrene UI — data layer
// Loads real data from the FastAPI backend before the React tree mounts.
// Static fallback values keep the UI usable if the backend is unreachable.

const DATA = {
  user: { name: "loading…", handle: "loading", initials: "…" },
  assistantName: "Cyrene",

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

  settings: {
    sections: [
      { id: "general", label: "General" },
      { id: "models", label: "Models" },
      { id: "agents", label: "Agents" },
      { id: "tools", label: "Tools" },
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
    const r = await fetch("/api/ui-data");
    if (!r.ok) throw new Error("ui-data fetch failed: " + r.status);
    const fresh = await r.json();
    if (fresh.user) DATA.user = fresh.user;
    if (fresh.assistantName) DATA.assistantName = fresh.assistantName;
    if (Array.isArray(fresh.sessions) && fresh.sessions.length) DATA.sessions = fresh.sessions;
    if (fresh.status) DATA.status = fresh.status;
    if (Array.isArray(fresh.skills) && fresh.skills.length) DATA.skills = fresh.skills;
    if (fresh.settings) DATA.settings = { ...DATA.settings, ...fresh.settings };
    bumpData();
  } catch (e) {
    console.warn("Cyrene: failed to load /api/ui-data, using fallback data", e);
  }
}

let __sessionsRequestSeq = 0;
let __statusRequestSeq = 0;
let __refreshTimer = null;

async function refreshSessions() {
  const seq = ++__sessionsRequestSeq;
  try {
    const r = await fetch("/api/sessions");
    if (!r.ok) return;
    const { sessions } = await r.json();
    if (seq !== __sessionsRequestSeq) return;
    if (Array.isArray(sessions) && sessions.length) {
      DATA.sessions = sessions;
      bumpData();
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
    DATA.status = status;
    bumpData();
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
          "tool_call",
          "llm_call",
          "chat_filter",
          "phase_transition",
          "subagent_update",
          "session_update",
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
