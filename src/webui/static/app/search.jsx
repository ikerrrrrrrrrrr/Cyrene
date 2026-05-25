// Cyrene — search overlay for full-text conversation search
const { useState: useStateSr, useEffect: useEffectSr, useRef: useRefSr, useCallback: useCallbackSr, useMemo: useMemoSr } = React;

function SearchOverlay({ onClose, onOpenSession }) {
  const { t, lang } = useI18n();
  const inputRef = useRefSr(null);
  const [query, setQuery] = useStateSr("");
  const [results, setResults] = useStateSr([]);
  const [status, setStatus] = useStateSr("idle"); // idle | loading | done | error
  const debounceRef = useRefSr(null);

  // Auto-focus input on mount
  useEffectSr(function () {
    inputRef.current?.focus();
  }, []);

  // Keyboard: Escape to close, Arrow keys for navigation
  useEffectSr(function () {
    function onKeyDown(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose && onClose();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return function () { window.removeEventListener("keydown", onKeyDown); };
  }, [onClose]);

  // Debounced search
  useEffectSr(function () {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    var q = query.trim();
    if (!q) {
      setResults([]);
      setStatus("idle");
      return;
    }
    setStatus("loading");
    debounceRef.current = setTimeout(function () {
      doSearch(q);
    }, 250);
    return function () {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  async function doSearch(q) {
    try {
      var r = await fetch("/api/search/conversations?q=" + encodeURIComponent(q) + "&limit=50");
      if (!r.ok) throw new Error("HTTP " + r.status);
      var data = await r.json();
      if (data.ok && Array.isArray(data.results)) {
        setResults(data.results);
        setStatus("done");
      } else {
        setResults([]);
        setStatus("done");
      }
    } catch (e) {
      console.error("Search failed:", e);
      setStatus("error");
    }
  }

  function handleResultClick(result) {
    // Navigate to sessions page and select the session for this date
    if (window.selectUiSession) {
      // Try to find a session that matches this archive date
      var date = result.date;
      var session = (DATA.sessions || []).find(function (s) {
        return s.archiveDate === date || (s.id && s.id.indexOf(date) !== -1);
      });
      if (session) {
        window.selectUiSession(session.id);
      }
    }
    // Switch to sessions page
    if (window.__setAppPage) {
      window.__setAppPage("sessions");
    }
    onClose && onClose();
  }

  function highlightSnippet(text) {
    if (!query.trim() || !text) return text;
    var q = query.trim();
    // Escape regex special chars
    var escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var re = new RegExp("(" + escaped + ")", "gi");
    var parts = text.split(re);
    return parts.map(function (part, i) {
      if (part.toLowerCase() === q.toLowerCase()) {
        return React.createElement("mark", { key: i }, part);
      }
      return part;
    });
  }

  return React.createElement("div", { className: "search-overlay", onClick: function (e) { if (e.target === e.currentTarget) onClose && onClose(); } },
    React.createElement("div", { className: "search-overlay-panel", onClick: function (e) { e.stopPropagation(); } },
      // Header with search input
      React.createElement("div", { className: "search-overlay-header" },
        React.createElement("svg", { className: "search-icon", width: "16", height: "16", viewBox: "0 0 20 20", fill: "none", stroke: "currentColor", strokeWidth: "1.6" },
          React.createElement("circle", { cx: "9", cy: "9", r: "5" }),
          React.createElement("path", { d: "M13 13 L17 17" })
        ),
        React.createElement("input",
          {
            ref: inputRef,
            type: "text",
            value: query,
            onChange: function (e) { setQuery(e.target.value); },
            placeholder: t("search.placeholder"),
            "aria-label": t("search.placeholder"),
          }
        ),
        React.createElement("button", { className: "search-overlay-close", onClick: onClose, title: t("search.close") },
          "ESC"
        )
      ),

      // Results count
      status === "done" && results.length > 0 && React.createElement("div", { className: "search-results-count", key: "count" },
        t("search.resultsCount", { n: results.length })
      ),

      // Body
      React.createElement("div", { className: "search-overlay-body", key: "body" },
        // Loading state (no spin — the text pulse is enough)
        status === "loading" && React.createElement("div", { className: "search-loading-state" },
          React.createElement("span", { style: { opacity: 0.5 } }, t("search.loading"))
        ),

        // Initial empty state
        status === "idle" && React.createElement("div", { className: "search-empty-state" },
          React.createElement("div", { className: "empty-icon" },
            React.createElement("svg", { width: "40", height: "40", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "1.2" },
              React.createElement("circle", { cx: "11", cy: "11", r: "6" }),
              React.createElement("path", { d: "M16.5 16.5 L21 21" })
            )
          ),
          React.createElement("span", null, t("search.emptyState"))
        ),

        // No results
        status === "done" && results.length === 0 && React.createElement("div", { className: "search-no-results" },
          t("search.noResults")
        ),

        // Error state
        status === "error" && React.createElement("div", { className: "search-error-state" },
          t("search.error")
        ),

        // Results
        status === "done" && results.map(function (result, index) {
          var isLast = index === results.length - 1;
          var assistantLabel = t("search.assistantLabel", { name: DATA.assistantName || "Cyrene" });
          return React.createElement(React.Fragment, { key: result.date + "_" + result.timestamp + "_" + index },
            React.createElement("div", {
              className: "search-result-item",
              onClick: function () { handleResultClick(result); },
              title: result.date + " " + result.timestamp,
            },
              // Meta line: date + session title
              React.createElement("div", { className: "search-result-meta" },
                React.createElement("span", { className: "search-result-date" }, result.date),
                result.session_title && React.createElement("span", { className: "search-result-title" }, result.session_title),
              ),

              // Snippet with highlighted match
              React.createElement("div", { className: "search-result-snippet" },
                highlightSnippet(result.snippet || result.user_body + " " + result.assistant_body)
              ),

              // Tags
              React.createElement("div", { className: "search-result-excerpt" },
                React.createElement("span", { className: "search-result-tag" }, t("search.userLabel")),
                React.createElement("span", { className: "search-result-tag" }, assistantLabel),
              )
            ),
            !isLast && React.createElement("div", { className: "search-result-divider" })
          );
        })
      )
    )
  );
}

window.SearchOverlay = SearchOverlay;
