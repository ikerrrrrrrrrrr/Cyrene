// Evolution page — skills, CC learning, behavior patterns
function EvolutionPage() {
  useDataVersion();
  const { t } = useI18n();
  const [tab, setTab] = useStateSet("skills");
  const [scripts, setScripts] = useStateSet([]);
  const [ccData, setCcData] = useStateSet(null);
  const [installedSkills, setInstalledSkills] = useStateSet([]);
  const [loading, setLoading] = useStateSet(true);

  const fetchData = async () => {
    setLoading(true);
    try {
      const [evRes, ccRes, skillsRes] = await Promise.all([
        fetch("/api/evolution").then(r => r.json()),
        fetch("/api/cc/learning").then(r => r.json()),
        fetch("/api/skills/installed").then(r => r.json()),
      ]);
      setScripts(evRes.scripts || []);
      setCcData(ccRes);
      setInstalledSkills(skillsRes.skills || []);
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
  const handleInstall = async (skill) => {
    await fetch("/api/skills/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: skill.id, def: skill }),
    });
    fetchData();
  };
  const handleUninstall = async (id) => {
    await fetch(`/api/skills/${id}/uninstall`, { method: "POST" });
    fetchData();
  };
  const handleToggle = async (id) => {
    await fetch(`/api/skills/${id}/toggle`, { method: "POST" });
    fetchData();
  };

  const fmtPct = (v) => (v * 100).toFixed(0) + "%";
  const fmtTime = (ts) => ts ? new Date(ts).toLocaleDateString() : "—";

  const tabs = [
    { id: "skills", label: t("evolution.skills") },
    { id: "cc", label: t("evolution.ccLearning") },
    { id: "patterns", label: t("evolution.patterns") },
  ];

  const installedIds = new Set(installedSkills.map(s => s.id));

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
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {DATA.skills && DATA.skills.map((sk) => {
            const isInstalled = installedIds.has(sk.id);
            const inst = installedSkills.find(s => s.id === sk.id);
            return (
              <div key={sk.id} className="card" style={{
                display: "flex", alignItems: "center", gap: 12,
                padding: "12px 16px"
              }}>
                <span style={{ fontSize: 18 }}>{sk.icon || "✸"}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{sk.name}</div>
                  <div style={{ fontSize: 11, color: "var(--text-4)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {sk.desc || sk.id}
                  </div>
                </div>
                {isInstalled ? (
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <div className={"toggle " + (inst && inst.enabled !== false ? "on" : "")}
                         onClick={() => handleToggle(sk.id)} />
                    <button className="btn" style={{ fontSize: 11 }}
                            onClick={() => handleUninstall(sk.id)}>
                      {t("evolution.uninstall")}
                    </button>
                  </div>
                ) : (
                  <button className="btn primary" style={{ fontSize: 11 }}
                          onClick={() => handleInstall(sk)}>
                    {t("evolution.installSkill")}
                  </button>
                )}
              </div>
            );
          })}
          {(!DATA.skills || DATA.skills.length === 0) && (
            <div style={{ textAlign: "center", padding: 40, color: "var(--text-4)", fontSize: 12 }}>
              {t("evolution.noPatterns")}
            </div>
          )}
        </div>
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

              {ccData.tools && ccData.tools.common_tasks && ccData.tools.common_tasks.length > 0 && (
                <div className="card" style={{ padding: "12px 16px" }}>
                  <div className="card-head">
                    <span className="card-title">{t("evolution.commonTasks")}</span>
                  </div>
                  <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
                    {ccData.tools.common_tasks.map(([task, count], i) => (
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
