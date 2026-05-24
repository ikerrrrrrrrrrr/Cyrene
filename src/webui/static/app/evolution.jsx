// Evolution page — installed skills, CC learning, and learned-skill workbench
function EvolutionPage() {
  useDataVersion();
  const { t } = useI18n();
  const [tab, setTab] = useStateSet("skills");
  const [workbenchTab, setWorkbenchTab] = useStateSet("learned");
  const [scripts, setScripts] = useStateSet([]);
  const [patterns, setPatterns] = useStateSet([]);
  const [learnedSkills, setLearnedSkills] = useStateSet([]);
  const [ccData, setCcData] = useStateSet(null);
  const [installedSkills, setInstalledSkills] = useStateSet([]);
  const [vocabulary, setVocabulary] = useStateSet({ labels: [], aliases: [], unknown_labels: [], vocabulary_version: 1 });
  const [loading, setLoading] = useStateSet(true);
  const [query, setQuery] = useStateSet("");
  const [selectedSkillId, setSelectedSkillId] = useStateSet("");
  const [skillError, setSkillError] = useStateSet("");
  const [skillBusy, setSkillBusy] = useStateSet(false);
  const [learnBusy, setLearnBusy] = useStateSet(false);
  const [learnMessage, setLearnMessage] = useStateSet("");
  const [selectedLearnedSkillId, setSelectedLearnedSkillId] = useStateSet("");
  const [selectedPatternId, setSelectedPatternId] = useStateSet("");
  const [workbenchBusy, setWorkbenchBusy] = useStateSet(false);
  const [workbenchMessage, setWorkbenchMessage] = useStateSet("");
  const [learnedSkillDetail, setLearnedSkillDetail] = useStateSet(null);
  const [learnedSkillVersions, setLearnedSkillVersions] = useStateSet([]);
  const [learnedSkillPatches, setLearnedSkillPatches] = useStateSet([]);
  const [learnedSkillRuns, setLearnedSkillRuns] = useStateSet([]);
  const [learnedSkillReplayTests, setLearnedSkillReplayTests] = useStateSet([]);
  const [skillForm, setSkillForm] = useStateSet(emptySkillForm());
  const [labelForm, setLabelForm] = useStateSet({
    label_type: "intent_type",
    canonical_label: "",
    domain: "",
    parent_label: "",
    raw_description: "",
  });
  const [aliasForm, setAliasForm] = useStateSet({
    label_type: "intent_type",
    canonical_label: "",
    alias_label: "",
  });
  const [unknownDrafts, setUnknownDrafts] = useStateSet({});

  const fetchOverview = async () => {
    setLoading(true);
    try {
      const [evRes, skillsRes, vocabRes] = await Promise.all([
        fetch("/api/evolution").then((r) => r.json()),
        fetch("/api/skills/installed").then((r) => r.json()),
        fetch("/api/vocabulary").then((r) => r.json()),
      ]);
      setScripts(evRes.scripts || []);
      setPatterns(evRes.patterns || []);
      setLearnedSkills(evRes.learned_skills || []);
      setCcData(evRes.cc_learning || null);
      setVocabulary(vocabRes || { labels: [], aliases: [], unknown_labels: [], vocabulary_version: 1 });
      const skills = skillsRes.skills || [];
      setInstalledSkills(skills);
      setSelectedSkillId((current) => (
        current && skills.some((skill) => skill.id === current) ? current : (skills[0]?.id || "")
      ));
      setSelectedLearnedSkillId((current) => (
        current && (evRes.learned_skills || []).some((skill) => skill.id === current)
          ? current
          : (evRes.learned_skills || [])[0]?.id || ""
      ));
      setSelectedPatternId((current) => (
        current && (evRes.patterns || []).some((pattern) => pattern.id === current)
          ? current
          : (evRes.patterns || [])[0]?.id || ""
      ));
    } catch (e) {
      setWorkbenchMessage(t("evolution.loadFailed"));
    }
    setLoading(false);
  };

  useEffect(() => { fetchOverview(); }, []);

  useEffect(() => {
    if (!selectedLearnedSkillId) {
      setLearnedSkillDetail(null);
      setLearnedSkillVersions([]);
      setLearnedSkillPatches([]);
      setLearnedSkillRuns([]);
      setLearnedSkillReplayTests([]);
      setSkillForm(emptySkillForm());
      return;
    }
    loadLearnedSkillWorkbench(selectedLearnedSkillId);
  }, [selectedLearnedSkillId]);

  const loadLearnedSkillWorkbench = async (skillId) => {
    try {
      const [detailRes, versionsRes, patchesRes, runsRes, testsRes] = await Promise.all([
        fetch(`/api/learned-skills/${skillId}`).then((r) => r.json()),
        fetch(`/api/learned-skills/${skillId}/versions`).then((r) => r.json()),
        fetch(`/api/learned-skills/${skillId}/patches`).then((r) => r.json()),
        fetch(`/api/learned-skills/${skillId}/runs?limit=20`).then((r) => r.json()),
        fetch(`/api/learned-skills/${skillId}/replay-tests`).then((r) => r.json()),
      ]);
      const detail = detailRes.skill || null;
      setLearnedSkillDetail(detail);
      setLearnedSkillVersions(versionsRes.versions || []);
      setLearnedSkillPatches(patchesRes.patches || []);
      setLearnedSkillRuns(runsRes.runs || []);
      setLearnedSkillReplayTests(testsRes.tests || []);
      setSkillForm(skillFormFromDetail(detail));
    } catch (e) {
      setWorkbenchMessage(t("evolution.loadFailed"));
    }
  };

  const refreshVocabulary = async () => {
    const data = await fetch("/api/vocabulary").then((r) => r.json());
    setVocabulary(data || { labels: [], aliases: [], unknown_labels: [], vocabulary_version: 1 });
  };

  const refreshWorkbench = async () => {
    await fetchOverview();
    if (selectedLearnedSkillId) {
      await loadLearnedSkillWorkbench(selectedLearnedSkillId);
    }
  };

  const handleApprove = async (id) => {
    await fetch(`/api/scripts/${id}/approve`, { method: "POST" });
    fetchOverview();
  };
  const handleReject = async (id) => {
    await fetch(`/api/scripts/${id}/reject`, { method: "POST" });
    fetchOverview();
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
      setPatterns(data.patterns || []);
      setLearnedSkills(data.learned_skills || []);
      await refreshVocabulary();
      const stats = data.stats || {};
      setLearnMessage(t("evolution.learnSummary", {
        observed: stats.processed_turns || 0,
        promoted: stats.skills_created || 0,
        candidates: (stats.merged_patterns || 0) + (stats.new_patterns || 0),
      }));
    } catch (e) {
      setLearnMessage(t("evolution.learnFailed"));
    } finally {
      setLearnBusy(false);
    }
  };
  const handleInstall = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".md,.txt,.zip,.json,.yaml,.yml,.prompt";
    input.onchange = handleFileSelected;
    input.click();
  };
  const handleFileSelected = async (e) => {
    const file = e.target.files?.[0];
    if (!file) {
      setSkillError(t("skills.installCancelled"));
      setTimeout(() => setSkillError(""), 2000);
      return;
    }
    setSkillBusy(true);
    setSkillError("");
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/skills/install-upload", { method: "POST", body: formData });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setSkillError(data.error || t("skills.installFailed"));
        return;
      }
      await fetchOverview();
      window.reloadUiData && window.reloadUiData();
    } catch (e) {
      setSkillError(t("skills.networkError"));
    } finally {
      setSkillBusy(false);
    }
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
    await fetchOverview();
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
    await fetchOverview();
    window.reloadUiData && window.reloadUiData();
    setSkillBusy(false);
  };
  const handleActivateLearnedSkill = async (id) => {
    await fetch(`/api/learned-skills/${id}/activate`, { method: "POST" });
    await refreshWorkbench();
  };
  const handleDeprecateLearnedSkill = async (id) => {
    await fetch(`/api/learned-skills/${id}/deprecate`, { method: "POST" });
    await refreshWorkbench();
  };
  const handleRunLearnedSkill = async (id) => {
    await fetch(`/api/learned-skills/${id}/run`, { method: "POST" });
    await refreshWorkbench();
  };

  const handleSaveLearnedSkill = async () => {
    if (!selectedLearnedSkillId) return;
    setWorkbenchBusy(true);
    setWorkbenchMessage("");
    try {
      const payload = buildSkillUpdatePayload(skillForm);
      const res = await fetch(`/api/learned-skills/${selectedLearnedSkillId}/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason: skillForm.reason || "Manual skill edit from evolution workbench.",
          updates: payload,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.saveFailed"));
        setWorkbenchBusy(false);
        return;
      }
      setWorkbenchMessage(t("evolution.saveSucceeded"));
      await refreshWorkbench();
    } catch (e) {
      setWorkbenchMessage(t("evolution.saveFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleRunReplayTests = async () => {
    if (!selectedLearnedSkillId) return;
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/learned-skills/${selectedLearnedSkillId}/replay-tests/run`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        setWorkbenchMessage(t("evolution.replaySummary", {
          passed: data.result?.passed || 0,
          total: data.result?.total || 0,
          rate: Math.round((data.result?.pass_rate || 0) * 100),
        }));
        await loadLearnedSkillWorkbench(selectedLearnedSkillId);
      } else {
        setWorkbenchMessage(data.error || t("evolution.replayFailed"));
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.replayFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleRollbackSkill = async (version) => {
    if (!selectedLearnedSkillId) return;
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/learned-skills/${selectedLearnedSkillId}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.rollbackFailed"));
      } else {
        setWorkbenchMessage(t("evolution.rollbackSucceeded", { version }));
        await refreshWorkbench();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.rollbackFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleApplyPatch = async (patchId) => {
    if (!selectedLearnedSkillId) return;
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/learned-skills/${selectedLearnedSkillId}/patches/${patchId}/apply`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.patchApplyFailed"));
      } else {
        setWorkbenchMessage(t("evolution.patchApplied"));
        await refreshWorkbench();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.patchApplyFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleRejectPatch = async (patchId) => {
    if (!selectedLearnedSkillId) return;
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/learned-skills/${selectedLearnedSkillId}/patches/${patchId}/reject`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.patchRejectFailed"));
      } else {
        setWorkbenchMessage(t("evolution.patchRejected"));
        await loadLearnedSkillWorkbench(selectedLearnedSkillId);
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.patchRejectFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleCreateLabel = async () => {
    setWorkbenchBusy(true);
    try {
      const res = await fetch("/api/vocabulary/labels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(labelForm),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.labelCreateFailed"));
      } else {
        setWorkbenchMessage(t("evolution.labelCreated"));
        setLabelForm({ label_type: labelForm.label_type, canonical_label: "", domain: "", parent_label: "", raw_description: "" });
        await refreshVocabulary();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.labelCreateFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleCreateAlias = async () => {
    setWorkbenchBusy(true);
    try {
      const res = await fetch("/api/vocabulary/aliases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aliasForm),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.aliasCreateFailed"));
      } else {
        setWorkbenchMessage(t("evolution.aliasCreated"));
        setAliasForm({ label_type: aliasForm.label_type, canonical_label: "", alias_label: "" });
        await refreshVocabulary();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.aliasCreateFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handlePromoteUnknown = async (unknownId) => {
    const draft = unknownDrafts[unknownId] || {};
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/vocabulary/unknown/${unknownId}/promote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.unknownPromoteFailed"));
      } else {
        setWorkbenchMessage(t("evolution.unknownPromoted"));
        await refreshVocabulary();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.unknownPromoteFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const handleDismissUnknown = async (unknownId) => {
    setWorkbenchBusy(true);
    try {
      const res = await fetch(`/api/vocabulary/unknown/${unknownId}/dismiss`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setWorkbenchMessage(data.error || t("evolution.unknownDismissFailed"));
      } else {
        setWorkbenchMessage(t("evolution.unknownDismissed"));
        await refreshVocabulary();
      }
    } catch (e) {
      setWorkbenchMessage(t("evolution.unknownDismissFailed"));
    } finally {
      setWorkbenchBusy(false);
    }
  };

  const fmtPct = (v) => (Number(v || 0) * 100).toFixed(0) + "%";
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
    { id: "patterns", label: t("evolution.workbench") },
  ];

  const workbenchTabs = [
    { id: "learned", label: t("evolution.learnedSkills") },
    { id: "patterns", label: t("evolution.patterns") },
    { id: "vocabulary", label: t("evolution.vocabulary") },
  ];

  const filteredSkills = installedSkills.filter((skill) => {
    if (!query) return true;
    const haystack = [skill.name, skill.desc, skill.file_name, skill.source_path].join(" ").toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  const selectedSkill = filteredSkills.find((skill) => skill.id === selectedSkillId)
    || installedSkills.find((skill) => skill.id === selectedSkillId)
    || filteredSkills[0]
    || null;
  const selectedPattern = patterns.find((pattern) => pattern.id === selectedPatternId) || patterns[0] || null;

  return (
    <div className="evolution-page">
      <div className="evolution-banner">
        <span className="evolution-banner-icon">⚡</span>
        <div>
          <div className="evolution-banner-title">evolve / 进化</div>
          <div className="evolution-banner-desc">{t("status.evolveHint")}</div>
        </div>
      </div>

      <div className="seg evolution-tabbar">
        {tabs.map((item) => (
          <button
            key={item.id}
            className={"seg-btn " + (tab === item.id ? "active" : "")}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="evolution-scroll">
        {tab === "skills" && (
          <div className="skills-layout">
            <div className="skills-side">
              <div className="skills-tabs">
                <div className="skills-tab active">
                  {t("skills.installed")}
                  <span className="skills-tab-count">{installedSkills.length}</span>
                </div>
                <button className="btn primary" style={{ fontSize: 10, padding: "2px 10px", marginLeft: "auto" }} onClick={handleInstall} disabled={skillBusy}>
                  + {t("skills.installSkill")}
                </button>
              </div>
              <div className="skills-search">
                <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                  <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
                </svg>
                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("skills.filterPlaceholder")} />
              </div>
              {skillError && <div style={{ margin: "12px 10px 0", color: "var(--err)", fontSize: 12 }}>{skillError}</div>}
              <div className="skills-list-page">
                {loading && <div style={{ padding: "32px 16px", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>{t("skills.loading")}</div>}
                {!loading && filteredSkills.map((skill) => (
                  <div key={skill.id} className={"skill-row " + (skill.id === selectedSkillId ? "active" : " installed")} onClick={() => setSelectedSkillId(skill.id)}>
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
                      <div className={"toggle " + (skill.enabled !== false ? "on" : "")} onClick={(e) => { e.stopPropagation(); handleToggle(skill.id); }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="skill-detail">
              {!selectedSkill ? (
                <div className="skill-detail-head">
                  <div style={{ flex: 1 }}>
                    <h1 className="skill-detail-title">{t("skills.emptyTitle")}</h1>
                    <p className="skill-detail-desc">{t("skills.emptyDesc")}</p>
                  </div>
                  <button className="btn primary" onClick={handleInstall} disabled={skillBusy}>{t("skills.installSkill")}</button>
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
                        <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{selectedSkill.enabled ? t("skills.enabled") : t("skills.disabled")}</span>
                        <div className={"toggle " + (selectedSkill.enabled ? "on" : "")} onClick={() => handleToggle(selectedSkill.id)} />
                      </div>
                      <button className="btn" onClick={() => handleUninstall(selectedSkill.id)} disabled={skillBusy}>{t("skills.delete")}</button>
                    </div>
                  </div>

                  <div className="skill-detail-body">
                    <div className="skill-stats">
                      <Stat label={t("skills.fileSize")} value={fmtBytes(selectedSkill.size_bytes || 0)} />
                      <Stat label={t("skills.updatedAt")} value={fmtDateTime(selectedSkill.updated_at)} />
                      <Stat label={t("skills.installedAt")} value={fmtDateTime(selectedSkill.installed_at)} />
                      <Stat label={t("skills.agentVisible")} value={selectedSkill.agent_visible ? t("skills.yes") : t("skills.no")} />
                    </div>
                    <SkillSection title={t("skills.source")}><pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.source_path || "—"}</pre></SkillSection>
                    <SkillSection title={t("skills.path")}><pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.stored_path || "—"}</pre></SkillSection>
                    <SkillSection title={t("skills.preview")}><pre className="code-block" style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{selectedSkill.preview || "—"}</pre></SkillSection>
                  </div>
                </>
              )}
            </div>
          </div>
        )}

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
                    <div className="card-head"><span className="card-title">{t("evolution.communication")}</span></div>
                    <div className="evolution-bullet-list">
                      {ccData.summary?.highlights?.map((item, index) => <div key={index} className="evolution-bullet-row">{item}</div>)}
                    </div>
                    <div className="evolution-card-foot">
                      {(ccData.style?.chinese_ratio || 0) > 0.5 ? "中文为主" : "English / mixed"}
                      {ccData.cadence?.avg_gap_seconds ? ` · ${t("evolution.cadence")} ${ccData.cadence.avg_gap_seconds}s` : ""}
                    </div>
                  </div>
                  {ccData.tools?.top_tools?.length > 0 && (
                    <div className="card evolution-card">
                      <div className="card-head"><span className="card-title">{t("evolution.topTools")}</span></div>
                      <div className="evolution-pair-list">
                        {ccData.tools.top_tools.map(([name, count], index) => <div key={index} className="evolution-pair-row"><span>{name}</span><span>{count}x</span></div>)}
                      </div>
                    </div>
                  )}
                  {ccData.style?.common_tasks?.length > 0 && (
                    <div className="card evolution-card">
                      <div className="card-head"><span className="card-title">{t("evolution.commonTasks")}</span></div>
                      <div className="evolution-pair-list">
                        {ccData.style.common_tasks.map(([task, count], index) => <div key={index} className="evolution-pair-row"><span>{task}</span><span>{count}x</span></div>)}
                      </div>
                    </div>
                  )}
                  {ccData.corrections && (
                    <div className="card evolution-card">
                      <div className="card-head"><span className="card-title">{t("evolution.correctionRate")}</span></div>
                      <div className="evolution-correction-metric">{((ccData.corrections.correction_ratio || 0) * 100).toFixed(1)}%</div>
                      <div className="evolution-card-foot">{ccData.corrections.correction_count || 0} corrections</div>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="card evolution-empty-card">{t("evolution.noCcData")}</div>
            )}
          </div>
        )}

        {tab === "patterns" && (
          <div className="evolution-workbench">
            <div className="card evolution-pattern-banner">
              <div>
                <div className="card-title">{t("evolution.workbench")}</div>
                <div className="evolution-pattern-hint">{t("evolution.workbenchIntro")}</div>
              </div>
              <button className="btn primary" style={{ fontSize: 11 }} onClick={handleLearnPatterns} disabled={learnBusy}>
                {learnBusy ? t("evolution.learning") : t("evolution.learnNow")}
              </button>
            </div>
            <div className={"evolution-inline-note " + ((learnMessage || workbenchMessage) ? "show" : "")}>{learnMessage || workbenchMessage || " "}</div>
            <div className="seg evolution-workbench-tabs">
              {workbenchTabs.map((item) => (
                <button key={item.id} className={"seg-btn " + (workbenchTab === item.id ? "active" : "")} onClick={() => setWorkbenchTab(item.id)}>
                  {item.label}
                </button>
              ))}
            </div>

            {workbenchTab === "learned" && (
              <div className="evolution-workbench-grid">
                <div className="card evolution-workbench-side">
                  <div className="evolution-side-head">
                    <div className="card-title">{t("evolution.learnedSkills")}</div>
                    <span className="skills-tab-count">{learnedSkills.length}</span>
                  </div>
                  <div className="evolution-side-list">
                    {learnedSkills.map((skill) => (
                      <button key={skill.id} className={"evolution-side-item " + (selectedLearnedSkillId === skill.id ? "active" : "")} onClick={() => setSelectedLearnedSkillId(skill.id)}>
                        <div className="evolution-side-item-top">
                          <span className={"pattern-status " + skill.status}>{skill.status}</span>
                          <span className="evolution-side-item-version">v{skill.version}</span>
                        </div>
                        <div className="evolution-side-item-title">{skill.name}</div>
                        <div className="evolution-side-item-desc">{skill.description || skill.id}</div>
                      </button>
                    ))}
                    {learnedSkills.length === 0 && <div className="evolution-empty-card">{t("evolution.noScripts")}</div>}
                  </div>
                </div>

                <div className="card evolution-workbench-main">
                  {!learnedSkillDetail ? (
                    <div className="evolution-empty-card">{t("evolution.selectSkill")}</div>
                  ) : (
                    <div className="evolution-detail-stack">
                      <div className="evolution-detail-head">
                        <div>
                          <div className="evolution-detail-kicker">{learnedSkillDetail.pattern_id}</div>
                          <div className="evolution-pattern-title">{learnedSkillDetail.name}</div>
                          <div className="evolution-pattern-desc">{learnedSkillDetail.description}</div>
                        </div>
                        <div className="evolution-pattern-actions">
                          {learnedSkillDetail.status !== "active" && learnedSkillDetail.status !== "deprecated" && (
                            <button className="btn" onClick={() => handleActivateLearnedSkill(learnedSkillDetail.skill_id)}>{t("evolution.activate")}</button>
                          )}
                          {learnedSkillDetail.status !== "deprecated" && (
                            <button className="btn" onClick={() => handleDeprecateLearnedSkill(learnedSkillDetail.skill_id)}>{t("evolution.deprecate")}</button>
                          )}
                          <button className="btn" onClick={handleRunReplayTests} disabled={workbenchBusy}>{t("evolution.runReplay")}</button>
                          <button className="btn primary" onClick={() => handleRunLearnedSkill(learnedSkillDetail.skill_id)}>{t("evolution.run")}</button>
                        </div>
                      </div>

                      <div className="evolution-stat-grid">
                        <Stat label={t("evolution.skillType")} value={learnedSkillDetail.skill_type} />
                        <Stat label={t("evolution.version")} value={`v${learnedSkillDetail.version}`} />
                        <Stat label={t("evolution.runCount")} value={String(learnedSkillDetail.run_statistics?.total_runs || 0)} />
                        <Stat label={t("evolution.shadowStatus")} value={`${learnedSkillDetail.run_statistics?.shadow_success || 0}/${learnedSkillDetail.run_statistics?.shadow_failure || 0}`} />
                      </div>

                      <div className="evolution-editor-grid">
                        <FormField label={t("evolution.name")}>
                          <input value={skillForm.name} onChange={(e) => setSkillForm((s) => ({ ...s, name: e.target.value }))} />
                        </FormField>
                        <FormField label={t("evolution.description")}>
                          <input value={skillForm.description} onChange={(e) => setSkillForm((s) => ({ ...s, description: e.target.value }))} />
                        </FormField>
                        <FormField label={t("evolution.status")}>
                          <input value={skillForm.status} onChange={(e) => setSkillForm((s) => ({ ...s, status: e.target.value }))} />
                        </FormField>
                        <FormField label={t("evolution.skillType")}>
                          <input value={skillForm.skill_type} onChange={(e) => setSkillForm((s) => ({ ...s, skill_type: e.target.value }))} />
                        </FormField>
                      </div>

                      <div className="evolution-editor-grid single">
                        <JsonField label="trigger" value={skillForm.trigger_json} onChange={(value) => setSkillForm((s) => ({ ...s, trigger_json: value }))} />
                        <JsonField label="input_schema" value={skillForm.input_schema_json} onChange={(value) => setSkillForm((s) => ({ ...s, input_schema_json: value }))} />
                        <JsonField label="parameter_extractor" value={skillForm.parameter_extractor_json} onChange={(value) => setSkillForm((s) => ({ ...s, parameter_extractor_json: value }))} />
                        <JsonField label="steps" value={skillForm.steps_json} onChange={(value) => setSkillForm((s) => ({ ...s, steps_json: value }))} />
                        <JsonField label="guards" value={skillForm.guards_json} onChange={(value) => setSkillForm((s) => ({ ...s, guards_json: value }))} />
                        <JsonField label="fallback_policy" value={skillForm.fallback_policy_json} onChange={(value) => setSkillForm((s) => ({ ...s, fallback_policy_json: value }))} />
                        <FormField label={t("evolution.editReason")}>
                          <input value={skillForm.reason} onChange={(e) => setSkillForm((s) => ({ ...s, reason: e.target.value }))} />
                        </FormField>
                      </div>

                      <div className="evolution-pattern-actions">
                        <button className="btn primary" onClick={handleSaveLearnedSkill} disabled={workbenchBusy}>{t("evolution.saveSkill")}</button>
                      </div>

                      <div className="evolution-detail-panels">
                        <MiniPanel title={t("evolution.versions")}>
                          {learnedSkillVersions.map((version) => (
                            <div key={version.version} className="evolution-mini-row">
                              <div>
                                <div className="evolution-mini-title">v{version.version} · {version.change_type}</div>
                                <div className="evolution-mini-sub">{version.change_summary || "—"}</div>
                              </div>
                              <button className="btn" onClick={() => handleRollbackSkill(version.version)}>{t("evolution.rollback")}</button>
                            </div>
                          ))}
                        </MiniPanel>

                        <MiniPanel title={t("evolution.patches")}>
                          {learnedSkillPatches.map((patch) => (
                            <div key={patch.patch_id} className="evolution-mini-row">
                              <div>
                                <div className="evolution-mini-title">{patch.patch_type} · {patch.status}</div>
                                <div className="evolution-mini-sub">{patch.reason}</div>
                              </div>
                              <div className="evolution-mini-actions">
                                {patch.status === "proposed" && <button className="btn" onClick={() => handleApplyPatch(patch.patch_id)}>{t("evolution.applyPatch")}</button>}
                                {patch.status === "proposed" && <button className="btn" onClick={() => handleRejectPatch(patch.patch_id)}>{t("evolution.rejectPatch")}</button>}
                              </div>
                            </div>
                          ))}
                        </MiniPanel>

                        <MiniPanel title={t("evolution.recentRuns")}>
                          {learnedSkillRuns.map((run) => (
                            <div key={run.run_id} className="evolution-mini-row compact">
                              <div>
                                <div className="evolution-mini-title">{run.execution_status}</div>
                                <div className="evolution-mini-sub">{fmtDateTime(run.created_at)} · score {Number(run.match_score || 0).toFixed(2)}</div>
                              </div>
                            </div>
                          ))}
                        </MiniPanel>

                        <MiniPanel title={t("evolution.replayTests")}>
                          {learnedSkillReplayTests.map((test) => {
                            const result = safeParseJson(test.last_result, {});
                            return (
                              <div key={test.test_id} className="evolution-mini-row compact">
                                <div>
                                  <div className="evolution-mini-title">{test.test_type} · {result.ok ? "pass" : "pending/fail"}</div>
                                  <div className="evolution-mini-sub">{test.turn_id}</div>
                                </div>
                              </div>
                            );
                          })}
                        </MiniPanel>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {workbenchTab === "patterns" && (
              <div className="evolution-workbench-grid">
                <div className="card evolution-workbench-side">
                  <div className="evolution-side-head">
                    <div className="card-title">{t("evolution.patterns")}</div>
                    <span className="skills-tab-count">{patterns.length}</span>
                  </div>
                  <div className="evolution-side-list">
                    {patterns.map((pattern) => (
                      <button key={pattern.id} className={"evolution-side-item " + (selectedPatternId === pattern.id ? "active" : "")} onClick={() => setSelectedPatternId(pattern.id)}>
                        <div className="evolution-side-item-top">
                          <span className={"pattern-status " + pattern.status}>{pattern.status}</span>
                          <span className="evolution-side-item-version">{pattern.frequency}x</span>
                        </div>
                        <div className="evolution-side-item-title">{pattern.description || pattern.id}</div>
                        <div className="evolution-side-item-desc">{pattern.id}</div>
                      </button>
                    ))}
                  </div>
                </div>
                <div className="card evolution-workbench-main">
                  {!selectedPattern ? (
                    <div className="evolution-empty-card">{t("evolution.noPatterns")}</div>
                  ) : (
                    <div className="evolution-detail-stack">
                      <div className="evolution-detail-head">
                        <div>
                          <div className="evolution-pattern-title">{selectedPattern.description || selectedPattern.id}</div>
                          <div className="evolution-pattern-desc">{selectedPattern.id}</div>
                        </div>
                      </div>
                      <div className="evolution-stat-grid">
                        <Stat label={t("evolution.occurrences")} value={String(selectedPattern.frequency || 0)} />
                        <Stat label={t("evolution.effectiveCount")} value={String(selectedPattern.effective_count || 0)} />
                        <Stat label={t("evolution.actionStability")} value={fmtPct(selectedPattern.action_stability || 0)} />
                        <Stat label={t("evolution.lastSeen")} value={fmtTime(selectedPattern.last_seen_at)} />
                      </div>
                      <MiniPanel title="prototype_fingerprint">
                        <pre className="code-block evolution-code-block">{prettyJson(selectedPattern.prototype_fingerprint || {})}</pre>
                      </MiniPanel>
                      <MiniPanel title="action_sequence">
                        <pre className="code-block evolution-code-block">{prettyJson(selectedPattern.action_sequence || [])}</pre>
                      </MiniPanel>
                      <MiniPanel title="skillability">
                        <pre className="code-block evolution-code-block">{prettyJson(selectedPattern.skillability || {})}</pre>
                      </MiniPanel>
                    </div>
                  )}
                </div>
              </div>
            )}

            {workbenchTab === "vocabulary" && (
              <div className="evolution-detail-stack">
                <div className="evolution-stat-grid">
                  <Stat label={t("evolution.vocabularyVersion")} value={String(vocabulary.vocabulary_version || 1)} />
                  <Stat label={t("evolution.labelCount")} value={String((vocabulary.labels || []).length)} />
                  <Stat label={t("evolution.aliasCount")} value={String((vocabulary.aliases || []).length)} />
                  <Stat label={t("evolution.unknownCount")} value={String((vocabulary.unknown_labels || []).length)} />
                </div>

                <div className="evolution-detail-panels">
                  <MiniPanel title={t("evolution.createLabel")}>
                    <div className="evolution-form-grid">
                      <input value={labelForm.label_type} onChange={(e) => setLabelForm((s) => ({ ...s, label_type: e.target.value }))} placeholder="label_type" />
                      <input value={labelForm.canonical_label} onChange={(e) => setLabelForm((s) => ({ ...s, canonical_label: e.target.value }))} placeholder="canonical_label" />
                      <input value={labelForm.domain} onChange={(e) => setLabelForm((s) => ({ ...s, domain: e.target.value }))} placeholder="domain" />
                      <input value={labelForm.parent_label} onChange={(e) => setLabelForm((s) => ({ ...s, parent_label: e.target.value }))} placeholder="parent_label" />
                      <input value={labelForm.raw_description} onChange={(e) => setLabelForm((s) => ({ ...s, raw_description: e.target.value }))} placeholder="raw_description" />
                    </div>
                    <div className="evolution-pattern-actions"><button className="btn primary" onClick={handleCreateLabel}>{t("evolution.createLabel")}</button></div>
                  </MiniPanel>

                  <MiniPanel title={t("evolution.createAlias")}>
                    <div className="evolution-form-grid">
                      <input value={aliasForm.label_type} onChange={(e) => setAliasForm((s) => ({ ...s, label_type: e.target.value }))} placeholder="label_type" />
                      <input value={aliasForm.canonical_label} onChange={(e) => setAliasForm((s) => ({ ...s, canonical_label: e.target.value }))} placeholder="canonical_label" />
                      <input value={aliasForm.alias_label} onChange={(e) => setAliasForm((s) => ({ ...s, alias_label: e.target.value }))} placeholder="alias_label" />
                    </div>
                    <div className="evolution-pattern-actions"><button className="btn primary" onClick={handleCreateAlias}>{t("evolution.createAlias")}</button></div>
                  </MiniPanel>
                </div>

                <div className="evolution-detail-panels">
                  <MiniPanel title={t("evolution.unknownPool")}>
                    {(vocabulary.unknown_labels || []).map((unknown) => (
                      <div key={unknown.unknown_id} className="evolution-unknown-card">
                        <div className="evolution-mini-title">{unknown.label_type} · {unknown.raw_description}</div>
                        <div className="evolution-mini-sub">seen {unknown.seen_count} · status {unknown.status}</div>
                        <div className="evolution-form-grid compact">
                          <input
                            value={unknownDrafts[unknown.unknown_id]?.canonical_label || unknown.proposed_subtype || unknown.proposed_type || unknown.proposed_domain || ""}
                            onChange={(e) => setUnknownDrafts((drafts) => ({ ...drafts, [unknown.unknown_id]: { ...(drafts[unknown.unknown_id] || {}), canonical_label: e.target.value } }))}
                            placeholder="canonical_label"
                          />
                          <input
                            value={unknownDrafts[unknown.unknown_id]?.alias_label || ""}
                            onChange={(e) => setUnknownDrafts((drafts) => ({ ...drafts, [unknown.unknown_id]: { ...(drafts[unknown.unknown_id] || {}), alias_label: e.target.value } }))}
                            placeholder="alias_label"
                          />
                        </div>
                        <div className="evolution-pattern-actions">
                          <button className="btn" onClick={() => handlePromoteUnknown(unknown.unknown_id)}>{t("evolution.promoteUnknown")}</button>
                          <button className="btn" onClick={() => handleDismissUnknown(unknown.unknown_id)}>{t("evolution.dismissUnknown")}</button>
                        </div>
                      </div>
                    ))}
                  </MiniPanel>

                  <MiniPanel title={t("evolution.aliases")}>
                    <pre className="code-block evolution-code-block">{prettyJson(vocabulary.aliases || [])}</pre>
                  </MiniPanel>

                  <MiniPanel title={t("evolution.labels")}>
                    <pre className="code-block evolution-code-block">{prettyJson(vocabulary.labels || [])}</pre>
                  </MiniPanel>
                </div>
              </div>
            )}
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

function MiniPanel({ title, children }) {
  return (
    <div className="card evolution-mini-panel">
      <div className="card-head"><span className="card-title">{title}</span></div>
      <div className="evolution-mini-panel-body">{children}</div>
    </div>
  );
}

function FormField({ label, children }) {
  return (
    <label className="evolution-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function JsonField({ label, value, onChange }) {
  return (
    <label className="evolution-field">
      <span>{label}</span>
      <textarea className="evolution-json-input" value={value} onChange={(e) => onChange(e.target.value)} spellCheck="false" />
    </label>
  );
}

function emptySkillForm() {
  return {
    name: "",
    description: "",
    status: "",
    skill_type: "",
    trigger_json: "{}",
    input_schema_json: "[]",
    parameter_extractor_json: "{}",
    steps_json: "[]",
    guards_json: "{}",
    fallback_policy_json: "{}",
    reason: "Manual skill edit from evolution workbench.",
  };
}

function skillFormFromDetail(detail) {
  if (!detail) return emptySkillForm();
  return {
    name: detail.name || "",
    description: detail.description || "",
    status: detail.status || "",
    skill_type: detail.skill_type || "",
    trigger_json: prettyJson(detail.trigger || {}),
    input_schema_json: prettyJson(detail.input_schema || []),
    parameter_extractor_json: prettyJson(detail.parameter_extractor || {}),
    steps_json: prettyJson(detail.steps || []),
    guards_json: prettyJson(detail.guards || {}),
    fallback_policy_json: prettyJson(detail.fallback_policy || {}),
    reason: "Manual skill edit from evolution workbench.",
  };
}

function buildSkillUpdatePayload(form) {
  return {
    name: form.name,
    description: form.description,
    status: form.status,
    skill_type: form.skill_type,
    trigger: safeParseJson(form.trigger_json, {}),
    input_schema: safeParseJson(form.input_schema_json, []),
    parameter_extractor: safeParseJson(form.parameter_extractor_json, {}),
    steps: safeParseJson(form.steps_json, []),
    guards: safeParseJson(form.guards_json, {}),
    fallback_policy: safeParseJson(form.fallback_policy_json, {}),
  };
}

function prettyJson(value) {
  return JSON.stringify(value ?? null, null, 2);
}

function safeParseJson(raw, fallback) {
  if (raw && typeof raw === "object") return raw;
  try {
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}
