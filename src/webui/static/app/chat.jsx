// Chat page — wired to /api/chat with live state and SSE updates
const { useState, useRef, useEffect } = React;
var _useLayoutEffect = React.useLayoutEffect;

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderMarkdown(text) {
  const source = String(text || "");
  if (!source) return "";
  if (window.marked && window.DOMPurify) {
    const html = window.marked.parse(source);
    return window.DOMPurify.sanitize(html, { ADD_ATTR: ["data-line", "data-language"] });
  }
  return escapeHtml(source).replace(/\n/g, "<br>");
}

function extractHtmlBlocks(text) {
  var source = String(text || "");
  var parts = [];
  var regex = /```html\s*\n([\s\S]*?)```/g;
  var lastIndex = 0;
  var match;
  while ((match = regex.exec(source)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: "markdown", content: source.slice(lastIndex, match.index) });
    }
    parts.push({ type: "html", content: match[1].trim() });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < source.length) {
    parts.push({ type: "markdown", content: source.slice(lastIndex) });
  }
  return {
    hasBlocks: parts.some(function (p) { return p.type === "html"; }),
    parts: parts.length > 0 ? parts : [{ type: "markdown", content: source }]
  };
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function injectAttachmentLinks(text, attachments) {
  var source = String(text || "");
  var files = Array.isArray(attachments) ? attachments : [];
  if (!source || files.length === 0) return source;
  files.forEach(function(file) {
    var url = String(file && file.url || "").trim();
    var label = String(file && file.name || "").trim();
    if (!url || !label) return;
    var safeLabel = escapeRegExp(label);
    source = source.replace(new RegExp("(^|[\\s(])(" + safeLabel + ")(?=$|[\\s).,!?])", "g"), function(_match, prefix, matched) {
      return prefix + "[" + matched + "](" + url + ")";
    });
  });
  return source;
}

function formatElapsedMs(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function tokensDisplay(raw, t) {
  if (!raw || raw === "—") return raw || "—";
  // "X in / Y out / Z total" → translate labels
  return raw
    .replace(/\b(in)\b/g, t("chat.tokenIn"))
    .replace(/\b(out)\b/g, t("chat.tokenOut"))
    .replace(/\b(total)\b/g, t("chat.tokenTotal"));
}

function syncTextareaHeight(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = Math.min(200, textarea.scrollHeight) + "px";
}

function isAbortError(error) {
  return error && (error.name === "AbortError" || String(error.message || "").includes("aborted"));
}

function attachmentAltText(file) {
  return String(file && file.name || "uploaded image");
}

function attachmentThumbStyle(file, maxWidth, maxHeight) {
  var width = Number(file && file.width) || 0;
  var height = Number(file && file.height) || 0;
  if (!(width > 0) || !(height > 0)) {
    return {
      maxWidth: maxWidth + "px",
      maxHeight: maxHeight + "px",
      width: "auto",
      height: "auto",
    };
  }
  var ratio = Math.min(maxWidth / width, maxHeight / height, 1);
  return {
    width: Math.max(1, Math.round(width * ratio)) + "px",
    height: Math.max(1, Math.round(height * ratio)) + "px",
  };
}

async function readNdjsonStream(response, onEvent) {
  if (!response.body || !response.body.getReader) {
    const payload = await response.json();
    onEvent(payload.awaiting_user
      ? { type: "awaiting_user", ...payload }
      : payload.queued
      ? { type: "queued", ...payload }
      : { type: "reply_done", response: payload.response || "" });
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  async function dispatchEvent(event) {
    if (!event || event.type !== "reply_delta") {
      onEvent(event);
      return;
    }
    const source = String(event.delta || "");
    if (!source) return;
    const chars = Array.from(source);
    const chunkSize = 24;
    for (let i = 0; i < chars.length; i += chunkSize) {
      onEvent({ ...event, delta: chars.slice(i, i + chunkSize).join("") });
      await new Promise(function (resolve) { window.requestAnimationFrame(resolve); });
    }
  }

  while (true) {
    const read = await reader.read();
    if (read.done) break;
    buffer += decoder.decode(read.value, { stream: true });
    while (true) {
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex < 0) break;
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (!line) continue;
      await dispatchEvent(JSON.parse(line));
    }
  }
  buffer += decoder.decode();
  const tail = buffer.trim();
  if (tail) await dispatchEvent(JSON.parse(tail));
}

function upsertStreamingAgentMessage(requestId, delta, done) {
  updateChatRuntime(function (state) {
    const pending = (state.pendingMessages || []).slice();
    const targetId = "pending_agent_" + requestId;
    const targetIndex = pending.findIndex(function (msg) {
      return String(msg && msg.clientRequestId || "") === requestId && msg.role === "agent";
    });
    if (targetIndex >= 0) {
      const current = pending[targetIndex];
      pending[targetIndex] = {
        ...current,
        body: String(current.body || "") + String(delta || ""),
        streamingReply: !done,
      };
    } else {
      pending.push({
        id: targetId,
        role: "agent",
        time: new Date().toLocaleTimeString(),
        body: String(delta || ""),
        clientRequestId: requestId,
        streamingReply: !done,
      });
    }
    return { pendingMessages: pending };
  });
}

function messageKey(msg) {
  const messageId = String(msg && msg.messageId || msg && msg.id || "");
  if (msg && (msg.intermediateReply || msg.questionPrompt) && messageId) return "message::" + messageId;
  const clientRequestId = String(msg && msg.clientRequestId || "");
  if (clientRequestId) return "request::" + clientRequestId + "::" + String(msg && msg.role || "");
  const queuedGuidanceId = String(msg && msg.queuedGuidanceId || "");
  if (queuedGuidanceId) return "guide::" + queuedGuidanceId;
  const guidanceAckForGuidanceId = String(msg && msg.guidanceAckForGuidanceId || "");
  if (guidanceAckForGuidanceId) return "guidance-ack::" + guidanceAckForGuidanceId;
  if (messageId) return "message::" + messageId;
  return [
    String(msg && msg.role || ""),
    String(msg && msg.roundId || ""),
    String(msg && msg.body || ""),
  ].join("::");
}

function unmatchedMessages(existingMessages, transientMessages) {
  const counts = new Map();
  (existingMessages || []).forEach(function (msg) {
    const key = messageKey(msg);
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const visible = [];
  (transientMessages || []).forEach(function (msg) {
    const key = messageKey(msg);
    const remaining = counts.get(key) || 0;
    if (remaining > 0) {
      counts.set(key, remaining - 1);
      return;
    }
    visible.push(msg);
  });
  return visible;
}

function pruneRetainedMessages(existingMessages, transientMessages) {
  return unmatchedMessages(existingMessages, transientMessages).filter(function (msg) {
    const attachmentRequestId = String(msg && msg.attachToAssistantReplyForRequestId || "");
    if (attachmentRequestId) return true;
    const replacementRequestId = String(msg && msg.replaceWhenAssistantReplyForRequestId || "");
    if (!replacementRequestId) return true;
    return !hasVisibleAssistantReplyForRequest(existingMessages, replacementRequestId);
  });
}

function visibleRetainedMessages(existingMessages, transientMessages) {
  return pruneRetainedMessages(existingMessages, transientMessages).filter(function (msg) {
    return !String(msg && msg.attachToAssistantReplyForRequestId || "");
  });
}

function mergeMessagesWithAnchors(baseMessages, anchoredMessages) {
  const merged = (baseMessages || []).slice();
  (anchoredMessages || []).forEach(function (msg) {
    const anchorKey = String(msg && msg.insertAfterKey || "");
    if (!anchorKey) {
      merged.push(msg);
      return;
    }
    let anchorIndex = -1;
    for (let i = 0; i < merged.length; i += 1) {
      if (messageKey(merged[i]) === anchorKey) anchorIndex = i;
    }
    if (anchorIndex < 0) {
      merged.push(msg);
      return;
    }
    merged.splice(anchorIndex + 1, 0, msg);
  });
  return merged;
}

function renderMessageEntries(messages) {
  const counts = new Map();
  return (messages || []).map(function (msg) {
    const baseKey = messageKey(msg);
    const occurrence = (counts.get(baseKey) || 0) + 1;
    counts.set(baseKey, occurrence);
    return {
      msg,
      renderKey: baseKey + "::" + occurrence,
    };
  });
}

function runtimeTraceDescriptor(activeRequest) {
  const isGuidance = Boolean(activeRequest && activeRequest.guideRoundId);
  const guidanceAccepted = Boolean(isGuidance && activeRequest && activeRequest.guidanceAccepted);
  if (isGuidance && !guidanceAccepted) {
    return {
      timeLabel: window.t("chat.guiding"),
      summary: window.t("chat.detailsInbox"),
      head: window.t("chat.queue"),
      empty: window.t("chat.sendingToInbox"),
    };
  }
  if (guidanceAccepted) {
    return {
      timeLabel: "…",
      summary: window.t("chat.detailsAfterGuidance"),
      head: window.t("chat.processing"),
      empty: window.t("chat.continuingGuidance"),
    };
  }
  return {
    timeLabel: "…",
    summary: window.t("chat.detailsProcessing"),
    head: window.t("chat.processing"),
    empty: window.t("chat.thinking"),
  };
}

function snapshotRuntimeTrace(state, options) {
  if (!state || !state.startedAt) return null;
  const activeRequest = options && options.activeRequest ? options.activeRequest : state.activeRequest;
  const descriptor = runtimeTraceDescriptor(activeRequest);
  const traceEntries = state.liveProgress && state.liveProgress.length
    ? state.liveProgress.slice()
    : [{ icon: "◎", text: descriptor.empty }];
  if (!traceEntries.length) return null;
  const endedAt = options && options.endedAt ? options.endedAt : Date.now();
  const traceId = String(options && options.traceId || ("runtime_trace_" + endedAt + "_" + Math.random().toString(36).slice(2, 8)));
  return {
    id: traceId,
    messageId: traceId,
    role: "agent",
    time: new Date(endedAt).toLocaleTimeString(),
    runtimeTrace: true,
    traceSummary: descriptor.summary,
    traceHead: descriptor.head,
    traceEntries,
    traceElapsed: formatElapsedMs(endedAt - state.startedAt),
    insertAfterKey: String(options && options.insertAfterKey || ""),
    replaceWhenAssistantReplyForRequestId: String(options && options.replaceWhenAssistantReplyForRequestId || ""),
    attachToAssistantReplyForRequestId: String(options && options.attachToAssistantReplyForRequestId || ""),
  };
}

function runtimeAttachmentFromTrace(msg) {
  return {
    summary: String(msg && msg.traceSummary || window.t("chat.detailsProcessing")),
    head: String(msg && msg.traceHead || window.t("chat.processing")),
    elapsed: String(msg && msg.traceElapsed || "00:00"),
    timeLabel: "—",
    entries: Array.isArray(msg && msg.traceEntries) ? msg.traceEntries.slice() : [],
  };
}

function collectRetainedRuntimeAttachments(retainedMessages) {
  const attachments = new Map();
  (retainedMessages || []).forEach(function (msg) {
    const requestId = String(msg && msg.attachToAssistantReplyForRequestId || "");
    if (!requestId || !msg.runtimeTrace) return;
    attachments.set(requestId, runtimeAttachmentFromTrace(msg));
  });
  return attachments;
}

function isTraceOnlyAssistantMessage(msg) {
  return Boolean(
    msg
    && msg.role === "agent"
    && !msg.body
    && !msg.runtimeTrace
    && (msg.thinking || (msg.tools && msg.tools.length))
  );
}

function isMapToolName(name) {
  var raw = String(name || "").trim();
  return raw === "pin_location" || raw === "connect_pins";
}

function messageHasMapTool(msg) {
  var tools = Array.isArray(msg && msg.tools) ? msg.tools : [];
  return tools.some(function (tool) {
    return isMapToolName(tool && tool.name);
  });
}

function isFinalVisibleAssistantReply(msg) {
  return Boolean(
    msg
    && msg.role === "agent"
    && !msg.runtimeTrace
    && !msg.intermediateReply
    && !msg.questionPrompt
    && String(msg.body || "").trim()
  );
}

function buildAttachedRuntime(activeTraceDescriptor, liveElapsed, visibleLiveProgress, watchingGuidance, activeGuideRoundTitle, activeRequest) {
  const entries = [];
  if (watchingGuidance) {
    entries.push({
      icon: "↳",
      text: t("chat.targetRound") + ": " + (activeGuideRoundTitle || (activeRequest && activeRequest.guideRoundId) || ""),
    });
  }
  if (visibleLiveProgress.length === 0) {
    entries.push({ icon: "◎", text: activeTraceDescriptor.empty });
  } else {
    entries.push(...visibleLiveProgress);
  }
  return {
    summary: activeTraceDescriptor.summary,
    head: activeTraceDescriptor.head,
    elapsed: liveElapsed,
    timeLabel: activeTraceDescriptor.timeLabel,
    entries,
  };
}

function canAttachRuntimeToLastMessage(msg, activeRequest, session) {
  if (!msg || msg.role !== "agent" || msg.runtimeTrace) return false;
  if (msg.guidanceAckForGuidanceId || msg.inReplyToGuidanceId || msg.queuedGuidanceId) return false;
  if (msg.questionPrompt || msg.intermediateReply) return false;
  const activeRequestId = String(activeRequest && activeRequest.id || "");
  const messageRequestId = String(msg && msg.clientRequestId || "");
  if (activeRequestId && messageRequestId && activeRequestId === messageRequestId) return true;
  if (!isTraceOnlyAssistantMessage(msg)) return false;
  const currentRoundId = String(session && session.currentRoundId || "");
  const messageRoundId = String(msg && msg.roundId || "");
  return Boolean(!messageRequestId && currentRoundId && messageRoundId && currentRoundId === messageRoundId);
}

function hasVisibleAssistantReplyForRequest(messages, requestId) {
  const targetRequestId = String(requestId || "");
  if (!targetRequestId) return false;
  return (messages || []).some(function (msg) {
    return msg
      && isFinalVisibleAssistantReply(msg)
      && String(msg.clientRequestId || "") === targetRequestId;
  });
}

function formatProgressEvent(event) {
  switch (event.type) {
    case "phase_transition":
      return { icon: "●", text: event.detail || event.from + " → " + event.to, type: "phase_transition", from: event.from, to: event.to };
    case "tool_call": {
      const args = event.args || {};
      const argPreview = Object.values(args).filter(Boolean).map(String).join(", ").slice(0, 60);
      return { icon: "▸", text: event.tool + (argPreview ? "(" + argPreview + ")" : "()"), type: "tool_call", tool: event.tool, args: args };
    }
    case "llm_call":
      return { icon: "◎", text: (event.caller || "agent") + " · " + (event.phase || "thinking"), type: "llm_call", caller: event.caller, phase: event.phase };
    default:
      return null;
  }
}

function getChatRuntime() {
  if (!window.__chatRuntime) {
    window.__chatRuntime = {
      sending: false,
      startedAt: 0,
      pendingMessages: [],
      retainedMessages: [],
      liveProgress: [],
      activeRequest: null,
      watchRequestId: "",
      requestSeq: 0,
      requests: {},
      listeners: new Set(),
      sseHandler: null,
      retiredRequestIds: [],
    };
  }
  return window.__chatRuntime;
}

function getChatRuntimeSnapshot() {
  const runtime = getChatRuntime();
  return {
    sending: runtime.sending,
    startedAt: runtime.startedAt || 0,
    pendingMessages: runtime.pendingMessages.slice(),
    retainedMessages: runtime.retainedMessages.slice(),
    liveProgress: runtime.liveProgress.slice(),
    activeRequest: runtime.activeRequest ? { ...runtime.activeRequest } : null,
  };
}

function isUnfinishedSubagent(status) {
  return status === "running" || status === "queued";
}

function visibleRoundSubagents(session) {
  const all = Array.isArray(session && session.subagents) ? session.subagents : [];
  const currentRoundId = String(session && session.currentRoundId || "").trim();
  if (!currentRoundId) return all.filter(function (sa) { return isUnfinishedSubagent(sa && sa.status); });
  return all.filter(function (sa) {
    if (sa && String(sa.id || "").startsWith("agent_summary_")) return false;
    const roundId = String(sa && sa.roundId || "").trim();
    if (roundId && roundId === currentRoundId) return true;
    return isUnfinishedSubagent(sa && sa.status);
  });
}

function selectableLiveRounds(session) {
  const rounds = Array.isArray(session && session.liveRounds) ? session.liveRounds : [];
  return rounds.filter(function (round) {
    return round && (round.status === "running" || round.status === "queued");
  });
}

function emitChatRuntime() {
  const runtime = getChatRuntime();
  const snapshot = getChatRuntimeSnapshot();
  runtime.listeners.forEach(function (listener) { listener(snapshot); });
}

function updateChatRuntime(updater) {
  const runtime = getChatRuntime();
  const next = typeof updater === "function" ? updater(runtime) : updater;
  if (next && typeof next === "object") Object.assign(runtime, next);
  emitChatRuntime();
}

function ensureChatRuntimeSseSubscription() {
  const runtime = getChatRuntime();
  if (runtime.sseHandler) return;
  runtime.sseHandler = function (event) {
    const eventGuidanceId = String(event && event.guidance_id || "");
    const eventRequestId = String(event && event.client_request_id || "");
    if (event && event.type === "guidance_acknowledged" && eventRequestId && runtime.watchRequestId === eventRequestId) {
      updateChatRuntime({
        startedAt: Date.now(),
        liveProgress: [],
        activeRequest: runtime.activeRequest
          ? {
              ...runtime.activeRequest,
              guidanceAccepted: true,
              guidanceId: eventGuidanceId,
              finalTraceAnchorKey: eventGuidanceId ? "guidance-ack::" + eventGuidanceId : "",
            }
          : null,
      });
      return;
    }
    if (event && event.type === "chat_message" && eventRequestId && runtime.watchRequestId === eventRequestId) {
      const frozenFinalTrace = snapshotRuntimeTrace(runtime, {
        attachToAssistantReplyForRequestId: eventRequestId,
      });
      delete runtime.requests[eventRequestId];
      updateChatRuntime({
        sending: false,
        liveProgress: [],
        startedAt: 0,
        activeRequest: null,
        watchRequestId: "",
        pendingMessages: [],
        retainedMessages: frozenFinalTrace
          ? runtime.retainedMessages.concat([frozenFinalTrace])
          : runtime.retainedMessages.slice(),
      });
      clearChatRuntimeSseSubscription();
      return;
    }
    const entry = formatProgressEvent(event);
    if (!entry) return;
    updateChatRuntime(function (state) {
      return { liveProgress: state.liveProgress.concat([entry]).slice(-30) };
    });
  };
  window.__sseHandlers.add(runtime.sseHandler);
}

function clearChatRuntimeSseSubscription() {
  const runtime = getChatRuntime();
  if (!runtime.sseHandler) return;
  window.__sseHandlers.delete(runtime.sseHandler);
  runtime.sseHandler = null;
}

function resetChatRuntime(options) {
  const runtime = getChatRuntime();
  const shouldAbort = !options || options.abort !== false;
  if (shouldAbort) {
    Object.values(runtime.requests || {}).forEach(function (requestMeta) {
      if (requestMeta && requestMeta.controller) requestMeta.controller.abort();
    });
  }
  runtime.requests = {};
  runtime.requestSeq = 0;
  updateChatRuntime({
    sending: false,
    startedAt: 0,
    pendingMessages: [],
    retainedMessages: [],
    liveProgress: [],
    activeRequest: null,
    watchRequestId: "",
  });
  clearChatRuntimeSseSubscription();
}

window.resetChatRuntime = resetChatRuntime;

var _SIDEBAR_CODE_EXTS = new Set(["py","js","ts","jsx","tsx","css","json","yaml","yml","toml","xml","sql","sh","bash","rs","go","java","c","cpp","h","rb","php","swift","kt","txt","csv","ini","cfg","env"]);

function classifyAttachmentTab(file) {
  var ct = String(file.content_type || "");
  var ext = String(file.name || "").split(".").pop().toLowerCase();
  if (ct.startsWith("image/")) return null;
  if (ct === "application/pdf") return "pdf";
  if (ct === "application/vnd.ms-powerpoint" || ct === "application/vnd.openxmlformats-officedocument.presentationml.presentation") return "ppt";
  if (ct === "text/html" || ct === "application/xhtml+xml") return "html";
  if (file.kind === "map" || ct === "application/geo+json" || ct === "application/vnd.geo+json") return "map";
  if (file.kind === "markdown" || ext === "md" || ext === "markdown") return "markdown";
  if (file.kind === "code" || _SIDEBAR_CODE_EXTS.has(ext)) return "code-editor";
  return null;
}

function ChatPage({ selectedSessionId, onSelectSession, rightSidebarCollapsed = false, setRightSidebarCollapsed, rightSidebarView = "overview", setRightSidebarView }) {
  useDataVersion(); // re-render when DATA refreshes
  const { t, lang } = useI18n();

  const session = (selectedSessionId
    ? DATA.sessions.find(function (s) { return s.id === selectedSessionId; })
    : null) || DATA.sessions[0] || {
    id: "—", title: "—", status: "queued", started: "—", model: "—",
    currentRoundId: "",
    currentRoundTitle: "",
    summary: { tokens: "—", spend: "—", toolCalls: 0 },
    chat: { contextChips: [], messages: [] },
    liveRounds: [],
    shells: [], subagents: [],
  };
  // Defensive: ensure shells / summary exist even if backend omits them
  if (!Array.isArray(session.shells)) session.shells = [];
  if (!Array.isArray(session.liveRounds)) session.liveRounds = [];
  if (!session.summary) session.summary = { tokens: "—", spend: "—", toolCalls: 0 };

  const isLiveSession = session.id === "run_live";
  const pendingQuestion = isLiveSession && session.pendingQuestion ? session.pendingQuestion : null;
  const subagents = visibleRoundSubagents(session);
  const liveRounds = selectableLiveRounds(session);
  const runningSubagents = subagents.filter((s) => s.status === "running").length;

  // Expose global session switcher so the sidebar can switch the chat view
  useEffect(function () {
    window.selectChatSession = function (id) { onSelectSession && onSelectSession(id); };
    return function () { delete window.selectChatSession; };
  }, [onSelectSession]);

  // Load context state (SOUL.md / workspace active status + workspace history)
  useEffect(function () {
    if (!isLiveSession) return;
    fetch("/api/context/state")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        setContextState(s);
        setWorkspaceHistory(s.workspace_history || []);
      })
      .catch(function () {});
  }, [isLiveSession, session.id]);

  /* Reset transient chat state when switching sessions. */
  useEffect(function () {
    initialScrollDoneRef.current = false;
    setMutationDiff({ diff: "", signature: "" });
  }, [session.id]);

  /* Restore runtime trace state on page refresh if the session is still running.
     Also clear the restored state when the session finishes. */
  useEffect(function () {
    if (!isLiveSession) {
      return;
    }
    const runtime = getChatRuntime();
    if (session.status === "running" && !runtime.sending && !restoredRef.current) {
      // Session is running but runtime state was lost (page refresh).
      // Show the runtime trace panel and subscribe to SSE for live progress.
      restoredRef.current = true;
      updateChatRuntime({
        sending: true,
        startedAt: Date.now(),
        liveProgress: [],
        activeRequest: {
          id: "restored_" + Date.now(),
          message: "",
          guideRoundId: "",
          guideRoundTitle: "",
          guideRequestId: "",
          guidanceAccepted: false,
          finalTraceAnchorKey: "",
        },
        watchRequestId: "restored_" + Date.now(),
      });
      ensureChatRuntimeSseSubscription();
    } else if (restoredRef.current && session.status !== "running") {
      // Session finished running — clear the restored runtime state.
      restoredRef.current = false;
      const r = getChatRuntime();
      if (r.sending && r.watchRequestId && r.watchRequestId.startsWith("restored_")) {
        const frozenFinalTrace = snapshotRuntimeTrace(r, {
          attachToAssistantReplyForRequestId: r.watchRequestId,
        });
        delete r.requests[r.watchRequestId];
        updateChatRuntime({
          sending: false,
          liveProgress: [],
          startedAt: 0,
          activeRequest: null,
          watchRequestId: "",
          pendingMessages: [],
          retainedMessages: frozenFinalTrace
            ? r.retainedMessages.concat([frozenFinalTrace])
            : r.retainedMessages.slice(),
        });
        clearChatRuntimeSseSubscription();
      }
    }
  }, [isLiveSession, session.status, session.id]);

  useEffect(function () {
    function onOpenEditor(e) {
      var detail = e.detail || {};
      setEditorData({
        code: detail.code || "",
        language: detail.language || "",
        filePath: detail.filePath || "",
      });
      addSidebarTab("code-editor");
      setRightSidebarView("code-editor");
    }
    window.addEventListener("cyrene:open-editor", onOpenEditor);
    function onOpenDiff(e) {
      var detail = e.detail || {};
      setDiffData({
        diff: detail.diff || "",
        mode: detail.mode || "text",
        left: detail.left || "",
        right: detail.right || "",
      });
      addSidebarTab("diff-viewer");
      setRightSidebarView("diff-viewer");
    }
    window.addEventListener("cyrene:open-diff", onOpenDiff);
    return function () {
      window.removeEventListener("cyrene:open-editor", onOpenEditor);
      window.removeEventListener("cyrene:open-diff", onOpenDiff);
    };
  }, [setRightSidebarView]);

  useEffect(function () {
    if (!isLiveSession) return;
    var messages = Array.isArray(session.chat && session.chat.messages) ? session.chat.messages : [];
    var candidate = null;
    for (var i = messages.length - 1; i >= 0; i--) {
      var msg = messages[i];
      if (!msg || msg.role !== "agent" || !Array.isArray(msg.tools) || msg.tools.length === 0) continue;
      if (!msg.tools.some(isCodeMutationTool)) continue;
      candidate = msg;
      break;
    }
    if (!candidate) return;
    var candidateRequestId = String(candidate.clientRequestId || "");
    if (!candidateRequestId) {
      for (var prevIndex = messages.indexOf(candidate) - 1; prevIndex >= 0; prevIndex--) {
        var prevMsg = messages[prevIndex];
        if (prevMsg && prevMsg.role === "user" && prevMsg.clientRequestId) {
          candidateRequestId = String(prevMsg.clientRequestId || "");
          break;
        }
      }
    }
    var signature = String(candidateRequestId || candidate.messageId || candidate.id || "") + ":" + candidate.tools.map(function (tool) {
      return String(tool && (tool.toolCallId || tool.name) || "");
    }).join(",");
    if (!signature || signature === autoDiffSignatureRef.current) return;
    if (gitDiffUnavailableRef.current) return;
    autoDiffSignatureRef.current = signature;
    refreshMutationDiffForTools(
      candidate.tools.filter(isCodeMutationTool),
      signature,
      candidateRequestId
    );
  }, [isLiveSession, session.id, session.chat.messages, setRightSidebarView]);

  // ── Round change: clear content tabs ───────────────────────────────────
  useEffect(function () {
    var roundId = session.currentRoundId || "";
    if (!roundId) return;
    if (prevRoundIdRef.current && roundId !== prevRoundIdRef.current) {
      setSidebarTabs(new Set());
      setActiveHtmlContent(null);
      setActivePdfUrl(null);
      setActivePdfName("");
      setActivePptUrl(null);
      setActivePptName("");
      setActiveMarkdownContent("");
      setActiveMarkdownName("");
      setHtmlViewTab("rendered");
      autoLoadedAttachmentsRef.current = new Set();
      autoDiffSignatureRef.current = "";
      var contentViews = ["html", "pdf", "ppt", "markdown", "map", "code-editor", "diff-viewer"];
      if (contentViews.indexOf(rightSidebarView) !== -1) {
        setRightSidebarView("overview");
      }
    }
    prevRoundIdRef.current = roundId;
  }, [session.currentRoundId]);

  // ── Auto-detect agent attachments → register sidebar tabs ──────────────
  useEffect(function () {
    var messages = Array.isArray(session.chat && session.chat.messages) ? session.chat.messages : [];
    var currentRound = session.currentRoundId || "";
    var newTabs = new Set();
    var latestFiles = {};

    for (var i = messages.length - 1; i >= 0; i--) {
      var msg = messages[i];
      if (msg.role !== "agent") continue;
      if (currentRound && msg.roundId && msg.roundId !== currentRound) continue;
      var files = Array.isArray(msg.files) ? msg.files : [];
      for (var j = 0; j < files.length; j++) {
        var file = files[j];
        if (!file.url) continue;
        var tabId = classifyAttachmentTab(file);
        if (!tabId) continue;
        newTabs.add(tabId);
        if (!latestFiles[tabId]) latestFiles[tabId] = file;
      }
    }

    if (newTabs.size === 0) return;

    setSidebarTabs(function (prev) {
      var merged = new Set(prev);
      var changed = false;
      newTabs.forEach(function (t) { if (!merged.has(t)) { merged.add(t); changed = true; } });
      return changed ? merged : prev;
    });

    Object.keys(latestFiles).forEach(function (tid) {
      var f = latestFiles[tid];
      var loadKey = tid + ":" + f.url;
      if (autoLoadedAttachmentsRef.current.has(loadKey)) return;
      autoLoadedAttachmentsRef.current.add(loadKey);

      if (tid === "pdf") {
        setActivePdfUrl(f.url);
        setActivePdfName(f.name || "");
      } else if (tid === "ppt") {
        setActivePptUrl(f.url);
        setActivePptName(f.name || "");
      } else if (tid === "html") {
        fetch(f.url).then(function (r) { return r.text(); }).then(function (html) {
          setActiveHtmlContent(html);
        }).catch(function () {});
      } else if (tid === "markdown") {
        fetch(f.url).then(function (r) { return r.text(); }).then(function (md) {
          setActiveMarkdownContent(md);
          setActiveMarkdownName(f.name || "");
        }).catch(function () {});
      } else if (tid === "code-editor") {
        fetch(f.url).then(function (r) { return r.text(); }).then(function (code) {
          setEditorData({ code: code, language: "", filePath: f.name || "" });
        }).catch(function () {});
      }
    });
  }, [session.chat.messages, session.currentRoundId]);

  function contextKey(c) { return c.key || c.label; }

  function contextDisplayLabel(c) {
    var key = c.key || c.label;
    if (key === "soul") return t("chat.contextSoul");
    if (key === "workspace") return t("chat.contextWorkspace");
    return c.label;
  }

  async function removeContext(chipKey) {
    setHiddenContexts(Object.assign({}, hiddenContexts, (function (o) { o[chipKey] = true; return o; })({})));
    var chips = session.chat.contextChips;
    if (chips) {
      for (var i = chips.length - 1; i >= 0; i--) {
        if (contextKey(chips[i]) === chipKey) chips.splice(i, 1);
      }
    }
    if (chipKey === "soul") {
      await fetch("/api/context/remove-soul", { method: "POST" });
    } else if (chipKey === "workspace") {
      await fetch("/api/context/remove-workspace", { method: "POST" });
    }
    setContextPickerOpen(false);
    if (window.refreshSessions) window.refreshSessions();
  }

  async function addContext(chipKey, path) {
    var h = Object.assign({}, hiddenContexts);
    delete h[chipKey];
    setHiddenContexts(h);
    var icon = chipKey === "soul" ? "🧠" : "📁";
    var chips = session.chat.contextChips;
    if (chips) {
      var found = false;
      for (var i = 0; i < chips.length; i++) {
        if (contextKey(chips[i]) === chipKey) { found = true; break; }
      }
      if (!found) chips.push({ icon: icon, label: chipKey, key: chipKey });
    }
    if (chipKey === "soul") {
      await fetch("/api/context/add-soul", { method: "POST" });
    } else if (chipKey === "workspace") {
      await fetch("/api/context/add-workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path || "" }),
      });
      if (path) {
        setWorkspaceHistory((function (prev) {
          var next = prev.filter(function (p) { return p !== path; });
          next.unshift(path);
          return next.slice(0, 10);
        })());
      }
    }
    setContextPickerOpen(false);
    if (window.refreshSessions) window.refreshSessions();
  }

  async function pickWorkspaceDir() {
    try {
      var r = await fetch("/api/context/pick-directory", { method: "POST" });
      var data = await r.json();
      if (data.path) {
        await addContext("workspace", data.path);
      }
    } catch (e) {}
  }

  const [draft, setDraft] = useState("");
  const [questionDraft, setQuestionDraft] = useState("");
  const [answeringQuestion, setAnsweringQuestion] = useState(false);
  const [contextPickerOpen, setContextPickerOpen] = useState(false);
  const [selectedGuideRoundId, setSelectedGuideRoundId] = useState("");
  const [selectedGuideRoundTitle, setSelectedGuideRoundTitle] = useState("");
  const [hiddenContexts, setHiddenContexts] = useState({});
  const [workspaceHistory, setWorkspaceHistory] = useState([]);
  const [contextState, setContextState] = useState({ soul_active: true, workspace_active: true, workspace_dir: "" });
  const [notice, setNotice] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [editorData, setEditorData] = useState({ code: "", language: "", filePath: "" });
  const [activeMarkdownContent, setActiveMarkdownContent] = useState("");
  const [activeMarkdownName, setActiveMarkdownName] = useState("");
  const [diffData, setDiffData] = useState({ diff: "", mode: "text", left: "", right: "" });
  const [mutationDiff, setMutationDiff] = useState({ diff: "", signature: "" });
  const [mutationDiffsByRequest, setMutationDiffsByRequest] = useState({});
  const autoDiffSignatureRef = useRef("");
  const gitDiffUnavailableRef = useRef(false);
  const [command, setCommand] = useState("");
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [slashIndex, setSlashIndex] = useState(-1);
  const [mentionMenuOpen, setMentionMenuOpen] = useState(false);
  const [mentionedAgents, setMentionedAgents] = useState([]);
  // True during the first ~second after mount.  Ensures scroll-to-latest-user-
  // message uses instant scroll instead of smooth, which is fragile during the
  // mounting phase when layout is still settling.
  const mountingRef = useRef(true);
  const restoredRef = useRef(false);

  // ── Sidebar tab lifecycle ──────────────────────────────────────────────
  const [sidebarTabs, setSidebarTabs] = useState(new Set());
  const prevRoundIdRef = useRef("");
  const autoLoadedAttachmentsRef = useRef(new Set());

  function addSidebarTab(tabId) {
    setSidebarTabs(function (prev) {
      if (prev.has(tabId)) return prev;
      var next = new Set(prev);
      next.add(tabId);
      return next;
    });
  }

  var ALL_COMMANDS = [
    { id: "quick-answer",    icon: "⚡", label: t("chat.commandQuickAnswer"),    desc: t("chat.commandQuickAnswerDesc"),    placeholder: t("chat.quickAnswerPlaceholder") },
    { id: "deep-research",   icon: "🔬", label: t("chat.commandDeepResearch"),   desc: t("chat.commandDeepResearchDesc"),   placeholder: t("chat.deepResearchPlaceholder") },
    { id: "deep-reflect",    icon: "💭", label: t("chat.commandDeepReflect"),    desc: t("chat.commandDeepReflectDesc"),    placeholder: t("chat.deepReflectPlaceholder") },
    { id: "help-me-decide",  icon: "🤔", label: t("chat.commandHelpMeDecide"),   desc: t("chat.commandHelpMeDecideDesc"),   placeholder: t("chat.helpMeDecidePlaceholder") },
    { id: "learning-plan",   icon: "📚", label: t("chat.commandLearningPlan"),   desc: t("chat.commandLearningPlanDesc"),   placeholder: t("chat.learningPlanPlaceholder") },
    { id: "daily-review",    icon: "🌙", label: t("chat.commandDailyReview"),    desc: t("chat.commandDailyReviewDesc"),    placeholder: t("chat.dailyReviewPlaceholder") },
    { id: "deep-compare",    icon: "🔄", label: t("chat.commandDeepCompare"),    desc: t("chat.commandDeepCompareDesc"),    placeholder: t("chat.deepComparePlaceholder") },
    { id: "claude-code",     icon: "💻", label: t("chat.commandClaudeCode"),     desc: t("chat.commandClaudeCodeDesc"),     placeholder: t("chat.claudeCodePlaceholder") },
  ];
  var slashSearch = (draft.startsWith("/") && draft.length > 1) ? draft.slice(1).toLowerCase() : "";
  var filteredCommands = slashSearch
    ? ALL_COMMANDS.filter(function (cmd) {
        return cmd.id.indexOf(slashSearch) !== -1
            || cmd.label.toLowerCase().indexOf(slashSearch) !== -1
            || cmd.desc.toLowerCase().indexOf(slashSearch) !== -1;
      })
    : ALL_COMMANDS;
  function findCommand(id) {
    for (var i = 0; i < ALL_COMMANDS.length; i++) {
      if (ALL_COMMANDS[i].id === id) return ALL_COMMANDS[i];
    }
    return null;
  }
  const [ccStatus, setCcStatus] = useState(null);
  const [sidebarWidth, setSidebarWidth] = useState(function() {
    try { return parseInt(localStorage.getItem("cyrene-sidebar-width") || "360", 10) || 360; } catch(e) { return 360; }
  });
  const [activeHtmlContent, setActiveHtmlContent] = useState(null);
  const [activePdfUrl, setActivePdfUrl] = useState(null);
  const [activePdfName, setActivePdfName] = useState("");
  const [activePptUrl, setActivePptUrl] = useState(null);
  const [activePptName, setActivePptName] = useState("");
  const [htmlViewTab, setHtmlViewTab] = useState("rendered");
  const [ccModal, setCcModal] = useState(null);
  const [shellModal, setShellModal] = useState(null);
  const [runtimeState, setRuntimeState] = useState(getChatRuntimeSnapshot);
  const [elapsedNow, setElapsedNow] = useState(Date.now());
  const taRef = useRef(null);
  const scrollRef = useRef(null);
  const composerRef = useRef(null);
  const fileInputRef = useRef(null);
  const userAtBottomRef = useRef(true);
  const initialScrollDoneRef = useRef(false);
  const latestPinnedUserKeyRef = useRef("");
  // When true, scrollChatToBottom is suppressed — used while the latest user
  // message is animating to the top of the viewport.  Cleared by user wheel.
  const pinnedMessageRef = useRef(false);
  const sending = runtimeState.sending;
  const pendingMessages = runtimeState.pendingMessages;
  const prunedRetainedMessages = isLiveSession
    ? pruneRetainedMessages(session.chat.messages, runtimeState.retainedMessages || [])
    : [];
  const retainedMessages = isLiveSession
    ? visibleRetainedMessages(session.chat.messages, runtimeState.retainedMessages || [])
    : [];
  const visiblePendingMessages = isLiveSession
    ? unmatchedMessages((session.chat.messages || []).concat(retainedMessages), pendingMessages || [])
    : [];
  const retainedRuntimeAttachments = isLiveSession
    ? collectRetainedRuntimeAttachments(prunedRetainedMessages)
    : new Map();
  const liveProgress = runtimeState.liveProgress;
  const activeRequest = runtimeState.activeRequest;
  const selectedGuideRound = liveRounds.find(function (round) { return round.id === selectedGuideRoundId; }) || null;
  const hasSelectedGuideRound = Boolean(selectedGuideRoundId);
  const currentGuideRoundTitle = selectedGuideRound
    ? selectedGuideRound.title
    : (selectedGuideRoundTitle || selectedGuideRoundId);
  const activeGuideRoundTitle = activeRequest && activeRequest.guideRoundTitle
    ? activeRequest.guideRoundTitle
    : currentGuideRoundTitle;
  const watchingGuidance = Boolean(activeRequest && activeRequest.guideRoundId && !activeRequest.guidanceAccepted);
  const activeTraceDescriptor = runtimeTraceDescriptor(activeRequest);
  const liveElapsed = runtimeState.startedAt ? formatElapsedMs(elapsedNow - runtimeState.startedAt) : "00:00";
  const requestFulfilledInSession = isLiveSession
    ? hasVisibleAssistantReplyForRequest(session.chat.messages, runtimeState.watchRequestId)
    : false;
  const visibleSending = isLiveSession && sending && !requestFulfilledInSession;
  const visibleNotice = isLiveSession ? notice : "";
  const visibleLiveProgress = isLiveSession ? liveProgress : [];
  const questionOptionCount = pendingQuestion && Array.isArray(pendingQuestion.options)
    ? pendingQuestion.options.length
    : 0;

  useEffect(function () {
    if (!isLiveSession || !visibleSending || gitDiffUnavailableRef.current) return;
    const paths = uniqueMutationPathsFromProgress(visibleLiveProgress);
    if (paths.length === 0) return;
    const signature = String(runtimeState.watchRequestId || session.id || "") + ":" + paths.join("|");
    refreshMutationDiffForPaths(paths, signature, runtimeState.watchRequestId);
  }, [isLiveSession, visibleSending, visibleLiveProgress, runtimeState.watchRequestId, session.id, mutationDiff.signature, mutationDiffsByRequest]);

  function isNearBottom(el, threshold) {
    if (!el) return true;
    // When content is too short to overflow, the user cannot be "at the
    // bottom" in a meaningful sense — every position is near the bottom.
    if (el.scrollHeight <= el.clientHeight) return false;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= (threshold || 60);
  }

  function scrollChatToBottom(settle) {
    var el = scrollRef.current;
    if (!el) return function () {};
    // Don't override a pinned user message — scrollToLatestUserMessage is
    // animating it to the top and we must not fight the animation.
    if (pinnedMessageRef.current) return function () {};
    // After initial mount, only scroll if user is actually near bottom
    if (initialScrollDoneRef.current && !isNearBottom(el, 60)) {
      userAtBottomRef.current = false;
      return function () {};
    }
    initialScrollDoneRef.current = true;
    userAtBottomRef.current = false;
    var timers = [];
    function scrollDown() {
      var target = scrollRef.current;
      if (target) target.scrollTop = target.scrollHeight;
    }
    scrollDown();
    requestAnimationFrame(function () {
      scrollDown();
      requestAnimationFrame(scrollDown);
    });
    if (settle) {
      timers.push(window.setTimeout(scrollDown, 80));
      timers.push(window.setTimeout(scrollDown, 220));
    }
    return function () {
      timers.forEach(function (timer) { window.clearTimeout(timer); });
    };
  }

  function scrollToLatestUserMessage() {
    var el = scrollRef.current;
    if (!el) return;
    pinnedMessageRef.current = true;
    el.style.setProperty('--scroll-pb-extra', '0px');
    var userMessages = el.querySelectorAll('.modern-message.user');
    var latestUser = userMessages.length ? userMessages[userMessages.length - 1] : null;
    var desired = Math.max(0, el.scrollHeight - el.clientHeight);
    if (latestUser) {
      var containerRect = el.getBoundingClientRect();
      var padTop = parseFloat(getComputedStyle(el).paddingTop) || 0;
      desired = Math.max(0, latestUser.getBoundingClientRect().top - containerRect.top + el.scrollTop - padTop + 2);
      var maxScroll = Math.max(0, el.scrollHeight - el.clientHeight);
      if (desired > maxScroll) {
        el.style.setProperty('--scroll-pb-extra', Math.ceil(desired - maxScroll + 16) + 'px');
        var _forceLayout = el.offsetHeight;
      }
    }
    if (mountingRef.current) {
      el.scrollTop = desired;
    } else {
      el.scrollTo({ top: desired, behavior: 'smooth' });
    }
    userAtBottomRef.current = false;
  }

  function resetEmptyConversationViewport() {
    var el = scrollRef.current;
    pinnedMessageRef.current = false;
    latestPinnedUserKeyRef.current = "";
    userAtBottomRef.current = false;
    initialScrollDoneRef.current = false;
    if (!el) return;
    el.style.setProperty('--scroll-pb-extra', '0px');
    el.scrollTop = 0;
  }

  /* When user sends a message: scroll the user message to viewport top so the agent
     reply can unfold below it. Checks renderedMessages (includes pending) so we catch
     the first render, not just SSE updates. */
  useEffect(function () {
    if (renderedMessages.length === 0) {
      resetEmptyConversationViewport();
      return;
    }
    var lastRendered = renderedMessages[renderedMessages.length - 1];
    if (lastRendered && lastRendered.role === 'user') {
      var latestUserKey = messageKey(lastRendered);
      if (latestPinnedUserKeyRef.current === latestUserKey) return;
      latestPinnedUserKeyRef.current = latestUserKey;
      scrollToLatestUserMessage();
    }
  }, [session.id, session.chat.messages.length, retainedMessages.length, visiblePendingMessages.length]);

  /* Set --scroll-pb before paint and position the viewport in a single layout
     cycle, so the first paint already shows the correct scroll position (no
     flash of the conversation top before effects kick in).  The timeout releases
     pinnedMessageRef after mount settles, allowing later corrections if archive
     loading shifted the layout. */
  _useLayoutEffect(function () {
    // 1. Sync --scroll-pb so initial height is correct
    var el = scrollRef.current;
    var composer = composerRef.current;
    if (el && composer) {
      el.style.setProperty("--scroll-pb", composer.offsetHeight + "px");
    }
    // 2. Scroll to latest user message or to bottom (before paint)
    mountingRef.current = true;
    if (renderedMessages.length > 0) {
      var lastRendered = renderedMessages[renderedMessages.length - 1];
      if (lastRendered && lastRendered.role === 'user') {
        scrollToLatestUserMessage();
      }
    }
    var cleanup = scrollChatToBottom(true);
    // 3. Release pinnedMessageRef after settling
    var timer = setTimeout(function () {
      mountingRef.current = false;
      if (pinnedMessageRef.current) {
        pinnedMessageRef.current = false;
        scrollChatToBottom(true);
      }
    }, 600);
    return function () {
      cleanup();
      clearTimeout(timer);
    };
  }, []);

  /* Keep .chat-scroll bottom padding in sync with the fixed composer height,
     so the last message is never hidden behind the composer. */
  useEffect(function () {
    var el = scrollRef.current;
    var composer = composerRef.current;
    if (!el || !composer) return;
    function sync() {
      el.style.setProperty("--scroll-pb", composer.offsetHeight + "px");
      var scrollEl = scrollRef.current;
      if (!scrollEl) return;
      if (pinnedMessageRef.current) return;
      // If scrollTop is past the content (e.g. padding shrank after a
      // scroll-to-bottom), clamp it back to the valid range.  This prevents
      // the viewport from showing empty background ("going black") until
      // the user manually scrolls.
      var maxScroll = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight);
      if (scrollEl.scrollTop > maxScroll) {
        scrollEl.scrollTop = maxScroll;
      } else if (userAtBottomRef.current) {
        scrollEl.scrollTop = scrollEl.scrollHeight;
      }
    }
    sync();
    var ro = new ResizeObserver(sync);
    ro.observe(composer);
    return function () { ro.disconnect(); };
  }, [isLiveSession]);

  useEffect(function () {
    let cancelled = false;

    function loadCcStatus() {
      fetch("/api/cc/status")
        .then(function (response) { return response.json(); })
        .then(function (payload) {
          if (!cancelled) setCcStatus(payload);
        })
        .catch(function () {
          if (!cancelled) {
            setCcStatus({
              available: false,
              reason: "Failed to reach /api/cc/status.",
            });
          }
        });
    }

    loadCcStatus();
    const timer = window.setInterval(loadCcStatus, 15000);
    return function () {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const runtime = getChatRuntime();
    runtime.listeners.add(setRuntimeState);
    setRuntimeState(getChatRuntimeSnapshot());
    return function () { runtime.listeners.delete(setRuntimeState); };
  }, []);

  useEffect(function () {
    if (!isLiveSession) return;
    if (!runtimeState.retainedMessages || runtimeState.retainedMessages.length === 0) return;
    const nextRetained = pruneRetainedMessages(session.chat.messages, runtimeState.retainedMessages);
    if (nextRetained.length === runtimeState.retainedMessages.length) return;
    updateChatRuntime({ retainedMessages: nextRetained });
  }, [isLiveSession, session.id, session.chat.messages.length, runtimeState.retainedMessages]);

  useEffect(function () {
    if (!selectedGuideRound || !selectedGuideRound.title) return;
    if (selectedGuideRound.title === selectedGuideRoundTitle) return;
    setSelectedGuideRoundTitle(selectedGuideRound.title);
  }, [selectedGuideRound, selectedGuideRoundTitle]);

  useEffect(function () {
    if (!pendingQuestion) {
      setQuestionDraft("");
      setAnsweringQuestion(false);
      return;
    }
    setQuestionDraft("");
  }, [pendingQuestion ? pendingQuestion.id : ""]);

  useEffect(function () {
    try { localStorage.setItem("cyrene-sidebar-width", String(sidebarWidth)); } catch(e) {}
  }, [sidebarWidth]);

  useEffect(function () {
    if (!sending || !runtimeState.startedAt) return;
    const timer = window.setInterval(function () {
      setElapsedNow(Date.now());
    }, 1000);
    setElapsedNow(Date.now());
    return function () { window.clearInterval(timer); };
  }, [sending, runtimeState.startedAt]);

  useEffect(function () {
    if (!slashMenuOpen) return;
    function onDown(e) {
      if (e.target.closest(".slash-menu") || e.target.closest(".iconbtn")) return;
      setSlashMenuOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return function () { document.removeEventListener("mousedown", onDown); };
  }, [slashMenuOpen]);

  useEffect(function () {
    if (!mentionMenuOpen) return;
    function onDown(e) {
      if (e.target.closest(".mention-menu") || e.target.closest(".iconbtn")) return;
      setMentionMenuOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return function () { document.removeEventListener("mousedown", onDown); };
  }, [mentionMenuOpen]);

  function autosize(e) {
    var val = e.target.value;
    var prev = draft;
    setDraft(val);
    syncTextareaHeight(taRef.current);
    if (val.startsWith("/") && !slashMenuOpen) {
      setSlashMenuOpen(true);
      setSlashIndex(0);
    } else if (slashMenuOpen && !val.startsWith("/")) {
      setSlashMenuOpen(false);
      setSlashIndex(-1);
    } else if (slashMenuOpen && val.startsWith("/") && prev.slice(1) !== val.slice(1)) {
      setSlashIndex(0);
    }
    var slashSpace = val.match(/^\/(\S+)\s+(.+)/);
    if (slashSpace) {
      var search = slashSpace[1].toLowerCase();
      var rest = slashSpace[2];
      var matched = ALL_COMMANDS.find(function (cmd) {
        return cmd.id === search
            || cmd.label.toLowerCase() === search
            || cmd.id.indexOf(search) !== -1
            || cmd.label.toLowerCase().indexOf(search) !== -1;
      });
      if (matched) {
        setCommand(matched.id);
        setDraft(rest);
        setSlashMenuOpen(false);
        setSlashIndex(-1);
        e.target.value = rest;
      }
    }
  }

  function completeWatchedRequest(requestId) {
    const runtime = getChatRuntime();
    delete runtime.requests[requestId];
    if (runtime.watchRequestId !== requestId) return;
    const frozenFinalTrace = snapshotRuntimeTrace(runtime, {
      attachToAssistantReplyForRequestId: requestId,
    });
    updateChatRuntime({
      sending: false,
      liveProgress: [],
      startedAt: 0,
      activeRequest: null,
      watchRequestId: "",
      pendingMessages: [],
      retainedMessages: frozenFinalTrace
        ? runtime.retainedMessages.concat([frozenFinalTrace])
        : runtime.retainedMessages.slice(),
    });
    clearChatRuntimeSseSubscription();
    setNotice("");
  }

  useEffect(function () {
    if (!isLiveSession) return;
    const requestId = runtimeState.watchRequestId;
    if (!requestId) return;
    const hasAssistantReply = (session.chat.messages || []).some(function (msg) {
      return msg
        && msg.role === "agent"
        && !msg.questionPrompt
        && !msg.intermediateReply
        && String(msg.clientRequestId || "") === requestId;
    });
    if (!hasAssistantReply) return;
    completeWatchedRequest(requestId);
  }, [isLiveSession, session.id, session.chat.messages, runtimeState.watchRequestId]);

  useEffect(function () {
    if (!isLiveSession || session.status === "running") return;
    const runtime = getChatRuntime();
    const requestId = runtime.watchRequestId;
    if (!runtime.sending || !requestId || runtime.requests[requestId]) return;
    completeWatchedRequest(requestId);
  }, [isLiveSession, session.id, session.status, runtimeState.sending, runtimeState.watchRequestId]);

  async function send(options) {
    const preserveProgress = Boolean(options && options.preserveProgress);
    const activeCommand = command;
    let text = (options && options.text !== undefined) ? options.text : draft.trim();
    if (!text && activeCommand === "deep-reflect") text = "/deep-reflect";
    const curAttachments = (options && options.attachments !== undefined) ? options.attachments : attachments;
    const curGuideRoundId = (options && options.guideRoundId !== undefined) ? options.guideRoundId : selectedGuideRoundId;
    const runtime = getChatRuntime();
    if (!text && curAttachments.length === 0) return;
    if (pendingQuestion) {
      setNotice(t("chat.answerPendingWarning"));
      return;
    }
    if (uploadingAttachments) {
      setNotice(t("chat.filesStillUploading"));
      return;
    }
    setNotice("");
    runtime.requestSeq = (runtime.requestSeq || 0) + 1;
    const requestId = "req_" + Date.now() + "_" + runtime.requestSeq;
    const controller = new AbortController();
    const requestMeta = {
      id: requestId,
      message: text,
      guideRoundId: curGuideRoundId || "",
      guideRoundTitle: currentGuideRoundTitle,
      controller,
    };
    runtime.requests[requestId] = requestMeta;
    const userMsg = {
      id: "pending_user_" + Date.now(),
      role: "user", time: new Date().toLocaleTimeString(),
      body: text,
      attachments: curAttachments.slice(),
      roundId: curGuideRoundId || "",
      clientRequestId: requestId,
    };
    restoredRef.current = true;
    updateChatRuntime({
      sending: true,
      startedAt: Date.now(),
      pendingMessages: [userMsg],
      liveProgress: preserveProgress ? runtime.liveProgress.slice() : [],
      activeRequest: {
        id: requestId,
        message: text,
        guideRoundId: requestMeta.guideRoundId,
        guideRoundTitle: requestMeta.guideRoundTitle,
        guideRequestId: "",
        guidanceAccepted: false,
        finalTraceAnchorKey: "",
      },
      watchRequestId: requestId,
    });
    ensureChatRuntimeSseSubscription();
    if (!options || options.text === undefined) {
      setDraft("");
      setAttachments([]);
    }
    setCommand("");
    setMentionedAgents([]);
    if (taRef.current) taRef.current.style.height = "";

    let keepWatching = false;
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          attachments: curAttachments,
          guide_round_id: curGuideRoundId || undefined,
          client_request_id: requestId,
          stream: true,
          lang: lang,
          retry: options && options.retry || undefined,
          retry_request_id: options && options.retryRequestId || undefined,
          command: activeCommand || undefined,
          mentions: mentionedAgents.length > 0 ? mentionedAgents : undefined,
        }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      let streamCompleted = false;
      await readNdjsonStream(r, function (event) {
        const isWatching = getChatRuntime().watchRequestId === requestId;
        if (event.type === "queued") {
          keepWatching = true;
          if (runtime.requests[requestId]) {
            runtime.requests[requestId].guideRequestId = event.guide_request_id || "";
            runtime.requests[requestId].queued = true;
          }
          if (isWatching) {
            updateChatRuntime(function (state) {
              const queuedProgress = {
                icon: "↳",
                text: t("chat.guidanceQueuedProgress"),
              };
              return {
                pendingMessages: state.pendingMessages.map(function (msg) {
                  if (String(msg.clientRequestId || "") !== requestId) return msg;
                  return { ...msg, queuedGuidanceId: event.guide_request_id || "" };
                }),
                activeRequest: state.activeRequest && state.activeRequest.id === requestId
                  ? {
                      ...state.activeRequest,
                      guideRequestId: event.guide_request_id || "",
                      queued: true,
                    }
                  : state.activeRequest,
                liveProgress: state.liveProgress.concat([queuedProgress]).slice(-30),
              };
            });
          }
          return;
        }
        if (event.type === "awaiting_user") {
          streamCompleted = true;
          delete runtime.requests[requestId];
          if (isWatching) {
            updateChatRuntime({
              sending: false,
              liveProgress: [],
              startedAt: 0,
              activeRequest: null,
              watchRequestId: "",
              pendingMessages: [],
            });
            clearChatRuntimeSseSubscription();
          }
          return;
        }
        if (event.type === "reply_start") {
          if (isWatching) upsertStreamingAgentMessage(requestId, "", false);
          return;
        }
        if (event.type === "reply_delta") {
          if (isWatching) upsertStreamingAgentMessage(requestId, event.delta || "", false);
          return;
        }
        if (event.type === "reply_done") {
          streamCompleted = true;
          if (isWatching) {
            upsertStreamingAgentMessage(requestId, "", true);
            delete runtime.requests[requestId];
            const frozenFinalTrace = snapshotRuntimeTrace(getChatRuntime(), {
              attachToAssistantReplyForRequestId: requestId,
            });
            updateChatRuntime({
              sending: false,
              liveProgress: [],
              startedAt: 0,
              activeRequest: null,
              watchRequestId: "",
              retainedMessages: frozenFinalTrace
                ? getChatRuntime().retainedMessages.concat([frozenFinalTrace])
                : getChatRuntime().retainedMessages.slice(),
            });
            clearChatRuntimeSseSubscription();
          }
        }
        if (event.type === "error") {
          // The agent run failed (model timeout/5xx/network). Surface it as a
          // visible system message instead of silently ending on a "done" — #7.
          streamCompleted = true;
          if (isWatching) {
            delete runtime.requests[requestId];
            updateChatRuntime(function (state) {
              return {
                sending: false,
                liveProgress: [],
                startedAt: 0,
                activeRequest: null,
                watchRequestId: "",
                pendingMessages: state.pendingMessages.concat([{
                  id: "err_" + Date.now(),
                  role: "system",
                  time: new Date().toLocaleTimeString(),
                  body: t("chat.modelFailed", { error: event.message || event.error || "unknown error" }),
                }]),
              };
            });
            clearChatRuntimeSseSubscription();
          }
          return;
        }
      });
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      if (streamCompleted) {
        updateChatRuntime(function (state) {
          return {
            pendingMessages: (state.pendingMessages || []).filter(function (msg) {
              return String(msg.clientRequestId || "") !== requestId;
            }),
          };
        });
      }
    } catch (e) {
      if (!isAbortError(e) && getChatRuntime().watchRequestId === requestId) {
        updateChatRuntime(function (state) {
          return {
            pendingMessages: state.pendingMessages.concat([{
              id: "err_" + Date.now(),
              role: "system", time: new Date().toLocaleTimeString(),
              body: "Error: " + e.message,
            }]),
          };
        });
      }
    } finally {
      if (options && options.retryRequestId) {
        var _rt = getChatRuntime();
        var _ridx = _rt.retiredRequestIds.indexOf(options.retryRequestId);
        if (_ridx !== -1) {
          var _newIds = _rt.retiredRequestIds.slice();
          _newIds.splice(_ridx, 1);
          updateChatRuntime({ retiredRequestIds: _newIds });
        }
      }
      if (!keepWatching) {
        delete runtime.requests[requestId];
        if (getChatRuntime().watchRequestId === requestId) {
          updateChatRuntime({
            sending: false,
            liveProgress: [],
            startedAt: 0,
            activeRequest: null,
            watchRequestId: "",
          });
          clearChatRuntimeSseSubscription();
        }
      }
    }
  }

  async function submitQuestionAnswer(options) {
    if (!pendingQuestion || answeringQuestion) return;
    const selectedOption = String(options && options.selectedOption || "");
    const text = String(options && options.text || questionDraft || "").trim() || selectedOption;
    if (!text) return;

    setNotice("");
    setAnsweringQuestion(true);

    const runtime = getChatRuntime();
    runtime.requestSeq = (runtime.requestSeq || 0) + 1;
    const requestId = "req_" + Date.now() + "_" + runtime.requestSeq;
    const controller = new AbortController();
    runtime.requests[requestId] = {
      id: requestId,
      message: text,
      controller,
      questionId: pendingQuestion.id,
    };

    const userMsg = {
      id: "pending_answer_" + Date.now(),
      role: "user",
      time: new Date().toLocaleTimeString(),
      body: text,
      roundId: pendingQuestion.roundId || "",
      clientRequestId: requestId,
    };

    restoredRef.current = true;
    updateChatRuntime({
      sending: true,
      startedAt: Date.now(),
      pendingMessages: [userMsg],
      liveProgress: [],
      activeRequest: {
        id: requestId,
        message: text,
        guideRoundId: "",
        guideRoundTitle: "",
        guideRequestId: "",
        guidanceAccepted: false,
        finalTraceAnchorKey: "",
      },
      watchRequestId: requestId,
    });
    ensureChatRuntimeSseSubscription();

    try {
      const r = await fetch("/api/chat/answer-question", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          question_id: pendingQuestion.id,
          answer: text,
          selected_option: selectedOption || undefined,
          client_request_id: requestId,
          stream: true,
        }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      let streamCompleted = false;
      await readNdjsonStream(r, function (event) {
        const isWatching = getChatRuntime().watchRequestId === requestId;
        if (event.type === "awaiting_user") {
          streamCompleted = true;
          delete runtime.requests[requestId];
          if (isWatching) {
            updateChatRuntime({
              sending: false,
              liveProgress: [],
              startedAt: 0,
              activeRequest: null,
              watchRequestId: "",
              pendingMessages: [],
            });
            clearChatRuntimeSseSubscription();
          }
          return;
        }
        if (event.type === "reply_start") {
          if (isWatching) upsertStreamingAgentMessage(requestId, "", false);
          return;
        }
        if (event.type === "reply_delta") {
          if (isWatching) upsertStreamingAgentMessage(requestId, event.delta || "", false);
          return;
        }
        if (event.type === "reply_done") {
          streamCompleted = true;
          if (isWatching) {
            upsertStreamingAgentMessage(requestId, "", true);
            delete runtime.requests[requestId];
            const frozenFinalTrace = snapshotRuntimeTrace(getChatRuntime(), {
              attachToAssistantReplyForRequestId: requestId,
            });
            updateChatRuntime({
              sending: false,
              liveProgress: [],
              startedAt: 0,
              activeRequest: null,
              watchRequestId: "",
              retainedMessages: frozenFinalTrace
                ? getChatRuntime().retainedMessages.concat([frozenFinalTrace])
                : getChatRuntime().retainedMessages.slice(),
            });
            clearChatRuntimeSseSubscription();
          }
        }
        if (event.type === "error") {
          // The agent run failed (model timeout/5xx/network). Surface it as a
          // visible system message instead of silently ending on a "done" — #7.
          streamCompleted = true;
          if (isWatching) {
            delete runtime.requests[requestId];
            updateChatRuntime(function (state) {
              return {
                sending: false,
                liveProgress: [],
                startedAt: 0,
                activeRequest: null,
                watchRequestId: "",
                pendingMessages: state.pendingMessages.concat([{
                  id: "err_" + Date.now(),
                  role: "system",
                  time: new Date().toLocaleTimeString(),
                  body: t("chat.modelFailed", { error: event.message || event.error || "unknown error" }),
                }]),
              };
            });
            clearChatRuntimeSseSubscription();
          }
          return;
        }
      });
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      if (streamCompleted) {
        updateChatRuntime(function (state) {
          return {
            pendingMessages: (state.pendingMessages || []).filter(function (msg) {
              return String(msg.clientRequestId || "") !== requestId;
            }),
          };
        });
      }
      setQuestionDraft("");
    } catch (e) {
      if (!isAbortError(e) && getChatRuntime().watchRequestId === requestId) {
        updateChatRuntime(function (state) {
          return {
            pendingMessages: state.pendingMessages.concat([{
              id: "err_" + Date.now(),
              role: "system",
              time: new Date().toLocaleTimeString(),
              body: "Error: " + e.message,
            }]),
          };
        });
      }
    } finally {
      delete runtime.requests[requestId];
      if (getChatRuntime().watchRequestId === requestId) {
        updateChatRuntime({
          sending: false,
          liveProgress: [],
          startedAt: 0,
          activeRequest: null,
          watchRequestId: "",
        });
        clearChatRuntimeSseSubscription();
      }
      setAnsweringQuestion(false);
    }
  }

  function releaseWatchedRequest(nextNotice, options) {
    const runtime = getChatRuntime();
    if (!runtime.watchRequestId) return;
    const retain = Boolean(options && options.retainMessages);
    const retained = retain ? runtime.retainedMessages.concat(runtime.pendingMessages) : runtime.retainedMessages;
    updateChatRuntime({
      sending: false,
      startedAt: 0,
      pendingMessages: [],
      retainedMessages: retained,
      liveProgress: [],
      activeRequest: null,
      watchRequestId: "",
    });
    clearChatRuntimeSseSubscription();
    if (nextNotice) setNotice(nextNotice);
  }

  function openNextDialogue() {
    if (!sending || !draft.trim()) return;
    if (hasSelectedGuideRound) {
      updateChatRuntime(function (state) {
        const preservedPending = state.pendingMessages.slice();
        const anchorKey = preservedPending.length
          ? messageKey(preservedPending[preservedPending.length - 1])
          : (allMessages.length ? messageKey(allMessages[allMessages.length - 1]) : "");
        const frozenCurrentTrace = snapshotRuntimeTrace(state, { insertAfterKey: anchorKey });
        return {
          retainedMessages: state.retainedMessages
            .concat(preservedPending)
            .concat(frozenCurrentTrace ? [frozenCurrentTrace] : []),
        };
      });
      send();
      return;
    }
    releaseWatchedRequest(
      hasSelectedGuideRound
        ? t("chat.runContinuingBgGuide")
        : t("chat.runContinuingBg"),
      { retainMessages: true }
    );
    send();
  }

  async function stopActiveRun() {
    const runtime = getChatRuntime();
    const requestId = runtime.watchRequestId;
    const requestMeta = requestId ? runtime.requests[requestId] : null;
    if (!requestId) return;
    if (requestMeta) {
      requestMeta.controller.abort();
      delete runtime.requests[requestId];
      setDraft(requestMeta.message || "");
      setAttachments([]);
      setSelectedGuideRoundId(requestMeta.guideRoundId || "");
      setSelectedGuideRoundTitle(requestMeta.guideRoundTitle || "");
    } else {
      // Restored mode (page refresh): no local requestMeta, just clear state
      setDraft("");
      setAttachments([]);
      setSelectedGuideRoundId("");
      setSelectedGuideRoundTitle("");
    }
    releaseWatchedRequest("", { retainMessages: false });
    setContextPickerOpen(false);
    setNotice(t("chat.stoppedRequest"));
    window.requestAnimationFrame(function () {
      syncTextareaHeight(taRef.current);
      if (taRef.current) taRef.current.focus();
    });
    try {
      await fetch("/api/chat/interrupt", { method: "POST" });
    } catch (_e) {
      /* best effort */
    }
  }

  function onKey(e) {
    if (slashMenuOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex(function (prev) { return prev < filteredCommands.length - 1 ? prev + 1 : 0; });
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex(function (prev) { return prev > 0 ? prev - 1 : filteredCommands.length - 1; });
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && slashIndex >= 0 && slashIndex < filteredCommands.length) {
        e.preventDefault();
        setCommand(filteredCommands[slashIndex].id);
        setSlashMenuOpen(false);
        setSlashIndex(-1);
        return;
      }
      if (e.key === "Escape") {
        setSlashMenuOpen(false);
        setSlashIndex(-1);
        e.preventDefault();
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  }

  function onQuestionKey(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitQuestionAnswer();
    }
  }

  async function handleAttachmentPick(event) {
    const pickedFiles = Array.from(event.target.files || []);
    if (pickedFiles.length === 0) return;
    const formData = new FormData();
    pickedFiles.forEach(function (file) {
      formData.append("files", file);
    });
    setUploadingAttachments(true);
    setNotice("");
    try {
      const response = await fetch("/api/chat/upload", {
        method: "POST",
        body: formData,
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      setAttachments(function (prev) {
        return prev.concat(Array.isArray(payload.files) ? payload.files : []);
      });
    } catch (error) {
      setNotice(t("chat.uploadFailed", { error: error.message }));
    } finally {
      setUploadingAttachments(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function removeAttachment(index) {
    setAttachments(function (prev) {
      return prev.filter(function (_item, i) { return i !== index; });
    });
  }

  const allMessages = isLiveSession
    ? mergeMessagesWithAnchors(
        [...session.chat.messages, ...visiblePendingMessages],
        retainedMessages
      )
    : session.chat.messages;
  const messagesWithRetainedRuntime = allMessages.map(function (msg) {
    const requestId = String(msg && msg.clientRequestId || "");
    const retainedRuntime = requestId ? retainedRuntimeAttachments.get(requestId) : null;
    if (!retainedRuntime || msg.role !== "agent" || msg.runtimeTrace) return msg;
    return { ...msg, attachedRuntime: retainedRuntime };
  });
  const lastMessage = messagesWithRetainedRuntime.length ? messagesWithRetainedRuntime[messagesWithRetainedRuntime.length - 1] : null;
  const runtimeAttachedToLastMessage = visibleSending && canAttachRuntimeToLastMessage(lastMessage, activeRequest, session);
  const renderedMessages = runtimeAttachedToLastMessage
    ? messagesWithRetainedRuntime.slice(0, -1).concat([{
        ...lastMessage,
        attachedRuntime: buildAttachedRuntime(
          activeTraceDescriptor,
          liveElapsed,
          visibleLiveProgress,
          watchingGuidance,
          activeGuideRoundTitle,
          activeRequest
        ),
      }])
    : messagesWithRetainedRuntime;
  const renderedMessageEntries = renderMessageEntries(renderedMessages);
  const renderedMessageSignature = renderedMessages.map(function (msg) {
    return [
      messageKey(msg),
      msg && msg.streamingReply ? "streaming" : "done",
      msg && msg.attachedRuntime ? String(msg.attachedRuntime.elapsed || "") : "",
    ].join(":");
  }).join("|");
  const hasStreamingReply = renderedMessages.some(function (msg) {
    return Boolean(msg && msg.streamingReply && String(msg.body || "").trim());
  });
  const hasAssistantReplyBody = renderedMessages.some(function (msg) {
    return Boolean(
      msg
      && isFinalVisibleAssistantReply(msg)
      && String(msg.clientRequestId || "") === String(runtimeState.watchRequestId || "")
    );
  });

  _useLayoutEffect(function () {
    if (!isLiveSession || visibleSending || pendingQuestion || renderedMessages.length !== 0) return;
    resetEmptyConversationViewport();
  }, [isLiveSession, visibleSending, Boolean(pendingQuestion), renderedMessages.length, session.started, session.chat.messages.length, retainedMessages.length, visiblePendingMessages.length]);

  useEffect(function () {
    // When the latest message is from the user, keep it pinned at the top
    // instead of scrolling to bottom — scrollToLatestUserMessage handles this case.
    if (renderedMessages.length === 0) {
      resetEmptyConversationViewport();
      return;
    }
    var lastRendered = renderedMessages[renderedMessages.length - 1];
    if (lastRendered && lastRendered.role === 'user') return;
    return scrollChatToBottom(!visibleSending);
  }, [renderedMessageSignature, visibleSending]);

  // Release pinnedMessageRef when the agent finishes replying, and clear the
  // temporary extra bottom padding (no longer needed once reply content exists).
  useEffect(function () {
    if (!visibleSending && pinnedMessageRef.current) {
      pinnedMessageRef.current = false;
      var el = scrollRef.current;
      if (el) el.style.setProperty('--scroll-pb-extra', '0px');
    }
  }, [visibleSending]);

  /* Wheel listener: user scroll releases the auto-scroll pin. */
  useEffect(function () {
    var el = scrollRef.current;
    if (!el || !isLiveSession) return;
    function onWheel(e) {
      // Any user wheel scroll releases the pinned message — the user is
      // taking control, so auto-scrolling can resume.
      if (pinnedMessageRef.current) pinnedMessageRef.current = false;
    }
    el.addEventListener('wheel', onWheel, {passive: true});
    return function () { el.removeEventListener('wheel', onWheel); };
  }, [isLiveSession, session.id]);

  function onChatScroll() {
    var el = scrollRef.current;
    if (!el) return;
    userAtBottomRef.current = isNearBottom(el, 60);
  }

  function expandRightSidebar() {
    if (rightSidebarCollapsed && setRightSidebarCollapsed) setRightSidebarCollapsed(false);
  }

  // Auto-reveal the live browser panel when the agent starts browsing (M2).
  const _browserState = window.DATA && window.DATA.browser;
  const browserActivityKey = _browserState && _browserState.active ? ("on:" + (_browserState.roundId || "")) : "off";
  useEffect(function () {
    if (browserActivityKey === "off") return;
    addSidebarTab("browser");
    expandRightSidebar();
    if (setRightSidebarView) setRightSidebarView("browser");
  }, [browserActivityKey]);

  const currentRoundHasMapActivity = (session.chat.messages || []).some(function (msg) {
    if (!msg || msg.role !== "agent" || !messageHasMapTool(msg)) return false;
    if (!session.currentRoundId) return true;
    return String(msg.roundId || "") === String(session.currentRoundId || "");
  });
  useEffect(function () {
    if (!currentRoundHasMapActivity) return;
    addSidebarTab("map");
    expandRightSidebar();
    if (setRightSidebarView) setRightSidebarView("map");
  }, [currentRoundHasMapActivity, session.currentRoundId]);

  function handleShowHtml(content) {
    expandRightSidebar();
    addSidebarTab("html");
    setActiveHtmlContent(content);
    setHtmlViewTab("rendered");
    setRightSidebarView("html");
  }

  function handleShowPdf(url, name) {
    expandRightSidebar();
    addSidebarTab("pdf");
    setActivePdfUrl(url);
    setActivePdfName(name || "");
    setRightSidebarView("pdf");
  }

  function handleShowPpt(url, name) {
    expandRightSidebar();
    addSidebarTab("ppt");
    setActivePptUrl(url);
    setActivePptName(name || "");
    setRightSidebarView("ppt");
  }

  function handleShowMap() {
    expandRightSidebar();
    addSidebarTab("map");
    setRightSidebarView("map");
  }

  function handleShowCode(url, name) {
    expandRightSidebar();
    addSidebarTab("code-editor");
    fetch(url).then(function(r) { return r.text(); }).then(function(code) {
      setEditorData({ code: code, language: "", filePath: name || "" });
      setRightSidebarView("code-editor");
    }).catch(function() {});
  }

  function handleShowMarkdown(url, name) {
    expandRightSidebar();
    addSidebarTab("markdown");
    fetch(url).then(function(r) { return r.text(); }).then(function(md) {
      setActiveMarkdownContent(md);
      setActiveMarkdownName(name || "");
      setRightSidebarView("markdown");
    }).catch(function() {});
  }

  function uniqueMutationPathsFromTools(tools) {
    const seen = new Set();
    const paths = [];
    (tools || []).forEach(function (tool) {
      if (!isCodeMutationTool(tool)) return;
      const filePath = extractToolFilePath(tool && tool.rawArgs);
      if (!filePath || seen.has(filePath)) return;
      seen.add(filePath);
      paths.push(filePath);
    });
    return paths;
  }

  function uniqueMutationPathsFromProgress(progressEntries) {
    const seen = new Set();
    const paths = [];
    (progressEntries || []).forEach(function (entry) {
      if (!entry || entry.type !== "tool_call") return;
      const tool = { name: entry.tool, status: "done", rawArgs: entry.args || {} };
      if (!isCodeMutationTool(tool)) return;
      const filePath = extractToolFilePath(tool.rawArgs);
      if (!filePath || seen.has(filePath)) return;
      seen.add(filePath);
      paths.push(filePath);
    });
    return paths;
  }

  async function refreshMutationDiffForPaths(paths, signature, requestId) {
    const uniquePaths = (paths || []).filter(Boolean);
    const diffRequestId = String(requestId || (signature ? String(signature).split(":")[0] : "") || "");
    const cachedDiff = diffRequestId ? mutationDiffsByRequest[diffRequestId] : null;
    if (uniquePaths.length === 0 || !signature || signature === mutationDiff.signature || (cachedDiff && cachedDiff.signature === signature)) return;
    const diffs = [];
    for (let i = 0; i < uniquePaths.length; i += 1) {
      const path = uniquePaths[i];
      try {
        const r = await fetch("/api/code/git-diff", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path }),
        });
        if (!r.ok) {
          if (r.status === 404) gitDiffUnavailableRef.current = true;
          continue;
        }
        const data = await r.json();
        if (data && data.has_changes && data.diff) diffs.push(data.diff);
      } catch (_e) {}
    }
    const diff = diffs.join("\n");
    if (!diff.trim()) return;
    setMutationDiff({ diff: diff, signature: signature });
    if (diffRequestId) {
      setMutationDiffsByRequest(function (prev) {
        if (prev[diffRequestId] && prev[diffRequestId].signature === signature) return prev;
        return {
          ...prev,
          [diffRequestId]: {
            diff: diff,
            signature: signature,
          },
        };
      });
    }
    setDiffData({ diff: diff, mode: "text", left: "", right: "" });
    addSidebarTab("diff-viewer");
  }

  function refreshMutationDiffForTools(tools, signature, requestId) {
    const paths = uniqueMutationPathsFromTools(tools);
    refreshMutationDiffForPaths(paths, signature, requestId);
  }

  function openMutationDiff(diffText, fileName) {
    const diff = String(diffText || mutationDiff.diff || "");
    if (!diff.trim()) return;
    expandRightSidebar();
    addSidebarTab("diff-viewer");
    setDiffData({
      diff: diff,
      mode: "text",
      left: fileName || "",
      right: fileName || "",
    });
    setRightSidebarView("diff-viewer");
  }

  function retryMessage(retryData) {
    if (!retryData || !retryData.requestId) return;
    var _runtime = getChatRuntime();
    if (_runtime.retiredRequestIds.indexOf(retryData.requestId) === -1) {
      updateChatRuntime({ retiredRequestIds: _runtime.retiredRequestIds.concat([retryData.requestId]) });
    }
    send({
      text: retryData.text,
      attachments: retryData.attachments,
      guideRoundId: "",
      retry: true,
      retryRequestId: retryData.requestId,
    });
  }

  var visibleChips = (session.chat.contextChips || []).filter(function (c) { return !hiddenContexts[contextKey(c)]; });
  var visibleKeys = visibleChips.map(function (c) { return contextKey(c); });
  var addableContexts = [];
  if (visibleKeys.indexOf("soul") === -1) addableContexts.push({ icon: "🧠", key: "soul", hasPicker: false });
  if (visibleKeys.indexOf("workspace") === -1) addableContexts.push({ icon: "📁", key: "workspace", hasPicker: true });
  var hasAddable = addableContexts.length > 0 || liveRounds.length > 0;

  return (
    <div className={"chat-layout" + (rightSidebarCollapsed ? " right-collapsed" : "")} style={{ "--chat-right-panel-width": sidebarWidth + "px" }}>
      <div className="chat-main">
        {ccModal ? (
          <window.CCTerminalPanel
            statusInfo={{
              available: true,
              tmux_session: ccModal.tmuxSession,
              reason: "",
              can_launch: false,
              latest_jsonl: ccModal.latestJsonl || "",
            }}
            modal={true}
            onClose={function () { setCcModal(null); }}
            onRefresh={function () {
              if (typeof window.refreshSessions === "function") window.refreshSessions();
              if (typeof window.refreshStatus === "function") window.refreshStatus();
            }}
          />
        ) : shellModal ? (
          <ShellTerminalPanel
            shell={shellModal}
            onClose={function () { setShellModal(null); }}
            onRefresh={function () {
              if (typeof window.refreshSessions === "function") window.refreshSessions();
            }}
          />
        ) : (
        <>
        <ModernConversation
          scrollRef={scrollRef}
          onScroll={onChatScroll}
          renderedMessageEntries={renderedMessageEntries}
          renderedMessages={renderedMessages}
          pendingQuestion={pendingQuestion}
          visibleSending={visibleSending}
          hasStreamingReply={hasStreamingReply}
          hasAssistantReplyBody={hasAssistantReplyBody}
          activeRequestId={String(runtimeState.watchRequestId || (mutationDiff.signature ? String(mutationDiff.signature).split(":")[0] : "") || "")}
          visibleLiveProgress={visibleLiveProgress}
          visibleNotice={visibleNotice}
          mutationDiff={mutationDiff}
          mutationDiffsByRequest={mutationDiffsByRequest}
          assistantName={DATA.assistantName}
          isLiveSession={isLiveSession}
          onRetryMessage={retryMessage}
          onOpenDiff={openMutationDiff}
          onShowHtml={handleShowHtml}
          onShowPdf={handleShowPdf}
          onShowPpt={handleShowPpt}
          onShowMap={handleShowMap}
          onShowCode={handleShowCode}
          onShowMarkdown={handleShowMarkdown}
        />
        <ModernChatComposer
          isLiveSession={isLiveSession}
          onReturnLive={function () { onSelectSession && onSelectSession(null); }}
          composerRef={composerRef}
          pendingQuestion={pendingQuestion}
          questionDraft={questionDraft}
          onQuestionDraftChange={setQuestionDraft}
          onQuestionOptionSelect={function (label) { submitQuestionAnswer({ selectedOption: label }); }}
          onQuestionSubmit={function () { submitQuestionAnswer(); }}
          onQuestionKeyDown={onQuestionKey}
          answeringQuestion={answeringQuestion}
          questionOptionCount={questionOptionCount}
          visibleChips={visibleChips}
          contextDisplayLabel={contextDisplayLabel}
          contextKey={contextKey}
          removeContext={removeContext}
          hasSelectedGuideRound={hasSelectedGuideRound}
          currentGuideRoundTitle={currentGuideRoundTitle}
          setSelectedGuideRoundId={setSelectedGuideRoundId}
          setSelectedGuideRoundTitle={setSelectedGuideRoundTitle}
          setContextPickerOpen={setContextPickerOpen}
          command={command}
          findCommand={findCommand}
          setCommand={setCommand}
          hasAddable={hasAddable}
          contextPickerOpen={contextPickerOpen}
          addableContexts={addableContexts}
          addContext={addContext}
          workspaceHistory={workspaceHistory}
          pickWorkspaceDir={pickWorkspaceDir}
          liveRounds={liveRounds}
          selectedGuideRoundId={selectedGuideRoundId}
          mentionedAgents={mentionedAgents}
          setMentionedAgents={setMentionedAgents}
          runningSubagents={runningSubagents}
          assistantName={DATA.assistantName}
          session={session}
          taRef={taRef}
          draft={draft}
          onDraftChange={autosize}
          onKeyDown={onKey}
          attachments={attachments}
          removeAttachment={removeAttachment}
          fileInputRef={fileInputRef}
          onAttachmentPick={handleAttachmentPick}
          uploadingAttachments={uploadingAttachments}
          slashMenuOpen={slashMenuOpen}
          setSlashMenuOpen={setSlashMenuOpen}
          filteredCommands={filteredCommands}
          slashIndex={slashIndex}
          setSlashIndex={setSlashIndex}
          mentionMenuOpen={mentionMenuOpen}
          setMentionMenuOpen={setMentionMenuOpen}
          visibleSending={visibleSending}
          openNextDialogue={openNextDialogue}
          stopActiveRun={stopActiveRun}
          send={send}
        />
        </>
        )}
      </div>

      <ChatSide
        session={session}
        subagents={subagents}
        ccStatus={ccStatus}
        refreshCcStatus={function () {
          fetch("/api/cc/status")
            .then(function (response) { return response.json(); })
            .then(function (payload) { setCcStatus(payload); })
            .catch(function () {});
        }}
        onOpenCCModal={function (info) { setCcModal(info); }}
        onOpenShellModal={function (shell) { setShellModal(shell); }}
        view={rightSidebarView}
        onViewChange={setRightSidebarView}
        roundId={session.currentRoundId}
        onResize={setSidebarWidth}
        activeHtmlContent={activeHtmlContent}
        activePdfUrl={activePdfUrl}
        activePdfName={activePdfName}
        activePptUrl={activePptUrl}
        activePptName={activePptName}
        htmlViewTab={htmlViewTab}
        onHtmlViewTabChange={setHtmlViewTab}
        editorData={editorData}
        diffData={diffData}
        activeMarkdownContent={activeMarkdownContent}
        activeMarkdownName={activeMarkdownName}
        sidebarTabs={sidebarTabs}
      />
    </div>
  );
}

function extractToolFilePath(rawArgs) {
  if (!rawArgs || typeof rawArgs !== "object") return "";
  var pathKeys = [
    "path",
    "file_path",
    "filepath",
    "filePath",
    "filename",
    "file",
    "target_file",
    "targetFile",
  ];
  for (var i = 0; i < pathKeys.length; i++) {
    var value = rawArgs[pathKeys[i]];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function isLikelyCodePath(filePath) {
  var path = String(filePath || "").trim().toLowerCase();
  if (!path) return false;
  var codeLikeNames = [
    "dockerfile",
    "makefile",
    "jenkinsfile",
    "procfile",
    ".gitignore",
    ".editorconfig",
  ];
  var codeLikeExts = [
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".swift", ".rb", ".php", ".go", ".rs",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh",
    ".cs", ".scala", ".sh", ".bash", ".zsh", ".fish",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini",
    ".xml", ".sql", ".vue", ".svelte",
  ];
  for (var i = 0; i < codeLikeNames.length; i++) {
    if (path === codeLikeNames[i] || path.endsWith("/" + codeLikeNames[i])) return true;
  }
  for (var j = 0; j < codeLikeExts.length; j++) {
    if (path.endsWith(codeLikeExts[j])) return true;
  }
  return /(^|\/)(src|app|lib|pkg|cmd|internal|server|client|tests?|spec)\//.test(path);
}

function isCodeMutationTool(tool) {
  var name = String(tool && tool.name || "").toLowerCase();
  if (name !== "write" && name !== "edit") return false;
  if (String(tool && tool.status || "").toLowerCase() !== "done") return false;
  return isLikelyCodePath(extractToolFilePath(tool && tool.rawArgs));
}

function ChatSide({ session, subagents, ccStatus, refreshCcStatus, onOpenCCModal, onOpenShellModal, view = "overview", onViewChange, roundId, onResize, activeHtmlContent, activePdfUrl, activePdfName, activePptUrl, activePptName, htmlViewTab, onHtmlViewTabChange, editorData, diffData, activeMarkdownContent, activeMarkdownName, sidebarTabs }) {
  const { t } = useI18n();
  const sideRef = useRef(null);
  const tabs = sidebarTabs || new Set();
  const hasHtmlContent = Boolean(activeHtmlContent);
  const hasPdfContent = Boolean(activePdfUrl);
  const hasPptContent = Boolean(activePptUrl);
  const hasMarkdownContent = Boolean(activeMarkdownContent);

  const viewOptions = [{ id: "overview", label: t("chat.side.overview") }];
  if (tabs.has("html"))        viewOptions.push({ id: "html",        label: t("chat.html.sideTitle") });
  if (tabs.has("pdf"))         viewOptions.push({ id: "pdf",         label: t("chat.pdf.sideTitle") });
  if (tabs.has("ppt"))         viewOptions.push({ id: "ppt",         label: t("chat.ppt.sideTitle") });
  if (tabs.has("markdown"))    viewOptions.push({ id: "markdown",    label: t("chat.md.sideTitle") });
  if (subagents.length > 0)    viewOptions.push({ id: "agents",      label: t("chat.side.agents") });
  if (session.shells.length > 0) viewOptions.push({ id: "shells",    label: t("chat.side.shells") });
  if (tabs.has("map"))         viewOptions.push({ id: "map",         label: t("chat.side.map") });
  if (tabs.has("code-editor")) viewOptions.push({ id: "code-editor", label: t("chat.side.codeEditor") });
  if (tabs.has("diff-viewer")) viewOptions.push({ id: "diff-viewer", label: t("chat.side.diffViewer") });
  if (tabs.has("browser"))     viewOptions.push({ id: "browser",     label: t("chat.side.browser") });

  const viewIds = viewOptions.map(function (o) { return o.id; });

  useEffect(function () {
    if (viewIds.indexOf(view) === -1) {
      onViewChange && onViewChange("overview");
    }
  }, [view, viewIds.join(",")]);

  function onHandleMouseDown(e) {
    e.preventDefault();
    var startX = e.clientX;
    var startWidth = sideRef.current ? sideRef.current.getBoundingClientRect().width : 320;
    var layout = sideRef.current && sideRef.current.parentElement;
    if (layout) layout.classList.add("resizing");
    function onMove(mv) {
      var delta = startX - mv.clientX;
      var newWidth = Math.min(520, Math.max(200, startWidth + delta));
      onResize && onResize(newWidth);
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (layout) layout.classList.remove("resizing");
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  function renderSideContent() {
    if (view === "html" && hasHtmlContent) {
      return <div className="side-section" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        <HtmlViewPanel htmlContent={activeHtmlContent} tab={htmlViewTab} onTabChange={onHtmlViewTabChange} />
      </div>;
    }
    if (view === "pdf" && hasPdfContent) {
      return <div className="side-section" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        <PdfViewPanel pdfUrl={activePdfUrl} pdfName={activePdfName} />
      </div>;
    }
    if (view === "ppt" && hasPptContent) {
      return <div className="side-section" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        <PdfViewPanel pdfUrl={activePptUrl} pdfName={activePptName} />
      </div>;
    }
    if (view === "shells") {
      return <div className="side-section" style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column" }}>
        <div className="side-head">
          {t("chat.activeShells")}
          <span className="count">{session.shells.length}</span>
        </div>
        {session.shells.length === 0 && (
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)" }}>—</div>
        )}
        {session.shells.map((s) => <ShellCard key={s.id} shell={s} ccStatus={ccStatus} onOpenCCModal={onOpenCCModal} onOpenShellModal={onOpenShellModal} />)}
      </div>;
    }
    if (view === "agents") {
      return <div className="side-section" style={{ flex: 1, overflowY: "auto", padding: 0, display: "flex", flexDirection: "column" }}>
        <AgentGroupChat roundId={roundId} subagents={subagents} session={session} />
      </div>;
    }
    if (view === "markdown" && hasMarkdownContent) {
      return <div className="side-section" style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        {activeMarkdownName && (
          <div style={{ padding: "10px 14px 6px", fontSize: 11, color: "var(--text-3)", fontFamily: "var(--mono)", borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
            {activeMarkdownName}
          </div>
        )}
        <div className="msg-body markdown" style={{ padding: "14px 16px", flex: 1 }} dangerouslySetInnerHTML={{ __html: renderMarkdown(activeMarkdownContent) }} />
      </div>;
    }
    if (view === "map") {
      return <div className="side-section" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        <MapView />
      </div>;
    }
    if (view === "code-editor") {
      return <div className="side-section side-section--flush" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        {typeof CodeEditorPanel !== "undefined" && React.createElement(CodeEditorPanel, {
          code: editorData.code,
          language: editorData.language,
          filePath: editorData.filePath,
          onClose: function () { onViewChange("overview"); },
        })}
      </div>;
    }
    if (view === "diff-viewer") {
      return <div className="side-section side-section--flush" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        {typeof DiffViewerPanel !== "undefined" && React.createElement(DiffViewerPanel, {
          diff: diffData.diff,
          mode: diffData.mode,
          left: diffData.left,
          right: diffData.right,
          onClose: function () { onViewChange("overview"); },
        })}
      </div>;
    }
    if (view === "browser") {
      return <div className="side-section side-section--flush" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", borderBottom: 0 }}>
        {typeof window.BrowserViewportPanel !== "undefined" && React.createElement(window.BrowserViewportPanel, {
          roundId: roundId,
          onClose: function () { onViewChange("overview"); },
        })}
      </div>;
    }
    // default: overview
    return <div className="side-section" style={{ borderBottom: 0 }}>
      <SideTokenRing tokens={session.summary.tokens} />
      {(() => {
        var total = session.main_agent_context_tokens != null ? session.main_agent_context_tokens : (session.main_agent_total_tokens != null ? session.main_agent_total_tokens : session.summary.total_tokens);
        var limit = session.ctx_limit || 0;
        if (limit > 0 && total != null) {
          var pct = Math.min(Math.round(total / limit * 100), 100);
          var barColor = pct > 90 ? "#e74c3c" : pct > 70 ? "#f39c12" : "#4caf50";
          var fmt = function (n) {
            return n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
          };
          return (
            <div className="ctx-bar-overview">
              <span className="ctx-bar-overview-label">上下文</span>
              <span className="ctx-bar-overview-track">
                <span className="ctx-bar-overview-fill" style={{ width: pct + "%", background: barColor }}></span>
              </span>
              <span className="ctx-bar-overview-nums">{fmt(total)} / {fmt(limit)}</span>
            </div>
          );
        }
        return null;
      })()}
      <div className="side-head">{t("chat.runSummary")}</div>
      <div className="side-overview-kv">
        <span className="k">{t("chat.runId")}</span><span className="v">{session.id}</span>
        <span className="k">{t("chat.started")}</span><span className="v">{session.started}</span>
        <span className="k">{t("chat.elapsed")}</span><span className="v">{session.dur}</span>
        <span className="k">{t("chat.toolCalls")}</span><span className="v">{session.summary.toolCalls}</span>
        <span className="k">{t("chat.spend")}</span><span className="v">{session.summary.spend}</span>
      </div>
      <SideModelUsage />
    </div>;
  }

  return (
    <div className="chat-side" ref={sideRef}>
      <div className="chat-side-resize-handle" onMouseDown={onHandleMouseDown} />
      <div className="chat-side-inner">
        <div className={"chat-side-switcher" + (viewOptions.length <= 1 ? " single" : "")}>
          {viewOptions.map(function (item) {
            return (
              <button
                key={item.id}
                type="button"
                className={view === item.id ? "active" : ""}
                onClick={function () { onViewChange && onViewChange(item.id); }}
              >
                {item.label}
              </button>
            );
          })}
        </div>

        {renderSideContent()}
      </div>
    </div>
  );
}

// ── HTML Viewer Panel ──

function HtmlViewPanel({ htmlContent, tab, onTabChange }) {
  const { t, lang } = useI18n();
  const [sourceText, setSourceText] = useState(htmlContent || "");
  const [copied, setCopied] = useState(false);

  useEffect(function () {
    setSourceText(htmlContent || "");
    setCopied(false);
  }, [htmlContent]);

  if (!htmlContent) {
    return <div className="html-view-container" style={{ padding: 24, color: "var(--text-3)", fontSize: 12 }}>{t("chat.html.noContent")}</div>;
  }

  function handleCopy() {
    navigator.clipboard.writeText(sourceText).then(function () {
      setCopied(true);
      setTimeout(function () { setCopied(false); }, 2000);
    }).catch(function () {});
  }

  return (
    <div className="html-view-container">
      <div className="html-view-tabs">
        <button type="button" className={"html-view-tab" + (tab === "source" ? " active" : "")} onClick={function () { onTabChange && onTabChange("source"); }}>{t("chat.html.sourceTab")}</button>
        <button type="button" className={"html-view-tab" + (tab === "rendered" ? " active" : "")} onClick={function () { onTabChange && onTabChange("rendered"); }}>{t("chat.html.renderedTab")}</button>
      </div>
      {tab === "source" ? (
        <div className="html-source-panel">
          <textarea className="html-source-textarea" value={sourceText} onChange={function (e) { setSourceText(e.target.value); }} spellCheck={false} />
          <div className="html-source-actions" style={{ display: "flex", padding: "8px 10px", borderTop: "1px solid var(--line)", gap: 6 }}>
            <button type="button" className="msg-action-btn" onClick={handleCopy} title={t("chat.html.sourceTab")}>
              {copied ? (lang === "zh" ? "已复制" : "Copied") : (lang === "zh" ? "复制" : "Copy")}
            </button>
          </div>
        </div>
      ) : (
        <iframe className="html-rendered-iframe" sandbox="allow-scripts" srcDoc={sourceText} title="HTML Preview" />
      )}
    </div>
  );
}

// ── PDF / PPT Viewer Panel ──

function PdfViewPanel({ pdfUrl, pdfName }) {
  const { t } = useI18n();
  const [objectUrl, setObjectUrl] = useState(null);
  var urlRef = useRef(null);

  useEffect(function () {
    setObjectUrl(null);
    if (urlRef.current) { URL.revokeObjectURL(urlRef.current); urlRef.current = null; }
    if (!pdfUrl) return;
    var cancelled = false;
    fetch(pdfUrl).then(function (r) { return r.blob(); }).then(function (blob) {
      if (cancelled) return;
      var url = URL.createObjectURL(blob);
      urlRef.current = url;
      setObjectUrl(url);
    }).catch(function () {});
    return function () { cancelled = true; };
  }, [pdfUrl]);

  if (!pdfUrl) {
    return <div className="side-section" style={{ borderBottom: 0, padding: 24, color: "var(--text-3)", fontSize: 12 }}>{t("chat.pdf.noContent")}</div>;
  }

  return (
    <div className="html-view-container">
      {pdfName && (
        <div style={{ padding: "8px 12px", fontSize: 11, fontFamily: "var(--mono)", color: "var(--text-3)", borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
          {pdfName}
        </div>
      )}
      {objectUrl ? (
        <embed className="pdf-view-iframe" src={objectUrl} type="application/pdf" title={pdfName || "PDF"} />
      ) : (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-3)", fontSize: 12 }}>
          {t("chat.pdf.noContent")}
        </div>
      )}
    </div>
  );
}

// ── Side overview token ring ──

function SideTokenRing({ tokens }) {
  var { t } = useI18n();

  // Parse session tokens for the ring chart
  var prompt = null, completion = null, total = null, match;
  if (tokens && tokens !== "—") {
    var re = /([\d.]+)(k|M)?\s*(in|out)/g;
    while ((match = re.exec(tokens)) !== null) {
      var val = parseFloat(match[1]) * (match[2] === "k" ? 1000 : match[2] === "M" ? 1000000 : 1);
      if (match[3] === "in") prompt = val;
      if (match[3] === "out") completion = val;
    }
    if (prompt !== null || completion !== null) {
      total = (prompt || 0) + (completion || 0);
      if (total === 0) total = null;
    }
  }

  // Dashboard data (cache + model stats)
  var dash = (typeof DATA !== "undefined" && DATA.dashboard) || {};
  var usage = dash.usage || {};
  var cacheHit = Number(usage.cache_hit_tokens || 0);
  var cacheMiss = Number(usage.cache_miss_tokens || 0);
  var cacheTotal = cacheHit + cacheMiss;
  var cachePct = cacheTotal > 0 ? Math.round(cacheHit / cacheTotal * 100) : null;

  // ring: cache hit proportion (fallback to token in/out when no cache data)
  var hasTokens = prompt !== null || completion !== null;
  var showRing = true;
  var ringTotal = cacheTotal > 0 ? cacheTotal : (hasTokens ? (prompt || 0) + (completion || 0) : 0);
  var ringA = cacheTotal > 0 ? cacheHit : (hasTokens ? (prompt || 0) : 0);
  var ringLabel = cacheTotal > 0 ? cachePct + "%" : (hasTokens ? compactNumber(total) : "—");
  var ringSub = cacheTotal > 0 ? t("chat.side.cacheHitRate") : (hasTokens ? t("chat.tokenTotal") : t("chat.side.cacheHitRate"));
  var r = 42, c = 2 * Math.PI * r;
  var ringRatio = ringTotal > 0 ? ringA / ringTotal : 0;
  var ringOffset = c * (1 - ringRatio);

  return (
    <div className="side-overview-top">
      {showRing && <div className="side-token-ring">
        <div className="side-ring-wrap">
          <svg width="100" height="100" viewBox="0 0 100 100">
            <circle cx="50" cy="50" r={r} fill="none" stroke="var(--line)" strokeWidth="6" />
            {ringRatio > 0 && <circle cx="50" cy="50" r={r} fill="none" stroke="var(--accent)" strokeWidth="6"
              strokeDasharray={c} strokeDashoffset={ringOffset}
              transform="rotate(-90 50 50)" strokeLinecap="round" />}
          </svg>
          {ringLabel !== null && <div className="side-ring-label">
            <span className="side-ring-pct">{ringLabel}</span>
            {ringSub !== null && <span className="side-ring-sub">{ringSub}</span>}
          </div>}
        </div>
        <div className="side-token-ring-meta">
          <div className="side-token-ring-item">
            <span className="dot dot-in"></span>
            <span>{t("chat.tokenIn")}</span>
            <span className="num">{prompt !== null ? compactNumber(prompt) : "-"}</span>
          </div>
          <div className="side-token-ring-item">
            <span className="dot dot-out"></span>
            <span>{t("chat.tokenOut")}</span>
            <span className="num">{completion !== null ? compactNumber(completion) : "-"}</span>
          </div>
          <div className="side-token-ring-item">
            <span className="dot dot-total"></span>
            <span>{t("chat.tokenTotal")}</span>
            <span className="num">{total !== null ? compactNumber(total) : "-"}</span>
          </div>
        </div>
      </div>}
    </div>
  );
}

// ── Side model usage ──

function SideModelUsage() {
  var { t } = useI18n();
  var dash = (typeof DATA !== "undefined" && DATA.dashboard) || {};
  var rawStats = Array.isArray(dash.model_stats) ? dash.model_stats : [];
  var modelMap = {};
  rawStats.forEach(function (row) {
    if (!modelMap[row.model]) modelMap[row.model] = 0;
    modelMap[row.model] += row.requests || 0;
  });
  var modelEntries = Object.keys(modelMap)
    .map(function (m) { return { model: m, requests: modelMap[m] }; })
    .sort(function (a, b) { return b.requests - a.requests; })
    .slice(0, 5);
  var modelTotal = modelEntries.reduce(function (s, m) { return s + m.requests; }, 0);
  if (modelEntries.length === 0) return null;
  return (
    <div className="side-model-usage">
      <div className="side-head">{t("chat.side.modelUsage")}</div>
      {modelEntries.map(function (m) {
        var pct = modelTotal ? Math.round(m.requests / modelTotal * 100) : 0;
        return (
          <div key={m.model} className="side-model-row">
            <span className="side-model-name">{m.model}</span>
            <div className="side-model-track">
              <div className="side-model-fill" style={{ width: pct + "%" }}></div>
            </div>
            <span className="side-model-pct">{pct}%</span>
          </div>
        );
      })}
    </div>
  );
}

function ShellCard({ shell, ccStatus, onOpenCCModal, onOpenShellModal }) {
  const { t } = useI18n();
  const isCC = shell.kind === "cc" && shell.tmuxSession;

  if (!isCC) {
    return (
      <div className="shell-card">
        <div className="shell-card-head shell-card-head--clickable" onClick={function () {
          onOpenShellModal && onOpenShellModal(shell);
        }}>
          <span>▣</span>
          <span>{shell.title || t("chat.independentShell")}</span>
          <span className="cwd">{shell.cwd}</span>
          <span className={"pill " + (shell.status === "running" ? "running" : shell.status === "err" ? "err" : "")}>{shell.status}</span>
          <span className="pid">{t("chat.ccExpand")}</span>
        </div>
        <div className="shell-card-body">
          {shell.lines.map((l, i) => (
            <div key={i} className={"shell-" + l.kind}>{l.text}</div>
          ))}
        </div>
        <div className="shell-card-foot">{shell.elapsed || "—"} · {shell.updatedAt || "—"}</div>
      </div>
    );
  }

  return (
    <div className="shell-card shell-card--cc">
      <div className="shell-card-head shell-card-head--clickable" onClick={function () {
        onOpenCCModal && onOpenCCModal({
          tmuxSession: shell.tmuxSession,
          latestJsonl: shell.latestJsonl || "",
        });
      }}>
        <span>▸</span>
        <span>{"Claude Code"}</span>
        <span className={"pill " + (shell.status === "running" ? "running" : shell.status === "err" ? "err" : "")}>{shell.status}</span>
        <span className="pid">{t("chat.ccExpand")}</span>
      </div>
      {shell.lines.length > 0 && (
        <div className="shell-card-body">
          {shell.lines.map((l, i) => (
            <div key={i} className={"shell-" + l.kind}>{l.text}</div>
          ))}
        </div>
      )}
      <div className="shell-card-foot">{shell.elapsed || "—"} · {shell.updatedAt || "—"}</div>
    </div>
  );
}

// ── Agent group chat (right sidebar) ──

// Dedup helper: match by id OR by from+content (handles SSE vs API ID mismatch)
function _msgDup(m, existing) {
  return existing.some(function (e) {
    return e.id === m.id || (e.from === m.from && e.content === m.content);
  });
}

var AGENT_COLORS = [
  "#4A90D9", "#E8734A", "#50B86C", "#D94A8C", "#8B6CC4",
  "#D9A64A", "#4AD9C4", "#C44A6C", "#6CB8D9", "#8CC44A",
];

function _agentColor(agentId) {
  var hash = 0;
  for (var i = 0; i < agentId.length; i++) {
    hash = ((hash << 5) - hash) + agentId.charCodeAt(i);
    hash |= 0;
  }
  return AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length];
}

function _formatTime(iso) {
  if (!iso) return "";
  try {
    var d = new Date(iso);
    return d.getHours().toString().padStart(2,"0") + ":" + d.getMinutes().toString().padStart(2,"0");
  } catch (e) { return ""; }
}

function AgentListModal({ agents, onClose }) {
  return ReactDOM.createPortal(
    <div className="agent-list-overlay" onClick={onClose}>
      <div className="agent-list-modal" onClick={function (e) { e.stopPropagation(); }}>
        <div className="agent-list-head">
          <span>All Subagents</span>
          <button onClick={onClose}>&times;</button>
        </div>
        {agents.map(function (a) {
          var dotCls = ({running:"running",waiting:"running",resumed:"running",done:"done",timeout:"err"})[a.status] || "done";
          return (
            <div className="agent-list-row" key={a.id}>
              <div className={"agent-list-dot " + dotCls}></div>
              <span className="agent-list-id">{a.id}</span>
              <span className="agent-list-task">{a.task || "—"}</span>
              <span className="agent-list-meta">{a.status}{a.tokens != null ? " · " + a.tokens + " tok" : ""}</span>
            </div>
          );
        })}
        {agents.length === 0 && <div className="agent-chat-empty">No subagents</div>}
      </div>
    </div>,
    document.body
  );
}

function ShellTerminalPanel({ shell, onClose, onRefresh }) {
  var bodyRef = useRef(null);
  // Auto-scroll to bottom when lines update
  useEffect(function () {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [shell.lines]);
  return (
    <div className="cc-terminal cc-terminal--expanded cc-terminal--modal">
      <div className="cc-terminal__panel">
        <div className="cc-terminal__header">
          <div className="cc-terminal__titleRow">
            <span className="cc-terminal__title">{shell.title || "Terminal"}</span>
            <span className={"cc-terminal__status cc-terminal__status--" + (shell.status === "running" ? "running" : "offline")}>
              {shell.status === "running" ? "live" : shell.status}
            </span>
            <span className="cc-terminal__esc" style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>
              pid {shell.pid} · {shell.cwd || ""}
            </span>
          </div>
          <div className="cc-terminal__actions">
            <button className="cc-terminal__button" onClick={onRefresh}>
              Refresh
            </button>
            <button className="cc-terminal__button cc-terminal__button--accent" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        <div className="cc-terminal__surface">
          <div ref={bodyRef} className="cc-terminal__viewport" style={{ overflow: "auto", whiteSpace: "pre", background: "var(--bg-0)" }}>
            {shell.lines.map(function (l, i) {
              return <div key={i} className={"shell-" + l.kind}>{l.text}</div>;
            })}
          </div>
        </div>

        <div className="cc-terminal__footer">
          <div className="cc-terminal__meta">
            {shell.elapsed || "—"} · {shell.updatedAt || "—"}
          </div>
        </div>
      </div>
    </div>
  );
}

function GroupChatMessage({ msg, prevFrom }) {
  // System messages render as a full-width separator
  if (msg.from === "system") {
    return <div className="agent-chat-ended">━━ {msg.content} ━━</div>;
  }

  var color = _agentColor(msg.from);
  var nameEl = null;
  var msgClass = "agent-chat-row";

  if (msg.from === "user") {
    msgClass += " right";
  } else if (prevFrom !== msg.from) {
    nameEl = <div className="agent-chat-name" style={{ color: color }}>{msg.from}</div>;
  } else {
    msgClass += " same-agent";
  }

  var bubbleClass = "agent-chat-bubble" + (msg.from === "user" ? " user" : "");
  var html = renderMarkdown(msg.content || "");
  // Highlight @mentions in blue (after markdown, before render)
  html = html.replace(/@(\w[\w.-]*)/g, '<span class="agent-mention">@$1</span>');

  return (
    <div className={msgClass}>
      {nameEl}
      <div className={bubbleClass} dangerouslySetInnerHTML={{ __html: html }}></div>
    </div>
  );
}

function GroupChatMessages({ messages, agentsActive }) {
  var scrollRef = React.useRef(null);
  var userAtBottom = React.useRef(false);
  var initialRenderDone = React.useRef(false);

  // Track user's scroll position via onScroll (measures BEFORE new content)
  function handleScroll() {
    var el = scrollRef.current;
    if (!el) return;
    userAtBottom.current = (el.scrollTop + el.clientHeight >= el.scrollHeight - 40);
  }

  // Auto-scroll when new messages arrive (only if user was already at bottom)
  React.useEffect(function () {
    var el = scrollRef.current;
    if (!el) return;
    // On the first real render (messages just arrived), scroll to the bottom so
    // the user sees the latest messages. Subsequent updates only auto-scroll when
    // the user is already near the bottom (tracked by handleScroll).
    if (!initialRenderDone.current) {
      initialRenderDone.current = true;
      el.scrollTop = el.scrollHeight;
      userAtBottom.current = true;
      return;
    }
    if (userAtBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="agent-chat-messages">
        <div className="agent-chat-empty">
          {agentsActive
            ? <span className="agent-chat-waiting">subagent 运行中...</span>
            : "暂无 subagent 对话"}
        </div>
      </div>
    );
  }

  var rows = [];
  var prevFrom = null;
  for (var i = 0; i < messages.length; i++) {
    var msg = messages[i];
    // Time separator (5+ min gap)
    if (i > 0) {
      var prevTs = messages[i - 1].timestamp;
      var curTs = msg.timestamp;
      if (prevTs && curTs) {
        try {
          var diff = new Date(curTs) - new Date(prevTs);
          if (diff > 300000) { // 5 min
            rows.push(<div className="agent-chat-timesep" key={"ts_" + i}>{_formatTime(curTs)}</div>);
          }
        } catch (e) {}
      }
    }
    rows.push(<GroupChatMessage key={msg.id || i} msg={msg} prevFrom={prevFrom} />);
    prevFrom = msg.from;
  }

  return (
    <div className="agent-chat-messages" ref={scrollRef} onScroll={handleScroll}>
      {rows}
    </div>
  );
}

function GroupChatHeader({ title, agents, chatEnded, settingsOpen, onToggleSettings, onShowAgents, onStop }) {
  var menuRef = React.useRef(null);

  React.useEffect(function () {
    if (!settingsOpen) return;
    function handleClick(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        onToggleSettings();
      }
    }
    document.addEventListener("mousedown", handleClick);
    return function () { document.removeEventListener("mousedown", handleClick); };
  }, [settingsOpen]);

  return (
    <div className="agent-chat-header">
      <div className="agent-chat-title" title={title}>{title || "Agent Chat"}</div>
      <div style={{ position: "relative" }}>
        <button className="agent-chat-settings-btn" onClick={onToggleSettings}>&#8942;</button>
        {settingsOpen && (
          <div className="agent-chat-settings-menu" ref={menuRef}>
            <button onClick={onShowAgents}>查看全部 subagent</button>
            {!chatEnded && <button className="danger" onClick={onStop}>停止对话并总结</button>}
          </div>
        )}
      </div>
    </div>
  );
}

function GroupChatComposer({ agents, chatEnded, onSend }) {
  var taRef = React.useRef(null);
  var fileInputRef = React.useRef(null);
  var [text, setText] = React.useState("");
  var [attachments, setAttachments] = React.useState([]);
  var [mentionOpen, setMentionOpen] = React.useState(false);
  var [mentionFilter, setMentionFilter] = React.useState("");

  // Autosize textarea
  function syncHeight() {
    var ta = taRef.current;
    if (ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; }
  }

  function handleChange(e) {
    var val = e.target.value;
    setText(val);
    syncHeight();

    // Detect @ trigger
    var lastAt = val.lastIndexOf("@");
    if (lastAt >= 0 && (lastAt === 0 || val[lastAt - 1] === " " || val[lastAt - 1] === "\n")) {
      var after = val.slice(lastAt + 1);
      if (!after.includes(" ") && !after.includes("\n")) {
        setMentionFilter(after);
        setMentionOpen(true);
        return;
      }
    }
    setMentionOpen(false);
  }

  function selectMention(agentId) {
    var val = text;
    var lastAt = val.lastIndexOf("@");
    if (lastAt < 0) {
      // Menu was opened via @ button (no @ in text yet)
      setText("@" + agentId + " ");
    } else {
      var before = val.slice(0, lastAt);
      var after = val.slice(lastAt + 1);
      var spaceIdx = after.search(/[\s\n]/);
      var rest = spaceIdx >= 0 ? after.slice(spaceIdx) : "";
      setText(before + "@" + agentId + rest + " ");
    }
    setMentionOpen(false);
    syncHeight();
    setTimeout(function () { if (taRef.current) taRef.current.focus(); }, 0);
  }

  function handleKeyDown(e) {
    if (e.key === "Escape") { setMentionOpen(false); }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleFileSelect() {
    var files = fileInputRef.current && fileInputRef.current.files;
    if (!files || !files.length) return;
    // Upload via existing /api/chat/upload endpoint
    var formData = new FormData();
    for (var fi = 0; fi < files.length; fi++) {
      formData.append("files", files[fi]);
    }
    fetch("/api/chat/upload", { method: "POST", body: formData })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var newAtts = (data.attachments || data.files || []).map(function (f) {
          return { path: f.path || f.url || "", name: f.name || f.filename || "file" };
        });
        setAttachments(attachments.concat(newAtts));
      })
      .catch(function (err) { console.warn("File upload failed", err); });
    // Reset file input so the same file can be re-selected
    fileInputRef.current.value = "";
  }

  function removeAttachment(idx) {
    var copy = attachments.slice();
    copy.splice(idx, 1);
    setAttachments(copy);
  }

  function handleSend() {
    var trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;

    // Parse @mentions from text
    var mentionIds = [];
    var mentionRe = /@(\S+)/g;
    var match;
    while ((match = mentionRe.exec(trimmed)) !== null) {
      var name = match[1].replace(/[^A-Za-z0-9_-]/g, "");
      // Check it's a real agent
      if (agents.some(function (a) { return a.id === name; })) {
        if (mentionIds.indexOf(name) < 0) mentionIds.push(name);
      }
    }

    onSend({ text: trimmed, mentions: mentionIds, attachments: attachments });
    setText("");
    setAttachments([]);
    if (taRef.current) taRef.current.style.height = "";
  }

  var inputRow = (
    <div className="agent-chat-input-row">
      <input ref={fileInputRef} type="file" multiple style={{ display: "none" }} onChange={handleFileSelect} />
      <button className="iconbtn" onClick={function () { fileInputRef.current && fileInputRef.current.click(); }}
        disabled={chatEnded}>+</button>
      <button className="iconbtn" onClick={function () { setMentionOpen(!mentionOpen); }}
        disabled={chatEnded}>@</button>
      {mentionOpen && (
        <div className="agent-chat-mentions">
          {agents.filter(function (a) {
            return !mentionFilter || a.id.toLowerCase().indexOf(mentionFilter.toLowerCase()) >= 0;
          }).map(function (a) {
            return (
              <button key={a.id} className="agent-chat-mention-option"
                style={{ color: _agentColor(a.id) }}
                onMouseDown={function (e) { e.preventDefault(); selectMention(a.id); }}>
                @{a.id}
              </button>
            );
          })}
        </div>
      )}
      <textarea ref={taRef} value={text} onChange={handleChange} onKeyDown={handleKeyDown}
        placeholder="发送消息到 subagent..." rows={1} disabled={chatEnded}></textarea>
      <button className="send" onClick={handleSend} disabled={chatEnded}>发送</button>
    </div>
  );

  if (chatEnded) {
    return (
      <div className="agent-chat-composer ended">
        {inputRow}
      </div>
    );
  }

  return (
    <div className="agent-chat-composer">
      {inputRow}
      {attachments.length > 0 && (
        <div className="agent-chat-attachments">
          {attachments.map(function (f, idx) {
            return (
              <span key={idx} className="chip" style={{ cursor: "pointer" }}
                onClick={function () { removeAttachment(idx); }}>
                {f.name} &times;
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

function AgentGroupChat({ roundId, subagents, session }) {
  var [messages, setMessages] = React.useState([]);
  var [agents, setAgents] = React.useState([]);
  var [loading, setLoading] = React.useState(true);
  var [error, setError] = React.useState(null);
  var [chatEnded, setChatEnded] = React.useState(false);
  var [settingsOpen, setSettingsOpen] = React.useState(false);
  var [modalOpen, setModalOpen] = React.useState(false);

  // Track current round to discard stale fetch responses
  var fetchRoundRef = React.useRef("");
  var subagentFetchTimerRef = React.useRef(null);
  // Mirror of chatEnded so the roundId effect can read it synchronously
  var chatEndedRef = React.useRef(false);

  // Keep chatEndedRef in sync with chatEnded state
  React.useEffect(function () { chatEndedRef.current = chatEnded; }, [chatEnded]);

  // Fetch initial messages
  React.useEffect(function () {
    if (!roundId) {
      setLoading(false);
      setMessages([]);
      setAgents([]);
      chatEndedRef.current = false;
      return;
    }
    fetchRoundRef.current = roundId;
    // Only clear messages when the previous round was actually finished.
    // If the old round is still running and the user starts a new round in
    // parallel, keep the existing chat visible until the new round's data loads.
    var wasEnded = chatEndedRef.current;
    chatEndedRef.current = false;
    if (wasEnded) {
      setMessages([]);
      setAgents([]);
      setLoading(true);
    }
    setError(null);
    setChatEnded(false);
    fetch("/api/chat/agent-chat-messages?round_id=" + encodeURIComponent(roundId))
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        // Discard stale responses from previous roundId
        if (fetchRoundRef.current !== roundId) return;
        setMessages(function (existing) {
          var fetched = data.messages || [];
          var merged = fetched.slice();
          existing.forEach(function (em) {
            if (!_msgDup(em, merged)) merged.push(em);
          });
          merged.sort(function (a, b) { return (a.timestamp || "") < (b.timestamp || "") ? -1 : 1; });
          return merged;
        });
        setAgents(data.agents || []);
        var all = data.agents || [];
        var allDone = all.length > 0 && all.every(function (a) { return a.status === "done" || a.status === "timeout"; });
        if (allDone) setChatEnded(true);
        setLoading(false);
      })
      .catch(function (err) {
        setError(String(err));
        setLoading(false);
      });
  }, [roundId]);

  // SSE handlers for real-time updates
  React.useEffect(function () {
    function handler(event) {
      if (event.type === "agent_comm" && event.round_id === roundId) {
        // Add incoming agent message to the list
        var newMsg = {
          id: event.message_id || (event.from + "_" + Date.now()),
          type: event.broadcast ? "agent_broadcast" : "agent_send",
          from: event.from,
          to: event.to || "all",
          content: event.content || "",
          timestamp: event.timestamp || new Date().toISOString(),
          round_id: event.round_id,
        };
        if (!newMsg.content) return;
        // Format display content
        if (newMsg.type === "agent_broadcast") {
          newMsg.content = "@所有人 " + newMsg.content;
        } else if (newMsg.to && newMsg.to !== "all") {
          newMsg.content = "@" + newMsg.to + " " + newMsg.content;
        }
        setMessages(function (prev) {
          if (_msgDup(newMsg, prev)) return prev;
          return prev.concat([newMsg]);
        });
      } else if (event.type === "agent_chat_user_message" && event.round_id === roundId) {
        var userMsg = event.message;
        if (userMsg) {
          setMessages(function (prev) {
            if (_msgDup(userMsg, prev)) return prev;
            return prev.concat([userMsg]);
          });
        }
      } else if (event.type === "subagent_update" && event.round_id === roundId) {
        // Debounce re-fetch: multiple rapid subagent updates (e.g. save_messages
        // called frequently) should only trigger one fetch.
        if (subagentFetchTimerRef.current) clearTimeout(subagentFetchTimerRef.current);
        subagentFetchTimerRef.current = setTimeout(function () {
          subagentFetchTimerRef.current = null;
          var _fetchRound = roundId;
          fetch("/api/chat/agent-chat-messages?round_id=" + encodeURIComponent(roundId))
            .then(function (r) {
              if (!r.ok) throw new Error("HTTP " + r.status);
              return r.json();
            })
            .then(function (data) {
              if (fetchRoundRef.current !== _fetchRound) return;
              setMessages(function (existing) {
                var fetched = data.messages || [];
                // Fast path: if lengths match and IDs overlap, skip re-render
                if (existing.length === fetched.length) {
                  var same = true;
                  for (var ei = 0; ei < existing.length; ei++) {
                    if (!_msgDup(existing[ei], fetched)) { same = false; break; }
                  }
                  if (same) return existing;
                }
                var merged = fetched.slice();
                existing.forEach(function (em) {
                  if (!_msgDup(em, merged)) merged.push(em);
                });
                merged.sort(function (a, b) { return (a.timestamp || "") < (b.timestamp || "") ? -1 : 1; });
                return merged;
              });
              setAgents(data.agents || []);
              var all = data.agents || [];
              var allDone = all.length > 0 && all.every(function (a) {
                return a.status === "done" || a.status === "timeout";
              });
              if (allDone) setChatEnded(true);
            })
            .catch(function () {});
        }, 300);
      }
    }
    window.__sseHandlers.add(handler);
    return function () {
      window.__sseHandlers.delete(handler);
      if (subagentFetchTimerRef.current) {
        clearTimeout(subagentFetchTimerRef.current);
        subagentFetchTimerRef.current = null;
      }
    };
  }, [roundId]);

  // When subagents prop changes, merge agent info (status, tokens, etc.)
  React.useEffect(function () {
    if (subagents && subagents.length > 0) {
      setAgents(function (prev) {
        var merged = prev.map(function (a) {
          var match = subagents.find(function (s) { return s.id === a.id; });
          return match ? Object.assign({}, a, { status: match.status, tokens: match.tokens, elapsed: match.elapsed }) : a;
        });
        // Add any agents from subagents not yet in list
        subagents.forEach(function (s) {
          if (s.id !== "main" && !merged.some(function (a) { return a.id === s.id; })) {
            merged.push({ id: s.id, task: s.task || "", status: s.status, tokens: s.tokens, elapsed: s.elapsed });
          }
        });
        return merged;
      });
    }
  }, [subagents]);

  // Sync chatEnded whenever agents state changes — catches the case where
  // agent status arrives via the subagents prop rather than a direct fetch.
  React.useEffect(function () {
    if (agents.length === 0 || chatEnded) return;
    var allDone = agents.every(function (a) {
      return a.status === "done" || a.status === "timeout";
    });
    if (allDone) setChatEnded(true);
  }, [agents]);

  function handleStop() {
    fetch("/api/chat/interrupt", { method: "POST" })
      .then(function () {
        setChatEnded(true);
        setSettingsOpen(false);
        // Show that summarization is starting
        setMessages(function (prev) {
          var endMsg = {
            id: "chat_ended_" + Date.now(),
            type: "agent_result",
            from: "system",
            to: "",
            content: "━ 正在总结… ━",
            timestamp: new Date().toISOString(),
            round_id: roundId,
          };
          return prev.concat([endMsg]);
        });
      })
      .catch(function (err) { console.warn("Interrupt failed", err); });
  }

  function handleSend(payload) {
    fetch("/api/chat/send-to-agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        round_id: roundId,
        text: payload.text,
        mentions: payload.mentions.length > 0 ? payload.mentions : undefined,
        attachments: payload.attachments,
      }),
    }).catch(function (err) { console.warn("Send to agents failed", err); });
  }

  // Title from session or first user message
  // Title: use session.title (set by main agent), fallback to first user message
  var title = (session && session.title) || (session && session.currentRoundTitle) || "";
  if (!title && session && session.chat && session.chat.messages) {
    for (var ti = 0; ti < session.chat.messages.length; ti++) {
      var m = session.chat.messages[ti];
      if (m.role === "user" && m.content) {
        title = String(m.content).replace(/\s+/g, " ").slice(0, 30);
        break;
      }
    }
  }

  if (loading) {
    return <div className="agent-chat"><div className="agent-chat-loading">加载中...</div></div>;
  }
  if (error) {
    return <div className="agent-chat"><div className="agent-chat-error">{error}</div></div>;
  }
  if (!roundId || agents.length === 0) {
    return <div className="agent-chat"><div className="agent-chat-empty">暂无活跃 subagent</div></div>;
  }

  return (
    <div className="agent-chat">
      <GroupChatHeader title={title} agents={agents} chatEnded={chatEnded}
        settingsOpen={settingsOpen}
        onToggleSettings={function () { setSettingsOpen(!settingsOpen); }}
        onShowAgents={function () { setSettingsOpen(false); setModalOpen(true); }}
        onStop={handleStop} />
      <GroupChatMessages messages={messages} agentsActive={agents.some(function (a) { return a.status === "running" || a.status === "waiting"; })} />
      <GroupChatComposer agents={agents} chatEnded={chatEnded} onSend={handleSend} />
      {modalOpen && <AgentListModal agents={agents}
        onClose={function () { setModalOpen(false); }} />}
    </div>
  );
}

window.ChatPage = ChatPage;
