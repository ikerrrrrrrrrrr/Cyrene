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
  const queuedGuidanceId = String(msg && msg.queuedGuidanceId || "");
  if (queuedGuidanceId) return "guide::" + queuedGuidanceId;
  return [
    String(msg && msg.role || ""),
    String(msg && msg.roundId || ""),
    String(msg && msg.body || ""),
  ].join("::");
}

function unmatchedRetainedMessages(sessionMessages, retainedMessages) {
  const counts = new Map();
  (sessionMessages || []).forEach(function (msg) {
    const key = messageKey(msg);
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const visible = [];
  (retainedMessages || []).forEach(function (msg) {
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
  const subagents = visibleRoundSubagents(session);
  const liveRounds = selectableLiveRounds(session);
  const runningSubagents = subagents.filter((s) => s.status === "running").length;

  // Expose global session switcher so the sidebar can switch the chat view
  useEffect(function () {
    window.selectChatSession = function (id) { onSelectSession && onSelectSession(id); };
    return function () { delete window.selectChatSession; };
  }, [onSelectSession]);

  const [draft, setDraft] = useState("");
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
  const retainedMessages = unmatchedRetainedMessages(session.chat.messages, runtimeState.retainedMessages || []);
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
  const watchingGuidance = Boolean(activeRequest && activeRequest.guideRoundId);
  const liveElapsed = runtimeState.startedAt ? formatElapsedMs(elapsedNow - runtimeState.startedAt) : "00:00";

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [session.chat.messages.length, pendingMessages.length, liveProgress.length, sending, notice]);

  useEffect(() => {
    const runtime = getChatRuntime();
    runtime.listeners.add(setRuntimeState);
    setRuntimeState(getChatRuntimeSnapshot());
    return function () { runtime.listeners.delete(setRuntimeState); };
  }, []);

  useEffect(function () {
    if (!runtimeState.retainedMessages || runtimeState.retainedMessages.length === 0) return;
    const nextRetained = unmatchedRetainedMessages(session.chat.messages, runtimeState.retainedMessages);
    if (nextRetained.length === runtimeState.retainedMessages.length) return;
    updateChatRuntime({ retainedMessages: nextRetained });
  }, [session.chat.messages.length, runtimeState.retainedMessages]);

  useEffect(function () {
    if (!selectedGuideRound || !selectedGuideRound.title) return;
    if (selectedGuideRound.title === selectedGuideRoundTitle) return;
    setSelectedGuideRoundTitle(selectedGuideRound.title);
  }, [selectedGuideRound, selectedGuideRoundTitle]);

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

  async function send() {
    const text = draft.trim();
    const runtime = getChatRuntime();
    if (!text) return;
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
    };
    updateChatRuntime({
      sending: true,
      startedAt: Date.now(),
      pendingMessages: [userMsg],
      liveProgress: [],
      activeRequest: {
        id: requestId,
        message: text,
        guideRoundId: requestMeta.guideRoundId,
        guideRoundTitle: requestMeta.guideRoundTitle,
      },
      watchRequestId: requestId,
    });
    ensureChatRuntimeSseSubscription();
    setDraft("");
    syncTextareaHeight(taRef.current);

    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          guide_round_id: selectedGuideRoundId || undefined,
        }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const isWatching = getChatRuntime().watchRequestId === requestId;
      if (data.queued) {
        if (window.refreshSessions) {
          await window.refreshSessions();
        }
        if (isWatching) {
          updateChatRuntime(function (state) {
            return {
              pendingMessages: [],
              retainedMessages: state.retainedMessages.concat([{
                ...userMsg,
                queuedGuidanceId: data.guide_request_id || "",
              }]),
            };
          });
          setNotice(data.response || "Guidance queued.");
        }
        return;
      }
      const agentMsg = {
        id: "pending_agent_" + Date.now(),
        role: "agent", time: new Date().toLocaleTimeString(),
        body: data.response || "(no response)",
      };
      if (isWatching) {
        updateChatRuntime(function (state) {
          return { pendingMessages: state.pendingMessages.concat([agentMsg]) };
        });
      }
      // Refresh sessions FIRST so the run_live entry contains the new
      // messages before we clear pending — otherwise there's a flash of
      // "No messages yet" between pending-clear and sessions-arriving.
      if (window.refreshSessions) {
        await window.refreshSessions();
      }
      if (isWatching) {
        updateChatRuntime({ pendingMessages: [] });
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

  const allMessages = [...session.chat.messages, ...retainedMessages, ...pendingMessages];

  async function newSession() {
    if (!confirm("Start a new session? The current conversation will be compressed into short-term memory.")) return;
    try {
      const runtime = getChatRuntime();
      Object.values(runtime.requests || {}).forEach(function (requestMeta) {
        if (requestMeta && requestMeta.controller) requestMeta.controller.abort();
      });
      runtime.requests = {};
      runtime.requestSeq = 0;
      const r = await fetch("/api/sessions", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
      onSelectSession && onSelectSession(null);
      updateChatRuntime({ pendingMessages: [], retainedMessages: [], sending: false, startedAt: 0, liveProgress: [], activeRequest: null, watchRequestId: "" });
      clearChatRuntimeSseSubscription();
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
              <span>Viewing archive · {session.id.replace("day_", "")}</span>
              <span className="archive-banner-action"
                    onClick={function () { onSelectSession && onSelectSession(null); }}>
                ← return to live session
              </span>
            </div>
          )}
          {allMessages.length === 0 && (
            <div style={{ padding: "40px 0", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
              No messages yet. Say hello to {DATA.assistantName}.
            </div>
          )}
          {allMessages.map((m) => <Message key={m.id} msg={m} assistantName={DATA.assistantName} />)}
          {sending && (
            <div className="msg agent">
              <div className="msg-meta">
                <span className="msg-role agent">● {DATA.assistantName}</span>
                <span className="msg-time">{watchingGuidance ? "guiding…" : "…"}</span>
              </div>
              <details className="msg-trace only-trace runtime-trace">
                <summary className="msg-trace-summary">
                  <span className="msg-trace-caret">▸</span>
                  <span>{watchingGuidance ? "details · queued guidance" : "details · processing"} · {liveElapsed}</span>
                </summary>
                <div className="msg-trace-body">
                  <div className="thinking">
                    <div className="thinking-head">{watchingGuidance ? "queue" : "processing"}</div>
                    {watchingGuidance && (
                      <div className="progress-entry">
                        <span className="progress-icon">↳</span>
                        <span className="progress-text">Target round: {activeGuideRoundTitle || activeRequest.guideRoundId}</span>
                      </div>
                    )}
                    {liveProgress.length === 0 && <div className="progress-entry"><span className="progress-icon">◎</span><span className="progress-text">{watchingGuidance ? "Queueing follow-up…" : "Thinking..."}</span></div>}
                    {liveProgress.map(function (p, i) {
                      return <div key={i} className="progress-entry"><span className="progress-icon">{p.icon}</span><span className="progress-text">{p.text}</span></div>;
                    })}
                  </div>
                </div>
              </details>
            </div>
          )}
          {notice && (
            <div className="msg system">
              <div className="msg-meta">
                <span className="msg-role system">system</span>
                <span className="msg-time">—</span>
              </div>
              <div className="msg-body">{notice}</div>
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
              placeholder={"Message " + DATA.assistantName + "… (⌘+↵ to send)"}
            />
            <div className="composer-actions">
              <button className="iconbtn" title="Attach">+</button>
              <button className="iconbtn" title="Slash command">/</button>
              <button className="iconbtn" title="Mention">@</button>
              <span style={{ flex: 1 }}></span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>
                {session.model}
              </span>
              {sending && (
                <button className="send secondary" disabled={!draft.trim()} onClick={openNextDialogue}>
                  {hasSelectedGuideRound ? "guide" : "new dialogue"}
                </button>
              )}
              <button
                className={"send" + (sending ? " stop" : "")}
                disabled={!sending && !draft.trim()}
                onClick={sending ? stopActiveRun : send}
              >
                {sending ? "stop" : <>{hasSelectedGuideRound ? "guide" : "send"} <span className="kbd">⌘↵</span></>}
              </button>
            </div>
          </div>
          <div className="composer-hint">
            <span>
              {sending
                ? (hasSelectedGuideRound
                    ? "Watching the current run. Type the next message, then click guide to send it to the selected round without waiting."
                    : "Watching the current run. Type the next message, then click new dialogue to send it without waiting.")
                : hasSelectedGuideRound
                ? "Guidance mode: this message will queue behind the selected round's current main-agent output."
                : DATA.assistantName + " plans, then acts. Subagents spawn for parallel work."}
            </span>
            <span>
              {sending ? "running · " : ""}
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
  const hasTrace = Boolean(msg.thinking || (msg.tools && msg.tools.length));
  return (
    <div className={"msg " + msg.role}>
      <div className="msg-meta">
        <span className={"msg-role " + msg.role}>
          {msg.role === "user" ? "▸ you" :
           msg.role === "agent" ? "● " + (assistantName || "agent") :
           msg.role}
        </span>
        <span className="msg-time">{msg.time}</span>
      </div>

      {hasTrace && (
        <details className={"msg-trace" + (!msg.body ? " only-trace" : "")}>
          <summary className="msg-trace-summary">
            <span className="msg-trace-caret">▸</span>
            <span>{traceSummary(msg)}</span>
          </summary>
          <div className="msg-trace-body">
            {msg.thinking && (
              <div className="thinking">
                <div className="thinking-head">reasoning</div>
                {msg.thinking}
              </div>
            )}
            {msg.tools && msg.tools.map((t, i) => <ToolCard key={i} tool={t} />)}
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
