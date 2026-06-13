// Workbench Memory page.
//
// Fully independent from the legacy memory UI (`compiled/memory.js`, used by the
// old `--agent` shell). It has its own model, components and styles, and talks
// ONLY to the workspace-scoped `/api/workbench/memory/*` backend, passing the
// active project id as the `workspace` so every project/workspace owns a
// separate memory store. Cross-workspace memory is intentionally not surfaced.
(function () {
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useMemo = React.useMemo;
  var useRef = React.useRef;
  var h = React.createElement;

  // ── date helpers ─────────────────────────────────────────────────────
  function parseDate(s) {
    if (!s) return null;
    var d = new Date(String(s).length <= 10 ? String(s) + "T00:00:00" : s);
    return isNaN(d.getTime()) ? null : d;
  }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  // Relative label for list cards: 今天 / 昨天 / MM-DD / YYYY-MM-DD.
  function formatRel(s) {
    var d = parseDate(s);
    if (!d) return "—";
    var now = new Date();
    var startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var startThat = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var days = Math.round((startToday - startThat) / 86400000);
    if (days === 0) return "今天";
    if (days === 1) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }
  function formatFull(s) {
    var d = parseDate(s);
    if (!d) return "—";
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  // ── classification metadata (icon + tone per category) ───────────────
  function svg(props, children) {
    return h("svg", Object.assign({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" }, props), children);
  }
  var ICON = {
    all: function (s) { return svg({ width: s, height: s, fill: "currentColor", stroke: "none" }, h("path", { d: "M12 3.6 14 9.4 20 11l-6 1.6L12 18l-2-5.4L4 11l6-1.6Z" })); },
    preference: function (s) { return svg({ width: s, height: s, fill: "currentColor", stroke: "none" }, h("path", { d: "M12 20s-7-4.3-7-9.3A3.7 3.7 0 0 1 12 7a3.7 3.7 0 0 1 7 3.7C19 15.7 12 20 12 20Z" })); },
    project: function (s) { return svg({ width: s, height: s }, h("path", { d: "M4 7.5A1.5 1.5 0 0 1 5.5 6h4l2 2.2H19a1.5 1.5 0 0 1 1.5 1.5V17a1.5 1.5 0 0 1-1.5 1.5H5.5A1.5 1.5 0 0 1 4 17Z" })); },
    habit: function (s) { return svg({ width: s, height: s }, h("circle", { cx: 12, cy: 12, r: 8 }), h("path", { d: "M12 7.5V12l3 2" })); },
    fact: function (s) { return svg({ width: s, height: s }, h("circle", { cx: 12, cy: 8.2, r: 3.4 }), h("path", { d: "M5.5 19a6.5 6.5 0 0 1 13 0" })); },
    conversation: function (s) { return svg({ width: s, height: s }, h("path", { d: "M20 11.4a6.9 6.9 0 0 1-9.6 6.4L5 19l1.1-4.1A6.9 6.9 0 1 1 20 11.4Z" })); },
  };
  var CATS = {
    preference: { label: "个人偏好", tone: "rose" },
    project: { label: "项目背景", tone: "green" },
    habit: { label: "工作习惯", tone: "blue" },
    fact: { label: "事实信息", tone: "amber" },
    conversation: { label: "对话记忆", tone: "violet" },
  };
  var CAT_ORDER = ["preference", "project", "habit", "fact", "conversation"];
  var SOURCE_TONE = { conversation: "violet", knowledge: "amber", manual: "green", agent: "blue", other: "slate" };
  var CONF_TONE = { high: "green", medium: "amber", low: "slate" };

  function catMeta(id) { return CATS[id] || { label: id, tone: "slate" }; }
  function catIcon(id, size) { return (ICON[id] || ICON.fact)(size || 18); }

  // ── API model (workspace-scoped) ─────────────────────────────────────
  function jsonOrThrow(r) {
    return r.json().catch(function () { return {}; }).then(function (p) {
      if (!r.ok) throw new Error(p.error || p.detail || ("HTTP " + r.status));
      return p;
    });
  }
  function api(ws) {
    var qs = "?workspace=" + encodeURIComponent(ws || "default");
    return {
      list: function () { return fetch("/api/workbench/memory" + qs).then(jsonOrThrow); },
      create: function (body) {
        return fetch("/api/workbench/memory" + qs, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(jsonOrThrow);
      },
      update: function (id, body) {
        return fetch("/api/workbench/memory/" + encodeURIComponent(id) + qs, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(jsonOrThrow);
      },
      remove: function (id) {
        return fetch("/api/workbench/memory/" + encodeURIComponent(id) + qs, { method: "DELETE" }).then(jsonOrThrow);
      },
    };
  }

  // ── small presentational pieces ──────────────────────────────────────
  function Chip(props) {
    return h("span", { className: "wb-mem-chip" + (props.tone ? " " + props.tone : "") }, props.children);
  }

  // Donut chart for 记忆来源 built from {label,count,pct} segments.
  function Donut(props) {
    var segs = (props.segments || []).filter(function (s) { return s.count > 0; });
    var total = segs.reduce(function (a, s) { return a + s.count; }, 0);
    var R = 30, C = 2 * Math.PI * R, off = 0;
    if (!total) {
      return h("svg", { className: "wb-mem-donut", viewBox: "0 0 80 80", width: 78, height: 78 },
        h("circle", { cx: 40, cy: 40, r: R, fill: "none", stroke: "var(--wb-line)", strokeWidth: 12 }));
    }
    var arcs = segs.map(function (s, i) {
      var len = (s.count / total) * C;
      var el = h("circle", {
        key: i, cx: 40, cy: 40, r: R, fill: "none",
        stroke: "var(--wb-mem-" + (SOURCE_TONE[s.id] || "slate") + ")", strokeWidth: 12,
        strokeDasharray: len + " " + (C - len), strokeDashoffset: -off,
        transform: "rotate(-90 40 40)",
      });
      off += len;
      return el;
    });
    return h("svg", { className: "wb-mem-donut", viewBox: "0 0 80 80", width: 78, height: 78 },
      arcs,
      h("text", { x: 40, y: 37, textAnchor: "middle", className: "wb-mem-donut-num" }, total),
      h("text", { x: 40, y: 50, textAnchor: "middle", className: "wb-mem-donut-cap" }, "条"));
  }

  // ── create / edit modal ──────────────────────────────────────────────
  function MemoryModal(props) {
    var init = props.draft || {};
    var contentState = useState(init.content || ""); var content = contentState[0]; var setContent = contentState[1];
    var catState = useState(init.category || "fact"); var category = catState[0]; var setCategory = catState[1];
    var srcState = useState(init.source || "manual"); var source = srcState[0]; var setSource = srcState[1];
    var confState = useState(init.confidence || ""); var confidence = confState[0]; var setConfidence = confState[1];
    var tagsState = useState((init.tags || []).join(", ")); var tags = tagsState[0]; var setTags = tagsState[1];
    var ref = useRef(null);
    useEffect(function () { if (ref.current) ref.current.focus(); }, []);

    function submit() {
      var body = {
        content: content.trim(),
        category: category,
        source: source,
        confidence: confidence,
        tags: tags.split(/[,，;；]/).map(function (t) { return t.trim(); }).filter(Boolean),
      };
      if (!body.content) { if (ref.current) ref.current.focus(); return; }
      props.onSubmit(body);
    }

    var sel = function (value, setter, options) {
      return h("div", { className: "wb-mem-seg" }, options.map(function (o) {
        return h("button", { key: o.id, type: "button", className: "wb-mem-seg-btn" + (value === o.id ? " on" : ""), onClick: function () { setter(o.id); } }, o.label);
      }));
    };

    return h("div", { className: "wb-mem-modal-scrim", onMouseDown: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("div", { className: "wb-mem-modal", role: "dialog" },
        h("div", { className: "wb-mem-modal-head" },
          h("b", null, props.mode === "edit" ? "编辑记忆" : "新建记忆"),
          h("button", { type: "button", className: "wb-mem-iconbtn", onClick: props.onClose, title: "关闭" },
            svg({ width: 17, height: 17 }, h("path", { d: "m6 6 12 12M18 6 6 18" })))),
        h("div", { className: "wb-mem-modal-body" },
          h("label", { className: "wb-mem-field-label" }, "记忆内容"),
          h("textarea", { ref: ref, className: "wb-mem-textarea", value: content, placeholder: "描述这条记忆的内容…", onChange: function (e) { setContent(e.target.value); }, rows: 4 }),
          h("label", { className: "wb-mem-field-label" }, "类型"),
          sel(category, setCategory, CAT_ORDER.map(function (c) { return { id: c, label: CATS[c].label }; })),
          h("label", { className: "wb-mem-field-label" }, "来源"),
          sel(source, setSource, [
            { id: "manual", label: "手动添加" }, { id: "conversation", label: "对话" },
            { id: "knowledge", label: "知识库" }, { id: "other", label: "其他" },
          ]),
          h("label", { className: "wb-mem-field-label" }, "置信度"),
          sel(confidence, setConfidence, [
            { id: "", label: "自动" }, { id: "high", label: "高" }, { id: "medium", label: "中" }, { id: "low", label: "低" },
          ]),
          h("label", { className: "wb-mem-field-label" }, "标签"),
          h("input", { className: "wb-mem-input", value: tags, placeholder: "用逗号分隔，如：表达偏好, 沟通方式", onChange: function (e) { setTags(e.target.value); } })),
        h("div", { className: "wb-mem-modal-foot" },
          h("button", { type: "button", className: "wb-btn ghost", onClick: props.onClose }, "取消"),
          h("button", { type: "button", className: "wb-btn primary", onClick: submit, disabled: props.busy }, props.busy ? "保存中…" : "保存"))));
  }

  // ── detail panel ─────────────────────────────────────────────────────
  function MetaRow(props) {
    return h("div", { className: "wb-mem-meta-row" },
      h("label", null, props.label),
      h("div", { className: "wb-mem-meta-val" }, props.children));
  }

  function DetailPanel(props) {
    var m = props.memory;
    var tabState = useState("detail"); var tab = tabState[0]; var setTab = tabState[1];
    useEffect(function () { setTab("detail"); }, [m ? m.id : ""]);
    if (!m) {
      return h("aside", { className: "wb-mem-detail empty" },
        h("div", { className: "wb-mem-detail-ph" },
          svg({ width: 34, height: 34, strokeWidth: 1.4 }, h("path", { d: "M12 3.6 14 9.4 20 11l-6 1.6L12 18l-2-5.4L4 11l6-1.6Z" })),
          h("p", null, "选择一条记忆查看详情")));
    }
    var meta = catMeta(m.category);
    var related = props.related || [];
    var tabs = [
      { id: "detail", label: "详情" },
      { id: "cite", label: "引用 (" + m.citation_count + ")" },
      { id: "related", label: "相关记忆 (" + related.length + ")" },
      { id: "history", label: "编辑历史" },
    ];

    var detailBody = h("div", { className: "wb-mem-detail-scroll" },
      h("div", { className: "wb-mem-detail-hero" },
        h("span", { className: "wb-mem-ico " + meta.tone }, catIcon(m.category, 18)),
        h("p", null, m.content),
        h("div", { className: "wb-mem-hero-actions" },
          h("button", { type: "button", className: "wb-mem-iconbtn", title: "编辑", onClick: function () { props.onEdit(m); } },
            svg({ width: 15, height: 15 }, h("path", { d: "M12 20h9" }), h("path", { d: "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" }))),
          h("button", { type: "button", className: "wb-mem-iconbtn", title: "删除", onClick: function () { props.onDelete(m); } },
            svg({ width: 15, height: 15 }, h("path", { d: "M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" }))))),
      h("div", { className: "wb-mem-meta" },
        MetaRow({ label: "类型", children: h(Chip, { tone: meta.tone }, m.category_label) }),
        MetaRow({ label: "标签", children: h("div", { className: "wb-mem-tagwrap" },
          (m.tags.length ? m.tags : []).map(function (t, i) { return h(Chip, { key: i }, t); }),
          h("button", { type: "button", className: "wb-mem-tag-add", title: "编辑标签", onClick: function () { props.onEdit(m); } }, "+")) }),
        MetaRow({ label: "来源", children: m.source_label }),
        m.stale && MetaRow({ label: "状态", children: h(Chip, { tone: "slate" }, "已过时 · 不注入") }),
        MetaRow({ label: "创建时间", children: formatFull(m.created_at) }),
        MetaRow({ label: "更新时间", children: formatFull(m.updated_at) }),
        MetaRow({ label: "置信度", children: h(Chip, { tone: CONF_TONE[m.confidence] }, m.confidence_label) }),
        MetaRow({ label: "引用次数", children: String(m.citation_count) })),
      h("div", { className: "wb-mem-section" },
        h("div", { className: "wb-mem-section-head" }, "记忆内容"),
        h("p", { className: "wb-mem-content-full" }, m.content)),
      related.length > 0 && h("div", { className: "wb-mem-section" },
        h("div", { className: "wb-mem-section-head" }, "相关记忆", h("button", { type: "button", className: "wb-mem-link", onClick: function () { setTab("related"); } }, "查看全部 (" + related.length + ")")),
        related.slice(0, 3).map(function (r) {
          return h("button", { key: r.id, type: "button", className: "wb-mem-related-row", onClick: function () { props.onSelect(r.id); } },
            h("span", { className: "wb-mem-ico sm " + catMeta(r.category).tone }, catIcon(r.category, 13)),
            h("span", { className: "wb-mem-related-text" }, r.content),
            h("time", null, formatRel(r.updated_at)));
        })));

    var citeBody = h("div", { className: "wb-mem-detail-scroll" },
      h("div", { className: "wb-mem-cite-summary" }, h("b", null, m.citation_count), h("span", null, "次被引用")),
      h("div", { className: "wb-mem-empty-soft" },
        svg({ width: 26, height: 26, strokeWidth: 1.5 }, h("path", { d: "M8 10h8M8 14h5" }), h("path", { d: "M21 11.4a6.9 6.9 0 0 1-9.6 6.4L6 19l1.1-4.1A6.9 6.9 0 1 1 21 11.4Z" })),
        h("p", null, "引用记录会在 Agent 引用此记忆时自动记录")));

    var relatedBody = h("div", { className: "wb-mem-detail-scroll" },
      related.length === 0
        ? h("div", { className: "wb-mem-empty-soft" }, h("p", null, "暂无相关记忆"))
        : related.map(function (r) {
          return h("button", { key: r.id, type: "button", className: "wb-mem-related-row", onClick: function () { props.onSelect(r.id); } },
            h("span", { className: "wb-mem-ico sm " + catMeta(r.category).tone }, catIcon(r.category, 13)),
            h("span", { className: "wb-mem-related-text" }, r.content),
            h("time", null, formatRel(r.updated_at)));
        }));

    var historyBody = h("div", { className: "wb-mem-detail-scroll" },
      h("div", { className: "wb-mem-history" },
        h("div", { className: "wb-mem-history-row" }, h("span", { className: "wb-mem-dot" }), h("div", null, h("b", null, "最后更新"), h("small", null, formatFull(m.updated_at)))),
        h("div", { className: "wb-mem-history-row" }, h("span", { className: "wb-mem-dot muted" }), h("div", null, h("b", null, "创建记忆"), h("small", null, formatFull(m.created_at))))));

    return h("aside", { className: "wb-mem-detail" },
      h("div", { className: "wb-mem-detail-tabs" }, tabs.map(function (t) {
        return h("button", { key: t.id, type: "button", className: "wb-mem-detail-tab" + (tab === t.id ? " active" : ""), onClick: function () { setTab(t.id); } }, t.label);
      })),
      tab === "detail" ? detailBody : tab === "cite" ? citeBody : tab === "related" ? relatedBody : historyBody,
      h("div", { className: "wb-mem-detail-foot" },
        h("button", { type: "button", className: "wb-btn ghost", onClick: function () { props.onEdit(m); } }, "编辑记忆"),
        h("button", { type: "button", className: "wb-btn ghost", disabled: props.busy, title: m.stale ? "恢复后会重新注入 Agent" : "过时后不再注入 Agent，但保留记录", onClick: function () { props.onToggleStale(m); } }, m.stale ? "恢复使用" : "标记过时"),
        h("button", { type: "button", className: "wb-btn danger", onClick: function () { props.onDelete(m); } }, "删除记忆")));
  }

  // ── main page ────────────────────────────────────────────────────────
  function WorkbenchMemoryPage(props) {
    var project = props && props.project;
    var workspace = (project && (project.dataKey || project.id)) || "default";

    var payloadState = useState(null); var payload = payloadState[0]; var setPayload = payloadState[1];
    var loadState = useState(true); var loading = loadState[0]; var setLoading = loadState[1];
    var errState = useState(""); var error = errState[0]; var setError = errState[1];
    var queryState = useState(""); var query = queryState[0]; var setQuery = queryState[1];
    var catState = useState("all"); var activeCat = catState[0]; var setActiveCat = catState[1];
    var srcState = useState(""); var sourceFilter = srcState[0]; var setSourceFilter = srcState[1];
    var sortState = useState("updated"); var sortKey = sortState[0]; var setSortKey = sortState[1];
    var selState = useState(""); var selectedId = selState[0]; var setSelectedId = selState[1];
    var menuState = useState(""); var menu = menuState[0]; var setMenu = menuState[1]; // "type" | "source" | "sort"
    var modalState = useState(null); var modal = modalState[0]; var setModal = modalState[1];
    var busyState = useState(false); var busy = busyState[0]; var setBusy = busyState[1];

    var client = useMemo(function () { return api(workspace); }, [workspace]);

    function load() {
      setLoading(true); setError("");
      return client.list()
        .then(function (p) { setPayload(p); })
        .catch(function (e) { setError(e.message || String(e)); setPayload({ memories: [], categories: [], sources: [], overview: {} }); })
        .finally(function () { setLoading(false); });
    }
    useEffect(function () { setSelectedId(""); setActiveCat("all"); setSourceFilter(""); load(); }, [workspace]);

    var memories = (payload && payload.memories) || [];
    var categories = (payload && payload.categories) || [];
    var sources = (payload && payload.sources) || [];
    var overview = (payload && payload.overview) || {};

    var visible = useMemo(function () {
      var q = query.trim().toLowerCase();
      var list = memories.filter(function (m) {
        if (activeCat !== "all" && m.category !== activeCat) return false;
        if (sourceFilter && m.source !== sourceFilter) return false;
        if (!q) return true;
        return (m.content + " " + (m.tags || []).join(" ")).toLowerCase().indexOf(q) >= 0;
      });
      list.sort(function (a, b) {
        if (sortKey === "created") return String(b.created_at).localeCompare(String(a.created_at));
        if (sortKey === "citations") return (b.citation_count || 0) - (a.citation_count || 0);
        return String(b.updated_at).localeCompare(String(a.updated_at));
      });
      return list;
    }, [memories, query, activeCat, sourceFilter, sortKey]);

    var selected = selectedId ? memories.find(function (m) { return m.id === selectedId; }) || null : null;
    var related = useMemo(function () {
      if (!selected) return [];
      return memories.filter(function (m) { return m.id !== selected.id && m.category === selected.category; }).slice(0, 8);
    }, [selected, memories]);

    function applyPayload(p) {
      setPayload(p);
      return p;
    }
    function handleCreate(body) {
      setBusy(true);
      client.create(body)
        .then(function (p) { applyPayload(p); setModal(null); if (p && p.id) setSelectedId(p.id); })
        .catch(function (e) { setError(e.message || String(e)); })
        .finally(function () { setBusy(false); });
    }
    function handleEditSubmit(id, body) {
      setBusy(true);
      client.update(id, body)
        .then(function (p) { applyPayload(p); setModal(null); })
        .catch(function (e) { setError(e.message || String(e)); })
        .finally(function () { setBusy(false); });
    }
    function handleDelete(m) {
      if (!window.confirm("确定删除这条记忆吗？此操作不可撤销。")) return;
      client.remove(m.id)
        .then(function (p) { applyPayload(p); if (selectedId === m.id) setSelectedId(""); })
        .catch(function (e) { setError(e.message || String(e)); });
    }
    // Retire / revive a memory: stale entries stay listed but are no longer
    // injected into agent runs.
    function handleToggleStale(m) {
      setBusy(true);
      client.update(m.id, { stale: !m.stale })
        .then(applyPayload)
        .catch(function (e) { setError(e.message || String(e)); })
        .finally(function () { setBusy(false); });
    }

    if (!project) {
      return h("section", { className: "wb-mem-page" },
        h("div", { className: "wb-mem-empty" }, "请选择一个项目以查看其记忆。"));
    }

    var typeOptions = [{ id: "all", label: "全部类型" }].concat(CAT_ORDER.map(function (c) { return { id: c, label: CATS[c].label }; }));
    var sourceOptions = [{ id: "", label: "全部来源" }, { id: "conversation", label: "对话" }, { id: "knowledge", label: "知识库" }, { id: "manual", label: "手动添加" }, { id: "agent", label: "Agent 记录" }, { id: "other", label: "其他" }];
    var sortOptions = [{ id: "updated", label: "最新更新" }, { id: "created", label: "最近创建" }, { id: "citations", label: "引用最多" }];
    function curLabel(opts, val) { for (var i = 0; i < opts.length; i++) if (opts[i].id === val) return opts[i].label; return opts[0].label; }

    function dropdown(key, label, options, value, setter) {
      return h("div", { className: "wb-mem-tool-wrap" },
        h("button", { type: "button", className: "wb-mem-tool" + (value && value !== "all" ? " on" : ""), onClick: function () { setMenu(menu === key ? "" : key); } },
          h("span", null, label),
          svg({ width: 13, height: 13, strokeWidth: 2 }, h("path", { d: "m6 9 6 6 6-6" }))),
        menu === key && h("div", { className: "wb-mem-menu" }, options.map(function (o) {
          return h("button", { key: o.id, type: "button", className: value === o.id ? "sel" : "", onClick: function () { setter(o.id); setMenu(""); } }, o.label);
        })));
    }

    // ── category rail ──
    var rail = h("aside", { className: "wb-mem-rail" },
      h("div", { className: "wb-mem-rail-head" },
        h("b", null, "记忆"),
        h("button", { type: "button", className: "wb-mem-new-btn", onClick: function () { setModal({ mode: "create", draft: {} }); } },
          svg({ width: 13, height: 13, strokeWidth: 2.4 }, h("path", { d: "M12 5v14M5 12h14" })), h("span", null, "新建记忆"))),
      h("div", { className: "wb-mem-cats" }, categories.map(function (c) {
        var meta = c.id === "all" ? { tone: "accent" } : catMeta(c.id);
        return h("button", { key: c.id, type: "button", className: "wb-mem-cat" + (activeCat === c.id ? " active" : ""), onClick: function () { setActiveCat(c.id); } },
          h("span", { className: "wb-mem-cat-ico " + meta.tone }, c.id === "all" ? ICON.all(15) : catIcon(c.id, 15)),
          h("span", { className: "wb-mem-cat-label" }, c.label),
          h("span", { className: "wb-mem-cat-count" }, c.count));
      })),
      h("div", { className: "wb-mem-card" },
        h("div", { className: "wb-mem-card-head" }, "记忆概览"),
        h("div", { className: "wb-mem-ov-row" }, h("span", null, "总记忆数"), h("b", null, overview.total || 0)),
        h("div", { className: "wb-mem-ov-row" }, h("span", null, "近期新增"), h("b", null, overview.recent_added || 0)),
        h("div", { className: "wb-mem-ov-row" }, h("span", null, "被引用次数"), h("b", null, overview.total_citations || 0)),
        h("div", { className: "wb-mem-ov-row" }, h("span", null, "最后更新"), h("b", null, formatRel(overview.last_updated)))),
      h("div", { className: "wb-mem-card" },
        h("div", { className: "wb-mem-card-head" }, "记忆来源"),
        h("div", { className: "wb-mem-source-body" },
          h(Donut, { segments: sources }),
          h("div", { className: "wb-mem-source-legend" }, sources.map(function (s) {
            return h("div", { key: s.id, className: "wb-mem-legend-row" },
              h("span", { className: "wb-mem-legend-dot " + (SOURCE_TONE[s.id] || "slate") }),
              h("span", { className: "wb-mem-legend-label" }, s.label),
              h("span", { className: "wb-mem-legend-pct" }, s.pct + "%"));
          })))));

    // ── memory card list ──
    function card(m) {
      var meta = catMeta(m.category);
      return h("button", { key: m.id, type: "button", className: "wb-mem-item" + (selectedId === m.id ? " active" : "") + (m.stale ? " stale" : ""), onClick: function () { setSelectedId(m.id); } },
        h("span", { className: "wb-mem-ico " + meta.tone }, catIcon(m.category, 17)),
        h("div", { className: "wb-mem-item-body" },
          h("div", { className: "wb-mem-item-top" },
            h("p", { className: "wb-mem-item-text" }, m.content),
            h("time", null, formatRel(m.updated_at))),
          h("div", { className: "wb-mem-item-tags" },
            h(Chip, { tone: meta.tone }, m.category_label),
            (m.tags || []).slice(0, 2).map(function (t, i) { return h(Chip, { key: i }, t); }),
            h(Chip, { tone: "ghost" }, m.source_label))));
    }

    var main = h("div", { className: "wb-mem-main" },
      h("div", { className: "wb-mem-toolbar" },
        h("div", { className: "wb-mem-searchbox" },
          svg({ width: 15, height: 15, strokeWidth: 1.9 }, h("circle", { cx: 11, cy: 11, r: 7 }), h("path", { d: "m20 20-3.2-3.2" })),
          h("input", { type: "text", placeholder: "搜索记忆…", value: query, onChange: function (e) { setQuery(e.target.value); } })),
        h("div", { className: "wb-mem-tools" },
          dropdown("type", curLabel(typeOptions, activeCat), typeOptions, activeCat, setActiveCat),
          dropdown("source", curLabel(sourceOptions, sourceFilter), sourceOptions, sourceFilter, setSourceFilter),
          dropdown("sort", curLabel(sortOptions, sortKey), sortOptions, sortKey, setSortKey))),
      error && h("div", { className: "wb-mem-error" }, error),
      h("div", { className: "wb-mem-list-col" },
        h("div", { className: "wb-mem-scroll" },
          loading
            ? h("div", { className: "wb-mem-empty" }, "加载记忆中…")
            : visible.length === 0
              ? h("div", { className: "wb-mem-empty" },
                h("div", { className: "wb-mem-empty-icon" }, ICON.all(38)),
                h("p", null, query || activeCat !== "all" || sourceFilter ? "没有匹配的记忆。" : "还没有记忆内容。"),
                h("button", { type: "button", className: "wb-btn primary", onClick: function () { setModal({ mode: "create", draft: {} }); } }, "新建第一条记忆"))
              : h("div", { className: "wb-mem-list" }, visible.map(card))),
        h("div", { className: "wb-mem-count" }, "共 " + visible.length + " 条记忆")));

    return h("section", { className: "wb-mem-page" },
      rail,
      main,
      h(DetailPanel, {
        memory: selected, related: related, busy: busy,
        onSelect: setSelectedId,
        onEdit: function (m) { setModal({ mode: "edit", id: m.id, draft: { content: m.content, category: m.category, source: m.source, confidence: m.confidence, tags: m.tags } }); },
        onDelete: handleDelete,
        onToggleStale: handleToggleStale,
      }),
      menu && h("div", { className: "wb-mem-scrim", onClick: function () { setMenu(""); } }),
      modal && h(MemoryModal, {
        mode: modal.mode, draft: modal.draft, busy: busy,
        onClose: function () { setModal(null); },
        onSubmit: function (body) { if (modal.mode === "edit") handleEditSubmit(modal.id, body); else handleCreate(body); },
      }));
  }

  window.WorkbenchMemoryPage = WorkbenchMemoryPage;
})();
