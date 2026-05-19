// Chat page — wired to /api/chat with live state and SSE updates
const { useState, useRef, useEffect } = React;

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
    window.marked.setOptions({
      gfm: true,
      breaks: true,
      headerIds: false,
      mangle: false,
    });
    const html = window.marked.parse(source);
    return window.DOMPurify.sanitize(html);
  }
  return escapeHtml(source).replace(/\n/g, "<br>");
}

function traceSummary(msg) {
  const parts = [];
  if (msg.thinking) parts.push("reasoning");
  if (msg.tools && msg.tools.length) {
    parts.push(msg.tools.length === 1 ? "1 tool call" : msg.tools.length + " tool calls");
  }
  return parts.length ? "details · " + parts.join(" · ") : "details";
}

function formatElapsedMs(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function syncTextareaHeight(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = Math.min(200, textarea.scrollHeight) + "px";
}

function isAbortError(error) {
  return error && (error.name === "AbortError" || String(error.message || "").includes("aborted"));
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

function visibleRetainedMessages(existingMessages, transientMessages) {
  return unmatchedMessages(existingMessages, transientMessages).filter(function (msg) {
    const replacementRequestId = String(msg && msg.replaceWhenAssistantReplyForRequestId || "");
    if (!replacementRequestId) return true;
    return !hasVisibleAssistantReplyForRequest(existingMessages, replacementRequestId);
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
      timeLabel: "guiding…",
      summary: "details · main inbox",
      head: "queue",
      empty: "Sending to main inbox…",
    };
  }
  if (guidanceAccepted) {
    return {
      timeLabel: "…",
      summary: "details · after guidance",
      head: "processing",
      empty: "Continuing with the accepted guidance…",
    };
  }
  return {
    timeLabel: "…",
    summary: "details · processing",
    head: "processing",
    empty: "Thinking...",
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
  };
}

function guidanceAckMessage(guidanceId, body, insertAfterKey) {
  const safeGuidanceId = String(guidanceId || "");
  const text = String(body || "已接受引导。我会按这条新要求调整当前这一轮的工作，并在完成后给你更新。");
  return {
    id: "guidance_ack_" + (safeGuidanceId || Date.now()),
    role: "agent",
    time: new Date().toLocaleTimeString(),
    body: text,
    guidanceAckForGuidanceId: safeGuidanceId,
    insertAfterKey: String(insertAfterKey || ""),
  };
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

function buildAttachedRuntime(activeTraceDescriptor, liveElapsed, visibleLiveProgress, watchingGuidance, activeGuideRoundTitle, activeRequest) {
  const entries = [];
  if (watchingGuidance) {
    entries.push({
      icon: "↳",
      text: "Target round: " + (activeGuideRoundTitle || (activeRequest && activeRequest.guideRoundId) || ""),
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
  if (!isTraceOnlyAssistantMessage(msg)) return false;
  if (msg.guidanceAckForGuidanceId || msg.inReplyToGuidanceId || msg.queuedGuidanceId) return false;
  const activeRequestId = String(activeRequest && activeRequest.id || "");
  const messageRequestId = String(msg && msg.clientRequestId || "");
  if (activeRequestId && messageRequestId && activeRequestId === messageRequestId) return true;
  const currentRoundId = String(session && session.currentRoundId || "");
  const messageRoundId = String(msg && msg.roundId || "");
  return Boolean(!messageRequestId && currentRoundId && messageRoundId && currentRoundId === messageRoundId);
}

function hasVisibleAssistantReplyForRequest(messages, requestId) {
  const targetRequestId = String(requestId || "");
  if (!targetRequestId) return false;
  return (messages || []).some(function (msg) {
    return msg
      && msg.role === "agent"
      && !msg.runtimeTrace
      && !msg.intermediateReply
      && !msg.questionPrompt
      && (msg.body || msg.thinking || (msg.tools && msg.tools.length))
      && String(msg.clientRequestId || "") === targetRequestId;
  });
}

function formatProgressEvent(event) {
  switch (event.type) {
    case "phase_transition":
      return { icon: "●", text: event.detail || event.from + " → " + event.to };
    case "tool_call": {
      const args = event.args || {};
      const argPreview = Object.values(args).filter(Boolean).map(String).join(", ").slice(0, 60);
      return { icon: "▸", text: event.tool + (argPreview ? "(" + argPreview + ")" : "()") };
    }
    case "llm_call":
      return { icon: "◎", text: (event.caller || "agent") + " · " + (event.phase || "thinking") };
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
      const queueAnchorKey = eventGuidanceId
        ? "guide::" + eventGuidanceId
        : (runtime.activeRequest && runtime.activeRequest.guideRequestId ? "guide::" + runtime.activeRequest.guideRequestId : "");
      const queueTraceId = eventGuidanceId ? "guidance_queue_trace_" + eventGuidanceId : "";
      const frozenQueueTrace = snapshotRuntimeTrace(runtime, {
        insertAfterKey: queueAnchorKey,
        traceId: queueTraceId || undefined,
      });
      const ackMsg = guidanceAckMessage(
        eventGuidanceId,
        event && event.ack_text,
        frozenQueueTrace ? messageKey(frozenQueueTrace) : queueAnchorKey
      );
      updateChatRuntime({
        retainedMessages: runtime.retainedMessages
          .concat(frozenQueueTrace ? [frozenQueueTrace] : [])
          .concat([ackMsg]),
        startedAt: Date.now(),
        liveProgress: [],
        activeRequest: runtime.activeRequest
          ? {
              ...runtime.activeRequest,
              guidanceAccepted: true,
              guidanceId: eventGuidanceId,
              finalTraceAnchorKey: eventGuidanceId ? "guidance-ack::" + eventGuidanceId : messageKey(ackMsg),
            }
          : null,
      });
      return;
    }
    if (event && event.type === "chat_message" && eventRequestId && runtime.watchRequestId === eventRequestId) {
      const frozenFinalTrace = runtime.activeRequest && runtime.activeRequest.guideRoundId
        ? snapshotRuntimeTrace(runtime, {
            insertAfterKey: String(
              runtime.activeRequest.finalTraceAnchorKey
              || (runtime.activeRequest.guideRequestId ? "guide::" + runtime.activeRequest.guideRequestId : "")
            ),
            replaceWhenAssistantReplyForRequestId: eventRequestId,
          })
        : null;
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

function ChatPage({ selectedSessionId, onSelectSession }) {
  useDataVersion(); // re-render when DATA refreshes

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

  const [draft, setDraft] = useState("");
  const [questionDraft, setQuestionDraft] = useState("");
  const [answeringQuestion, setAnsweringQuestion] = useState(false);
  const [contextPickerOpen, setContextPickerOpen] = useState(false);
  const [selectedGuideRoundId, setSelectedGuideRoundId] = useState("");
  const [selectedGuideRoundTitle, setSelectedGuideRoundTitle] = useState("");
  const [notice, setNotice] = useState("");
  const [runtimeState, setRuntimeState] = useState(getChatRuntimeSnapshot);
  const [elapsedNow, setElapsedNow] = useState(Date.now());
  const taRef = useRef(null);
  const scrollRef = useRef(null);
  const sending = runtimeState.sending;
  const pendingMessages = runtimeState.pendingMessages;
  const retainedMessages = isLiveSession
    ? visibleRetainedMessages(session.chat.messages, runtimeState.retainedMessages || [])
    : [];
  const visiblePendingMessages = isLiveSession
    ? unmatchedMessages((session.chat.messages || []).concat(retainedMessages), pendingMessages || [])
    : [];
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

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [session.id, session.chat.messages.length, retainedMessages.length, visiblePendingMessages.length, visibleLiveProgress.length, visibleSending, visibleNotice]);

  useEffect(() => {
    const runtime = getChatRuntime();
    runtime.listeners.add(setRuntimeState);
    setRuntimeState(getChatRuntimeSnapshot());
    return function () { runtime.listeners.delete(setRuntimeState); };
  }, []);

  useEffect(function () {
    if (!isLiveSession) return;
    if (!runtimeState.retainedMessages || runtimeState.retainedMessages.length === 0) return;
    const nextRetained = visibleRetainedMessages(session.chat.messages, runtimeState.retainedMessages);
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
    if (!sending || !runtimeState.startedAt) return;
    const timer = window.setInterval(function () {
      setElapsedNow(Date.now());
    }, 1000);
    setElapsedNow(Date.now());
    return function () { window.clearInterval(timer); };
  }, [sending, runtimeState.startedAt]);

  function autosize(e) {
    setDraft(e.target.value);
    syncTextareaHeight(taRef.current);
  }

  function completeWatchedRequest(requestId) {
    const runtime = getChatRuntime();
    delete runtime.requests[requestId];
    if (runtime.watchRequestId !== requestId) return;
    const frozenFinalTrace = runtime.activeRequest && runtime.activeRequest.guideRoundId
      ? snapshotRuntimeTrace(runtime, {
          insertAfterKey: String(
            runtime.activeRequest.finalTraceAnchorKey
            || (runtime.activeRequest.guideRequestId ? "guide::" + runtime.activeRequest.guideRequestId : "")
          ),
          replaceWhenAssistantReplyForRequestId: requestId,
        })
      : null;
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

  async function send(options) {
    const preserveProgress = Boolean(options && options.preserveProgress);
    const text = draft.trim();
    const runtime = getChatRuntime();
    if (!text) return;
    if (pendingQuestion) {
      setNotice("Answer the pending question above before starting a new message.");
      return;
    }
    setNotice("");
    runtime.requestSeq = (runtime.requestSeq || 0) + 1;
    const requestId = "req_" + Date.now() + "_" + runtime.requestSeq;
    const controller = new AbortController();
    const requestMeta = {
      id: requestId,
      message: text,
      guideRoundId: selectedGuideRoundId || "",
      guideRoundTitle: currentGuideRoundTitle,
      controller,
    };
    runtime.requests[requestId] = requestMeta;
    const userMsg = {
      id: "pending_user_" + Date.now(),
      role: "user", time: new Date().toLocaleTimeString(),
      body: text,
      roundId: selectedGuideRoundId || "",
      clientRequestId: requestId,
    };
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
    setDraft("");
    syncTextareaHeight(taRef.current);

    let keepWatching = false;
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          guide_round_id: selectedGuideRoundId || undefined,
          client_request_id: requestId,
        }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const isWatching = getChatRuntime().watchRequestId === requestId;
      if (data.queued) {
        keepWatching = true;
        if (runtime.requests[requestId]) {
          runtime.requests[requestId].guideRequestId = data.guide_request_id || "";
          runtime.requests[requestId].queued = true;
        }
        if (window.refreshSessions) {
          await window.refreshSessions();
        }
        if (isWatching) {
          updateChatRuntime(function (state) {
            const queuedProgress = {
              icon: "↳",
              text: "Guidance accepted. Waiting for the current round to reach the main agent.",
            };
            return {
              pendingMessages: state.pendingMessages.map(function (msg) {
                if (String(msg.clientRequestId || "") !== requestId) return msg;
                return { ...msg, queuedGuidanceId: data.guide_request_id || "" };
              }),
              activeRequest: state.activeRequest && state.activeRequest.id === requestId
                ? {
                    ...state.activeRequest,
                    guideRequestId: data.guide_request_id || "",
                    queued: true,
                  }
                : state.activeRequest,
              liveProgress: state.liveProgress.concat([queuedProgress]).slice(-30),
            };
          });
        }
        return;
      }
      if (data.awaiting_user) {
        delete runtime.requests[requestId];
        if (window.refreshSessions) {
          await window.refreshSessions();
        }
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
      const agentMsg = {
        id: "pending_agent_" + Date.now(),
        role: "agent", time: new Date().toLocaleTimeString(),
        body: data.response || "(no response)",
        clientRequestId: requestId,
      };
      if (isWatching) {
        delete runtime.requests[requestId];
        updateChatRuntime(function (state) {
          return {
            pendingMessages: state.pendingMessages.concat([agentMsg]),
            sending: false,
            liveProgress: [],
            startedAt: 0,
            activeRequest: null,
            watchRequestId: "",
          };
        });
        clearChatRuntimeSseSubscription();
      }
      // Refresh sessions FIRST so the run_live entry contains the new
      // messages before we clear pending — otherwise there's a flash of
      // "No messages yet" between pending-clear and sessions-arriving.
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      if (isWatching) {
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
    if (!pendingQuestion || answeringQuestion || sending) return;
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
        }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const isWatching = getChatRuntime().watchRequestId === requestId;
      if (data.awaiting_user) {
        delete runtime.requests[requestId];
        if (window.refreshSessions) {
          await window.refreshSessions();
        }
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
        setQuestionDraft("");
        return;
      }

      const agentMsg = {
        id: "pending_agent_" + Date.now(),
        role: "agent",
        time: new Date().toLocaleTimeString(),
        body: data.response || "(no response)",
        clientRequestId: requestId,
      };
      if (isWatching) {
        delete runtime.requests[requestId];
        updateChatRuntime(function (state) {
          return {
            pendingMessages: state.pendingMessages.concat([agentMsg]),
            sending: false,
            liveProgress: [],
            startedAt: 0,
            activeRequest: null,
            watchRequestId: "",
          };
        });
        clearChatRuntimeSseSubscription();
      }
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      if (isWatching) {
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
        ? "The current run is continuing in the background while this guidance is sent."
        : "The current run is continuing in the background while this new dialogue is sent.",
      { retainMessages: true }
    );
    send();
  }

  async function stopActiveRun() {
    const runtime = getChatRuntime();
    const requestId = runtime.watchRequestId;
    const requestMeta = requestId ? runtime.requests[requestId] : null;
    if (!requestId || !requestMeta) return;
    requestMeta.controller.abort();
    releaseWatchedRequest("", { retainMessages: false });
    delete runtime.requests[requestId];
    setDraft(requestMeta.message || "");
    setSelectedGuideRoundId(requestMeta.guideRoundId || "");
    setSelectedGuideRoundTitle(requestMeta.guideRoundTitle || "");
    setContextPickerOpen(false);
    setNotice("Stopped the current request. The last sent message was restored to the input box.");
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
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  }

  function onQuestionKey(e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submitQuestionAnswer();
    }
  }

  const allMessages = isLiveSession
    ? mergeMessagesWithAnchors(
        [...session.chat.messages, ...visiblePendingMessages],
        retainedMessages
      )
    : session.chat.messages;
  const lastMessage = allMessages.length ? allMessages[allMessages.length - 1] : null;
  const runtimeAttachedToLastMessage = visibleSending && canAttachRuntimeToLastMessage(lastMessage, activeRequest, session);
  const renderedMessages = runtimeAttachedToLastMessage
    ? allMessages.slice(0, -1).concat([{
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
    : allMessages;
  const renderedMessageEntries = renderMessageEntries(renderedMessages);

  async function newSession() {
    if (!confirm("Start a new session? The current conversation will be compressed into short-term memory.")) return;
    try {
      resetChatRuntime({ abort: true });
      const r = await fetch("/api/sessions", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
      onSelectSession && onSelectSession(null);
    } catch (e) {
      alert("Failed: " + e.message);
    }
  }

  return (
    <div className="chat-layout">
      <div className="chat-main">
        <div className="chat-scroll" ref={scrollRef}>
          <div className="thread-header">
            <span className={"sa-dot " + session.status} style={{ marginTop: 0, width: 6, height: 6 }}></span>
            <span>{session.title}</span>
            <span style={{ marginLeft: "auto" }}>{session.id} · started {session.started}</span>
            <span style={{
                    cursor: "pointer", color: "var(--text-3)",
                    border: "1px solid var(--line)", borderRadius: 4,
                    padding: "2px 8px", fontSize: 10.5, letterSpacing: "0.04em",
                  }}
                  onClick={newSession}
                  onMouseEnter={(e) => (e.target.style.color = "var(--accent)")}
                  onMouseLeave={(e) => (e.target.style.color = "var(--text-3)")}
                  title="Compress current session and start a new one">
              + new session
            </span>
          </div>
          {!isLiveSession && (
            <div className="archive-banner">
              <span>Viewing archive · {session.started}</span>
              <span className="archive-banner-action"
                    onClick={function () { onSelectSession && onSelectSession(null); }}>
                ← return to live session
              </span>
            </div>
          )}
          {renderedMessages.length === 0 && (
            <div style={{ padding: "40px 0", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
              No messages yet. Say hello to {DATA.assistantName}.
            </div>
          )}
          {renderedMessageEntries.map((entry) => (
            <Message
              key={entry.renderKey}
              msg={entry.msg}
              assistantName={DATA.assistantName}
            />
          ))}
          {visibleSending && !runtimeAttachedToLastMessage && (
            <div className="msg agent">
              <div className="msg-meta">
                <span className="msg-role agent">● {DATA.assistantName}</span>
                <span className="msg-time">{activeTraceDescriptor.timeLabel}</span>
              </div>
              <details className="msg-trace only-trace runtime-trace">
                <summary className="msg-trace-summary">
                  <span className="msg-trace-caret">▸</span>
                  <span>{activeTraceDescriptor.summary} · {liveElapsed}</span>
                </summary>
                <div className="msg-trace-body">
                  <div className="thinking">
                    <div className="thinking-head">{activeTraceDescriptor.head}</div>
                    {watchingGuidance && (
                      <div className="progress-entry">
                        <span className="progress-icon">↳</span>
                        <span className="progress-text">Target round: {activeGuideRoundTitle || activeRequest.guideRoundId}</span>
                      </div>
                    )}
                    {visibleLiveProgress.length === 0 && <div className="progress-entry"><span className="progress-icon">◎</span><span className="progress-text">{activeTraceDescriptor.empty}</span></div>}
                    {visibleLiveProgress.map(function (p, i) {
                      return <div key={i} className="progress-entry"><span className="progress-icon">{p.icon}</span><span className="progress-text">{p.text}</span></div>;
                    })}
                  </div>
                </div>
              </details>
            </div>
          )}
          {visibleNotice && (
            <div className="msg system">
              <div className="msg-meta">
                <span className="msg-role system">system</span>
                <span className="msg-time">—</span>
              </div>
              <div className="msg-body">{visibleNotice}</div>
            </div>
          )}
        </div>

        {!isLiveSession && (
          <div className="composer" style={{ textAlign: "center", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 11 }}>
            <div style={{ padding: "16px 0" }}>
              This is an archived session — open the <a style={{ color: "var(--accent)", cursor: "pointer", textDecoration: "underline" }}
                  onClick={function () { onSelectSession && onSelectSession(null); }}>live session</a> to send messages.
            </div>
          </div>
        )}
        {isLiveSession && pendingQuestion && (
          <QuestionPanel
            pendingQuestion={pendingQuestion}
            draft={questionDraft}
            onDraftChange={setQuestionDraft}
            onOptionSelect={function (label) { submitQuestionAnswer({ selectedOption: label }); }}
            onSubmit={function () { submitQuestionAnswer(); }}
            onKeyDown={onQuestionKey}
            answering={answeringQuestion}
            sending={sending}
            optionCount={questionOptionCount}
          />
        )}
        {isLiveSession && (
        <div className="composer">
          <div className="composer-box">
            <div className="composer-chips">
              {(session.chat.contextChips || []).map((c, i) => (
                <span className="chip" key={i}>
                  {c.icon} {c.label} <span className="x">×</span>
                </span>
              ))}
              {hasSelectedGuideRound && (
                <span className="chip chip-guide">
                  ↳ guide {currentGuideRoundTitle}
                  <span className="x" onClick={function () { setSelectedGuideRoundId(""); setSelectedGuideRoundTitle(""); setContextPickerOpen(false); }}>×</span>
                </span>
              )}
              <span
                className={"chip chip-add-context" + (liveRounds.length === 0 ? " disabled" : "")}
                style={{ borderStyle: "dashed", cursor: liveRounds.length === 0 ? "default" : "pointer" }}
                onClick={function () {
                  if (liveRounds.length === 0) return;
                  setContextPickerOpen(!contextPickerOpen);
                }}
              >
                + add context
              </span>
            </div>
            {contextPickerOpen && liveRounds.length > 0 && (
              <div className="context-picker">
                <div className="context-picker-head">Running rounds</div>
                {liveRounds.map(function (round) {
                  const isActive = selectedGuideRoundId === round.id;
                  return (
                    <button
                      key={round.id}
                      className={"context-option" + (isActive ? " active" : "")}
                      onClick={function () {
                        setSelectedGuideRoundId(round.id);
                        setSelectedGuideRoundTitle(round.title || round.id);
                        setContextPickerOpen(false);
                      }}
                    >
                      <span className={"sa-dot " + round.status} style={{ marginTop: 0 }}></span>
                      <span className="context-option-body">
                        <span className="context-option-title">{round.title}</span>
                        <span className="context-option-meta">
                          {round.elapsed} · {round.runningSubagents}/{round.subagentCount} subagents
                          {round.pendingGuidance ? " · " + round.pendingGuidance + " queued" : ""}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            <textarea
              ref={taRef}
              value={draft}
              onChange={autosize}
              onKeyDown={onKey}
              disabled={Boolean(pendingQuestion)}
              placeholder={
                pendingQuestion
                  ? "Answer the pending question above to continue this round…"
                  : ("Message " + DATA.assistantName + "… (⌘+↵ to send)")
              }
            />
            <div className="composer-actions">
              <button className="iconbtn" title="Attach">+</button>
              <button className="iconbtn" title="Slash command">/</button>
              <button className="iconbtn" title="Mention">@</button>
              <span style={{ flex: 1 }}></span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>
                {session.model}
              </span>
              {visibleSending && (
                <button className="send secondary" disabled={!draft.trim() || Boolean(pendingQuestion)} onClick={openNextDialogue}>
                  {hasSelectedGuideRound ? "guide" : "new dialogue"}
                </button>
              )}
              <button
                className={"send" + (visibleSending ? " stop" : "")}
                disabled={pendingQuestion ? true : (!visibleSending && !draft.trim())}
                onClick={visibleSending ? stopActiveRun : send}
              >
                {visibleSending ? "stop" : <>{hasSelectedGuideRound ? "guide" : "send"} <span className="kbd">⌘↵</span></>}
              </button>
            </div>
          </div>
          <div className="composer-hint">
            <span>
              {visibleSending
                ? (hasSelectedGuideRound
                    ? "Watching the current run. Type the next message, then click guide to send it to the selected round without waiting."
                    : "Watching the current run. Type the next message, then click new dialogue to send it without waiting.")
                : pendingQuestion
                ? "The main agent is waiting for your answer above before it can continue this round."
                : hasSelectedGuideRound
                ? "Guidance mode: this message will be sent to the selected round's main-agent inbox."
                : DATA.assistantName + " plans, then acts. Subagents spawn for parallel work."}
            </span>
            <span>
              {visibleSending ? "running · " : ""}
              {runningSubagents} active subagent(s)
            </span>
          </div>
        </div>
        )}
      </div>

      <ChatSide session={session} subagents={subagents} />
    </div>
  );
}

function Message({ msg, assistantName }) {
  const markdownBody = (msg.role === "agent" || msg.role === "system") && msg.body
    ? renderMarkdown(msg.body)
    : "";
  const isRuntimeTrace = Boolean(msg.runtimeTrace);
  const attachedRuntime = msg.attachedRuntime || null;
  const hasTrace = isRuntimeTrace || Boolean(msg.thinking || (msg.tools && msg.tools.length));
  const traceLabel = isRuntimeTrace
    ? (msg.traceSummary + (msg.traceElapsed ? " · " + msg.traceElapsed : ""))
    : traceSummary(msg);
  const runtimeSuffix = attachedRuntime
    ? " · " + attachedRuntime.summary.replace(/^details\s·\s/, "") + " · " + attachedRuntime.elapsed
    : "";
  return (
    <div className={"msg " + msg.role}>
      <div className="msg-meta">
        <span className={"msg-role " + msg.role}>
          {msg.role === "user" ? "▸ you" :
           msg.role === "agent" ? "● " + (assistantName || "agent") :
           msg.role}
        </span>
        <span className="msg-time">{attachedRuntime ? attachedRuntime.timeLabel : msg.time}</span>
      </div>

      {hasTrace && (
        <details className={"msg-trace" + (!msg.body ? " only-trace" : "") + (isRuntimeTrace ? " runtime-trace" : "")}>
          <summary className="msg-trace-summary">
            <span className="msg-trace-caret">▸</span>
            <span>{traceLabel + runtimeSuffix}</span>
          </summary>
          <div className="msg-trace-body">
            {isRuntimeTrace && (
              <div className="thinking">
                <div className="thinking-head">{msg.traceHead || "processing"}</div>
                {(msg.traceEntries || []).map(function (entry, index) {
                  return (
                    <div key={index} className="progress-entry">
                      <span className="progress-icon">{entry.icon}</span>
                      <span className="progress-text">{entry.text}</span>
                    </div>
                  );
                })}
              </div>
            )}
            {!isRuntimeTrace && msg.thinking && (
              <div className="thinking">
                <div className="thinking-head">reasoning</div>
                {msg.thinking}
              </div>
            )}
            {!isRuntimeTrace && msg.tools && msg.tools.map((t, i) => <ToolCard key={i} tool={t} />)}
            {attachedRuntime && (
              <div className="thinking">
                <div className="thinking-head">{attachedRuntime.head}</div>
                {attachedRuntime.entries.map(function (entry, index) {
                  return (
                    <div key={index} className="progress-entry">
                      <span className="progress-icon">{entry.icon}</span>
                      <span className="progress-text">{entry.text}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </details>
      )}

      {msg.body && (
        msg.role === "agent" || msg.role === "system"
          ? <div className="msg-body markdown" dangerouslySetInnerHTML={{ __html: markdownBody }}></div>
          : <div className="msg-body">{msg.body}</div>
      )}
    </div>
  );
}

function QuestionPanel({ pendingQuestion, draft, onDraftChange, onOptionSelect, onSubmit, onKeyDown, answering, sending, optionCount }) {
  if (!pendingQuestion) return null;
  const options = Array.isArray(pendingQuestion.options) ? pendingQuestion.options : [];
  const customDisabled = answering || sending;
  return (
    <div className="question-panel">
      <div className="question-panel-head">
        <span className="question-panel-kicker">clarification needed</span>
        <span className="question-panel-meta">
          {optionCount ? optionCount + " option" + (optionCount === 1 ? "" : "s") + " + custom answer" : "custom answer"}
        </span>
      </div>
      <div className="question-panel-body">
        <div className="question-panel-title">{pendingQuestion.text}</div>
        {options.length > 0 && (
          <div className="question-options">
            {options.map(function (option) {
              return (
                <button
                  key={option.id}
                  className="question-option"
                  disabled={customDisabled}
                  onClick={function () { onOptionSelect && onOptionSelect(option.label); }}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        )}
        <div className="question-custom">
          <textarea
            className="question-textarea"
            value={draft}
            onChange={function (e) { onDraftChange && onDraftChange(e.target.value); }}
            onKeyDown={onKeyDown}
            disabled={customDisabled}
            placeholder="Type your answer… (⌘+↵ to submit)"
          />
          <button
            className="question-submit"
            disabled={customDisabled || !String(draft || "").trim()}
            onClick={onSubmit}
          >
            answer <span className="kbd">⌘↵</span>
          </button>
        </div>
      </div>
    </div>
  );
}

function ToolCard({ tool }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tool-card">
      <div className="tool-head" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
        <span>{open ? "▾" : "▸"}</span>
        <span className="name">{tool.name}</span>
        <span className="arg">({tool.arg})</span>
        <span className={"pill " + (tool.status === "running" ? "running" : tool.status === "err" ? "err" : "")}>
          {tool.status}
        </span>
      </div>
      {open && tool.out && <div className="tool-body">{tool.out}</div>}
    </div>
  );
}

function ChatSide({ session, subagents }) {
  return (
    <div className="chat-side">
      <div className="side-section" style={{ maxHeight: "40%", overflowY: "auto" }}>
        <div className="side-head">
          Active shells
          <span className="count">{session.shells.length}</span>
        </div>
        {session.shells.length === 0 && (
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)" }}>—</div>
        )}
        {session.shells.map((s) => <ShellCard key={s.id} shell={s} />)}
      </div>

      <div className="side-section" style={{ flex: 1, overflowY: "auto" }}>
        <div className="side-head">
          Subagents
          <span className="count">{subagents.length}</span>
        </div>
        {subagents.length === 0 && (
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)" }}>—</div>
        )}
        {subagents.map((s) => <SubagentMini key={s.id} sa={s} />)}
      </div>

      <div className="side-section" style={{ borderBottom: 0 }}>
        <div className="side-head">Run summary</div>
        <div className="kv" style={{ rowGap: 6 }}>
          <span className="k">run id</span><span className="v">{session.id}</span>
          <span className="k">started</span><span className="v">{session.started}</span>
          <span className="k">elapsed</span><span className="v">{session.dur}</span>
          <span className="k">tool calls</span><span className="v">{session.summary.toolCalls}</span>
          <span className="k">tokens</span><span className="v">{session.summary.tokens}</span>
          <span className="k">spend</span><span className="v">{session.summary.spend}</span>
        </div>
      </div>
    </div>
  );
}

function ShellCard({ shell }) {
  return (
    <div className="shell-card">
      <div className="shell-card-head">
        <span>▣</span>
        <span>{shell.title || "independent shell"}</span>
        <span className="cwd">{shell.cwd}</span>
        <span className={"pill " + (shell.status === "running" ? "running" : shell.status === "err" ? "err" : "")}>{shell.status}</span>
        <span className="pid">pid {shell.pid}</span>
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

function SubagentMini({ sa }) {
  return (
    <div className="subagent-mini">
      <div className={"sa-dot " + sa.status}></div>
      <div className="sa-body">
        <div className="sa-name">
          {sa.name} <span className="id">· {sa.id}</span>
        </div>
        <div className="sa-task">{sa.task}</div>
        <div className="sa-meta">
          <span><b>tok</b> {sa.tokens || 0}</span>
          <span><b>t+</b> {sa.elapsed || "—"}</span>
          <span style={{ marginLeft: "auto" }}>{sa.status}</span>
        </div>
        {sa.status === "running" && (
          <div className="bar warn"><div style={{ width: ((sa.progress || 0.5) * 100) + "%" }}></div></div>
        )}
      </div>
    </div>
  );
}

window.ChatPage = ChatPage;
