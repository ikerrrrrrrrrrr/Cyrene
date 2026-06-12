// Workbench 对话页面 — workspace-bound conversations (kind: "chat").
// Independent from the legacy chat UI (chat.jsx / chat-surface.jsx): only the
// backend endpoints (/api/workbench/chats*, /api/chat/upload, /api/events SSE)
// are shared. Layout: chat rail | conversation | right context panel.

var {
  useState: useWbcState,
  useEffect: useWbcEffect,
  useMemo: useWbcMemo,
  useRef: useWbcRef,
  useCallback: useWbcCallback,
} = React;

// ---------------------------------------------------------------------------
// Data access
// ---------------------------------------------------------------------------

var WorkbenchChatModel = (function () {
  function apiJson(url, options) {
    return fetch(url, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) throw new Error(payload.error || payload.detail || ("HTTP " + response.status));
        return payload;
      });
    });
  }

  function listChats(projectId) {
    return apiJson("/api/workbench/chats?project=" + encodeURIComponent(projectId || ""))
      .then(function (payload) { return Array.isArray(payload.chats) ? payload.chats : []; });
  }

  function createChat(projectId, title) {
    return apiJson("/api/workbench/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectId, title: title || "" }),
    }).then(function (payload) { return payload.chat; });
  }

  function getChat(chatId) {
    return apiJson("/api/workbench/chats/" + encodeURIComponent(chatId))
      .then(function (payload) { return payload.chat; });
  }

  function renameChat(chatId, title) {
    return apiJson("/api/workbench/chats/" + encodeURIComponent(chatId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title }),
    }).then(function (payload) { return payload.chat; });
  }

  function deleteChat(chatId) {
    return apiJson("/api/workbench/chats/" + encodeURIComponent(chatId), { method: "DELETE" });
  }

  function toTask(chatId, input) {
    return apiJson("/api/workbench/chats/" + encodeURIComponent(chatId) + "/to-task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input || {}),
    });
  }

  function interrupt(chatId) {
    return fetch("/api/chat/interrupt?session_id=" + encodeURIComponent(chatId), { method: "POST" })
      .catch(function () {});
  }

  function uploadFiles(files) {
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

  // Streaming send. handlers: { onAck, onReplyStart, onReplyDelta, onReplyDone, onSaved, onError }
  function sendMessage(chatId, input, handlers, signal) {
    handlers = handlers || {};
    return fetch("/api/workbench/chats/" + encodeURIComponent(chatId) + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: input.message || "",
        attachments: input.attachments || [],
        mode: input.mode || "auto",
        command: input.command || "",
        retry: !!input.retry,
        stream: true,
      }),
      signal: signal,
    }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          throw new Error(payload.error || payload.detail || ("HTTP " + response.status));
        });
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      function handleLine(line) {
        if (!line.trim()) return;
        var event;
        try { event = JSON.parse(line); } catch (e) { return; }
        var type = String(event.type || "");
        if (type === "ack" && handlers.onAck) handlers.onAck(event);
        else if (type === "reply_start" && handlers.onReplyStart) handlers.onReplyStart(event);
        else if (type === "reply_delta" && handlers.onReplyDelta) handlers.onReplyDelta(event.delta || "");
        else if (type === "reply_done" && handlers.onReplyDone) handlers.onReplyDone(event.response || "");
        else if (type === "saved" && handlers.onSaved) handlers.onSaved(event);
        else if (type === "error" && handlers.onError) handlers.onError(new Error(event.message || "执行失败"));
      }

      function pump() {
        return reader.read().then(function (step) {
          if (step.done) {
            if (buffer) handleLine(buffer);
            return null;
          }
          buffer += decoder.decode(step.value, { stream: true });
          var lines = buffer.split("\n");
          buffer = lines.pop();
          lines.forEach(handleLine);
          return pump();
        });
      }
      return pump();
    });
  }

  window.WorkbenchChatModel = {
    listChats: listChats,
    createChat: createChat,
    getChat: getChat,
    renameChat: renameChat,
    deleteChat: deleteChat,
    toTask: toTask,
    interrupt: interrupt,
    uploadFiles: uploadFiles,
    sendMessage: sendMessage,
  };
  return window.WorkbenchChatModel;
})();

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function wbcRenderMarkdown(text) {
  var source = String(text == null ? "" : text);
  try {
    var raw = window.marked ? window.marked.parse(source) : source;
    return window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw;
  } catch (e) {
    return source;
  }
}

function wbcFormatTime(value) {
  if (!value) return "";
  try {
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    var now = new Date();
    var dayMs = 24 * 3600 * 1000;
    var startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (date >= startOfDay) {
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    if (date >= new Date(startOfDay.getTime() - dayMs)) return "昨天";
    var days = Math.floor((startOfDay.getTime() - date.getTime()) / dayMs) + 1;
    if (days <= 7) return days + "天前";
    return date.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
  } catch (e) {
    return "";
  }
}

function wbcCompactNumber(value) {
  var num = Number(value || 0);
  if (!num) return "0";
  if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
  if (num >= 1000) return (num / 1000).toFixed(1) + "k";
  return String(num);
}

function wbcT(key, fallback, params) {
  if (typeof window.t === "function") {
    var value = window.t(key, params);
    if (value && value !== key) return value;
  }
  if (params && fallback) {
    Object.keys(params).forEach(function (name) {
      fallback = fallback.split("{" + name + "}").join(String(params[name]));
    });
  }
  return fallback || key;
}

function wbcErrorText(err) {
  var raw = String((err && err.message) || err || "").trim();
  if (!raw || raw === "Load failed" || raw === "Failed to fetch" || raw === "NetworkError when attempting to fetch resource.") {
    return wbcT("workbenchChat.error.loadFailed", "Load failed");
  }
  return raw;
}

var WBC_ICONS = {
  plus: <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>,
  search: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>,
  alert: <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 4 2.5 18a1.5 1.5 0 0 0 1.3 2.3h16.4a1.5 1.5 0 0 0 1.3-2.3L13.7 4a1.5 1.5 0 0 0-3.4 0Z"/><path d="M12 9v4.5M12 17h.01"/></svg>,
  edit: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>,
  dots: <svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor"><circle cx="5.5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/></svg>,
  play: <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M7 4.8c0-1 1.1-1.6 2-1.1l11 6.3c.9.5.9 1.8 0 2.3L9 18.6c-.9.5-2-.1-2-1.1Z"/></svg>,
  send: <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4Z"/></svg>,
  stop: <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>,
  attach: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m21.44 11.05-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>,
  slash: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7.5 9.5 2.5 2.5-2.5 2.5"/><path d="M12.5 15h4"/></svg>,
  bolt: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg>,
  copy: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>,
  retry: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 1 0 2.6-6.3"/><path d="M3 4v4h4"/></svg>,
  check: <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12.5 4.5 4.5L19 7"/></svg>,
  x: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m6 6 12 12M18 6 6 18"/></svg>,
  tool: <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3Z"/></svg>,
  chat: <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 11.5a8.5 8.5 0 0 1-12.2 7.6L3 21l1.9-5.8A8.5 8.5 0 1 1 21 11.5Z"/></svg>,
  file: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/></svg>,
  trash: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>,
  task: <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1.5"/><path d="M9 14 10.5 15.5 15 11"/></svg>,
  spark: <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><path d="M12 2.5 13.7 9 20 10.7 13.7 12.4 12 19l-1.7-6.6L4 10.7 10.3 9Z"/></svg>,
  folder: <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>,
};

// Slash commands + permission modes (mirrors the legacy agent capabilities;
// defined locally so this page stays independent from workbench.jsx).
var WBC_COMMANDS = [
  { id: "quick-answer", label: "快速回答", desc: "用最简洁的方式直接回答" },
  { id: "deep-research", label: "深度研究", desc: "联网检索并产出研究报告" },
  { id: "deep-reflect", label: "深度反思", desc: "对话题做多角度深入思考" },
  { id: "help-me-decide", label: "帮我决定", desc: "梳理选项与利弊给出建议" },
  { id: "learning-plan", label: "学习计划", desc: "为一个主题制定学习路径" },
  { id: "daily-review", label: "每日回顾", desc: "回顾并总结今天的事项" },
  { id: "deep-compare", label: "深度对比", desc: "对多个对象做结构化对比" },
  { id: "claude-code", label: "Claude Code", desc: "用 Claude Code 处理代码任务" },
];

var WBC_MODES = [
  { id: "default", label: "默认", desc: "敏感操作前先询问你" },
  { id: "auto", label: "自动", desc: "自动批准，加速执行" },
  { id: "plan", label: "规划", desc: "只制定方案，不改动文件" },
  { id: "full_access", label: "完全访问", desc: "允许所有操作（谨慎使用）" },
];

function wbcModeMeta(id) {
  for (var i = 0; i < WBC_MODES.length; i++) if (WBC_MODES[i].id === id) return WBC_MODES[i];
  return WBC_MODES[1];
}

// ---- file classification for the side viewer -------------------------------

var WBC_CODE_EXTS = ["py","js","ts","jsx","tsx","css","json","yaml","yml","toml","xml","sql","sh","bash","rs","go","java","c","cpp","h","rb","php","swift","kt","txt","csv","ini","cfg","env","log"];

function wbcFileViewKind(file) {
  if (!file) return "";
  var ct = String(file.content_type || "");
  var ext = String(file.name || "").split(".").pop().toLowerCase();
  if (ct.indexOf("image/") === 0 || file.kind === "image") return "image";
  if (ct === "application/pdf" || ext === "pdf") return "pdf";
  if (ct.indexOf("presentation") !== -1 || ct.indexOf("ms-powerpoint") !== -1 || ext === "ppt" || ext === "pptx") return "pdf";
  if (ct.indexOf("wordprocessingml") !== -1 || ct === "application/msword" || ext === "doc" || ext === "docx") return "pdf";
  if (ct === "text/html" || ct === "application/xhtml+xml" || ext === "html" || ext === "htm") return "html";
  if (file.kind === "markdown" || ext === "md" || ext === "markdown") return "markdown";
  if (file.kind === "code" || WBC_CODE_EXTS.indexOf(ext) !== -1 || ct.indexOf("text/") === 0) return "code";
  return "download";
}

// Map tools mark the conversation as having a 地图 tab (same tool set as the
// legacy chat surface: pin_location / connect_pins).
function wbcIsMapTool(name) {
  var raw = String(name || "").trim();
  return raw === "pin_location" || raw === "connect_pins";
}

function wbcChatUsedMap(chat, runtime) {
  if (runtime && Array.isArray(runtime.progress)) {
    for (var i = 0; i < runtime.progress.length; i++) {
      if (wbcIsMapTool(runtime.progress[i].text)) return true;
    }
  }
  var messages = chat && Array.isArray(chat.messages) ? chat.messages : [];
  for (var m = 0; m < messages.length; m++) {
    var trace = messages[m].trace;
    if (!Array.isArray(trace)) continue;
    for (var t = 0; t < trace.length; t++) {
      if (wbcIsMapTool(trace[t].tool)) return true;
    }
  }
  return false;
}

function wbcCommandMeta(id) {
  for (var i = 0; i < WBC_COMMANDS.length; i++) if (WBC_COMMANDS[i].id === id) return WBC_COMMANDS[i];
  return null;
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function WorkbenchChatPage({ project, onOpenTask, onActiveChatChange }) {
  var model = window.WorkbenchChatModel;
  var projectId = project ? project.id : "";
  var [chats, setChats] = useWbcState([]);
  var [activeChatId, setActiveChatId] = useWbcState("");
  var [activeChat, setActiveChat] = useWbcState(null);
  var [loading, setLoading] = useWbcState(true);
  var [error, setError] = useWbcState("");
  var [sideTab, setSideTab] = useWbcState("overview");
  var [viewerFile, setViewerFile] = useWbcState(null);
  // Streaming runtime for the in-flight request (single concurrent run per page).
  var [runtime, setRuntime] = useWbcState(null); // {chatId, text, progress[], startedAt}
  var runtimeRef = useWbcRef(null);
  var abortRef = useWbcRef(null);

  function openViewer(file) {
    if (!file) return;
    setViewerFile(file);
    setSideTab("viewer");
  }

  function syncRuntime(next) {
    runtimeRef.current = next;
    setRuntime(next);
  }

  function refreshChats(selectId) {
    if (!projectId) return Promise.resolve([]);
    return model.listChats(projectId).then(function (list) {
      setChats(list);
      var targetId = selectId || activeChatId;
      var exists = list.some(function (c) { return c.id === targetId; });
      if (!exists) targetId = list[0] ? list[0].id : "";
      setActiveChatId(targetId);
      return list;
    });
  }

  // Initial load + project switch.
  useWbcEffect(function () {
    setLoading(true);
    setError("");
    setActiveChat(null);
    setActiveChatId("");
    syncRuntime(null);
    if (!projectId) { setChats([]); setLoading(false); return; }
    model.listChats(projectId)
      .then(function (list) {
        setChats(list);
        setActiveChatId(list[0] ? list[0].id : "");
      })
      .catch(function (err) { setError(wbcErrorText(err)); })
      .finally(function () { setLoading(false); });
  }, [projectId]);

  // Load the full transcript when the selection changes.
  useWbcEffect(function () {
    if (!activeChatId) { setActiveChat(null); return; }
    var cancelled = false;
    model.getChat(activeChatId)
      .then(function (chat) { if (!cancelled) setActiveChat(chat); })
      .catch(function (err) { if (!cancelled) setError(wbcErrorText(err)); });
    return function () { cancelled = true; };
  }, [activeChatId]);

  // Viewer / content tabs belong to one conversation — reset on switch.
  useWbcEffect(function () {
    setViewerFile(null);
    setSideTab(function (prev) { return (prev === "viewer" || prev === "map") ? "overview" : prev; });
  }, [activeChatId]);

  // Surface the active conversation title in the topbar crumbs.
  useWbcEffect(function () {
    if (onActiveChatChange) onActiveChatChange(activeChat ? activeChat.title : "");
    return function () { if (onActiveChatChange) onActiveChatChange(""); };
  }, [activeChat && activeChat.title]);

  // Live tool progress: reuse the global SSE feed (data.jsx) and keep only
  // events tagged with the running conversation's session id.
  useWbcEffect(function () {
    if (!window.__sseHandlers) return;
    function onEvent(event) {
      var current = runtimeRef.current;
      if (!current || !event || event.session_id !== current.chatId) return;
      var entry = null;
      if (event.type === "tool_call") {
        var args = event.args || {};
        var preview = Object.values(args).filter(Boolean).map(String).join(", ").slice(0, 60);
        entry = { kind: "tool", text: String(event.tool || "工具"), preview: preview };
      } else if (event.type === "phase_transition" && event.detail) {
        entry = { kind: "phase", text: String(event.detail).slice(0, 80), preview: "" };
      }
      if (!entry) return;
      var next = {
        ...current,
        progress: current.progress.concat([entry]).slice(-30),
      };
      syncRuntime(next);
    }
    window.__sseHandlers.add(onEvent);
    return function () { window.__sseHandlers.delete(onEvent); };
  }, []);

  function ensureChat() {
    if (activeChatId) return Promise.resolve(activeChatId);
    return model.createChat(projectId).then(function (chat) {
      setChats(function (prev) { return [chat].concat(prev); });
      setActiveChatId(chat.id);
      setActiveChat(chat);
      return chat.id;
    });
  }

  function retryLoad() {
    if (!projectId) return;
    setError("");
    setLoading(true);
    refreshChats(activeChatId)
      .then(function (list) {
        var chatId = activeChatId || (list[0] && list[0].id) || "";
        if (!chatId) {
          setActiveChat(null);
          return null;
        }
        return model.getChat(chatId).then(function (chat) {
          setActiveChat(chat);
          setActiveChatId(chat.id);
          return chat;
        });
      })
      .catch(function (err) { setError(wbcErrorText(err)); })
      .finally(function () { setLoading(false); });
  }

  function handleSend(input) {
    setError("");
    return ensureChat().then(function (chatId) {
      var ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
      abortRef.current = ac;
      syncRuntime({ chatId: chatId, text: "", progress: [], startedAt: Date.now(), replying: false });
      return model.sendMessage(chatId, input, {
        onAck: function (event) {
          if (event.retry) {
            // Regenerating: drop the previous reply (everything after the
            // replayed user message) from the local transcript.
            var afterId = String(event.truncateAfterMessageId || "");
            setActiveChat(function (prev) {
              if (!prev || prev.id !== chatId) return prev;
              var list = prev.messages || [];
              var cut = -1;
              for (var i = 0; i < list.length; i++) {
                if (String(list[i].id) === afterId) { cut = i; break; }
              }
              if (cut < 0) return prev;
              return { ...prev, messages: list.slice(0, cut + 1) };
            });
            return;
          }
          if (!event.userMessage) return;
          setActiveChat(function (prev) {
            if (!prev || prev.id !== chatId) return prev;
            return { ...prev, messages: (prev.messages || []).concat([event.userMessage]) };
          });
        },
        onReplyStart: function () {
          var current = runtimeRef.current;
          if (current) syncRuntime({ ...current, replying: true });
        },
        onReplyDelta: function (delta) {
          var current = runtimeRef.current;
          if (!current) return;
          syncRuntime({ ...current, replying: true, text: current.text + delta });
        },
        onReplyDone: function (text) {
          var current = runtimeRef.current;
          if (current) syncRuntime({ ...current, text: text || current.text });
        },
        onSaved: function (event) {
          if (event.assistantMessage) {
            setActiveChat(function (prev) {
              if (!prev || prev.id !== chatId) return prev;
              return { ...prev, status: "idle", messages: (prev.messages || []).concat([event.assistantMessage]) };
            });
          }
          syncRuntime(null);
          refreshChats(chatId);
        },
        onError: function (err) {
          setError(wbcErrorText(err));
          syncRuntime(null);
        },
      }, ac ? ac.signal : undefined).catch(function (err) {
        if (err && err.name === "AbortError") return;
        setError(wbcErrorText(err));
      }).finally(function () {
        abortRef.current = null;
        var current = runtimeRef.current;
        if (current && current.chatId === chatId) {
          // Stream ended without a `saved` event (e.g. interrupted) — re-pull.
          syncRuntime(null);
          model.getChat(chatId).then(setActiveChat).catch(function () {});
          refreshChats(chatId);
        }
      });
    }).catch(function (err) {
      setError(wbcErrorText(err));
    });
  }

  function handleInterrupt() {
    var current = runtimeRef.current;
    if (current) model.interrupt(current.chatId);
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
  }

  // Regenerate the last assistant reply (replays the last user message).
  function handleRetryMessage() {
    if (!activeChat || activeChat.legacy || runtimeRef.current) return;
    handleSend({ retry: true });
  }

  function handleCreateChat() {
    model.createChat(projectId).then(function (chat) {
      setChats(function (prev) { return [chat].concat(prev); });
      setActiveChatId(chat.id);
      setActiveChat(chat);
    }).catch(function (err) { setError(wbcErrorText(err)); });
  }

  function handleRename(title) {
    if (!activeChat) return Promise.resolve();
    return model.renameChat(activeChat.id, title).then(function (chat) {
      setActiveChat(function (prev) { return prev ? { ...prev, title: chat.title } : prev; });
      setChats(function (prev) {
        return prev.map(function (item) { return item.id === chat.id ? { ...item, title: chat.title } : item; });
      });
    });
  }

  function handleDelete() {
    if (!activeChat) return;
    if (!window.confirm(wbcT("workbenchChat.confirmDelete", "Delete this chat? Its messages cannot be recovered."))) return;
    var doomedId = activeChat.id;
    model.deleteChat(doomedId).then(function () {
      setActiveChat(null);
      setChats(function (prev) {
        var next = prev.filter(function (item) { return item.id !== doomedId; });
        setActiveChatId(next[0] ? next[0].id : "");
        return next;
      });
    }).catch(function (err) { setError(wbcErrorText(err)); });
  }

  function handleToTask() {
    if (!activeChat) return;
    model.toTask(activeChat.id).then(function (payload) {
      if (onOpenTask) onOpenTask(payload);
    }).catch(function (err) { setError(wbcErrorText(err)); });
  }

  var running = !!runtime;

  return (
    <div className="wbc-page">
      <WbcRail
        chats={chats}
        activeChatId={activeChatId}
        loading={loading}
        runningChatId={runtime ? runtime.chatId : ""}
        onSelect={function (id) { if (!running || (runtime && runtime.chatId === id)) setActiveChatId(id); }}
        onCreate={handleCreateChat}
      />
      <WbcMain
        project={project}
        chat={activeChat}
        runtime={runtime}
        error={error}
        onRetry={retryLoad}
        running={running}
        onSend={handleSend}
        onInterrupt={handleInterrupt}
        onRetryMessage={handleRetryMessage}
        onRename={handleRename}
        onDelete={handleDelete}
        onToTask={handleToTask}
        onOpenFile={openViewer}
      />
      <WbcSide
        project={project}
        chat={activeChat}
        runtime={runtime}
        tab={sideTab}
        onTabChange={setSideTab}
        viewerFile={viewerFile}
        onOpenFile={openViewer}
        onRename={handleRename}
        onDelete={handleDelete}
        onToTask={handleToTask}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Conversation rail (column 2)
// ---------------------------------------------------------------------------

function WbcRail({ chats, activeChatId, loading, runningChatId, onSelect, onCreate }) {
  var [query, setQuery] = useWbcState("");
  var filtered = useWbcMemo(function () {
    var q = query.trim().toLowerCase();
    if (!q) return chats;
    return chats.filter(function (chat) {
      return String(chat.title || "").toLowerCase().indexOf(q) !== -1
        || String(chat.preview || "").toLowerCase().indexOf(q) !== -1;
    });
  }, [chats, query]);

  return (
    <aside className="wbc-rail">
      <div className="workbench-rail-head">
        <span>{wbcT("workbenchChat.railTitle", "Chats")}</span>
        <button type="button" className="workbench-add-btn" onClick={onCreate}>
          <span>{WBC_ICONS.plus}</span>
          <span>{wbcT("workbenchChat.newChat", "New chat")}</span>
        </button>
      </div>
      <div className="wbc-search">
        <span className="wbc-search-icon">{WBC_ICONS.search}</span>
        <input
          value={query}
          onChange={function (e) { setQuery(e.target.value); }}
          placeholder={wbcT("workbenchChat.search", "Search chats...")}
        />
      </div>
      {loading && <div className="workbench-muted">{wbcT("workbenchChat.loading", "Loading chats...")}</div>}
      {!loading && filtered.length === 0 && (
        <div className="workbench-muted">{query ? wbcT("workbenchChat.noMatches", "No matching chats.") : wbcT("workbenchChat.emptyRail", "No chats yet. Create one from the top right.")}</div>
      )}
      <div className="wbc-chat-list">
        {filtered.map(function (chat) {
          var active = chat.id === activeChatId;
          var chatRunning = chat.id === runningChatId || chat.status === "running";
          return (
            <button
              type="button"
              key={chat.id}
              className={"wbc-chat-card" + (active ? " active" : "")}
              onClick={function () { onSelect(chat.id); }}
            >
              <span className="wbc-chat-card-top">
                <b>{chat.title || wbcT("workbenchChat.newChat", "New chat")}</b>
                <time>{wbcFormatTime(chat.updatedAt || chat.createdAt)}</time>
              </span>
              <span className="wbc-chat-card-preview">
                {chatRunning ? <i className="wbc-running-dot" /> : null}
                {chat.preview || wbcT("workbenchChat.noMessages", "No messages yet")}
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Conversation main (column 3)
// ---------------------------------------------------------------------------

function WbcMain({ project, chat, runtime, error, onRetry, running, onSend, onInterrupt, onRetryMessage, onRename, onDelete, onToTask, onOpenFile }) {
  var scrollRef = useWbcRef(null);
  var stickRef = useWbcRef(true);
  var messages = chat && Array.isArray(chat.messages) ? chat.messages : [];
  var isLegacy = !!(chat && chat.legacy);
  var lastAssistantId = "";
  for (var mi = messages.length - 1; mi >= 0; mi--) {
    if (messages[mi].role !== "user") { lastAssistantId = String(messages[mi].id || ""); break; }
  }

  // Track whether the user is reading scrollback; only auto-stick near bottom.
  function onScroll() {
    var el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  useWbcEffect(function () {
    var el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, runtime && runtime.text, runtime && runtime.progress.length]);

  useWbcEffect(function () {
    stickRef.current = true;
    var el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat && chat.id]);

  if (!project) {
    return <main className="wbc-main"><div className="workbench-empty">{wbcT("workbenchChat.noProject", "Select a project first.")}</div></main>;
  }

  return (
    <main className="wbc-main">
      {chat ? (
        <WbcHeader
          project={project}
          chat={chat}
          running={running}
          onRename={onRename}
          onDelete={onDelete}
          onToTask={onToTask}
        />
      ) : (
        <div className="wbc-header">
          <div className="wbc-header-info">
            <h1>{wbcT("workbenchChat.newChat", "New chat")}</h1>
            <div className="wbc-header-meta"><span>{project.name}</span></div>
          </div>
        </div>
      )}
      {error && <WbcErrorNotice message={error} onRetry={onRetry} />}
      <div className="wbc-thread" ref={scrollRef} onScroll={onScroll}>
        {messages.length === 0 && !runtime && (
          <div className="wbc-empty-thread">
            <div className="wbc-empty-icon">{WBC_ICONS.chat}</div>
            <b>{wbcT("workbenchChat.emptyTitle", "Start a new chat")}</b>
            <p>{wbcT("workbenchChat.emptyBody", "Chats are bound to the current workspace. The agent can read project context, and work can be converted into a task when needed.")}</p>
          </div>
        )}
        {messages.map(function (msg) {
          var canRetry = !isLegacy && !running && String(msg.id || "") === lastAssistantId;
          return msg.role === "user"
            ? <WbcUserMessage key={msg.id} msg={msg} onOpenFile={onOpenFile} />
            : <WbcAssistantMessage key={msg.id} msg={msg} onOpenFile={onOpenFile} onRetryMessage={canRetry ? onRetryMessage : null} />;
        })}
        {runtime && <WbcLiveMessage runtime={runtime} />}
      </div>
      <WbcComposer
        chat={chat}
        project={project}
        running={running}
        onSend={onSend}
        onInterrupt={onInterrupt}
      />
    </main>
  );
}

function WbcErrorNotice({ message, onRetry }) {
  var title = wbcT("workbenchChat.error.title", "Could not load this chat");
  var detail = String(message || "").trim() || wbcT("workbenchChat.error.loadFailed", "Load failed");
  var generic = wbcT("workbenchChat.error.loadFailed", "Load failed");
  var body = detail === generic
    ? wbcT("workbenchChat.error.body", "The conversation data did not load. Check the local service and try again.")
    : detail;
  return (
    <div className="workbench-error wbc-error-card" role="alert">
      <span className="wbc-error-icon">{WBC_ICONS.alert}</span>
      <span className="wbc-error-copy">
        <b>{title}</b>
        <small>{body}</small>
      </span>
      {onRetry && (
        <button type="button" className="wbc-error-retry" onClick={onRetry}>
          {wbcT("workbenchChat.error.retry", "Retry")}
        </button>
      )}
    </div>
  );
}

function WbcHeader({ project, chat, running, onRename, onDelete, onToTask }) {
  var [editing, setEditing] = useWbcState(false);
  var [draft, setDraft] = useWbcState(chat.title || "");
  var [menuOpen, setMenuOpen] = useWbcState(false);
  var inputRef = useWbcRef(null);

  useWbcEffect(function () {
    setDraft(chat.title || "");
    setEditing(false);
    setMenuOpen(false);
  }, [chat.id]);

  useWbcEffect(function () {
    if (editing && inputRef.current) { inputRef.current.focus(); inputRef.current.select(); }
  }, [editing]);

  function commitTitle() {
    var next = String(draft || "").trim();
    setEditing(false);
    if (!next || next === chat.title) { setDraft(chat.title || ""); return; }
    onRename(next).catch(function (err) {
      window.alert(err.message || String(err));
      setDraft(chat.title || "");
    });
  }

  var isLegacy = !!chat.legacy;
  var statusText = isLegacy
    ? wbcT("workbenchChat.status.archived", "Archived")
    : running ? wbcT("workbenchChat.status.replying", "Replying") : wbcT("workbenchChat.status.idle", "Idle");

  return (
    <div className="wbc-header">
      <div className="wbc-header-info">
        <div className="wbc-header-title">
          {editing ? (
            <input
              ref={inputRef}
              className="wbc-title-input"
              value={draft}
              onChange={function (e) { setDraft(e.target.value); }}
              onBlur={commitTitle}
              onKeyDown={function (e) {
                if (e.key === "Enter") commitTitle();
                if (e.key === "Escape") { setDraft(chat.title || ""); setEditing(false); }
              }}
              aria-label={wbcT("workbenchChat.titleLabel", "Chat title")}
            />
          ) : (
            <h1 title={chat.title}>{chat.title || wbcT("workbenchChat.newChat", "New chat")}</h1>
          )}
          {!editing && !isLegacy && (
            <button type="button" className="wbc-icon-btn" title="重命名" onClick={function () { setEditing(true); }}>
              {WBC_ICONS.edit}
            </button>
          )}
        </div>
        <div className="wbc-header-meta">
          <span className={"wbc-status-chip" + (running ? " running" : "")}>{statusText}</span>
          <span>{chat.model || "—"}</span>
          <span>{project.name}</span>
        </div>
      </div>
      <div className="wbc-header-actions">
        {!isLegacy && (
          <button type="button" className="wb-btn primary wbc-totask" disabled={running} onClick={onToTask} title={wbcT("workbenchChat.toTaskTitle", "Create a task from this chat")}>
            {WBC_ICONS.play}<span>{wbcT("workbenchChat.toTask", "Convert to task")}</span>
          </button>
        )}
        {!isLegacy && (
          <div className="wbc-menu-wrap">
            <button type="button" className="wbc-icon-btn" title="更多" onClick={function () { setMenuOpen(!menuOpen); }}>
              {WBC_ICONS.dots}
            </button>
            {menuOpen && (
              <>
                <div className="wbc-menu-scrim" onClick={function () { setMenuOpen(false); }}></div>
                <div className="wbc-menu">
                  <button type="button" onClick={function () { setMenuOpen(false); setEditing(true); }}>{wbcT("workbenchChat.rename", "Rename chat")}</button>
                  <button type="button" onClick={function () { setMenuOpen(false); onToTask(); }}>{wbcT("workbenchChat.toTask", "Convert to task")}</button>
                  <button type="button" className="danger" onClick={function () { setMenuOpen(false); onDelete(); }}>{wbcT("workbenchChat.delete", "Delete chat")}</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function WbcUserMessage({ msg, onOpenFile }) {
  var attachments = Array.isArray(msg.attachments) ? msg.attachments : [];
  return (
    <div className="wbc-msg user">
      <div className="wbc-msg-row">
        <time>{wbcFormatTime(msg.createdAt)}</time>
        <div className="wbc-bubble">
          {attachments.length > 0 && (
            <div className="wbc-msg-attachments">
              {attachments.map(function (file, i) {
                var isImg = file.kind === "image" || String(file.content_type || "").indexOf("image") === 0;
                var open = function () { if (onOpenFile && file.url) onOpenFile(file); };
                return isImg && file.url
                  ? <img key={file.id || i} src={file.url} alt={file.name || "image"} onClick={open} style={{ cursor: "zoom-in" }} />
                  : <button type="button" key={file.id || i} className="wbc-attach-chip" onClick={open} title="在右侧查看">{WBC_ICONS.file}{file.name || "file"}</button>;
              })}
            </div>
          )}
          {msg.content ? <p>{msg.content}</p> : null}
        </div>
      </div>
    </div>
  );
}

// Files the agent produced in this reply — rendered like the reference's
// artifact card, with a 查看 action that opens the side viewer.
function WbcAgentFiles({ files, onOpenFile }) {
  if (!files || !files.length) return null;
  return (
    <div className="wbc-agent-files">
      {files.map(function (file, i) {
        return (
          <div className="wbc-agent-file" key={file.id || file.url || i}>
            <span className="wbc-file-icon">{WBC_ICONS.file}</span>
            <span className="wbc-file-meta">
              <b title={file.name}>{file.name || "file"}</b>
              <small>{file.content_type || ""}</small>
            </span>
            <span className="wbc-agent-file-actions">
              <button type="button" className="wb-btn ghost" onClick={function () { onOpenFile && onOpenFile(file); }}>查看</button>
              {file.url ? <a className="wb-btn ghost" href={file.url} target="_blank" rel="noreferrer" title="新窗口打开">↗</a> : null}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function WbcTraceCard({ trace, live, label }) {
  var entries = Array.isArray(trace) ? trace : [];
  if (!entries.length && !live) return null;
  return (
    <div className={"wbc-trace" + (live ? " live" : "")}>
      <div className="wbc-trace-head">
        {live ? <span className="wb-spinner" /> : <span className="wbc-trace-icon">{WBC_ICONS.tool}</span>}
        <b>{label || (live ? "正在处理..." : "执行过程（" + entries.length + " 个工具调用）")}</b>
      </div>
      {entries.length > 0 && (
        <ul className="wbc-trace-list">
          {entries.map(function (entry, i) {
            var isLast = live && i === entries.length - 1;
            return (
              <li key={i} className={isLast ? "active" : "done"}>
                <span className="wbc-trace-mark">{isLast ? <span className="wb-spinner small" /> : WBC_ICONS.check}</span>
                <span className="wbc-trace-text">
                  {entry.tool || entry.text}
                  {(entry.preview) ? <small>（{entry.preview}）</small> : null}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function WbcAssistantMessage({ msg, onOpenFile, onRetryMessage }) {
  var [copied, setCopied] = useWbcState(false);
  function copyText() {
    try {
      navigator.clipboard.writeText(String(msg.content || "")).then(function () {
        setCopied(true);
        setTimeout(function () { setCopied(false); }, 1600);
      });
    } catch (e) {}
  }
  return (
    <div className="wbc-msg assistant">
      {msg.trace && msg.trace.length > 0 && <WbcTraceCard trace={msg.trace} />}
      <div className="wbc-msg-body markdown" dangerouslySetInnerHTML={{ __html: wbcRenderMarkdown(msg.content) }} />
      <WbcAgentFiles files={msg.attachments} onOpenFile={onOpenFile} />
      <div className="wbc-msg-foot">
        <button type="button" className="wbc-msg-action" onClick={copyText} title={wbcT("workbenchChat.copy", "Copy")}>
          {copied ? WBC_ICONS.check : WBC_ICONS.copy}
        </button>
        {onRetryMessage && (
          <button type="button" className="wbc-msg-action" onClick={onRetryMessage} title={wbcT("workbenchChat.regenerate", "Regenerate")}>
            {WBC_ICONS.retry}
          </button>
        )}
        <time>{wbcFormatTime(msg.createdAt)}</time>
        {msg.usage && msg.usage.total_tokens ? <small>{wbcCompactNumber(msg.usage.total_tokens)} tokens</small> : null}
      </div>
    </div>
  );
}

function WbcLiveMessage({ runtime }) {
  var progressEntries = runtime.progress.map(function (entry) {
    return { tool: entry.text, preview: entry.preview };
  });
  return (
    <div className="wbc-msg assistant">
      {(progressEntries.length > 0 || !runtime.text) && (
        <WbcTraceCard
          trace={progressEntries}
          live={true}
          label={runtime.text ? "执行过程" : (progressEntries.length ? "正在调用工具..." : "正在思考...")}
        />
      )}
      {runtime.text && (
        <div className="wbc-msg-body markdown">
          <div dangerouslySetInnerHTML={{ __html: wbcRenderMarkdown(runtime.text) }} />
          <span className="wbc-caret" />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------

function WbcComposer({ chat, project, running, onSend, onInterrupt }) {
  var model = window.WorkbenchChatModel;
  var [draft, setDraft] = useWbcState("");
  var [attachments, setAttachments] = useWbcState([]);
  var [mode, setMode] = useWbcState("auto");
  var [command, setCommand] = useWbcState("");
  var [uploading, setUploading] = useWbcState(false);
  var [slashOpen, setSlashOpen] = useWbcState(false);
  var [modeOpen, setModeOpen] = useWbcState(false);
  var [contextState, setContextState] = useWbcState(null);
  var taRef = useWbcRef(null);
  var fileRef = useWbcRef(null);
  var chatId = chat ? chat.id : "";

  useWbcEffect(function () {
    setAttachments([]);
    setCommand("");
    setSlashOpen(false);
    setModeOpen(false);
      }, [chatId]);

  useWbcEffect(function () {
    var cancelled = false;
    fetch("/api/context/state").then(function (r) { return r.json(); }).then(function (s) {
      if (!cancelled) setContextState(s);
    }).catch(function () {});
    return function () { cancelled = true; };
  }, []);

  function syncHeight() {
    var ta = taRef.current;
    if (ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 180) + "px"; }
  }

  function submit() {
    if (running) { onInterrupt(); return; }
    var text = draft.trim();
    if (!text && attachments.length === 0) return;
    setDraft("");
    if (taRef.current) taRef.current.style.height = "";
    var payload = { message: text, attachments: attachments, mode: mode, command: command };
    setAttachments([]);
    setCommand("");
    onSend(payload);
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
      if (event.nativeEvent && event.nativeEvent.isComposing) return; // IME guard
      event.preventDefault();
      submit();
    } else if (event.key === "Escape") {
      setSlashOpen(false);
      setModeOpen(false);
    }
  }

  function pickFiles() { if (fileRef.current) fileRef.current.click(); }
  function onFilePick(event) {
    var files = event.target.files;
    if (!files || !files.length) return;
    setUploading(true);
    model.uploadFiles(files)
      .then(function (uploaded) { setAttachments(function (prev) { return prev.concat(uploaded); }); })
      .catch(function (err) { window.alert(wbcT("workbenchChat.uploadFailed", "Upload failed: {error}", { error: wbcErrorText(err) })); })
      .finally(function () { setUploading(false); if (fileRef.current) fileRef.current.value = ""; });
  }

  var slashQuery = draft.indexOf("/") === 0 ? draft.slice(1).toLowerCase() : "";
  var slashItems = WBC_COMMANDS.filter(function (c) {
    return !slashQuery || c.id.indexOf(slashQuery) !== -1 || c.label.toLowerCase().indexOf(slashQuery) !== -1;
  });
  var showSlash = (slashOpen || (draft.indexOf("/") === 0 && draft.indexOf(" ") === -1)) && slashItems.length > 0 && !running;
  var activeCommand = command ? wbcCommandMeta(command) : null;
  var currentMode = wbcModeMeta(mode);
  var workspaceTail = (project && project.workspacePath || "").split("/").filter(Boolean).pop() || "workspace";
  var soulOn = !contextState || contextState.soul_active !== false;
  var modelName = (chat && chat.model) || (project && project.model) || "";
  var sendDisabled = running ? false : (!draft.trim() && attachments.length === 0);
  var isLegacy = !!(chat && chat.legacy);

  if (isLegacy) {
    return (
      <div className="wbc-composer">
        <div className="wbc-composer-box wbc-composer-readonly">
          {wbcT("workbenchChat.legacyReadonly", "This is an archived legacy session — read-only. Start a new chat to continue the topic.")}
        </div>
      </div>
    );
  }

  return (
    <div className="wbc-composer">
      {activeCommand && (
        <div className="wbc-command-row">
          <span className="wbc-command-chip">
            {WBC_ICONS.slash}
            {activeCommand.label}
            <button type="button" onClick={function () { setCommand(""); }} aria-label={wbcT("workbenchChat.removeCommand", "Remove command")}>{WBC_ICONS.x}</button>
          </span>
        </div>
      )}
      <div className="wbc-composer-box">
        {attachments.length > 0 && (
          <div className="wbc-attach-row">
            {attachments.map(function (file, i) {
              var isImg = file.kind === "image" || String(file.content_type || "").indexOf("image") === 0;
              return (
                <div className={"wbc-attach-card" + (isImg ? " image" : "")} key={file.id || i}>
                  {isImg && file.url
                    ? <img src={file.url} alt={file.name || "image"} />
                    : <span className="wbc-attach-name" title={file.name}>{file.name || "file"}</span>}
                  <button type="button" className="wbc-attach-x" onClick={function () {
                    setAttachments(attachments.filter(function (_f, idx) { return idx !== i; }));
                  }} aria-label={wbcT("workbenchChat.removeAttachment", "Remove attachment")}>{WBC_ICONS.x}</button>
                </div>
              );
            })}
          </div>
        )}
        <textarea
          ref={taRef}
          value={draft}
          rows={2}
          disabled={running}
          onChange={function (e) { setDraft(e.target.value); syncHeight(); }}
          onKeyDown={onKeyDown}
          placeholder={running ? wbcT("workbenchChat.placeholderRunning", "The agent is replying. Click stop to interrupt...") : wbcT("workbenchChat.placeholder", "Message Cyrene... (Enter to send, Shift+Enter for a new line)")}
        />
        <div className="wbc-context-chips">
          <span className="wbc-ctx-chip" title={soulOn ? wbcT("workbenchChat.personaOnTitle", "Persona context is on") : wbcT("workbenchChat.personaOffTitle", "Persona context is off")}>
            {WBC_ICONS.spark}<span>{soulOn ? wbcT("workbenchChat.persona", "Persona") : wbcT("workbenchChat.personaOff", "Persona: off")}</span>
          </span>
          <span className="wbc-ctx-chip" title={project && project.workspacePath}>
            {WBC_ICONS.folder}<span>{wbcT("workbenchChat.workspaceChip", "Workspace: {name}", { name: workspaceTail })}</span>
          </span>
        </div>
        <div className="wbc-composer-actions">
          <input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={onFilePick} />
          <button type="button" className="wbc-composer-icon" title={uploading ? wbcT("workbenchChat.uploading", "Uploading...") : wbcT("workbenchChat.addAttachment", "Add attachment")} disabled={uploading || running} onClick={pickFiles}>
            {uploading ? <span className="wb-spinner small" /> : WBC_ICONS.attach}
          </button>
          <span className="wbc-pop-anchor">
            <button type="button" className={"wbc-composer-icon" + (showSlash || command ? " active" : "")} title="斜杠命令" disabled={running} onClick={function () { setSlashOpen(!slashOpen); setModeOpen(false); }}>
              {WBC_ICONS.slash}
            </button>
            {showSlash && (
              <div className="wbc-popmenu">
                <div className="wbc-popmenu-head">{wbcT("workbenchChat.commands", "Commands")}</div>
                {slashItems.map(function (c) {
                  var on = command === c.id;
                  return (
                    <button key={c.id} type="button" className={on ? "active" : ""} onClick={function () {
                      setCommand(on ? "" : c.id);
                      setSlashOpen(false);
                      if (draft.indexOf("/") === 0) setDraft("");
                      if (taRef.current) taRef.current.focus();
                    }}>
                      <span className="wbc-popmenu-label">{c.label}</span>
                      <span className="wbc-popmenu-desc">{c.desc}</span>
                      {on ? <span className="wbc-popmenu-check">{WBC_ICONS.check}</span> : null}
                    </button>
                  );
                })}
              </div>
            )}
          </span>
          <span className="wbc-pop-anchor">
            <button type="button" className={"wbc-composer-icon mode" + (modeOpen ? " active" : "")} title={wbcT("workbenchChat.permissionMode", "Permission mode")} onClick={function () { setModeOpen(!modeOpen); setSlashOpen(false); }}>
              {WBC_ICONS.bolt}
              <span>{currentMode.label}</span>
            </button>
            {modeOpen && (
              <div className="wbc-popmenu">
                <div className="wbc-popmenu-head">{wbcT("workbenchChat.permissionMode", "Permission mode")}</div>
                {WBC_MODES.map(function (m) {
                  var on = mode === m.id;
                  return (
                    <button key={m.id} type="button" className={on ? "active" : ""} onClick={function () { setMode(m.id); setModeOpen(false); }}>
                      <span className="wbc-popmenu-label">{m.label}</span>
                      <span className="wbc-popmenu-desc">{m.desc}</span>
                      {on ? <span className="wbc-popmenu-check">{WBC_ICONS.check}</span> : null}
                    </button>
                  );
                })}
              </div>
            )}
          </span>
          <span className="wbc-composer-spacer" />
          {modelName ? <span className="wbc-model-label" title={wbcT("workbenchChat.currentModel", "Current model")}>{modelName}</span> : null}
          <button
            type="button"
            className={"wbc-send" + (running ? " stop" : "")}
            onClick={submit}
            disabled={sendDisabled}
            title={running ? wbcT("workbenchChat.stop", "Stop") : wbcT("workbenchChat.send", "Send")}
          >
            {running ? WBC_ICONS.stop : WBC_ICONS.send}
            <span>{running ? wbcT("workbenchChat.stop", "Stop") : wbcT("workbenchChat.send", "Send")}</span>
          </button>
        </div>
      </div>
      <div className="wb-composer-disclaimer">{wbcT("workbench.composerDisclaimer", "Cyrene is AI and can make mistakes. Please verify responses.")}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right context panel (column 4)
// ---------------------------------------------------------------------------

function WbcSide({ project, chat, runtime, tab, onTabChange, viewerFile, onOpenFile, onRename, onDelete, onToTask }) {
  var hasMap = wbcChatUsedMap(chat, runtime);
  var tabs = [
    { id: "overview", label: "概览" },
    { id: "context", label: "上下文" },
    { id: "artifacts", label: "产物" },
  ];
  if (viewerFile) tabs.push({ id: "viewer", label: "查看器" });
  if (hasMap) tabs.push({ id: "map", label: "地图" });
  var activeTab = tabs.some(function (item) { return item.id === tab; }) ? tab : "overview";
  var flush = activeTab === "viewer" || activeTab === "map";
  return (
    <aside className="wbc-side">
      <div className="workbench-right-tabs">
        {tabs.map(function (item) {
          return (
            <button key={item.id} type="button" className={activeTab === item.id ? "active" : ""} onClick={function () { onTabChange(item.id); }}>
              {item.label}
            </button>
          );
        })}
      </div>
      <div className={"wbc-side-body" + (flush ? " flush" : "")}>
        {activeTab === "overview" && <WbcOverviewTab chat={chat} onRename={onRename} onDelete={onDelete} onToTask={onToTask} />}
        {activeTab === "context" && <WbcContextTab project={project} chat={chat} />}
        {activeTab === "artifacts" && <WbcArtifactsTab chat={chat} onOpenFile={onOpenFile} />}
        {activeTab === "viewer" && <WbcViewerTab file={viewerFile} />}
        {activeTab === "map" && <WbcMapTab chatId={chat ? chat.id : ""} active={true} />}
      </div>
    </aside>
  );
}

// ---- side viewer (PDF / HTML / Markdown / 代码 / 图片) ----------------------

function WbcViewerTab({ file }) {
  var kind = wbcFileViewKind(file);
  var [text, setText] = useWbcState("");
  var [blobUrl, setBlobUrl] = useWbcState("");
  var [htmlMode, setHtmlMode] = useWbcState("rendered");
  var [zoom, setZoom] = useWbcState(1);
  var [failed, setFailed] = useWbcState(false);
  var codeRef = useWbcRef(null);
  var url = file && file.url;

  // text-ish contents are fetched; pdf goes through a blob URL (same as the
  // legacy viewer — <embed src=...> with a route URL re-downloads on zoom).
  useWbcEffect(function () {
    setText("");
    setFailed(false);
    setHtmlMode("rendered");
    setZoom(1);
    if (blobUrl) { try { URL.revokeObjectURL(blobUrl); } catch (e) {} }
    setBlobUrl("");
    if (!url) return;
    var cancelled = false;
    if (kind === "html" || kind === "markdown" || kind === "code") {
      fetch(url).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      }).then(function (body) {
        if (!cancelled) setText(body);
      }).catch(function () { if (!cancelled) setFailed(true); });
    } else if (kind === "pdf") {
      fetch(url).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      }).then(function (blob) {
        if (cancelled) return;
        setBlobUrl(URL.createObjectURL(blob));
      }).catch(function () { if (!cancelled) setFailed(true); });
    }
    return function () { cancelled = true; };
  }, [url, kind]);

  useWbcEffect(function () {
    return function () { if (blobUrl) { try { URL.revokeObjectURL(blobUrl); } catch (e) {} } };
  }, [blobUrl]);

  // syntax highlight code once loaded
  useWbcEffect(function () {
    if (kind === "code" && text && codeRef.current && window.hljs) {
      try { window.hljs.highlightElement(codeRef.current); } catch (e) {}
    }
  }, [text, kind]);

  if (!file) return <p className="workbench-muted">从消息附件或产物列表选择一个文件。</p>;

  var head = (
    <div className="wbc-viewer-head">
      <span className="wbc-viewer-name" title={file.name}>{file.name || "file"}</span>
      {kind === "html" && (
        <span className="wbc-viewer-switch">
          <button type="button" className={htmlMode === "rendered" ? "active" : ""} onClick={function () { setHtmlMode("rendered"); }}>渲染</button>
          <button type="button" className={htmlMode === "source" ? "active" : ""} onClick={function () { setHtmlMode("source"); }}>源码</button>
        </span>
      )}
      {kind === "pdf" && (
        <span className="wbc-viewer-switch">
          <button type="button" onClick={function () { setZoom(function (z) { return Math.max(0.4, z - 0.2); }); }}>−</button>
          <button type="button" onClick={function () { setZoom(1); }}>{Math.round(zoom * 100) + "%"}</button>
          <button type="button" onClick={function () { setZoom(function (z) { return Math.min(3, z + 0.2); }); }}>+</button>
        </span>
      )}
      {url ? <a className="wbc-viewer-open" href={url} target="_blank" rel="noreferrer" title="新窗口打开">↗</a> : null}
    </div>
  );

  var body = null;
  if (failed) {
    body = <p className="workbench-muted wbc-viewer-pad">文件加载失败。{url ? "可尝试新窗口打开。" : ""}</p>;
  } else if (kind === "image") {
    body = <div className="wbc-viewer-scroll center"><img className="wbc-viewer-img" src={url} alt={file.name || "image"} /></div>;
  } else if (kind === "pdf") {
    body = blobUrl ? (
      <div className="wbc-viewer-scroll">
        <embed className="wbc-viewer-embed" src={blobUrl} type="application/pdf" style={{ width: (zoom * 100) + "%", height: zoom >= 1 ? (zoom * 100) + "%" : "100%" }} />
      </div>
    ) : <p className="workbench-muted wbc-viewer-pad">加载中…</p>;
  } else if (kind === "html") {
    body = htmlMode === "rendered"
      ? <iframe className="wbc-viewer-iframe" sandbox="allow-scripts" srcDoc={text} title={file.name || "HTML"} />
      : <pre className="wbc-viewer-pre">{text}</pre>;
  } else if (kind === "markdown") {
    body = <div className="wbc-viewer-md wbc-msg-body markdown" dangerouslySetInnerHTML={{ __html: wbcRenderMarkdown(text) }} />;
  } else if (kind === "code") {
    body = <pre className="wbc-viewer-pre"><code ref={codeRef}>{text}</code></pre>;
  } else {
    body = (
      <div className="wbc-viewer-pad">
        <p className="workbench-muted">暂不支持预览该类型。</p>
        {url ? <a className="wb-btn ghost" href={url} target="_blank" rel="noreferrer">下载 / 打开</a> : null}
      </div>
    );
  }

  return (
    <div className="wbc-viewer">
      {head}
      {body}
    </div>
  );
}

// ---- side map (pin_location / connect_pins 结果) ----------------------------

// WGS-84 → GCJ-02 (火星坐标) — AMap tiles use GCJ-02, so raw WGS pins must be
// shifted or they land ~500m off. Same math as the legacy map view.
function wbcWgs84ToGcj02(wgsLat, wgsLng) {
  if (wgsLng < 72.004 || wgsLng > 137.8347 || wgsLat < 0.8293 || wgsLat > 55.8271) return [wgsLat, wgsLng];
  var pi = 3.1415926535897932384626, a = 6378245.0, ee = 0.00669342162296594323;
  function tLat(x, y) {
    var r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * pi) + 20.0 * Math.sin(2.0 * x * pi)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * pi) + 40.0 * Math.sin(y / 3.0 * pi)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * pi) + 320.0 * Math.sin(y * pi / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function tLng(x, y) {
    var r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * pi) + 20.0 * Math.sin(2.0 * x * pi)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * pi) + 40.0 * Math.sin(x / 3.0 * pi)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * pi) + 300.0 * Math.sin(x / 30.0 * pi)) * 2.0 / 3.0;
    return r;
  }
  var dlat = tLat(wgsLng - 105.0, wgsLat - 35.0);
  var dlng = tLng(wgsLng - 105.0, wgsLat - 35.0);
  var radlat = wgsLat / 180.0 * pi;
  var magic = Math.sin(radlat);
  magic = 1 - ee * magic * magic;
  var sqrtmagic = Math.sqrt(magic);
  dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi);
  dlng = (dlng * 180.0) / (a / sqrtmagic * Math.cos(radlat) * pi);
  return [wgsLat + dlat, wgsLng + dlng];
}

// Same provider setting as the legacy map ("direct" = CARTO, "amap" = 高德).
function wbcMapProvider() {
  try { return localStorage.getItem("cyrene-tweak-map-provider") || "direct"; } catch (e) { return "direct"; }
}

function wbcTileConfig(provider) {
  var isDark = document.documentElement.dataset.theme === "dark";
  if (provider === "amap") {
    return {
      url: "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=" + (isDark ? 8 : 7) + "&x={x}&y={y}&z={z}",
      options: {},
    };
  }
  return {
    url: "https://{s}.basemaps.cartocdn.com/" + (isDark ? "dark_all" : "light_all") + "/{z}/{x}/{y}{r}.png",
    options: { subdomains: "abcd" },
  };
}

function WbcMapTab({ chatId }) {
  var holderRef = useWbcRef(null);
  var mapRef = useWbcRef(null);
  var layerRef = useWbcRef(null);
  var tileRef = useWbcRef(null);
  var switchedRef = useWbcRef(false);
  var [provider, setProvider] = useWbcState(wbcMapProvider());
  var [data, setData] = useWbcState(null);

  useWbcEffect(function () {
    if (!window.L || !holderRef.current || mapRef.current) return;
    var L = window.L;
    var map = L.map(holderRef.current, { zoomControl: true, attributionControl: false }).setView([35, 105], 4);
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);
    setTimeout(function () { try { map.invalidateSize(); } catch (e) {} }, 100);
    return function () {
      try { map.remove(); } catch (e) {}
      mapRef.current = null;
      layerRef.current = null;
      tileRef.current = null;
    };
  }, []);

  // (Re)mount the tile layer per provider; on repeated tile failures fall back
  // to the other provider once (e.g. CARTO unreachable → 高德, and vice versa).
  useWbcEffect(function () {
    var map = mapRef.current;
    if (!map || !window.L) return;
    var L = window.L;
    if (tileRef.current) { try { map.removeLayer(tileRef.current); } catch (e) {} }
    var config = wbcTileConfig(provider);
    var errors = 0;
    var tiles = L.tileLayer(config.url, config.options);
    tiles.on("tileerror", function () {
      errors += 1;
      if (errors >= 3 && !switchedRef.current) {
        switchedRef.current = true;
        setProvider(provider === "amap" ? "direct" : "amap");
      }
    });
    tiles.addTo(map);
    tileRef.current = tiles;
  }, [provider]);

  useWbcEffect(function () {
    if (!chatId) return;
    var cancelled = false;
    fetch("/api/map/pins?session_id=" + encodeURIComponent(chatId))
      .then(function (r) { return r.json(); })
      .then(function (payload) { if (!cancelled) setData(payload || {}); })
      .catch(function () { if (!cancelled) setData({}); });
    return function () { cancelled = true; };
  }, [chatId]);

  // Render pins + routes; AMap needs GCJ-02 coordinates.
  useWbcEffect(function () {
    var layer = layerRef.current;
    if (!layer || !window.L || !data) return;
    var L = window.L;
    layer.clearLayers();
    var pins = Array.isArray(data.pins) ? data.pins : [];
    var routes = Array.isArray(data.routes) ? data.routes : [];
    var convert = provider === "amap"
      ? function (lat, lng) { return wbcWgs84ToGcj02(lat, lng); }
      : function (lat, lng) { return [lat, lng]; };
    var byName = {};
    var latlngs = [];
    pins.forEach(function (pin) {
      var lat = Number(pin.lat), lng = Number(pin.lng);
      if (!isFinite(lat) || !isFinite(lng)) return;
      var pos = convert(lat, lng);
      byName[String(pin.name || "")] = pos;
      latlngs.push(pos);
      var marker = L.marker(pos).addTo(layer);
      var note = String(pin.note || "").trim();
      marker.bindPopup("<b>" + String(pin.name || "").replace(/</g, "&lt;") + "</b>" + (note ? "<br/>" + note.replace(/</g, "&lt;") : ""));
    });
    routes.forEach(function (route) {
      var from = byName[String(route.from_name || route.from || "")];
      var to = byName[String(route.to_name || route.to || "")];
      if (!from || !to) return;
      var line = L.polyline([from, to], { color: "#1f9d57", weight: 3, opacity: 0.8, dashArray: "6 6" }).addTo(layer);
      var label = [route.transport, route.route_note].filter(Boolean).join(" · ");
      if (label) line.bindPopup(String(label).replace(/</g, "&lt;"));
    });
    if (latlngs.length && mapRef.current) {
      try { mapRef.current.fitBounds(latlngs, { padding: [28, 28], maxZoom: 12 }); } catch (e) {}
    }
  }, [data, provider]);

  var empty = data && (!Array.isArray(data.pins) || data.pins.length === 0);

  return (
    <div className="wbc-map">
      <div className="wbc-map-holder" ref={holderRef}></div>
      {empty && <div className="wbc-map-empty">{wbcT("workbenchChat.mapEmpty", "No map pins in this chat yet.")}</div>}
    </div>
  );
}

function WbcUsageRing({ usage }) {
  usage = usage || {};
  var hit = Number(usage.prompt_cache_hit_tokens || 0);
  var miss = Number(usage.prompt_cache_miss_tokens || 0);
  var prompt = Number(usage.prompt_tokens || 0);
  var completion = Number(usage.completion_tokens || 0);
  var total = Number(usage.total_tokens || 0) || (prompt + completion);
  var cacheTotal = hit + miss;
  var ratio = cacheTotal > 0 ? hit / cacheTotal : 0;
  var label = cacheTotal > 0 ? Math.round(ratio * 100) + "%" : (total ? wbcCompactNumber(total) : "—");
  var sub = cacheTotal > 0 ? "缓存命中率" : "Token 合计";
  var r = 40, c = 2 * Math.PI * r;
  var dashOffset = c * (1 - (cacheTotal > 0 ? ratio : (total ? 1 : 0)));
  return (
    <div className="wbc-ring-wrap">
      <div className="wbc-ring">
        <svg width="96" height="96" viewBox="0 0 96 96">
          <circle cx="48" cy="48" r={r} fill="none" stroke="var(--wb-line)" strokeWidth="7" />
          <circle cx="48" cy="48" r={r} fill="none" stroke="var(--wb-green)" strokeWidth="7"
            strokeDasharray={c} strokeDashoffset={dashOffset}
            transform="rotate(-90 48 48)" strokeLinecap="round" />
        </svg>
        <div className="wbc-ring-label">
          <b>{label}</b>
          <small>{sub}</small>
        </div>
      </div>
      <div className="wbc-ring-meta">
        <div><span className="wbc-dot in" />输入<b>{prompt ? wbcCompactNumber(prompt) : "—"}</b></div>
        <div><span className="wbc-dot out" />输出<b>{completion ? wbcCompactNumber(completion) : "—"}</b></div>
        <div><span className="wbc-dot total" />合计<b>{total ? wbcCompactNumber(total) : "—"}</b></div>
      </div>
    </div>
  );
}

function WbcModelUsage() {
  var dash = (typeof DATA !== "undefined" && DATA.dashboard) || {};
  var rawStats = Array.isArray(dash.model_stats) ? dash.model_stats : [];
  var modelMap = {};
  rawStats.forEach(function (row) {
    if (!modelMap[row.model]) modelMap[row.model] = 0;
    modelMap[row.model] += row.requests || 0;
  });
  var entries = Object.keys(modelMap)
    .map(function (m) { return { model: m, requests: modelMap[m] }; })
    .sort(function (a, b) { return b.requests - a.requests; })
    .slice(0, 5);
  var totalRequests = entries.reduce(function (sum, m) { return sum + m.requests; }, 0);
  if (!entries.length) return null;
  return (
    <section className="workbench-side-section">
      <h3>模型占比</h3>
      {entries.map(function (m) {
        var pct = totalRequests ? Math.round(m.requests / totalRequests * 100) : 0;
        return (
          <div key={m.model} className="wbc-model-row">
            <span className="wbc-model-name" title={m.model}>{m.model}</span>
            <span className="wbc-model-track"><span style={{ width: pct + "%" }} /></span>
            <span className="wbc-model-pct">{pct}%</span>
          </div>
        );
      })}
    </section>
  );
}

function WbcOverviewTab({ chat, onRename, onDelete, onToTask }) {
  if (!chat) {
    return <p className="workbench-muted">选择或新建一个对话。</p>;
  }
  var usage = chat.usage || {};
  return (
    <div className="workbench-side-stack">
      <section className="workbench-side-section">
        <h3>运行概要</h3>
        <WbcUsageRing usage={usage} />
      </section>
      <section className="workbench-side-section">
        <h3>会话信息</h3>
        <div className="wb-kv"><span>状态</span><b>{chat.status === "running" ? "回复中" : "空闲"}</b></div>
        <div className="wb-kv"><span>消息数</span><b>{chat.messageCount != null ? chat.messageCount : (chat.messages || []).length}</b></div>
        <div className="wb-kv"><span>模型</span><b className="wbc-kv-mono">{chat.model || "—"}</b></div>
        <div className="wb-kv"><span>会话 ID</span><b className="wbc-kv-mono">{chat.id}</b></div>
        <div className="wb-kv"><span>创建时间</span><b>{wbcFormatTime(chat.createdAt) || "—"}</b></div>
      </section>
      <WbcModelUsage />
      <section className="workbench-side-section">
        <h3>快捷操作</h3>
        <div className="wbc-quick-actions">
          <button type="button" onClick={function () {
            var next = window.prompt("对话标题", chat.title || "");
            if (next != null) onRename(String(next).trim() || chat.title);
          }}>{WBC_ICONS.edit}<span>重命名对话</span></button>
          <button type="button" onClick={onToTask}>{WBC_ICONS.task}<span>转为任务</span></button>
          <button type="button" className="danger" onClick={onDelete}>{WBC_ICONS.trash}<span>删除对话</span></button>
        </div>
      </section>
    </div>
  );
}

function WbcContextTab({ project, chat }) {
  var [state, setState] = useWbcState(null);
  useWbcEffect(function () {
    var cancelled = false;
    fetch("/api/context/state").then(function (r) { return r.json(); }).then(function (s) {
      if (!cancelled) setState(s);
    }).catch(function () {});
    return function () { cancelled = true; };
  }, [chat && chat.id]);
  return (
    <div className="workbench-side-stack">
      <section className="workbench-side-section">
        <h3>项目上下文</h3>
        <div className="wb-kv"><span>项目</span><b>{project ? project.name : "—"}</b></div>
        <p className="workbench-muted">{(project && project.workspacePath) || "—"}</p>
        {project && project.context && project.context.summary ? <p>{project.context.summary}</p> : null}
      </section>
      <section className="workbench-side-section">
        <h3>注入上下文</h3>
        <div className="workbench-check">
          <span className={"workbench-status-dot " + (!state || state.soul_active !== false ? "green" : "muted")}></span>
          人格（SOUL.md）
        </div>
        <div className="workbench-check">
          <span className={"workbench-status-dot " + (!state || state.workspace_active !== false ? "green" : "muted")}></span>
          工作区文件
        </div>
        {state && state.workspace_dir ? <p className="workbench-muted">{state.workspace_dir}</p> : null}
      </section>
      <section className="workbench-side-section">
        <h3>对话统计</h3>
        <div className="wb-kv"><span>消息数</span><b>{chat ? (chat.messages || []).length : 0}</b></div>
        <div className="wb-kv"><span>最近更新</span><b>{chat ? (wbcFormatTime(chat.updatedAt) || "—") : "—"}</b></div>
      </section>
    </div>
  );
}

function WbcArtifactsTab({ chat, onOpenFile }) {
  var files = [];
  (chat && chat.messages || []).forEach(function (msg) {
    (msg.attachments || []).forEach(function (file) { files.push({ file: file, role: msg.role }); });
  });
  return (
    <div className="workbench-side-stack">
      <section className="workbench-side-section">
        <h3>{"文件与产物 (" + files.length + ")"}</h3>
        {files.length === 0 && <p className="workbench-muted">这个对话还没有产生文件。上传的附件和 Agent 生成的文件会出现在这里。</p>}
        {files.map(function (item, i) {
          var file = item.file;
          return (
            <div
              className="wbc-file-row clickable"
              key={(file.id || file.url || i) + "_" + i}
              onClick={function () { if (onOpenFile && file.url) onOpenFile(file); }}
              title="在查看器中打开"
            >
              <span className="wbc-file-icon">{WBC_ICONS.file}</span>
              <span className="wbc-file-meta">
                <b>{file.name || "file"}</b>
                <small>{item.role === "user" ? "用户上传" : "Agent 生成"}</small>
              </span>
              {file.url ? (
                <a
                  className="wbc-file-open"
                  href={file.url}
                  target="_blank"
                  rel="noreferrer"
                  title="新窗口打开"
                  onClick={function (e) { e.stopPropagation(); }}
                >↗</a>
              ) : null}
            </div>
          );
        })}
      </section>
    </div>
  );
}

window.WorkbenchChatPage = WorkbenchChatPage;
