import { EditorState, Compartment } from "@codemirror/state";
import {
  EditorView,
  Decoration,
  ViewPlugin,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
  dropCursor,
  highlightSpecialChars,
  rectangularSelection,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import {
  HighlightStyle,
  indentOnInput,
  bracketMatching,
  foldGutter,
  foldKeymap,
  syntaxHighlighting,
  defaultHighlightStyle,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { autocompletion, closeBrackets } from "@codemirror/autocomplete";

// CodeMirror 6 editor panel for the right sidebar.
// Exposes window.CyreneCodeEditor API and listens for cyrene:open-editor events.

(function () {
  if (typeof window === "undefined") return;
  if (typeof React === "undefined") return;

  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  var useCallback = React.useCallback;

  // ── CodeMirror bundle (local build-time dependency) ──

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
    return cmLang;
  }

  function isDarkMode() {
    try {
      return document && document.documentElement && document.documentElement.dataset.theme === "dark";
    } catch (e) {
      return false;
    }
  }

  var cyreneHighlightStyleLight = HighlightStyle.define([
    { tag: [tags.keyword, tags.modifier, tags.controlKeyword], color: "#8c4ec9", fontWeight: "700" },
    { tag: [tags.definitionKeyword, tags.moduleKeyword], color: "#0f766e", fontWeight: "700" },
    { tag: [tags.name, tags.deleted, tags.character, tags.propertyName, tags.macroName], color: "#1f2937" },
    { tag: [tags.function(tags.variableName), tags.labelName], color: "#0f766e" },
    { tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)], color: "#c2410c" },
    { tag: [tags.definition(tags.name), tags.separator], color: "#334155" },
    { tag: [tags.className, tags.typeName], color: "#2563eb", fontWeight: "600" },
    { tag: [tags.number, tags.changed, tags.annotation, tags.self, tags.namespace], color: "#b45309" },
    { tag: [tags.operator, tags.operatorKeyword, tags.url, tags.escape, tags.regexp, tags.link], color: "#be185d" },
    { tag: [tags.meta, tags.comment], color: "#94a3b8", fontStyle: "italic" },
    { tag: [tags.strong], fontWeight: "700" },
    { tag: [tags.emphasis], fontStyle: "italic" },
    { tag: [tags.strikethrough], textDecoration: "line-through" },
    { tag: [tags.string, tags.special(tags.string)], color: "#15803d" },
    { tag: [tags.atom, tags.bool, tags.special(tags.variableName)], color: "#7c3aed" },
    { tag: [tags.invalid], color: "#dc2626", textDecoration: "wavy underline" },
  ], { themeType: "light" });

  var cyreneHighlightStyleDark = HighlightStyle.define([
    { tag: [tags.keyword, tags.modifier, tags.controlKeyword], color: "#f0abfc", fontWeight: "700" },
    { tag: [tags.definitionKeyword, tags.moduleKeyword], color: "#7dd3fc", fontWeight: "700" },
    { tag: [tags.name, tags.deleted, tags.character, tags.macroName], color: "#edf4ff" },
    { tag: [tags.variableName, tags.propertyName], color: "#dbeafe" },
    { tag: [tags.function(tags.variableName), tags.labelName], color: "#5eead4" },
    { tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)], color: "#fdba74" },
    { tag: [tags.definition(tags.name), tags.separator], color: "#e2e8f0" },
    { tag: [tags.className, tags.typeName], color: "#93c5fd", fontWeight: "600" },
    { tag: [tags.number, tags.changed, tags.annotation, tags.self, tags.namespace], color: "#fbbf24" },
    { tag: [tags.operator, tags.operatorKeyword, tags.url, tags.escape, tags.regexp, tags.link], color: "#f472b6" },
    { tag: [tags.meta, tags.comment], color: "#b6c5d9", fontStyle: "italic" },
    { tag: [tags.strong], fontWeight: "700" },
    { tag: [tags.emphasis], fontStyle: "italic" },
    { tag: [tags.strikethrough], textDecoration: "line-through" },
    { tag: [tags.string, tags.special(tags.string)], color: "#86efac" },
    { tag: [tags.atom, tags.bool, tags.special(tags.variableName)], color: "#c4b5fd" },
    { tag: [tags.invalid], color: "#fca5a5", textDecoration: "wavy underline" },
  ], { themeType: "dark" });

  function buildEditorThemeExtension(darkMode) {
    return [
      syntaxHighlighting(darkMode ? cyreneHighlightStyleDark : cyreneHighlightStyleLight),
      EditorView.theme({
        "&": {
          height: "100%",
          backgroundColor: darkMode ? "#101720" : "var(--surface-1)",
          color: darkMode ? "#edf4ff" : "var(--text-1)",
        },
        ".cm-scroller": { overflow: "auto", fontFamily: "var(--mono)" },
        ".cm-content": {
          fontFamily: "var(--mono)",
          fontSize: "13px",
          lineHeight: "1.65",
          padding: "14px 0 24px",
          caretColor: "var(--accent)",
        },
        ".cm-line": { padding: "0 16px" },
        ".cm-focused": { outline: "none" },
        ".cm-activeLine": { backgroundColor: darkMode ? "rgba(94, 234, 212, 0.09)" : "color-mix(in srgb, var(--accent) 8%, transparent)" },
        ".cm-selectionBackground, ::selection": { backgroundColor: darkMode ? "rgba(125, 211, 252, 0.22)" : "color-mix(in srgb, var(--accent) 26%, white)" },
        ".cm-cursor, .cm-dropCursor": { borderLeftColor: darkMode ? "#7dd3fc" : "var(--accent)" },
        ".cm-gutters": {
          fontFamily: "var(--mono)",
          fontSize: "11px",
          color: darkMode ? "#9fb2c8" : "var(--text-4)",
          backgroundColor: darkMode ? "#131d29" : "color-mix(in srgb, var(--surface-2) 92%, transparent)",
          borderRight: darkMode ? "1px solid rgba(159, 178, 200, 0.18)" : "1px solid var(--line)",
        },
        ".cm-activeLineGutter": {
          backgroundColor: darkMode ? "rgba(125, 211, 252, 0.12)" : "color-mix(in srgb, var(--accent) 10%, transparent)",
          color: darkMode ? "#edf4ff" : "var(--text-2)",
        },
        ".cm-foldGutter .cm-gutterElement": { opacity: 0.7 },
        ".cm-matchingBracket": {
          backgroundColor: darkMode ? "rgba(196, 181, 253, 0.15)" : "color-mix(in srgb, var(--accent) 12%, white)",
          color: darkMode ? "#f8fafc" : "var(--text-1)",
          outline: darkMode ? "1px solid rgba(196, 181, 253, 0.35)" : "1px solid color-mix(in srgb, var(--accent) 28%, transparent)",
        },
        ".cm-codeblock-line": {
          borderLeft: darkMode ? "2px solid rgba(94, 234, 212, 0.42)" : "2px solid color-mix(in srgb, var(--accent) 36%, transparent)",
          borderRight: darkMode ? "2px solid rgba(94, 234, 212, 0.22)" : "2px solid color-mix(in srgb, var(--accent) 18%, transparent)",
          backgroundColor: darkMode ? "rgba(94, 234, 212, 0.05)" : "color-mix(in srgb, var(--accent) 4%, transparent)",
        },
        ".cm-codeblock-start": {
          borderTop: darkMode ? "2px solid rgba(94, 234, 212, 0.42)" : "2px solid color-mix(in srgb, var(--accent) 36%, transparent)",
          borderTopLeftRadius: "8px",
          borderTopRightRadius: "8px",
        },
        ".cm-codeblock-mid": {},
        ".cm-codeblock-end": {
          borderBottom: darkMode ? "2px solid rgba(94, 234, 212, 0.42)" : "2px solid color-mix(in srgb, var(--accent) 36%, transparent)",
          borderBottomLeftRadius: "8px",
          borderBottomRightRadius: "8px",
        },
      }, { dark: darkMode }),
    ];
  }

  function lineIndent(text) {
    var m = String(text || "").match(/^\s*/);
    return m ? m[0].replace(/\t/g, "    ").length : 0;
  }

  function buildBlockDecorations(state) {
    var doc = state.doc;
    if (!doc || !doc.lines) return Decoration.none;
    var main = state.selection.main;
    var currentLine = doc.lineAt(main.head);
    var currentLineNo = currentLine.number;
    var currentText = currentLine.text || "";
    var currentIndent = lineIndent(currentText);
    var headerLineNo = 0;
    var headerIndent = 0;

    for (var i = currentLineNo; i >= 1; i--) {
      var candidate = doc.line(i);
      var trimmed = (candidate.text || "").trim();
      if (!trimmed) continue;
      var indent = lineIndent(candidate.text);
      if (trimmed.endsWith(":") && indent <= currentIndent) {
        headerLineNo = i;
        headerIndent = indent;
        break;
      }
    }

    if (!headerLineNo) return Decoration.none;

    var endLineNo = headerLineNo;
    for (var j = headerLineNo + 1; j <= doc.lines; j++) {
      var nextLine = doc.line(j);
      var nextTrimmed = (nextLine.text || "").trim();
      if (!nextTrimmed) {
        endLineNo = j;
        continue;
      }
      var nextIndent = lineIndent(nextLine.text);
      if (nextIndent <= headerIndent) break;
      endLineNo = j;
    }

    if (endLineNo <= headerLineNo) return Decoration.none;

    var ranges = [];
    for (var lineNo = headerLineNo; lineNo <= endLineNo; lineNo++) {
      var line = doc.line(lineNo);
      var cls = "cm-codeblock-line";
      if (lineNo === headerLineNo) cls += " cm-codeblock-start";
      else if (lineNo === endLineNo) cls += " cm-codeblock-end";
      else cls += " cm-codeblock-mid";
      ranges.push(Decoration.line({ attributes: { class: cls } }).range(line.from));
    }
    return Decoration.set(ranges, true);
  }

  var blockOutlinePlugin = ViewPlugin.fromClass(class {
    constructor(view) {
      this.decorations = buildBlockDecorations(view.state);
    }
    update(update) {
      if (update.selectionSet || update.docChanged) {
        this.decorations = buildBlockDecorations(update.state);
      }
    }
  }, {
    decorations: function (v) { return v.decorations; },
  });

  function getWordAt(docText, pos) {
    var text = String(docText || "");
    if (!text) return "";
    var start = pos;
    var end = pos;
    while (start > 0 && /[\w$]/.test(text.charAt(start - 1))) start--;
    while (end < text.length && /[\w$]/.test(text.charAt(end))) end++;
    return text.slice(start, end);
  }

  function findDefinitionPosition(docText, symbol) {
    var name = String(symbol || "").trim();
    if (!name) return -1;
    var patterns = [
      new RegExp("(^|\\n)\\s*def\\s+" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\(", "m"),
      new RegExp("(^|\\n)\\s*class\\s+" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b", "m"),
      new RegExp("(^|\\n)\\s*(?:const|let|var|function)\\s+" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b", "m"),
    ];
    for (var i = 0; i < patterns.length; i++) {
      var m = docText.match(patterns[i]);
      if (m && typeof m.index === "number") {
        var offset = m[0].lastIndexOf(name);
        return m.index + (offset >= 0 ? offset : 0);
      }
    }
    return -1;
  }

  // ── CodeEditorPanel Component ──

  function CodeEditorPanel(props) {
    var initialCode = props.code || "";
    var filePath = props.filePath || "";
    var language = props.language || langForFile(filePath, "");
    var readOnly = props.readOnly || false;

    var viewRef = useRef(null);
    var containerRef = useRef(null);
    var [dirty, setDirty] = useState(false);
    var [saving, setSaving] = useState(false);
    var [saveMsg, setSaveMsg] = useState("");
    var [loaded, setLoaded] = useState(false);
    var [loadError, setLoadError] = useState("");
    var [fallbackCode, setFallbackCode] = useState(initialCode);
    var originalCodeRef = useRef(initialCode);
    var themeCompartmentRef = useRef(null);

    // Initialize CodeMirror
    useEffect(function () {
      var container = containerRef.current;
      if (!container) return;
      if (viewRef.current) return;

      try {
        setLoadError("");

        var langModules = {
          python: python,
          javascript: javascript,
          html: html,
          css: css,
          json: json,
          markdown: markdown,
        };
        var langName = getExtension(language);
        var langMod = langName ? langModules[langName] : null;
        var langFn = langMod ? (langMod.lang || langMod) : null;
        var darkMode = isDarkMode();
        var themeCompartment = new Compartment();
        themeCompartmentRef.current = themeCompartment;
        var extensions = [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightSpecialChars(),
          drawSelection(),
          dropCursor(),
          rectangularSelection(),
          indentOnInput(),
          bracketMatching(),
          closeBrackets(),
          autocompletion(),
          highlightActiveLine(),
          history(),
          foldGutter(),
          keymap.of([
            ...defaultKeymap,
            ...historyKeymap,
            ...foldKeymap,
            { key: "Mod-s", run: function () { handleSave(); return true; }, preventDefault: true },
          ]),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          themeCompartment.of(buildEditorThemeExtension(darkMode)),
          blockOutlinePlugin,
          EditorView.updateListener.of(function (update) {
            if (update.docChanged) {
              setDirty(true);
              if (props.onChange) {
                props.onChange(update.state.doc.toString());
              }
            }
          }),
          EditorView.domEventHandlers({
            mousedown: function (event, view) {
              if (!event.metaKey) return false;
              var pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
              if (pos == null) return false;
              var docText = view.state.doc.toString();
              var symbol = getWordAt(docText, pos);
              var defPos = findDefinitionPosition(docText, symbol);
              if (defPos < 0 || defPos === pos) return false;
              event.preventDefault();
              view.dispatch({
                selection: { anchor: defPos },
                effects: EditorView.scrollIntoView(defPos, { y: "center" }),
              });
              return true;
            },
          }),
        ];

        if (langFn) extensions.push(langFn());
        if (readOnly) extensions.push(EditorState.readOnly.of(true));

        var state = EditorState.create({
          doc: initialCode,
          extensions: extensions,
        });

        var view = new EditorView({ state: state, parent: container });
        viewRef.current = view;
        setLoaded(true);
      } catch (err) {
        console.error("CodeMirror: failed to initialize local bundle", err);
        setLoadError(err && err.message ? err.message : String(err || "Failed to load editor"));
        setLoaded(false);
      }

      return function () {
        if (viewRef.current) {
          viewRef.current.destroy();
          viewRef.current = null;
        }
      };
    }, []);

    useEffect(function () {
      originalCodeRef.current = initialCode;
      setFallbackCode(initialCode);
      setDirty(false);
      var view = viewRef.current;
      if (!view) return;
      var current = view.state.doc.toString();
      if (current === initialCode) return;
      view.dispatch({
        changes: { from: 0, to: current.length, insert: initialCode },
      });
      setDirty(false);
    }, [initialCode, filePath, language]);

    useEffect(function () {
      function applyTheme() {
        var view = viewRef.current;
        var compartment = themeCompartmentRef.current;
        if (!view || !compartment) return;
        view.dispatch({
          effects: compartment.reconfigure(buildEditorThemeExtension(isDarkMode())),
        });
      }

      applyTheme();
      window.addEventListener("cyrene:theme-change", applyTheme);
      return function () {
        window.removeEventListener("cyrene:theme-change", applyTheme);
      };
    }, []);

    // Get current code
    var getCode = useCallback(function () {
      if (viewRef.current) {
        return viewRef.current.state.doc.toString();
      }
      return fallbackCode;
    }, [fallbackCode]);

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
          setFallbackCode(code);
          originalCodeRef.current = code;
          setSaveMsg(result.error ? "Save failed" : "Saved");
          setTimeout(function () { setSaveMsg(""); }, 2000);
        })
        .catch(function (err) {
          setSaving(false);
          setSaveMsg("Error: " + (err.message || err));
        });
    }, [fallbackCode, getCode, filePath, language]);

    var handleShowDiff = useCallback(function () {
      var currentCode = getCode();
      var originalCode = originalCodeRef.current || "";
      fetch("/api/code/diff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "text",
          left: originalCode,
          right: currentCode,
        }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (window.CyreneDiffViewer) {
            window.CyreneDiffViewer.open(data.diff || "");
          }
        })
        .catch(function (err) {
          setSaveMsg("Diff error: " + (err.message || err));
        });
    }, [getCode]);

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
        loadError && React.createElement("span", { className: "code-editor-fallback-badge", title: loadError }, "fallback"),
        dirty && React.createElement("span", { className: "code-editor-dirty" }, "•"),
        React.createElement("span", { style: { flex: 1 } }),
        saveMsg && React.createElement("span", { className: "code-editor-save-msg" }, saveMsg),
        React.createElement("button", {
          className: "code-editor-ghost-btn",
          onClick: handleShowDiff,
          title: "Show red/green diff against the original buffer",
        }, "Diff"),
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
        hidden: !!loadError,
        style: { flex: 1, overflow: "hidden" },
      }),
      loadError && React.createElement("div", { className: "code-editor-fallback" },
        React.createElement("div", { className: "code-editor-fallback-note" }, "CodeMirror unavailable. Using plain text mode."),
        React.createElement(readOnly ? "pre" : "textarea", readOnly ? {
          className: "code-editor-fallback-input code-editor-fallback-pre",
        } : {
          className: "code-editor-fallback-input",
          value: fallbackCode,
          onChange: function (e) {
            setFallbackCode(e.target.value);
            setDirty(true);
            if (props.onChange) props.onChange(e.target.value);
          },
          spellCheck: false,
          readOnly: readOnly,
        }, readOnly ? fallbackCode : null)
      ),
      !loaded && !loadError && React.createElement("div", {
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
