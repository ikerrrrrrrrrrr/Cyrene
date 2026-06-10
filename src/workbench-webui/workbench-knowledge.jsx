// Workbench Knowledge base page.
//
// Fully independent from the legacy `knowledge.jsx` (`window.KnowledgePage`):
// its own model, components and styles. Talks ONLY to the workspace-scoped
// `/api/workbench/knowledge/*` backend, passing the active project id as the
// `workspace` so every project/workspace owns a separate knowledge base.
(function () {
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useMemo = React.useMemo;
  var useRef = React.useRef;

  // ── helpers ──────────────────────────────────────────────────────────

  function formatDate(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso).slice(0, 10);
      var y = d.getFullYear();
      var m = ("0" + (d.getMonth() + 1)).slice(-2);
      var day = ("0" + d.getDate()).slice(-2);
      var hh = ("0" + d.getHours()).slice(-2);
      var mm = ("0" + d.getMinutes()).slice(-2);
      return y + "-" + m + "-" + day + " " + hh + ":" + mm;
    } catch (e) { return String(iso).slice(0, 16); }
  }

  function formatDateShort(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso).slice(0, 10);
      var y = d.getFullYear();
      var m = ("0" + (d.getMonth() + 1)).slice(-2);
      var day = ("0" + d.getDate()).slice(-2);
      var hh = ("0" + d.getHours()).slice(-2);
      var mm = ("0" + d.getMinutes()).slice(-2);
      return y + "-" + m + "-" + day + " " + hh + ":" + mm;
    } catch (e) { return String(iso).slice(0, 10); }
  }

  function formatBytes(bytes) {
    var n = Number(bytes || 0);
    if (!n) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  }

  function formatNumber(n) {
    var v = Number(n || 0);
    return v.toLocaleString ? v.toLocaleString() : String(v);
  }

  function docTitle(doc) {
    return String((doc && (doc.title || doc.name)) || "未命名文档").trim();
  }

  function fileExt(doc) {
    var name = String((doc && doc.name) || "");
    var idx = name.lastIndexOf(".");
    return idx >= 0 ? name.slice(idx + 1).toLowerCase() : "";
  }

  // Visual kind drives the colored icon tile. Refine the backend `kind`
  // (image/pdf/map/code/file) using the extension for nicer affordances.
  function visualKind(doc) {
    var kind = String((doc && doc.kind) || "file");
    var ext = fileExt(doc);
    var name = String((doc && doc.name) || "");
    var ctype = String((doc && doc.content_type) || "");
    if (ext === "url" || ext === "webloc" || ext === "link" || ctype.indexOf("uri-list") >= 0 || /^https?:\/\//i.test(name)) return "link";
    if (kind === "image") return "image";
    if (ext === "pdf" || kind === "pdf") return "pdf";
    if (["md", "markdown", "mdown", "mkd"].indexOf(ext) >= 0) return "markdown";
    if (["xlsx", "xls", "xlsm", "csv", "tsv", "numbers"].indexOf(ext) >= 0) return "sheet";
    if (["doc", "docx", "rtf", "odt"].indexOf(ext) >= 0) return "doc";
    if (["ppt", "pptx", "key", "odp"].indexOf(ext) >= 0) return "slide";
    if (kind === "map") return "map";
    if (kind === "code") return "code";
    if (["txt", "text", "log"].indexOf(ext) >= 0) return "note";
    return "file";
  }

  var KIND_TONE = {
    pdf: "red", doc: "blue", sheet: "green", slide: "orange",
    markdown: "indigo", link: "cyan", image: "purple", code: "slate",
    map: "teal", note: "amber", file: "slate",
  };

  function kindIconSvg(vk) {
    var s = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" };
    // A document silhouette carrying a short type label (PDF / DOC) — the
    // clearest way to tell otherwise-identical text documents apart.
    function labeledDoc(label) {
      return React.createElement("svg", s,
        React.createElement("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" }),
        React.createElement("path", { d: "M14 3v5h5" }),
        React.createElement("text", { x: 11.2, y: 17.6, textAnchor: "middle", fontSize: 5.3, fontWeight: 800, fill: "currentColor", stroke: "none", style: { letterSpacing: "-0.2px" } }, label));
    }
    if (vk === "pdf") return labeledDoc("PDF");
    if (vk === "doc") return labeledDoc("DOC");
    if (vk === "sheet") {
      // Excel-style grid.
      return React.createElement("svg", s,
        React.createElement("rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }),
        React.createElement("path", { d: "M3 9h18M3 14h18M9 4v16M15 4v16" }));
    }
    if (vk === "slide") {
      // Presentation screen with a small bar chart + stand.
      return React.createElement("svg", s,
        React.createElement("rect", { x: 3, y: 4, width: 18, height: 13, rx: 2 }),
        React.createElement("path", { d: "M7.5 13V11M12 13V8.5M16.5 13v-3" }),
        React.createElement("path", { d: "M12 17v4M8.5 21h7" }));
    }
    if (vk === "markdown") {
      // Markdown mark: rounded frame with an "M" and a down arrow.
      return React.createElement("svg", s,
        React.createElement("rect", { x: 2.5, y: 5, width: 19, height: 14, rx: 2.5 }),
        React.createElement("path", { d: "M6 15.5V9.5l2.4 2.8 2.4-2.8v6" }),
        React.createElement("path", { d: "M15.6 9.7v4M15.6 13.9l-1.6-1.8M15.6 13.9l1.6-1.8" }));
    }
    if (vk === "link") {
      // Chain link.
      return React.createElement("svg", s,
        React.createElement("path", { d: "M10 13a4.5 4.5 0 0 0 6.6.3l2.5-2.5a4.5 4.5 0 0 0-6.4-6.4l-1.4 1.4" }),
        React.createElement("path", { d: "M14 11a4.5 4.5 0 0 0-6.6-.3l-2.5 2.5a4.5 4.5 0 0 0 6.4 6.4l1.4-1.4" }));
    }
    if (vk === "image") {
      return React.createElement("svg", s,
        React.createElement("rect", { x: 3, y: 3, width: 18, height: 18, rx: 2.5 }),
        React.createElement("circle", { cx: 8.5, cy: 9, r: 1.6 }),
        React.createElement("path", { d: "m21 16-5-5L5 21" }));
    }
    if (vk === "code") {
      return React.createElement("svg", s,
        React.createElement("path", { d: "m9 8-4 4 4 4" }),
        React.createElement("path", { d: "m15 8 4 4-4 4" }));
    }
    if (vk === "map") {
      return React.createElement("svg", s,
        React.createElement("path", { d: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z" }),
        React.createElement("path", { d: "M9 4v14M15 6v14" }));
    }
    if (vk === "note") {
      return React.createElement("svg", s,
        React.createElement("path", { d: "M5 3h14a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" }),
        React.createElement("path", { d: "M8 8h8M8 12h8M8 16h5" }));
    }
    // generic file → document sheet
    return React.createElement("svg", s,
      React.createElement("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" }),
      React.createElement("path", { d: "M14 3v5h5" }),
      React.createElement("path", { d: "M9 13h6M9 17h4" }));
  }

  function DocIcon(props) {
    var vk = visualKind(props.doc);
    var tone = KIND_TONE[vk] || "slate";
    return React.createElement("span", { className: "wb-kb-ico " + tone + (props.lg ? " lg" : "") }, kindIconSvg(vk));
  }

  function docDescription(doc) {
    var summary = String((doc && doc.summary) || "").trim();
    if (summary) return summary;
    var status = String((doc && doc.status) || "");
    if (status === "pending" || status === "indexing") return "正在索引内容…";
    if (status === "error") return "索引失败，可重新索引重试。";
    var vk = visualKind(doc);
    var typeLabel = { pdf: "PDF 文档", doc: "Word 文档", sheet: "电子表格", slide: "演示文稿", markdown: "Markdown", link: "链接", image: "图片", code: "代码文件", map: "地图数据", note: "文本", file: "文件" }[vk] || "文件";
    return typeLabel + " · " + formatBytes(doc && doc.size);
  }

  var SOURCE_LABELS = {
    kb_upload: "知识库上传", chat_upload: "对话上传", import: "导入", export: "导出文件", sync: "同步",
  };
  function sourceLabel(source) {
    return SOURCE_LABELS[String(source || "")] || (source ? String(source) : "其他");
  }

  function statusMeta(status) {
    var raw = String(status || "");
    if (raw === "indexed" || raw === "done" || raw === "completed") return { tone: "green", text: "已索引" };
    if (raw === "pending" || raw === "indexing") return { tone: "amber", text: "索引中" };
    if (raw === "error") return { tone: "red", text: "索引失败" };
    return { tone: "slate", text: raw || "未知" };
  }

  // ── API model (workspace-scoped) ─────────────────────────────────────

  function api(workspace) {
    var ws = encodeURIComponent(workspace || "default");
    function withWs(qs) { return "workspace=" + ws + (qs ? "&" + qs : ""); }

    async function jsonOrThrow(r) {
      var payload = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(payload.error || payload.detail || ("HTTP " + r.status));
      return payload;
    }
    return {
      list: async function (params) {
        var qs = new URLSearchParams(params || {}).toString();
        var r = await fetch("/api/workbench/knowledge/documents?" + withWs(qs));
        var payload = await jsonOrThrow(r);
        return (payload && payload.documents) || [];
      },
      detail: async function (id) {
        var r = await fetch("/api/workbench/knowledge/documents/" + encodeURIComponent(id) + "?" + withWs());
        return jsonOrThrow(r);
      },
      upload: async function (files) {
        var fd = new FormData();
        for (var i = 0; i < files.length; i++) fd.append("files", files[i]);
        var r = await fetch("/api/workbench/knowledge/documents?" + withWs(), { method: "POST", body: fd });
        return jsonOrThrow(r);
      },
      update: async function (id, body) {
        var r = await fetch("/api/workbench/knowledge/documents/" + encodeURIComponent(id) + "?" + withWs(), {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
        });
        return jsonOrThrow(r);
      },
      reindex: async function (id) {
        var r = await fetch("/api/workbench/knowledge/documents/" + encodeURIComponent(id) + "/reindex?" + withWs(), { method: "POST" });
        return jsonOrThrow(r);
      },
      remove: async function (id) {
        var r = await fetch("/api/workbench/knowledge/documents/" + encodeURIComponent(id) + "?" + withWs(), { method: "DELETE" });
        return jsonOrThrow(r);
      },
      rawUrl: function (id) {
        return "/api/workbench/knowledge/documents/" + encodeURIComponent(id) + "/raw?" + withWs();
      },
    };
  }

  // ── card ─────────────────────────────────────────────────────────────

  function KbCard(props) {
    var doc = props.doc;
    var listMode = props.listMode;
    var tags = Array.isArray(doc.tags) ? doc.tags : [];
    var sm = statusMeta(doc.status);
    var indexing = sm.tone === "amber";
    return React.createElement(
      "div",
      {
        className: "wb-kb-card" + (listMode ? " list" : "") + (props.active ? " active" : ""),
        onClick: function () { props.onSelect(doc.id); },
        role: "button",
        tabIndex: 0,
        onKeyDown: function (e) { if (e.key === "Enter") props.onSelect(doc.id); },
      },
      React.createElement(DocIcon, { doc: doc }),
      React.createElement(
        "div", { className: "wb-kb-card-body" },
        React.createElement(
          "div", { className: "wb-kb-card-title-row" },
          React.createElement("b", { className: "wb-kb-card-title", title: docTitle(doc) }, docTitle(doc)),
          indexing && React.createElement("span", { className: "wb-kb-badge amber" }, sm.text)
        ),
        React.createElement("p", { className: "wb-kb-card-desc" }, docDescription(doc)),
        tags.length > 0 && React.createElement(
          "div", { className: "wb-kb-tags" },
          tags.slice(0, 4).map(function (tag) {
            return React.createElement("span", { className: "wb-kb-tag", key: tag }, "# " + tag);
          })
        ),
        React.createElement(
          "div", { className: "wb-kb-card-foot" },
          React.createElement("span", null, "更新于 " + formatDateShort(doc.updated_at || doc.created_at)),
          React.createElement(
            "button",
            {
              type: "button", className: "wb-kb-card-menu-btn",
              title: "更多",
              onClick: function (e) { e.stopPropagation(); props.onMenu(doc, e); },
            },
            React.createElement("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "currentColor" },
              React.createElement("circle", { cx: 5, cy: 12, r: 1.6 }),
              React.createElement("circle", { cx: 12, cy: 12, r: 1.6 }),
              React.createElement("circle", { cx: 19, cy: 12, r: 1.6 }))
          )
        )
      )
    );
  }

  // ── detail panel ─────────────────────────────────────────────────────

  function KbDetailPanel(props) {
    var doc = props.doc;        // list-level doc (always present)
    var detail = props.detail;  // full detail (chunks + relations), may be loading
    var tab = props.tab;
    var setTab = props.setTab;
    var userName = (window.DATA && DATA.user && DATA.user.name) || "我";

    var chunks = (detail && Array.isArray(detail.chunks)) ? detail.chunks : [];
    var relations = (detail && Array.isArray(detail.relations)) ? detail.relations : [];
    var tags = Array.isArray(doc.tags) ? doc.tags : [];
    var sm = statusMeta(doc.status);

    var tabs = [
      { id: "detail", label: "详情" },
      { id: "content", label: "内容" },
      { id: "related", label: "关联对话" },
    ];

    return React.createElement(
      "aside", { className: "wb-kb-detail" },
      React.createElement(
        "div", { className: "wb-kb-detail-tabs" },
        tabs.map(function (it) {
          return React.createElement("button", {
            key: it.id, type: "button",
            className: tab === it.id ? "active" : "",
            onClick: function () { setTab(it.id); },
          }, it.label);
        })
      ),
      React.createElement(
        "div", { className: "wb-kb-detail-head" },
        React.createElement(DocIcon, { doc: doc, lg: true }),
        React.createElement("b", { className: "wb-kb-detail-title", title: docTitle(doc) }, docTitle(doc)),
        React.createElement(
          "div", { className: "wb-kb-detail-head-actions" },
          React.createElement("a", {
            className: "wb-kb-iconbtn", href: props.rawUrl, target: "_blank", rel: "noreferrer", title: "查看原文件",
          }, React.createElement("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" },
            React.createElement("path", { d: "M1.5 12s4-7 10.5-7 10.5 7 10.5 7-4 7-10.5 7S1.5 12 1.5 12Z" }),
            React.createElement("circle", { cx: 12, cy: 12, r: 3 })))
        )
      ),
      React.createElement(
        "div", { className: "wb-kb-detail-body" },
        tab === "detail" && React.createElement(KbDetailInfo, {
          doc: doc, detail: detail, tags: tags, sm: sm, userName: userName,
          chunks: chunks, relations: relations, rawUrl: props.rawUrl,
        }),
        tab === "content" && React.createElement(KbContentTab, { detail: detail, doc: doc, loading: props.detailLoading }),
        tab === "related" && React.createElement(KbRelatedTab, { relations: relations, docsById: props.docsById, onSelect: props.onSelect })
      )
    );
  }

  function MetaRow(props) {
    return React.createElement(
      "div", { className: "wb-kb-meta-row" },
      React.createElement("span", { className: "wb-kb-meta-label" }, props.label),
      React.createElement("span", { className: "wb-kb-meta-value" }, props.value)
    );
  }

  function KbDetailInfo(props) {
    var doc = props.doc;
    return React.createElement(
      "div", { className: "wb-kb-detail-stack" },
      React.createElement("p", { className: "wb-kb-detail-desc" }, docDescription(doc)),
      React.createElement(
        "div", { className: "wb-kb-tags" },
        (props.tags.length ? props.tags : []).map(function (tag) {
          return React.createElement("span", { className: "wb-kb-tag", key: tag }, "# " + tag);
        }),
        props.tags.length === 0 && React.createElement("span", { className: "wb-kb-muted" }, "暂无标签")
      ),
      React.createElement(
        "div", { className: "wb-kb-meta-card" },
        React.createElement(MetaRow, { label: "创建时间", value: formatDate(doc.created_at) }),
        React.createElement(MetaRow, { label: "更新时间", value: formatDate(doc.updated_at) }),
        React.createElement(MetaRow, { label: "创建者", value: props.userName }),
        React.createElement(MetaRow, { label: "来源", value: sourceLabel(doc.source) }),
        React.createElement(MetaRow, { label: "状态", value: React.createElement("span", { className: "wb-kb-badge " + props.sm.tone }, props.sm.text) }),
        React.createElement(MetaRow, { label: "字符数", value: formatNumber(doc.char_count) }),
        React.createElement(MetaRow, { label: "分块数", value: formatNumber(doc.chunk_count) }),
        React.createElement(MetaRow, { label: "关联", value: formatNumber(props.relations.length) })
      ),
      React.createElement(
        "div", { className: "wb-kb-filelist" },
        React.createElement("div", { className: "wb-kb-section-head" }, React.createElement("span", null, "文件"), React.createElement("small", null, "1 个文件")),
        React.createElement(
          "a", { className: "wb-kb-file-row", href: props.rawUrl, target: "_blank", rel: "noreferrer" },
          React.createElement(DocIcon, { doc: doc }),
          React.createElement("span", { className: "wb-kb-file-name" }, doc.name || docTitle(doc)),
          React.createElement("small", null, formatBytes(doc.size))
        )
      )
    );
  }

  function KbContentTab(props) {
    var detail = props.detail;
    var chunks = (detail && Array.isArray(detail.chunks)) ? detail.chunks : [];
    if (props.loading) return React.createElement("div", { className: "wb-kb-muted pad" }, "加载内容中…");
    if (!chunks.length) {
      var st = statusMeta(props.doc.status);
      return React.createElement("div", { className: "wb-kb-muted pad" },
        st.tone === "amber" ? "正在索引，稍后即可查看提取内容。" : "暂无已提取的文本内容。");
    }
    return React.createElement(
      "div", { className: "wb-kb-chunks" },
      chunks.map(function (c, i) {
        return React.createElement(
          "div", { className: "wb-kb-chunk", key: c.id || i },
          React.createElement("div", { className: "wb-kb-chunk-ord" }, "#" + ((c.ordinal != null ? c.ordinal : i) + 1)),
          React.createElement("p", null, String(c.content || "").trim())
        );
      })
    );
  }

  function KbRelatedTab(props) {
    var relations = props.relations || [];
    if (!relations.length) {
      return React.createElement("div", { className: "wb-kb-muted pad" }, "暂无关联文档或对话。");
    }
    return React.createElement(
      "div", { className: "wb-kb-related" },
      relations.map(function (rel, i) {
        var other = props.docsById[rel.dst_id] || props.docsById[rel.src_id];
        var label = other ? docTitle(other) : (rel.dst_id || rel.src_id);
        return React.createElement(
          "button", {
            type: "button", className: "wb-kb-related-row", key: rel.id || i,
            onClick: function () { if (other) props.onSelect(other.id); },
          },
          React.createElement("span", { className: "wb-kb-related-rel" }, rel.relation || "related"),
          React.createElement("span", { className: "wb-kb-related-name" }, label)
        );
      })
    );
  }

  // ── main page ────────────────────────────────────────────────────────

  function WorkbenchKnowledgePage(props) {
    var project = props && props.project;
    var workspace = (project && project.id) || "default";

    var docsState = useState([]); var documents = docsState[0]; var setDocuments = docsState[1];
    var loadState = useState(true); var loading = loadState[0]; var setLoading = loadState[1];
    var errState = useState(""); var error = errState[0]; var setError = errState[1];
    var queryState = useState(""); var query = queryState[0]; var setQuery = queryState[1];
    var tabState = useState("all"); var activeTab = tabState[0]; var setActiveTab = tabState[1];
    var viewState = useState("grid"); var viewMode = viewState[0]; var setViewMode = viewState[1];
    var sortState = useState("updated"); var sortKey = sortState[0]; var setSortKey = sortState[1];
    var kindState = useState(""); var kindFilter = kindState[0]; var setKindFilter = kindState[1];
    var menuState = useState(null); var openMenu = menuState[0]; var setOpenMenu = menuState[1]; // "sort" | "filter" | "card:<id>"
    var menuPosState = useState(null); var menuPos = menuPosState[0]; var setMenuPos = menuPosState[1];
    var selState = useState(""); var selectedId = selState[0]; var setSelectedId = selState[1];
    var detailState = useState(null); var detail = detailState[0]; var setDetail = detailState[1];
    var detailLoadState = useState(false); var detailLoading = detailLoadState[0]; var setDetailLoading = detailLoadState[1];
    var detailTabState = useState("detail"); var detailTab = detailTabState[0]; var setDetailTab = detailTabState[1];
    var busyState = useState(false); var busy = busyState[0]; var setBusy = busyState[1];
    var fileRef = useRef(null);

    var client = useMemo(function () { return api(workspace); }, [workspace]);

    function loadDocuments() {
      setLoading(true);
      setError("");
      return client.list({ limit: 500 })
        .then(function (docs) { setDocuments(Array.isArray(docs) ? docs : []); })
        .catch(function (err) { setError(err.message || String(err)); setDocuments([]); })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () {
      // Reset selection when switching workspace/project.
      setSelectedId("");
      setDetail(null);
      loadDocuments();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [workspace]);

    function selectDoc(id) {
      setSelectedId(id);
      setDetail(null);
      setDetailLoading(true);
      client.detail(id)
        .then(function (full) { setDetail(full); })
        .catch(function () { setDetail(null); })
        .finally(function () { setDetailLoading(false); });
    }

    function triggerUpload() { if (fileRef.current) fileRef.current.click(); }

    function handleFiles(fileList) {
      var files = Array.prototype.slice.call(fileList || []);
      if (!files.length) return;
      setBusy(true);
      setError("");
      client.upload(files)
        .then(function () { return loadDocuments(); })
        .catch(function (err) { setError(err.message || String(err)); })
        .finally(function () { setBusy(false); if (fileRef.current) fileRef.current.value = ""; });
    }

    function handleDelete(doc) {
      if (!window.confirm("确定删除「" + docTitle(doc) + "」？此操作不可撤销。")) return;
      setOpenMenu(null);
      client.remove(doc.id)
        .then(function () {
          if (selectedId === doc.id) { setSelectedId(""); setDetail(null); }
          return loadDocuments();
        })
        .catch(function (err) { setError(err.message || String(err)); });
    }

    function handleReindex(doc) {
      setOpenMenu(null);
      client.reindex(doc.id)
        .then(function () { setTimeout(loadDocuments, 600); })
        .catch(function (err) { setError(err.message || String(err)); });
    }

    var docsById = useMemo(function () {
      var map = {};
      documents.forEach(function (d) { map[d.id] = d; });
      return map;
    }, [documents]);

    var selectedDoc = selectedId ? (docsById[selectedId] || (detail && detail.id === selectedId ? detail : null)) : null;

    // filter + sort (client-side, snappy over the loaded set)
    var visibleDocs = useMemo(function () {
      var q = query.trim().toLowerCase();
      var list = documents.filter(function (d) {
        if (kindFilter && visualKind(d) !== kindFilter && String(d.kind) !== kindFilter) return false;
        if (!q) return true;
        var hay = [d.title, d.name, d.summary].concat(Array.isArray(d.tags) ? d.tags : []).join(" ").toLowerCase();
        return hay.indexOf(q) >= 0;
      });
      list.sort(function (a, b) {
        if (sortKey === "name") return docTitle(a).localeCompare(docTitle(b), "zh-Hans-CN");
        if (sortKey === "size") return Number(b.size || 0) - Number(a.size || 0);
        return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
      });
      return list;
    }, [documents, query, kindFilter, sortKey]);

    var kindFilters = [
      { id: "", label: "全部类型" },
      { id: "pdf", label: "PDF" },
      { id: "doc", label: "Word 文档" },
      { id: "sheet", label: "Excel 表格" },
      { id: "slide", label: "PPT 幻灯片" },
      { id: "markdown", label: "Markdown" },
      { id: "link", label: "链接" },
      { id: "image", label: "图片" },
      { id: "code", label: "代码" },
      { id: "file", label: "其他" },
    ];
    var sortOptions = [
      { id: "updated", label: "最近更新" },
      { id: "name", label: "名称" },
      { id: "size", label: "大小" },
    ];

    // group docs for folders / tags tabs
    var groups = useMemo(function () {
      if (activeTab === "folders") {
        var bySource = {};
        visibleDocs.forEach(function (d) {
          var key = String(d.source || "other");
          (bySource[key] = bySource[key] || []).push(d);
        });
        return Object.keys(bySource).map(function (k) { return { key: k, label: sourceLabel(k), docs: bySource[k] }; });
      }
      if (activeTab === "tags") {
        var byTag = {};
        var untagged = [];
        visibleDocs.forEach(function (d) {
          var tags = Array.isArray(d.tags) ? d.tags : [];
          if (!tags.length) { untagged.push(d); return; }
          tags.forEach(function (tg) { (byTag[tg] = byTag[tg] || []).push(d); });
        });
        var out = Object.keys(byTag).sort().map(function (k) { return { key: k, label: "# " + k, docs: byTag[k] }; });
        if (untagged.length) out.push({ key: "__untagged", label: "未分类", docs: untagged });
        return out;
      }
      return null;
    }, [activeTab, visibleDocs]);

    if (!project) {
      return React.createElement("section", { className: "wb-kb-page" },
        React.createElement("div", { className: "wb-kb-empty" }, "请选择一个项目以查看其知识库。"));
    }

    var menuDoc = null;
    if (openMenu && openMenu.indexOf("card:") === 0) {
      menuDoc = docsById[openMenu.slice(5)] || null;
    }

    function renderCards(list) {
      return React.createElement(
        "div", { className: "wb-kb-grid" + (viewMode === "list" ? " list" : "") },
        list.map(function (doc) {
          return React.createElement(KbCard, {
            key: doc.id, doc: doc, listMode: viewMode === "list",
            active: doc.id === selectedId,
            onSelect: selectDoc,
            onMenu: function (d, e) {
              var rect = e && e.currentTarget ? e.currentTarget.getBoundingClientRect() : null;
              if (rect) setMenuPos({ x: Math.min(rect.right, window.innerWidth - 160), y: rect.bottom + 4 });
              setOpenMenu(openMenu === ("card:" + d.id) ? null : ("card:" + d.id));
            },
          });
        }),
        // "new knowledge" add-card (only in the default 'all' grid)
        activeTab === "all" && viewMode === "grid" && React.createElement(
          "button", { type: "button", className: "wb-kb-card add", onClick: triggerUpload, key: "__add" },
          React.createElement("svg", { width: 26, height: 26, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" },
            React.createElement("path", { d: "M12 5v14M5 12h14" })),
          React.createElement("span", null, "新建知识")
        )
      );
    }

    return React.createElement(
      "section", { className: "wb-kb-page" },
      React.createElement("input", {
        ref: fileRef, type: "file", multiple: true, className: "wb-kb-file-input",
        onChange: function (e) { handleFiles(e.target.files); },
      }),

      // main area (header / tabs / toolbar / list). The detail panel is a
      // sibling of this column so it spans the full page height.
      React.createElement(
        "div", { className: "wb-kb-main" },

      // header
      React.createElement(
        "header", { className: "wb-kb-header" },
        React.createElement(
          "div", { className: "wb-kb-header-text" },
          React.createElement(
            "div", { className: "wb-kb-title-row" },
            props.onBack && React.createElement(
              "button", { type: "button", className: "wb-kb-iconbtn wb-kb-back", onClick: props.onBack, title: "返回" },
              React.createElement("svg", { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.9, strokeLinecap: "round", strokeLinejoin: "round" },
                React.createElement("path", { d: "m15 18-6-6 6-6" }))
            ),
            React.createElement("h1", null, "知识库")
          ),
          React.createElement("p", null, "管理你的知识内容，让 Agent 基于你的知识更好地回答问题")
        ),
        React.createElement(
          "button", { type: "button", className: "wb-btn tonal", onClick: triggerUpload, disabled: busy },
          busy ? React.createElement("span", { className: "wb-kb-spin" }) : React.createElement("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2.2, strokeLinecap: "round" }, React.createElement("path", { d: "M12 5v14M5 12h14" })),
          React.createElement("span", null, busy ? "上传中…" : "新建知识")
        )
      ),

      // tabs
      React.createElement(
        "div", { className: "wb-kb-tabbar" },
        [{ id: "all", label: "全部" }, { id: "folders", label: "文件夹" }, { id: "tags", label: "标签" }].map(function (t) {
          return React.createElement("button", {
            key: t.id, type: "button", className: "wb-kb-tab" + (activeTab === t.id ? " active" : ""),
            onClick: function () { setActiveTab(t.id); },
          }, t.label);
        })
      ),

      // toolbar
      React.createElement(
        "div", { className: "wb-kb-toolbar" },
        React.createElement(
          "div", { className: "wb-kb-searchbox" },
          React.createElement("svg", { width: 15, height: 15, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.9, strokeLinecap: "round", strokeLinejoin: "round" },
            React.createElement("circle", { cx: 11, cy: 11, r: 7 }), React.createElement("path", { d: "m20 20-3.2-3.2" })),
          React.createElement("input", {
            type: "text", placeholder: "搜索知识库…", value: query,
            onChange: function (e) { setQuery(e.target.value); },
          })
        ),
        React.createElement(
          "div", { className: "wb-kb-tools" },
          // filter
          React.createElement(
            "div", { className: "wb-kb-tool-wrap" },
            React.createElement("button", {
              type: "button", className: "wb-kb-tool" + (kindFilter ? " on" : ""),
              onClick: function () { setOpenMenu(openMenu === "filter" ? null : "filter"); },
            },
              React.createElement("svg", { width: 15, height: 15, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" }, React.createElement("path", { d: "M3 5h18l-7 8v6l-4-2v-4Z" })),
              React.createElement("span", null, "筛选")),
            openMenu === "filter" && React.createElement(
              "div", { className: "wb-kb-menu" },
              kindFilters.map(function (k) {
                return React.createElement("button", {
                  key: k.id || "all", type: "button", className: (kindFilter === k.id ? "sel" : ""),
                  onClick: function () { setKindFilter(k.id); setOpenMenu(null); },
                }, k.label);
              })
            )
          ),
          // sort
          React.createElement(
            "div", { className: "wb-kb-tool-wrap" },
            React.createElement("button", {
              type: "button", className: "wb-kb-tool",
              onClick: function () { setOpenMenu(openMenu === "sort" ? null : "sort"); },
            },
              React.createElement("svg", { width: 15, height: 15, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" }, React.createElement("path", { d: "M3 6h12M3 12h8M3 18h5M17 8V6m0 0 3 3m-3-3-3 3M17 6v12" })),
              React.createElement("span", null, "排序")),
            openMenu === "sort" && React.createElement(
              "div", { className: "wb-kb-menu" },
              sortOptions.map(function (o) {
                return React.createElement("button", {
                  key: o.id, type: "button", className: (sortKey === o.id ? "sel" : ""),
                  onClick: function () { setSortKey(o.id); setOpenMenu(null); },
                }, o.label);
              })
            )
          ),
          // view toggle
          React.createElement(
            "div", { className: "wb-kb-viewtoggle" },
            React.createElement("button", { type: "button", className: viewMode === "grid" ? "on" : "", title: "网格", onClick: function () { setViewMode("grid"); } },
              React.createElement("svg", { width: 15, height: 15, viewBox: "0 0 24 24", fill: "currentColor" }, React.createElement("rect", { x: 3, y: 3, width: 8, height: 8, rx: 1.5 }), React.createElement("rect", { x: 13, y: 3, width: 8, height: 8, rx: 1.5 }), React.createElement("rect", { x: 3, y: 13, width: 8, height: 8, rx: 1.5 }), React.createElement("rect", { x: 13, y: 13, width: 8, height: 8, rx: 1.5 }))),
            React.createElement("button", { type: "button", className: viewMode === "list" ? "on" : "", title: "列表", onClick: function () { setViewMode("list"); } },
              React.createElement("svg", { width: 15, height: 15, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" }, React.createElement("path", { d: "M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" })))
          )
        )
      ),

      error && React.createElement("div", { className: "wb-kb-error" }, error),

      // list column (scrolls); the "共 N 个知识" count is pinned at the bottom
      React.createElement(
        "div", { className: "wb-kb-list-col" },
        React.createElement(
          "div", { className: "wb-kb-scroll" },
          loading
            ? React.createElement("div", { className: "wb-kb-empty" }, "加载知识库中…")
            : (visibleDocs.length === 0
              ? React.createElement(
                "div", { className: "wb-kb-empty" },
                React.createElement("div", { className: "wb-kb-empty-icon" },
                  React.createElement("svg", { width: 40, height: 40, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.4, strokeLinecap: "round", strokeLinejoin: "round" },
                    React.createElement("path", { d: "M5 4.5A2.5 2.5 0 0 1 7.5 2H20v15H7.5A2.5 2.5 0 0 0 5 19.5Z" }), React.createElement("path", { d: "M5 19.5A2.5 2.5 0 0 0 7.5 22H20" }))),
                React.createElement("p", null, query || kindFilter ? "没有匹配的知识。" : "还没有知识内容。"),
                React.createElement("button", { type: "button", className: "wb-btn primary", onClick: triggerUpload }, "上传第一个文件")
              )
              : (groups
                ? React.createElement(
                  "div", { className: "wb-kb-groups" },
                  groups.map(function (g) {
                    return React.createElement(
                      "div", { className: "wb-kb-group", key: g.key },
                      React.createElement("div", { className: "wb-kb-group-head" }, React.createElement("span", null, g.label), React.createElement("small", null, g.docs.length)),
                      renderCards(g.docs)
                    );
                  })
                )
                : renderCards(visibleDocs))
            )
        ),
        React.createElement("div", { className: "wb-kb-count" }, "共 " + visibleDocs.length + " 个知识")
      )
      ),

      // detail panel — full-height right column (sibling of .wb-kb-main)
      selectedDoc
        ? React.createElement(KbDetailPanel, {
          doc: selectedDoc, detail: detail, detailLoading: detailLoading,
          tab: detailTab, setTab: setDetailTab,
          rawUrl: client.rawUrl(selectedDoc.id),
          docsById: docsById, onSelect: selectDoc,
        })
        : React.createElement(
          "aside", { className: "wb-kb-detail empty" },
          React.createElement("div", { className: "wb-kb-detail-placeholder" },
            React.createElement("svg", { width: 34, height: 34, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.4, strokeLinecap: "round", strokeLinejoin: "round" },
              React.createElement("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" }), React.createElement("path", { d: "M14 3v5h5" })),
            React.createElement("p", null, "选择一个知识查看详情"))
        ),

      // card / tool menu scrim + card menu popover
      openMenu && React.createElement("div", { className: "wb-kb-scrim", onClick: function () { setOpenMenu(null); } }),
      menuDoc && React.createElement(
        "div", {
          className: "wb-kb-cardmenu", onClick: function (e) { e.stopPropagation(); },
          style: menuPos ? { top: menuPos.y + "px", left: menuPos.x + "px" } : undefined,
        },
        React.createElement("button", { type: "button", onClick: function () { setOpenMenu(null); selectDoc(menuDoc.id); } }, "查看详情"),
        React.createElement("button", { type: "button", onClick: function () { handleReindex(menuDoc); } }, "重新索引"),
        React.createElement("a", { href: client.rawUrl(menuDoc.id), target: "_blank", rel: "noreferrer", onClick: function () { setOpenMenu(null); } }, "查看原文件"),
        React.createElement("button", { type: "button", className: "danger", onClick: function () { handleDelete(menuDoc); } }, "删除")
      )
    );
  }

  window.WorkbenchKnowledgePage = WorkbenchKnowledgePage;
})();
