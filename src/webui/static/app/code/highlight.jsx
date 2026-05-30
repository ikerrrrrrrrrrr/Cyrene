// Pre-configure marked.js with highlight.js for syntax highlighting.
// Must be loaded after marked.js but before chat.jsx.

(function () {
  if (!window.marked || !window.hljs) return;

  window.marked.setOptions({
    gfm: true,
    breaks: true,
    headerIds: false,
    mangle: false,
    highlight: function (code, lang) {
      var language = lang || "";
      var result;
      if (language && hljs.getLanguage(language)) {
        try {
          result = hljs.highlight(code, { language: language, ignoreIllegals: true });
        } catch (e) {
          result = hljs.highlightAuto(code);
        }
      } else {
        result = hljs.highlightAuto(code);
        language = result.language || language || "text";
      }
      var lines = result.value.split("\n");
      var numbered = lines
        .map(function (line, i) {
          return (
            '<span class="hljs-ln-line">' +
            '<span class="hljs-ln-n" data-line="' +
            (i + 1) +
            '"></span>' +
            line +
            "</span>"
          );
        })
        .join("\n");
      return (
        '<code class="hljs language-' +
        language.replace(/[/\\:+*?.()\[\]{}|^$#@!~`'"&;<>=,\-%]/g, "") +
        '" data-language="' +
        language +
        '">' +
        numbered +
        "</code>"
      );
    },
  });

  window.CodeHighlight = {
    getLanguageName: function (lang) {
      var map = {
        py: "Python",
        python: "Python",
        js: "JavaScript",
        javascript: "JavaScript",
        ts: "TypeScript",
        typescript: "TypeScript",
        html: "HTML",
        css: "CSS",
        json: "JSON",
        md: "Markdown",
        markdown: "Markdown",
        sh: "Shell",
        bash: "Bash",
        shell: "Shell",
        sql: "SQL",
        yaml: "YAML",
        yml: "YAML",
        toml: "TOML",
        xml: "XML",
        rust: "Rust",
        go: "Go",
        java: "Java",
        cpp: "C++",
        c: "C",
        rb: "Ruby",
        ruby: "Ruby",
        php: "PHP",
        swift: "Swift",
        kotlin: "Kotlin",
        scala: "Scala",
        r: "R",
        text: "Text",
      };
      return map[lang] || lang || "Code";
    },
  };
})();
