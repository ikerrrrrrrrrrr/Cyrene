// Agents page — flowchart with nodes + edges + inspector
const { useState: useStateAg, useRef: useRefAg, useEffect: useEffectAg, useMemo } = React;

const EMPTY_FLOW = { nodes: [], edges: [] };

function staggerLevel(index) {
  if (index <= 0) return 0;
  const magnitude = Math.ceil(index / 2);
  return index % 2 === 1 ? magnitude : -magnitude;
}

function flowRoundKey(id) {
  const match = String(id || "").match(/^(r\d+)_/);
  return match ? match[1] : "r0";
}

function compareRoundKeys(a, b) {
  return Number(String(a).slice(1) || 0) - Number(String(b).slice(1) || 0);
}

function roundStatus(nodes, fallback) {
  const statuses = nodes.map((node) => node.status).filter(Boolean);
  if (statuses.includes("running")) return "running";
  if (statuses.includes("err")) return "err";
  if (statuses.includes("queued")) return "queued";
  if (statuses.includes("done")) return "done";
  return fallback || "queued";
}

function roundPreview(nodes) {
  const inputNode = nodes.find((node) => node.kind === "input" && node.detail && node.detail.text);
  if (inputNode) return String(inputNode.detail.text).replace(/\s+/g, " ").trim();
  const outputNode = nodes.find((node) => node.kind === "output" && node.detail && node.detail.content);
  if (outputNode) return String(outputNode.detail.content).replace(/\s+/g, " ").trim();
  return nodes[0]?.title || "—";
}

function roundName(nodes, fallback) {
  const inputNode = nodes.find((node) => node.kind === "input" && node.title);
  return inputNode && String(inputNode.title).trim() ? String(inputNode.title).trim() : fallback;
}

function edgeSelectionKey(edge) {
  const msg = edge && edge.message ? edge.message : {};
  return [
    edge && edge.kind ? edge.kind : "",
    edge && edge.from ? edge.from : "",
    edge && edge.to ? edge.to : "",
    edge && edge.label ? edge.label : "",
    msg.ts || "",
    msg.body || "",
  ].join("::");
}

function buildAgentRounds(session) {
  const rounds = [];
  const flow = session && session.flow ? session.flow : EMPTY_FLOW;
  const sessionNodes = Array.isArray(flow.nodes) ? flow.nodes : [];
  const sessionEdges = Array.isArray(flow.edges) ? flow.edges : [];
  if (!sessionNodes.length) return rounds;

  const buckets = new Map();
  function ensureBucket(key) {
    if (!buckets.has(key)) buckets.set(key, { key, nodes: [], edges: [] });
    return buckets.get(key);
  }

  sessionNodes.forEach(function (node) {
    ensureBucket(flowRoundKey(node.id)).nodes.push(node);
  });

  sessionEdges.forEach(function (edge) {
    const key = flowRoundKey(edge.from || edge.to);
    ensureBucket(key).edges.push(edge);
  });

  Array.from(buckets.values())
    .sort(function (a, b) { return compareRoundKeys(a.key, b.key); })
    .reverse()
    .forEach(function (bucket) {
      const nodeIds = new Set(bucket.nodes.map((node) => node.id));
      const edges = bucket.edges.filter(function (edge) {
        return nodeIds.has(edge.from) && nodeIds.has(edge.to);
      });
      const roundNumber = Number(bucket.key.slice(1) || 0) + 1;
      const fallbackLabel = (window.t && window.t("agents.roundN", {n: roundNumber})) || ("Round " + roundNumber);
      rounds.push({
        id: (session && session.id ? session.id : "session") + ":" + bucket.key,
        key: bucket.key,
        label: roundName(bucket.nodes, fallbackLabel),
        fallbackLabel,
        preview: roundPreview(bucket.nodes),
        status: roundStatus(bucket.nodes, session && session.status),
        sessionId: session && session.id ? session.id : "",
        sessionTitle: session && session.title ? session.title : "No session selected",
        sessionStarted: session && session.started ? session.started : "—",
        sessionDuration: session && session.dur ? session.dur : "—",
        flow: { nodes: bucket.nodes, edges },
      });
    });

  return rounds;
}

function AgentsPage({ orientation = "horizontal", selectedSessionId, rightSidebarCollapsed = false }) {
  const dv = useDataVersion();
  const { t } = useI18n();
  const activeSession = useMemo(() => {
    const sessions = DATA.sessions || [];
    const preferredId = selectedSessionId;
    return sessions.find((session) => session.id === preferredId) || sessions[0] || null;
  }, [dv, selectedSessionId]);
  const rounds = useMemo(() => buildAgentRounds(activeSession), [dv, activeSession && activeSession.id]);
  const [selectedRound, setSelectedRound] = useStateAg(rounds[0]?.id || "");
  const round = rounds.find((item) => item.id === selectedRound) || rounds[0] || {
    id: "",
    label: "Round 1",
    preview: "—",
    status: "queued",
    sessionId: "",
    sessionTitle: "No rounds yet",
    sessionStarted: "—",
    sessionDuration: "—",
    flow: EMPTY_FLOW,
  };
  const [selectedNode, setSelectedNode] = useStateAg(round.flow.nodes[0]?.id || null);
  const [selectedEdgeKey, setSelectedEdgeKey] = useStateAg(null);
  const [zoom, setZoom] = useStateAg(0.85);
  const [pan, setPan] = useStateAg({ x: 20, y: 20 });
  const [viewport, setViewport] = useStateAg({ width: 1400, height: 900 });
  const wrapRef = useRefAg(null);
  const lastRoundRef = useRefAg(selectedRound);
  const selectionMemoryRef = useRefAg({});

  function selectNodeForRound(nodeId) {
    selectionMemoryRef.current[selectedRound] = { nodeId, edgeKey: null };
    setSelectedNode(nodeId);
    setSelectedEdgeKey(null);
  }

  function selectEdgeForRound(edgeKey) {
    selectionMemoryRef.current[selectedRound] = { nodeId: null, edgeKey };
    setSelectedEdgeKey(edgeKey);
    setSelectedNode(null);
  }

  useEffectAg(() => {
    if (!rounds.some((item) => item.id === selectedRound)) {
      setSelectedRound(rounds[0]?.id || "");
    }
  }, [rounds, selectedRound]);

  useEffectAg(() => {
    if (lastRoundRef.current === selectedRound) return;
    lastRoundRef.current = selectedRound;
    const saved = selectionMemoryRef.current[selectedRound];
    if (saved && saved.edgeKey && round.flow.edges.some((edge) => edgeSelectionKey(edge) === saved.edgeKey)) {
      setSelectedEdgeKey(saved.edgeKey);
      setSelectedNode(null);
      return;
    }
    if (saved && saved.nodeId && round.flow.nodes.some((node) => node.id === saved.nodeId)) {
      setSelectedNode(saved.nodeId);
      setSelectedEdgeKey(null);
      return;
    }
    setSelectedNode(round.flow.nodes[0]?.id || null);
    setSelectedEdgeKey(null);
  }, [selectedRound, round.flow.nodes]);

  // Reset pan when orientation changes
  useEffectAg(() => {
    setPan({ x: 20, y: 20 });
  }, [orientation]);

  useEffectAg(() => {
    setPan({ x: 20, y: 20 });
    setZoom(0.85);
  }, [selectedRound]);

  useEffectAg(() => {
    const saved = selectionMemoryRef.current[selectedRound];
    const savedEdgeKey = saved && saved.edgeKey &&
      round.flow.edges.some((edge) => edgeSelectionKey(edge) === saved.edgeKey)
      ? saved.edgeKey
      : null;
    const savedNodeId = saved && saved.nodeId &&
      round.flow.nodes.some((node) => node.id === saved.nodeId)
      ? saved.nodeId
      : null;
    if (savedEdgeKey && (selectedEdgeKey !== savedEdgeKey || selectedNode !== null)) {
      setSelectedEdgeKey(savedEdgeKey);
      setSelectedNode(null);
      return;
    }
    if (savedNodeId && (selectedNode !== savedNodeId || selectedEdgeKey !== null)) {
      setSelectedNode(savedNodeId);
      setSelectedEdgeKey(null);
      return;
    }
    const nodeExists = selectedNode && round.flow.nodes.some((node) => node.id === selectedNode);
    const edgeExists = selectedEdgeKey && round.flow.edges.some((edge) => edgeSelectionKey(edge) === selectedEdgeKey);
    if (nodeExists || edgeExists) return;
    if (selectedEdgeKey) {
      setSelectedEdgeKey(null);
      return;
    }
    const firstNodeId = round.flow.nodes[0]?.id || null;
    if (selectedNode !== firstNodeId) {
      setSelectedNode(firstNodeId);
    }
  }, [round.flow.nodes, round.flow.edges, selectedNode, selectedEdgeKey]);

  useEffectAg(() => {
    const el = wrapRef.current;
    if (!el) return;
    let timer = null;
    function measure() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        setViewport({
          width: el.clientWidth || 1400,
          height: el.clientHeight || 900,
        });
      }, 150);
    }
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return function () {
        window.removeEventListener("resize", measure);
        if (timer) clearTimeout(timer);
      };
    }
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return function () {
      ro.disconnect();
      if (timer) clearTimeout(timer);
    };
  }, []);

  const { nodes, edges } = round.flow;

  // O(1) node lookup map — eliminates O(n*m) find() calls in sub-components
  const nodeMap = useMemo(function () {
    const m = new Map();
    nodes.forEach(function (n) { m.set(n.id, n); });
    return m;
  }, [nodes]);

  const sel = nodeMap.get(selectedNode);
  const selectedEdge = selectedEdgeKey
    ? edges.find(function (edge) { return edgeSelectionKey(edge) === selectedEdgeKey; }) || null
    : null;

  // Drag-to-pan
  const dragRef = useRefAg(null);
  useEffectAg(function () {
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
    return function () {
      el.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [pan]);

  // Wheel: cmd/ctrl = zoom, plain wheel = scroll/pan, shift+wheel = horizontal pan
  function onWheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      setZoom(function (z) { return Math.max(0.4, Math.min(1.6, z + (e.deltaY < 0 ? 0.05 : -0.05))); });
      return;
    }
    e.preventDefault();
    setPan(function (p) { return {
      x: p.x - (e.shiftKey ? e.deltaY : e.deltaX),
      y: p.y - (e.shiftKey ? 0 : e.deltaY),
    }; });
  }

  // Compute node rects with wrapping:
  // horizontal = preserve backend lane layout per round
  // vertical   = transpose the same layout for top-to-bottom reading
  const nodeRects = useMemo(function () {
    const padding = 20;
    const roundGapY = 120;
    const rects = {};

    function nodeSize(n) {
      return {
        w: n.kind === "main" ? 220 : n.kind === "subagent" ? 200 : n.kind === "tool" ? 180 : n.kind === "output" ? 200 : 200,
        h: 86,
      };
    }

    function roundKeyForNode(node) {
      const m = String(node.id || "").match(/^(r\d+)_/);
      return m ? m[1] : "r0";
    }

    function layoutRound(roundNodes, topOffset) {
      if (!roundNodes.length) return topOffset;
      const minX = Math.min.apply(null, roundNodes.map(function (n) { return Number(n.x) || 0; }));
      const minY = Math.min.apply(null, roundNodes.map(function (n) { return Number(n.y) || 0; }));
      const subagentOffsets = new Map();
      roundNodes
        .filter(function (node) { return node.kind === "subagent"; })
        .slice()
        .sort(function (a, b) {
          if ((a.y || 0) !== (b.y || 0)) return (a.y || 0) - (b.y || 0);
          return (a.x || 0) - (b.x || 0);
        })
        .forEach(function (node, index) {
          subagentOffsets.set(node.id, staggerLevel(index));
        });
      let roundBottom = topOffset;

      for (var i = 0; i < roundNodes.length; i++) {
        var n = roundNodes[i];
        var _a = nodeSize(n), w = _a.w, h = _a.h;
        var relX = (Number(n.x) || 0) - minX;
        var relY = (Number(n.y) || 0) - minY;
        if (orientation === "horizontal") relX *= 0.68;
        if (orientation === "vertical") relY *= 0.68;
        var x = orientation === "vertical" ? padding + relY : padding + relX;
        var y = orientation === "vertical" ? topOffset + padding + relX : topOffset + padding + relY;
        if (n.kind === "subagent") {
          var lane = subagentOffsets.get(n.id) || 0;
          if (orientation === "vertical") {
            x += lane * 78;
            y += Math.abs(lane) * 26;
          } else {
            x += Math.abs(lane) * 30;
            y += lane * 72;
          }
        }
        rects[n.id] = { x: x, y: y, w: w, h: h };
        roundBottom = Math.max(roundBottom, y + h);
      }

      return roundBottom;
    }

    var roundsMap = new Map();
    for (var j = 0; j < nodes.length; j++) {
      var n = nodes[j];
      var roundKey = roundKeyForNode(n);
      if (!roundsMap.has(roundKey)) roundsMap.set(roundKey, []);
      roundsMap.get(roundKey).push(n);
    }

    var sortedRounds = Array.from(roundsMap.entries())
      .map(function (entry) { return { key: entry[0], list: entry[1] }; })
      .sort(function (a, b) { return Number(a.key.slice(1)) - Number(b.key.slice(1)); });

    var roundTop = 0;
    for (var k = 0; k < sortedRounds.length; k++) {
      var roundBottom = layoutRound(sortedRounds[k].list, roundTop);
      roundTop = roundBottom + roundGapY;
    }
    return rects;
  }, [nodes, orientation]);

  const canvasSize = useMemo(function () {
    var maxX = 0, maxY = 0;
    for (var id in nodeRects) {
      var r = nodeRects[id];
      maxX = Math.max(maxX, r.x + r.w);
      maxY = Math.max(maxY, r.y + r.h);
    }
    return { w: maxX + 80, h: maxY + 80 };
  }, [nodeRects]);

  // Precompute edge paths, labels, and CSS classes (cached)
  const edgeData = useMemo(function () {
    return edges.map(function (e) {
      var a = nodeRects[e.from];
      var b = nodeRects[e.to];
      var path = "";
      var lp = { x: 0, y: 0 };
      if (a && b) {
        if (orientation === "vertical") {
          var x1 = a.x + a.w / 2, y1 = a.y + a.h;
          var x2 = b.x + b.w / 2, y2 = b.y;
          if (b.y < a.y) { y1 = a.y; y2 = b.y + b.h; }
          var dy = Math.max(30, (y2 - y1) / 2);
          path = "M " + x1 + " " + y1 + " C " + x1 + " " + (y1 + dy) + ", " + x2 + " " + (y2 - dy) + ", " + x2 + " " + y2;
          lp = { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 4 };
        } else {
          var _x1 = a.x + a.w, _y1 = a.y + a.h / 2;
          var _x2 = b.x, _y2 = b.y + b.h / 2;
          if (b.x < a.x) { _x1 = a.x; _x2 = b.x + b.w; }
          var dx = Math.max(40, (_x2 - _x1) / 2);
          path = "M " + _x1 + " " + _y1 + " C " + (_x1 + dx) + " " + _y1 + ", " + (_x2 - dx) + " " + _y2 + ", " + _x2 + " " + _y2;
          lp = { x: (_x1 + _x2) / 2, y: (_y1 + _y2) / 2 - 6 };
        }
      }
      var edgeKey = edgeSelectionKey(e);
      var weight = Number(e.weight) || 1;
      var weightCls = e.kind === "comm" && weight >= 2 ? " weight-" + Math.min(weight, 4) : "";
      var priorityCls = e.kind === "comm" && e.message && e.message.priority === "high" ? " priority-high" : "";
      var rawTs = e.message && e.message.raw_timestamp ? e.message.raw_timestamp : "";
      var recentCls = "";
      if (e.kind === "comm" && rawTs) {
        try {
          var msgDate = new Date(rawTs);
          if (!isNaN(msgDate.getTime()) && (Date.now() - msgDate.getTime()) < 30000) recentCls = " recent";
        } catch (_) { /* skip */ }
      }
      var cls =
        "edge " +
        (e.kind === "active" ? "active" : e.kind === "dashed" ? "dashed" : e.kind === "comm" ? "comm" : "") +
        weightCls + priorityCls + recentCls +
        (selectedEdgeKey === edgeKey ? " selected" : "");
      var marker =
        e.kind === "active" ? "url(#arrow-active)" : e.kind === "comm" ? "url(#arrow-comm)" : "url(#arrow)";
      var clickable = e.kind === "comm" && e.message;
      return { edge: e, edgeKey: edgeKey, path: path, lp: lp, cls: cls, marker: marker, clickable: clickable };
    });
  }, [edges, nodeRects, orientation, selectedEdgeKey]);

  // Viewport culling: only render nodes/edges in the visible area
  const visibleNodes = useMemo(function () {
    var vpLeft = -pan.x / zoom;
    var vpTop = -pan.y / zoom;
    var vpRight = vpLeft + viewport.width / zoom;
    var vpBottom = vpTop + viewport.height / zoom;
    var buffer = 300;
    return nodes.filter(function (n) {
      var r = nodeRects[n.id];
      if (!r) return true;
      return !(r.x + r.w < vpLeft - buffer || r.x > vpRight + buffer ||
               r.y + r.h < vpTop - buffer || r.y > vpBottom + buffer);
    });
  }, [nodes, nodeRects, pan.x, pan.y, zoom, viewport.width, viewport.height]);

  const visibleNodeIds = useMemo(function () {
    var s = new Set();
    for (var i = 0; i < visibleNodes.length; i++) { s.add(visibleNodes[i].id); }
    return s;
  }, [visibleNodes]);

  const visibleEdgeData = useMemo(function () {
    return edgeData.filter(function (ed) {
      return visibleNodeIds.has(ed.edge.from) || visibleNodeIds.has(ed.edge.to);
    });
  }, [edgeData, visibleNodeIds]);

  return (
    <div className={"agents-layout" + (rightSidebarCollapsed ? " right-collapsed" : "")}>
      <RoundsList rounds={rounds} selected={selectedRound} onSelect={setSelectedRound} />

      <div className="canvas-wrap" ref={wrapRef} onWheel={onWheel}>
        <div className="canvas-context">
          <div className="canvas-context-round">{round.label}</div>
          <div className="canvas-context-session">{round.sessionTitle}</div>
          <div className="canvas-context-meta">
            <span>{round.sessionId || "—"}</span>
            <span>· {round.sessionStarted || "—"}</span>
            <span>· {round.sessionDuration || "—"}</span>
          </div>
        </div>
        <div className="canvas-toolbar">
          <button onClick={function () { setZoom(function (z) { return Math.min(1.6, z + 0.1); }); }}>＋</button>
          <button onClick={function () { setZoom(function (z) { return Math.max(0.4, z - 0.1); }); }}>−</button>
          <button onClick={function () { setZoom(0.85); setPan({ x: 20, y: 20 }); }}>⊕</button>
          <span style={{
            color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 10,
            alignSelf: "center", padding: "0 6px"
          }}>
            {Math.round(zoom * 100)}%
          </span>
        </div>

        <div className="flow" style={{
          transform: "translate(" + pan.x + "px, " + pan.y + "px) scale(" + zoom + ")"
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
            {visibleEdgeData.map(function (ed) {
              return (
                <MemoEdge key={ed.edgeKey} ed={ed} onSelect={function () { selectEdgeForRound(ed.edgeKey); }} />
              );
            })}
          </svg>

          {visibleNodes.map(function (n) {
            return (
              <FlowNode key={n.id} node={n}
                        pos={nodeRects[n.id]}
                        selected={n.id === selectedNode}
                        onClick={function () { selectNodeForRound(n.id); }} />
            );
          })}
        </div>

        <div className="canvas-legend">
          <div className="legend-row">
            <span className="legend-item">
              <span className="legend-swatch" style={{ background: "var(--accent)" }}></span>
              {t("agents.mainAgent")}
            </span>
            <span className="legend-item">
              <span className="legend-swatch" style={{ background: "var(--info)" }}></span>
              {t("agents.subagent")}
            </span>
            <span className="legend-item">
              <span className="legend-swatch" style={{ background: "var(--magenta)" }}></span>
              {t("agents.toolCall")}
            </span>
            <span className="legend-item">
              <span className="legend-swatch" style={{ background: "var(--warn)" }}></span>
              {t("agents.output")}
            </span>
          </div>
          <div className="legend-row legend-tips">
            {t("agents.canvasHelp")}
          </div>
        </div>

      </div>

      <Inspector node={sel} edge={selectedEdge}
                 flow={round.flow} nodeMap={nodeMap}
                 onSelectNode={function (id) { selectNodeForRound(id); }} />
    </div>
  );
}

function RoundsList({ rounds, selected, onSelect }) {
  const { t } = useI18n();
  return (
    <div className="runs-list">
      <div className="side-head" style={{ padding: "14px 14px 10px", margin: 0 }}>
        {t("agents.rounds")} <span className="count">{rounds.length}</span>
      </div>
      {rounds.length === 0 && (
        <div className="runs-empty">{t("agents.noRounds")}</div>
      )}
      {rounds.map(function (round) {
        return (
          <div key={round.id}
               className={"run-item " + (round.id === selected ? "active" : "")}
               onClick={function () { onSelect(round.id); }}>
            <div className={"sa-dot " + round.status}></div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="run-title">{round.label}</div>
              <div className="run-meta">
                <span>{round.sessionTitle}</span>
                <span>· {round.sessionStarted}</span>
              </div>
              <div className="run-preview">{round.preview}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

var MemoEdge = React.memo(function MemoEdge({ ed, onSelect }) {
  return (
    <g style={{ pointerEvents: ed.clickable ? "auto" : "none" }}>
      {ed.clickable && (
        <path d={ed.path} stroke="transparent" strokeWidth="14" fill="none"
              style={{ cursor: "pointer" }}
              onClick={onSelect} />
      )}
      <path d={ed.path} className={ed.cls} markerEnd={ed.marker}
            style={{ cursor: ed.clickable ? "pointer" : "default" }}
            onClick={ed.clickable ? onSelect : undefined} />
      {ed.edge.label && (
        <text className={"edge-label" + (ed.edge.kind === "comm" ? " comm" : "")}
              x={ed.lp.x} y={ed.lp.y} textAnchor="middle"
              style={{ cursor: ed.clickable ? "pointer" : "default" }}
              onClick={ed.clickable ? onSelect : undefined}>
          {ed.edge.label}
        </text>
      )}
    </g>
  );
});

var FlowNode = React.memo(function FlowNode({ node, pos, selected, onClick }) {
  const { t } = useI18n();
  const cls = "node " + node.kind + (selected ? " selected" : "");
  const kindLabel =
    node.kind === "main" ? t("agents.mainAgent") :
    node.kind === "subagent" ? t("agents.subagent") :
    node.kind === "tool" ? t("agents.tool") :
    node.kind === "output" ? t("agents.output") :
    t("agents.input");
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
});

function Inspector({ node, edge, flow, nodeMap, onSelectNode }) {
  const { t } = useI18n();
  const [tab, setTab] = useStateAg("details");
  if (edge) {
    return <EdgeInspector edge={edge} nodeMap={nodeMap} onSelectNode={onSelectNode} />;
  }
  if (!node) {
    return (
      <div className="inspector">
        <div className="empty-insp">{t("agents.clickToInspect")}</div>
      </div>
    );
  }

  const d = node.detail || {};
  const kindLabel =
    node.kind === "main" ? t("agents.MainAgent") :
    node.kind === "subagent" ? t("agents.Subagent") :
    node.kind === "tool" ? t("agents.ToolCall") :
    node.kind === "output" ? t("agents.Output") :
    t("agents.Input");

  return (
    <div className="inspector">
      <div className="insp-head">
        <div className="insp-kind">{kindLabel}</div>
        <div className="insp-title">{node.title}</div>
        <div className="insp-id">{node.id} · {node.status || "—"}</div>
      </div>
      <div className="insp-tabs">
        <div className={"insp-tab " + (tab === "details" ? "active" : "")} onClick={function () { setTab("details"); }}>{t("agents.details")}</div>
        <div className={"insp-tab " + (tab === "io" ? "active" : "")} onClick={function () { setTab("io"); }}>{t("agents.inputOutput")}</div>
        <div className={"insp-tab " + (tab === "raw" ? "active" : "")} onClick={function () { setTab("raw"); }}>{t("agents.raw")}</div>
      </div>

      {tab === "details" && <InspectorDetails node={node} d={d} flow={flow} nodeMap={nodeMap} onSelectNode={onSelectNode} />}
      {tab === "io" && <InspectorIO node={node} d={d} />}
      {tab === "raw" && (
        <div className="insp-section" style={{ flex: 1 }}>
          <div className="insp-label">{t("agents.json")}</div>
          <pre className="code-block json">{JSON.stringify(node, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function InspectorDetails({ node, d, flow, nodeMap, onSelectNode }) {
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
        <CommsSection nodeId={node.id} flow={flow} nodeMap={nodeMap} onSelectNode={onSelectNode} />
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
        <CommsSection nodeId={node.id} flow={flow} nodeMap={nodeMap} onSelectNode={onSelectNode} />
        <div className="insp-section">
          <div className="insp-label">connected nodes</div>
          <ConnectedList nodeId={node.id} flow={flow} nodeMap={nodeMap} />
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

function ConnectedList({ nodeId, flow, nodeMap }) {
  const conn = flow.edges.filter(function (e) { return e.from === nodeId || e.to === nodeId; });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {conn.map(function (e, i) {
        const other = e.from === nodeId ? e.to : e.from;
        const otherNode = nodeMap.get(other);
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

function CommsSection({ nodeId, flow, nodeMap, onSelectNode }) {
  const comms = flow.edges
    .map(function (e, i) { return { ...e, idx: i }; })
    .filter(function (e) { return e.kind === "comm" && e.message && (e.from === nodeId || e.to === nodeId); });
  if (comms.length === 0) return null;
  return (
    <div className="insp-section">
      <div className="insp-label">communications · {comms.length}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {comms.map(function (e) {
          const outgoing = e.from === nodeId;
          const partnerId = outgoing ? e.to : e.from;
          const partner = nodeMap.get(partnerId);
          return (
            <div key={e.idx} className="comm-card">
              <div className="comm-card-head">
                <span className={"comm-dir " + (outgoing ? "out" : "in")}>
                  {outgoing ? "out →" : "← in"}
                </span>
                <span className="comm-partner"
                      onClick={function () { onSelectNode && onSelectNode(partnerId); }}
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

function EdgeInspector({ edge, nodeMap, onSelectNode }) {
  const { t } = useI18n();
  const from = nodeMap.get(edge.from);
  const to = nodeMap.get(edge.to);
  const kindLabel =
    edge.kind === "comm" ? (t && t("agents.Communication") || "Communication") :
    edge.kind === "active" ? (t && t("agents.ActiveEdge") || "Active edge") :
    edge.kind === "dashed" ? (t && t("agents.PendingEdge") || "Pending edge") :
    "Edge";
  const weight = Number(edge.weight) || 1;
  const msgType = edge.message && edge.message.msg_type ? edge.message.msg_type : "chat";
  const priority = edge.message && edge.message.priority ? edge.message.priority : "normal";
  const typeLabel =
    msgType === "progress" ? (t && t("agents.commTypeProgress") || "Progress") :
    msgType === "question" ? (t && t("agents.commTypeQuestion") || "Question") :
    msgType === "finding"  ? (t && t("agents.commTypeFinding")  || "Finding")  :
    msgType === "result"   ? (t && t("agents.commTypeResult")   || "Result")   :
    msgType === "ack"      ? (t && t("agents.commTypeAck")      || "Ack")      :
    (t && t("agents.commTypeChat") || "Chat");

  // Collect all messages on this edge
  const allMessages = Array.isArray(edge.messages) ? edge.messages : (
    edge.message ? [{
      from: (from && from.title) || edge.from,
      to: (to && to.title) || edge.to,
      body: edge.message.body || "",
      label: msgType,
      time: edge.message.time || "—",
      summary: edge.message.summary || "",
      priority: priority,
      source: edge.message.source || "",
    }] : []
  );

  return (
    <div className="inspector">
      <div className="insp-head">
        <div className="insp-kind">{kindLabel}</div>
        <div className="insp-title">{edge.label || (from?.title + " → " + to?.title)}</div>
        <div className="insp-id">{edge.from} → {edge.to}</div>
      </div>

      <div className="insp-section">
        <div className="insp-label">{t && t("agents.participants") || "participants"}</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", fontFamily: "var(--mono)", fontSize: 11.5, flexWrap: "wrap" }}>
          <span className="comm-partner"
                onClick={function () { onSelectNode && onSelectNode(edge.from); }}
                style={{ cursor: "pointer", color: "var(--text)" }}>
            {from ? from.title : edge.from}
          </span>
          <span style={{ color: "var(--text-4)" }}>→</span>
          <span className="comm-partner"
                onClick={function () { onSelectNode && onSelectNode(edge.to); }}
                style={{ cursor: "pointer", color: "var(--text)" }}>
            {to ? to.title : edge.to}
          </span>
        </div>
        <div className="comm-jump-links">
          <span className="comm-jump-link" onClick={function () { onSelectNode && onSelectNode(edge.from); }}>
            ← {t && t("agents.commJumpSender") || "Jump to sender"}
          </span>
          <span className="comm-jump-link" onClick={function () { onSelectNode && onSelectNode(edge.to); }}>
            {t && t("agents.commJumpReceiver") || "Jump to receiver"} →
          </span>
        </div>
      </div>

      <div className="insp-section">
        <div className="kv">
          <span className="k">{t && t("agents.commType") || "type"}</span>
          <span className="v" style={{ color: msgType === "finding" ? "var(--accent)" : msgType === "question" ? "var(--warn)" : "var(--text)" }}>{typeLabel}</span>
          <span className="k">{t && t("agents.commWeight") || "weight"}</span>
          <span className="v">{weight} {t && t("agents.commMessages") || "messages"}</span>
          {priority === "high" && (
            <>
              <span className="k">{t && t("agents.commPriority") || "priority"}</span>
              <span className="v" style={{ color: "var(--warn)" }}>HIGH</span>
            </>
          )}
        </div>
      </div>

      {allMessages.length === 0 && (
        <div className="insp-section">
          <div className="insp-val" style={{ color: "var(--text-3)" }}>
            {t && t("agents.commNoMessages") || "No messages between these agents."}
          </div>
        </div>
      )}

      {allMessages.length > 0 && (
        <div className="insp-section">
          <div className="insp-label">
            {allMessages.length === 1
              ? (t && t("agents.message") || "message")
              : (t && t("agents.commAllMessages") || "All messages") + " (" + allMessages.length + ")"}
          </div>
          <div className="comm-thread">
            {allMessages.map(function (msg, idx) {
              var mType = msg.label || "chat";
              var mPriority = msg.priority || "normal";
              var clsCard = "comm-card type-" + mType + (mPriority === "high" ? " priority-high" : "");
              return (
                <div key={idx} className={clsCard}>
                  <div className="comm-card-head">
                    <span className={"comm-dir " + (msg.from === (from && from.title) ? "out" : "in")}>
                      {msg.from === (from && from.title) ? (t && t("agents.out") || "out →") : (t && t("agents.in") || "← in")}
                    </span>
                    <span className="comm-time">{msg.time}</span>
                  </div>
                  {msg.summary && (
                    <div className="comm-summary">{msg.summary}</div>
                  )}
                  <div className="comm-body">{msg.body}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
window.AgentsPage = AgentsPage;
