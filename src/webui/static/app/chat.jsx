// Chat page — wired to /api/chat with live state and SSE updates
const { useState, useRef, useEffect } = React;

function ChatPage() {
  useDataVersion(); // re-render when DATA refreshes
  const [selectedSessionId, setSelectedSessionId] = useState(null);

  const session = (selectedSessionId
    ? DATA.sessions.find(function (s) { return s.id === selectedSessionId; })
    : null) || DATA.sessions[0] || {
    id: "—", title: "—", status: "queued", started: "—", model: "—",
    summary: { tokens: "—", spend: "—", toolCalls: 0 },
    chat: { contextChips: [], messages: [] },
    shells: [], subagents: [],
  };

  const isLiveSession = session.id === "run_live";

  // When sessions list refreshes, drop stale selection
  useEffect(function () {
    if (selectedSessionId && !DATA.sessions.some(function (s) { return s.id === selectedSessionId; })) {
      setSelectedSessionId(null);
    }
  }, [selectedSessionId, DATA.sessions]);

  // Expose global session switcher so the sidebar can switch the chat view
  useEffect(function () {
    window.selectChatSession = function (id) { setSelectedSessionId(id); };
    return function () { delete window.selectChatSession; };
  }, []);

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingMessages, setPendingMessages] = useState([]);
  const [liveProgress, setLiveProgress] = useState([]);
  const progressRef = useRef([]);
  const taRef = useRef(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [session.chat.messages.length, pendingMessages.length]);

  // Subscribe to SSE events during sending for real-time progress display
  useEffect(() => {
    if (!sending) {
      setLiveProgress([]);
      progressRef.current = [];
      return;
    }
    function formatEvent(event) {
      switch (event.type) {
        case "phase_transition":
          return { icon: "●", text: event.detail || event.from + " → " + event.to };
        case "tool_call": {
          const args = event.args || {};
          const argPreview = Object.values(args).filter(Boolean).map(String).join(", ").slice(0, 60);
          return { icon: "▸", text: event.tool + (argPreview ? "(" + argPreview + ")" : "()") };
        }
        case "llm_call":
          return { icon: "◎", text: (event.caller || "agent") + " · " + (event.phase || "thinking") };
        case "chat_filter":
          return { icon: "✱", text: "Applying persona voice..." };
        default:
          return null;
      }
    }
    function handler(event) {
      var entry = formatEvent(event);
      if (!entry) return;
      progressRef.current = progressRef.current.concat([entry]).slice(-30);
      setLiveProgress(progressRef.current);
    }
    window.__sseHandlers.add(handler);
    return function () { window.__sseHandlers.delete(handler); };
  }, [sending]);

  function autosize(e) {
    setDraft(e.target.value);
    if (taRef.current) {
      taRef.current.style.height = "auto";
      taRef.current.style.height = Math.min(200, taRef.current.scrollHeight) + "px";
    }
  }

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    const userMsg = {
      id: "pending_user_" + Date.now(),
      role: "user", time: new Date().toLocaleTimeString(),
      body: text,
    };
    setPendingMessages((arr) => [...arr, userMsg]);
    setDraft("");
    if (taRef.current) taRef.current.style.height = "auto";

    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const agentMsg = {
        id: "pending_agent_" + Date.now(),
        role: "agent", time: new Date().toLocaleTimeString(),
        body: data.response || "(no response)",
      };
      setPendingMessages((arr) => [...arr, agentMsg]);
      // Refresh sessions FIRST so the run_live entry contains the new
      // messages before we clear pending — otherwise there's a flash of
      // "No messages yet" between pending-clear and sessions-arriving.
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      setPendingMessages([]);
    } catch (e) {
      setPendingMessages((arr) => [...arr, {
        id: "err_" + Date.now(),
        role: "system", time: new Date().toLocaleTimeString(),
        body: "Error: " + e.message,
      }]);
    } finally {
      setSending(false);
    }
  }

  function onKey(e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  }

  const allMessages = [...session.chat.messages, ...pendingMessages];

  async function newSession() {
    if (!confirm("Start a new session? The current conversation will be compressed into short-term memory.")) return;
    try {
      const r = await fetch("/api/sessions", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
      setPendingMessages([]);
    } catch (e) {
      alert("Failed: " + e.message);
    }
  }

  return (
    <div className="chat-layout">
      <div className="chat-main">
        <div className="chat-scroll" ref={scrollRef}>
          <div className="thread-header">
            <span className={"sa-dot " + session.status} style={{ marginTop: 0, width: 6, height: 6 }}></span>
            <span>{session.title}</span>
            <span style={{ marginLeft: "auto" }}>{session.id} · started {session.started}</span>
            <span style={{
                    cursor: "pointer", color: "var(--text-3)",
                    border: "1px solid var(--line)", borderRadius: 4,
                    padding: "2px 8px", fontSize: 10.5, letterSpacing: "0.04em",
                  }}
                  onClick={newSession}
                  onMouseEnter={(e) => (e.target.style.color = "var(--accent)")}
                  onMouseLeave={(e) => (e.target.style.color = "var(--text-3)")}
                  title="Compress current session and start a new one">
              + new session
            </span>
          </div>
          {!isLiveSession && (
            <div className="archive-banner">
              <span>Viewing archive · {session.id.replace("day_", "")}</span>
              <span className="archive-banner-action"
                    onClick={function () { setSelectedSessionId(null); window.selectChatSession = undefined; }}>
                ← return to live session
              </span>
            </div>
          )}
          {allMessages.length === 0 && (
            <div style={{ padding: "40px 0", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
              No messages yet. Say hello to {DATA.assistantName}.
            </div>
          )}
          {allMessages.map((m) => <Message key={m.id} msg={m} assistantName={DATA.assistantName} />)}
          {sending && (
            <div className="msg agent">
              <div className="msg-meta">
                <span className="msg-role agent">● {DATA.assistantName}</span>
                <span className="msg-time">…</span>
              </div>
              <div className="thinking">
                <div className="thinking-head">processing</div>
                {liveProgress.length === 0 && <div className="progress-entry"><span className="progress-icon">◎</span><span className="progress-text">Thinking...</span></div>}
                {liveProgress.map(function (p, i) {
                  return <div key={i} className="progress-entry"><span className="progress-icon">{p.icon}</span><span className="progress-text">{p.text}</span></div>;
                })}
              </div>
            </div>
          )}
        </div>

        {!isLiveSession && (
          <div className="composer" style={{ textAlign: "center", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 11 }}>
            <div style={{ padding: "16px 0" }}>
              This is an archived session — open the <a style={{ color: "var(--accent)", cursor: "pointer", textDecoration: "underline" }}
                  onClick={function () { setSelectedSessionId(null); }}>live session</a> to send messages.
            </div>
          </div>
        )}
        {isLiveSession && (
        <div className="composer">
          <div className="composer-box">
            <div className="composer-chips">
              {(session.chat.contextChips || []).map((c, i) => (
                <span className="chip" key={i}>
                  {c.icon} {c.label} <span className="x">×</span>
                </span>
              ))}
              <span className="chip" style={{ borderStyle: "dashed", cursor: "pointer" }}>+ add context</span>
            </div>
            <textarea
              ref={taRef}
              value={draft}
              onChange={autosize}
              onKeyDown={onKey}
              placeholder={"Message " + DATA.assistantName + "… (⌘+↵ to send)"}
            />
            <div className="composer-actions">
              <button className="iconbtn" title="Attach">+</button>
              <button className="iconbtn" title="Slash command">/</button>
              <button className="iconbtn" title="Mention">@</button>
              <span style={{ flex: 1 }}></span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>
                {session.model}
              </span>
              <button className="send" disabled={!draft.trim() || sending} onClick={send}>
                {sending ? "sending…" : <>send <span className="kbd">⌘↵</span></>}
              </button>
            </div>
          </div>
          <div className="composer-hint">
            <span>{DATA.assistantName} plans, then acts. Subagents spawn for parallel work.</span>
            <span>
              {sending ? "running · " : ""}
              {session.subagents.filter((s) => s.status === "running").length} active subagent(s)
            </span>
          </div>
        </div>
        )}
      </div>

      <ChatSide session={session} />
    </div>
  );
}

function Message({ msg, assistantName }) {
  return (
    <div className={"msg " + msg.role}>
      <div className="msg-meta">
        <span className={"msg-role " + msg.role}>
          {msg.role === "user" ? "▸ you" :
           msg.role === "agent" ? "● " + (assistantName || "agent") :
           msg.role}
        </span>
        <span className="msg-time">{msg.time}</span>
      </div>

      {msg.thinking && (
        <div className="thinking">
          <div className="thinking-head">reasoning</div>
          {msg.thinking}
        </div>
      )}

      {msg.body && <div className="msg-body">{msg.body}</div>}

      {msg.tools && msg.tools.map((t, i) => <ToolCard key={i} tool={t} />)}
    </div>
  );
}

function ToolCard({ tool }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tool-card">
      <div className="tool-head" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
        <span>{open ? "▾" : "▸"}</span>
        <span className="name">{tool.name}</span>
        <span className="arg">({tool.arg})</span>
        <span className={"pill " + (tool.status === "running" ? "running" : tool.status === "err" ? "err" : "")}>
          {tool.status}
        </span>
      </div>
      {open && tool.out && <div className="tool-body">{tool.out}</div>}
    </div>
  );
}

function ChatSide({ session }) {
  return (
    <div className="chat-side">
      <div className="side-section">
        <div className="side-head">
          Active shells
          <span className="count">{session.shells.length}</span>
        </div>
        {session.shells.length === 0 && (
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)" }}>—</div>
        )}
        {session.shells.map((s) => <ShellCard key={s.id} shell={s} />)}
      </div>

      <div className="side-section" style={{ flex: 1, overflowY: "auto" }}>
        <div className="side-head">
          Subagents
          <span className="count">{session.subagents.length}</span>
        </div>
        {session.subagents.length === 0 && (
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)" }}>—</div>
        )}
        {session.subagents.map((s) => <SubagentMini key={s.id} sa={s} />)}
      </div>

      <div className="side-section" style={{ borderBottom: 0 }}>
        <div className="side-head">Run summary</div>
        <div className="kv" style={{ rowGap: 6 }}>
          <span className="k">run id</span><span className="v">{session.id}</span>
          <span className="k">started</span><span className="v">{session.started}</span>
          <span className="k">elapsed</span><span className="v">{session.dur}</span>
          <span className="k">tool calls</span><span className="v">{session.summary.toolCalls}</span>
          <span className="k">tokens</span><span className="v">{session.summary.tokens}</span>
          <span className="k">spend</span><span className="v">{session.summary.spend}</span>
        </div>
      </div>
    </div>
  );
}

function ShellCard({ shell }) {
  return (
    <div className="shell-card">
      <div className="shell-card-head">
        <span>▣</span>
        <span className="cwd">{shell.cwd}</span>
        <span className="pid">pid {shell.pid}</span>
      </div>
      <div className="shell-card-body">
        {shell.lines.map((l, i) => (
          <div key={i} className={"shell-" + l.kind}>{l.text}</div>
        ))}
      </div>
    </div>
  );
}

function SubagentMini({ sa }) {
  return (
    <div className="subagent-mini">
      <div className={"sa-dot " + sa.status}></div>
      <div className="sa-body">
        <div className="sa-name">
          {sa.name} <span className="id">· {sa.id}</span>
        </div>
        <div className="sa-task">{sa.task}</div>
        <div className="sa-meta">
          <span><b>tok</b> {sa.tokens || 0}</span>
          <span><b>t+</b> {sa.elapsed || "—"}</span>
          <span style={{ marginLeft: "auto" }}>{sa.status}</span>
        </div>
        {sa.status === "running" && (
          <div className="bar warn"><div style={{ width: ((sa.progress || 0.5) * 100) + "%" }}></div></div>
        )}
      </div>
    </div>
  );
}

window.ChatPage = ChatPage;
