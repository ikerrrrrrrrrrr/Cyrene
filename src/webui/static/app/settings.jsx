// Settings page
const { useState: useStateSet } = React;

function SettingsPage({ tweaks, setTweak }) {
  useDataVersion();
  const [section, setSection] = useStateSet("general");
  const [config, setConfig] = useStateSet({
    model: "—", base_url: "—", assistant_name: "—",
    soul_path: "—", workspace_dir: "—", soul_content: "",
  });
  const [soulDraft, setSoulDraft] = useStateSet("");
  const [soulStatus, setSoulStatus] = useStateSet("");
  const [toggles, setToggles] = useStateSet({
    sandboxedShell: true,
    networkAllowlist: false,
    redactSecrets: true,
    streamThinking: true,
    desktopNotif: false,
  });
  const [searchMode, setSearchMode] = useStateSet("builtin");
  const [searchExternalUrl, setSearchExternalUrl] = useStateSet("");
  const [searchSaved, setSearchSaved] = useStateSet("");
  const [keys, setKeys] = useStateSet({});
  const [keysSaved, setKeysSaved] = useStateSet("");
  const [models, setModels] = useStateSet([]);
  const [activeModel, setActiveModel] = useStateSet("");
  const [baseUrl, setBaseUrl] = useStateSet("");
  const [newModel, setNewModel] = useStateSet({ name: "", desc: "", ctx: "", price: "" });
  const [modelsSaved, setModelsSaved] = useStateSet("");
  const [toolList, setToolList] = useStateSet([]);
  const [toolsSaved, setToolsSaved] = useStateSet("");

  function t(k) { setToggles({ ...toggles, [k]: !toggles[k] }); }

  React.useEffect(() => {
    fetch("/api/settings/config").then((r) => r.json()).then((c) => {
      setConfig(c);
      setSoulDraft(c.soul_content || "");
      if (c.search_mode) setSearchMode(c.search_mode);
      if (c.search_external_url !== undefined) setSearchExternalUrl(c.search_external_url);
    }).catch(() => {});
    fetch("/api/settings/keys").then((r) => r.json()).then((data) => {
      const map = {};
      (data.keys || []).forEach((k) => { map[k.key] = k.value || ""; });
      setKeys(map);
    }).catch(() => {});
    fetch("/api/settings/models").then((r) => r.json()).then((data) => {
      setModels(data.models || []);
      setActiveModel(data.active || "");
      setBaseUrl(data.base_url || "");
    }).catch(() => {});
    fetch("/api/settings/tools").then((r) => r.json()).then((data) => {
      setToolList(data.tools || []);
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

  async function saveSearch() {
    setSearchSaved("saving…");
    try {
      const r = await fetch("/api/settings/search", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ search_mode: searchMode, search_external_url: searchExternalUrl }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setSearchSaved("saved ✓");
      setTimeout(() => setSearchSaved(""), 1500);
    } catch (e) {
      setSearchSaved("error: " + e.message);
    }
  }

  async function saveKeys() {
    setKeysSaved("saving…");
    try {
      const r = await fetch("/api/settings/keys", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(keys),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      setKeysSaved("saved " + (data.updated || []).join(", ") + " ✓");
      setTimeout(() => setKeysSaved(""), 2500);
    } catch (e) {
      setKeysSaved("error: " + e.message);
    }
  }

  async function saveModels() {
    setModelsSaved("saving…");
    try {
      const r = await fetch("/api/settings/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: models, selected: activeModel, base_url: baseUrl }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setModelsSaved("saved ✓");
      setTimeout(() => setModelsSaved(""), 1500);
    } catch (e) {
      setModelsSaved("error: " + e.message);
    }
  }

  function selectModel(id) {
    setActiveModel(id);
    setModels(models.map(function(m) { return { ...m, _active: m.id === id }; }));
  }

  function addModel() {
    var name = (newModel.name || "").trim();
    if (!name) return;
    var id = name.toLowerCase().replace(/\s+/g, "-");
    var added = { id: id, name: name, desc: newModel.desc || "", ctx: newModel.ctx || "—", price: newModel.price || "—" };
    setModels(models.concat(added));
    setActiveModel(id);
    setNewModel({ name: "", desc: "", ctx: "", price: "" });
  }

  function deleteModel(id) {
    if (models.length <= 1) return;
    var next = models.filter(function(m) { return m.id !== id; });
    setModels(next);
    if (activeModel === id) setActiveModel(next[0] ? next[0].id : "");
  }

  async function saveTools() {
    setToolsSaved("saving…");
    try {
      var map = {};
      toolList.forEach(function(tl) { map[tl.name] = tl.enabled; });
      const r = await fetch("/api/settings/tools", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tools: map }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setToolsSaved("saved ✓");
      setTimeout(() => setToolsSaved(""), 1500);
    } catch (e) {
      setToolsSaved("error: " + e.message);
    }
  }

  function toggleTool(name) {
    setToolList(toolList.map(function(tl) {
      return tl.name === name ? { ...tl, enabled: !tl.enabled } : tl;
    }));
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
            <p className="subtitle">Manage available models. Click a model to select it — changes apply to new LLM calls immediately.</p>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {models.map(function(m) {
                var isActive = m.id === activeModel;
                return (
                  <div key={m.id}
                       className={"model-card" + (isActive ? " active" : "")}
                       onClick={function() { selectModel(m.id); }}
                       style={{ cursor: "pointer" }}>
                    <div className="model-radio" style={isActive ? { background: "var(--accent)", borderColor: "var(--accent)" } : {}}></div>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div className="model-name">{m.name}</div>
                      <div className="model-desc">{m.desc}</div>
                    </div>
                    <div className="model-meta">
                      <div>{m.ctx}</div>
                      <div style={{ color: "var(--text-3)" }}>{m.price} <span style={{ color: "var(--text-4)" }}>/ M tok</span></div>
                    </div>
                    <button className="iconbtn"
                            title={"Delete " + m.name}
                            onClick={function(e) { e.stopPropagation(); deleteModel(m.id); }}
                            style={{ marginLeft: 8, color: "var(--text-4)", opacity: models.length <= 1 ? 0.3 : 1 }}
                            disabled={models.length <= 1}>
                      <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                        <path d="M5 5 L15 15 M15 5 L5 15" />
                      </svg>
                    </button>
                  </div>
                );
              })}
            </div>
            <div className="field" style={{ marginTop: 8 }}>
              <div className="label">API endpoint<small>OPENAI_BASE_URL — OpenAI-compatible API base.</small></div>
              <input className="input mono" value={baseUrl}
                     onChange={function(e) { setBaseUrl(e.target.value); }}
                     placeholder="https://api.deepseek.com/v1" style={{ maxWidth: 480 }} />
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
              <button className="btn primary" onClick={saveModels}>save & apply</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{modelsSaved}</span>
            </div>
            <h3 style={{ marginTop: 16, marginBottom: 8, fontSize: 13 }}>Add model</h3>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <input className="input mono" placeholder="name" value={newModel.name}
                     onChange={function(e) { setNewModel({ ...newModel, name: e.target.value }); }}
                     style={{ maxWidth: 180 }} />
              <input className="input mono" placeholder="desc" value={newModel.desc}
                     onChange={function(e) { setNewModel({ ...newModel, desc: e.target.value }); }}
                     style={{ maxWidth: 200 }} />
              <input className="input mono" placeholder="ctx" value={newModel.ctx}
                     onChange={function(e) { setNewModel({ ...newModel, ctx: e.target.value }); }}
                     style={{ maxWidth: 80 }} />
              <input className="input mono" placeholder="price" value={newModel.price}
                     onChange={function(e) { setNewModel({ ...newModel, price: e.target.value }); }}
                     style={{ maxWidth: 100 }} />
              <button className="btn" onClick={addModel}>add</button>
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
            <p className="subtitle">Enable or disable tools the agent can call. Changes take effect on the next agent turn. <b>quit</b> is always enabled.</p>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {toolList.map(function(tl) {
                return (
                  <div className="field" key={tl.name}>
                    <div className="label">
                      <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--accent)" }}>{tl.name}</span>
                      <small>{tl.desc}</small>
                    </div>
                    <div className={"toggle " + (tl.enabled ? "on" : "")}
                         onClick={function() { toggleTool(tl.name); }}></div>
                  </div>
                );
              })}
            </div>
            <div style={{ marginTop: 12 }}>
              <button className="btn primary" onClick={saveTools}>save tools</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)", marginLeft: 8 }}>{toolsSaved}</span>
            </div>
          </>
        )}

        {section === "search" && (
          <>
            <h2>Web Search</h2>
            <p className="subtitle">Choose how the agent searches the web. Save to apply changes.</p>
            <div className="field">
              <div className="label">Search backend<small>Built-in uses SimpleXNG (auto-started, no Docker). External points to your own SearXNG instance. Fallback uses DDG/Bing/Baidu scraping only.</small></div>
              <div className="seg">
                <button
                  className={"seg-btn " + (searchMode === "builtin" ? "active" : "")}
                  onClick={() => setSearchMode("builtin")}>
                  built-in
                </button>
                <button
                  className={"seg-btn " + (searchMode === "external" ? "active" : "")}
                  onClick={() => setSearchMode("external")}>
                  external
                </button>
                <button
                  className={"seg-btn " + (searchMode === "fallback" ? "active" : "")}
                  onClick={() => setSearchMode("fallback")}>
                  fallback only
                </button>
              </div>
            </div>
            {searchMode === "external" && (
              <div className="field">
                <div className="label">External SearXNG URL<small>e.g. http://localhost:8888 or https://search.example.com</small></div>
                <input
                  className="input mono"
                  value={searchExternalUrl}
                  onChange={(e) => setSearchExternalUrl(e.target.value)}
                  placeholder="http://localhost:8888"
                  style={{ maxWidth: 400 }}
                />
              </div>
            )}
            {searchMode === "builtin" && (
              <div className="field">
                <div className="label">Built-in status<small>SimpleXNG auto-starts on port {config.search_port || "8888"}. Make sure <code>pip install simplexng</code> is done.</small></div>
                <input className="input mono" value="Auto-started on launch — no config needed" readOnly style={{ maxWidth: 420 }} />
              </div>
            )}
            {searchMode === "fallback" && (
              <div className="field">
                <div className="label">Fallback engines<small>DuckDuckGo, Bing, and Baidu HTML scraping. Rate-limited and less reliable.</small></div>
                <input className="input mono" value="DDG → Bing → Baidu (no SearXNG)" readOnly style={{ maxWidth: 420 }} />
              </div>
            )}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
              <button className="btn primary" onClick={saveSearch}>save search settings</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{searchSaved}</span>
            </div>
          </>
        )}

        {section === "keys" && (
          <>
            <h2>API keys</h2>
            <p className="subtitle">Edit your .env file from the UI. Changes take effect immediately for LLM calls. Telegram token requires restart.</p>
            <div className="field">
              <div className="label">LLM endpoint<small>OPENAI_BASE_URL — OpenAI-compatible API (DeepSeek, OpenAI, LMStudio).</small></div>
              <input className="input mono" value={keys.OPENAI_BASE_URL || config.base_url || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_BASE_URL: e.target.value })}
                     placeholder="https://api.deepseek.com/v1" style={{ maxWidth: 480 }} />
            </div>
            <div className="field">
              <div className="label">Model name<small>OPENAI_MODEL — e.g. deepseek-chat, claude-sonnet-4-7.</small></div>
              <input className="input mono" value={keys.OPENAI_MODEL || config.model || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_MODEL: e.target.value })}
                     placeholder="deepseek-chat" style={{ maxWidth: 320 }} />
            </div>
            <div className="field">
              <div className="label">API key<small>OPENAI_API_KEY — bearer token for LLM authentication.</small></div>
              <input className="input mono" type="password"
                     value={keys.OPENAI_API_KEY || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_API_KEY: e.target.value })}
                     placeholder="sk-…" style={{ maxWidth: 480 }} />
            </div>
            <div className="field">
              <div className="label">Telegram bot token<small>TELEGRAM_BOT_TOKEN — optional, for Telegram interface. Requires restart to take effect.</small></div>
              <input className="input mono" type="password"
                     value={keys.TELEGRAM_BOT_TOKEN || ""}
                     onChange={(e) => setKeys({ ...keys, TELEGRAM_BOT_TOKEN: e.target.value })}
                     placeholder="(optional)" style={{ maxWidth: 480 }} />
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
              <button className="btn primary" onClick={saveKeys}>save API keys</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{keysSaved}</span>
            </div>
            <div className="field" style={{ marginTop: 16 }}>
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
