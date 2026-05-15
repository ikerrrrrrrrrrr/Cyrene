// Agents page — flowchart with nodes + edges + inspector
const { useState: useStateAg, useRef: useRefAg, useEffect: useEffectAg, useMemo } = React;

function AgentsPage({ orientation = "horizontal" }) {
  useDataVersion();
  const [selectedRun, setSelectedRun] = useStateAg(DATA.sessions[0]?.id || "");
  const session = DATA.sessions.find((s) => s.id === selectedRun) || DATA.sessions[0] || { flow: { nodes: [], edges: [] } };
  const [selectedNode, setSelectedNode] = useStateAg("n_main");
  const [selectedEdgeIdx, setSelectedEdgeIdx] = useStateAg(null);
  const [zoom, setZoom] = useStateAg(0.85);
  const [pan, setPan] = useStateAg({ x: 20, y: 20 });
  const wrapRef = useRefAg(null);

  useEffectAg(() => {
    setSelectedNode(session.flow.nodes[0]?.id || null);
    setSelectedEdgeIdx(null);
  }, [selectedRun]);

  // Reset pan when orientation changes
  useEffectAg(() => {
    setPan({ x: 20, y: 20 });
  }, [orientation]);

  const { nodes, edges } = session.flow;
  const sel = nodes.find((n) => n.id === selectedNode);

  // Drag-to-pan
  const dragRef = useRefAg(null);
  useEffectAg(() => {
    const el = wrapRef.current;
    if (!el) return;
    function onDown(e) {
      if (e.target.closest(".node")) return;
      if (e.target.closest(".canvas-toolbar")) return;
      if (e.target.closest(".canvas-legend")) return;
      dragRef.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
    }
    function onMove(e) {
      if (!dragRef.current) return;
      const d = dragRef.current;
      setPan({ x: d.px + (e.clientX - d.x), y: d.py + (e.clientY - d.y) });
    }
    function onUp() { dragRef.current = null; }
    el.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      el.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [pan]);

  // Wheel: cmd/ctrl = zoom, plain wheel = scroll/pan, shift+wheel = horizontal pan
  function onWheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      setZoom((z) => Math.max(0.4, Math.min(1.6, z + (e.deltaY < 0 ? 0.05 : -0.05))));
      return;
    }
    e.preventDefault();
    setPan((p) => ({
      x: p.x - (e.shiftKey ? e.deltaY : e.deltaX),
      y: p.y - (e.shiftKey ? 0 : e.deltaY),
    }));
  }

  // Compute node rects (positions may be transposed for vertical layout)
  const nodeRects = useMemo(() => {
    const rects = {};
    for (const n of nodes) {
      const w = n.kind === "main" ? 220 : n.kind === "subagent" ? 200 : n.kind === "tool" ? 180 : n.kind === "output" ? 200 : 200;
      const h = 86;
      let x = n.x, y = n.y;
      if (orientation === "vertical") {
        // Swap axes; scale so columns/rows breathe given typical node sizes
        x = n.y * 1.45 + 20;
        y = n.x * 0.7 + 20;
      }
      rects[n.id] = { x, y, w, h };
    }
    return rects;
  }, [nodes, orientation]);

  const canvasSize = useMemo(() => {
    let maxX = 0, maxY = 0;
    for (const id in nodeRects) {
      const r = nodeRects[id];
      maxX = Math.max(maxX, r.x + r.w);
      maxY = Math.max(maxY, r.y + r.h);
    }
    return { w: maxX + 80, h: maxY + 80 };
  }, [nodeRects]);
  function edgePath(e) {
    const a = nodeRects[e.from];
    const b = nodeRects[e.to];
    if (!a || !b) return "";
    if (orientation === "vertical") {
      // bottom-mid of a → top-mid of b (or top → bottom for upward edges)
      let x1 = a.x + a.w / 2, y1 = a.y + a.h;
      let x2 = b.x + b.w / 2, y2 = b.y;
      if (b.y < a.y) { y1 = a.y; y2 = b.y + b.h; }
      const dy = Math.max(30, (y2 - y1) / 2);
      return `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
    }
    // horizontal: right-mid of a → left-mid of b
    let x1 = a.x + a.w, y1 = a.y + a.h / 2;
    let x2 = b.x,        y2 = b.y + b.h / 2;
    if (b.x < a.x) { x1 = a.x; x2 = b.x + b.w; }
    const dx = Math.max(40, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }

  function edgeLabelPos(e) {
    const a = nodeRects[e.from];
    const b = nodeRects[e.to];
    if (!a || !b) return { x: 0, y: 0 };
    if (orientation === "vertical") {
      return { x: (a.x + a.w / 2 + b.x + b.w / 2) / 2,
               y: (a.y + a.h + b.y) / 2 - 4 };
    }
    return { x: (a.x + a.w + b.x) / 2,
             y: (a.y + a.h / 2 + b.y + b.h / 2) / 2 - 6 };
  }

  return (
    <div className="agents-layout">
      <RunsList sessions={DATA.sessions} selected={selectedRun} onSelect={setSelectedRun} />

      <div className="canvas-wrap" ref={wrapRef} onWheel={onWheel}>
        <div className="canvas-toolbar">
          <button onClick={() => setZoom(z => Math.min(1.6, z + 0.1))}>＋</button>
          <button onClick={() => setZoom(z => Math.max(0.4, z - 0.1))}>−</button>
          <button onClick={() => { setZoom(0.85); setPan({ x: 20, y: 20 }); }}>⊕</button>
          <span style={{
            color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 10,
            alignSelf: "center", padding: "0 6px"
          }}>
            {Math.round(zoom * 100)}%
          </span>
        </div>

        <div className="flow" style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`
        }}>
          <svg className="flow-svg" width={canvasSize.w} height={canvasSize.h}>
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
                      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--line-2)" />
              </marker>
              <marker id="arrow-active" viewBox="0 0 10 10" refX="8" refY="5"
                      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--accent)" />
              </marker>
              <marker id="arrow-comm" viewBox="0 0 10 10" refX="8" refY="5"
                      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--info)" />
              </marker>
            </defs>
            {edges.map((e, i) => {
              const cls =
                "edge " +
                (e.kind === "active" ? "active" :
                 e.kind === "dashed" ? "dashed" :
                 e.kind === "comm"   ? "comm" : "") +
                (selectedEdgeIdx === i ? " selected" : "");
              const marker =
                e.kind === "active" ? "url(#arrow-active)" :
                e.kind === "comm"   ? "url(#arrow-comm)" :
                "url(#arrow)";
              const clickable = e.kind === "comm" && e.message;
              const lp = edgeLabelPos(e);
              return (
                <g key={i} style={{ pointerEvents: clickable ? "auto" : "none" }}>
                  {clickable && (
                    <path d={edgePath(e)} stroke="transparent" strokeWidth="14" fill="none"
                          style={{ cursor: "pointer" }}
                          onClick={() => { setSelectedEdgeIdx(i); setSelectedNode(null); }} />
                  )}
                  <path d={edgePath(e)} className={cls} markerEnd={marker}
                        style={{ cursor: clickable ? "pointer" : "default" }}
                        onClick={clickable ? () => { setSelectedEdgeIdx(i); setSelectedNode(null); } : undefined} />
                  {e.label && (
                    <text className={"edge-label" + (e.kind === "comm" ? " comm" : "")}
                          x={lp.x} y={lp.y} textAnchor="middle"
                          style={{ cursor: clickable ? "pointer" : "default" }}
                          onClick={clickable ? () => { setSelectedEdgeIdx(i); setSelectedNode(null); } : undefined}>
                      {e.label}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>

          {nodes.map((n) => (
            <FlowNode key={n.id} node={n}
                      pos={nodeRects[n.id]}
                      selected={n.id === selectedNode}
                      onClick={() => { setSelectedNode(n.id); setSelectedEdgeIdx(null); }} />
          ))}
        </div>

        <div className="canvas-legend">
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--accent)" }}></span>
            main agent
          </span>
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--info)" }}></span>
            subagent
          </span>
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--magenta)" }}></span>
            tool call
          </span>
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--warn)" }}></span>
            output
          </span>
          <span className="legend-item" style={{ marginLeft: 12, color: "var(--text-4)" }}>
            wheel · scroll  ·  ⌘+wheel · zoom  ·  drag · pan
          </span>
        </div>
      </div>

      <Inspector node={sel} edge={selectedEdgeIdx != null ? edges[selectedEdgeIdx] : null}
                 flow={session.flow}
                 onSelectNode={(id) => { setSelectedNode(id); setSelectedEdgeIdx(null); }} />
    </div>
  );
}

function RunsList({ sessions, selected, onSelect }) {
  return (
    <div className="runs-list">
      <div className="side-head" style={{ padding: "14px 14px 10px", margin: 0 }}>
        Sessions <span className="count">{sessions.length}</span>
      </div>
      {sessions.map((r) => (
        <div key={r.id}
             className={"run-item " + (r.id === selected ? "active" : "")}
             onClick={() => onSelect(r.id)}>
          <div className={"sa-dot " + r.status}></div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="run-title">{r.title}</div>
            <div className="run-meta">
              <span>{r.id}</span>
              <span>· {r.started}</span>
              <span>· {r.dur}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function FlowNode({ node, pos, selected, onClick }) {
  const cls = "node " + node.kind + (selected ? " selected" : "");
  const kindLabel =
    node.kind === "main" ? "main agent" :
    node.kind === "subagent" ? "subagent" :
    node.kind === "tool" ? "tool" :
    node.kind === "output" ? "output" :
    "input";
  return (
    <div className={cls}
         style={{ left: pos.x, top: pos.y, width: pos.w }}
         onClick={onClick}>
      <div className="node-head">
        <span>{kindLabel}</span>
        {node.status && <span className={"status-pill " + node.status}>{node.status}</span>}
      </div>
      <div className="node-title">{node.title}</div>
      {(node.subtitle || node.model) && (
        <div className="node-foot">
          {node.subtitle && <span>{node.subtitle}</span>}
          {node.model && <span style={{ marginLeft: "auto" }}><b>{node.model}</b></span>}
        </div>
      )}
    </div>
  );
}

function Inspector({ node, edge, flow, onSelectNode }) {
  const [tab, setTab] = useStateAg("details");
  if (edge) {
    return <EdgeInspector edge={edge} flow={flow} onSelectNode={onSelectNode} />;
  }
  if (!node) {
    return (
      <div className="inspector">
        <div className="empty-insp">Click any node or comm line to inspect it.</div>
      </div>
    );
  }

  const d = node.detail || {};
  const kindLabel =
    node.kind === "main" ? "Main agent" :
    node.kind === "subagent" ? "Subagent" :
    node.kind === "tool" ? "Tool call" :
    node.kind === "output" ? "Output" :
    "Input";

  return (
    <div className="inspector">
      <div className="insp-head">
        <div className="insp-kind">{kindLabel}</div>
        <div className="insp-title">{node.title}</div>
        <div className="insp-id">{node.id} · {node.status || "—"}</div>
      </div>
      <div className="insp-tabs">
        <div className={"insp-tab " + (tab === "details" ? "active" : "")} onClick={() => setTab("details")}>details</div>
        <div className={"insp-tab " + (tab === "io" ? "active" : "")} onClick={() => setTab("io")}>input / output</div>
        <div className={"insp-tab " + (tab === "raw" ? "active" : "")} onClick={() => setTab("raw")}>raw</div>
      </div>

      {tab === "details" && <InspectorDetails node={node} d={d} flow={flow} onSelectNode={onSelectNode} />}
      {tab === "io" && <InspectorIO node={node} d={d} />}
      {tab === "raw" && (
        <div className="insp-section" style={{ flex: 1 }}>
          <div className="insp-label">json</div>
          <pre className="code-block json">{JSON.stringify(node, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function InspectorDetails({ node, d, flow, onSelectNode }) {
  if (node.kind === "main") {
    return (
      <>
        <div className="insp-section">
          <div className="insp-label">system prompt</div>
          <div className="code-block">{d.systemPrompt}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">reasoning</div>
          <div className="code-block" style={{ color: "var(--text-2)" }}>{d.reasoning}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">model</div>
          <div className="kv">
            <span className="k">model</span><span className="v">{d.model}</span>
            <span className="k">temp</span><span className="v">{d.temp}</span>
            <span className="k">tokens in</span><span className="v">{d.tokensIn}</span>
            <span className="k">tokens out</span><span className="v">{d.tokensOut}</span>
          </div>
        </div>
        <CommsSection nodeId={node.id} flow={flow} onSelectNode={onSelectNode} />
      </>
    );
  }
  if (node.kind === "subagent") {
    return (
      <>
        <div className="insp-section">
          <div className="insp-label">task</div>
          <div className="insp-val" style={{ color: "var(--text)" }}>{d.task}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">lineage</div>
          <div className="kv">
            <span className="k">spawned by</span><span className="v">{d.parent}</span>
            <span className="k">spawned at</span><span className="v">{d.spawnedAt || "—"}</span>
            <span className="k">model</span><span className="v">{d.model || "—"}</span>
            <span className="k">tokens in</span><span className="v">{d.tokensIn ?? "—"}</span>
            <span className="k">tokens out</span><span className="v">{d.tokensOut ?? "—"}</span>
          </div>
        </div>
        <CommsSection nodeId={node.id} flow={flow} onSelectNode={onSelectNode} />
        <div className="insp-section">
          <div className="insp-label">connected nodes</div>
          <ConnectedList nodeId={node.id} flow={flow} />
        </div>
      </>
    );
  }
  if (node.kind === "tool") {
    return (
      <>
        <div className="insp-section">
          <div className="insp-label">tool</div>
          <div className="insp-val" style={{ color: "var(--magenta)" }}>{d.name}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">input</div>
          <pre className="code-block">{JSON.stringify(d.input, null, 2)}</pre>
        </div>
        <div className="insp-section">
          <div className="insp-label">output</div>
          <div className="code-block">{d.output}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">duration</div>
          <div className="insp-val">{d.duration || "—"}</div>
        </div>
      </>
    );
  }
  if (node.kind === "output") {
    return (
      <div className="insp-section">
        <div className="insp-label">content</div>
        <div className="code-block" style={{ color: "var(--text)" }}>{d.content}</div>
      </div>
    );
  }
  // input
  return (
    <>
      <div className="insp-section">
        <div className="insp-label">{d.role || "input"}</div>
        <div className="insp-val" style={{ color: "var(--text)" }}>{d.text}</div>
      </div>
      <div className="insp-section">
        <div className="kv">
          <span className="k">tokens</span><span className="v">{d.tokens}</span>
          <span className="k">at</span><span className="v">{d.time}</span>
        </div>
      </div>
    </>
  );
}

function InspectorIO({ node, d }) {
  return (
    <>
      <div className="insp-section">
        <div className="insp-label">input</div>
        <pre className="code-block">{
          node.kind === "tool" ? JSON.stringify(d.input, null, 2) :
          node.kind === "main" ? d.systemPrompt :
          node.kind === "subagent" ? d.task :
          node.kind === "input" ? d.text :
          "—"
        }</pre>
      </div>
      <div className="insp-section">
        <div className="insp-label">output</div>
        <pre className="code-block">{
          node.kind === "tool" ? (d.output || "—") :
          node.kind === "main" ? d.reasoning :
          node.kind === "output" ? d.content :
          "—"
        }</pre>
      </div>
    </>
  );
}

function ConnectedList({ nodeId, flow }) {
  const conn = flow.edges.filter((e) => e.from === nodeId || e.to === nodeId);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {conn.map((e, i) => {
        const other = e.from === nodeId ? e.to : e.from;
        const otherNode = flow.nodes.find((n) => n.id === other);
        const dir = e.from === nodeId ? "→" : "←";
        return (
          <div key={i} style={{
            display: "flex", gap: 8, padding: "5px 8px",
            background: "var(--bg-2)", border: "1px solid var(--line)",
            borderRadius: 4, fontFamily: "var(--mono)", fontSize: 11
          }}>
            <span style={{ color: "var(--text-4)" }}>{dir}</span>
            <span>{otherNode ? otherNode.title : other}</span>
            {e.kind === "comm" && <span style={{ marginLeft: "auto", color: "var(--info)" }}>comm</span>}
          </div>
        );
      })}
    </div>
  );
}

function CommsSection({ nodeId, flow, onSelectNode }) {
  const comms = flow.edges
    .map((e, i) => ({ ...e, idx: i }))
    .filter((e) => e.kind === "comm" && e.message && (e.from === nodeId || e.to === nodeId));
  if (comms.length === 0) return null;
  return (
    <div className="insp-section">
      <div className="insp-label">communications · {comms.length}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {comms.map((e) => {
          const outgoing = e.from === nodeId;
          const partnerId = outgoing ? e.to : e.from;
          const partner = flow.nodes.find((n) => n.id === partnerId);
          return (
            <div key={e.idx} className="comm-card">
              <div className="comm-card-head">
                <span className={"comm-dir " + (outgoing ? "out" : "in")}>
                  {outgoing ? "out →" : "← in"}
                </span>
                <span className="comm-partner"
                      onClick={() => onSelectNode && onSelectNode(partnerId)}
                      style={{ cursor: onSelectNode ? "pointer" : "default" }}>
                  {partner ? partner.title : partnerId}
                </span>
                <span className="comm-time">{e.message.time}</span>
              </div>
              {e.message.summary && (
                <div className="comm-summary">{e.message.summary}</div>
              )}
              <div className="comm-body">{e.message.body}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EdgeInspector({ edge, flow, onSelectNode }) {
  const from = flow.nodes.find((n) => n.id === edge.from);
  const to = flow.nodes.find((n) => n.id === edge.to);
  const kindLabel =
    edge.kind === "comm" ? "Communication" :
    edge.kind === "active" ? "Active edge" :
    edge.kind === "dashed" ? "Pending edge" :
    "Edge";
  return (
    <div className="inspector">
      <div className="insp-head">
        <div className="insp-kind">{kindLabel}</div>
        <div className="insp-title">{edge.label || (from?.title + " → " + to?.title)}</div>
        <div className="insp-id">{edge.from} → {edge.to}</div>
      </div>
      <div className="insp-section">
        <div className="insp-label">participants</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", fontFamily: "var(--mono)", fontSize: 11.5 }}>
          <span className="comm-partner"
                onClick={() => onSelectNode && onSelectNode(edge.from)}
                style={{ cursor: "pointer", color: "var(--text)" }}>
            {from ? from.title : edge.from}
          </span>
          <span style={{ color: "var(--text-4)" }}>→</span>
          <span className="comm-partner"
                onClick={() => onSelectNode && onSelectNode(edge.to)}
                style={{ cursor: "pointer", color: "var(--text)" }}>
            {to ? to.title : edge.to}
          </span>
        </div>
      </div>
      {edge.message && (
        <>
          <div className="insp-section">
            <div className="insp-label">sent at</div>
            <div className="insp-val">{edge.message.time}</div>
          </div>
          {edge.message.summary && (
            <div className="insp-section">
              <div className="insp-label">summary</div>
              <div className="insp-val" style={{ color: "var(--text)" }}>{edge.message.summary}</div>
            </div>
          )}
          <div className="insp-section">
            <div className="insp-label">message</div>
            <div className="code-block" style={{ color: "var(--text)" }}>{edge.message.body}</div>
          </div>
        </>
      )}
      {!edge.message && (
        <div className="insp-section">
          <div className="insp-val" style={{ color: "var(--text-3)" }}>
            No payload recorded for this edge.
          </div>
        </div>
      )}
    </div>
  );
}

window.AgentsPage = AgentsPage;
