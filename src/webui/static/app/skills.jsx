// Skills page
const { useState: useStateSk } = React;

function SkillsPage() {
  const dv = useDataVersion();
  const { t } = useI18n();
  const [skills, setSkills] = useStateSk(() => DATA.skills.map((s) => ({ ...s })));
  const [selected, setSelected] = useStateSk(DATA.skills[0]?.id || "");
  const [tab, setTab] = useStateSk("installed"); // installed | available | all
  const [query, setQuery] = useStateSk("");

  // Re-seed from DATA when fresh data arrives.
  React.useEffect(() => {
    if (DATA.skills && DATA.skills.length) {
      setSkills(DATA.skills.map((s) => ({ ...s })));
      if (!selected && DATA.skills[0]) setSelected(DATA.skills[0].id);
    }
  }, [dv]);

  if (!skills.length) {
    return (
      <div style={{ padding: 48, color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 13 }}>
        {t("skills.loading")}
      </div>
    );
  }

  const skill = skills.find((s) => s.id === selected) || skills[0];

  const filtered = skills.filter((s) => {
    if (tab === "installed" && !s.installed) return false;
    if (tab === "available" && s.installed) return false;
    if (query && !(s.name + " " + s.desc).toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  function toggleEnabled(id) {
    setSkills((arr) => arr.map((s) => s.id === id ? { ...s, enabled: !s.enabled } : s));
  }
  function install(id) {
    setSkills((arr) => arr.map((s) => s.id === id ? { ...s, installed: true, enabled: true } : s));
    setTab("installed");
  }
  function uninstall(id) {
    setSkills((arr) => arr.map((s) => s.id === id ? { ...s, installed: false, enabled: false } : s));
  }

  const counts = {
    installed: skills.filter((s) => s.installed).length,
    available: skills.filter((s) => !s.installed).length,
    all: skills.length,
  };

  return (
    <div className="skills-layout">
      <div className="skills-side">
        <div className="skills-tabs">
          {["installed", "available", "all"].map((tabKey) => (
            <div key={tabKey}
                 className={"skills-tab " + (tab === tabKey ? "active" : "")}
                 onClick={() => setTab(tabKey)}>
              {t("skills." + tabKey)}<span className="skills-tab-count">{counts[tabKey]}</span>
            </div>
          ))}
        </div>
        <div className="skills-search">
          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
          </svg>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("skills.filterPlaceholder")} />
        </div>
        <div className="skills-list-page">
          {filtered.map((s) => (
            <div key={s.id}
                 className={"skill-row " + (s.id === selected ? "active" : "") + (s.installed ? " installed" : " available")}
                 onClick={() => setSelected(s.id)}>
              <div className="skill-row-icon">{s.icon || "•"}</div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="skill-row-name">{s.name}</div>
                <div className="skill-row-desc">{s.desc}</div>
                <div className="skill-row-meta">
                  <span>v{s.version}</span>
                  <span>· {s.author}</span>
                  {s.installed && s.invocations != null && <span>· {s.invocations} {t("skills.runs")}</span>}
                </div>
              </div>
              <div className="skill-row-state">
                {s.installed ? (
                  <span className={"skill-state-dot " + (s.enabled ? "on" : "off")}></span>
                ) : (
                  <span className="skill-state-tag">{t("skills.available")}</span>
                )}
              </div>
            </div>
          ))}
          {filtered.length === 0 && (
            <div style={{ padding: "32px 16px", color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 12, textAlign: "center" }}>
              {t("skills.noMatch")}
            </div>
          )}
        </div>
      </div>

      <SkillDetail
        skill={skill}
        onToggle={() => toggleEnabled(skill.id)}
        onInstall={() => install(skill.id)}
        onUninstall={() => uninstall(skill.id)}
      />
    </div>
  );
}

function SkillDetail({ skill, onToggle, onInstall, onUninstall }) {
  const { t } = useI18n();
  if (!skill) return null;

  return (
    <div className="skill-detail">
      <div className="skill-detail-head">
        <div className="skill-detail-icon">{skill.icon || "•"}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="skill-detail-meta">
            {skill.tags && skill.tags.map((t) => <span key={t} className="skill-tag">{t}</span>)}
            <span className="skill-detail-version">v{skill.version}</span>
            <span className="skill-detail-author">· {skill.author}</span>
          </div>
          <h1 className="skill-detail-title">{skill.name}</h1>
          <p className="skill-detail-desc">{skill.desc}</p>
        </div>
        <div className="skill-detail-actions">
          {skill.installed ? (
            <>
              <div className="enable-toggle">
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>
                  {skill.enabled ? t("skills.enabled") : t("skills.disabled")}
                </span>
                <div className={"toggle " + (skill.enabled ? "on" : "")} onClick={onToggle}></div>
              </div>
              <button className="btn" onClick={onUninstall}>{t("skills.uninstall")}</button>
            </>
          ) : (
            <button className="btn primary" onClick={onInstall}>
              {t("skills.installSkill")}
            </button>
          )}
        </div>
      </div>

      <div className="skill-detail-body">
        {skill.installed && (
          <div className="skill-stats">
            <Stat label={t("skills.invocations")} value={skill.invocations ?? "—"} />
            <Stat label={t("skills.successRate")} value={skill.successRate != null ? Math.round(skill.successRate * 100) + "%" : "—"} />
            <Stat label={t("skills.avgDuration")} value={skill.avgDuration || "—"} />
            <Stat label={t("skills.lastUsed")} value={skill.lastUsed || "—"} />
          </div>
        )}

        <SkillSection title={t("skills.systemPrompt")}>
          <pre className="code-block" style={{ color: "var(--text)" }}>{skill.prompt || "—"}</pre>
        </SkillSection>

        <SkillSection title={t("skills.toolsUsed")}>
          <div className="tool-chips">
            {(skill.tools || []).map((t) => (
              <span key={t} className="tool-chip">{t}</span>
            ))}
            {(skill.tools || []).length === 0 && <span style={{ color: "var(--text-4)", fontFamily: "var(--mono)", fontSize: 11.5 }}>—</span>}
          </div>
        </SkillSection>

        <SkillSection title={t("skills.trigger")}>
          <div className="kv">
            <span className="k">{t("skills.slashCommand")}</span>
            <span className="v">{skill.hotkey ? "/" + skill.hotkey.toLowerCase() : "—"}</span>
            <span className="k">{t("skills.autoSuggest")}</span>
            <span className="v">{skill.installed && skill.enabled ? t("skills.on") : t("skills.off")}</span>
            <span className="k">{t("skills.mention")}</span>
            <span className="v">@{skill.id}</span>
          </div>
        </SkillSection>

        {skill.installed && skill.recent && skill.recent.length > 0 && (
          <SkillSection title={t("skills.recentActivity") + " · " + skill.recent.length}>
            <div className="skill-recent">
              {skill.recent.map((r, i) => (
                <div key={i} className="recent-row">
                  <span className={"sa-dot " + (r.outcome === "ok" ? "done" : r.outcome === "warn" ? "running" : r.outcome === "err" ? "err" : "queued")}
                        style={{ marginTop: 0, flexShrink: 0 }}></span>
                  <span className="recent-target">{r.target}</span>
                  <span className="recent-time">{r.time}</span>
                </div>
              ))}
            </div>
          </SkillSection>
        )}
      </div>
    </div>
  );
}

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

window.SkillsPage = SkillsPage;
