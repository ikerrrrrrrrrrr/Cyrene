// Evolution page — skills, CC learning, behavior patterns
function EvolutionPage() {
  useDataVersion();
  const { t } = useI18n();
  const [tab, setTab] = useStateSet("skills");
  const [scripts, setScripts] = useStateSet([]);
  const [ccData, setCcData] = useStateSet(null);
  const [installedSkills, setInstalledSkills] = useStateSet([]);
  const [loading, setLoading] = useStateSet(true);
  const [query, setQuery] = useStateSet("");
  const [selectedSkillId, setSelectedSkillId] = useStateSet("");
  const [skillError, setSkillError] = useStateSet("");
  const [skillBusy, setSkillBusy] = useStateSet(false);
  const [learnBusy, setLearnBusy] = useStateSet(false);
  const [learnMessage, setLearnMessage] = useStateSet("");

  const fetchData = async () => {
    setLoading(true);
    try {
      const [evRes, skillsRes] = await Promise.all([
        fetch("/api/evolution").then(r => r.json()),
        fetch("/api/skills/installed").then(r => r.json()),
      ]);
      setScripts(evRes.scripts || []);
      setCcData(evRes.cc_learning || null);
      const skills = skillsRes.skills || [];
      setInstalledSkills(skills);
      setSelectedSkillId((current) => (
        current && skills.some((skill) => skill.id === current) ? current : (skills[0]?.id || "")
      ));
    } catch (e) { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { fetchData(); }, []);

  const handleApprove = async (id) => {
    await fetch(`/api/scripts/${id}/approve`, { method: "POST" });
    fetchData();
  };
  const handleReject = async (id) => {
    await fetch(`/api/scripts/${id}/reject`, { method: "POST" });
    fetchData();
  };
  const handleRun = async (id) => {
    await fetch(`/api/scripts/${id}/run`, { method: "POST" });
  };
  const handleLearnPatterns = async () => {
    setLearnBusy(true);
    setLearnMessage("");
    try {
      const res = await fetch("/api/patterns/learn", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setLearnMessage(data.error || t("evolution.learnFailed"));
        setLearnBusy(false);
        return;
      }
      setScripts(data.scripts || []);
      const stats = data.stats || {};
      setLearnMessage(t("evolution.learnSummary", {
        observed: stats.observed || 0,
        promoted: stats.promoted || 0,
        candidates: stats.candidates || 0,
      }));
    } catch (e) {
      setLearnMessage(t("evolution.learnFailed"));
    } finally {
      setLearnBusy(false);
    }
  };
  const handleInstall = async () => {
    setSkillBusy(true);
    setSkillError("");
    const res = await fetch("/api/skills/install-picker", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (!data.cancelled) setSkillError(data.error || t("skills.installFailed"));
      setSkillBusy(false);
      return;
    }
    await fetchData();
    window.reloadUiData && window.reloadUiData();
    setSkillBusy(false);
  };
  const handleUninstall = async (id) => {
    setSkillBusy(true);
    setSkillError("");
    const res = await fetch(`/api/skills/${id}/uninstall`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setSkillError(data.error || t("skills.deleteFailed"));
      setSkillBusy(false);
      return;
    }
    await fetchData();
    window.reloadUiData && window.reloadUiData();
    setSkillBusy(false);
  };
  const handleToggle = async (id) => {
    setSkillBusy(true);
    setSkillError("");
    const res = await fetch(`/api/skills/${id}/toggle`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setSkillError(data.error || t("skills.toggleFailed"));
      setSkillBusy(false);
      return;
    }
    await fetchData();
    window.reloadUiData && window.reloadUiData();
    setSkillBusy(false);
  };

  const fmtPct = (v) => (v * 100).toFixed(0) + "%";
  const fmtTime = (ts) => ts ? new Date(ts).toLocaleDateString() : "—";
  const fmtDateTime = (ts) => ts ? new Date(ts).toLocaleString() : "—";
  const fmtBytes = (bytes) => {
    if (!bytes) return "0 B";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  };

  const tabs = [
    { id: "skills", label: t("evolution.skills") },
    { id: "cc", label: t("evolution.ccLearning") },
    { id: "patterns", label: t("evolution.patterns") },
  ];

  return (
    <div className="evolution-page">
      {/* phase banner */}
      <div className="evolution-banner">
        <span className="evolution-banner-icon">⚡</span>
        <div>
          <div className="evolution-banner-title">
            evolve / 进化
          </div>
          <div className="evolution-banner-desc">
            {t("status.evolveHint")}
          </div>
        </div>
      </div>

      {/* tab bar */}
      <div className="seg evolution-tabbar">
        {tabs.map(t => (
          <button key={t.id}
            className={"seg-btn " + (tab === t.id ? "active" : "")}
            onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      <div className="evolution-scroll">
        {/* skills tab */}
        {tab === "skills" && (
          (() => {
            const filteredSkills = installedSkills.filter((skill) => {
              if (!query) return true;
              const haystack = [skill.name, skill.desc, skill.file_name, skill.source_path].join(" ").toLowerCase();
              return haystack.includes(query.toLowerCase());
            });
            const selectedSkill = filteredSkills.find((skill) => skill.id === selectedSkillId)
              || installedSkills.find((skill) => skill.id === selectedSkillId)
              || filteredSkills[0]
              || null;

            return (
              <div className="skills-layout">
                <div className="skills-side">
                  <div className="skills-tabs">
                    <div className="skills-tab active">
                      {t("skills.installed")}
                      <span className="skills-tab-count">{installedSkills.length}</span>
                    </div>
                  </div>
                  <div className="skills-search">
                    <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                      <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
                    </svg>
                    <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("skills.filterPlaceholder")} />
                  </div>
                  {skillError && (
                    <div style={{ margin: "12px 10px 0", color: "var(--err)", fontSize: 12 }}>
                      {skillError}
                    </div>
                  )}
                  <div className="skills-list-page">
                    {loading && (
                      <div style={{ padding: "32px 16px", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
                        {t("skills.loading")}
                      </div>
                    )}
                    {!loading && filteredSkills.map((skill) => (
                      <div key={skill.id}
                           className={"skill-row " + (skill.id === selectedSkillId ? "active" : " installed")}
                           onClick={() => setSelectedSkillId(skill.id)}>
                        <div className="skill-row-icon">{skill.name?.slice(0, 1) || "S"}</div>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div className="skill-row-name">{skill.name}</div>
                          <div className="skill-row-desc">{skill.desc}</div>
                          <div className="skill-row-meta">
                            <span>{skill.file_name}</span>
                            <span>· {fmtBytes(skill.size_bytes || 0)}</span>
                          </div>
                        </div>
                        <div className="skill-row-state" style={{ paddingTop: 0 }}>
                          <div className={"toggle " + (skill.enabled !== false ? "on" : "")}
                               onClick={(e) => { e.stopPropagation(); handleToggle(skill.id); }} />
                        </div>
                      </div>
                    ))}
                    {!loading && filteredSkills.length === 0 && (
                      <div style={{ padding: "32px 16px", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
                        {installedSkills.length === 0 ? t("skills.empty") : t("skills.noMatch")}
                      </div>
                    )}
                  </div>
                </div>

                <div className="skill-detail">
                  {!selectedSkill ? (
                    <div className="skill-detail-head">
                      <div style={{ flex: 1 }}>
                        <h1 className="skill-detail-title">{t("skills.emptyTitle")}</h1>
                        <p className="skill-detail-desc">{t("skills.emptyDesc")}</p>
                      </div>
                      <button className="btn primary" onClick={handleInstall} disabled={skillBusy}>
                        {t("skills.installSkill")}
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="skill-detail-head">
                        <div className="skill-detail-icon">{selectedSkill.name?.slice(0, 1) || "S"}</div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="skill-detail-meta">
                            {(selectedSkill.tags || []).map((tag) => <span key={tag} className="skill-tag">{tag}</span>)}
                            <span className="skill-detail-version">{selectedSkill.file_name}</span>
                          </div>
                          <h1 className="skill-detail-title">{selectedSkill.name}</h1>
                          <p className="skill-detail-desc">{selectedSkill.desc}</p>
                        </div>
                        <div className="skill-detail-actions">
                          <div className="enable-toggle">
                            <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>
                              {selectedSkill.enabled ? t("skills.enabled") : t("skills.disabled")}
                            </span>
                            <div className={"toggle " + (selectedSkill.enabled ? "on" : "")}
                                 onClick={() => handleToggle(selectedSkill.id)} />
                          </div>
                          <button className="btn" onClick={() => handleUninstall(selectedSkill.id)} disabled={skillBusy}>
                            {t("skills.delete")}
                          </button>
                        </div>
                      </div>

                      <div className="skill-detail-body">
                        <div className="skill-stats">
                          <Stat label={t("skills.fileSize")} value={fmtBytes(selectedSkill.size_bytes || 0)} />
                          <Stat label={t("skills.updatedAt")} value={fmtDateTime(selectedSkill.updated_at)} />
                          <Stat label={t("skills.installedAt")} value={fmtDateTime(selectedSkill.installed_at)} />
                          <Stat label={t("skills.agentVisible")} value={selectedSkill.agent_visible ? t("skills.yes") : t("skills.no")} />
                        </div>

                        <SkillSection title={t("skills.source")}>
                          <pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.source_path || "—"}</pre>
                        </SkillSection>

                        <SkillSection title={t("skills.path")}>
                          <pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.stored_path || "—"}</pre>
                        </SkillSection>

                        <SkillSection title={t("skills.preview")}>
                          <pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.preview || "—"}</pre>
                        </SkillSection>
                      </div>
                    </>
                  )}
                </div>
              </div>
            );
          })()
        )}

        {/* CC learning tab */}
        {tab === "cc" && (
          <div className="evolution-stack">
            {ccData && ccData.available ? (
              <>
                <div className="evolution-stat-grid">
                  <Stat label={t("evolution.exchanges")} value={String(ccData.style?.message_count || 0)} />
                  <Stat label={t("evolution.avgLength")} value={String((ccData.style?.avg_length || 0).toFixed(0))} />
                  <Stat label={t("evolution.directiveCount")} value={String(ccData.style?.directive_count || 0)} />
                  <Stat label={t("evolution.correctionRate")} value={`${((ccData.corrections?.correction_ratio || 0) * 100).toFixed(1)}%`} />
                </div>

                <div className="evolution-cc-grid">
                  <div className="card evolution-card">
                    <div className="card-head">
                      <span className="card-title">{t("evolution.communication")}</span>
                    </div>
                    <div className="evolution-bullet-list">
                      {ccData.summary && ccData.summary.highlights && ccData.summary.highlights.map((h, i) => (
                        <div key={i} className="evolution-bullet-row">{h}</div>
                      ))}
                    </div>
                    <div className="evolution-card-foot">
                      {(ccData.style?.chinese_ratio || 0) > 0.5 ? "中文为主" : "English / mixed"}
                      {ccData.cadence?.avg_gap_seconds ? ` · ${t("evolution.cadence")} ${ccData.cadence.avg_gap_seconds}s` : ""}
                    </div>
                  </div>

                  {ccData.tools && ccData.tools.top_tools && ccData.tools.top_tools.length > 0 && (
                    <div className="card evolution-card">
                      <div className="card-head">
                        <span className="card-title">{t("evolution.topTools")}</span>
                      </div>
                      <div className="evolution-pair-list">
                        {ccData.tools.top_tools.map(([name, count], i) => (
                          <div key={i} className="evolution-pair-row">
                            <span>{name}</span>
                            <span>{count}x</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {ccData.style && ccData.style.common_tasks && ccData.style.common_tasks.length > 0 && (
                    <div className="card evolution-card">
                      <div className="card-head">
                        <span className="card-title">{t("evolution.commonTasks")}</span>
                      </div>
                      <div className="evolution-pair-list">
                        {ccData.style.common_tasks.map(([task, count], i) => (
                          <div key={i} className="evolution-pair-row">
                            <span>{task}</span>
                            <span>{count}x</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {ccData.corrections && (
                    <div className="card evolution-card">
                      <div className="card-head">
                        <span className="card-title">{t("evolution.correctionRate")}</span>
                      </div>
                      <div className="evolution-correction-metric">
                        {((ccData.corrections.correction_ratio || 0) * 100).toFixed(1)}%
                      </div>
                      <div className="evolution-card-foot">
                        {ccData.corrections.correction_count || 0} corrections
                      </div>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="card evolution-empty-card">
                {t("evolution.noCcData")}
              </div>
            )}
          </div>
        )}

        {/* patterns / scripts tab */}
        {tab === "patterns" && (
          <div className="evolution-patterns">
            <div className="card evolution-pattern-banner">
              <div>
                <div className="card-title">{t("evolution.patterns")}</div>
                <div className="evolution-pattern-hint">{t("evolution.patternIntro")}</div>
              </div>
              <button className="btn primary" style={{ fontSize: 11 }} onClick={handleLearnPatterns} disabled={learnBusy}>
                {learnBusy ? t("evolution.learning") : t("evolution.learnNow")}
              </button>
            </div>
            <div className={"evolution-inline-note " + (learnMessage ? "show" : "")}>
              {learnMessage || " "}
            </div>
            {scripts.length === 0 && (
              <div className="card evolution-empty-card">
                {t("evolution.noScripts")}
              </div>
            )}
            {scripts.map((s) => (
              <div key={s.id} className="card evolution-pattern-card">
                <div className="evolution-pattern-head">
                  <span className={"pattern-status " + s.status}>
                    {s.status === "approved" ? t("evolution.approved") :
                     s.status === "rejected" ? t("evolution.rejected") : t("evolution.pending")}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="evolution-pattern-title">{s.name || s.id}</div>
                    {s.description && (
                      <div className="evolution-pattern-desc">{s.description}</div>
                    )}
                  </div>
                </div>
                <div className="evolution-pattern-meta">
                  {s.confidence !== undefined && (
                    <span>{t("evolution.confidence")}: {fmtPct(s.confidence)}</span>
                  )}
                  {s.occurrences !== undefined && (
                    <span>{t("evolution.occurrences")}: {s.occurrences}</span>
                  )}
                  {Array.isArray(s.round_ids) && s.round_ids.length > 0 && (
                    <span>{t("evolution.rounds")}: {s.round_ids.length}</span>
                  )}
                  {s.last_seen && (
                    <span>{t("evolution.lastSeen")}: {fmtTime(s.last_seen)}</span>
                  )}
                </div>
                {s.steps && s.steps.length > 0 && (
                  <div className="evolution-pattern-steps">
                    {s.steps.map((step, i) => (
                      <span key={i}>{i > 0 && " → "}{step.tool}</span>
                    ))}
                  </div>
                )}
                <div className="evolution-pattern-actions">
                  {s.status === "pending" && (
                    <>
                      <button className="btn" style={{ fontSize: 11 }}
                              onClick={() => handleApprove(s.id)}>
                        {t("evolution.approve")}
                      </button>
                      <button className="btn" style={{ fontSize: 11 }}
                              onClick={() => handleReject(s.id)}>
                        {t("evolution.reject")}
                      </button>
                    </>
                  )}
                  {s.status === "approved" && (
                    <button className="btn primary" style={{ fontSize: 11 }}
                            onClick={() => handleRun(s.id)}>
                      {t("evolution.run")}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

window.EvolutionPage = EvolutionPage;

function SkillSection({ title, children }) {
  return (
    <div className="skill-section">
      <div className="skill-section-title">{title}</div>
      {children}
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div className="stat-tile">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
