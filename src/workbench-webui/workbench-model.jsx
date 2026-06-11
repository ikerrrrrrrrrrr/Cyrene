// Cyrene workbench data adapter.
// Keeps the new Project/Task Session UI decoupled from the legacy chat shell.

var WorkbenchModel = (function () {
  function apiJson(url, options) {
    return fetch(url, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          throw new Error(payload.error || payload.detail || ("HTTP " + response.status));
        }
        return payload;
      });
    });
  }

  function normalizeStore(payload) {
    var store = payload && typeof payload === "object" ? payload : {};
    var projects = Array.isArray(store.projects) ? store.projects : [];
    projects.forEach(function (project) {
      if (!Array.isArray(project.sessions)) project.sessions = [];
      project.sessions.forEach(function (session) {
        if (!Array.isArray(session.constraints)) session.constraints = [];
        if (!Array.isArray(session.plan)) session.plan = [];
        if (!Array.isArray(session.events)) session.events = [];
        if (!Array.isArray(session.runs)) session.runs = [];
        if (!Array.isArray(session.artifacts)) session.artifacts = [];
        if (!Array.isArray(session.acceptanceCriteria)) session.acceptanceCriteria = [];
      });
    });
    var activeProjectId = store.activeProjectId || (projects[0] && projects[0].id) || "";
    var activeProject = projects.find(function (project) { return project.id === activeProjectId; }) || projects[0] || null;
    var activeSessionId = store.activeSessionId || (activeProject && activeProject.sessions[0] && activeProject.sessions[0].id) || "";
    var activeSession = activeProject
      ? (activeProject.sessions.find(function (session) { return session.id === activeSessionId; }) || activeProject.sessions[0] || null)
      : null;
    return {
      projects: projects,
      activeProjectId: activeProject ? activeProject.id : "",
      activeSessionId: activeSession ? activeSession.id : "",
      activeProject: activeProject,
      activeSession: activeSession,
    };
  }

  function fetchProjects() {
    return apiJson("/api/projects").then(normalizeStore);
  }

  function createProject(input) {
    return apiJson("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input || {}),
    }).then(normalizeStore);
  }

  function createSession(projectId, input) {
    return apiJson("/api/projects/" + encodeURIComponent(projectId) + "/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input || {}),
    }).then(normalizeStore);
  }

  // Ask the agent to (re)generate the onboarding questions for a project's
  // "初始化项目" session, tailored to the project's name/description/template.
  function generateInitForm(projectId) {
    return apiJson("/api/projects/" + encodeURIComponent(projectId) + "/init/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(normalizeStore);
  }

  // Finalize initialization: persist answers, write the project brief, and seed
  // the first real task. Returns the normalized store (active = first task).
  function submitInit(sessionId, answers) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/init/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: answers || {} }),
    }).then(normalizeStore);
  }

  function reviseInitPlan(sessionId, feedback) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/init/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: feedback || "" }),
    }).then(normalizeStore);
  }

  function confirmInitPlan(sessionId, taskPlan) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/init/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ taskPlan: Array.isArray(taskPlan) ? taskPlan : [] }),
    }).then(normalizeStore);
  }

  // options: { attachments, mode, command, signal }
  function createRun(sessionId, input, options) {
    options = options || {};
    var init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input: input || "",
        attachments: options.attachments || [],
        mode: options.mode || undefined,
        command: options.command || undefined,
      }),
    };
    if (options.signal) init.signal = options.signal;
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/runs", init).then(normalizeStore);
  }

  function sendChat(sessionId, message, options) {
    options = options || {};
    var init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message || "",
        attachments: options.attachments || [],
        mode: options.mode || undefined,
        command: options.command || undefined,
      }),
    };
    if (options.signal) init.signal = options.signal;
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/chat", init).then(normalizeStore);
  }

  // Stop the active agent run for a session (best-effort, per-session interrupt).
  function interruptSession(sessionId) {
    return fetch("/api/chat/interrupt?session_id=" + encodeURIComponent(sessionId), { method: "POST" })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .catch(function () { return {}; });
  }

  // Upload files via the shared /api/chat/upload endpoint. Returns attachment
  // objects ({ id, name, path, content_type, size, kind, url, ... }).
  function uploadAttachments(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return Promise.resolve([]);
    var form = new FormData();
    list.forEach(function (f) { form.append("files", f); });
    return fetch("/api/chat/upload", { method: "POST", body: form }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (payload) {
        if (!r.ok) throw new Error(payload.error || ("HTTP " + r.status));
        return Array.isArray(payload.files) ? payload.files : [];
      });
    });
  }

  // Generic session patch — drives the task state machine entirely from the
  // client (status / plan / acceptanceCriteria / events / artifacts ...).
  function patchSession(sessionId, patch) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch || {}),
    }).then(normalizeStore);
  }

  function statusText(status) {
    var raw = String(status || "idle");
    var map = {
      idle: "未开始",
      pending: "待执行",
      initializing: "初始化中",
      planning: "规划中",
      running: "进行中",
      waiting_for_user: "等待确认",
      waiting_for_approval: "等待确认",
      blocked: "阻塞",
      review: "待验收",
      failed: "失败",
      paused: "已暂停",
      cancelled: "已取消",
      done: "已完成",
      completed: "已完成",
      skipped: "已跳过",
    };
    return map[raw] || raw;
  }

  function statusTone(status) {
    var raw = String(status || "idle");
    if (raw === "running" || raw === "planning" || raw === "review" || raw === "initializing") return "blue";
    if (raw === "waiting_for_user" || raw === "waiting_for_approval" || raw === "blocked") return "amber";
    if (raw === "paused") return "amber";
    if (raw === "failed") return "red";
    if (raw === "done" || raw === "completed") return "green";
    return "muted"; // idle / pending / cancelled / skipped
  }

  // Short human-readable label for a run-log event type.
  function eventLabel(type) {
    var map = {
      UserMessageEvent: "用户输入",
      AgentResponseEvent: "Agent 回复",
      PlanUpdatedEvent: "计划更新",
      PlanGenerated: "生成计划",
      PlanRevised: "调整计划",
      PlanApproved: "批准计划",
      ActionRejected: "拒绝操作",
      ExecutionStarted: "开始执行",
      ExecutionFinished: "执行完成",
      ExecutionFailed: "执行失败",
      Paused: "暂停任务",
      Resumed: "继续任务",
      StepSkipped: "跳过步骤",
      TaskCompleted: "任务完成",
      Reopened: "重新打开",
      Cancelled: "取消任务",
    };
    return map[String(type || "")] || String(type || "事件");
  }

  function formatTime(value) {
    if (!value) return "—";
    try {
      var date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
      var now = new Date();
      var sameDay = date.toDateString() === now.toDateString();
      if (sameDay) {
        return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
      return date.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
    } catch (e) {
      return String(value).slice(0, 16);
    }
  }

  function initials(name) {
    var source = String(name || "C").trim();
    if (!source) return "C";
    var parts = source.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return source.slice(0, 1).toUpperCase();
  }

  // Stable per-project icon gradient derived from a seed (project id or name),
  // so each project card gets its own color like the reference design.
  function projectGradient(seed) {
    var palette = [
      ["#8f5cff", "#5b7dff"],
      ["#3b82f6", "#2567e8"],
      ["#22b07a", "#149e63"],
      ["#fb7185", "#ef4d57"],
      ["#f2a51a", "#ef7e1a"],
      ["#06b6d4", "#0e8fb0"],
    ];
    var str = String(seed || "");
    var hash = 0;
    for (var i = 0; i < str.length; i++) {
      hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
    }
    var pair = palette[hash % palette.length];
    return "linear-gradient(135deg, " + pair[0] + ", " + pair[1] + ")";
  }

  // ---- State-machine data helpers (pure) ----------------------------------

  function shortId(prefix) {
    return String(prefix || "id") + "_" + Math.random().toString(36).slice(2, 12);
  }

  // Build a default execution plan. Mirrors the backend's base steps so a plan
  // generated client-side looks consistent with one the agent produced.
  function buildPlanSteps(goal, constraints) {
    var titles = ["理解目标与约束", "读取项目上下文", "分析相关文件结构", "设计执行方案", "实施或生成变更", "验证结果并总结"];
    return titles.map(function (title, index) {
      return {
        id: shortId("step"),
        title: title,
        description: "",
        status: "pending",
        order: index + 1,
        currentAction: "",
        relatedFiles: [],
        progressEvents: [],
        toolCalls: [],
        artifacts: [],
        error: null,
      };
    });
  }

  function buildAcceptance(goal, constraints) {
    var items = (Array.isArray(constraints) ? constraints : [])
      .map(function (item) { return String(item || "").trim(); })
      .filter(Boolean)
      .slice(0, 4);
    if (!items.length) items = ["任务目标已明确", "执行计划已生成", "相关变更可追踪", "最终总结已生成"];
    return items.map(function (text) {
      return { id: shortId("accept"), text: text, status: "pending" };
    });
  }

  // Summary shown in the "需要你确认" card before a sensitive run.
  function confirmSummary(session) {
    var plan = session && Array.isArray(session.plan) ? session.plan : [];
    var actions = plan.slice(0, 5).map(function (step) { return step.title; });
    if (!actions.length) actions = ["执行当前任务"];
    var constraints = session && Array.isArray(session.constraints) ? session.constraints : [];
    return {
      actions: actions,
      scope: ["当前任务执行区", "右侧上下文 / 运行日志", "验收标准与产物"],
      risk: constraints.length ? "中" : "低",
      files: [],
    };
  }

  function makeEvent(type, body, extra) {
    return Object.assign(
      { id: shortId("event"), type: type, createdAt: new Date().toISOString(), body: body || "" },
      extra || {}
    );
  }

  // Returns a NEW events array with one appended (never mutates the session).
  function withEvent(session, type, body, extra) {
    var events = session && Array.isArray(session.events) ? session.events.slice() : [];
    events.push(makeEvent(type, body, extra));
    return events;
  }

  function markStep(plan, index, status, action) {
    if (!Array.isArray(plan)) return [];
    return plan.map(function (step, i) {
      if (i !== index) return step;
      return Object.assign({}, step, {
        status: status,
        currentAction: action != null ? action : step.currentAction,
        updatedAt: new Date().toISOString(),
      });
    });
  }

  function markAllSteps(plan, status) {
    if (!Array.isArray(plan)) return [];
    return plan.map(function (step) {
      return Object.assign({}, step, { status: status, updatedAt: new Date().toISOString() });
    });
  }

  function markAllAcceptance(items, status) {
    if (!Array.isArray(items)) return [];
    return items.map(function (item) { return Object.assign({}, item, { status: status }); });
  }

  // Ensure the session has at least one artifact once it has executed.
  function ensureArtifacts(session) {
    var arr = session && Array.isArray(session.artifacts) ? session.artifacts.slice() : [];
    if (arr.length) return arr;
    return [{
      id: shortId("artifact"),
      type: "summary",
      name: "task-summary.md",
      status: "ready",
      createdAt: new Date().toISOString(),
      summary: "任务执行过程与结果的结构化总结。",
    }];
  }

  // Heuristic: does this composer message ask for something beyond the current
  // task (rule 2 — "Agent 不应脱离当前任务")?
  function looksOutOfScope(text) {
    var src = String(text || "");
    return /(顺便|另外|额外|再帮我|再做|新功能|加一个功能|做个新的|新的任务|新建一个)/.test(src);
  }

  window.WorkbenchModel = {
    normalizeStore: normalizeStore,
    fetchProjects: fetchProjects,
    createProject: createProject,
    createSession: createSession,
    generateInitForm: generateInitForm,
    submitInit: submitInit,
    reviseInitPlan: reviseInitPlan,
    confirmInitPlan: confirmInitPlan,
    createRun: createRun,
    sendChat: sendChat,
    patchSession: patchSession,
    interruptSession: interruptSession,
    uploadAttachments: uploadAttachments,
    statusText: statusText,
    statusTone: statusTone,
    eventLabel: eventLabel,
    formatTime: formatTime,
    initials: initials,
    projectGradient: projectGradient,
    shortId: shortId,
    buildPlanSteps: buildPlanSteps,
    buildAcceptance: buildAcceptance,
    confirmSummary: confirmSummary,
    makeEvent: makeEvent,
    withEvent: withEvent,
    markStep: markStep,
    markAllSteps: markAllSteps,
    markAllAcceptance: markAllAcceptance,
    ensureArtifacts: ensureArtifacts,
    looksOutOfScope: looksOutOfScope,
  };

  return window.WorkbenchModel;
})();
