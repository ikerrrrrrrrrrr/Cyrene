// Sessions page — overview of every run
const { useState: useStateSes } = React;

function tokensTotal(tok) {
  if (!tok || tok === "—") return tok || "—";
  const m = String(tok).match(/(\S+)\s+total/);
  return m ? m[1] : tok;
}

function SessionsPage({ selectedSessionId, onSelectSession, onOpenAgents, rightSidebarCollapsed = false }) {
  useDataVersion();
  const { t } = useI18n();
  const [filter, setFilter] = useStateSes("all"); // all | running | done | err
  const [query, setQuery] = useStateSes(window._searchQuery || "");
  const searchRef = React.useRef(null);
  React.useEffect(() => {
    if (window._searchQuery !== undefined) {
      window._searchQuery = undefined;
      searchRef.current?.focus();
    }
  }, []);

  async function deleteSession(id) {
    const sess = DATA.sessions.find((s) => s.id === id);
    const isLive = id === "run_live";
    const msg = isLive
      ? "Clear the current session? Conversation will be compressed into short-term memory."
      : `Delete archive "${sess ? sess.title : id}"? This permanently removes the markdown file.`;
    if (!confirm(msg)) return;
    try {
      if (isLive && window.resetChatRuntime) window.resetChatRuntime({ abort: true });
      const r = await fetch("/api/sessions/" + encodeURIComponent(id), { method: "DELETE" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
      if (selectedSessionId === id) onSelectSession && onSelectSession(null);
    } catch (err) { alert("Delete failed: " + err.message); }
  }

  const session = (selectedSessionId
    ? DATA.sessions.find((s) => s.id === selectedSessionId)
    : null) || DATA.sessions[0];

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
    <div className={"sessions-layout" + (rightSidebarCollapsed ? " right-collapsed" : "")}>
      <div className="sessions-main">
        <div className="sessions-summary">
          <SummaryTile label={t("sessions.total")} value={totals.all} />
          <SummaryTile label={t("sessions.running")} value={totals.running} dotClass="running" />
          <SummaryTile label={t("sessions.completed")} value={totals.done} dotClass="done" />
          <SummaryTile label={t("sessions.errored")} value={totals.err} dotClass="err" />
        </div>

        <div className="sessions-controls">
          <div className="sessions-filters">
            {[
              { id: "all",     label: t("sessions.all") },
              { id: "running", label: t("sessions.running") },
              { id: "done",    label: t("sessions.done") },
              { id: "err",     label: t("sessions.err") },
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
            <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("sessions.searchPlaceholder")} />
          </div>
          <button className="btn primary"
                  style={{ marginLeft: 8 }}
                  onClick={async () => {
                    if (!confirm(t("sessions.confirmNewSession"))) return;
                    try {
                      if (window.resetChatRuntime) window.resetChatRuntime({ abort: true });
                      const r = await fetch("/api/sessions", { method: "POST" });
                      if (!r.ok) throw new Error("HTTP " + r.status);
                      const data = await r.json();
                      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
                      onSelectSession && onSelectSession(null);
                    } catch (e) { alert(t("chat.failedToCreate") + ": " + e.message); }
                  }}>
            {t("sessions.newSession")}
          </button>
        </div>

        <div className="sessions-table-wrap">
          <table className="sessions-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}></th>
                <th>{t("sessions.title")}</th>
                <th>{t("sessions.started")}</th>
                <th>{t("sessions.duration")}</th>
                <th style={{ textAlign: "right" }}>{t("sessions.tools")}</th>
                <th style={{ textAlign: "right" }}>{t("sessions.tokens")}</th>
                <th style={{ textAlign: "right" }}>{t("sessions.spend")}</th>
                <th>{t("sessions.model")}</th>
                <th style={{ width: 28 }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => (
                <tr key={s.id}
                    className={s.id === session?.id ? "row-active" : ""}
                    onClick={() => onSelectSession && onSelectSession(s.id)}>
                  <td><span className={"sa-dot " + s.status} style={{ marginTop: 0, width: 7, height: 7 }}></span></td>
                  <td>
                    <div className="cell-title">{s.title}</div>
                    <div className="cell-preview">{s.preview}</div>
                  </td>
                  <td className="cell-mono" style={{ whiteSpace: "nowrap" }}>{s.started}</td>
                  <td className="cell-mono" style={{ whiteSpace: "nowrap" }}>{s.dur}</td>
                  <td className="cell-mono" style={{ textAlign: "right", whiteSpace: "nowrap" }}>{s.summary.toolCalls}</td>
                  <td className="cell-mono" style={{ textAlign: "right", whiteSpace: "nowrap" }}>{tokensTotal(s.summary.tokens)}</td>
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
                  <td colSpan="9" style={{
                    padding: "40px 16px", textAlign: "center",
                    color: "var(--text-4)", fontFamily: "var(--mono)"
                  }}>
                    {t("sessions.noMatch")}
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
  const { t } = useI18n();
  const msgs = (session.chat && session.chat.messages) || [];
  const lastMsg = msgs[msgs.length - 1];
  const assistantName = (DATA && DATA.assistantName) || "agent";
  const isLive = session.id === "run_live";

  function statusLabel(s) {
    if (s === "done") return t("sessions.done");
    if (s === "running") return t("sessions.running");
    if (s === "err") return t("sessions.err");
    return s;
  }

  return (
    <div className="session-detail">
      <div className="session-detail-head">
        <div className="session-detail-status">
          <span className={"sa-dot " + session.status} style={{ marginTop: 0, width: 8, height: 8 }}></span>
          <span className="session-status-text">{statusLabel(session.status)}</span>
        </div>
        <h2 className="session-detail-title">{session.title}</h2>
        <div className="session-detail-id">{session.id} · {session.model}</div>
        <div style={{ marginTop: 12, display: "flex", gap: 6 }}>
          <button className="btn primary"
                  style={{ flex: 1, justifyContent: "center" }}
                  onClick={() => onOpenAgents && onOpenAgents(session.id)}>
            {t("sessions.openFlowchart")}
          </button>
          <button className="btn danger"
                  style={{ justifyContent: "center" }}
                  onClick={() => onDelete && onDelete(session.id)}
                  title={isLive ? t("sessions.clearTooltip") : t("sessions.deleteTooltip")}>
            {isLive ? t("sessions.clear") : t("sessions.delete")}
          </button>
        </div>
      </div>

      <div className="session-section">
        <div className="session-section-title">{t("sessions.stats")}</div>
        <div className="kv">
          <span className="k">{t("sessions.started")}</span><span className="v">{session.started}</span>
          <span className="k">{t("sessions.duration")}</span><span className="v">{session.dur}</span>
          <span className="k">{t("sessions.toolCalls")}</span><span className="v">{session.summary.toolCalls}</span>
          <span className="k">{t("sessions.requests")}</span><span className="v">{session.summary.requests ?? "—"}</span>
          <span className="k">{t("sessions.tokens")}</span><span className="v">{session.summary.tokens}</span>
          <span className="k">{t("sessions.spend")}</span><span className="v">{session.summary.spend}</span>
        </div>
      </div>

      <div className="session-section">
        <div className="session-section-title">{t("sessions.context")}</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {session.chat.contextChips.map((c, i) => (
            <span className="chip" key={i}>{c.icon} {c.label}</span>
          ))}
        </div>
      </div>

      {session.subagents.length > 0 && (
        <div className="session-section">
          <div className="session-section-title">{t("sessions.subagents")} · {session.subagents.length}</div>
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
          <div className="session-section-title">{t("sessions.lastMessage")}</div>
          <div className="last-msg">
            <div className="last-msg-meta">
              <span className={"msg-role " + (lastMsg.role === "user" ? "user" : "agent")}>
                {lastMsg.role === "user" ? "▸ " + t("chat.you") : "● " + assistantName}
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
