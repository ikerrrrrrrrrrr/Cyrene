// Cyrene — app shell + page router
const { useState: useStateApp, useEffect: useEffectApp, useMemo: useMemoApp } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "accent": "#8fb8a6",
  "density": "cozy",
  "textSize": "default",
  "orientation": "horizontal",
  "showLegend": true,
  "animatePulse": true
}/*EDITMODE-END*/;

const ACCENT_PRESETS = {
  dark:  ["#8fb8a6", "#7faecf", "#a896c4", "#d4a373", "#c97878"],
  light: ["#4a8a72", "#4a7fa8", "#7a5fa0", "#a8754a", "#b35858"],
};

function App() {
  useDataVersion();
  const [page, setPage] = useStateApp("chat");
  const [selectedSessionId, setSelectedSessionId] = useStateApp(null);
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  const activeSession = useMemoApp(function () {
    return (selectedSessionId
      ? DATA.sessions.find(function (session) { return session.id === selectedSessionId; })
      : null) || DATA.sessions[0] || null;
  }, [selectedSessionId, DATA.sessions]);

  function selectSession(id) {
    setSelectedSessionId(id || null);
  }

  useEffectApp(() => {
    document.documentElement.dataset.theme = t.theme;
    document.documentElement.style.setProperty("--accent", t.accent);
    const m = t.accent.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      const r = parseInt(m[1].slice(0,2),16), g = parseInt(m[1].slice(2,4),16), b = parseInt(m[1].slice(4,6),16);
      document.documentElement.style.setProperty("--accent-faint", `rgba(${r},${g},${b},0.08)`);
      document.documentElement.style.setProperty("--accent-dim", `rgba(${r},${g},${b},0.35)`);
      // pick readable text color on accent
      const lum = (0.299*r + 0.587*g + 0.114*b) / 255;
      document.documentElement.style.setProperty("--accent-text", lum > 0.55 ? "#0d1612" : "#ffffff");
    }
    document.documentElement.dataset.density = t.density;
    document.documentElement.dataset.textSize = t.textSize || "default";
    document.documentElement.dataset.animPulse = t.animatePulse ? "on" : "off";
    document.documentElement.dataset.legend = t.showLegend ? "on" : "off";
  }, [t.theme, t.accent, t.density, t.textSize, t.animatePulse, t.showLegend]);

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

  function toggleTheme() {
    const next = t.theme === "dark" ? "light" : "dark";
    const presetIndex = (ACCENT_PRESETS[t.theme] || []).indexOf(t.accent);
    const nextAccent =
      presetIndex >= 0 ? ACCENT_PRESETS[next][presetIndex] : t.accent;
    setTweak({ theme: next, accent: nextAccent });
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
        {page === "settings" && <SettingsPage tweaks={t} setTweak={setTweak} />}
      </div>

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme" />
        <TweakRadio label="Mode" value={t.theme}
                    options={["light", "dark"]}
                    onChange={(v) => {
                      const i = (ACCENT_PRESETS[t.theme] || []).indexOf(t.accent);
                      const a = i >= 0 ? ACCENT_PRESETS[v][i] : t.accent;
                      setTweak({ theme: v, accent: a });
                    }} />
        <TweakColor label="Accent" value={t.accent}
                    options={ACCENT_PRESETS[t.theme]}
                    onChange={(v) => setTweak("accent", v)} />
        <TweakSection label="Display" />
        <TweakRadio label="Density" value={t.density}
                    options={["cozy", "compact"]}
                    onChange={(v) => setTweak("density", v)} />
        <TweakRadio label="Text size" value={t.textSize}
                    options={["default", "large"]}
                    onChange={(v) => setTweak("textSize", v)} />
        <TweakRadio label="Flowchart" value={t.orientation}
                    options={["horizontal", "vertical"]}
                    onChange={(v) => setTweak("orientation", v)} />
        <TweakToggle label="Canvas legend" value={t.showLegend}
                     onChange={(v) => setTweak("showLegend", v)} />
        <TweakToggle label="Pulse animation" value={t.animatePulse}
                     onChange={(v) => setTweak("animatePulse", v)} />
      </TweaksPanel>
    </div>
  );
}

function Sidebar({ page, setPage, selectedSessionId, onSelectSession }) {
  useDataVersion();
  const [skillsOpen, setSkillsOpen] = useStateApp(true);
  const sessionCount = (DATA.sessions || []).length;
  const activeRecentSessionId = selectedSessionId || DATA.sessions[0]?.id || null;
  const items = [
    { id: "chat",     label: "Chat",     icon: "▸", key: "1" },
    { id: "agents",   label: "Agents",   icon: "⌘", key: "2" },
    { id: "sessions", label: "Sessions", icon: "≡", key: "3", badge: sessionCount > 0 ? String(sessionCount) : null },
    { id: "skills",   label: "Skills",   icon: "✸", key: "4" },
    { id: "memory",   label: "Memory",   icon: "▤", key: "5" },
    { id: "status",   label: "Status",   icon: "◉", key: "6" },
    { id: "settings", label: "Settings", icon: "✱", key: "7" },
  ];
  const brandName = (DATA.assistantName || "CYRENE").toUpperCase();
  return (
    <div className="sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark"></div>
        <div className="brand-name">{brandName}</div>
        <div className="brand-version">v0.1.0</div>
      </div>

      <div className="nav-section">Workspace</div>
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
          manage →
        </span>
      </div>
      {skillsOpen && <SkillsRail onOpenPage={() => setPage("skills")} />}

      <div className="nav-section nav-section-collapsible" style={{ cursor: "default" }}>
        <span>Recent sessions</span>
        <span className="nav-section-link"
              title="Start a new session"
              onClick={async (e) => {
                e.stopPropagation();
                if (!confirm("Start a new session? Current conversation will be compressed.")) return;
                try {
                  const r = await fetch("/api/sessions", { method: "POST" });
                  if (!r.ok) throw new Error("HTTP " + r.status);
                  const data = await r.json();
                  if (data.sessions) { DATA.sessions = data.sessions; window.bumpData && window.bumpData(); }
                } catch (err) { alert("Failed: " + err.message); }
              }}>
          + new
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
              fontSize: 12, color: "var(--text-2)"
            }}>{r.title}</span>
          </div>
        ))}
      </div>

      <div className="sidebar-footer">
        <div className="avatar">{DATA.user.initials}</div>
        <div className="who">
          {DATA.user.name}
          <small>@{DATA.user.handle}</small>
        </div>
        <button className="iconbtn" title="Account">▾</button>
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
  const enabledCount = skills.filter((s) => s.enabled).length;
  return (
    <div className="skills-rail">
      <div className="skills-meta">
        <span>{enabledCount} of {skills.length} enabled</span>
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
  const session = activeSession || { title: "—", subagents: [] };
  const runningSubagents = (session.subagents || []).filter((s) => s.status === "running").length;
  const title =
    page === "chat" ? <>Chat<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "agents" ? <>Agents<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "sessions" ? <>Sessions<span className="crumb-sep">/</span><b>{session.title}</b></> :
    page === "skills" ? <>Skills<span className="crumb-sep">/</span><b>library</b></> :
    page === "memory" ? <>Memory<span className="crumb-sep">/</span><b>pipeline</b></> :
    page === "status" ? <>Status<span className="crumb-sep">/</span><b>overview</b></> :
    <>Settings<span className="crumb-sep">/</span><b>workspace</b></>;

  return (
    <div className="topbar">
      <span className="topbar-title">{title}</span>
      <div className="topbar-right">
        <span className="statlight">
          <span className="dot"></span> orchestrator · running
        </span>
        {runningSubagents > 0 && (
          <span className="statlight">
            <span className="dot warn"></span> {runningSubagents} subagent{runningSubagents > 1 ? "s" : ""}
          </span>
        )}
        <span style={{ width: 1, height: 18, background: "var(--line)", margin: "0 4px" }}></span>
        <button className="iconbtn" title="Search">
          <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="9" cy="9" r="5" /><path d="M13 13 L17 17" />
          </svg>
        </button>
        <button className="iconbtn" title={theme === "dark" ? "Switch to light" : "Switch to dark"}
                onClick={onToggleTheme}>
          {theme === "dark" ? (
            // sun
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
              <circle cx="10" cy="10" r="3.2" />
              <path d="M10 2.5v1.6 M10 15.9v1.6 M2.5 10h1.6 M15.9 10h1.6 M4.7 4.7l1.1 1.1 M14.2 14.2l1.1 1.1 M4.7 15.3l1.1-1.1 M14.2 5.8l1.1-1.1" />
            </svg>
          ) : (
            // moon
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
              <path d="M16.5 12.2A6.5 6.5 0 0 1 7.8 3.5a6.5 6.5 0 1 0 8.7 8.7Z" />
            </svg>
          )}
        </button>
        <button className="iconbtn" title="Pause">
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
