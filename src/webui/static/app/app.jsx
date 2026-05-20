// Cyrene — app shell + page router
const { useState: useStateApp, useEffect: useEffectApp, useMemo: useMemoApp } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "system",
  "accent": "#5ec59e",
  "density": "cozy",
  "textSize": "default",
  "orientation": "horizontal",
  "showLegend": true,
  "animatePulse": true
}/*EDITMODE-END*/;

const ACCENT_PRESETS = {
  dark:  ["#4fd1a0", "#6dbde0", "#b8a2e0", "#e8ae5c", "#e87070"],
  light: ["#2da873", "#3b90c8", "#7858b0", "#c88520", "#d04848"],
};

function readStoredUiPage() {
  try {
    var page = localStorage.getItem("cyrene-ui-page");
    return page || "chat";
  } catch (e) {
    return "chat";
  }
}

function readStoredSessionId() {
  try {
    return localStorage.getItem("cyrene-ui-session-id");
  } catch (e) {
    return null;
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
    base_url: onboarding.llm?.baseUrl || "https://api.deepseek.com/v1",
    model: onboarding.llm?.model || "deepseek-chat",
  });
  const [mode, setMode] = useStateApp(onboarding.personality?.mode || "name");
  const [personalityName, setPersonalityName] = useStateApp(onboarding.personality?.label || "");
  const [customSoul, setCustomSoul] = useStateApp(onboarding.personality?.currentContent || "");

  React.useEffect(function () {
    setStep(onboarding.activeStep || "done");
    setLlmForm({
      api_key: "",
      base_url: onboarding.llm?.baseUrl || "https://api.deepseek.com/v1",
      model: onboarding.llm?.model || "deepseek-chat",
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
            <div className="setup-kicker">{t("setup.kicker")}</div>
          </div>
        </div>
        <button className="theme-toggle-btn" title={theme === "dark" ? t("topbar.switchToLight") : t("topbar.switchToDark")} onClick={onToggleTheme}>
          <span className="theme-toggle-icon">{theme === "system" ? "🖥" : theme === "dark" ? "☀" : "☾"}</span>
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
  const [selectedSessionId, setSelectedSessionId] = useStateApp(readStoredSessionId);
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  const activeSession = useMemoApp(function () {
    return (selectedSessionId
      ? DATA.sessions.find(function (session) { return session.id === selectedSessionId; })
      : null) || DATA.sessions[0] || null;
  }, [selectedSessionId, DATA.sessions]);

  function selectSession(id) {
    setSelectedSessionId(id || null);
  }

  function resolveActualTheme(mode) {
    if (mode === "system") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return mode;
  }

  const actualTheme = React.useMemo(function () {
    return resolveActualTheme(t.theme);
  }, [t.theme]);

  useEffectApp(function () {
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
    delete document.documentElement.dataset.booting;
  }, [t.theme, t.accent, t.density, t.textSize, t.animatePulse, t.showLegend, actualTheme]);

  useEffectApp(function () {
    if (t.theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    function onChange() { setTweak("theme", "system"); }
    mq.addEventListener("change", onChange);
    return function () { mq.removeEventListener("change", onChange); };
  }, [t.theme]);

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
    if (selectedSessionId && !DATA.sessions.some(function (session) { return session.id === selectedSessionId; })) {
      setSelectedSessionId(null);
    }
  }, [selectedSessionId, DATA.sessions]);

  useEffectApp(function () {
    window.__selectedSessionId = activeSession ? activeSession.id : null;
    window.selectUiSession = selectSession;
    return function () {
      delete window.__selectedSessionId;
      delete window.selectUiSession;
    };
  }, [activeSession]);

  const needsOnboarding = !!(DATA.onboarding && DATA.onboarding.needsOnboarding);

  if (needsOnboarding) {
    return <SetupWizard theme={t.theme} onToggleTheme={toggleTheme} />;
  }

  return (
    <div className="app" data-screen-label={"Cyrene · " + page}>
      <Sidebar
        page={page}
        setPage={setPage}
        selectedSessionId={activeSession ? activeSession.id : null}
        onSelectSession={selectSession}
      />
      <div className="page">
        <Topbar
          page={page}
          theme={t.theme}
          onToggleTheme={toggleTheme}
          activeSession={activeSession}
        />
        {page === "chat"     && <ChatPage selectedSessionId={activeSession ? activeSession.id : null} onSelectSession={selectSession} />}
        {page === "agents"   && <AgentsPage orientation={t.orientation} selectedSessionId={activeSession ? activeSession.id : null} />}
        {page === "sessions" && <SessionsPage
                                  selectedSessionId={activeSession ? activeSession.id : null}
                                  onSelectSession={selectSession}
                                  onOpenAgents={(sessionId) => {
                                    selectSession(sessionId);
                                    setPage("agents");
                                  }} />}
        {page === "skills"   && <SkillsPage />}
        {page === "memory"   && <MemoryPage />}
        {page === "status"   && <StatusPage />}
        {page === "settings" && (
          <SettingsPage
            tweaks={t}
            setTweak={setTweak}
            actualTheme={actualTheme}
            accentPresets={ACCENT_PRESETS[actualTheme] || []}
          />
        )}
      </div>

    </div>
  );
}

function Sidebar({ page, setPage, selectedSessionId, onSelectSession }) {
  useDataVersion();
  const { t } = useI18n();
  const [skillsOpen, setSkillsOpen] = useStateApp(true);
  const sessionCount = (DATA.sessions || []).length;
  const activeRecentSessionId = selectedSessionId || DATA.sessions[0]?.id || null;
  const items = [
    { id: "chat",     label: t("nav.chat"),     icon: "▸", key: "1" },
    { id: "agents",   label: t("nav.agentFlow"),   icon: "⌘", key: "2" },
    { id: "sessions", label: t("nav.sessions"), icon: "≡", key: "3", badge: sessionCount > 0 ? String(sessionCount) : null },
    { id: "skills",   label: t("nav.skills"),   icon: "✸", key: "4" },
    { id: "memory",   label: t("nav.memory"),   icon: "▤", key: "5" },
    { id: "status",   label: t("nav.status"),   icon: "◉", key: "6" },
    { id: "settings", label: t("nav.settings"), icon: "✱", key: "7" },
  ];
  const brandName = (DATA.assistantName || "CYRENE").toUpperCase();
  return (
    <div className="sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark"></div>
        <div className="brand-name">{brandName}</div>
        <div className="brand-version">{DATA.appVersion || "v0.1.9"}</div>
      </div>

      <div className="nav-section">{t("nav.workspace")}</div>
      <div className="nav" style={{ paddingTop: 0 }}>
        {items.map((it) => (
          <div key={it.id}
               className={"nav-item " + (page === it.id ? "active" : "")}
               onClick={() => setPage(it.id)}>
            <span style={{ color: "var(--text-4)", fontFamily: "var(--mono)", width: 14, textAlign: "center" }}>
              {it.icon}
            </span>
            <span>{it.label}</span>
            {it.badge && <span className="nav-badge">{it.badge}</span>}
            {!it.badge && <span className="nav-key">⌘{it.key}</span>}
          </div>
        ))}
      </div>

      <div className="nav-section nav-section-collapsible"
           onClick={() => setSkillsOpen(!skillsOpen)}>
        <span className="nav-section-chevron">{skillsOpen ? "▾" : "▸"}</span>
        <span>Skills</span>
        <span className="nav-section-link"
              onClick={(e) => { e.stopPropagation(); setPage("skills"); }}>
          {t("nav.manage")}
        </span>
      </div>
      {skillsOpen && <SkillsRail onOpenPage={() => setPage("skills")} />}

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
      <div className="nav" style={{ paddingTop: 0 }}>
        {DATA.sessions.slice(0, 4).map((r) => (
          <div key={r.id}
               className={"nav-item " + (r.id === activeRecentSessionId ? "active" : "")}
               onClick={function () {
                        onSelectSession && onSelectSession(r.id);
                      }}
               title={r.title}>
            <span className={"sa-dot " + r.status} style={{ marginTop: 0, width: 6, height: 6, flexShrink: 0 }}></span>
            <span style={{
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              fontSize: 14, color: "var(--text-2)"
            }}>{r.title}</span>
          </div>
        ))}
      </div>

      <div className="sidebar-footer">
        <div className="avatar">{DATA.user.initials}</div>
        <div className="who">
          {DATA.user.name}
          <small>@{DATA.user.handle} · {DATA.appVersion || "v0.1.9"}</small>
        </div>
        <button className="iconbtn" title={t("nav.account")}>▾</button>
      </div>
    </div>
  );
}

function SkillsRail({ onOpenPage }) {
  const dv = useDataVersion();
  const [skills, setSkills] = useStateApp(() => (DATA.skills || []).filter((s) => s.installed).map((s) => ({ ...s })));
  // Re-seed when DATA.skills arrives from backend.
  useEffectApp(() => {
    setSkills((DATA.skills || []).filter((s) => s.installed).map((s) => ({ ...s })));
  }, [dv]);
  function toggle(id) {
    setSkills((arr) => arr.map((s) => s.id === id ? { ...s, enabled: !s.enabled } : s));
  }
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
               onClick={() => toggle(s.id)}
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
      </div>
    </div>
  );
}

function Topbar({ page, theme, onToggleTheme, activeSession }) {
  useDataVersion();
  const { t } = useI18n();
  const session = activeSession || { title: "—", subagents: [] };
  const runningSubagents = (session.subagents || []).filter((s) => s.status === "running").length;
  const status = session.status === "err" ? "error" : (session.status || "idle");
  const title =
    page === "chat" ? <>{t("topbar.chat")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "agents" ? <>{t("topbar.agentFlow")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "sessions" ? <>{t("topbar.sessions")}<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "skills" ? <>{t("topbar.skills")}<span className="crumb-sep">/</span><b>{t("topbar.library")}</b></> :
    page === "memory" ? <>{t("topbar.memory")}<span className="crumb-sep">/</span><b>{t("topbar.pipeline")}</b></> :
    page === "status" ? <>{t("topbar.status")}<span className="crumb-sep">/</span><b>{t("topbar.overview")}</b></> :
    <>{t("topbar.settings")}<span className="crumb-sep">/</span><b>{t("topbar.workspace")}</b></>;

  return (
    <div className="topbar">
      <span className="topbar-title">{title}</span>
      <div className="topbar-right">
        <span className="statlight">
          <span className={"dot " + status}></span> {t("topbar.status." + status)}
        </span>
        {runningSubagents > 0 && (
          <span className="statlight">
            <span className="dot warn"></span> {t("topbar.subagents", { n: runningSubagents, pl: runningSubagents > 1 ? "s" : "" })}
          </span>
        )}
        <span style={{ width: 1, height: 18, background: "var(--line)", margin: "0 4px" }}></span>
        <button className="theme-toggle-btn" title={theme === "dark" ? t("topbar.switchToLight") : t("topbar.switchToDark")}
                onClick={onToggleTheme}>
          <span className="theme-toggle-icon">{theme === "system" ? "🖥" : theme === "dark" ? "☀" : "☾"}</span>
          <span>{theme === "system" ? t("settings.system") : theme === "dark" ? t("settings.light") : t("settings.dark")}</span>
        </button>
        <button className="iconbtn" title={t("topbar.search")}>
          <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
          </svg>
        </button>
        <button className="iconbtn" title={t("topbar.pause")}>
          <svg width="11" height="11" viewBox="0 0 20 20" fill="currentColor">
            <rect x="5" y="4" width="3" height="12" rx="0.5" />
            <rect x="12" y="4" width="3" height="12" rx="0.5" />
          </svg>
        </button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
