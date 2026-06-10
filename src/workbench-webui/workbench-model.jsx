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

  function createRun(sessionId, input) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: input || "" }),
    }).then(normalizeStore);
  }

  function sendChat(sessionId, message) {
    return apiJson("/api/task-sessions/" + encodeURIComponent(sessionId) + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message || "" }),
    }).then(normalizeStore);
  }

  function statusText(status) {
    var raw = String(status || "idle");
    var map = {
      idle: "未开始",
      pending: "待执行",
      planning: "规划中",
      running: "进行中",
      waiting_for_user: "待确认",
      waiting_for_approval: "待审批",
      blocked: "阻塞",
      failed: "失败",
      done: "已完成",
      completed: "已完成",
      skipped: "已跳过",
    };
    return map[raw] || raw;
  }

  function statusTone(status) {
    var raw = String(status || "idle");
    if (raw === "running" || raw === "planning") return "blue";
    if (raw === "waiting_for_user" || raw === "waiting_for_approval" || raw === "blocked") return "amber";
    if (raw === "failed") return "red";
    if (raw === "done" || raw === "completed") return "green";
    return "muted";
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

  window.WorkbenchModel = {
    normalizeStore: normalizeStore,
    fetchProjects: fetchProjects,
    createProject: createProject,
    createSession: createSession,
    createRun: createRun,
    sendChat: sendChat,
    statusText: statusText,
    statusTone: statusTone,
    formatTime: formatTime,
    initials: initials,
    projectGradient: projectGradient,
  };

  return window.WorkbenchModel;
})();
