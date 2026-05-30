// Plans page — list, create, edit, delete
const { useState: useStateT, useEffect: useEffectT, useRef: useRefT } = React;

function ScheduledTasksPage() {
  useDataVersion();
  const { t } = useI18n();
  const [tasks, setTasks] = useStateT([]);
  const [entities, setEntities] = useStateT(DATA.entities || []);
  const [loading, setLoading] = useStateT(true);
  const [error, setError] = useStateT(null);
  const [editingId, setEditingId] = useStateT(null);
  const [showForm, setShowForm] = useStateT(false);
  const [viewMode, setViewMode] = useStateT("list");

  async function loadTasks() {
    try {
      setLoading(true);
      const r = await fetch("/api/tasks");
      const data = await r.json();
      setTasks(data.tasks || []);
      // 同时加载有截止日期的事务
      const re = await fetch("/api/entities?has_due_date=true");
      if (re.ok) {
        const entityData = await re.json();
        setEntities(entityData);
      }
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffectT(() => { loadTasks(); }, []);

  async function handleDelete(id) {
    if (!confirm(t("tasks.confirmDelete"))) return;
    try {
      const r = await fetch("/api/tasks/" + id, { method: "DELETE" });
      const data = await r.json();
      setTasks(data.tasks || []);
    } catch (e) {
      alert(e.message);
    }
  }

  async function handleToggleStatus(task) {
    const newStatus = task.status === "active" ? "paused" : "active";
    try {
      const r = await fetch("/api/tasks/" + task.id, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      const data = await r.json();
      setTasks(data.tasks || []);
    } catch (e) {
      alert(e.message);
    }
  }

  return (
    <div className="page tasks-page">
      <div className="page-head">
        <h2>{t("tasks.title")}</h2>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <div className="tasks-view-toggle">
            <button className={"tasks-view-btn" + (viewMode === "list" ? " active" : "")} onClick={() => setViewMode("list")}>{t("tasks.viewList")}</button>
            <button className={"tasks-view-btn" + (viewMode === "calendar" ? " active" : "")} onClick={() => setViewMode("calendar")}>{t("tasks.viewCalendar")}</button>
          </div>
          <button className="iconbtn tasks-add-btn" onClick={() => { setEditingId(null); setShowForm(true); }}>
            + {t("tasks.newTask")}
          </button>
        </div>
      </div>

      {showForm && (
        <TaskForm
          task={editingId ? tasks.find(t => t.id === editingId) : null}
          onSave={async (data) => {
            try {
              const method = editingId ? "PUT" : "POST";
              const url = editingId ? "/api/tasks/" + editingId : "/api/tasks";
              const r = await fetch(url, {
                method,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(data),
              });
              const result = await r.json();
              setTasks(result.tasks || []);
              setShowForm(false);
              setEditingId(null);
            } catch (e) {
              alert(e.message);
            }
          }}
          onCancel={() => { setShowForm(false); setEditingId(null); }}
        />
      )}

      {error && <div className="tasks-error">{t("tasks.loadError")}: {error}</div>}

      {loading ? (
        <div className="tasks-loading">{t("tasks.loading")}</div>
      ) : viewMode === "calendar" ? React.createElement(window.TaskCalendarView, {
        tasks: tasks,
        entities: entities,
        onEdit: function(task) { setEditingId(task.id); setShowForm(true); },
        onToggle: handleToggleStatus,
        onDelete: handleDelete,
      }) : tasks.length === 0 ? (
        <div className="tasks-empty">{t("tasks.empty")}</div>
      ) : (
        <div className="tasks-list">
          <div className="tasks-list-header">
            <span className="tasks-col-status">{t("tasks.status")}</span>
            <span className="tasks-col-prompt">{t("tasks.prompt")}</span>
            <span className="tasks-col-schedule">{t("tasks.schedule")}</span>
            <span className="tasks-col-next">{t("tasks.nextRun")}</span>
            <span className="tasks-col-actions">{t("tasks.actions")}</span>
          </div>
          {tasks.map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              onDelete={() => handleDelete(task.id)}
              onToggle={() => handleToggleStatus(task)}
              onEdit={() => { setEditingId(task.id); setShowForm(true); }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function TaskRow({ task, onDelete, onToggle, onEdit }) {
  const statusClass = task.status === "active" ? "status-active" : "status-paused";
  const statusLabel = task.status === "active" ? "active" : "paused";

  function formatNextRun(nextRun) {
    if (!nextRun) return "—";
    try {
      const d = new Date(nextRun);
      if (isNaN(d.getTime())) return nextRun;
      const now = new Date();
      const diff = d - now;
      const isPast = diff < 0;
      const absDiff = Math.abs(diff);
      const hours = Math.floor(absDiff / 3600000);
      const minutes = Math.floor((absDiff % 3600000) / 60000);
      let rel = "";
      if (hours > 24) {
        const days = Math.floor(hours / 24);
        rel = days + "d " + (hours % 24) + "h";
      } else if (hours > 0) {
        rel = hours + "h " + minutes + "m";
      } else {
        rel = minutes + "m";
      }
      return (isPast ? "-" : "") + rel;
    } catch (e) {
      return nextRun;
    }
  }

  function formatSchedule(task) {
    var label = t("tasks." + task.schedule_type) || task.schedule_type;
    return label + ": " + task.schedule_value;
  }

  return (
    <div className={"tasks-row " + (task.status === "paused" ? "row-paused" : "")}>
      <span className="tasks-col-status">
        <span className={"tasks-status-dot " + statusClass} title={statusLabel}></span>
      </span>
      <span className="tasks-col-prompt" title={task.prompt}>{task.prompt}</span>
      <span className="tasks-col-schedule">
        {formatSchedule(task)}
        {task.permission_mode === "full_access" ? <span className="tasks-perm-badge" title={t("tasks.fullAccessBadge")}>🔓</span> : null}
      </span>
      <span className="tasks-col-next">{formatNextRun(task.next_run)}</span>
      <span className="tasks-col-actions">
        <button className="iconbtn tasks-action-btn" onClick={onEdit} title={t("tasks.edit")}>{t("tasks.edit")}</button>
        <button className="iconbtn tasks-action-btn" onClick={onToggle} title={task.status === "active" ? t("tasks.pause") : t("tasks.resume")}>
          {task.status === "active" ? "⏸" : "▶"}
        </button>
        <button className="iconbtn tasks-action-btn tasks-action-delete" onClick={onDelete} title={t("tasks.delete")}>{t("tasks.delete")}</button>
      </span>
    </div>
  );
}

function TaskForm({ task, onSave, onCancel }) {
  const isEdit = !!task;
  const [prompt, setPrompt] = useStateT(task ? task.prompt : "");
  const [scheduleType, setScheduleType] = useStateT(task ? task.schedule_type : "cron");
  const [scheduleValue, setScheduleValue] = useStateT(task ? task.schedule_value : "");
  const [nextRun, setNextRun] = useStateT(task ? task.next_run || "" : "");
  const [permissionMode, setPermissionMode] = useStateT(task ? (task.permission_mode || "workspace_only") : "workspace_only");
  const [saving, setSaving] = useStateT(false);
  const promptRef = useRefT(null);
  const { t } = useI18n();

  useEffectT(() => { if (promptRef.current) promptRef.current.focus(); }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!prompt.trim() || !scheduleValue.trim()) return;
    setSaving(true);
    try {
      const payload = {
        prompt: prompt.trim(),
        schedule_type: scheduleType,
        schedule_value: scheduleValue.trim(),
        permission_mode: permissionMode,
      };
      if (nextRun.trim()) payload.next_run = nextRun.trim();
      await onSave(payload);
    } finally {
      setSaving(false);
    }
  }

  const scheduleHint = scheduleType === "cron"
    ? t("tasks.hintCron")
    : scheduleType === "interval"
    ? t("tasks.hintInterval")
    : t("tasks.hintOnce");

  return (
    <form className="tasks-form" onSubmit={handleSubmit}>
      <div className="tasks-form-head">
        <span>{isEdit ? t("tasks.editTask") : t("tasks.newTask")}</span>
        <button type="button" className="iconbtn" onClick={onCancel}>&times;</button>
      </div>
      <label className="tasks-form-field">
        <span>{t("tasks.prompt")}</span>
        <textarea
          ref={promptRef}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
          placeholder={t("tasks.promptPlaceholder")}
          required
        />
      </label>
      <div className="tasks-form-row">
        <label className="tasks-form-field">
          <span>{t("tasks.scheduleType")}</span>
          <select value={scheduleType} onChange={(e) => setScheduleType(e.target.value)}>
            <option value="cron">{t("tasks.cron")}</option>
            <option value="interval">{t("tasks.interval")}</option>
            <option value="once">{t("tasks.once")}</option>
          </select>
        </label>
        <label className="tasks-form-field">
          <span>{t("tasks.scheduleValue")}</span>
          <input
            type="text"
            value={scheduleValue}
            onChange={(e) => setScheduleValue(e.target.value)}
            placeholder={scheduleHint}
            required
          />
        </label>
      </div>
      {scheduleType === "once" && (
        <label className="tasks-form-field">
          <span>{t("tasks.nextRun")}</span>
          <input
            type="datetime-local"
            value={nextRun ? nextRun.slice(0, 16) : ""}
            onChange={(e) => setNextRun(e.target.value ? new Date(e.target.value).toISOString() : "")}
          />
        </label>
      )}
      <div className="tasks-form-field">
        <span>{t("tasks.permissionMode")}</span>
        <div className="seg" style={{ marginTop: 4 }}>
          <button type="button" className={"seg-btn " + (permissionMode === "workspace_only" ? "active" : "")}
            onClick={() => setPermissionMode("workspace_only")}>
            {t("tasks.workspaceOnly")}
          </button>
          <button type="button" className={"seg-btn " + (permissionMode === "full_access" ? "active" : "")}
            onClick={() => setPermissionMode("full_access")}>
            {t("tasks.fullAccess")}
          </button>
        </div>
        <small className="hint" style={{ marginTop: 4, display: "block" }}>
          {permissionMode === "full_access" ? t("tasks.fullAccessHint") : t("tasks.workspaceOnlyHint")}
        </small>
      </div>
      <div className="tasks-form-actions">
        <button type="submit" className="iconbtn tasks-form-submit" disabled={saving || !prompt.trim() || !scheduleValue.trim()}>
          {saving ? t("tasks.saving") : (isEdit ? t("tasks.save") : t("tasks.create"))}
        </button>
        <button type="button" className="iconbtn" onClick={onCancel}>{t("tasks.cancel")}</button>
      </div>
    </form>
  );
}

window.ScheduledTasksPage = ScheduledTasksPage;
