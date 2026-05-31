// Diff viewer panel for the right sidebar.
// Exposes window.CyreneDiffViewer API and window.DiffViewerPanel component.

(function () {
  if (typeof window === "undefined") return;
  if (typeof React === "undefined") return;

  var useState = React.useState;
  var useEffect = React.useEffect;
  var createElement = React.createElement;

  function diffT(key, fallback, vars) {
    if (typeof window !== "undefined" && typeof window.t === "function") {
      return window.t(key, vars);
    }
    return fallback;
  }

  function isBinaryDiff(text) {
    return text && /^Binary files /.test(text.trim());
  }

  function parseDiff(text) {
    if (!text) return [];
    var lines = text.split("\n");
    var hunks = [];
    var currentHunk = null;
    var leftLine = 0;
    var rightLine = 0;

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("@@") === 0) {
        if (currentHunk) hunks.push(currentHunk);
        var match = line.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@/);
        leftLine = match ? parseInt(match[1], 10) : 0;
        rightLine = match ? parseInt(match[2], 10) : 0;
        currentHunk = { header: line, lines: [], leftStart: leftLine, rightStart: rightLine };
      } else if (currentHunk) {
        if (line.indexOf("+") === 0) {
          currentHunk.lines.push({ type: "add", text: line.slice(1), leftNum: null, rightNum: rightLine });
          rightLine++;
        } else if (line.indexOf("-") === 0) {
          currentHunk.lines.push({ type: "del", text: line.slice(1), leftNum: leftLine, rightNum: null });
          leftLine++;
        } else if (line.indexOf(" ") === 0) {
          currentHunk.lines.push({ type: "ctx", text: line.slice(1), leftNum: leftLine, rightNum: rightLine });
          leftLine++;
          rightLine++;
        }
      }
    }
    if (currentHunk) hunks.push(currentHunk);
    return hunks;
  }

  function DiffViewerPanel(props) {
    var [diffText, setDiffText] = useState(props.diff || "");
    var [loading, setLoading] = useState(false);

    useEffect(function () {
      if (props.mode === "file" && props.left && props.right) {
        setLoading(true);
        fetch("/api/code/diff", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: "file", left: props.left, right: props.right }),
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            setDiffText(data.diff || data.error || "");
            setLoading(false);
          })
          .catch(function (e) {
            setDiffText(diffT("chat.diff.errorPrefix", "Error") + ": " + (e.message || e));
            setLoading(false);
          });
      } else if (props.diff) {
        setDiffText(props.diff);
      }
    }, [props.diff, props.left, props.right, props.mode]);

    var binary = isBinaryDiff(diffText);
    var hunks = parseDiff(diffText);

    return createElement("div", {
      className: "diff-viewer-panel",
      style: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
    },
      // Header
      createElement("div", { className: "diff-viewer-header" },
        createElement("span", { className: "diff-viewer-title" }, diffT("chat.diff.title", "Diff")),
        props.left && props.right && createElement("span", { className: "diff-viewer-files" },
          props.left + " → " + props.right
        ),
        createElement("span", { style: { flex: 1 } }),
        props.onClose && createElement("button", {
          className: "code-editor-close-btn",
          onClick: props.onClose,
        }, "×")
      ),
      // Content
      loading
        ? createElement("div", {
            className: "diff-viewer-empty",
          }, diffT("chat.diff.loading", "Loading diff..."))
        : createElement("div", { className: "diff-viewer-content" },
            (function () {
              if (!diffText) {
                return createElement("div", { className: "diff-viewer-empty" }, diffT("chat.diff.noDifferences", "No differences"));
              }
              if (binary) {
                return createElement("div", { className: "diff-viewer-empty" }, diffText.trim());
              }
              if (hunks.length === 0) {
                return createElement("div", { className: "diff-viewer-empty" }, diffT("chat.diff.noDifferences", "No differences"));
              }
              return hunks.map(function (hunk, hi) {
                return createElement("div", { key: "h" + hi, className: "diff-hunk" },
                  createElement("div", { className: "diff-hunk-header" }, hunk.header),
                  hunk.lines.map(function (l, li) {
                    return createElement("div", {
                      key: "l" + li,
                      className: "diff-line diff-line-" + l.type,
                    },
                      createElement("span", { className: "diff-ln diff-ln-left" }, l.leftNum != null ? l.leftNum : ""),
                      createElement("span", { className: "diff-ln diff-ln-right" }, l.rightNum != null ? l.rightNum : ""),
                      createElement("span", { className: "diff-text" }, l.text)
                    );
                  })
                );
              });
            })()
          )
    );
  }

  // ── Global API ──

  window.CyreneDiffViewer = {
    Panel: DiffViewerPanel,
    open: function (diffText) {
      window.dispatchEvent(
        new CustomEvent("cyrene:open-diff", {
          detail: { diff: diffText || "", mode: "text" },
          bubbles: true,
        })
      );
    },
    openFiles: function (left, right) {
      window.dispatchEvent(
        new CustomEvent("cyrene:open-diff", {
          detail: { mode: "file", left: left, right: right },
          bubbles: true,
        })
      );
    },
  };

  window.DiffViewerPanel = DiffViewerPanel;
})();
