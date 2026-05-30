// Entities page — list, kanban, and timeline views for managing entities (tasks, projects, knowledge, etc.)
(function () {
  const { useState: useStateE, useEffect: useEffectE, useMemo: useMemoE } = React;

  // ── 工具函数 ──────────────────────────────────────────────────────

  function formatDate(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleDateString();
    } catch (e) { return iso; }
  }

  function formatDateFull(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch (e) { return iso; }
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleString();
    } catch (e) { return iso; }
  }

  function daysUntil(iso) {
    if (!iso) return null;
    try {
      var now = new Date();
      var d = new Date(iso);
      return Math.ceil((d - now) / (1000 * 60 * 60 * 24));
    } catch (e) { return null; }
  }

  function sortByDate(arr, field) {
    return (arr || []).slice().sort(function (a, b) {
      var da = a[field] || "", db = b[field] || "";
      if (!da && !db) return 0;
      if (!da) return 1;
      if (!db) return -1;
      return da < db ? -1 : da > db ? 1 : 0;
    });
  }

  // ── API 函数 ──────────────────────────────────────────────────────

  async function fetchEntities(params) {
    var qs = new URLSearchParams(params || {}).toString();
    var url = "/api/entities" + (qs ? "?" + qs : "");
    var r = await fetch(url);
    return r.ok ? r.json() : [];
  }

  async function deleteEntity(id, permanent) {
    var url = "/api/entities/" + id + (permanent ? "?permanent=true" : "");
    await fetch(url, { method: "DELETE" });
  }

  async function updateEntity(id, body) {
    await fetch("/api/entities/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async function fetchSingleEntity(id) {
    var r = await fetch("/api/entities/" + id);
    return r.ok ? r.json() : null;
  }

  // ── 状态颜色映射 ─────────────────────────────────────────────────

  var STATUS_COLOR = {
    active: "var(--accent)",
    paused: "var(--warn)",
    done: "var(--text-3)",
    archived: "var(--text-3)",
    abandoned: "var(--err)",
  };

  var TYPE_ICON = {
    task: "◎",
    project: "◈",
    decision: "→",
    knowledge: "✽",
    relationship: "♢",
    event: "⌚",
    resource: "▤",
    idea: "☆",
    problem: "⚠",
    habit: "↻",
  };

  var PRIORITY_LABEL = {
    high: "↑↑",
    medium: "↑",
    low: "·",
  };

  // ── 实体详情弹窗 ─────────────────────────────────────────────────

  function EntityModal(props) {
    var entity = props.entity;
    var onClose = props.onClose;
    var onStatusChange = props.onStatusChange;
    var t = props.t;

    if (!entity) return null;

    return React.createElement("div", { className: "entity-modal-overlay", onClick: onClose },
      React.createElement("div", { className: "entity-modal", onClick: function (e) { e.stopPropagation(); } },
        React.createElement("button", { className: "entity-modal-close", onClick: onClose }, "×"),
        React.createElement("div", { className: "entity-modal-header" },
          React.createElement("span", { className: "entity-type-badge entity-type-badge-lg" },
            (TYPE_ICON[entity.type] || "⊙") + " " + (t("entityType." + entity.type) || entity.type)
          ),
          React.createElement("h3", { className: "entity-modal-title" }, entity.title),
        ),
        entity.content && React.createElement("div", { className: "entity-modal-section" },
          React.createElement("div", { className: "entity-modal-label" }, t("entities.content")),
          React.createElement("div", { className: "entity-modal-content" }, entity.content),
        ),
        React.createElement("div", { className: "entity-modal-grid" },
          React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.status")),
            React.createElement("select", {
              value: entity.status,
              onChange: function (e) { onStatusChange(entity.id, e.target.value); },
              className: "entity-modal-select",
            },
              ["active", "paused", "done", "archived", "abandoned"].map(function (s) {
                return React.createElement("option", { key: s, value: s }, t("entityStatus." + s) || s);
              })
            ),
          ),
          React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.priority")),
            React.createElement("div", { className: "entity-modal-value" }, t("entityPriority." + entity.priority) || entity.priority),
          ),
          entity.due_date && React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.dueDate")),
            React.createElement("div", {
              className: "entity-modal-value" + (daysUntil(entity.due_date) <= 1 ? " due-soon" : ""),
            }, formatDateFull(entity.due_date)),
          ),
          entity.confidence !== undefined && React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.confidence")),
            React.createElement("div", { className: "entity-modal-value" }, Math.round(entity.confidence * 100) + "%"),
          ),
        ),
        entity.people && entity.people.length > 0 && React.createElement("div", { className: "entity-modal-section" },
          React.createElement("div", { className: "entity-modal-label" }, t("entities.people")),
          React.createElement("div", { className: "entity-people-list" },
            entity.people.map(function (p, i) {
              return React.createElement("span", { key: i, className: "entity-people-tag" }, p);
            })
          ),
        ),
        entity.tags && entity.tags.length > 0 && React.createElement("div", { className: "entity-modal-section" },
          React.createElement("div", { className: "entity-modal-label" }, t("entities.tags")),
          React.createElement("div", { className: "entity-card-tags" },
            entity.tags.map(function (tag, i) {
              return React.createElement("span", { key: i, className: "entity-tag" }, tag);
            })
          ),
        ),
        React.createElement("div", { className: "entity-modal-footer" },
          React.createElement("span", { className: "entity-meta" },
            t("entities.createdAt") + ": " + formatDateTime(entity.created_at)
          ),
          entity.source && React.createElement("span", { className: "entity-meta" },
            t("entities.source") + ": " + entity.source
          ),
        ),
      )
    );
  }

  // ── 单个事务卡片 ─────────────────────────────────────────────────

  function EntityCard(props) {
    var entity = props.entity;
    var onStatusChange = props.onStatusChange;
    var onDelete = props.onDelete;
    var onClick = props.onClick;
    var t = props.t;

    var typeLabel = t("entityType." + entity.type) || entity.type;
    var statusLabel = t("entityStatus." + entity.status) || entity.status;
    var days = entity.due_date ? daysUntil(entity.due_date) : null;

    return React.createElement("div", { className: "entity-card", onClick: onClick },
      React.createElement("div", { className: "entity-card-header" },
        React.createElement("span", { className: "entity-type-badge" },
          (TYPE_ICON[entity.type] || "⊙") + " " + typeLabel
        ),
        React.createElement("span", {
          className: "entity-status-dot",
          style: { color: STATUS_COLOR[entity.status] || "var(--text)" },
          title: statusLabel,
        }, "●"),
        React.createElement("span", { className: "entity-priority-indicator" },
          PRIORITY_LABEL[entity.priority] || ""
        ),
      ),
      React.createElement("div", { className: "entity-card-title" }, entity.title),
      entity.content && React.createElement("div", { className: "entity-card-content" }, entity.content),
      entity.due_date && React.createElement("div", {
        className: "entity-card-due" + (days !== null && days <= 1 ? " due-soon" : ""),
      }, t("entities.dueDate") + ": " + formatDate(entity.due_date)),
      entity.people && entity.people.length > 0 && React.createElement("div", { className: "entity-card-people" },
        entity.people.map(function (p, i) {
          return React.createElement("span", { key: i, className: "entity-people-tag" }, p);
        })
      ),
      entity.tags && entity.tags.length > 0 && React.createElement("div", { className: "entity-card-tags" },
        entity.tags.slice(0, 3).map(function (tag, i) {
          return React.createElement("span", { key: i, className: "entity-tag" }, tag);
        }),
        entity.tags.length > 3 && React.createElement("span", { className: "entity-tag entity-tag-more" }, "+" + (entity.tags.length - 3)),
      ),
      React.createElement("div", { className: "entity-card-actions" },
        React.createElement("select", {
          value: entity.status,
          onChange: function (e) { e.stopPropagation(); onStatusChange(entity.id, e.target.value); },
          className: "entity-status-select",
        },
          ["active", "paused", "done", "archived", "abandoned"].map(function (s) {
            return React.createElement("option", { key: s, value: s }, t("entityStatus." + s) || s);
          })
        ),
        React.createElement("button", {
          className: "entity-delete-btn",
          onClick: function (e) { e.stopPropagation(); onDelete(entity.id); },
          title: t("entities.archive"),
        }, "×"),
      )
    );
  }

  // ── 列表视图 ─────────────────────────────────────────────────────

  function ListView(props) {
    var entities = props.entities;
    var onStatusChange = props.onStatusChange;
    var onDelete = props.onDelete;
    var onEntityClick = props.onEntityClick;
    var t = props.t;

    if (!entities || entities.length === 0) {
      return React.createElement("div", { className: "entities-empty" }, t("entities.noItems"));
    }
    return React.createElement("div", { className: "entities-list" },
      entities.map(function (e) {
        return React.createElement(EntityCard, {
          key: e.id,
          entity: e,
          onStatusChange: onStatusChange,
          onDelete: onDelete,
          onClick: function () { onEntityClick(e); },
          t: t,
        });
      })
    );
  }

  // ── 看板视图 ─────────────────────────────────────────────────────

  function KanbanView(props) {
    var entities = props.entities;
    var onStatusChange = props.onStatusChange;
    var onDelete = props.onDelete;
    var onEntityClick = props.onEntityClick;
    var t = props.t;

    var columns = ["active", "paused", "done"];
    return React.createElement("div", { className: "entities-kanban" },
      columns.map(function (status) {
        var col = (entities || []).filter(function (e) { return e.status === status; });
        return React.createElement("div", { key: status, className: "kanban-column" },
          React.createElement("div", { className: "kanban-column-header" },
            React.createElement("span", { className: "kanban-column-title" },
              React.createElement("span", { className: "kanban-column-dot", style: { color: STATUS_COLOR[status] } }, "●"),
              " " + (t("entityStatus." + status) || status)
            ),
            React.createElement("span", { className: "kanban-column-count" }, col.length),
          ),
          col.map(function (e) {
            return React.createElement(EntityCard, {
              key: e.id,
              entity: e,
              onStatusChange: onStatusChange,
              onDelete: onDelete,
              onClick: function () { onEntityClick(e); },
              t: t,
            });
          })
        );
      })
    );
  }

  // ── 时间线视图 ───────────────────────────────────────────────────

  function TimelineView(props) {
    var entities = props.entities;
    var onStatusChange = props.onStatusChange;
    var onDelete = props.onDelete;
    var onEntityClick = props.onEntityClick;
    var t = props.t;

    var grouped = useMemoE(function () {
      var now = new Date();
      var groups = [];
      var pastDue = [];
      var noDate = [];

      (entities || []).forEach(function (e) {
        if (!e.due_date) {
          noDate.push(e);
          return;
        }
        try {
          var d = new Date(e.due_date);
          if (d < now) {
            pastDue.push(e);
            return;
          }
          var key = d.toISOString().slice(0, 7); // YYYY-MM
          var found = false;
          for (var i = 0; i < groups.length; i++) {
            if (groups[i].key === key) {
              groups[i].items.push(e);
              found = true;
              break;
            }
          }
          if (!found) {
            groups.push({ key: key, label: d.toLocaleDateString(undefined, { year: "numeric", month: "long" }), items: [e] });
          }
        } catch (ex) {
          noDate.push(e);
        }
      });

      // Sort groups chronologically
      groups.sort(function (a, b) { return a.key < b.key ? -1 : a.key > b.key ? 1 : 0; });

      var result = [];
      if (pastDue.length > 0) {
        result.push({ key: "_past", label: t("entities.timeline.pastDue"), items: sortByDate(pastDue, "due_date"), past: true });
      }
      result = result.concat(groups);
      if (noDate.length > 0) {
        result.push({ key: "_none", label: t("entities.timeline.noDate"), items: noDate, past: false });
      }
      return result;
    }, [entities, t]);

    if (!entities || entities.length === 0) {
      return React.createElement("div", { className: "entities-empty" }, t("entities.noItems"));
    }

    return React.createElement("div", { className: "entities-timeline" },
      grouped.map(function (group) {
        return React.createElement("div", {
          key: group.key,
          className: "timeline-group" + (group.past ? " past-due" : ""),
        },
          React.createElement("div", { className: "timeline-group-header" },
            React.createElement("span", { className: "timeline-marker" }),
            React.createElement("span", { className: "timeline-group-label" }, group.label),
            React.createElement("span", { className: "timeline-group-count" }, group.items.length),
          ),
          React.createElement("div", { className: "timeline-items" },
            group.items.map(function (e) {
              return React.createElement(EntityCard, {
                key: e.id,
                entity: e,
                onStatusChange: onStatusChange,
                onDelete: onDelete,
                onClick: function () { onEntityClick(e); },
                t: t,
              });
            })
          )
        );
      })
    );
  }

  // ── 统计概览条 ───────────────────────────────────────────────────

  function EntitySummaryBar(props) {
    var entities = props.entities;
    var t = props.t;

    var counts = useMemoE(function () {
      var total = (entities || []).length;
      var byType = {};
      var active = 0;
      (entities || []).forEach(function (e) {
        if (e.status === "active") active++;
        byType[e.type] = (byType[e.type] || 0) + 1;
      });
      var topType = Object.keys(byType).sort(function (a, b) { return byType[b] - byType[a]; })[0] || "";
      return { total: total, active: active, topType: topType };
    }, [entities]);

    if (!entities || entities.length === 0) return null;

    return React.createElement("div", { className: "entity-summary-bar" },
      React.createElement("span", { className: "entity-summary-item" },
        React.createElement("strong", null, counts.total), " ", t("entities.total")
      ),
      React.createElement("span", { className: "entity-summary-sep" }),
      React.createElement("span", { className: "entity-summary-item" },
        React.createElement("strong", null, counts.active), " ", t("entities.active")
      ),
      counts.topType && React.createElement(React.Fragment, null,
        React.createElement("span", { className: "entity-summary-sep" }),
        React.createElement("span", { className: "entity-summary-item" },
          t("entities.mostType") + ": ", React.createElement("strong", null,
            t("entityType." + counts.topType) || counts.topType
          )
        ),
      )
    );
  }

  // ── 主页面组件 ───────────────────────────────────────────────────

  function EntitiesPage() {
    useDataVersion();
    var ti18n = useI18n();
    var t = ti18n.t;
    var entities = useStateE(DATA.entities || []);
    var setEntities = entities[1];
    var entitiesVal = entities[0];
    var view = useStateE("list");
    var setView = view[1];
    var viewVal = view[0];
    var typeFilter = useStateE("");
    var setTypeFilter = typeFilter[1];
    var typeFilterVal = typeFilter[0];
    var statusFilter = useStateE("active");
    var setStatusFilter = statusFilter[1];
    var statusFilterVal = statusFilter[0];
    var searchQ = useStateE("");
    var setSearchQ = searchQ[1];
    var searchQVal = searchQ[0];
    var loading = useStateE(false);
    var setLoading = loading[1];
    var loadingVal = loading[0];
    var selectedEntity = useStateE(null);
    var setSelectedEntity = selectedEntity[1];
    var selectedEntityVal = selectedEntity[0];

    function reload() {
      setLoading(true);
      try {
        var params = {};
        if (typeFilterVal) params.type = typeFilterVal;
        if (statusFilterVal) params.status = statusFilterVal;
        if (searchQVal) params.q = searchQVal;
        var promise = fetchEntities(params);
        promise.then(function (data) {
          setEntities(data);
          setLoading(false);
        }).catch(function (err) {
          console.error(err);
          setLoading(false);
        });
      } catch (e) {
        console.error(e);
        setLoading(false);
      }
    }

    useEffectE(function () { reload(); }, [typeFilterVal, statusFilterVal, searchQVal]);

    function handleStatusChange(id, newStatus) {
      var promise = updateEntity(id, { status: newStatus });
      promise.then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    function handleDelete(id) {
      var promise = deleteEntity(id, false);
      promise.then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    function handleEntityClick(entity) {
      setSelectedEntity(entity);
    }

    var types = ["task", "project", "decision", "knowledge", "relationship", "event", "resource", "idea", "problem", "habit"];
    var statuses = ["", "active", "paused", "done", "archived"];

    return React.createElement("div", { className: "page entities-page" },
      // Header
      React.createElement("div", { className: "entities-header" },
        React.createElement("div", null,
          React.createElement("h2", { className: "entities-title" }, t("entities.title")),
          React.createElement(EntitySummaryBar, { entities: entitiesVal, t: t }),
        ),
        React.createElement("div", { className: "entities-view-tabs" },
          React.createElement("button", {
            className: "view-tab" + (viewVal === "list" ? " active" : ""),
            onClick: function () { setView("list"); },
          }, t("entities.viewList")),
          React.createElement("button", {
            className: "view-tab" + (viewVal === "kanban" ? " active" : ""),
            onClick: function () { setView("kanban"); },
          }, t("entities.viewKanban")),
          React.createElement("button", {
            className: "view-tab" + (viewVal === "timeline" ? " active" : ""),
            onClick: function () { setView("timeline"); },
          }, t("entities.viewTimeline")),
        ),
      ),
      // Filters
      React.createElement("div", { className: "entities-filters" },
        React.createElement("input", {
          className: "entities-search",
          type: "text",
          placeholder: t("entities.search"),
          value: searchQVal,
          onChange: function (e) { setSearchQ(e.target.value); },
        }),
        React.createElement("select", {
          className: "entities-filter-select",
          value: typeFilterVal,
          onChange: function (e) { setTypeFilter(e.target.value); },
        },
          React.createElement("option", { value: "" }, t("entities.type") + ": " + t("entities.filterAll")),
          types.map(function (tp) {
            return React.createElement("option", { key: tp, value: tp }, t("entityType." + tp) || tp);
          })
        ),
        React.createElement("select", {
          className: "entities-filter-select",
          value: statusFilterVal,
          onChange: function (e) { setStatusFilter(e.target.value); },
        },
          statuses.map(function (s) {
            return React.createElement("option", { key: s, value: s },
              s ? (t("entityStatus." + s) || s) : t("entities.filterAll")
            );
          })
        ),
      ),
      // Content
      loadingVal
        ? React.createElement("div", { className: "entities-loading" }, "…")
        : (viewVal === "kanban"
          ? React.createElement(KanbanView, { entities: entitiesVal, onStatusChange: handleStatusChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
          : viewVal === "timeline"
            ? React.createElement(TimelineView, { entities: entitiesVal, onStatusChange: handleStatusChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
            : React.createElement(ListView, { entities: entitiesVal, onStatusChange: handleStatusChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
        ),
      // Entity detail modal
      selectedEntityVal && React.createElement(EntityModal, {
        entity: selectedEntityVal,
        onClose: function () { setSelectedEntity(null); },
        onStatusChange: handleStatusChange,
        t: t,
      }),
    );
  }

  window.EntitiesPage = EntitiesPage;
})();
