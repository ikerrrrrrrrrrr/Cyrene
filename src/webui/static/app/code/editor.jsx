// CodeMirror 6 editor panel for the right sidebar.
// Exposes window.CyreneCodeEditor API and listens for cyrene:open-editor events.

(function () {
  if (typeof window === "undefined") return;
  if (typeof React === "undefined") return;

  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  var useCallback = React.useCallback;

  // ── CodeMirror module loader (lazy, from importmap) ──

  var cmReady = null;
  function ensureCodeMirror() {
    if (cmReady) return cmReady;
    cmReady = Promise.all([
      import("codemirror"),
      import("@codemirror/state"),
      import("@codemirror/view"),
      import("@codemirror/commands"),
      import("@codemirror/language"),
      import("@codemirror/lang-python"),
      import("@codemirror/lang-javascript"),
      import("@codemirror/lang-html"),
      import("@codemirror/lang-css"),
      import("@codemirror/lang-json"),
      import("@codemirror/lang-markdown"),
      import("@codemirror/autocomplete"),
      import("@codemirror/theme-one-dark"),
    ]).then(function (mods) {
      return {
        EditorView: mods[0].EditorView,
        EditorState: mods[1].EditorState,
        keymap: mods[2].keymap,
        lineNumbers: mods[2].lineNumbers,
        highlightActiveLine: mods[2].highlightActiveLine,
        highlightActiveLineGutter: mods[2].highlightActiveLineGutter,
        drawSelection: mods[2].drawSelection,
        dropCursor: mods[2].dropCursor,
        highlightSpecialChars: mods[2].highlightSpecialChars,
        rectangularSelection: mods[2].rectangularSelection,
        defaultKeymap: mods[3].defaultKeymap,
        history: mods[3].history,
        historyKeymap: mods[3].historyKeymap,
        indentOnInput: mods[4].indentOnInput,
        bracketMatching: mods[4].bracketMatching,
        foldGutter: mods[4].foldGutter,
        foldKeymap: mods[4].foldKeymap,
        syntaxHighlighting: mods[4].syntaxHighlighting,
        defaultHighlightStyle: mods[4].defaultHighlightStyle,
        python: mods[5].python,
        javascript: mods[6].javascript,
        html: mods[7].html,
        css: mods[8].css,
        json: mods[9].json,
        markdown: mods[10].markdown,
        autocompletion: mods[11].autocompletion,
        closeBrackets: mods[11].closeBrackets,
        oneDark: mods[12].oneDark,
      };
    }).catch(function (e) {
      console.error("CodeMirror: failed to load modules", e);
      cmReady = null;
      throw e;
    });
    return cmReady;
  }

  function langForFile(path, language) {
    if (language) return language;
    var ext = (path || "").split(".").pop().toLowerCase();
    var map = {
      py: "python", js: "javascript", ts: "typescript", jsx: "javascript",
      tsx: "typescript", html: "html", htm: "html", css: "css",
      json: "json", md: "markdown", yaml: "yaml", yml: "yaml",
      toml: "toml", xml: "xml", sql: "sql", sh: "shell", bash: "shell",
      rs: "rust", go: "go", java: "java", c: "c", cpp: "cpp", h: "c",
      rb: "ruby", php: "php", swift: "swift", kt: "kotlin",
    };
    return map[ext] || "text";
  }

  function langExtension(lang) {
    switch (lang) {
      case "python": case "py": return "python";
      case "javascript": case "js": case "typescript": case "ts": return "javascript";
      case "html": return "html";
      case "css": return "css";
      case "json": return "json";
      case "markdown": case "md": return "markdown";
      default: return null;
    }
  }

  function getExtension(lang) {
    var cmLang = langExtension(lang);
    if (!cmLang) return null;
    // We'll resolve this at mount time from the loaded modules.
    return cmLang;
  }

  // ── CodeEditorPanel Component ──

  function CodeEditorPanel(props) {
    var initialCode = props.code || "";
    var filePath = props.filePath || "";
    var language = props.language || langForFile(filePath, "");
    var readOnly = props.readOnly || false;

    var editorRef = useRef(null);
    var viewRef = useRef(null);
    var containerRef = useRef(null);
    var [dirty, setDirty] = useState(false);
    var [saving, setSaving] = useState(false);
    var [saveMsg, setSaveMsg] = useState("");
    var [loaded, setLoaded] = useState(false);

    // Initialize CodeMirror
    useEffect(function () {
      var container = containerRef.current;
      if (!container) return;
      if (viewRef.current) return;

      var cancelled = false;

      ensureCodeMirror().then(function (CM) {
        if (cancelled) return;

        var langName = getExtension(language);
        var langMod = langName ? CM[langName] : null;
        var langFn = langMod ? (langMod.lang || langMod) : null;
        var extensions = [
          CM.lineNumbers(),
          CM.highlightActiveLineGutter(),
          CM.highlightSpecialChars(),
          CM.drawSelection(),
          CM.dropCursor(),
          CM.rectangularSelection(),
          CM.indentOnInput(),
          CM.bracketMatching(),
          CM.closeBrackets(),
          CM.autocompletion(),
          CM.highlightActiveLine(),
          CM.history(),
          CM.foldGutter(),
          CM.keymap.of([
            CM.defaultKeymap,
            CM.historyKeymap,
            CM.foldKeymap,
            { key: "Mod-s", run: function () { handleSave(); return true; }, preventDefault: true },
          ]),
          CM.syntaxHighlighting(CM.defaultHighlightStyle, { fallback: true }),
          CM.EditorView.updateListener.of(function (update) {
            if (update.docChanged) {
              setDirty(true);
              if (props.onChange) {
                props.onChange(update.state.doc.toString());
              }
            }
          }),
          CM.EditorView.theme({
            "&": { height: "100%" },
            ".cm-scroller": { overflow: "auto" },
            ".cm-content": { fontFamily: "var(--mono)", fontSize: "13px", lineHeight: "1.65" },
            ".cm-gutters": { fontFamily: "var(--mono)", fontSize: "11px" },
          }),
        ];

        if (langFn) extensions.push(langFn());
        if (readOnly) extensions.push(CM.EditorState.readOnly.of(true));

        var state = CM.EditorState.create({
          doc: initialCode,
          extensions: extensions,
        });

        var view = new CM.EditorView({ state: state, parent: container });
        viewRef.current = view;
        setLoaded(true);
      });

      return function () {
        cancelled = true;
        if (viewRef.current) {
          viewRef.current.destroy();
          viewRef.current = null;
        }
      };
    }, []);

    // Get current code
    var getCode = useCallback(function () {
      if (viewRef.current) {
        return viewRef.current.state.doc.toString();
      }
      return initialCode;
    }, [initialCode]);

    // Expose save handler
    var handleSave = useCallback(function () {
      var code = getCode();
      setSaving(true);
      setSaveMsg("");

      var lang = language || langForFile(filePath);
      fetch("/api/code/format", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code, language: lang }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var formatted = data.formatted || code;
          return fetch("/api/code/file", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: filePath || "untitled", content: formatted }),
          });
        })
        .then(function (r) { return r.json(); })
        .then(function (result) {
          setSaving(false);
          setDirty(false);
          setSaveMsg(result.error ? "Save failed" : "Saved");
          setTimeout(function () { setSaveMsg(""); }, 2000);
        })
        .catch(function (err) {
          setSaving(false);
          setSaveMsg("Error: " + (err.message || err));
        });
    }, [getCode, filePath, language]);

    return React.createElement("div", {
      className: "code-editor-panel",
      style: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
    },
      // Toolbar
      React.createElement("div", { className: "code-editor-toolbar" },
        filePath
          ? React.createElement("span", { className: "code-editor-path" }, filePath)
          : React.createElement("span", { className: "code-editor-path", style: { opacity: 0.5 } }, "New file"),
        React.createElement("span", { className: "code-editor-lang" }, language || "text"),
        dirty && React.createElement("span", { className: "code-editor-dirty" }, "•"),
        React.createElement("span", { style: { flex: 1 } }),
        saveMsg && React.createElement("span", { className: "code-editor-save-msg" }, saveMsg),
        !readOnly && React.createElement("button", {
          className: "code-editor-save-btn",
          disabled: saving,
          onClick: handleSave,
        }, saving ? "Saving..." : "Save"),
        props.onClose && React.createElement("button", {
          className: "code-editor-close-btn",
          onClick: function () {
            if (dirty && !confirm("Unsaved changes will be lost. Close anyway?")) return;
            props.onClose();
          },
        }, "×")
      ),
      // Editor container
      React.createElement("div", {
        ref: containerRef,
        className: "code-editor-container",
        style: { flex: 1, overflow: "hidden" },
      }),
      !loaded && React.createElement("div", {
        style: { padding: 24, textAlign: "center", color: "var(--text-4)", fontSize: 12 },
      }, "Loading editor...")
    );
  }

  // ── Global API ──

  window.CyreneCodeEditor = {
    Panel: CodeEditorPanel,
    open: function (code, language, filePath) {
      window.dispatchEvent(
        new CustomEvent("cyrene:open-editor", {
          detail: { code: code, language: language, filePath: filePath },
          bubbles: true,
        })
      );
    },
    openFile: function (filePath) {
      fetch("/api/code/file?path=" + encodeURIComponent(filePath))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) {
            console.error("Failed to open file:", data.error);
            return;
          }
          window.CyreneCodeEditor.open(data.content, data.language, filePath);
        })
        .catch(function (e) { console.error("Failed to open file:", e); });
    },
    newFile: function () {
      window.CyreneCodeEditor.open("", "python", "");
    },
  };

  window.CodeEditorPanel = CodeEditorPanel;
})();
