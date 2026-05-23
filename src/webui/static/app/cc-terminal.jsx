const { useEffect, useRef, useState } = React;

function ccText(key, fallback) {
  return typeof window.t === "function" ? window.t(key) : fallback;
}

function ccWsUrl(tmuxSession) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return protocol + "//" + window.location.host + "/ws/cc-terminal/" + encodeURIComponent(tmuxSession);
}

function ccStatusLabel(status) {
  if (status === "running") return "live";
  if (status === "connecting") return "connecting";
  if (status === "offline") return "offline";
  if (status === "unsupported") return "missing xterm";
  return "unavailable";
}

function ccLearningText(learningSnapshot, liveLearning, compactOnly) {
  if (compactOnly) {
    return ccText("chat.ccLearningActive", "Cyrene learning");
  }
  if (liveLearning && liveLearning.phase === "started" && liveLearning.user_input) {
    return "Learning from: " + liveLearning.user_input;
  }
  if (liveLearning && Array.isArray(liveLearning.highlights) && liveLearning.highlights.length) {
    return liveLearning.highlights.slice(0, 2).join(" · ");
  }
  if (learningSnapshot && learningSnapshot.summary && Array.isArray(learningSnapshot.summary.highlights) && learningSnapshot.summary.highlights.length) {
    return learningSnapshot.summary.highlights.slice(0, 2).join(" · ");
  }
  return ccText("chat.ccLearningIdle", "Watching your Claude Code habits.");
}

function CCTerminalPanel({ statusInfo, onRefresh, modal, onClose }) {
  const tmuxSession = statusInfo && statusInfo.tmux_session ? statusInfo.tmux_session : "";
  const available = Boolean(statusInfo && statusInfo.available && tmuxSession);
  const containerRef = useRef(null);
  const terminalRef = useRef(null);
  const fitAddonRef = useRef(null);
  const wsRef = useRef(null);
  const syncSizeRef = useRef(function () {});
  const [expanded, setExpanded] = useState(Boolean(modal));
  const [connectionState, setConnectionState] = useState(available ? "connecting" : "unavailable");
  const [escCount, setEscCount] = useState(-1);
  const [learningSnapshot, setLearningSnapshot] = useState(null);
  const [liveLearning, setLiveLearning] = useState(null);

  useEffect(function () {
    setExpanded(Boolean(modal));
  }, [modal]);

  useEffect(function () {
    setConnectionState(available ? "connecting" : "unavailable");
  }, [available, tmuxSession]);

  useEffect(function () {
    if (!available) {
      setLearningSnapshot(null);
      return;
    }
    let cancelled = false;
    fetch("/api/cc/learning")
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (cancelled) return;
        if (payload && payload.available) {
          setLearningSnapshot(payload);
        }
      })
      .catch(function () {});
    return function () {
      cancelled = true;
    };
  }, [available, statusInfo && statusInfo.latest_jsonl, tmuxSession]);

  useEffect(function () {
    if (!window.__sseHandlers) return undefined;
    function handleEvent(event) {
      if (!event || event.type !== "cc_learning") return;
      if (event.tmux_session && tmuxSession && event.tmux_session !== tmuxSession) return;
      setLiveLearning(event);
      if (event.phase === "completed") {
        window.setTimeout(function () {
          fetch("/api/cc/learning")
            .then(function (response) { return response.json(); })
            .then(function (payload) {
              if (payload && payload.available) setLearningSnapshot(payload);
            })
            .catch(function () {});
        }, 120);
      }
      window.setTimeout(function () {
        setLiveLearning(function (current) {
          return current === event ? null : current;
        });
      }, 5000);
    }
    window.__sseHandlers.add(handleEvent);
    return function () {
      window.__sseHandlers.delete(handleEvent);
    };
  }, [tmuxSession]);

  useEffect(function () {
    if (!available || !containerRef.current) return undefined;
    var TerminalCtor = window.Terminal;
    if (!TerminalCtor) {
      setConnectionState("unsupported");
      return undefined;
    }

    var term = new TerminalCtor({
      allowTransparency: false,
      convertEol: true,
      cursorBlink: true,
      cursorStyle: "block",
      drawBoldTextInBrightColors: false,
      fontFamily: "\"IBM Plex Mono\", ui-monospace, Menlo, monospace",
      fontSize: expanded ? 14 : 11,
      lineHeight: 1.2,
      rows: expanded ? 32 : 10,
      cols: expanded ? 132 : 48,
      scrollback: 3000,
      theme: {
        background: "#0b0f14",
        foreground: "#e6edf3",
        cursor: "#7ee787",
        cursorAccent: "#0b0f14",
        selectionBackground: "rgba(99, 179, 143, 0.22)",
        black: "#0b0f14",
        red: "#ff7b72",
        green: "#7ee787",
        yellow: "#d29922",
        blue: "#79c0ff",
        magenta: "#bc8cff",
        cyan: "#39c5cf",
        white: "#c9d1d9",
        brightBlack: "#6e7681",
        brightRed: "#ffa198",
        brightGreen: "#56d364",
        brightYellow: "#e3b341",
        brightBlue: "#a5d6ff",
        brightMagenta: "#d2a8ff",
        brightCyan: "#56d4dd",
        brightWhite: "#f0f6fc",
      },
    });

    // FitAddon: try to load if available, otherwise manual resize
    var FitAddonCtor = (window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon)) || null;
    var fitAddon = null;
    if (FitAddonCtor) {
      try {
        fitAddon = new FitAddonCtor();
        fitAddonRef.current = fitAddon;
        term.loadAddon(fitAddon);
      } catch (e) { /* ignore */ }
    }

    term.open(containerRef.current);
    terminalRef.current = term;

    function syncSize() {
      if (!terminalRef.current) return;
      var el = containerRef.current;
      if (!el) return;
      if (fitAddonRef.current && typeof fitAddonRef.current.fit === "function") {
        try { fitAddonRef.current.fit(); } catch (e) { /* ignore */ }
      } else {
        try {
          var dims = term._core && term._core._renderService && term._core._renderService.dimensions;
          var cellW = dims && dims.css && dims.css.cell && dims.css.cell.width;
          var cellH = dims && dims.css && dims.css.cell && dims.css.cell.height;
          if (cellW > 0 && cellH > 0) {
            var newCols = Math.floor((el.clientWidth - 16) / cellW);
            var newRows = Math.floor((el.clientHeight - 8) / cellH);
            if (newCols > 10 && newRows > 3) {
              term.resize(Math.max(20, newCols), Math.max(4, newRows));
            }
          } else {
            window.requestAnimationFrame(syncSize);
            return;
          }
        } catch (e) { /* ignore */ }
      }
      var socket = wsRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
          type: "resize",
          cols: terminalRef.current.cols || (expanded ? 120 : 48),
          rows: terminalRef.current.rows || (expanded ? 32 : 10),
        }));
      }
    }

    syncSizeRef.current = syncSize;

    const ws = new WebSocket(ccWsUrl(tmuxSession));
    wsRef.current = ws;

    ws.onopen = function () {
      setConnectionState("running");
      syncSize();
      term.focus();
    };
    ws.onmessage = function (event) {
      var data = event.data;
      var count = 0;
      if (typeof data === "string") {
        for (var i = 0; i < data.length; i++) {
          if (data.charCodeAt(i) === 0x1b) count++;
        }
        setEscCount(count);
      }
      term.write(data);
      // Write diagnostic AFTER data so \x1bc doesn't erase it
      if (window.__ccFirstFrame && typeof data === "string") {
        window.__ccFirstFrame = false;
        console.log("CC DEBUG: first frame ESC count =", count);
        term.write("\r\n\x1b[33m⚠ ESC count: " + count + "\x1b[0m\r\n");
      }
    };

    window.__ccFirstFrame = true;
    ws.onerror = function () {
      setConnectionState("offline");
    };
    ws.onclose = function () {
      setConnectionState("offline");
    };

    term.onData(function (data) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data: data }));
      }
    });

    const resizeHandler = function () {
      window.requestAnimationFrame(syncSize);
    };
    window.addEventListener("resize", resizeHandler);

    var resizeObserver = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(function () {
        window.requestAnimationFrame(syncSize);
      });
      resizeObserver.observe(containerRef.current);
    }

    window.requestAnimationFrame(syncSize);

    return function () {
      window.removeEventListener("resize", resizeHandler);
      if (resizeObserver) resizeObserver.disconnect();
      ws.close();
      wsRef.current = null;
      fitAddonRef.current = null;
      term.dispose();
      terminalRef.current = null;
    };
  }, [available, tmuxSession]);

  useEffect(function () {
    const term = terminalRef.current;
    if (!term) return;
    term.options.fontSize = expanded ? 14 : 11;
    window.requestAnimationFrame(function () {
      if (syncSizeRef.current) syncSizeRef.current();
      if (expanded) term.focus();
    });
  }, [expanded]);

  if (!statusInfo) {
    return null;
  }

  function fillChatInput(text) {
    var el = document.querySelector(".chat-input textarea, .chat-input input, [data-cc-launch-input]");
    if (!el) return;
    var proto = window.HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var nativeSetter = Object.getOwnPropertyDescriptor(proto, "value");
    if (nativeSetter && nativeSetter.set) {
      nativeSetter.set.call(el, text);
    } else {
      el.value = text;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.focus();
  }

  if (!available) {
    return (
      <div className="cc-terminal cc-terminal--inactive">
        <div className="cc-terminal__header">
          <div className="cc-terminal__titleRow">
            <span className="cc-terminal__eyebrow">Claude Code</span>
            <span className="cc-terminal__title">{ccText("chat.ccUnavailable", "Terminal unavailable")}</span>
          </div>
          <div className="cc-terminal__actions">
            {statusInfo.can_launch && (
              <button
                className="cc-terminal__button cc-terminal__button--accent"
                onClick={function () { fillChatInput(ccText("chat.ccLaunchHint", "请帮我启动 Claude Code")); }}
              >
                {ccText("chat.ccLaunch", "Launch Claude Code")}
              </button>
            )}
            <button className="cc-terminal__button" onClick={onRefresh}>
              {ccText("chat.ccRetry", "Retry")}
            </button>
          </div>
        </div>
        <div className="cc-terminal__empty">
          <div className="cc-terminal__emptyReason">{statusInfo.reason || "No usable tmux session was found."}</div>
          {statusInfo.can_launch && (
            <div className="cc-terminal__emptyHint">
              {ccText("chat.ccLaunchHintText", "Ask Cyrene to launch Claude Code for you, or click the button above.")}
            </div>
          )}
          {statusInfo.project_dir && (
            <div className="cc-terminal__emptyMeta">{statusInfo.project_dir}</div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={"cc-terminal" + (expanded ? " cc-terminal--expanded" : "") + (modal ? " cc-terminal--modal" : "")}>
      {expanded && <div className="cc-terminal__backdrop" onClick={function () {
        if (modal && typeof onClose === "function") {
          onClose();
        } else {
          setExpanded(false);
        }
      }}></div>}
      <div className="cc-terminal__panel">
        <div className="cc-terminal__header">
          <div className="cc-terminal__titleRow">
            <span className="cc-terminal__title">Claude Code</span>
            <span className={"cc-terminal__status cc-terminal__status--" + connectionState}>
              {ccStatusLabel(connectionState)}
            </span>
            <span className="cc-terminal__esc" title={"ESC sequences in last update: " + escCount}>
              {"ESC:" + escCount}
            </span>
          </div>
          <div className="cc-terminal__actions">
            <button className="cc-terminal__button" onClick={onRefresh}>
              {ccText("chat.ccRefresh", "Refresh")}
            </button>
            <button
              className="cc-terminal__button cc-terminal__button--accent"
              onClick={function () {
                if (modal) {
                  if (typeof onClose === "function") onClose();
                } else {
                  setExpanded(!expanded);
                }
              }}
            >
              {modal ? ccText("chat.close", "Close") : (expanded ? ccText("chat.ccShrink", "Shrink") : ccText("chat.ccExpand", "Expand"))}
            </button>
          </div>
        </div>

        <div className="cc-terminal__surface">
          <div
            ref={containerRef}
            className="cc-terminal__viewport"
            onClick={function () {
              if (terminalRef.current) terminalRef.current.focus();
            }}
          ></div>
        </div>

        <div className="cc-terminal__footer">
          <div className="cc-terminal__learningLabel">{ccText("chat.ccLearning", "Cyrene learning")}</div>
          <div className="cc-terminal__learningText">{ccLearningText(learningSnapshot, liveLearning, Boolean(expanded || modal))}</div>
          {learningSnapshot && learningSnapshot.summary && Array.isArray(learningSnapshot.summary.top_tools) && learningSnapshot.summary.top_tools.length > 0 && (
            <div className="cc-terminal__meta">
              {ccText("chat.ccTopTools", "Top tools")}: {learningSnapshot.summary.top_tools.join(", ")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

window.CCTerminalPanel = CCTerminalPanel;
