const { useState: useStateCtxDbg, useEffect: useEffectCtxDbg, useMemo: useMemoCtxDbg } = React;

function ContextDebuggerPage() {
  const { t } = useI18n();
  const [events, setEvents] = useStateCtxDbg([]);
  const [activeId, setActiveId] = useStateCtxDbg("");
  const [detail, setDetail] = useStateCtxDbg(null);
  const [requestGraph, setRequestGraph] = useStateCtxDbg(null);
  const [hoverNode, setHoverNode] = useStateCtxDbg("");
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
      setRequestGraph(null);
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

  useEffectCtxDbg(function () {
    const requestId = detail && (detail.request_id || (detail.identity_graph && detail.identity_graph.request_id));
    if (!requestId) {
      setRequestGraph(null);
      return;
    }
    let cancelled = false;
    async function loadRequestGraph() {
      try {
        const r = await fetch("/api/context-debug/requests/" + encodeURIComponent(requestId));
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        if (!cancelled) setRequestGraph(data);
      } catch (e) {
        if (!cancelled) setRequestGraph({ error: e.message || String(e), nodes: [], links: [] });
      }
    }
    loadRequestGraph();
    return function () { cancelled = true; };
  }, [detail && (detail.request_id || (detail.identity_graph && detail.identity_graph.request_id))]);

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

        <div className="ctxdbg-main-pane">
          <ContextRequestGraph
            graph={requestGraph}
            event={detail}
            activeEventId={activeId}
            hoverNode={hoverNode}
            onHoverNode={setHoverNode}
            onSelectEvent={setActiveId}
          />
          <ContextDebugDetail event={detail} loading={detailLoading} />
        </div>
      </div>
    </div>
  );
}

function ContextRequestGraph({ graph, event, activeEventId, hoverNode, onHoverNode, onSelectEvent }) {
  const fallbackGraph = useMemoCtxDbg(function () {
    return buildFallbackCallGraph(event);
  }, [event && event.event_id, event && event.context_trace]);
  const effectiveGraph = graph && Array.isArray(graph.nodes) && graph.nodes.length ? graph : fallbackGraph;
  const nodes = effectiveGraph && Array.isArray(effectiveGraph.nodes) ? effectiveGraph.nodes : [];
  const links = effectiveGraph && Array.isArray(effectiveGraph.links) ? effectiveGraph.links : [];
  if (!effectiveGraph) return null;
  if (effectiveGraph.error) return <div className="ctxdbg-graph"><div className="ctxdbg-error">Graph failed: {effectiveGraph.error}</div></div>;
  if (!nodes.length) return null;

  const nodeById = {};
  nodes.forEach(function (node) { nodeById[node.id] = node; });
  const eventNodes = nodes.filter(function (node) { return node.event_id; });
  const sourceNodes = nodes.filter(function (node) { return !node.event_id; });
  const incoming = {};
  const outgoing = {};
  links.forEach(function (link) {
    if (!incoming[link.target]) incoming[link.target] = [];
    if (!outgoing[link.source]) outgoing[link.source] = [];
    incoming[link.target].push(link.source);
    outgoing[link.source].push(link.target);
  });
  const activeNode = nodes.find(function (node) { return node.event_id === activeEventId; });
  const activeNodeId = activeNode ? activeNode.id : "";
  const highlighted = new Set();
  if (hoverNode) {
    highlighted.add(hoverNode);
    (incoming[hoverNode] || []).forEach(function (id) { highlighted.add(id); });
    (outgoing[hoverNode] || []).forEach(function (id) { highlighted.add(id); });
  }

  function renderNode(node) {
    const type = node.type || "source";
    const isActive = node.id === activeNodeId;
    const isDim = hoverNode && !highlighted.has(node.id);
    const classes = [
      "ctxdbg-graph-node",
      "ctxdbg-node-" + type,
      isActive ? "active" : "",
      highlighted.has(node.id) ? "highlight" : "",
      isDim ? "dim" : "",
      node.event_id ? "clickable" : "",
    ].join(" ");
    return (
      <div
        key={node.id}
        className={classes}
        onMouseEnter={function () { onHoverNode(node.id); }}
        onMouseLeave={function () { onHoverNode(""); }}
        onClick={function () { if (node.event_id) onSelectEvent(node.event_id); }}
        title={node.cid || node.id}
      >
        <div className="ctxdbg-graph-node-kind">{type === "llm" ? "LLM" : type === "tool" ? "TOOL" : type}</div>
        <div className="ctxdbg-graph-node-title">{node.title || node.id}</div>
        {(node.phase || node.tool) && <div className="ctxdbg-graph-node-meta">{node.phase || node.tool}</div>}
      </div>
    );
  }

  return (
    <section className="ctxdbg-graph">
      <div className="ctxdbg-graph-head">
        <div>
          <div className="ctxdbg-section-title">{effectiveGraph.request_id ? "Request timeline" : "Call context graph"}</div>
          <div className="ctxdbg-detail-id">{effectiveGraph.request_id || activeEventId}</div>
        </div>
        <div className="ctxdbg-graph-count">{eventNodes.length} events · {sourceNodes.length} sources</div>
      </div>
      <div className="ctxdbg-event-timeline">
        {eventNodes.map(function (node, idx) {
          return (
            <React.Fragment key={node.id}>
              {idx > 0 && <div className="ctxdbg-timeline-arrow">→</div>}
              {renderNode(node)}
            </React.Fragment>
          );
        })}
      </div>
      <div className="ctxdbg-graph-canvas">
        <div className="ctxdbg-graph-column sources">
          {sourceNodes.slice(0, 40).map(renderNode)}
          {sourceNodes.length > 40 && <div className="ctxdbg-graph-more">+{sourceNodes.length - 40} more sources</div>}
        </div>
        <div className="ctxdbg-graph-column ctxdbg-dependency-note">
          <div className="ctxdbg-graph-note-title">Context dependencies</div>
          <div className="ctxdbg-graph-note-body">
            Hover an LLM/tool node to highlight directly connected context sources. Adjacent events are shown in the timeline; older context is represented as source nodes.
          </div>
        </div>
      </div>
    </section>
  );
}

function buildFallbackCallGraph(event) {
  if (!event || event.type !== "llm_call") return null;
  const trace = event.context_trace || {};
  const blocks = Array.isArray(trace.included) ? trace.included : [];
  if (!blocks.length) return null;
  const eventNodeId = "node.llm." + (event.event_id || "selected");
  const nodes = [{
    id: eventNodeId,
    type: "llm",
    title: (event.caller || "agent") + " · " + (event.phase || "call"),
    event_id: event.event_id || "",
    timestamp: event.timestamp || "",
    caller: event.caller || "",
    phase: event.phase || "",
  }];
  const links = [];
  blocks.forEach(function (block, idx) {
    const sourceId = block.source_node_id || ("node.source." + (block.cid || block.id || idx));
    nodes.push({
      id: sourceId,
      type: block.type || "source",
      title: block.id || block.cid || ("source " + idx),
      cid: block.cid || "",
    });
    links.push({ source: sourceId, target: eventNodeId, kind: "context" });
  });
  return {
    request_id: trace.request_id || "",
    nodes: nodes,
    links: links,
    events: [{
      event_id: event.event_id || "",
      type: event.type || "",
      timestamp: event.timestamp || "",
      caller: event.caller || "",
      phase: event.phase || "",
    }],
  };
}

function ContextDebugDetail({ event, loading }) {
  const { t } = useI18n();
  if (loading) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-empty">{t("contextDebug.loadingDetail")}</div></div>;
  if (!event) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-empty">{t("contextDebug.selectCall")}</div></div>;
  if (event.error) return <div className="ctxdbg-detail-pane"><div className="ctxdbg-error">{event.error}</div></div>;

  if (event.type === "tool_call") return <ContextToolDetail event={event} />;

  const trace = event.context_trace || {};
  const blocks = Array.isArray(trace.included) ? trace.included : [];
  const tokenByType = trace.token_by_type || {};
  const messages = Array.isArray(trace.message_map) ? trace.message_map : [];
  const rawMessages = Array.isArray(event.messages) ? event.messages : [];
  const maxTypeTokens = Math.max(1, ...Object.values(tokenByType).map(function (v) { return Number(v || 0); }));

  return (
    <div className="ctxdbg-detail-pane">
      <div className="ctxdbg-detail-head">
        <div>
          <div className="ctxdbg-detail-title">{event.caller || "agent"} · {event.phase || "call"}</div>
          <div className="ctxdbg-detail-id">{event.event_id}</div>
          {event.request_id && <div className="ctxdbg-detail-id">{event.request_id}</div>}
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
                <span className="ctxdbg-msg-blocks">{((msg.block_cids && msg.block_cids.length) ? msg.block_cids : msg.block_ids || []).join(", ") || "-"}</span>
              </div>
            );
          })}
          {messages.length === 0 && <div className="ctxdbg-empty small">{t("contextDebug.noMessages")}</div>}
        </div>
      </section>

      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">Full LLM messages</div>
        <div className="ctxdbg-message-detail-list">
          {rawMessages.map(function (msg, idx) {
            const mapped = messages.find(function (item) { return Number(item.message_index) === idx; }) || {};
            const blockRefs = (mapped.block_cids && mapped.block_cids.length) ? mapped.block_cids : (mapped.block_ids || []);
            return (
              <details key={idx} className="ctxdbg-message-detail" open={idx < 3}>
                <summary>
                  <span className="ctxdbg-msg-index">#{idx}</span>
                  <span className="ctxdbg-chip">{msg.role || "-"}</span>
                  <span className="ctxdbg-mono">{formatCtxNumber(mapped.tokens_est || 0)} tok</span>
                  <span className="ctxdbg-msg-blocks">{blockRefs.join(", ") || "-"}</span>
                </summary>
                <pre className="ctxdbg-message-content">{formatMessageContent(msg)}</pre>
              </details>
            );
          })}
          {rawMessages.length === 0 && <div className="ctxdbg-empty small">No full messages stored for this call.</div>}
        </div>
      </section>
    </div>
  );
}

function ContextToolDetail({ event }) {
  return (
    <div className="ctxdbg-detail-pane">
      <div className="ctxdbg-detail-head">
        <div>
          <div className="ctxdbg-detail-title">{event.caller || "agent"} · {event.tool || "tool"}</div>
          <div className="ctxdbg-detail-id">{event.event_id}</div>
          {event.request_id && <div className="ctxdbg-detail-id">{event.request_id}</div>}
        </div>
        <div className="ctxdbg-detail-time">{formatCtxTime(event.timestamp)}</div>
      </div>
      <div className="ctxdbg-summary-grid">
        <CtxMetric label="Tool" value={event.tool || "-"} compact />
        <CtxMetric label="Duration" value={event.duration_ms ? Math.round(event.duration_ms) + "ms" : "-"} compact />
        <CtxMetric label="Result" value={formatCtxNumber(String(event.result || "").length) + " chars"} compact />
      </div>
      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">Tool query</div>
        <pre className="ctxdbg-json">{JSON.stringify(event.args || {}, null, 2)}</pre>
      </section>

      <section className="ctxdbg-section">
        <div className="ctxdbg-section-title">Tool output</div>
        <pre className="ctxdbg-json">{String(event.result || "")}</pre>
      </section>
    </div>
  );
}

function formatMessageContent(msg) {
  const parts = [];
  if (msg.content !== undefined && msg.content !== null && msg.content !== "") {
    parts.push(typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content, null, 2));
  }
  if (msg.reasoning_content) {
    parts.push("[reasoning_content]\n" + String(msg.reasoning_content));
  }
  if (msg.tool_calls) {
    parts.push("[tool_calls]\n" + JSON.stringify(msg.tool_calls, null, 2));
  }
  if (msg.tool_call_id) {
    parts.unshift("[tool_call_id] " + msg.tool_call_id);
  }
  return parts.join("\n\n") || JSON.stringify(msg, null, 2);
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
