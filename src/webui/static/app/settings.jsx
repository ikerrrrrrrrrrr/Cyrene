// Settings page
const { useState: useStateSet } = React;

function SettingsPage({ tweaks, setTweak }) {
  useDataVersion();
  const [section, setSection] = useStateSet("general");
  const [model, setModel] = useStateSet("current");
  const [config, setConfig] = useStateSet({
    model: "—", base_url: "—", assistant_name: "—",
    soul_path: "—", workspace_dir: "—", soul_content: "",
  });
  const [soulDraft, setSoulDraft] = useStateSet("");
  const [soulStatus, setSoulStatus] = useStateSet("");
  const [toggles, setToggles] = useStateSet({
    autoSpawn: true,
    sandboxedShell: true,
    networkAllowlist: false,
    redactSecrets: true,
    streamThinking: true,
    desktopNotif: false,
  });

  function t(k) { setToggles({ ...toggles, [k]: !toggles[k] }); }

  React.useEffect(() => {
    fetch("/api/settings/config").then((r) => r.json()).then((c) => {
      setConfig(c);
      setSoulDraft(c.soul_content || "");
    }).catch(() => {});
  }, []);

  async function saveSoul() {
    setSoulStatus("saving…");
    try {
      const r = await fetch("/api/settings/soul", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: soulDraft }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setSoulStatus("saved ✓");
      setTimeout(() => setSoulStatus(""), 1500);
    } catch (e) {
      setSoulStatus("error: " + e.message);
    }
  }

  async function clearSession() {
    if (!confirm("Clear the current conversation session?")) return;
    await fetch("/api/chat/clear", { method: "POST" });
    if (window.refreshSessions) window.refreshSessions();
    alert("Session cleared.");
  }

  return (
    <div className="settings-layout">
      <div className="settings-nav">
        <div className="nav-section">Settings</div>
        {DATA.settings.sections.map((s) => (
          <div key={s.id}
               className={"nav-item " + (section === s.id ? "active" : "")}
               onClick={() => setSection(s.id)}>
            {s.label}
          </div>
        ))}
      </div>

      <div className="settings-content">
        {section === "general" && (
          <>
            <h2>General</h2>
            <p className="subtitle">Workspace identity and persona (SOUL.md).</p>
            <div className="field">
              <div className="label">Assistant name<small>From ASSISTANT_NAME env var. Used in chat + sidebar.</small></div>
              <input className="input" value={config.assistant_name} readOnly />
            </div>
            <div className="field">
              <div className="label">Workspace directory<small>Where SOUL.md, conversations/, and runtime files live.</small></div>
              <input className="input mono" value={config.workspace_dir} readOnly />
            </div>
            <div className="field" style={{ display: "block" }}>
              <div className="label" style={{ marginBottom: 8 }}>
                SOUL.md<small>Long-term persona + identity. Steward agent updates this every 30 min.</small>
              </div>
              <textarea
                className="input mono"
                value={soulDraft}
                onChange={(e) => setSoulDraft(e.target.value)}
                style={{ width: "100%", minHeight: 320, fontSize: 12, lineHeight: 1.5 }}
              />
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
                <button className="btn primary" onClick={saveSoul}>save SOUL.md</button>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>
                  {soulStatus || config.soul_path}
                </span>
              </div>
            </div>
            <div className="field">
              <div className="label">Stream reasoning to chat<small>Show the agent's thinking inline as it works.</small></div>
              <div className={"toggle " + (toggles.streamThinking ? "on" : "")} onClick={() => t("streamThinking")}></div>
            </div>
          </>
        )}

        {section === "models" && (
          <>
            <h2>Models</h2>
            <p className="subtitle">Pick the default model for new runs. Subagents inherit unless overridden.</p>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {DATA.settings.models.map((m) => (
                <div key={m.id}
                     className={"model-card " + (model === m.id ? "active" : "")}
                     onClick={() => setModel(m.id)}>
                  <div className="model-radio"></div>
                  <div style={{ minWidth: 0 }}>
                    <div className="model-name">{m.name}</div>
                    <div className="model-desc">{m.desc}</div>
                  </div>
                  <div className="model-meta">
                    <div>{m.ctx}</div>
                    <div style={{ color: "var(--text-3)" }}>{m.price} <span style={{ color: "var(--text-4)" }}>/ M tok</span></div>
                  </div>
                </div>
              ))}
            </div>
            <div className="field" style={{ marginTop: 8 }}>
              <div className="label">Temperature<small>Lower = more deterministic. Default 0.2.</small></div>
              <input className="input" defaultValue="0.2" style={{ maxWidth: 120 }} />
            </div>
            <div className="field">
              <div className="label">Max output tokens</div>
              <input className="input" defaultValue="4096" style={{ maxWidth: 120 }} />
            </div>
          </>
        )}

        {section === "agents" && (
          <>
            <h2>Agents</h2>
            <p className="subtitle">How the orchestrator plans, spawns, and tears down workers.</p>
            <div className="field">
              <div className="label">Flowchart orientation<small>Direction nodes flow on the Agents canvas.</small></div>
              <div className="seg">
                <button
                  className={"seg-btn " + (tweaks && tweaks.orientation === "horizontal" ? "active" : "")}
                  onClick={() => setTweak && setTweak("orientation", "horizontal")}>
                  <svg width="22" height="14" viewBox="0 0 22 14" fill="none" stroke="currentColor" strokeWidth="1.4">
                    <rect x="1" y="4" width="5" height="6" rx="1" />
                    <rect x="9" y="4" width="5" height="6" rx="1" />
                    <rect x="17" y="4" width="4" height="6" rx="1" />
                    <path d="M6 7 L9 7 M14 7 L17 7" />
                  </svg>
                  horizontal
                </button>
                <button
                  className={"seg-btn " + (tweaks && tweaks.orientation === "vertical" ? "active" : "")}
                  onClick={() => setTweak && setTweak("orientation", "vertical")}>
                  <svg width="14" height="22" viewBox="0 0 14 22" fill="none" stroke="currentColor" strokeWidth="1.4">
                    <rect x="4" y="1" width="6" height="5" rx="1" />
                    <rect x="4" y="9" width="6" height="5" rx="1" />
                    <rect x="4" y="17" width="6" height="4" rx="1" />
                    <path d="M7 6 L7 9 M7 14 L7 17" />
                  </svg>
                  vertical
                </button>
              </div>
            </div>
            <div className="field">
              <div className="label">Auto-spawn subagents<small>Let the main agent fan out parallelizable work.</small></div>
              <div className={"toggle " + (toggles.autoSpawn ? "on" : "")} onClick={() => t("autoSpawn")}></div>
            </div>
            <div className="field">
              <div className="label">Max concurrent subagents<small>Hard cap on parallel workers per run.</small></div>
              <select className="select" style={{ maxWidth: 160 }} defaultValue="4">
                <option>1</option><option>2</option><option>4</option><option>8</option><option>16</option>
              </select>
            </div>
            <div className="field">
              <div className="label">Subagent token budget<small>Per-subagent context cap.</small></div>
              <input className="input" defaultValue="32000" style={{ maxWidth: 160 }} />
            </div>
            <div className="field">
              <div className="label">Spawn policy<small>When the main agent is allowed to delegate.</small></div>
              <select className="select" style={{ maxWidth: 240 }} defaultValue="conservative">
                <option value="aggressive">aggressive — delegate often</option>
                <option value="conservative">conservative — only obvious parallelism</option>
                <option value="off">off — single agent only</option>
              </select>
            </div>
          </>
        )}

        {section === "tools" && (
          <>
            <h2>Tools</h2>
            <p className="subtitle">Toggle what the agent is allowed to call.</p>
            {[
              { k: "shell", n: "shell.exec", d: "Run shell commands in the sandbox." },
              { k: "fs",    n: "fs.read / fs.write", d: "Read and write files in the working directory." },
              { k: "git",   n: "git.*", d: "Inspect history, branches, diffs (read-only by default)." },
              { k: "web",   n: "web.fetch", d: "Fetch HTTP(S) — see allowlist below." },
              { k: "code",  n: "code.search", d: "Ripgrep-style search across the project." },
              { k: "ai",    n: "agent.spawn", d: "Spawn subagents." },
            ].map((tl) => (
              <div className="field" key={tl.k}>
                <div className="label">{tl.n}<small>{tl.d}</small></div>
                <div className={"toggle " + (toggles[tl.k] !== false ? "on" : "")}
                     onClick={() => setToggles({ ...toggles, [tl.k]: toggles[tl.k] === false ? true : false })}></div>
              </div>
            ))}
            <div className="field">
              <div className="label">Sandbox shell<small>Run shell.exec inside an isolated container.</small></div>
              <div className={"toggle " + (toggles.sandboxedShell ? "on" : "")} onClick={() => t("sandboxedShell")}></div>
            </div>
            <div className="field">
              <div className="label">Network allowlist<small>If on, web.fetch is restricted to listed domains.</small></div>
              <div className={"toggle " + (toggles.networkAllowlist ? "on" : "")} onClick={() => t("networkAllowlist")}></div>
            </div>
          </>
        )}

        {section === "keys" && (
          <>
            <h2>API keys</h2>
            <p className="subtitle">Edit your .env file directly — keys are not stored by the UI.</p>
            <div className="field">
              <div className="label">LLM endpoint<small>OPENAI_BASE_URL — set to any OpenAI-compatible API (DeepSeek, OpenAI, LMStudio).</small></div>
              <input className="input mono" value={config.base_url} readOnly />
            </div>
            <div className="field">
              <div className="label">Active model<small>OPENAI_MODEL.</small></div>
              <input className="input mono" value={config.model} readOnly />
            </div>
            <div className="field">
              <div className="label">OPENAI_API_KEY<small>Stored in .env at project root. Edit there.</small></div>
              <input className="input mono" placeholder="(hidden — check .env)" readOnly />
            </div>
            <div className="field">
              <div className="label">Telegram bot token<small>Optional. Required only for the Telegram interface.</small></div>
              <input className="input mono" placeholder="(hidden — check .env for TELEGRAM_BOT_TOKEN)" readOnly />
            </div>
            <div className="field">
              <div className="label">Redact secrets from logs<small>Mask API keys + bearer tokens before they hit disk.</small></div>
              <div className={"toggle " + (toggles.redactSecrets ? "on" : "")} onClick={() => t("redactSecrets")}></div>
            </div>
          </>
        )}

        {section === "appearance" && (
          <>
            <h2>Appearance</h2>
            <p className="subtitle">Use the floating Tweaks panel to live-preview theme changes.</p>
            <div className="field">
              <div className="label">Theme</div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.theme === "light" ? "active" : "")}
                        onClick={() => setTweak && setTweak("theme", "light")}>light</button>
                <button className={"seg-btn " + (tweaks && tweaks.theme === "dark" ? "active" : "")}
                        onClick={() => setTweak && setTweak("theme", "dark")}>dark</button>
              </div>
            </div>
            <div className="field">
              <div className="label">Text size<small>Use the larger version for readability.</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "default" ? "active" : "")}
                        onClick={() => setTweak && setTweak("textSize", "default")}>
                  <span style={{ fontSize: 11 }}>A</span> default
                </button>
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "large" ? "active" : "")}
                        onClick={() => setTweak && setTweak("textSize", "large")}>
                  <span style={{ fontSize: 15 }}>A</span> large
                </button>
              </div>
            </div>
            <div className="field">
              <div className="label">Density</div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.density === "cozy" ? "active" : "")}
                        onClick={() => setTweak && setTweak("density", "cozy")}>cozy</button>
                <button className={"seg-btn " + (tweaks && tweaks.density === "compact" ? "active" : "")}
                        onClick={() => setTweak && setTweak("density", "compact")}>compact</button>
              </div>
            </div>
          </>
        )}

        {section === "danger" && (
          <>
            <h2>Danger zone</h2>
            <p className="subtitle">Irreversible. Please be careful.</p>
            <div className="field">
              <div className="label">Clear current session<small>Wipes data/state.json — current conversation context is lost.</small></div>
              <button className="btn danger" onClick={clearSession}>clear session</button>
            </div>
            <div className="field">
              <div className="label">SOUL.md path<small>To reset persona, edit SOUL.md directly under General.</small></div>
              <input className="input mono" value={config.soul_path} readOnly />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

window.SettingsPage = SettingsPage;
