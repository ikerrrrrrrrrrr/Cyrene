// Knowledge page — file management, search, and knowledge map
(function () {
  const { useState: useStateK, useEffect: useEffectK, useMemo: useMemoK, useRef: useRefK } = React;

  // ── Utility Functions ──────────────────────────────────────────────

  function formatDate(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleDateString();
    } catch (e) { return iso; }
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleString();
    } catch (e) { return iso; }
  }

  function formatFileSize(bytes) {
    if (!bytes) return "0 B";
    var units = ["B", "KB", "MB", "GB"];
    var size = bytes;
    var unitIdx = 0;
    while (size >= 1024 && unitIdx < units.length - 1) {
      size /= 1024;
      unitIdx++;
    }
    return size.toFixed(1) + " " + units[unitIdx];
  }

  function getKindIcon(kind) {
    var icons = {
      "pdf": "📄",
      "image": "🖼",
      "code": "</> ",
      "file": "📋",
      "map": "🗺"
    };
    return icons[kind] || "📋";
  }

  // ── API Calls ──────────────────────────────────────────────────────

  async function fetchDocuments(params) {
    var qs = new URLSearchParams(params || {}).toString();
    var url = "/api/knowledge/documents" + (qs ? "?" + qs : "");
    var r = await fetch(url);
    return r.ok ? r.json() : [];
  }

  async function fetchDocumentDetail(id) {
    var r = await fetch("/api/knowledge/documents/" + id);
    return r.ok ? r.json() : null;
  }

  async function fetchStats() {
    var r = await fetch("/api/knowledge/stats");
    return r.ok ? r.json() : null;
  }

  async function uploadFiles(files) {
    var fd = new FormData();
    for (var i = 0; i < files.length; i++) {
      fd.append("files", files[i]);
    }
    var r = await fetch("/api/knowledge/documents", {
      method: "POST",
      body: fd
    });
    return r.ok ? r.json() : null;
  }

  async function syncDocuments() {
    var r = await fetch("/api/knowledge/sync", { method: "POST" });
    return r.ok ? r.json() : null;
  }

  async function searchDocuments(query, k) {
    var qs = new URLSearchParams({ q: query, k: k || 8 }).toString();
    var url = "/api/knowledge/search?" + qs;
    var r = await fetch(url);
    if (!r.ok) return [];
    var data = await r.json();
    // Backend wraps results as {results: [...]}
    return (data && data.results) || [];
  }

  async function updateDocument(id, body) {
    var r = await fetch("/api/knowledge/documents/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return r.ok ? r.json() : null;
  }

  async function reindexDocument(id) {
    var r = await fetch("/api/knowledge/documents/" + id + "/reindex", {
      method: "POST"
    });
    return r.ok;
  }

  async function deleteDocument(id) {
    var r = await fetch("/api/knowledge/documents/" + id, {
      method: "DELETE"
    });
    return r.ok;
  }

  // ── Files Tab Sub-component ────────────────────────────────────────

  function FilesTab(props) {
    var t = props.t;
    var selectedDocVal = props.selectedDoc;
    var setSelectedDoc = props.setSelectedDoc;
    var documents = useStateK([]);
    var setDocuments = documents[1];
    var documentsVal = documents[0];
    var loading = useStateK(true);
    var setLoading = loading[1];
    var loadingVal = loading[0];
    var stats = useStateK(null);
    var setStats = stats[1];
    var statsVal = stats[0];
    var searchFilter = useStateK("");
    var setSearchFilter = searchFilter[1];
    var searchFilterVal = searchFilter[0];
    var sourceFilter = useStateK("");
    var setSourceFilter = sourceFilter[1];
    var sourceFilterVal = sourceFilter[0];
    var uploadProgress = useStateK(false);
    var setUploadProgress = uploadProgress[1];
    var uploadProgressVal = uploadProgress[0];
    var pollingInterval = useStateK(null);
    var setPollingInterval = pollingInterval[1];
    var detail = useStateK(null);
    var setDetail = detail[1];
    var detailVal = detail[0];
    var tagInput = useStateK("");
    var setTagInput = tagInput[1];
    var tagInputVal = tagInput[0];
    var savingTags = useStateK(false);
    var setSavingTags = savingTags[1];
    var savingTagsVal = savingTags[0];

    function loadDetail(id) {
      fetchDocumentDetail(id)
        .then(function (d) {
          if (d && !d.error) { setDetail(d); setTagInput((d.tags || []).join(", ")); }
        })
        .catch(function (e) { console.error(e); });
    }

    function loadDocuments() {
      setLoading(true);
      var params = {};
      if (searchFilterVal) params.q = searchFilterVal;
      if (sourceFilterVal) params.source = sourceFilterVal;
      fetchDocuments(params)
        .then(function (data) {
          setDocuments(data || []);
          setLoading(false);
        })
        .catch(function (e) {
          console.error(e);
          setDocuments([]);
          setLoading(false);
        });
    }

    function loadStats() {
      fetchStats()
        .then(function (s) { setStats(s); })
        .catch(function (e) { console.error(e); });
    }

    useEffectK(function () {
      loadDocuments();
      loadStats();
    }, [searchFilterVal, sourceFilterVal]);

    // Poll for status changes
    useEffectK(function () {
      var hasIncomplete = (documentsVal || []).some(function (d) {
        return d.status === "pending" || d.status === "parsing";
      });

      if (hasIncomplete && !pollingInterval) {
        var interval = setInterval(function () {
          loadDocuments();
        }, 3000);
        setPollingInterval(interval);
      } else if (!hasIncomplete && pollingInterval) {
        clearInterval(pollingInterval);
        setPollingInterval(null);
      }

      return function () {
        if (pollingInterval) {
          clearInterval(pollingInterval);
        }
      };
    }, [documentsVal, pollingInterval]);

    // Fetch full detail (summary/chunks/relations/tags) whenever the selection changes
    useEffectK(function () {
      if (!selectedDocVal) { setDetail(null); setTagInput(""); return; }
      loadDetail(selectedDocVal.id);
    }, [selectedDocVal && selectedDocVal.id]);

    function handleSaveTags() {
      if (!selectedDocVal) return;
      var tags = tagInputVal.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      setSavingTags(true);
      updateDocument(selectedDocVal.id, { tags: tags })
        .then(function (updated) {
          setSavingTags(false);
          if (updated && !updated.error) { loadDetail(selectedDocVal.id); loadDocuments(); }
        })
        .catch(function (e) { console.error(e); setSavingTags(false); });
    }

    function handleUpload(e) {
      var files = e.target.files;
      if (!files || files.length === 0) return;
      setUploadProgress(true);
      uploadFiles(files)
        .then(function () {
          loadDocuments();
          setUploadProgress(false);
          e.target.value = "";
        })
        .catch(function (err) {
          console.error(err);
          setUploadProgress(false);
        });
    }

    function handleSync() {
      setUploadProgress(true);
      syncDocuments()
        .then(function () {
          loadDocuments();
          setUploadProgress(false);
        })
        .catch(function (err) {
          console.error(err);
          setUploadProgress(false);
        });
    }

    function handleReindex() {
      if (!selectedDocVal) return;
      reindexDocument(selectedDocVal.id)
        .then(function () {
          loadDocuments();
        })
        .catch(function (e) { console.error(e); });
    }

    function handleDelete() {
      if (!selectedDocVal) return;
      if (!confirm(t("knowledge.confirmDelete"))) return;
      deleteDocument(selectedDocVal.id)
        .then(function () {
          setSelectedDoc(null);
          loadDocuments();
        })
        .catch(function (e) { console.error(e); });
    }

    var statusColor = function (status) {
      var colors = {
        "pending": "var(--warn)",
        "parsing": "var(--text-3)",
        "indexed": "var(--success, #22c55e)",
        "error": "var(--err)"
      };
      return colors[status] || "var(--text-3)";
    };

    return React.createElement("div", { className: "kb-files-tab" },
      // Toolbar
      React.createElement("div", { className: "kb-toolbar" },
        React.createElement("label", { className: "btn" },
          React.createElement("input", {
            type: "file",
            multiple: true,
            onChange: handleUpload,
            disabled: uploadProgressVal,
            style: { display: "none" }
          }),
          uploadProgressVal ? "Uploading…" : "+ " + t("knowledge.upload")
        ),
        React.createElement("button", {
          className: "btn",
          onClick: handleSync,
          disabled: uploadProgressVal
        }, t("knowledge.sync")),
        React.createElement("div", { className: "kb-search-wrap" },
          React.createElement("input", {
            type: "text",
            className: "kb-search-input",
            placeholder: t("knowledge.searchFiles"),
            value: searchFilterVal,
            onChange: function (e) { setSearchFilter(e.target.value); }
          })
        ),
        React.createElement("select", {
          className: "kb-source-filter",
          value: sourceFilterVal,
          onChange: function (e) { setSourceFilter(e.target.value); },
          title: t("knowledge.sourceAll")
        },
          React.createElement("option", { value: "" }, t("knowledge.sourceAll")),
          ["chat_upload", "kb_upload", "generated", "import"].map(function (s) {
            return React.createElement("option", { key: s, value: s }, t("knowledge.source." + s) || s);
          })
        )
      ),

      // Stats bar
      statsVal && React.createElement("div", { className: "kb-stats-bar" },
        React.createElement("span", null, statsVal.documents + " " + t("knowledge.documents")),
        React.createElement("span", { className: "kb-stats-sep" }),
        React.createElement("span", null, statsVal.chunks + " " + t("knowledge.chunks")),
        statsVal.embedding_configured && React.createElement(React.Fragment, null,
          React.createElement("span", { className: "kb-stats-sep" }),
          React.createElement("span", { style: { color: "var(--success, #22c55e)" } }, t("knowledge.embeddingOn"))
        )
      ),

      // Documents list
      React.createElement("div", { className: "kb-documents-list" },
        loadingVal
          ? React.createElement("div", { className: "kb-empty" }, "…")
          : documentsVal.length === 0
            ? React.createElement("div", { className: "kb-empty" }, t("knowledge.noDocuments"))
            : documentsVal.map(function (doc) {
              return React.createElement("div", {
                key: doc.id,
                className: "kb-doc-row" + (selectedDocVal && selectedDocVal.id === doc.id ? " active" : ""),
                onClick: function () { setSelectedDoc(doc); }
              },
                React.createElement("span", { className: "kb-doc-icon" }, getKindIcon(doc.kind)),
                React.createElement("div", { className: "kb-doc-info" },
                  React.createElement("div", { className: "kb-doc-name" }, doc.name),
                  React.createElement("div", { className: "kb-doc-meta" },
                    React.createElement("span", { className: "kb-status-badge kb-status-" + doc.status, style: { color: statusColor(doc.status) } },
                      t("knowledge.status." + doc.status) || doc.status
                    ),
                    React.createElement("span", null, formatFileSize(doc.size)),
                    React.createElement("span", null, doc.chunk_count + " chunks"),
                    React.createElement("span", { className: "kb-doc-src" }, t("knowledge.source." + doc.source) || doc.source),
                    React.createElement("span", { className: "kb-doc-time" }, formatDate(doc.updated_at))
                  )
                )
              );
            })
      ),

      // Detail panel
      selectedDocVal && React.createElement("div", { className: "kb-detail-panel" },
        React.createElement("div", { className: "kb-detail-header" },
          React.createElement("h3", null, selectedDocVal.name),
          React.createElement("button", {
            className: "kb-detail-close",
            onClick: function () { setSelectedDoc(null); }
          }, "×")
        ),
        // status + error meta
        React.createElement("div", { className: "kb-detail-meta" },
          React.createElement("span", { className: "kb-status-badge kb-status-" + selectedDocVal.status, style: { color: statusColor(selectedDocVal.status) } },
            t("knowledge.status." + selectedDocVal.status) || selectedDocVal.status
          ),
          (detailVal && detailVal.error) ? React.createElement("span", { className: "kb-detail-error" }, detailVal.error) : null
        ),
        // summary
        (detailVal && detailVal.summary) && React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" }, t("knowledge.summary")),
          React.createElement("div", { className: "kb-detail-content" }, detailVal.summary)
        ),
        // editable tags
        React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" }, t("knowledge.tags")),
          React.createElement("div", { className: "kb-tag-edit" },
            React.createElement("input", {
              type: "text",
              className: "kb-search-input",
              placeholder: t("knowledge.tagsPlaceholder"),
              value: tagInputVal,
              onChange: function (e) { setTagInput(e.target.value); }
            }),
            React.createElement("button", {
              className: "btn",
              onClick: handleSaveTags,
              disabled: savingTagsVal
            }, savingTagsVal ? "…" : t("knowledge.saveTags"))
          )
        ),
        // chunk list
        React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" },
            t("knowledge.chunks") + (detailVal && detailVal.chunks ? " (" + detailVal.chunks.length + ")" : "")
          ),
          React.createElement("div", { className: "kb-chunk-list" },
            (detailVal && detailVal.chunks && detailVal.chunks.length > 0)
              ? detailVal.chunks.map(function (ch) {
                return React.createElement("div", { key: ch.id, className: "kb-chunk-item" },
                  React.createElement("span", { className: "kb-chunk-ord" }, "#" + ch.ordinal),
                  React.createElement("span", { className: "kb-chunk-text" }, ch.content)
                );
              })
              : React.createElement("div", { className: "kb-empty" }, t("knowledge.noChunks"))
          )
        ),
        // actions
        React.createElement("div", { className: "kb-detail-actions" },
          React.createElement("button", {
            className: "btn",
            onClick: handleReindex
          }, t("knowledge.reindex")),
          React.createElement("button", {
            className: "btn danger",
            onClick: handleDelete
          }, t("knowledge.delete")),
          selectedDocVal.path && React.createElement("a", {
            className: "btn",
            href: "/api/knowledge/documents/" + selectedDocVal.id + "/raw",
            download: true
          }, t("knowledge.download"))
        )
      )
    );
  }

  // ── Search Tab Sub-component ───────────────────────────────────────

  function SearchTab(props) {
    var t = props.t;
    var query = useStateK("");
    var setQuery = query[1];
    var queryVal = query[0];
    var results = useStateK([]);
    var setResults = results[1];
    var resultsVal = results[0];
    var searching = useStateK(false);
    var setSearching = searching[1];
    var searchingVal = searching[0];
    var stats = useStateK(null);
    var setStats = stats[1];
    var statsVal = stats[0];

    function loadStats() {
      fetchStats()
        .then(function (s) { setStats(s); })
        .catch(function (e) { console.error(e); });
    }

    useEffectK(function () {
      loadStats();
    }, []);

    function handleSearch(e) {
      e.preventDefault();
      if (!queryVal.trim()) {
        setResults([]);
        return;
      }
      setSearching(true);
      searchDocuments(queryVal, 8)
        .then(function (res) {
          setResults(res || []);
          setSearching(false);
        })
        .catch(function (e) {
          console.error(e);
          setSearching(false);
        });
    }

    return React.createElement("div", { className: "kb-search-tab" },
      // Status banner
      !statsVal || !statsVal.embedding_configured ? React.createElement("div", { className: "kb-search-hint" },
        t("knowledge.embeddingOff")
      ) : null,

      // Search form
      React.createElement("form", { className: "kb-search-form", onSubmit: handleSearch },
        React.createElement("input", {
          type: "text",
          className: "kb-search-input",
          placeholder: t("knowledge.searchQuery"),
          value: queryVal,
          onChange: function (e) { setQuery(e.target.value); },
          autoFocus: true
        }),
        React.createElement("button", {
          type: "submit",
          className: "btn primary",
          disabled: searchingVal
        }, searchingVal ? t("knowledge.searching") : t("knowledge.search"))
      ),

      // Results
      React.createElement("div", { className: "kb-search-results" },
        resultsVal.length === 0 && !searchingVal
          ? React.createElement("div", { className: "kb-empty" }, t("knowledge.noResults"))
          : searchingVal
            ? React.createElement("div", { className: "kb-empty" }, "…")
            : resultsVal.map(function (res, i) {
              return React.createElement("div", { key: i, className: "kb-result-card" },
                React.createElement("div", { className: "kb-result-header" },
                  React.createElement("span", { className: "kb-result-doc" }, res.document_name),
                  React.createElement("span", { className: "kb-result-mode kb-mode-" + res.mode }, res.mode),
                  React.createElement("span", { className: "kb-result-score" }, (res.score || 0).toFixed(3))
                ),
                React.createElement("div", { className: "kb-result-content" }, res.content)
              );
            })
      )
    );
  }

  // ── Graph API Calls ───────────────────────────────────────────────

  async function fetchGraph(includeAuto) {
    var qs = new URLSearchParams({ include_auto: includeAuto ? "true" : "false" }).toString();
    var url = "/api/knowledge/graph?" + qs;
    try {
      var r = await fetch(url);
      return r.ok ? r.json() : { nodes: [], edges: [] };
    } catch (e) {
      console.error("fetchGraph error:", e);
      return { nodes: [], edges: [] };
    }
  }

  async function createRelation(srcId, dstId, relation, weight) {
    try {
      var r = await fetch("/api/knowledge/relations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          src_id: srcId,
          dst_id: dstId,
          relation: relation,
          weight: weight || 1.0
        })
      });
      return r.ok ? r.json() : null;
    } catch (e) {
      console.error("createRelation error:", e);
      return null;
    }
  }

  async function updateRelation(id, relation, weight) {
    try {
      var r = await fetch("/api/knowledge/relations/" + id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          relation: relation,
          weight: weight !== undefined ? weight : undefined
        })
      });
      return r.ok ? r.json() : null;
    } catch (e) {
      console.error("updateRelation error:", e);
      return null;
    }
  }

  async function deleteRelation(id) {
    try {
      var r = await fetch("/api/knowledge/relations/" + id, {
        method: "DELETE"
      });
      return r.ok;
    } catch (e) {
      console.error("deleteRelation error:", e);
      return false;
    }
  }

  // ── KbGraph Sub-component ──────────────────────────────────────────

  function KbGraph(props) {
    var t = props.t;
    var onNodeClick = props.onNodeClick;
    var containerRef = useRefK(null);
    var networkRef = useRefK(null);
    var nodesRef = useRefK(null);
    var edgesRef = useRefK(null);
    var includeAuto = useStateK(false);
    var setIncludeAuto = includeAuto[1];
    var includeAutoVal = includeAuto[0];
    var selectedEdgeId = useStateK(null);
    var setSelectedEdgeId = selectedEdgeId[1];
    var selectedEdgeIdVal = selectedEdgeId[0];
    var nodeDetail = useStateK(null);
    var setNodeDetail = nodeDetail[1];
    var nodeDetailVal = nodeDetail[0];

    var kindColors = {
      "pdf":   { background: "#e0707a", border: "#bf5a64" },
      "code":  { background: "#5fb0e8", border: "#4490c8" },
      "image": { background: "#c07fd8", border: "#a062bc" },
      "file":  { background: "#7f8a9b", border: "#67707e" },
      "map":   { background: "#8fc079", border: "#74a460" }
    };

    var getNodeColor = function (kind) {
      var c = kindColors[kind] || kindColors.file;
      return { background: c.background, border: c.border, highlight: { background: c.background, border: "#ffffff" }, hover: { background: c.background, border: "#ffffff" } };
    };

    // vis-network draws on <canvas> and cannot resolve CSS variables, so read the
    // theme colors once and pass concrete values.
    var _cssVar = function (name, fallback) {
      try { return (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fallback; }
      catch (e) { return fallback; }
    };
    var themeText = _cssVar("--text", "#e6e6e6");
    var themeAccent = _cssVar("--accent", "#5ec59e");

    // Load graph data into the given DataSets so each request updates the instances
    // bound to vis-network rather than transient component state.
    var loadGraphInto = function (nodesDS, edgesDS, includeAutoFlag) {
      if (!nodesDS || !edgesDS) return;
      fetchGraph(includeAutoFlag).then(function (data) {
        var nodeData = (data.nodes || []).map(function (n) {
          return {
            id: n.id,
            label: n.label || n.id,
            title: n.label || n.id,
            color: getNodeColor(n.kind),
            kind: n.kind || "file"
          };
        });

        var edgeData = (data.edges || []).map(function (e) {
          var isAuto = e.source === "auto";
          return {
            id: e.id,
            from: e.from,
            to: e.to,
            label: isAuto ? (e.weight ? e.weight.toFixed(2) : "similar") : (e.relation || "related"),
            title: e.relation || "related",
            dashes: isAuto ? [6, 4] : false,
            color: { color: isAuto ? "#8aa0c0" : themeAccent, opacity: 0.95 },
            width: isAuto ? 1.5 : 2.5,
            source: e.source
          };
        });

        nodesDS.clear();
        edgesDS.clear();
        nodesDS.add(nodeData);
        edgesDS.add(edgeData);
      });
    };

    // Initialize network once on mount
    useEffectK(function () {
      if (!window.vis) {
        console.warn("vis-network not loaded");
        return;
      }

      var container = document.getElementById("kb-graph");
      if (!container) return;

      containerRef.current = container;

      var nodes = new vis.DataSet([]);
      var edges = new vis.DataSet([]);
      nodesRef.current = nodes;
      edgesRef.current = edges;

      var options = {
        physics: {
          enabled: true,
          forceAtlas2Based: {
            gravitationalConstant: -45,
            centralGravity: 0.015,
            springLength: 110,
            springConstant: 0.12
          },
          maxVelocity: 50,
          solver: "forceAtlas2Based",
          timestep: 0.35,
          stabilization: { iterations: 150 }
        },
        interaction: {
          hover: true,
          navigationButtons: false,
          keyboard: false,
          zoomView: true,
          dragView: true,
          tooltipDelay: 150
        },
        manipulation: {
          enabled: false,
          addEdge: function (edgeData, callback) {
            if (edgeData.from === edgeData.to) {
              callback(null);
              return;
            }
            createRelation(edgeData.from, edgeData.to, "related", 1.0).then(function (created) {
              if (created) {
                edgeData.id = created.id;
                edgeData.source = "manual";
                edgeData.label = created.relation || "related";
                edgeData.title = created.relation || "related";
                edgeData.width = 2;
                callback(edgeData);
              } else {
                callback(null);
              }
            });
          }
        },
        edges: {
          smooth: { type: "continuous" },
          color: { color: "#808a99", highlight: themeAccent, hover: themeAccent },
          width: 1.5,
          font: { size: 12, color: themeText, strokeWidth: 3, strokeColor: "rgba(0,0,0,0.25)" }
        },
        nodes: {
          shape: "dot",
          size: 16,
          borderWidth: 2,
          font: { size: 13, color: themeText, face: "inherit" }
        }
      };

      var network = new vis.Network(container, { nodes: nodes, edges: edges }, options);
      networkRef.current = network;

      // Frame the graph once physics settles (avoids nodes drifting off-canvas)
      network.on("stabilizationIterationsDone", function () {
        try { network.fit({ animation: false }); } catch (e) {}
      });

      // Node click: open the document detail on the right, staying on the map
      network.on("click", function (params) {
        if (params.nodes && params.nodes.length > 0) {
          var nodeId = params.nodes[0];
          fetchDocumentDetail(nodeId).then(function (d) {
            if (d && !d.error) setNodeDetail(d);
          }).catch(function (e) { console.error(e); });
        }
      });

      // Double-click edge to edit
      network.on("doubleClick", function (params) {
        if (params.edges && params.edges.length > 0) {
          var edgeId = params.edges[0];
          var edge = edges.get(edgeId);
          if (edge && edge.source === "manual") {
            var newLabel = prompt(t("knowledge.graph.editRelation"), edge.label);
            if (newLabel) {
              updateRelation(edgeId, newLabel).then(function (updated) {
                if (updated) {
                  edge.label = newLabel;
                  edge.title = newLabel;
                  edges.update(edge);
                }
              });
            }
          } else if (edge && edge.source === "auto") {
            alert(t("knowledge.graph.autoEdgeReadOnly"));
          }
        }
      });

      // Track selected edge
      network.on("selectEdge", function (params) {
        setSelectedEdgeId(params.edges && params.edges.length > 0 ? params.edges[0] : null);
      });

      network.on("deselectEdge", function () {
        setSelectedEdgeId(null);
      });

      return function () {
        if (network) {
          network.destroy();
        }
        containerRef.current = null;
        networkRef.current = null;
        nodesRef.current = null;
        edgesRef.current = null;
      };
    }, []);

    // Reload graph when includeAuto toggles
    useEffectK(function () {
      if (networkRef.current && nodesRef.current && edgesRef.current) {
        loadGraphInto(nodesRef.current, edgesRef.current, includeAutoVal);
      }
    }, [includeAutoVal]);

    var handleAddEdgeMode = function () {
      if (!networkRef.current) return;
      networkRef.current.addEdgeMode();
    };

    var handleDeleteEdge = function () {
      if (!selectedEdgeIdVal) {
        alert(t("knowledge.graph.selectEdgeFirst"));
        return;
      }

      var edge = edgesRef.current.get(selectedEdgeIdVal);
      if (edge && edge.source === "auto") {
        alert(t("knowledge.graph.autoEdgeCannotDelete"));
        return;
      }

      if (!confirm(t("knowledge.graph.confirmDeleteEdge"))) return;

      deleteRelation(selectedEdgeIdVal).then(function (ok) {
        if (ok) {
          edgesRef.current.remove(selectedEdgeIdVal);
          setSelectedEdgeId(null);
          networkRef.current.unselectAll();
        }
      });
    };

    var handleRelayout = function () {
      if (!networkRef.current) return;
      networkRef.current.stabilize();
    };

    var nodeStatusColor = function (status) {
      var colors = { "pending": "var(--warn)", "parsing": "var(--text-3)", "indexed": "var(--success, #22c55e)", "error": "var(--err)" };
      return colors[status] || "var(--text-3)";
    };

    var handleNodeReindex = function () {
      if (!nodeDetailVal) return;
      reindexDocument(nodeDetailVal.id).catch(function (e) { console.error(e); });
    };

    var handleNodeDelete = function () {
      if (!nodeDetailVal) return;
      if (!confirm(t("knowledge.confirmDelete"))) return;
      deleteDocument(nodeDetailVal.id).then(function (ok) {
        if (ok) {
          if (nodesRef.current) { try { nodesRef.current.remove(nodeDetailVal.id); } catch (e) {} }
          setNodeDetail(null);
        }
      }).catch(function (e) { console.error(e); });
    };

    return React.createElement("div", { className: "kb-map-tab" },
      // Toolbar
      React.createElement("div", { className: "kb-toolbar" },
        React.createElement("label", { className: "kb-toolbar-item" },
          React.createElement("input", {
            type: "checkbox",
            checked: includeAutoVal,
            onChange: function (e) { setIncludeAuto(e.target.checked); }
          }),
          " " + t("knowledge.graph.autoEdges")
        ),
        React.createElement("button", {
          className: "btn",
          onClick: handleAddEdgeMode
        }, t("knowledge.graph.addLink")),
        React.createElement("button", {
          className: "btn",
          onClick: handleDeleteEdge
        }, t("knowledge.graph.deleteLink")),
        React.createElement("button", {
          className: "btn",
          onClick: handleRelayout
        }, t("knowledge.graph.relayout"))
      ),

      // Graph container
      React.createElement("div", { id: "kb-graph", className: "kb-graph" }),

      // Node detail panel — opens on the right, stays on the map
      nodeDetailVal && React.createElement("div", { className: "kb-detail-panel" },
        React.createElement("div", { className: "kb-detail-header" },
          React.createElement("h3", null, nodeDetailVal.name),
          React.createElement("button", { className: "kb-detail-close", onClick: function () { setNodeDetail(null); } }, "×")
        ),
        React.createElement("div", { className: "kb-detail-meta" },
          React.createElement("span", {
            className: "kb-status-badge kb-status-" + nodeDetailVal.status,
            style: { color: nodeStatusColor(nodeDetailVal.status) }
          }, t("knowledge.status." + nodeDetailVal.status) || nodeDetailVal.status)
        ),
        nodeDetailVal.summary && React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" }, t("knowledge.summary")),
          React.createElement("div", { className: "kb-detail-content" }, nodeDetailVal.summary)
        ),
        (nodeDetailVal.tags && nodeDetailVal.tags.length > 0) && React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" }, t("knowledge.tags")),
          React.createElement("div", { className: "kb-detail-tags" },
            nodeDetailVal.tags.map(function (tag, i) { return React.createElement("span", { key: i, className: "kb-tag" }, tag); })
          )
        ),
        React.createElement("div", { className: "kb-detail-section" },
          React.createElement("div", { className: "kb-detail-label" },
            t("knowledge.chunks") + (nodeDetailVal.chunks ? " (" + nodeDetailVal.chunks.length + ")" : "")
          ),
          React.createElement("div", { className: "kb-chunk-list" },
            (nodeDetailVal.chunks && nodeDetailVal.chunks.length > 0)
              ? nodeDetailVal.chunks.map(function (ch) {
                return React.createElement("div", { key: ch.id, className: "kb-chunk-item" },
                  React.createElement("span", { className: "kb-chunk-ord" }, "#" + ch.ordinal),
                  React.createElement("span", { className: "kb-chunk-text" }, ch.content)
                );
              })
              : React.createElement("div", { className: "kb-empty" }, t("knowledge.noChunks"))
          )
        ),
        React.createElement("div", { className: "kb-detail-actions" },
          React.createElement("button", { className: "btn", onClick: handleNodeReindex }, t("knowledge.reindex")),
          React.createElement("button", { className: "btn danger", onClick: handleNodeDelete }, t("knowledge.delete")),
          nodeDetailVal.path && React.createElement("a", {
            className: "btn",
            href: "/api/knowledge/documents/" + nodeDetailVal.id + "/raw",
            download: true
          }, t("knowledge.download"))
        )
      )
    );
  }

  // ── Map Tab ────────────────────────────────────────────────────────

  function MapTab(props) {
    var t = props.t;
    var onNodeClick = props.onNodeClick;

    if (!window.vis) {
      return React.createElement("div", { className: "kb-map-tab" },
        React.createElement("div", { className: "kb-empty" },
          "vis-network library not loaded. Please check your CDN connection."
        )
      );
    }

    return React.createElement(KbGraph, { t: t, onNodeClick: onNodeClick });
  }

  // ── Main Knowledge Page Component ──────────────────────────────────

  function KnowledgePage() {
    useDataVersion();
    var ti18n = useI18n();
    var t = ti18n.t;
    var activeTab = useStateK("files");
    var setActiveTab = activeTab[1];
    var activeTabVal = activeTab[0];
    var selectedDoc = useStateK(null);
    var setSelectedDoc = selectedDoc[1];
    var selectedDocVal = selectedDoc[0];

    var handleNodeClick = function (nodeId) {
      // Fetch the document and select it, switch to files tab
      fetchDocumentDetail(nodeId).then(function (doc) {
        if (doc) {
          setSelectedDoc(doc);
          setActiveTab("files");
        }
      });
    };

    return React.createElement("div", { className: "page knowledge-page" },
      React.createElement("div", { className: "kb-tabs-header" },
        React.createElement("h2", { className: "kb-page-title" }, t("knowledge.title"))
      ),

      React.createElement("div", { className: "kb-tabs" },
        React.createElement("button", {
          className: "kb-tab" + (activeTabVal === "files" ? " active" : ""),
          onClick: function () { setActiveTab("files"); }
        }, t("knowledge.tabs.files")),
        React.createElement("button", {
          className: "kb-tab" + (activeTabVal === "search" ? " active" : ""),
          onClick: function () { setActiveTab("search"); }
        }, t("knowledge.tabs.search")),
        React.createElement("button", {
          className: "kb-tab" + (activeTabVal === "map" ? " active" : ""),
          onClick: function () { setActiveTab("map"); }
        }, t("knowledge.tabs.map"))
      ),

      React.createElement("div", { className: "kb-content" },
        activeTabVal === "files" && React.createElement(FilesTab, { t: t, selectedDoc: selectedDocVal, setSelectedDoc: setSelectedDoc }),
        activeTabVal === "search" && React.createElement(SearchTab, { t: t }),
        activeTabVal === "map" && React.createElement(MapTab, { t: t, onNodeClick: handleNodeClick })
      )
    );
  }

  window.KnowledgePage = KnowledgePage;
})();
