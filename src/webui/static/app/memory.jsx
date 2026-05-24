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
  const { t } = useI18n();
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
          <span style={{ color: "var(--text-3)", fontSize: 15 }}>{t("memory.loading")}</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="status-grid">
        <div className="card" style={{ gridColumn: "span 12", textAlign: "center", padding: 40 }}>
          <span style={{ color: "var(--err)", fontSize: 15 }}>{t("memory.failedToLoad")}: {error}</span>
        </div>
      </div>
    );
  }

  if (!mem) return null;

  const pipelineLayers = [
    {
      label: t("memory.contextWindow"),
      desc: mem.context_window.messages + " / " + mem.context_window.max + " msgs",
      icon: "◷",
      color: "var(--accent)",
      detail: t("memory.ctxDetail", {max: mem.context_window.max}),
    },
    {
      label: t("memory.shortTerm"),
      desc: mem.short_term.total + " entries",
      icon: "▤",
      color: "#a896c4",
      detail: t("memory.stDesc"),
    },
    {
      label: t("memory.longTermSoul"),
      desc: (mem.soul.sections || []).length + " sections",
      icon: "✱",
      color: "#d4a373",
      detail: t("memory.soulDesc"),
    },
  ];

  return (
    <div className="status-grid memory-page">
      {/* Pipeline visualization */}
      <div className="card" style={{ gridColumn: "span 12" }}>
        <div className="card-head">
          <span className="card-title">{t("memory.pipeline")}</span>
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
                  fontFamily: "var(--mono)", fontSize: 15, fontWeight: 700,
                  color: "var(--text)", marginBottom: 4
                }}>{layer.label}</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 13.5, color: "var(--text-3)" }}>
                  {layer.desc}
                </div>
                <div style={{
                  marginTop: 8, fontSize: 13, color: "var(--text-4)", lineHeight: 1.6
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
          fontFamily: "var(--mono)", fontSize: 13, color: "var(--text-4)",
          display: "flex", gap: 16, flexWrap: "wrap"
        }}>
          <span>{t("memory.pipelineFlow")}</span>
          <span style={{ color: "var(--accent)" }}>
            {t("memory.archiveStatus", {days: mem.archive.days, exchanges: mem.archive.today_exchanges})}
          </span>
        </div>
      </div>

      {/* Tab bar */}
      <div className="card" style={{ gridColumn: "span 12", padding: "4px 8px" }}>
        <div style={{ display: "flex", gap: 0 }}>
          {[
            ["soul", t("memory.soulTab", {n: (mem.soul.sections || []).length})],
            ["short", t("memory.shortTab", {n: mem.short_term.total})],
            ["context", t("memory.contextTab", {n: mem.context_window.messages})],
            ["archive", t("memory.archiveTab", {n: mem.archive.days})],
          ].map(([id, label]) => (
            <button key={id} onClick={() => setActiveTab(id)} style={{
              padding: "6px 14px", border: "none", borderRadius: 5,
              background: activeTab === id ? "var(--bg-2)" : "transparent",
              color: activeTab === id ? "var(--text)" : "var(--text-3)",
              fontFamily: "var(--mono)", fontSize: 12.5, cursor: "pointer",
              fontWeight: activeTab === id ? 650 : 500,
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
                      fontFamily: "var(--mono)", fontSize: 12, color: "var(--text-4)",
                      marginRight: 6
                    }}>{isExpanded ? "▾" : "▸"}</span>
                    <span className="card-title" style={{ fontFamily: "var(--mono)", fontSize: 13.5 }}>
                      {section.name}
                    </span>
                    <span style={{
                      marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 11.5,
                      color: isTemp && mem.soul.temporary_expired > 0 ? "var(--warn)" : "var(--text-4)"
                    }}>
                      {t("memory.itemCount", {n: section.entry_count})}
                      {isTemp && mem.soul.temporary_expired > 0 ? (" · " + t("memory.expiredCount", {n: mem.soul.temporary_expired})) : ""}
                    </span>
                  </div>
                  {isExpanded && (
                    <div style={{
                      maxHeight: 360, overflowY: "auto",
                      fontFamily: "var(--mono)", fontSize: 12.5, lineHeight: 1.7
                    }}>
                      {section.entries.length === 0 ? (
                        <div style={{ color: "var(--text-4)", padding: "8px 0" }}>{t("memory.empty")}</div>
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
              <span style={{ color: "var(--warn)" }}>{t("memory.soulNotFound", {path: mem.soul.path})}</span>
              <div style={{ marginTop: 8, fontSize: 12.5, color: "var(--text-4)" }}>
                {t("memory.soulAutoCreate")}
              </div>
            </div>
          )}
        </React.Fragment>
      )}

      {/* Short-term tab */}
      {activeTab === "short" && (
        <div className="card" style={{ gridColumn: "span 12" }}>
          <div className="card-head">
            <span className="card-title">{t("memory.shortTerm")}</span>
            <span className="card-action">{t("memory.itemCount", {n: mem.short_term.total})}</span>
          </div>
          {mem.short_term.entries.length === 0 ? (
            <div style={{ textAlign: "center", padding: 30, color: "var(--text-4)", fontSize: 14.5 }}>
              {t("memory.noShortTerm")}
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>{t("memory.type")}</th>
                  <th>{t("memory.content")}</th>
                  <th>{t("memory.mentions")}</th>
                  <th>{t("memory.valence")}</th>
                  <th>{t("memory.firstSeen")}</th>
                  <th>{t("memory.lastMentioned")}</th>
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
                        fontFamily: "var(--mono)", fontSize: 11.5, fontWeight: 650,
                      }}>
                        {TYPE_LABELS[entry.type] || entry.type}
                      </span>
                    </td>
                    <td style={{ color: "var(--text)", fontSize: 13, maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {entry.content}
                    </td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 12.5, textAlign: "center" }}>{entry.mention_count ?? 1}</td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 12.5, textAlign: "center", color: (entry.emotional_valence || 0) < 0 ? "var(--err)" : (entry.emotional_valence || 0) > 0 ? "var(--accent)" : "var(--text-3)" }}>
                      {entry.emotional_valence > 0 ? "+" : ""}{entry.emotional_valence ?? 0}
                    </td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-4)" }}>{entry.first_seen || "—"}</td>
                    <td style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-4)" }}>{entry.last_mentioned || "—"}</td>
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
              <span className="card-title">{t("memory.contextWindow")}</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "8px 0" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 13 }}>{t("memory.messages")}</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.context_window.messages}
                  <span style={{ fontSize: 13, color: "var(--text-4)", fontWeight: 500 }}> / {mem.context_window.max}</span>
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
                <div style={{ fontSize: 12.5, color: "var(--warn)", fontFamily: "var(--mono)" }}>
                  {t("memory.atCapacity")}
                </div>
              )}
            </div>
          </div>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">{t("memory.persistence")}</span>
            </div>
            <div style={{
              display: "flex", flexDirection: "column", gap: 8, padding: "8px 0",
              fontFamily: "var(--mono)", fontSize: 12.5
            }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.storage")}</span>
                <span style={{ color: "var(--text)" }}>data/state.json</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.compressionTrigger")}</span>
                <span style={{ color: "var(--text)" }}>{mem.context_window.max + 5} {t("memory.messages").toLowerCase()}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.clearedOn")}</span>
                <span style={{ color: "var(--text)" }}>{t("memory.sessionReset")}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.onClear")}</span>
                <span style={{ color: "var(--text)" }}>{t("memory.compressesAll")}</span>
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
              <span className="card-title">{t("memory.conversationArchive")}</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "8px 0" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 13 }}>{t("memory.archivedDays")}</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.archive.days}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)", fontSize: 13 }}>{t("memory.todaysExchanges")}</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                  {mem.archive.today_exchanges}
                </span>
              </div>
            </div>
          </div>
          <div className="card" style={{ gridColumn: "span 6" }}>
            <div className="card-head">
              <span className="card-title">{t("memory.archiveFormat")}</span>
            </div>
            <div style={{
              display: "flex", flexDirection: "column", gap: 8, padding: "8px 0",
              fontFamily: "var(--mono)", fontSize: 12.5
            }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.location")}</span>
                <span style={{ color: "var(--text)" }}>workspace/conversations/</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.naming")}</span>
                <span style={{ color: "var(--text)" }}>YYYY-MM-DD.md</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.usedBy")}</span>
                <span style={{ color: "var(--text)" }}>{t("memory.stewardAgent")}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-3)" }}>{t("memory.searchable")}</span>
                <span style={{ color: "var(--text)" }}>{t("memory.yes")}</span>
              </div>
            </div>
          </div>
        </React.Fragment>
      )}
    </div>
  );
}

window.MemoryPage = MemoryPage;
