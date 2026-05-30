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

  function toDateInputValue(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toISOString().slice(0, 10);
    } catch (e) { return ""; }
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

  // ── 示例数据 ──────────────────────────────────────────────────────

  var SAMPLE_ENTITIES = [
    { id: "sample_1", type: "project", title: "Cyrene v2.0 重构计划", content: "架构升级、性能优化和新功能开发的整体规划", status: "active", priority: "high", tags: ["架构", "计划"], people: ["Luciano"], due_date: new Date(Date.now() + 86400000 * 30).toISOString(), created_at: new Date(Date.now() - 86400000 * 7).toISOString(), confidence: 0.92, source: "user" },
    { id: "sample_2", type: "task", title: "完成数据迁移脚本", content: "将旧版数据迁移到新的向量存储格式", status: "active", priority: "high", tags: ["后端", "数据"], people: ["Luciano"], due_date: new Date(Date.now() + 86400000 * 5).toISOString(), created_at: new Date(Date.now() - 86400000 * 3).toISOString(), confidence: 0.85, source: "agent" },
    { id: "sample_3", type: "knowledge", title: "FastAPI 路由注册最佳实践", content: "使用 APIRouter 组织路由，避免循环导入", status: "active", priority: "medium", tags: ["Python", "后端"], created_at: new Date(Date.now() - 86400000 * 14).toISOString(), confidence: 0.98, source: "user" },
    { id: "sample_4", type: "task", title: "优化前端打包体积", content: "分析 webpack bundle，移除未使用的依赖", status: "paused", priority: "medium", tags: ["前端", "性能"], people: ["Luciano"], due_date: new Date(Date.now() + 86400000 * 14).toISOString(), created_at: new Date(Date.now() - 86400000 * 5).toISOString(), confidence: 0.72, source: "user" },
    { id: "sample_5", type: "decision", title: "采用 TailwindCSS 作为样式方案", content: "统一 UI 开发体验，减少自定义 CSS 维护成本", status: "done", priority: "medium", tags: ["前端", "架构"], people: ["Luciano"], created_at: new Date(Date.now() - 86400000 * 30).toISOString(), confidence: 0.95, source: "user" },
    { id: "sample_6", type: "habit", title: "每日代码审查", content: "每天花 15 分钟审查团队 PR", status: "pending", priority: "low", tags: ["习惯", "团队"], due_date: new Date(Date.now() + 86400000 * 60).toISOString(), created_at: new Date(Date.now() - 86400000 * 2).toISOString(), confidence: 0.65, source: "agent" },
    { id: "sample_7", type: "idea", title: "AI 驱动的代码片段推荐", content: "根据当前编辑上下文自动推荐相关代码片段", status: "pending", priority: "low", tags: ["AI", "功能"], created_at: new Date(Date.now() - 86400000 * 10).toISOString(), confidence: 0.45, source: "user" },
    { id: "sample_8", type: "event", title: "团队技术分享会", content: "每月一次的内部技术交流", status: "active", priority: "medium", tags: ["团队", "活动"], due_date: new Date(Date.now() + 86400000 * 20).toISOString(), created_at: new Date(Date.now() - 86400000 * 15).toISOString(), confidence: 0.88, source: "user" },
    { id: "sample_9", type: "problem", title: "生产环境内存泄漏排查", content: "长时间运行后内存占用持续增长，需定位泄漏源", status: "active", priority: "high", tags: ["后端", "运维"], people: ["Luciano"], due_date: new Date(Date.now() + 86400000 * 3).toISOString(), created_at: new Date(Date.now() - 86400000 * 1).toISOString(), confidence: 0.78, source: "agent" },
    { id: "sample_10", type: "resource", title: "React 18 迁移指南", content: "官方迁移文档及注意事项汇总", status: "done", priority: "low", tags: ["前端", "文档"], created_at: new Date(Date.now() - 86400000 * 20).toISOString(), confidence: 0.96, source: "user" },
    { id: "sample_11", type: "relationship", title: "与第三方 API 合作方对接", content: "处理 API 密钥和接口调用频率限制", status: "pending", priority: "medium", tags: ["对接", "API"], people: ["Luciano"], due_date: new Date(Date.now() + 86400000 * 10).toISOString(), created_at: new Date(Date.now() - 86400000 * 4).toISOString(), confidence: 0.82, source: "user" },
    { id: "sample_12", type: "task", title: "编写单元测试覆盖核心模块", content: "核心模块测试覆盖率提升至 80%", status: "active", priority: "high", tags: ["测试", "质量"], due_date: new Date(Date.now() + 86400000 * 7).toISOString(), created_at: new Date(Date.now() - 86400000 * 2).toISOString(), confidence: 0.7, source: "user" },
  ];

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

  // ── 常量和映射 ────────────────────────────────────────────────────

  var STATUS_COLOR = {
    pending: "var(--text-3)",
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

  var ALL_STATUSES = ["pending", "active", "paused", "done", "archived", "abandoned"];

  // ── 创建事务弹窗 ─────────────────────────────────────────────────

  function EntityCreateForm(props) {
    var onClose = props.onClose;
    var onCreate = props.onCreate;
    var t = props.t;

    var title = useStateE("");
    var setTitle = title[1];
    var titleVal = title[0];
    var type = useStateE("task");
    var setType = type[1];
    var typeVal = type[0];
    var content = useStateE("");
    var setContent = content[1];
    var contentVal = content[0];
    var priority = useStateE("medium");
    var setPriority = priority[1];
    var priorityVal = priority[0];
    var status = useStateE("active");
    var setStatus = status[1];
    var statusVal = status[0];
    var dueDate = useStateE("");
    var setDueDate = dueDate[1];
    var dueDateVal = dueDate[0];
    var tags = useStateE("");
    var setTags = tags[1];
    var tagsVal = tags[0];
    var people = useStateE("");
    var setPeople = people[1];
    var peopleVal = people[0];
    var busy = useStateE(false);
    var setBusy = busy[1];
    var busyVal = busy[0];

    function handleSubmit(e) {
      e.preventDefault();
      if (!titleVal.trim()) return;
      setBusy(true);
      var body = {
        type: typeVal,
        title: titleVal,
        content: contentVal || undefined,
        priority: priorityVal,
        status: statusVal,
        source: "user",
      };
      if (dueDateVal) {
        body.due_date = new Date(dueDateVal + "T23:59:59").toISOString();
      }
      if (tagsVal.trim()) {
        body.tags = tagsVal.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      }
      if (peopleVal.trim()) {
        body.people = peopleVal.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      }
      onCreate(body);
      setBusy(false);
    }

    var types = ["task", "project", "decision", "knowledge", "relationship", "event", "resource", "idea", "problem", "habit"];

    return React.createElement("div", { className: "entity-modal-overlay", onClick: onClose },
      React.createElement("div", { className: "entity-modal entity-create-modal", onClick: function (e) { e.stopPropagation(); } },
        React.createElement("button", { className: "entity-modal-close", onClick: onClose }, "×"),
        React.createElement("div", { className: "entity-modal-header" },
          React.createElement("h3", { className: "entity-modal-title" }, t("entities.newEntity")),
        ),
        React.createElement("form", { onSubmit: handleSubmit, className: "entity-create-form" },
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.type")),
            React.createElement("select", {
              value: typeVal,
              onChange: function (e) { setType(e.target.value); },
              className: "entity-create-input entity-create-select",
            },
              types.map(function (tp) {
                return React.createElement("option", { key: tp, value: tp },
                  (TYPE_ICON[tp] || "⊙") + " " + (t("entityType." + tp) || tp)
                );
              })
            ),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.status")),
            React.createElement("select", {
              value: statusVal,
              onChange: function (e) { setStatus(e.target.value); },
              className: "entity-create-input entity-create-select",
            },
              ALL_STATUSES.map(function (s) {
                return React.createElement("option", { key: s, value: s }, t("entityStatus." + s) || s);
              })
            ),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.priority")),
            React.createElement("select", {
              value: priorityVal,
              onChange: function (e) { setPriority(e.target.value); },
              className: "entity-create-input entity-create-select",
            },
              ["high", "medium", "low"].map(function (p) {
                return React.createElement("option", { key: p, value: p }, t("entityPriority." + p) || p);
              })
            ),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.title")),
            React.createElement("input", {
              type: "text",
              value: titleVal,
              onChange: function (e) { setTitle(e.target.value); },
              className: "entity-create-input",
              placeholder: t("entities.title"),
              autoFocus: true,
            }),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.content")),
            React.createElement("textarea", {
              value: contentVal,
              onChange: function (e) { setContent(e.target.value); },
              className: "entity-create-input entity-create-textarea",
              rows: 3,
              placeholder: t("entities.content"),
            }),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.dueDate")),
            React.createElement("input", {
              type: "date",
              value: dueDateVal,
              onChange: function (e) { setDueDate(e.target.value); },
              className: "entity-create-input",
            }),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.tags")),
            React.createElement("input", {
              type: "text",
              value: tagsVal,
              onChange: function (e) { setTags(e.target.value); },
              className: "entity-create-input",
              placeholder: "tag1, tag2, tag3",
            }),
          ),
          React.createElement("div", { className: "entity-create-row" },
            React.createElement("label", { className: "entity-create-label" }, t("entities.people")),
            React.createElement("input", {
              type: "text",
              value: peopleVal,
              onChange: function (e) { setPeople(e.target.value); },
              className: "entity-create-input",
              placeholder: "name1, name2",
            }),
          ),
          React.createElement("div", { className: "entity-create-actions" },
            React.createElement("button", {
              type: "button",
              className: "btn",
              onClick: onClose,
            }, t("entities.cancel")),
            React.createElement("button", {
              type: "submit",
              className: "btn primary",
              disabled: busyVal || !titleVal.trim(),
            }, t("entities.save")),
          ),
        ),
      )
    );
  }

  // ── 实体详情弹窗 ─────────────────────────────────────────────────

  function EntityModal(props) {
    var entity = props.entity;
    var onClose = props.onClose;
    var onStatusChange = props.onStatusChange;
    var onFieldChange = props.onFieldChange;
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
              ALL_STATUSES.map(function (s) {
                return React.createElement("option", { key: s, value: s }, t("entityStatus." + s) || s);
              })
            ),
          ),
          React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.priority")),
            React.createElement("select", {
              value: entity.priority || "medium",
              onChange: function (e) { onFieldChange(entity.id, "priority", e.target.value); },
              className: "entity-modal-select",
            },
              ["high", "medium", "low"].map(function (p) {
                return React.createElement("option", { key: p, value: p }, t("entityPriority." + p) || p);
              })
            ),
          ),
          React.createElement("div", { className: "entity-modal-field" },
            React.createElement("div", { className: "entity-modal-label" }, t("entities.dueDate")),
            React.createElement("input", {
              type: "date",
              value: toDateInputValue(entity.due_date),
              onChange: function (e) {
                var val = e.target.value;
                onFieldChange(entity.id, "due_date", val ? new Date(val + "T23:59:59").toISOString() : null);
              },
              className: "entity-modal-select",
              style: { color: daysUntil(entity.due_date) <= 1 ? "var(--err)" : "var(--text)" },
            }),
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
    var onFieldChange = props.onFieldChange;
    var onDelete = props.onDelete;
    var onClick = props.onClick;
    var onDragStart = props.onDragStart;
    var isDragging = props.isDragging;
    var t = props.t;

    var typeLabel = t("entityType." + entity.type) || entity.type;
    var statusLabel = t("entityStatus." + entity.status) || entity.status;
    var days = entity.due_date ? daysUntil(entity.due_date) : null;

    return React.createElement("div", {
      className: "entity-card" + (isDragging ? " entity-card-dragging" : ""),
      draggable: true,
      onClick: onClick,
      onDragStart: function (e) {
        e.dataTransfer.setData("text/plain", entity.id);
        e.dataTransfer.effectAllowed = "move";
        if (onDragStart) onDragStart(entity.id);
      },
      onDragEnd: function () {
        if (onDragStart) onDragStart(null);
      },
    },
      React.createElement("div", { className: "entity-card-header" },
        React.createElement("span", { className: "entity-type-badge" },
          (TYPE_ICON[entity.type] || "⊙") + " " + typeLabel
        ),
        React.createElement("span", {
          className: "entity-status-dot",
          style: { color: STATUS_COLOR[entity.status] || "var(--text)" },
          title: statusLabel,
        }, "●"),
        React.createElement("div", { className: "entity-priority-wrap", onClick: function (e) { e.stopPropagation(); } },
          React.createElement("select", {
            className: "entity-priority-select",
            value: entity.priority || "medium",
            onChange: function (e) { onFieldChange(entity.id, "priority", e.target.value); },
            title: t("entities.priority"),
          },
            ["high", "medium", "low"].map(function (p) {
              return React.createElement("option", { key: p, value: p },
                PRIORITY_LABEL[p] + " " + (t("entityPriority." + p) || p)
              );
            })
          ),
        ),
      ),
      React.createElement("div", { className: "entity-card-title" }, entity.title),
      entity.content && React.createElement("div", { className: "entity-card-content" }, entity.content),
      React.createElement("div", { className: "entity-card-footer" },
        React.createElement("div", { className: "entity-card-due-wrap", onClick: function (e) { e.stopPropagation(); } },
          React.createElement("input", {
            type: "date",
            className: "entity-card-due-input" + (days !== null && days <= 1 ? " due-soon" : ""),
            value: toDateInputValue(entity.due_date),
            onChange: function (e) {
              var val = e.target.value;
              onFieldChange(entity.id, "due_date", val ? new Date(val + "T23:59:59").toISOString() : null);
            },
            title: t("entities.dueDate"),
          }),
        ),
        entity.people && entity.people.length > 0 && React.createElement("div", { className: "entity-card-people" },
          entity.people.slice(0, 2).map(function (p, i) {
            return React.createElement("span", { key: i, className: "entity-people-tag" }, p);
          }),
          entity.people.length > 2 && React.createElement("span", { className: "entity-people-tag" }, "+" + (entity.people.length - 2)),
        ),
        entity.tags && entity.tags.length > 0 && React.createElement("div", { className: "entity-card-tags" },
          entity.tags.slice(0, 2).map(function (tag, i) {
            return React.createElement("span", { key: i, className: "entity-tag" }, tag);
          }),
          entity.tags.length > 2 && React.createElement("span", { className: "entity-tag entity-tag-more" }, "+" + (entity.tags.length - 2)),
        ),
      ),
      React.createElement("div", { className: "entity-card-actions" },
        React.createElement("select", {
          value: entity.status,
          onChange: function (e) { e.stopPropagation(); onStatusChange(entity.id, e.target.value); },
          className: "entity-status-select",
        },
          ALL_STATUSES.map(function (s) {
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
    var onFieldChange = props.onFieldChange;
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
          onFieldChange: onFieldChange,
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
    var onFieldChange = props.onFieldChange;
    var onDelete = props.onDelete;
    var onEntityClick = props.onEntityClick;
    var t = props.t;

    var draggedId = useStateE(null);
    var setDraggedId = draggedId[1];
    var draggedIdVal = draggedId[0];
    var dropTarget = useStateE(null);
    var setDropTarget = dropTarget[1];
    var dropTargetVal = dropTarget[0];

    var columns = ["pending", "active", "paused", "done"];

    function handleDrop(status) {
      if (!draggedIdVal) return;
      onStatusChange(draggedIdVal, status);
      setDraggedId(null);
      setDropTarget(null);
    }

    return React.createElement("div", { className: "entities-kanban" },
      columns.map(function (status) {
        var col = (entities || []).filter(function (e) { return e.status === status; });
        var isOver = dropTargetVal === status;
        return React.createElement("div", { key: status, className: "kanban-column" + (isOver ? " kanban-column-drop" : ""),
            onDragOver: function (e) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; },
            onDragEnter: function (e) { e.preventDefault(); setDropTarget(status); },
            onDragLeave: function (e) {
              var rect = e.currentTarget.getBoundingClientRect();
              var x = e.clientX, y = e.clientY;
              if (x <= rect.left || x >= rect.right || y <= rect.top || y >= rect.bottom) {
                setDropTarget(null);
              }
            },
            onDrop: function (e) { e.preventDefault(); handleDrop(status); },
          },
          React.createElement("div", { className: "kanban-column-header" },
            React.createElement("span", { className: "kanban-column-title" },
              React.createElement("span", { className: "kanban-column-dot", style: { color: STATUS_COLOR[status] } }, "●"),
              " " + (t("entityStatus." + status) || status)
            ),
            React.createElement("span", { className: "kanban-column-count" }, col.length),
          ),
          React.createElement("div", { className: "kanban-column-body" },
            col.map(function (e) {
              return React.createElement(EntityCard, {
                key: e.id,
                entity: e,
                onStatusChange: onStatusChange,
                onFieldChange: onFieldChange,
                onDelete: onDelete,
                onClick: function () { onEntityClick(e); },
                onDragStart: setDraggedId,
                isDragging: draggedIdVal === e.id,
                t: t,
              });
            })
          )
        );
      })
    );
  }

  // ── 时间线视图 ───────────────────────────────────────────────────

  function TimelineView(props) {
    var entities = props.entities;
    var onStatusChange = props.onStatusChange;
    var onFieldChange = props.onFieldChange;
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
          var key = d.toISOString().slice(0, 7);
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
                onFieldChange: onFieldChange,
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
    var entities = useStateE([]);
    var setEntities = entities[1];
    var entitiesVal = entities[0];
    var view = useStateE("list");
    var setView = view[1];
    var viewVal = view[0];
    var typeFilter = useStateE("");
    var setTypeFilter = typeFilter[1];
    var typeFilterVal = typeFilter[0];
    var statusFilter = useStateE("");
    var setStatusFilter = statusFilter[1];
    var statusFilterVal = statusFilter[0];
    var searchQ = useStateE("");
    var setSearchQ = searchQ[1];
    var searchQVal = searchQ[0];
    var loading = useStateE(true);
    var setLoading = loading[1];
    var loadingVal = loading[0];
    var selectedEntity = useStateE(null);
    var setSelectedEntity = selectedEntity[1];
    var selectedEntityVal = selectedEntity[0];
    var showForm = useStateE(false);
    var setShowForm = showForm[1];
    var showFormVal = showForm[0];
    var useSample = useStateE(false);
    var setUseSample = useSample[1];
    var useSampleVal = useSample[0];
    function reload() {
      setLoading(true);
      var params = {};
      if (typeFilterVal) params.type = typeFilterVal;
      if (statusFilterVal) params.status = statusFilterVal;
      if (searchQVal) params.q = searchQVal;
      fetchEntities(params).then(function (data) {
        if (data && data.length > 0) {
          setEntities(data);
          setUseSample(false);
        } else {
          setEntities(SAMPLE_ENTITIES);
          setUseSample(true);
        }
        setLoading(false);
      }).catch(function (err) {
        console.error(err);
        setEntities(SAMPLE_ENTITIES);
        setUseSample(true);
        setLoading(false);
      });
    }

    useEffectE(function () {
      reload();
    }, [typeFilterVal, statusFilterVal, searchQVal]);

    function handleStatusChange(id, newStatus) {
      if (useSampleVal) {
        setEntities(function (prev) {
          return prev.map(function (e) {
            return e.id === id ? Object.assign({}, e, { status: newStatus }) : e;
          });
        });
        return;
      }
      var promise = updateEntity(id, { status: newStatus });
      promise.then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    function handleFieldChange(id, field, value) {
      if (useSampleVal) {
        setEntities(function (prev) {
          return prev.map(function (e) {
            if (e.id !== id) return e;
            var updated = Object.assign({}, e);
            updated[field] = value;
            return updated;
          });
        });
        return;
      }
      var body = {};
      body[field] = value;
      var promise = updateEntity(id, body);
      promise.then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    function handleDelete(id) {
      if (useSampleVal) {
        setEntities(function (prev) {
          return prev.filter(function (e) { return e.id !== id; });
        });
        return;
      }
      var promise = deleteEntity(id, false);
      promise.then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    function handleEntityClick(entity) {
      setSelectedEntity(entity);
    }

    function handleCreate(body) {
      setShowForm(false);
      if (useSampleVal) {
        var newEntity = Object.assign({
          id: "sample_new_" + Date.now(),
          created_at: new Date().toISOString(),
          tags: body.tags || [],
          people: body.people || [],
          confidence: 1.0,
          source: "user",
        }, body);
        setEntities(function (prev) { return [newEntity].concat(prev); });
        return;
      }
      fetch("/api/entities", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function () { reload(); }).catch(function (e) { console.error(e); });
    }

    var types = ["task", "project", "decision", "knowledge", "relationship", "event", "resource", "idea", "problem", "habit"];
    var statuses = ["", "pending", "active", "paused", "done", "archived"];

    var displayEntities = entitiesVal;

    return React.createElement("div", { className: "page entities-page" },
      React.createElement("div", { className: "entities-header" },
        React.createElement("div", { className: "entities-header-left" },
          React.createElement("h2", { className: "entities-title" }, t("entities.title")),
          React.createElement(EntitySummaryBar, { entities: displayEntities, t: t }),
        ),
        React.createElement("div", { className: "entities-header-right" },
          React.createElement("button", {
            className: "btn primary entities-add-btn",
            onClick: function () { setShowForm(true); },
          }, "+ " + t("entities.newEntity")),
          React.createElement("div", { className: "seg entities-view-tabs" },
            React.createElement("button", {
              className: "seg-btn" + (viewVal === "list" ? " active" : ""),
              onClick: function () { setView("list"); },
            }, t("entities.viewList")),
            React.createElement("button", {
              className: "seg-btn" + (viewVal === "kanban" ? " active" : ""),
              onClick: function () { setView("kanban"); },
            }, t("entities.viewKanban")),
            React.createElement("button", {
              className: "seg-btn" + (viewVal === "timeline" ? " active" : ""),
              onClick: function () { setView("timeline"); },
            }, t("entities.viewTimeline")),
          ),
        ),
      ),
      React.createElement("div", { className: "entities-filters" },
        React.createElement("div", { className: "entities-search-wrap" },
          React.createElement("span", { className: "entities-search-icon" }, "⌕"),
          React.createElement("input", {
            className: "entities-search",
            type: "text",
            placeholder: t("entities.search"),
            value: searchQVal,
            onChange: function (e) { setSearchQ(e.target.value); },
          }),
        ),
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
      React.createElement("div", { className: "entities-scroll" },
        useSampleVal && React.createElement("div", { className: "entities-sample-note" },
          React.createElement("span", null, "ℹ " + (t("entities.showingSamples") || "Showing sample data — connect backend for real entities")),
        ),
        loadingVal
          ? React.createElement("div", { className: "entities-loading" }, "…")
          : (viewVal === "kanban"
            ? React.createElement(KanbanView, { entities: displayEntities, onStatusChange: handleStatusChange, onFieldChange: handleFieldChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
            : viewVal === "timeline"
              ? React.createElement(TimelineView, { entities: displayEntities, onStatusChange: handleStatusChange, onFieldChange: handleFieldChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
              : React.createElement(ListView, { entities: displayEntities, onStatusChange: handleStatusChange, onFieldChange: handleFieldChange, onDelete: handleDelete, onEntityClick: handleEntityClick, t: t })
          ),
      ),
      selectedEntityVal && React.createElement(EntityModal, {
        entity: selectedEntityVal,
        onClose: function () { setSelectedEntity(null); },
        onStatusChange: handleStatusChange,
        onFieldChange: handleFieldChange,
        t: t,
      }),
      showFormVal && React.createElement(EntityCreateForm, {
        onClose: function () { setShowForm(false); },
        onCreate: handleCreate,
        t: t,
      }),
    );
  }

  window.EntitiesPage = EntitiesPage;
})();
