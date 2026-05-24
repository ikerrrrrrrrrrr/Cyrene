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
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 16, padding: "0 4px" }}>
      {/* phase banner */}
      <div style={{
        padding: "16px 20px", borderRadius: "var(--r-m)", background: "var(--bg-2)",
        border: "1px solid var(--line)", display: "flex", alignItems: "center", gap: 12
      }}>
        <span style={{ fontSize: 20 }}>⚡</span>
        <div>
          <div style={{ fontWeight: 600, fontSize: 15, color: "var(--accent)" }}>
            evolve / 进化
          </div>
          <div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 2 }}>
            {t("status.evolveHint")}
          </div>
        </div>
      </div>

      {/* tab bar */}
      <div className="seg" style={{ alignSelf: "flex-start" }}>
        {tabs.map(t => (
          <button key={t.id}
            className={"seg-btn " + (tab === t.id ? "active" : "")}
            onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

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
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {ccData && ccData.available ? (
            <>
              <div className="card" style={{ padding: "12px 16px" }}>
                <div className="card-head">
                  <span className="card-title">{t("evolution.communication")}</span>
                </div>
                <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6, fontSize: 12 }}>
                  {ccData.summary && ccData.summary.highlights && ccData.summary.highlights.map((h, i) => (
                    <div key={i} style={{ color: "var(--text-3)", lineHeight: 1.5 }}>• {h}</div>
                  ))}
                  {ccData.style && (
                    <div style={{ color: "var(--text-4)", marginTop: 4 }}>
                      {ccData.style.message_count || 0} exchanges · {ccData.style.chinese_ratio > 0.5 ? "中文" : "English"}
                      · avg {(ccData.style.avg_length || 0).toFixed(0)} chars
                    </div>
                  )}
                </div>
              </div>

              {ccData.tools && ccData.tools.top_tools && ccData.tools.top_tools.length > 0 && (
                <div className="card" style={{ padding: "12px 16px" }}>
                  <div className="card-head">
                    <span className="card-title">{t("evolution.topTools")}</span>
                  </div>
                  <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
                    {ccData.tools.top_tools.map(([name, count], i) => (
                      <div key={i} style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ color: "var(--text)" }}>{name}</span>
                        <span style={{ color: "var(--text-4)" }}>{count}x</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {ccData.style && ccData.style.common_tasks && ccData.style.common_tasks.length > 0 && (
                <div className="card" style={{ padding: "12px 16px" }}>
                  <div className="card-head">
                    <span className="card-title">{t("evolution.commonTasks")}</span>
                  </div>
                  <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
                    {ccData.style.common_tasks.map(([task, count], i) => (
                      <div key={i} style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ color: "var(--text)" }}>{task}</span>
                        <span style={{ color: "var(--text-4)" }}>{count}x</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {ccData.corrections && (
                <div className="card" style={{ padding: "12px 16px" }}>
                  <div className="card-head">
                    <span className="card-title">{t("evolution.correctionRate")}</span>
                  </div>
                  <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-3)" }}>
                    {(ccData.corrections.correction_ratio * 100).toFixed(1)}%
                    <span style={{ color: "var(--text-4)", marginLeft: 8 }}>
                      ({ccData.corrections.correction_count} corrections)
                    </span>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div style={{ textAlign: "center", padding: 40, color: "var(--text-4)", fontSize: 12 }}>
              {t("evolution.noCcData")}
            </div>
          )}
        </div>
      )}

      {/* patterns / scripts tab */}
      {tab === "patterns" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {scripts.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, color: "var(--text-4)", fontSize: 12 }}>
              {t("evolution.noScripts")}
            </div>
          )}
          {scripts.map((s) => (
            <div key={s.id} className="card" style={{ padding: "12px 16px" }}>
              <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                <span style={{
                  fontSize: 10, fontWeight: 600, padding: "2px 6px", borderRadius: 4,
                  background: s.status === "approved" ? "var(--accent)" :
                              s.status === "rejected" ? "var(--err)" : "var(--warn)",
                  color: "#fff", marginTop: 2
                }}>
                  {s.status === "approved" ? t("evolution.approved") :
                   s.status === "rejected" ? t("evolution.rejected") : t("evolution.pending")}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{s.name || s.id}</div>
                  {s.description && (
                    <div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 2 }}>{s.description}</div>
                  )}
                  <div style={{ display: "flex", gap: 16, marginTop: 6, fontSize: 11, color: "var(--text-4)" }}>
                    {s.confidence !== undefined && (
                      <span>{t("evolution.confidence")}: {fmtPct(s.confidence)}</span>
                    )}
                    {s.occurrences !== undefined && (
                      <span>{t("evolution.occurrences")}: {s.occurrences}</span>
                    )}
                    {s.last_seen && (
                      <span>{t("evolution.lastSeen")}: {fmtTime(s.last_seen)}</span>
                    )}
                  </div>
                  {s.steps && s.steps.length > 0 && (
                    <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-3)", fontFamily: "var(--mono)" }}>
                      {s.steps.map((step, i) => (
                        <span key={i}>{i > 0 && " → "}{step.tool}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "flex-end" }}>
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
