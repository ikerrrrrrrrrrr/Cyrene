// Memory page — three-layer memory visualization
const { useState: useStateMem, useEffect: useEffectMem, useMemo: useMemoMem } = React;

const TYPE_COLORS = {
  fact: "var(--accent)",
  pattern: "#a896c4",
  preference: "#d4a373",
  emotion: "#c97878",
};

const TYPE_LABELS = {
  fact: "Fact",
  pattern: "Pattern",
  preference: "Preference",
  emotion: "Emotion",
};

function MemoryPage() {
  useDataVersion();
  const [mem, setMem] = useStateMem(null);
  const [loading, setLoading] = useStateMem(true);
  const [error, setError] = useStateMem(null);
  const [expandedSections, setExpandedSections] = useStateMem({});
  const [activeTab, setActiveTab] = useStateMem("soul");

  useEffectMem(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await fetch("/api/memory");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        if (!cancelled) { setMem(data); setLoading(false); }
      } catch (e) {
        if (!cancelled) { setError(e.message); setLoading(false); }
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  function toggleSection(name) {
    setExpandedSections((prev) => ({ ...prev, [name]: !prev[name] }));
  }

  if (loading) {
    return (
      <div className="status-grid">
        <div className="card" style={{ gridColumn: "span 12", textAlign: "center", padding: 40 }}>
          <span style={{ color: "var(--text-3)" }}>Loading memory state...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="status-grid">
        <div className="card" style={{ gridColumn: "span 12", textAlign: "center", padding: 40 }}>
          <span style={{ color: "var(--err)" }}>Failed to load: {error}</span>
        </div>
      </div>
    );
  }

  if (!mem) return null;

  const pipelineLayers = [
    {
      label: "Context Window",
      desc: mem.context_window.messages + " / " + mem.context_window.max + " msgs",
      icon: "◷",
      color: "var(--accent)",
      detail: "Active conversation messages in state.json. Compressed to short-term when exceeding " + mem.context_window.max + ".",
    },
    {
      label: "Short-Term",
      desc: mem.short_term.total + " entries",
      icon: "▤",
      color: "#a896c4",
      detail: "Compressed facts, patterns, preferences. Persists across sessions. Cleaned daily (7-day expiry).",
    },
    {
      label: "Long-Term (SOUL.md)",
      desc: (mem.soul.sections || []).length + " sections",
      icon: "✱",
      color: "#d4a373",
      detail: "Structured personality + permanent memories. Updated by Steward Agent every 30 min.",
    },
  ];

  return (
    <div className="status-grid">
      {/* Pipeline visualization */}
      <div className="card" style={{ gridColumn: "span 12" }}>
        <div className="card-head">
          <span className="card-title">Memory Pipeline</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 0, padding: "12px 0" }}>
          {pipelineLayers.map((layer, i) => (
            <React.Fragment key={layer.label}>
              <div style={{
                flex: 1, textAlign: "center", padding: "16px 12px",
                background: "var(--bg-2)", borderRadius: 8,
                border: "1px solid " + layer.color + "33",
              }}>
                <div style={{ fontSize: 20, marginBottom: 4 }}>{layer.icon}</div>
                <div style={{
                  fontFamily: "var(--mono)", fontSize: 11.5, fontWeight: 600,
                  color: "var(--text)", marginBottom: 2
                }}>{layer.label}</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-3)" }}>
                  {layer.desc}
                </div>
                <div style={{
                  marginTop: 6, fontSize: 10, color: "var(--text-4)", lineHeight: 1.4
                }}>{layer.detail}</div>
              </div>
              {i < pipelineLayers.length - 1 && (
                <div style={{
                  width: 24, height: 2,
                  background: "linear-gradient(90deg, " + pipelineLayers[i].color + "44, " + pipelineLayers[i + 1].color + "44)",
                  flexShrink: 0
                }}></div>
              )}
            </React.Fragment>
          ))}
        </div>
        {/* Pipeline flow */}
        <div style={{
          marginTop: 8, padding: "8px 12px",
          background: "var(--bg-2)", borderRadius: 6,
          fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-4)",
          display: "flex", gap: 16, flexWrap: "wrap"
        }}>
          <span>Conversation → [compress] → Short-Term → [Steward/30min] → SOUL.md</span>
          <span style={{ color: "var(--accent)" }}>
            Archive: {mem.archive.days} day(s) · Today: {mem.archive.today_exchanges} exchange(s)
          </span>
        </div>
      </div>

      {/* Tab bar */}
      <div className="card" style={{ gridColumn: "span 12", padding: "4px 8px" }}>
        <div style={{ display: "flex", gap: 0 }}>
          {[
            ["soul", "SOUL.md (" + (mem.soul.sections || []).length + " sections)"],
            ["short", "Short-Term (" + mem.short_term.total + " entries)"],
            ["context", "Context Window (" + mem.context_window.messages + " msgs)"],
            ["archive", "Archive (" + mem.archive.days + " days)"],
          ].map(([id, label]) => (
            <button key={id} onClick={() => setActiveTab(id)} style={{
              padding: "6px 14px", border: "none", borderRadius: 5,
              background: activeTab === id ? "var(--bg-2)" : "transparent",
              color: activeTab === id ? "var(--text)" : "var(--text-3)",
              fontFamily: "var(--mono)", fontSize: 11, cursor: "pointer",
              fontWeight: activeTab === id ? 600 : 400,
            }}>{label}</button>
          ))}
        </div>
      </div>

      {/* SOUL.md tab */}
      {activeTab === "soul" && (
        <React.Fragment>
          {mem.soul.exists ? (
            (mem.soul.sections || []).map((section) => {
              const isExpanded = expandedSections[section.name] !== false;
              const isTemp = section.name === "TEMPORARY";
              return (
                <div className="card" key={section.name} style={{ gridColumn: "span 6" }}>
                  <div className="card-head"
                       onClick={() => toggleSection(section.name)}
                       style={{ cursor: "pointer" }}>
                    <span style={{
                      fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)",
                      marginRight: 6
                    }}>{isExpanded ? "▾" : "▸"}</span>
                    <span className="card-title" style={{ fontFamily: "var(--mono)", fontSize: 12 }}>
                      {section.name}
                    </span>
                    <span style={{
                      marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 10.5,
                      color: isTemp && mem.soul.temporary_expired > 0 ? "var(--warn)" : "var(--text-4)"
                    }}>
                      {section.entry_count} item(s)
                      {isTemp && mem.soul.temporary_expired > 0 ? (" · " + mem.soul.temporary_expired + " expired") : ""}
                    </span>
                  </div>
                  {isExpanded && (
                    <div style={{
                      maxHeight: 360, overflowY: "auto",
                      fontFamily: "var(--mono)", fontSize: 11, lineHeight: 1.6
                    }}>
                      {section.entries.length === 0 ? (
                        <div style={{ color: "var(--text-4)", padding: "8px 0" }}>— empty —</div>
                      ) : (
                        section.entries.map((entry, j) => {
                          const dateMatch = entry.match(/(\d{4}-\d{2}-\d{2})/);
                          const isExpired = isTemp && dateMatch && (() => {
                            const d = new Date(dateMatch[1]);
                            const now = new Date();
                            return (now - d) / 86400000 >= 1;
                          })();
                          return (
                            <div key={j} style={{
                              padding: "3px 0", borderBottom: "1px solid var(--line)",
                              color: isExpired ? "var(--text-4)" : "var(--text-2)",
                              textDecoration: isExpired ? "line-through" : "none",
                            }}>
                              {entry}
                            </div>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
              );
            })
          ) : (
            <div className="card" style={{ gridColumn: "span 12", textAlign: "center", padding: 40 }}>
              <span style={{ color: "var(--warn)" }}>SOUL.md not found at {mem.soul.path}</span>
              <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-4)" }}>
                It will be created automatically on first run.
              </div>
            </div>
          )}
        </React.Fragment>
      )}

      {/* Short-term tab */}
      {activeTab === "short" && (
        <div className="card" style={{ gridColumn: "span 12" }}>
          <div className="card-head">
            <span className="card-title">Short-Term Memory Entries</span>
            <span className="card-action">{mem.short_term.total} total</span>
          </div>
          {mem.short_term.entries.length === 0 ? (
            <div style={{ textAlign: "center", padding: 30, color: "var(--text-4)" }}>
              No short-term entries yet. They accumulate as conversations are compressed.
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Content</th>
                  <th>Mentions</th>
                  <th>Valence</th>
                  <th>First Seen</th>
                  <th>Last Mentioned</th>
                </tr>
              </thead>
              <tbody>
                {mem.short_term.entries.map((entry, i) => (
                  <tr key={i}>
                    <td>
                      <span style={{
                        display: "inline-block", padding: "1px 6px", borderRadius: 3,
                        background: (TYPE_COLORS[entry.type] || "var(--text-4)") + "22",
                        color: TYPE_COLORS[entry.type] || "var(--text-3)",
                        fontFamily: "var(--mono)", fontSize: 10, fontWeight: 600,
                      }}>
                        {TYPE_LABELS[entry.type] || entry.type}
                      </span>
                    </td>
                    <td style={{ color: "var(--text)", maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {entry.content}
                    </td>
                    <td style={{ fontFamily: "var(--mono)", textAlign: "center" }}>{entry.mention_count ?? 1}</td>
                    <td style={{ fontFamily: "var(--mono)", textAlign: "center", color: (entry.emotional_valence || 0) < 0 ? "var(--err)" : (entry.emotional_valence || 0) > 0 ? "var(--accent)" : "var(--text-3)" }}>
                      {entry.emotional_valence > 0 ? "+" : ""}{entry.emotional_valence ?? 0}
                    </td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>{entry.first_seen || "—"}</td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-4)" }}>{entry.last_mentioned || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Context Window tab */}
      {activeTab === "context" && (
        <React.Fragment>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">Context Window</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "8px 0" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 11 }}>Messages</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.context_window.messages}
                  <span style={{ fontSize: 12, color: "var(--text-4)", fontWeight: 400 }}> / {mem.context_window.max}</span>
                </span>
              </div>
              <div style={{
                height: 6, borderRadius: 3, background: "var(--bg-2)",
                overflow: "hidden"
              }}>
                <div style={{
                  height: "100%", borderRadius: 3,
                  width: Math.min(100, (mem.context_window.messages / mem.context_window.max) * 100) + "%",
                  background: mem.context_window.messages >= mem.context_window.max ? "var(--warn)" : "var(--accent)",
                  transition: "width 0.4s ease",
                }}></div>
              </div>
              {mem.context_window.messages >= mem.context_window.max && (
                <div style={{ fontSize: 10.5, color: "var(--warn)", fontFamily: "var(--mono)" }}>
                  At capacity — oldest messages will be compressed on next save.
                </div>
              )}
            </div>
          </div>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">Persistence</span>
            </div>
            <div style={{
              display: "flex", flexDirection: "column", gap: 8, padding: "8px 0",
              fontFamily: "var(--mono)", fontSize: 11
            }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Storage</span>
                <span style={{ color: "var(--text)" }}>data/state.json</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Compression trigger</span>
                <span style={{ color: "var(--text)" }}>{mem.context_window.max + 5} messages</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Cleared on</span>
                <span style={{ color: "var(--text)" }}>Session reset (New Session)</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>On clear</span>
                <span style={{ color: "var(--text)" }}>Compresses all → short-term</span>
              </div>
            </div>
          </div>
        </React.Fragment>
      )}

      {/* Archive tab */}
      {activeTab === "archive" && (
        <React.Fragment>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">Conversation Archive</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "8px 0" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 11 }}>Archived days</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.archive.days}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 11 }}>Today's exchanges</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.archive.today_exchanges}
                </span>
              </div>
            </div>
          </div>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">Archive Format</span>
            </div>
            <div style={{
              display: "flex", flexDirection: "column", gap: 8, padding: "8px 0",
              fontFamily: "var(--mono)", fontSize: 11
            }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Location</span>
                <span style={{ color: "var(--text)" }}>workspace/conversations/</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Naming</span>
                <span style={{ color: "var(--text)" }}>YYYY-MM-DD.md</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Used by</span>
                <span style={{ color: "var(--text)" }}>Steward Agent (memory updates)</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>Searchable</span>
                <span style={{ color: "var(--text)" }}>Yes (plain-text search)</span>
              </div>
            </div>
          </div>
        </React.Fragment>
      )}
    </div>
  );
}

window.MemoryPage = MemoryPage;
