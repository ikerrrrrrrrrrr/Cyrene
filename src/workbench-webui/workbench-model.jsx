// Cyrene workbench data adapter.
// Keeps the new Project/Task Session UI decoupled from the legacy chat shell.

var WorkbenchModel = (function () {
  function wbModelT(key, fallback, params) {
    if (window.WorkbenchI18n && typeof window.WorkbenchI18n.t === "function") {
      return window.WorkbenchI18n.t(key, params, fallback);
    }
    if (params && fallback) {
      Object.keys(params).forEach(function (name) {
        fallback = fallback.split("{" + name + "}").join(String(params[name]));
      });
    }
    return fallback || key;
  }

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

  function updateProject(projectId, input) {
    return apiJson("/api/projects/" + encodeURIComponent(projectId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input || {}),
    }).then(normalizeStore);
  }

  function deleteProject(projectId) {
    return apiJson("/api/projects/" + encodeURIComponent(projectId), {
      method: "DELETE",
    }).then(normalizeStore);
  }

  function createSession(projectId, input) {
    return apiJson("/api/projects/" + encodeURIComponent(projectId) + "/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input || {}),
    }).then(normalizeStore);
  }

  function fetchNotifications(tab, limit) {
    var qs = "?tab=" + encodeURIComponent(tab || "all") + "&limit=" + encodeURIComponent(limit || 80);
    return apiJson("/api/workbench/notifications" + qs);
  }

  function markNotificationsRead(ids, markAll) {
    return apiJson("/api/workbench/notifications/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ids: Array.isArray(ids) ? ids : [],
        markAll: !!markAll,
      }),
    });
  }

  // Ask the agent to (re)generate the onboarding questions for a project's
  // "初始化项目" session, tailored to the project's name/description/template.
  function generateInitForm(projectId) {
    var lang = (window.WorkbenchI18n && window.WorkbenchI18n.getLang ? window.WorkbenchI18n.getLang() : "zh").trim();
    return apiJson("/api/projects/" + encodeURIComponent(projectId) + "/init/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lang: lang }),
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

  function reviseInitPlan(sessionId, feedback, taskPlan) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/init/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        feedback: feedback || "",
        taskPlan: Array.isArray(taskPlan) ? taskPlan : [],
      }),
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
        stepId: options.stepId || undefined,
        stepTitle: options.stepTitle || undefined,
        action: options.action || undefined,
        meta: options.meta || undefined,
      }),
    };
    if (options.signal) init.signal = options.signal;
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/runs", init).then(normalizeStore);
  }

  // Generate a REAL execution plan from the session goal (+ optional revision
  // feedback). The agent explores the project workspace server-side; no agent
  // work runs here — it only fills session.plan (all steps pending).
  // Run deep reflection over a task's accumulated history; attaches the packet
  // to session.reflection (used by replanning + execution).
  function reflect(sessionId, options) {
    options = options || {};
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/reflect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ focus: options.focus || "", goalGap: options.goalGap || "" }),
    }).then(normalizeStore);
  }

  // Independent acceptance agent verifies criteria against the real results.
  function verify(sessionId) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(normalizeStore);
  }

  // Reflect on a failed task, then fork a fresh session carrying the packet.
  function reflectAndFork(sessionId) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/reflect-and-fork", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(normalizeStore);
  }

  // Accept a sibling-reflection hint → merge its packet into this session.
  function acceptHint(sessionId, hintId) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/hints/" + encodeURIComponent(hintId) + "/accept", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(normalizeStore);
  }

  // Dismiss a sibling-reflection hint (no change to this session).
  function dismissHint(sessionId, hintId) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/hints/" + encodeURIComponent(hintId) + "/dismiss", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(normalizeStore);
  }

  function generatePlan(sessionId, goal, options) {
    options = options || {};
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/plan/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: goal || "", feedback: options.feedback || "" }),
    }).then(normalizeStore);
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

  function fetchFileDiff(sessionId, path) {
    return apiJson(
      "/api/task-sessions/" + encodeURIComponent(sessionId) + "/files/diff?path=" + encodeURIComponent(path || "")
    );
  }

  // Validate a workspace-relative context-file path. Resolves to
  // { exists, path, isDir, error? }; never throws (a 400 still carries a body so
  // the per-step file editor can show inline feedback).
  function checkWorkspacePath(sessionId, path) {
    return fetch(
      "/api/task-sessions/" + encodeURIComponent(sessionId) + "/workspace/exists?path=" + encodeURIComponent(path || "")
    )
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .catch(function () { return { exists: false, path: path || "", error: "网络错误" }; });
  }

  // Persist the active project (and optionally session) to the server store so
  // the selection survives page refresh. Returns a normalized store snapshot.
  function setActiveProject(projectId, sessionId) {
    var body = {};
    if (projectId != null) body.projectId = projectId;
    if (sessionId != null) body.sessionId = sessionId;
    return apiJson("/api/workbench/activate", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(normalizeStore);
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

  function deleteSession(sessionId) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId), {
      method: "DELETE",
    }).then(normalizeStore);
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
      idle: ["status.idle", "Not started"],
      pending: ["status.pending", "Pending"],
      initializing: ["status.initializing", "Initializing"],
      planning: ["status.planning", "Planning"],
      running: ["status.running", "Running"],
      waiting_for_user: ["status.waiting", "Waiting"],
      waiting_for_approval: ["status.waiting", "Waiting"],
      blocked: ["status.blocked", "Blocked"],
      review: ["status.review", "In review"],
      failed: ["status.failed", "Failed"],
      paused: ["status.paused", "Paused"],
      cancelled: ["status.cancelled", "Cancelled"],
      done: ["status.done", "Done"],
      completed: ["status.done", "Done"],
      skipped: ["status.skipped", "Skipped"],
    };
    return map[raw] ? wbModelT(map[raw][0], map[raw][1]) : raw;
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
      UserMessageEvent: ["event.userMessage", "User input"],
      AgentResponseEvent: ["event.agentResponse", "Agent response"],
      PlanUpdatedEvent: ["event.planUpdated", "Plan updated"],
      PlanGenerated: ["event.planGenerated", "Plan generated"],
      PlanRevised: ["event.planRevised", "Plan revised"],
      PlanApproved: ["event.planApproved", "Plan approved"],
      ActionRejected: ["event.actionRejected", "Action rejected"],
      ExecutionStarted: ["event.executionStarted", "Execution started"],
      ExecutionFinished: ["event.executionFinished", "Execution finished"],
      ExecutionFailed: ["event.executionFailed", "Execution failed"],
      Paused: ["event.paused", "Task paused"],
      Resumed: ["event.resumed", "Task resumed"],
      StepSkipped: ["event.stepSkipped", "Step skipped"],
      TaskCompleted: ["event.taskCompleted", "Task completed"],
      Reopened: ["event.reopened", "Reopened"],
      Cancelled: ["event.cancelled", "Task cancelled"],
      ToolCallEvent: ["event.toolCall", "工具调用"],
      LlmCallEvent: ["event.llmCall", "模型思考"],
      SubagentStatusEvent: ["event.subagentStatus", "Subagent 状态"],
    };
    var item = map[String(type || "")];
    return item ? wbModelT(item[0], item[1]) : String(type || wbModelT("event.generic", "Event"));
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

  function formatRelativeTime(value) {
    if (!value) return wbModelT("time.justNow", "Just now");
    try {
      var date = new Date(value);
      var diff = Date.now() - date.getTime();
      if (!Number.isFinite(diff)) return wbModelT("time.justNow", "Just now");
      var minute = 60 * 1000;
      var hour = 60 * minute;
      var day = 24 * hour;
      if (diff < minute) return wbModelT("time.justNow", "Just now");
      if (diff < hour) return wbModelT("time.minutesAgo", "{n}m ago", { n: Math.max(1, Math.floor(diff / minute)) });
      if (diff < day) return wbModelT("time.hoursAgo", "{n}h ago", { n: Math.max(1, Math.floor(diff / hour)) });
      if (diff < day * 2) return wbModelT("time.yesterday", "Yesterday");
      if (diff < day * 7) return wbModelT("time.daysAgo", "{n}d ago", { n: Math.max(1, Math.floor(diff / day)) });
      return formatTime(value);
    } catch (e) {
      return wbModelT("time.justNow", "Just now");
    }
  }

  function initials(name) {
    var source = String(name || "C").trim();
    if (!source) return "C";
    var parts = source.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return source.slice(0, 1).toUpperCase();
  }

  function pathLabel(path, projectName) {
    var raw = String(path || "").trim();
    if (!raw) return wbModelT("path.unsetWorkspace", "Workspace not set");
    var home = "";
    try {
      home = (window.DATA && DATA.user && DATA.user.home) || "";
    } catch (e) {}
    if (home && raw.indexOf(home + "/") === 0) raw = "~" + raw.slice(home.length);
    var parts = raw.split("/").filter(Boolean);
    if (raw[0] === "~" && parts.length) parts[0] = "~";
    var name = String(projectName || "").trim();
    if (name && parts.length >= 2 && parts[parts.length - 1] === "workspace" && parts[parts.length - 2] === name) {
      return name + "/workspace";
    }
    if (parts.length <= 3) return raw;
    return "..." + parts.slice(-3).join("/");
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

  // Stamp a step's lifecycle timing so the plan card can show 时间/时长:
  // record startedAt on the running transition and a concrete durationSec
  // (start → now) once it completes.
  function applyStepTiming(step, status, nowIso) {
    if (status === "running" && !step.startedAt) step.startedAt = nowIso;
    if (status === "completed" || status === "done") {
      if (!step.completedAt) step.completedAt = nowIso;
      if (step.startedAt && step.durationSec == null) {
        var sec = Math.round((Date.parse(nowIso) - Date.parse(step.startedAt)) / 1000);
        if (sec >= 1) step.durationSec = sec;
      }
    }
    return step;
  }

  function markStep(plan, index, status, action) {
    if (!Array.isArray(plan)) return [];
    var now = new Date().toISOString();
    return plan.map(function (step, i) {
      if (i !== index) return step;
      var next = Object.assign({}, step, {
        status: status,
        currentAction: action != null ? action : step.currentAction,
        updatedAt: now,
      });
      return applyStepTiming(next, status, now);
    });
  }

  function markAllSteps(plan, status) {
    if (!Array.isArray(plan)) return [];
    var now = new Date().toISOString();
    return plan.map(function (step) {
      var next = Object.assign({}, step, { status: status, updatedAt: now });
      return applyStepTiming(next, status, now);
    });
  }

  function markAllAcceptance(items, status) {
    if (!Array.isArray(items)) return [];
    return items.map(function (item) { return Object.assign({}, item, { status: status }); });
  }

  function isDoneStepStatus(status) {
    return status === "completed" || status === "done";
  }

  function isResolvedStepStatus(status) {
    return isDoneStepStatus(status) || status === "skipped";
  }

  function hasUnresolvedStartedSteps(plan) {
    if (!Array.isArray(plan)) return false;
    return plan.some(function (step) {
      if (!step) return false;
      var status = step.status || "pending";
      return status !== "pending" && !isResolvedStepStatus(status);
    });
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
    return /(顺便|另外|额外|再帮我|再做|加一个新的|重新做一个|新建一个)/.test(src);
  }

  window.WorkbenchModel = {
    normalizeStore: normalizeStore,
    fetchProjects: fetchProjects,
    createProject: createProject,
    updateProject: updateProject,
    deleteProject: deleteProject,
    createSession: createSession,
    deleteSession: deleteSession,
    fetchNotifications: fetchNotifications,
    markNotificationsRead: markNotificationsRead,
    generateInitForm: generateInitForm,
    submitInit: submitInit,
    reviseInitPlan: reviseInitPlan,
    confirmInitPlan: confirmInitPlan,
    createRun: createRun,
    reflect: reflect,
    verify: verify,
    reflectAndFork: reflectAndFork,
    acceptHint: acceptHint,
    dismissHint: dismissHint,
    generatePlan: generatePlan,
    sendChat: sendChat,
    fetchFileDiff: fetchFileDiff,
    checkWorkspacePath: checkWorkspacePath,
    patchSession: patchSession,
    setActiveProject: setActiveProject,
    interruptSession: interruptSession,
    uploadAttachments: uploadAttachments,
    statusText: statusText,
    statusTone: statusTone,
    eventLabel: eventLabel,
    formatTime: formatTime,
    formatRelativeTime: formatRelativeTime,
    initials: initials,
    pathLabel: pathLabel,
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
    hasUnresolvedStartedSteps: hasUnresolvedStartedSteps,
    ensureArtifacts: ensureArtifacts,
    looksOutOfScope: looksOutOfScope,
  };

  return window.WorkbenchModel;
})();
