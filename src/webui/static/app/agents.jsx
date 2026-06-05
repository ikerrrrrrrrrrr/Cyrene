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
  const panRef = useRefAg({ x: 20, y: 20 });
  const [viewport, setViewport] = useStateAg({ width: 1400, height: 900 });
  const wrapRef = useRefAg(null);
  const lastRoundRef = useRefAg(selectedRound);
  const selectionMemoryRef = useRefAg({});
  panRef.current = pan;

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
      const p = panRef.current;
      dragRef.current = { x: e.clientX, y: e.clientY, px: p.x, py: p.y };
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
  }, []);

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

  // Compute node rects: column-based grid layout.
  // Backend x values (40/320/600/900/1220/1540) are bucketed to columns via
  // Math.round(x/100), giving distinct col keys 0/3/6/9/12/15.
  // Within each column nodes stack along the cross axis; the first node of every
  // column is centered on a shared axis so the input→main→output chain runs
  // straight, while stacks fan out from there with real per-node heights.
  const nodeRects = useMemo(function () {
    const padding = 24;
    const colGap = 76;     // gap between columns (main flow direction)
    const rowGap = 30;     // gap between stacked siblings
    const subagentGap = 96; // gap between stacked subagent cards
    const staggerX = 116;  // zig-zag offset for subagents (horizontal mode)
    const staggerY = 64;   // zig-zag offset for subagents (vertical mode)
    const rects = {};

    function nW(n) {
      return n.kind === "main" ? 220 : n.kind === "subagent" ? 200 : n.kind === "tool" ? 180 : 200;
    }
    // Real rendered height: cards with a footer (subtitle/model) are taller.
    function nH(n) {
      return (n.kind === "main" || n.kind === "subagent" || n.kind === "tool") ? 106 : 76;
    }
    function gapBefore(n) {
      return n.kind === "subagent" ? subagentGap : rowGap;
    }

    function roundKeyForNode(node) {
      const m = String(node.id || "").match(/^(r\d+)_/);
      return m ? m[1] : "r0";
    }

    const byRound = new Map();
    for (var bi = 0; bi < nodes.length; bi++) {
      var bn = nodes[bi];
      var bk = roundKeyForNode(bn);
      if (!byRound.has(bk)) byRound.set(bk, []);
      byRound.get(bk).push(bn);
    }

    var sortedRounds = Array.from(byRound.entries())
      .map(function (e) { return { key: e[0], list: e[1] }; })
      .sort(function (a, b) { return Number(a.key.slice(1)) - Number(b.key.slice(1)); });

    var roundTop = 0;
    for (var ri = 0; ri < sortedRounds.length; ri++) {
      var roundNodes = sortedRounds[ri].list;
      if (!roundNodes.length) continue;

      var colMap = new Map();
      for (var j = 0; j < roundNodes.length; j++) {
        var rn = roundNodes[j];
        var ck = Math.round((Number(rn.x) || 0) / 100);
        if (!colMap.has(ck)) colMap.set(ck, []);
        colMap.get(ck).push(rn);
      }

      // Per column: sort by backend y, precompute max width/height.
      var cols = Array.from(colMap.entries())
        .sort(function (a, b) { return a[0] - b[0]; })
        .map(function (e) {
          var list = e[1].slice().sort(function (a, b) { return (Number(a.y) || 0) - (Number(b.y) || 0); });
          return {
            key: e[0],
            list: list,
            maxW: Math.max.apply(null, list.map(nW)),
            maxH: Math.max.apply(null, list.map(nH)),
            // zig-zag only when more than one subagent shares the column
            stagger: list.filter(function (n) { return n.kind === "subagent"; }).length > 1,
          };
        });

      var roundBottom = roundTop;

      if (orientation === "horizontal") {
        // Column x positions advance left→right; staggered columns reserve extra width.
        var curX = padding;
        for (var ci = 0; ci < cols.length; ci++) {
          cols[ci].pos = curX;
          curX += cols[ci].maxW + (cols[ci].stagger ? staggerX : 0) + colGap;
        }
        // Shared horizontal axis = center of the tallest first-row node.
        var axisH = 0;
        for (var ci = 0; ci < cols.length; ci++) axisH = Math.max(axisH, nH(cols[ci].list[0]));
        var axisCenter = roundTop + padding + axisH / 2;
        for (var ci = 0; ci < cols.length; ci++) {
          var col = cols[ci];
          var prevBottom = null;
          for (var ni = 0; ni < col.list.length; ni++) {
            var cn = col.list[ni];
            var h = nH(cn);
            var cy = ni === 0 ? (axisCenter - h / 2) : (prevBottom + gapBefore(cn));
            var sx = (col.stagger && cn.kind === "subagent" && ni % 2 === 1) ? staggerX : 0;
            rects[cn.id] = { x: col.pos + sx, y: cy, w: nW(cn), h: h };
            prevBottom = cy + h;
            roundBottom = Math.max(roundBottom, prevBottom);
          }
        }
      } else {
        // Column y positions advance top→bottom; staggered bands reserve extra height.
        var curY = roundTop + padding;
        for (var ci = 0; ci < cols.length; ci++) {
          cols[ci].pos = curY;
          curY += cols[ci].maxH + (cols[ci].stagger ? staggerY : 0) + colGap;
        }
        // Shared vertical axis = center of the widest first node.
        var axisW = 0;
        for (var ci = 0; ci < cols.length; ci++) axisW = Math.max(axisW, nW(cols[ci].list[0]));
        var axisCenterX = padding + axisW / 2;
        for (var ci = 0; ci < cols.length; ci++) {
          var col = cols[ci];
          var prevRight = null;
          for (var ni = 0; ni < col.list.length; ni++) {
            var cn = col.list[ni];
            var w = nW(cn);
            var cx = ni === 0 ? (axisCenterX - w / 2) : (prevRight + gapBefore(cn));
            var sy = (col.stagger && cn.kind === "subagent" && ni % 2 === 1) ? staggerY : 0;
            rects[cn.id] = { x: cx, y: col.pos + sy, w: w, h: nH(cn) };
            prevRight = cx + w;
            roundBottom = Math.max(roundBottom, col.pos + sy + nH(cn));
          }
        }
      }
      roundTop = roundBottom + 80;
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
          lp = { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 4 };
          if (Math.abs(x2 - x1) < 2) {
            path = "M " + x1 + " " + y1 + " L " + x2 + " " + y2;
          } else {
            var dy = Math.max(30, (y2 - y1) / 2);
            path = "M " + x1 + " " + y1 + " C " + x1 + " " + (y1 + dy) + ", " + x2 + " " + (y2 - dy) + ", " + x2 + " " + y2;
          }
        } else {
          var _x1 = a.x + a.w, _y1 = a.y + a.h / 2;
          var _x2 = b.x, _y2 = b.y + b.h / 2;
          if (b.x < a.x) { _x1 = a.x; _x2 = b.x + b.w; }
          lp = { x: (_x1 + _x2) / 2, y: (_y1 + _y2) / 2 - 6 };
          if (Math.abs(_y2 - _y1) < 2) {
            path = "M " + _x1 + " " + _y1 + " L " + _x2 + " " + _y2;
          } else {
            var dx = Math.max(40, (_x2 - _x1) / 2);
            path = "M " + _x1 + " " + _y1 + " C " + (_x1 + dx) + " " + _y1 + ", " + (_x2 - dx) + " " + _y2 + ", " + _x2 + " " + _y2;
          }
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
      if (!r) return false;
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
  const { t } = useI18n();
  if (node.kind === "main") {
    return (
      <>
        <div className="insp-section">
          <div className="insp-label">{t("agents.systemPrompt")}</div>
          <div className="code-block">{d.systemPrompt}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.reasoning")}</div>
          <div className="code-block" style={{ color: "var(--text-2)" }}>{d.reasoning}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.model")}</div>
          <div className="kv">
            <span className="k">{t("agents.model")}</span><span className="v">{d.model}</span>
            <span className="k">{t("agents.temp")}</span><span className="v">{d.temp}</span>
            <span className="k">{t("agents.tokensIn")}</span><span className="v">{d.tokensIn || "—"}</span>
            <span className="k">{t("agents.tokensOut")}</span><span className="v">{d.tokensOut || "—"}</span>
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
          <div className="insp-label">{t("agents.task")}</div>
          <div className="insp-val" style={{ color: "var(--text)" }}>{d.task}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.lineage")}</div>
          <div className="kv">
            <span className="k">{t("agents.spawnedBy")}</span><span className="v">{d.parent}</span>
            <span className="k">{t("agents.spawnedAt")}</span><span className="v">{d.spawnedAt || "—"}</span>
            <span className="k">{t("agents.model")}</span><span className="v">{d.model || "—"}</span>
            <span className="k">{t("agents.tokensIn")}</span><span className="v">{d.tokensIn ?? "—"}</span>
            <span className="k">{t("agents.tokensOut")}</span><span className="v">{d.tokensOut ?? "—"}</span>
          </div>
        </div>
        <CommsSection nodeId={node.id} flow={flow} nodeMap={nodeMap} onSelectNode={onSelectNode} />
        <div className="insp-section">
          <div className="insp-label">{t("agents.connectedNodes")}</div>
          <ConnectedList nodeId={node.id} flow={flow} nodeMap={nodeMap} />
        </div>
      </>
    );
  }
  if (node.kind === "tool") {
    return (
      <>
        <div className="insp-section">
          <div className="insp-label">{t("agents.tool")}</div>
          <div className="insp-val" style={{ color: "var(--magenta)" }}>{d.name}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.input")}</div>
          <pre className="code-block">{JSON.stringify(d.input, null, 2)}</pre>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.output")}</div>
          <div className="code-block">{d.output}</div>
        </div>
        <div className="insp-section">
          <div className="insp-label">{t("agents.duration")}</div>
          <div className="insp-val">{d.duration || "—"}</div>
        </div>
      </>
    );
  }
  if (node.kind === "output") {
    return (
      <div className="insp-section">
        <div className="insp-label">{t("agents.content")}</div>
        <div className="code-block" style={{ color: "var(--text)" }}>{d.content}</div>
      </div>
    );
  }
  // input
  return (
    <>
      <div className="insp-section">
        <div className="insp-label">{d.role || t("agents.input")}</div>
        <div className="insp-val" style={{ color: "var(--text)" }}>{d.text}</div>
      </div>
      <div className="insp-section">
        <div className="kv">
          <span className="k">{t("agents.tokensIn")}</span><span className="v">{d.tokens || "—"}</span>
          <span className="k">{t("agents.sentAt")}</span><span className="v">{d.time}</span>
        </div>
      </div>
    </>
  );
}

function InspectorIO({ node, d }) {
  const { t } = useI18n();
  return (
    <>
      <div className="insp-section">
        <div className="insp-label">{t("agents.input")}</div>
        <pre className="code-block">{
          node.kind === "tool" ? JSON.stringify(d.input, null, 2) :
          node.kind === "main" ? d.systemPrompt :
          node.kind === "subagent" ? d.task :
          node.kind === "input" ? d.text :
          node.kind === "output" ? (d.content || "—") :
          "—"
        }</pre>
      </div>
      <div className="insp-section">
        <div className="insp-label">{t("agents.output")}</div>
        <pre className="code-block">{
          node.kind === "tool" ? (d.output || "—") :
          node.kind === "main" ? (d.reasoning || "—") :
          node.kind === "subagent" ? (d.result || d.reasoning || "—") :
          node.kind === "output" ? (d.content || "—") :
          node.kind === "input" ? (d.text || "—") :
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
  const { t } = useI18n();
  const comms = flow.edges
    .map(function (e, i) { return { ...e, idx: i }; })
    .filter(function (e) { return e.kind === "comm" && e.message && (e.from === nodeId || e.to === nodeId); });
  if (comms.length === 0) return null;
  return (
    <div className="insp-section">
      <div className="insp-label">{t("agents.communications")} · {comms.length}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {comms.map(function (e) {
          const outgoing = e.from === nodeId;
          const partnerId = outgoing ? e.to : e.from;
          const partner = nodeMap.get(partnerId);
          return (
            <div key={e.idx} className="comm-card">
              <div className="comm-card-head">
                <span className={"comm-dir " + (outgoing ? "out" : "in")}>
                  {outgoing ? t("agents.out") : t("agents.in")}
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
    edge.kind === "comm" ? t("agents.Communication") :
    edge.kind === "active" ? t("agents.ActiveEdge") :
    edge.kind === "dashed" ? t("agents.PendingEdge") :
    "Edge";
  const weight = Number(edge.weight) || 1;
  const msgType = edge.message && edge.message.msg_type ? edge.message.msg_type : "chat";
  const priority = edge.message && edge.message.priority ? edge.message.priority : "normal";
  const typeLabel =
    msgType === "progress" ? t("agents.commTypeProgress") :
    msgType === "question" ? t("agents.commTypeQuestion") :
    msgType === "finding"  ? t("agents.commTypeFinding")  :
    msgType === "result"   ? t("agents.commTypeResult")   :
    msgType === "ack"      ? t("agents.commTypeAck")      :
    t("agents.commTypeChat");

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
        <div className="insp-label">{t("agents.participants")}</div>
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
            ← {t("agents.commJumpSender")}
          </span>
          <span className="comm-jump-link" onClick={function () { onSelectNode && onSelectNode(edge.to); }}>
            {t("agents.commJumpReceiver")} →
          </span>
        </div>
      </div>

      <div className="insp-section">
        <div className="kv">
          <span className="k">{t("agents.commType")}</span>
          <span className="v" style={{ color: msgType === "finding" ? "var(--accent)" : msgType === "question" ? "var(--warn)" : "var(--text)" }}>{typeLabel}</span>
          <span className="k">{t("agents.commWeight")}</span>
          <span className="v">{weight} {t("agents.commMessages")}</span>
          {priority === "high" && (
            <>
              <span className="k">{t("agents.commPriority")}</span>
              <span className="v" style={{ color: "var(--warn)" }}>HIGH</span>
            </>
          )}
        </div>
      </div>

      {allMessages.length === 0 && (
        <div className="insp-section">
          <div className="insp-val" style={{ color: "var(--text-3)" }}>
            {t("agents.commNoMessages")}
          </div>
        </div>
      )}

      {allMessages.length > 0 && (
        <div className="insp-section">
          <div className="insp-label">
            {allMessages.length === 1
              ? t("agents.message")
              : t("agents.commAllMessages") + " (" + allMessages.length + ")"}
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
                      {msg.from === (from && from.title) ? t("agents.out") : t("agents.in")}
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
