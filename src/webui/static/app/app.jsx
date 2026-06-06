// Cyrene — app shell + page router
const { useState: useStateApp, useEffect: useEffectApp, useMemo: useMemoApp } = React;

function readStoredTweak(key, fallback) {
  try { var v = localStorage.getItem("cyrene-tweak-" + key); return v !== null ? JSON.parse(v) : fallback; } catch(e) { return fallback; }
}

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": readStoredTweak("theme", "system"),
  "accent": readStoredTweak("accent", "#5ec59e"),
  "density": readStoredTweak("density", "cozy"),
  "textSize": readStoredTweak("textSize", "default"),
  "orientation": readStoredTweak("orientation", "horizontal"),
  "showLegend": readStoredTweak("showLegend", true),
  "animatePulse": readStoredTweak("animatePulse", true)
}/*EDITMODE-END*/;

const ACCENT_PRESETS = {
  dark:  ["#4fd1a0", "#6dbde0", "#b8a2e0", "#e8ae5c", "#e87070"],
  light: ["#2da873", "#3b90c8", "#7858b0", "#c88520", "#d04848"],
};
const VALID_UI_PAGES = new Set(["chat", "agents", "sessions", "memory", "evolution", "settings", "tasks", "entities", "context_debug", "knowledge"]);

function readStoredUiPage() {
  try {
    var page = localStorage.getItem("cyrene-ui-page");
    if (page === "status") page = "evolution"; // migrate old key
    if (page === "skills") page = "evolution"; // merge old skills page
    return VALID_UI_PAGES.has(page) ? page : "chat";
  } catch (e) {
    return "dashboard";
  }
}

function readStoredSessionId() {
  try {
    return localStorage.getItem("cyrene-ui-session-id");
  } catch (e) {
    return null;
  }
}

function readStoredBool(key, fallback) {
  try {
    var raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    return raw === "1";
  } catch (e) {
    return fallback;
  }
}

function SetupWizard({ theme, onToggleTheme }) {
  useDataVersion();
  const { t } = useI18n();
  const onboarding = DATA.onboarding || {};
  const [step, setStep] = useStateApp(onboarding.activeStep || "llm");
  const [busy, setBusy] = useStateApp(false);
  const [error, setError] = useStateApp("");
  const [notice, setNotice] = useStateApp("");
  const [llmForm, setLlmForm] = useStateApp({
    api_key: "",
    base_url: onboarding.llm?.baseUrl || "",
    model: onboarding.llm?.model || "",
  });
  const [mode, setMode] = useStateApp(onboarding.personality?.mode || "name");
  const [personalityName, setPersonalityName] = useStateApp(onboarding.personality?.label || "");
  const [customSoul, setCustomSoul] = useStateApp(onboarding.personality?.currentContent || "");

  React.useEffect(function () {
    setStep(onboarding.activeStep || "done");
    setLlmForm({
      api_key: "",
      base_url: onboarding.llm?.baseUrl || "",
      model: onboarding.llm?.model || "",
    });
    setMode(onboarding.personality?.mode || "name");
    setPersonalityName(onboarding.personality?.label || "");
    setCustomSoul(onboarding.personality?.currentContent || "");
  }, [onboarding.activeStep, onboarding.llm?.baseUrl, onboarding.llm?.model, onboarding.personality?.mode, onboarding.personality?.label, onboarding.personality?.currentContent]);

  async function applyOnboardingResponse(r) {
    const payload = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(payload.error || payload.detail || ("HTTP " + r.status));
    }
    if (payload.onboarding) {
      DATA.onboarding = payload.onboarding;
      window.bumpData && window.bumpData();
    }
    if (window.reloadUiData) await window.reloadUiData();
    return payload;
  }

  async function saveLlm() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = await applyOnboardingResponse(await fetch("/api/onboarding/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(llmForm),
      }));
      setNotice(t("setup.llmVerified") + (payload.preview ? ": " + payload.preview : "."));
      setStep((payload.onboarding && payload.onboarding.activeStep) || "personality");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function savePersonality() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = await applyOnboardingResponse(await fetch("/api/onboarding/personality", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: mode,
          name: personalityName,
          content: customSoul,
        }),
      }));
      setNotice(t("setup.personalityApplied"));
      setStep((payload.onboarding && payload.onboarding.activeStep) || "done");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const stepItems = [
    { id: "llm", label: t("setup.llmApi"), done: !!onboarding.llm?.configured },
    { id: "personality", label: t("setup.personality"), done: !!onboarding.personality?.configured },
  ];

  return (
    <div className="setup-shell" data-theme={theme}>
      <div className="setup-topbar">
        <div className="setup-brand">
          <div className="brand-mark"></div>
          <div>
            <div className="brand-name">{(DATA.assistantName || "Cyrene").toUpperCase()}</div>
            <div className="setup-brand-meta">
              <div className="setup-kicker">{t("setup.kicker")}</div>
              <div className="setup-version">{DATA.appVersion || "—"}</div>
            </div>
          </div>
        </div>
        <button className="theme-toggle-btn" title={theme === "dark" ? t("topbar.switchToLight") : t("topbar.switchToDark")} onClick={onToggleTheme}>
          <span className="theme-toggle-icon">
            {theme === "system"
              ? <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2" y="3" width="14" height="10" rx="1.5"/><path d="M7 13v2.5M11 13v2.5M5 15.5h8"/></svg>
              : theme === "dark"
                ? <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M11 4.5A7 7 0 1 1 9 13.5A5 5 0 1 0 11 4.5Z"/></svg>
                : <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="9" cy="9" r="3.5"/><path d="M9 2v2M9 14v2M2 9h2M14 9h2M4.5 4.5l1.5 1.5M12 12l1.5 1.5M4.5 13.5l1.5-1.5M12 6l1.5-1.5"/></svg>
            }
          </span>
          <span>{theme === "system" ? t("settings.system") : theme === "dark" ? t("settings.light") : t("settings.dark")}</span>
        </button>
      </div>

      <div className="setup-hero">
        <div className="setup-copy">
          <div className="setup-eyebrow">{onboarding.isAbsoluteFreshStart ? t("setup.freshDetected") : t("setup.setupIncomplete")}</div>
          <h1>{t("setup.heroTitle")}</h1>
          <p>{t("setup.heroDesc")}</p>
        </div>
        <div className="setup-steps">
          {stepItems.map((item, index) => (
            <div key={item.id} className={"setup-step-card " + ((step === item.id && onboarding.needsOnboarding) ? "active" : "")}>
              <div className="setup-step-index">{item.done ? "✓" : index + 1}</div>
              <div>
                <div className="setup-step-label">{item.label}</div>
                <div className="setup-step-meta">{item.done ? t("setup.configured") : t("setup.required")}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="setup-panel">
        {step === "llm" && (
          <div className="setup-section">
            <h2>{t("setup.llmSectionTitle")}</h2>
            <p className="subtitle">{t("setup.llmSubtitle")}</p>
            <div className="field">
              <div className="label">{t("setup.apiKeyLabel")}<small>{t("setup.apiKeyHint")}</small></div>
              <input
                className="input"
                type="password"
                value={llmForm.api_key}
                onChange={(e) => setLlmForm({ ...llmForm, api_key: e.target.value })}
                placeholder="sk-..."
              />
            </div>
            <div className="field">
              <div className="label">{t("setup.endpointLabel")}<small>{t("setup.endpointHint")}</small></div>
              <input
                className="input mono"
                value={llmForm.base_url}
                onChange={(e) => setLlmForm({ ...llmForm, base_url: e.target.value })}
              />
            </div>
            <div className="field">
              <div className="label">{t("setup.modelLabel")}<small>{t("setup.modelHint")}</small></div>
              <input
                className="input mono"
                value={llmForm.model}
                onChange={(e) => setLlmForm({ ...llmForm, model: e.target.value })}
              />
            </div>
            <div className="setup-actions">
              <button className="btn primary" onClick={saveLlm} disabled={busy}>{busy ? t("setup.testing") : t("setup.saveAndTest")}</button>
            </div>
          </div>
        )}

        {step === "personality" && (
          <div className="setup-section">
            <h2>{t("setup.personalitySectionTitle")}</h2>
            <p className="subtitle">{t("setup.personalitySubtitle")}</p>
            <div className="seg" style={{ marginBottom: 18 }}>
              <button className={"seg-btn " + (mode === "name" ? "active" : "")} onClick={() => setMode("name")}>{t("setup.byName")}</button>
              <button className={"seg-btn " + (mode === "custom" ? "active" : "")} onClick={() => setMode("custom")}>{t("setup.customSoul")}</button>
              <button className={"seg-btn " + (mode === "default" ? "active" : "")} onClick={() => setMode("default")}>{t("setup.defaultLabel")}</button>
            </div>

            {mode === "name" && (
              <div className="field">
                <div className="label">{t("setup.personalityNameLabel")}<small>{t("setup.personalityNameHint")}</small></div>
                <input
                  className="input"
                  value={personalityName}
                  onChange={(e) => setPersonalityName(e.target.value)}
                  placeholder="Lelouch Lamperouge / Steve Jobs / Sherlock Holmes"
                />
              </div>
            )}

            {mode === "custom" && (
              <div className="field" style={{ display: "block" }}>
                <div className="label" style={{ marginBottom: 8 }}>{t("setup.soulContentLabel")}<small>{t("setup.soulContentHint")}</small></div>
                <textarea
                  className="input mono"
                  value={customSoul}
                  onChange={(e) => setCustomSoul(e.target.value)}
                  style={{ width: "100%", minHeight: 260, fontSize: 12, lineHeight: 1.5 }}
                />
              </div>
            )}

            {mode === "default" && (
              <div className="setup-note">
                {t("setup.defaultDesc")}
              </div>
            )}

            <div className="setup-actions">
              <button className="btn primary" onClick={savePersonality} disabled={busy}>{busy ? t("setup.applying") : t("setup.applyPersonality")}</button>
            </div>
          </div>
        )}

        {step === "done" && (
          <div className="setup-section">
            <h2>{t("setup.workspaceReady")}</h2>
            <p className="subtitle">{t("setup.workspaceReadyDesc")}</p>
            <div className="setup-actions">
              <button className="btn primary" onClick={() => { DATA.onboarding = { ...DATA.onboarding, needsOnboarding: false }; window.bumpData && window.bumpData(); }}>{t("setup.enterWorkspace")}</button>
            </div>
          </div>
        )}

        {(notice || error) && (
          <div className={"setup-feedback " + (error ? "error" : "ok")}>
            {error || notice}
          </div>
        )}
      </div>
    </div>
  );
}

function App() {
  useDataVersion();
  const { lang } = useI18n();
  const [page, setPage] = useStateApp(readStoredUiPage);
  const [evolutionTab, setEvolutionTab] = useStateApp("skills");
  const [selectedSessionId, setSelectedSessionId] = useStateApp(readStoredSessionId);
  const [leftSidebarCollapsed, setLeftSidebarCollapsed] = useStateApp(function () { return readStoredBool("cyrene-left-sidebar-collapsed", false); });
  const [rightSidebarCollapsed, setRightSidebarCollapsed] = useStateApp(function () { return readStoredBool("cyrene-right-sidebar-collapsed", false); });
  const [rightSidebarView, setRightSidebarView] = useStateApp(function () {
    try { return localStorage.getItem("cyrene-right-sidebar-view") || "overview"; }
    catch (e) { return "overview"; }
  });
  const [searchOpen, setSearchOpen] = useStateApp(false);
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  // ── SSE-driven real-time status for the topbar status light ──
  const [realtimeStatus, setRealtimeStatus] = useStateApp(null);
  const realtimeStatusTimerRef = React.useRef(null);

  useEffectApp(function () {
    if (typeof window.__sseHandlers === "undefined") return;
    const PROCESSING_TYPES = ["phase_transition", "tool_call", "llm_call"];

    function handler(event) {
      var newStatus = null;
      if (PROCESSING_TYPES.indexOf(event.type) !== -1) {
        newStatus = "running";
      } else if (event.type === "chat_message") {
        newStatus = "done";
      } else if (event.type === "session_update" && event.status) {
        newStatus = event.status === "err" ? "error" : event.status;
      }
      if (newStatus) {
        setRealtimeStatus(newStatus);
      }
      // Auto-clear after 30 s of no SSE events to avoid stale "running"
      if (realtimeStatusTimerRef.current) {
        clearTimeout(realtimeStatusTimerRef.current);
      }
      realtimeStatusTimerRef.current = setTimeout(function () {
        setRealtimeStatus(null);
      }, 30000);
    }

    window.__sseHandlers.add(handler);
    return function () {
      window.__sseHandlers.delete(handler);
      if (realtimeStatusTimerRef.current) {
        clearTimeout(realtimeStatusTimerRef.current);
      }
    };
  }, []);

  const activeSession = useMemoApp(function () {
    return (selectedSessionId
      ? DATA.sessions.find(function (session) { return session.id === selectedSessionId; })
      : null) || DATA.sessions[0] || null;
  }, [selectedSessionId, DATA.sessions]);

  function selectSession(id) {
    setSelectedSessionId(id || null);
  }

  const [systemTheme, setSystemTheme] = useStateApp(function () {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  function resolveActualTheme(mode) {
    if (mode === "system") {
      return systemTheme;
    }
    return mode;
  }

  const actualTheme = React.useMemo(function () {
    return resolveActualTheme(t.theme);
  }, [t.theme, systemTheme]);

  useEffectApp(function () {
    localStorage.setItem("cyrene-tweak-theme", JSON.stringify(t.theme));
    localStorage.setItem("cyrene-tweak-accent", JSON.stringify(t.accent));
    localStorage.setItem("cyrene-tweak-density", JSON.stringify(t.density));
    localStorage.setItem("cyrene-tweak-textSize", JSON.stringify(t.textSize));
    localStorage.setItem("cyrene-tweak-orientation", JSON.stringify(t.orientation));
    localStorage.setItem("cyrene-tweak-showLegend", JSON.stringify(t.showLegend));
    localStorage.setItem("cyrene-tweak-animatePulse", JSON.stringify(t.animatePulse));
    /* Legacy key kept for backward compat (read by index.html <script>). */
    localStorage.setItem("cyrene-theme-mode", t.theme);
    const applied = actualTheme;
    document.documentElement.dataset.theme = applied;
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    document.documentElement.style.setProperty("--accent", t.accent);
    const m = t.accent.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      const r = parseInt(m[1].slice(0,2),16), g = parseInt(m[1].slice(2,4),16), b = parseInt(m[1].slice(4,6),16);
      document.documentElement.style.setProperty("--accent-faint", `rgba(${r},${g},${b},0.08)`);
      document.documentElement.style.setProperty("--accent-dim", `rgba(${r},${g},${b},0.35)`);
      const lum = (0.299*r + 0.587*g + 0.114*b) / 255;
      document.documentElement.style.setProperty("--accent-text", lum > 0.55 ? "#0d1612" : "#ffffff");
    }
    document.documentElement.dataset.density = t.density;
    document.documentElement.dataset.textSize = t.textSize || "default";
    document.documentElement.dataset.animPulse = t.animatePulse ? "on" : "off";
    document.documentElement.dataset.legend = t.showLegend ? "on" : "off";
    window.dispatchEvent(new CustomEvent("cyrene:theme-change", {
      detail: { mode: t.theme, actualTheme: applied },
    }));
    delete document.documentElement.dataset.booting;
  }, [t.theme, t.accent, t.density, t.textSize, t.animatePulse, t.showLegend, actualTheme]);

  useEffectApp(function () {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    function onChange(event) {
      setSystemTheme(event.matches ? "dark" : "light");
    }
    mq.addEventListener("change", onChange);
    return function () { mq.removeEventListener("change", onChange); };
  }, []);

  function toggleTheme() {
    const order = ["system", "light", "dark"];
    const idx = order.indexOf(t.theme);
    const nextMode = order[(idx + 1) % 3];
    const nextActual = resolveActualTheme(nextMode);
    const presetIndex = (ACCENT_PRESETS[actualTheme] || []).indexOf(t.accent);
    const nextAccent = presetIndex >= 0 ? (ACCENT_PRESETS[nextActual] || [])[presetIndex] : t.accent;
    setTweak({ theme: nextMode, accent: nextAccent });
  }

  useEffectApp(function () {
    try {
      localStorage.setItem("cyrene-ui-page", page);
    } catch (e) {}
  }, [page]);

  useEffectApp(function () {
    try {
      if (selectedSessionId) localStorage.setItem("cyrene-ui-session-id", selectedSessionId);
      else localStorage.removeItem("cyrene-ui-session-id");
    } catch (e) {}
  }, [selectedSessionId]);

  useEffectApp(function () {
    try {
      localStorage.setItem("cyrene-left-sidebar-collapsed", leftSidebarCollapsed ? "1" : "0");
    } catch (e) {}
  }, [leftSidebarCollapsed]);

  useEffectApp(function () {
    try {
      localStorage.setItem("cyrene-right-sidebar-collapsed", rightSidebarCollapsed ? "1" : "0");
    } catch (e) {}
  }, [rightSidebarCollapsed]);

  useEffectApp(function () {
    try {
      localStorage.setItem("cyrene-right-sidebar-view", rightSidebarView);
    } catch (e) {}
  }, [rightSidebarView]);

  useEffectApp(function () {
    if (selectedSessionId && !DATA.sessions.some(function (session) { return session.id === selectedSessionId; })) {
      setSelectedSessionId(null);
    }
  }, [selectedSessionId, DATA.sessions]);

  useEffectApp(function () {
    window.__selectedSessionId = activeSession ? activeSession.id : null;
    window.selectUiSession = selectSession;
    window.__setAppPage = setPage;
    return function () {
      delete window.__selectedSessionId;
      delete window.selectUiSession;
      delete window.__setAppPage;
    };
  }, [activeSession]);

  const needsOnboarding = !!(DATA.onboarding && DATA.onboarding.needsOnboarding);
  const canCollapseRightSidebar = page === "chat" || page === "agents" || page === "sessions";

  if (needsOnboarding) {
    return <SetupWizard theme={t.theme} onToggleTheme={toggleTheme} />;
  }

  return (
    <div className={"app" + (leftSidebarCollapsed ? " sidebar-collapsed" : "")} data-screen-label={"Cyrene · " + page}>
      <Sidebar
        page={page}
        setPage={setPage}
        selectedSessionId={activeSession ? activeSession.id : null}
        onSelectSession={selectSession}
        collapsed={leftSidebarCollapsed}
        onToggleCollapsed={function () { setLeftSidebarCollapsed(function (value) { return !value; }); }}
        onOpenSearch={function () { setSearchOpen(true); }}
      />
      <div className="page">
        <Topbar
          page={page}
          theme={t.theme}
          onToggleTheme={toggleTheme}
          activeSession={activeSession}
          realtimeStatus={realtimeStatus}
          setPage={setPage}
          leftSidebarCollapsed={leftSidebarCollapsed}
          onToggleLeftSidebar={function () { setLeftSidebarCollapsed(function (value) { return !value; }); }}
          rightSidebarCollapsed={rightSidebarCollapsed}
          onToggleRightSidebar={function () {
            if (!canCollapseRightSidebar) return;
            setRightSidebarCollapsed(function (value) { return !value; });
          }}
          canCollapseRightSidebar={canCollapseRightSidebar}
          evolutionTab={evolutionTab}
          setEvolutionTab={setEvolutionTab}
        />
        {page === "dashboard" && <DashboardPage />}
        {page === "chat"     && <ChatPage
                                  selectedSessionId={activeSession ? activeSession.id : null}
                                  onSelectSession={selectSession}
                                  rightSidebarCollapsed={rightSidebarCollapsed}
                                  setRightSidebarCollapsed={setRightSidebarCollapsed}
                                  rightSidebarView={rightSidebarView}
                                  setRightSidebarView={setRightSidebarView} />}
        {page === "agents"   && <AgentsPage orientation={t.orientation} selectedSessionId={activeSession ? activeSession.id : null} rightSidebarCollapsed={false} />}
        {page === "sessions" && <SessionsPage
                                  selectedSessionId={activeSession ? activeSession.id : null}
                                  onSelectSession={selectSession}
                                  rightSidebarCollapsed={false}
                                  onOpenAgents={(sessionId) => {
                                    selectSession(sessionId);
                                    setRightSidebarCollapsed(false);
                                    setRightSidebarView("agents");
                                    setPage("chat");
                                  }} />}
        {page === "memory"   && <MemoryPage />}
        {page === "context_debug" && React.createElement(
          window.ContextDebuggerPage || (function () { return React.createElement("div", { className: "page" }, "Loading context debugger..."); }),
          {}
        )}
        {page === "evolution" && <EvolutionPage tab={evolutionTab} setTab={setEvolutionTab} />}
        {page === "tasks" && React.createElement(
          window.ScheduledTasksPage || (function () { return React.createElement("div", { className: "page" }, "Loading tasks..."); }),
          {}
        )}
        {page === "entities" && React.createElement(
          window.EntitiesPage || (function () { return React.createElement("div", { className: "page" }, "Loading..."); }),
          {}
        )}
        {page === "knowledge" && React.createElement(
          window.KnowledgePage || (function () { return React.createElement("div", { className: "page" }, "Loading..."); }),
          {}
        )}
        {page === "settings" && (
          <SettingsPage
            tweaks={t}
            setTweak={setTweak}
            actualTheme={actualTheme}
            accentPresets={ACCENT_PRESETS[actualTheme] || []}
          />
        )}
      </div>

      {searchOpen && React.createElement(
        window.SearchOverlay || (function () { return null; }),
        {
          onClose: function () { setSearchOpen(false); },
          onOpenSession: function () { setPage("sessions"); },
        }
      )}
    </div>
  );
}

function readDeveloperMode() {
  try { return localStorage.getItem("cyrene-developer-mode") === "1"; } catch(e) { return false; }
}

function Sidebar({ page, setPage, selectedSessionId, onSelectSession, collapsed, onToggleCollapsed, onOpenSearch }) {
  useDataVersion();
  const { t } = useI18n();
  const [devMode, setDevMode] = useStateApp(readDeveloperMode);

  useEffectApp(function () {
    function onDevModeChange() { setDevMode(readDeveloperMode()); }
    window.addEventListener("cyrene-developer-mode-change", onDevModeChange);
    return function () { window.removeEventListener("cyrene-developer-mode-change", onDevModeChange); };
  }, []);

  const sessionCount = (DATA.sessions || []).length;
  const activeRecentSessionId = selectedSessionId || DATA.sessions[0]?.id || null;
  const allItems = [
    { id: "dashboard", label: t("nav.dashboard"), icon: "◫", key: "1", devOnly: true },
    { id: "chat",     label: t("nav.chat"),     icon: "▸", key: "2" },
    { id: "agents",   label: t("nav.agentFlow"),   icon: "⌘", key: "3", devOnly: true },
    { id: "tasks",    label: t("nav.tasks"),    icon: "◎", key: "4" },
    { id: "sessions", label: t("nav.sessions"), icon: "≡", key: "5", badge: sessionCount > 0 ? String(sessionCount) : null },
    { id: "memory",   label: t("nav.memory"),   icon: "▤", key: "6" },
    { id: "context_debug", label: t("nav.contextDebug"), icon: "◇", key: "7", devOnly: true },
    { id: "evolution", label: t("nav.evolution"), icon: "⟁", key: "8", cssClass: "evo-icon" },
    { id: "entities", label: t("nav.entities"), icon: "⊙", key: "9" },
    { id: "knowledge", label: t("nav.knowledge"), icon: "✦", key: "0" },
  ];
  const items = allItems.filter(function (it) { return !it.devOnly || devMode; });
  const brandName = (DATA.assistantName || "CYRENE").toUpperCase();
  return (
    <div className={"sidebar" + (collapsed ? " collapsed" : "")}>
      <div className="sidebar-tools">
        <div className="sidebar-brand-inline" title={brandName} onClick={() => { try { localStorage.setItem("cyrene-settings-section", "about"); } catch(e) {} setPage("settings"); }} style={{ cursor: "pointer" }}>
          <div className="brand-mark"></div>
          <div className="brand-name">{brandName}</div>
        </div>
        <span className="sidebar-tool-spacer"></span>
        <button className="windowbar-btn" type="button" title={t("topbar.search")} onClick={function () { onOpenSearch && onOpenSearch(); }}>
          <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="8" cy="8" r="4.2" />
            <path d="M11.2 11.2 15 15" />
          </svg>
        </button>
        <button className="windowbar-btn sidebar-collapse-btn" type="button" title={collapsed ? t("topbar.expandLeft") : t("topbar.collapseLeft")} onClick={onToggleCollapsed}>
          <svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7">
            <rect x="3" y="3" width="12" height="12" rx="2.5" />
            <path d="M7 3v12" />
          </svg>
        </button>
      </div>

      <div className="nav" style={{ paddingTop: 0 }}>
        {items.map((it) => (
          <div key={it.id}
               className={"nav-item" + (page === it.id ? " active" : "") + (it.cssClass ? " " + it.cssClass : "")}
               onClick={() => setPage(it.id)}>
            <span style={{ color: "currentColor", fontFamily: "var(--mono)", width: 14, textAlign: "center" }}>
              {it.icon}
            </span>
            <span>{it.label}</span>
            {it.badge && <span className="nav-badge">{it.badge}</span>}
          </div>
        ))}
      </div>

      {!collapsed && (
        <>
          <div className="nav-section nav-section-collapsible" style={{ cursor: "default" }}>
            <span>{t("nav.recentSessions")}</span>
            <span className="nav-section-link"
                  title={t("chat.newSessionTitle")}
                  onClick={async (e) => {
                    e.stopPropagation();
                    if (!confirm(t("chat.confirmNewSession"))) return;
                    try {
                      if (window.resetChatRuntime) window.resetChatRuntime({ abort: true });
                      const r = await fetch("/api/sessions", { method: "POST" });
                      if (!r.ok) throw new Error("HTTP " + r.status);
                      const data = await r.json();
                      if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
                    } catch (err) { alert("Failed: " + err.message); }
                  }}>
              {t("nav.newSession")}
            </span>
          </div>
          <div className="nav recent-session-list" style={{ paddingTop: 0 }}>
            {DATA.sessions.slice(0, 4).map((r) => (
              <div key={r.id}
                   className={"nav-item recent-session-item " + (r.id === activeRecentSessionId ? "active" : "")}
                   onClick={function () {
                            onSelectSession && onSelectSession(r.id);
                          }}
                   title={r.title}>
                <span className={"sa-dot " + r.status} style={{ marginTop: 0, width: 6, height: 6, flexShrink: 0 }}></span>
                <span style={{
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  fontSize: 14, color: "inherit"
                }}>{r.title}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="sidebar-footer">
        <div className="avatar">{DATA.user.initials}</div>
        <div className="who">
          {DATA.user.name}
          <small>@{DATA.user.handle} · {DATA.appVersion || "—"}</small>
        </div>
        <button className="windowbar-btn" type="button" title={t("nav.settings")} style={{ marginLeft: "auto" }} onClick={() => setPage("settings")}>
          <svg width="21" height="21" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="9" cy="9" r="4" />
            <rect x="7.5" y="2.5" width="3" height="2.5" rx="0.5" />
            <rect x="7.5" y="13" width="3" height="2.5" rx="0.5" />
            <rect x="2.5" y="7.5" width="2.5" height="3" rx="0.5" />
            <rect x="13" y="7.5" width="2.5" height="3" rx="0.5" />
            <rect x="7.5" y="2.5" width="3" height="2.5" rx="0.5" transform="rotate(45 9 9)" />
            <rect x="7.5" y="2.5" width="3" height="2.5" rx="0.5" transform="rotate(135 9 9)" />
            <rect x="7.5" y="2.5" width="3" height="2.5" rx="0.5" transform="rotate(225 9 9)" />
            <rect x="7.5" y="2.5" width="3" height="2.5" rx="0.5" transform="rotate(315 9 9)" />
            <circle cx="9" cy="9" r="2" />
          </svg>
        </button>
      </div>
    </div>
  );
}

function SkillsRail({ onOpenPage }) {
  const dv = useDataVersion();
  const [skills, setSkills] = useStateApp(() => (DATA.skills || []).map((s) => ({ ...s })));
  useEffectApp(() => {
    setSkills((DATA.skills || []).map((s) => ({ ...s })));
  }, [dv]);
  const { t: skT } = useI18n();
  const enabledCount = skills.filter((s) => s.enabled).length;
  return (
    <div className="skills-rail">
      <div className="skills-meta">
        <span>{skT("nav.enabledCount", { n: enabledCount, m: skills.length })}</span>
      </div>
      <div className="skills-list">
        {skills.map((s) => (
          <div key={s.id}
               className={"skill-item " + (s.enabled ? "on" : "off")}
               onClick={onOpenPage}
               title={s.desc}>
            <span className="skill-check" aria-hidden="true">
              {s.enabled ? (
                <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M1.6 5.4 L4 7.8 L8.4 2.2" />
                </svg>
              ) : null}
            </span>
            <span className="skill-name">{s.name}</span>
            {s.hotkey && <span className="skill-hotkey">/{s.hotkey.toLowerCase()}</span>}
          </div>
        ))}
        {skills.length === 0 && (
          <div className="skills-rail-empty" onClick={onOpenPage}>
            <span>{skT("skills.empty")}</span>
          </div>
        )}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
