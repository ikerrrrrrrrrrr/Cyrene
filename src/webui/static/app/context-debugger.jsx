const { useState: useStateCtxDbg, useEffect: useEffectCtxDbg, useMemo: useMemoCtxDbg } = React;

function ContextDebuggerPage() {
  const { t } = useI18n();
  const [events, setEvents] = useStateCtxDbg([]);
  const [activeId, setActiveId] = useStateCtxDbg("");
  const [detail, setDetail] = useStateCtxDbg(null);
  const [loading, setLoading] = useStateCtxDbg(true);
  const [detailLoading, setDetailLoading] = useStateCtxDbg(false);
  const [error, setError] = useStateCtxDbg("");
  const [query, setQuery] = useStateCtxDbg("");
  const [typeFilter, setTypeFilter] = useStateCtxDbg("all");

  async function loadEvents(selectLatest) {
    setError("");
    try {
      const r = await fetch("/api/context-debug/events?limit=200");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const next = data.events || [];
      setEvents(next);
      if (selectLatest && next.length && !activeId) {
        setActiveId(next[0].id);
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffectCtxDbg(function () {
    let cancelled = false;
    async function load() {
      if (!cancelled) await loadEvents(true);
    }
    load();
    const timer = setInterval(function () {
      if (!cancelled) loadEvents(false);
    }, 8000);
    return function () {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  useEffectCtxDbg(function () {
    if (!activeId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    async function loadDetail() {
      setDetailLoading(true);
      try {
        const r = await fetch("/api/context-debug/events/" + encodeURIComponent(activeId));
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        if (!cancelled) setDetail(data);
      } catch (e) {
        if (!cancelled) setDetail({ error: e.message || String(e) });
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    }
    loadDetail();
    return function () { cancelled = true; };
  }, [activeId]);

  const filtered = useMemoCtxDbg(function () {
    const q = query.trim().toLowerCase();
    return events.filter(function (event) {
      if (typeFilter !== "all") {
        const tokens = event.token_by_type || {};
        if (!Object.prototype.hasOwnProperty.call(tokens, typeFilter)) return false;
      }
      if (!q) return true;
      return [
        event.id,
        event.caller,
        event.phase,
        event.model,
        event.source_log,
      ].join(" ").toLowerCase().includes(q);
    });
  }, [events, query, typeFilter]);

  const totals = useMemoCtxDbg(function () {
    const typeCounts = {};
    let blocks = 0;
    let tokens = 0;
    events.forEach(function (event) {
      blocks += Number(event.block_count || 0);
      tokens += Number(event.total_tokens_est || 0);
      Object.keys(event.token_by_type || {}).forEach(function (key) {
        typeCounts[key] = (typeCounts[key] || 0) + 1;
      });
    });
    return { calls: events.length, blocks, tokens, typeCounts };
  }, [events]);

  const typeOptions = Object.keys(totals.typeCounts).sort();

  return (
    <div className="context-debugger-page">
      <div className="ctxdbg-header">
        <div>
          <div className="ctxdbg-kicker">{t("contextDebug.kicker")}</div>
          <h1>{t("contextDebug.title")}</h1>
        </div>
        <button className="btn" onClick={function () { setLoading(true); loadEvents(false); }}>
          {t("contextDebug.refresh")}
        </button>
      </div>

      <div className="ctxdbg-metrics">
        <CtxMetric label={t("contextDebug.calls")} value={formatCtxNumber(totals.calls)} />
        <CtxMetric label={t("contextDebug.blocks")} value={formatCtxNumber(totals.blocks)} />
        <CtxMetric label={t("contextDebug.tokens")} value={formatCtxNumber(totals.tokens)} />
      </div>

      <div className="ctxdbg-layout">
        <div className="ctxdbg-list-pane">
          <div className="ctxdbg-controls">
            <div className="sessions-search ctxdbg-search">
              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
              </svg>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("contextDebug.search")} />
            </div>
            <select className="input ctxdbg-select" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="all">{t("contextDebug.allTypes")}</option>
              {typeOptions.map(function (type) {
                return <option key={type} value={type}>{type}</option>;
              })}
            </select>
          </div>

          {loading && <div className="ctxdbg-empty">{t("contextDebug.loading")}</div>}
          {error && <div className="ctxdbg-error">{t("contextDebug.failed")}: {error}</div>}
          {!loading && !error && filtered.length === 0 && <div className="ctxdbg-empty">{t("contextDebug.empty")}</div>}

          <div className="ctxdbg-event-list">
            {filtered.map(function (event) {
              const active = event.id === activeId;
              return (
                <div key={event.id} className={"ctxdbg-event " + (active ? "active" : "")} onClick={function () { setActiveId(event.id); }}>
                  <div className="ctxdbg-event-top">
                    <span className="ctxdbg-event-title">{event.caller || "agent"} · {event.phase || "call"}</span>
                    <span className="ctxdbg-event-time">{formatCtxTime(event.timestamp)}</span>
                  </div>
                  <div className="ctxdbg-event-meta">
                    <span>{formatCtxNumber(event.total_tokens_est)} tok</span>
                    <span>{event.block_count || 0} blocks</span>
                    <span>{event.message_count || 0} msgs</span>
                  </div>
                  <div className="ctxdbg-type-chips">
                    {Object.entries(event.token_by_type || {}).slice(0, 5).map(function ([type, count]) {
                      return <span key={type} className="ctxdbg-chip">{type}<b>{formatCtxNumber(count)}</b></span>;
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <ContextDebugDetail event={detail} loading={detailLoading} />
      </div>
    </div>
  );
}

function ContextDebugDetail({ event, loading }) {
  const { t } = useI18n();
  if (loading) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-empty">{t("contextDebug.loadingDetail")}</div></div>;
  if (!event) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-empty">{t("contextDebug.selectCall")}</div></div>;
  if (event.error) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-error">{event.error}</div></div>;

  const trace = event.context_trace || {};
  const blocks = Array.isArray(trace.included) ? trace.included : [];
  const tokenByType = trace.token_by_type || {};
  const messages = Array.isArray(trace.message_map) ? trace.message_map : [];
  const maxTypeTokens = Math.max(1, ...Object.values(tokenByType).map(function (v) { return Number(v || 0); }));

  return (
    <div className="ctxdbg-detail-pane">
      <div className="ctxdbg-detail-head">
        <div>
          <div className="ctxdbg-detail-title">{event.caller || "agent"} · {event.phase || "call"}</div>
          <div className="ctxdbg-detail-id">{event.event_id}</div>
        </div>
        <div className="ctxdbg-detail-time">{formatCtxTime(event.timestamp)}</div>
      </div>

      <div className="ctxdbg-summary-grid">
        <CtxMetric label={t("contextDebug.tokens")} value={formatCtxNumber(trace.total_tokens_est || 0)} compact />
        <CtxMetric label={t("contextDebug.blocks")} value={formatCtxNumber(blocks.length)} compact />
        <CtxMetric label={t("contextDebug.messages")} value={formatCtxNumber(messages.length)} compact />
        <CtxMetric label={t("contextDebug.duration")} value={event.duration_ms ? Math.round(event.duration_ms) + "ms" : "-"} compact />
      </div>

      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">{t("contextDebug.typeBreakdown")}</div>
        <div className="ctxdbg-type-bars">
          {Object.keys(tokenByType).length === 0 && <div className="ctxdbg-empty small">{t("contextDebug.noTypes")}</div>}
          {Object.entries(tokenByType).sort(function (a, b) { return Number(b[1] || 0) - Number(a[1] || 0); }).map(function ([type, count]) {
            const pct = Math.max(3, Math.round(Number(count || 0) / maxTypeTokens * 100));
            return (
              <div key={type} className="ctxdbg-type-row">
                <span>{type}</span>
                <div className="ctxdbg-type-track"><i style={{ width: pct + "%" }}></i></div>
                <b>{formatCtxNumber(count)}</b>
              </div>
            );
          })}
        </div>
      </section>

      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">{t("contextDebug.includedBlocks")}</div>
        <div className="ctxdbg-block-table-wrap">
          <table className="ctxdbg-block-table">
            <thead>
              <tr>
                <th>{t("contextDebug.block")}</th>
                <th>{t("contextDebug.type")}</th>
                <th>{t("contextDebug.source")}</th>
                <th>{t("contextDebug.reason")}</th>
                <th>{t("contextDebug.tokens")}</th>
              </tr>
            </thead>
            <tbody>
              {blocks.map(function (block, idx) {
                return (
                  <tr key={(block.id || "block") + idx}>
                    <td className="ctxdbg-mono">{block.id || "-"}</td>
                    <td><span className="ctxdbg-chip">{block.type || "-"}</span></td>
                    <td>{block.source || "-"}</td>
                    <td>{block.reason || "-"}</td>
                    <td className="ctxdbg-mono right">{formatCtxNumber(block.tokens_est || 0)}</td>
                  </tr>
                );
              })}
              {blocks.length === 0 && (
                <tr><td colSpan="5" className="ctxdbg-empty-table">{t("contextDebug.noBlocks")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">{t("contextDebug.messageMap")}</div>
        <div className="ctxdbg-message-map">
          {messages.map(function (msg) {
            return (
              <div key={msg.message_index} className="ctxdbg-message-row">
                <span className="ctxdbg-msg-index">#{msg.message_index}</span>
                <span className="ctxdbg-chip">{msg.role || "-"}</span>
                <span className="ctxdbg-mono">{formatCtxNumber(msg.tokens_est || 0)} tok</span>
                <span className="ctxdbg-msg-blocks">{(msg.block_ids || []).join(", ") || "-"}</span>
              </div>
            );
          })}
          {messages.length === 0 && <div className="ctxdbg-empty small">{t("contextDebug.noMessages")}</div>}
        </div>
      </section>
    </div>
  );
}

function CtxMetric({ label, value, compact }) {
  return (
    <div className={"ctxdbg-metric " + (compact ? "compact" : "")}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatCtxNumber(value) {
  const n = Number(value || 0);
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1) + "K";
  return String(n);
}

function formatCtxTime(value) {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString();
  } catch (e) {
    return String(value);
  }
}

window.ContextDebuggerPage = ContextDebuggerPage;
