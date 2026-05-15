// Sessions page — overview of every run
const { useState: useStateSes, useEffect: useEffectSes } = React;

function SessionsPage({ initialSessionId, onClearInitial, onOpenAgents }) {
  useDataVersion();
  const [selected, setSelected] = useStateSes(initialSessionId || DATA.sessions[0]?.id || "");
  const [filter, setFilter] = useStateSes("all"); // all | running | done | err
  const [query, setQuery] = useStateSes("");

  async function deleteSession(id) {
    const sess = DATA.sessions.find((s) => s.id === id);
    const isLive = id === "run_live";
    const msg = isLive
      ? "Clear the current session? Conversation will be compressed into short-term memory."
      : `Delete archive "${sess ? sess.title : id}"? This permanently removes the markdown file.`;
    if (!confirm(msg)) return;
    try {
      const r = await fetch("/api/sessions/" + encodeURIComponent(id), { method: "DELETE" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
      if (selected === id) setSelected(DATA.sessions[0]?.id || "");
    } catch (err) { alert("Delete failed: " + err.message); }
  }

  useEffectSes(() => {
    if (initialSessionId) {
      setSelected(initialSessionId);
      onClearInitial && onClearInitial();
    }
  }, [initialSessionId]);

  const session = DATA.sessions.find((s) => s.id === selected) || DATA.sessions[0];

  const filtered = DATA.sessions.filter((s) => {
    if (filter !== "all" && s.status !== filter) return false;
    if (query && !(s.title + " " + s.id).toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  // Summary stats
  const totals = {
    all: DATA.sessions.length,
    running: DATA.sessions.filter((s) => s.status === "running").length,
    done: DATA.sessions.filter((s) => s.status === "done").length,
    err: DATA.sessions.filter((s) => s.status === "err").length,
  };

  return (
    <div className="sessions-layout">
      <div className="sessions-main">
        <div className="sessions-summary">
          <SummaryTile label="Total" value={totals.all} />
          <SummaryTile label="Running" value={totals.running} dotClass="running" />
          <SummaryTile label="Completed" value={totals.done} dotClass="done" />
          <SummaryTile label="Errored" value={totals.err} dotClass="err" />
        </div>

        <div className="sessions-controls">
          <div className="sessions-filters">
            {[
              { id: "all",     label: "all" },
              { id: "running", label: "running" },
              { id: "done",    label: "done" },
              { id: "err",     label: "errored" },
            ].map((f) => (
              <div key={f.id}
                   className={"sessions-filter " + (filter === f.id ? "active" : "")}
                   onClick={() => setFilter(f.id)}>
                {f.label}<span className="filter-count">{f.id === "all" ? totals.all : totals[f.id] ?? 0}</span>
              </div>
            ))}
          </div>
          <div className="sessions-search">
            <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
            </svg>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search sessions" />
          </div>
          <button className="btn primary"
                  style={{ marginLeft: 8 }}
                  onClick={async () => {
                    if (!confirm("Start a new session? The current conversation will be compressed into short-term memory and the active context window will be cleared.")) return;
                    try {
                      const r = await fetch("/api/sessions", { method: "POST" });
                      if (!r.ok) throw new Error("HTTP " + r.status);
                      const data = await r.json();
                      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
                      setSelected(DATA.sessions[0]?.id || "");
                    } catch (e) { alert("Failed to create session: " + e.message); }
                  }}>
            + new session
          </button>
        </div>

        <div className="sessions-table-wrap">
          <table className="sessions-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}></th>
                <th>title</th>
                <th>id</th>
                <th>started</th>
                <th>duration</th>
                <th style={{ textAlign: "right" }}>tools</th>
                <th style={{ textAlign: "right" }}>tokens</th>
                <th style={{ textAlign: "right" }}>spend</th>
                <th>model</th>
                <th style={{ width: 28 }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => (
                <tr key={s.id}
                    className={s.id === selected ? "row-active" : ""}
                    onClick={() => setSelected(s.id)}>
                  <td><span className={"sa-dot " + s.status} style={{ marginTop: 0, width: 7, height: 7 }}></span></td>
                  <td>
                    <div className="cell-title">{s.title}</div>
                    <div className="cell-preview">{s.preview}</div>
                  </td>
                  <td className="cell-mono">{s.id}</td>
                  <td className="cell-mono">{s.started}</td>
                  <td className="cell-mono">{s.dur}</td>
                  <td className="cell-mono" style={{ textAlign: "right" }}>{s.summary.toolCalls}</td>
                  <td className="cell-mono" style={{ textAlign: "right" }}>{s.summary.tokens}</td>
                  <td className="cell-mono" style={{ textAlign: "right", color: "var(--text)" }}>{s.summary.spend}</td>
                  <td className="cell-mono" style={{ color: "var(--text-3)" }}>{s.model}</td>
                  <td style={{ textAlign: "right" }}>
                    <span title={s.id === "run_live" ? "Clear current session" : "Delete this archive"}
                          style={{ cursor: "pointer", color: "var(--text-4)", padding: "0 6px" }}
                          onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                          onMouseEnter={(e) => (e.target.style.color = "var(--err)")}
                          onMouseLeave={(e) => (e.target.style.color = "var(--text-4)")}>
                      ×
                    </span>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan="10" style={{
                    padding: "40px 16px", textAlign: "center",
                    color: "var(--text-4)", fontFamily: "var(--mono)"
                  }}>
                    no sessions match.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <SessionDetailPane session={session} onOpenAgents={onOpenAgents} onDelete={deleteSession} />
    </div>
  );
}

function SummaryTile({ label, value, dotClass }) {
  return (
    <div className="summary-tile">
      <div className="summary-label">
        {dotClass && <span className={"sa-dot " + dotClass} style={{ marginTop: 0, width: 6, height: 6 }}></span>}
        {label}
      </div>
      <div className="summary-value">{value}</div>
    </div>
  );
}

function SessionDetailPane({ session, onOpenAgents, onDelete }) {
  if (!session) return null;
  const msgs = (session.chat && session.chat.messages) || [];
  const lastMsg = msgs[msgs.length - 1];
  const assistantName = (DATA && DATA.assistantName) || "agent";
  const isLive = session.id === "run_live";

  return (
    <div className="session-detail">
      <div className="session-detail-head">
        <div className="session-detail-status">
          <span className={"sa-dot " + session.status} style={{ marginTop: 0, width: 8, height: 8 }}></span>
          <span className="session-status-text">{session.status}</span>
        </div>
        <h2 className="session-detail-title">{session.title}</h2>
        <div className="session-detail-id">{session.id} · {session.model}</div>
        <div style={{ marginTop: 12, display: "flex", gap: 6 }}>
          <button className="btn primary"
                  style={{ flex: 1, justifyContent: "center" }}
                  onClick={onOpenAgents}>
            open flowchart →
          </button>
          <button className="btn danger"
                  style={{ justifyContent: "center" }}
                  onClick={() => onDelete && onDelete(session.id)}
                  title={isLive ? "Clear current session" : "Delete archive"}>
            {isLive ? "clear" : "delete"}
          </button>
        </div>
      </div>

      <div className="session-section">
        <div className="session-section-title">Stats</div>
        <div className="kv">
          <span className="k">started</span><span className="v">{session.started}</span>
          <span className="k">duration</span><span className="v">{session.dur}</span>
          <span className="k">tool calls</span><span className="v">{session.summary.toolCalls}</span>
          <span className="k">tokens</span><span className="v">{session.summary.tokens}</span>
          <span className="k">spend</span><span className="v">{session.summary.spend}</span>
        </div>
      </div>

      <div className="session-section">
        <div className="session-section-title">Context</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {session.chat.contextChips.map((c, i) => (
            <span className="chip" key={i}>{c.icon} {c.label}</span>
          ))}
        </div>
      </div>

      {session.subagents.length > 0 && (
        <div className="session-section">
          <div className="session-section-title">Subagents · {session.subagents.length}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {session.subagents.map((sa) => (
              <div key={sa.id} className="mini-subagent">
                <span className={"sa-dot " + sa.status} style={{ marginTop: 4, flexShrink: 0 }}></span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="mini-sa-name">{sa.name}</div>
                  <div className="mini-sa-task">{sa.task}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {lastMsg && (
        <div className="session-section">
          <div className="session-section-title">Last message</div>
          <div className="last-msg">
            <div className="last-msg-meta">
              <span className={"msg-role " + (lastMsg.role === "user" ? "user" : "agent")}>
                {lastMsg.role === "user" ? "▸ you" : "● " + assistantName}
              </span>
              <span style={{ color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 10.5 }}>{lastMsg.time}</span>
            </div>
            <div className="last-msg-body">{lastMsg.body}</div>
          </div>
        </div>
      )}
    </div>
  );
}

window.SessionsPage = SessionsPage;
