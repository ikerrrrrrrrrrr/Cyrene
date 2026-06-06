function Topbar({
  page,
  theme,
  onToggleTheme,
  activeSession,
  realtimeStatus,
  rightSidebarCollapsed,
  onToggleRightSidebar,
  evolutionTab,
  setEvolutionTab,
}) {
  useDataVersion();
  const { t } = useI18n();
  const evolutionTabs = [
    { id: "skills", label: t("evolution.skills") },
    { id: "cc", label: t("evolution.ccLearning") },
    { id: "patterns", label: t("evolution.workbench") },
  ];
  const session = activeSession || { title: "—", subagents: [] };
  const runningSubagents = (session.subagents || []).filter((s) => s.status === "running").length;
  const fallbackStatus = session.status === "err" ? "error" : (session.status || "idle");
  const status = realtimeStatus || fallbackStatus;
  const title =
    page === "dashboard" ? <>{t("topbar.dashboard")}<span className="crumb-sep">/</span><b>{t("topbar.home")}</b></> :
    page === "chat" ? <>{t("topbar.chat")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "agents" ? <>{t("topbar.agentFlow")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "sessions" ? <>{t("topbar.sessions")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "memory" ? <>{t("topbar.memory")}<span className="crumb-sep">/</span><b>{t("topbar.pipeline")}</b></> :
    page === "context_debug" ? <>{t("topbar.contextDebug")}<span className="crumb-sep">/</span><b>{t("contextDebug.calls")}</b></> :
    page === "evolution" ? <b>{t("topbar.evolution")}</b> :
    page === "tasks" ? <b>{t("tasks.title")}</b> :
    page === "entities" ? <>{t("nav.entities")}<span className="crumb-sep">/</span><b>{t("entities.title")}</b></> :
    page === "knowledge" ? <>{t("nav.knowledge")}<span className="crumb-sep">/</span><b>{t("knowledge.title")}</b></> :
    <>{t("topbar.settings")}<span className="crumb-sep">/</span><b>{t("topbar.workspace")}</b></>;

  return (
    <div className="topbar">
      <span className="topbar-title">{title}</span>
      <div className="topbar-right">
        {page === "evolution" && (
          <div className="seg evolution-topbar-tabs">
            {evolutionTabs.map((item) => (
              <button
                key={item.id}
                className={"seg-btn" + (evolutionTab === item.id ? " active" : "")}
                onClick={() => setEvolutionTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        )}
        <span className="statlight">
          <span className={"dot " + status}></span> {t("topbar.status." + status)}
        </span>
        {runningSubagents > 0 && (
          <span className="statlight">
            <span className="dot warn"></span> {t("topbar.subagents", { n: runningSubagents, pl: runningSubagents > 1 ? "s" : "" })}
          </span>
        )}
        <span className="topbar-separator"></span>
        {page === "chat" && (
          <span className="right-panel-control">
            <button
              className={"iconbtn" + (!rightSidebarCollapsed ? " active" : "")}
              type="button"
              title={rightSidebarCollapsed ? t("topbar.expandRight") : t("topbar.collapseRight")}
              aria-expanded={!rightSidebarCollapsed}
              onClick={onToggleRightSidebar}
            >
              <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8">
                <rect x="3" y="4" width="12" height="10" rx="2" />
                <path d="M11 4v10" />
              </svg>
            </button>
          </span>
        )}
        <button
          className={"iconbtn theme-mode-button theme-mode-" + theme}
          title={theme === "dark" ? t("topbar.switchToLight") : t("topbar.switchToDark")}
          onClick={onToggleTheme}
        >
          {theme === "system"
            ? <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2" y="3" width="14" height="10" rx="1.5"/><path d="M7 13v2.5M11 13v2.5M5 15.5h8"/></svg>
            : theme === "dark"
              ? <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M13.8 11.4A6 6 0 0 1 6.6 4.2 5.2 5.2 0 1 0 13.8 11.4Z"/></svg>
              : <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="9" cy="9" r="3.5"/><path d="M9 2v2M9 14v2M2 9h2M14 9h2M4.5 4.5l1.5 1.5M12 12l1.5 1.5M4.5 13.5l1.5-1.5M12 6l1.5-1.5"/></svg>
          }
        </button>
      </div>
    </div>
  );
}
