// Settings page
const { useState: useStateSet } = React;

function readStoredSettingsSection() {
  try {
    return localStorage.getItem("cyrene-settings-section") || "general";
  } catch (e) {
    return "general";
  }
}

function SettingsPage({ tweaks, setTweak, actualTheme, accentPresets }) {
  useDataVersion();
  const { t, lang, setLang } = useI18n();
  const [section, setSection] = useStateSet(readStoredSettingsSection);
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
  const [mcpServers, setMcpServers] = useStateSet([]);
  const [mcpConfigs, setMcpConfigs] = useStateSet([]);
  const [mcpSaved, setMcpSaved] = useStateSet("");
  const [newMcpServer, setNewMcpServer] = useStateSet({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });

  function toggleKey(k) { setToggles({ ...toggles, [k]: !toggles[k] }); }

  React.useEffect(() => {
    try {
      localStorage.setItem("cyrene-settings-section", section);
    } catch (e) {}
  }, [section]);

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
    fetch("/api/settings/mcp").then((r) => r.json()).then((data) => {
      setMcpServers(data.servers || []);
      setMcpConfigs(data.configs || []);
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

  function addMcpServer() {
    var name = (newMcpServer.name || "").trim();
    if (!name) return;
    var server = {
      name: name,
      transport: newMcpServer.transport || "stdio",
      command: newMcpServer.command || "",
      args: (newMcpServer.args || "").split(" ").filter(Boolean),
      url: newMcpServer.url || "",
      enabled: newMcpServer.enabled !== false,
    };
    setMcpConfigs(mcpConfigs.concat(server));
    setNewMcpServer({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
  }

  function removeMcpServer(name) {
    setMcpConfigs(mcpConfigs.filter(function(s) { return s.name !== name; }));
  }

  function toggleMcpServer(name) {
    setMcpConfigs(mcpConfigs.map(function(s) {
      return s.name === name ? { ...s, enabled: !s.enabled } : s;
    }));
  }

  async function saveMcpServers() {
    setMcpSaved("saving…");
    try {
      const r = await fetch("/api/settings/mcp", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ servers: mcpConfigs }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setMcpSaved("saved ✓");
      // Refresh status
      fetch("/api/settings/mcp").then(function(resp) { return resp.json(); }).then(function(data) {
        setMcpServers(data.servers || []);
        setMcpConfigs(data.configs || []);
      }).catch(function() {});
      setTimeout(function() { setMcpSaved(""); }, 1500);
    } catch (e) {
      setMcpSaved("error: " + e.message);
    }
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
        <div className="nav-section">{t("nav.settings")}</div>
        {DATA.settings.sections.map((s) => {
          const labelKey = "section." + s.id;
          const translated = t(labelKey);
          const label = translated === labelKey ? s.label : translated;
          return (
            <div key={s.id}
                 className={"nav-item " + (section === s.id ? "active" : "")}
                 onClick={() => setSection(s.id)}>
              {label}
            </div>
          );
        })}
      </div>

      <div className="settings-content">
        {section === "general" && (
          <div className="settings-pane">
            <h2>{t("settings.general")}</h2>
            <p className="subtitle">{t("settings.generalSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.assistantName")}<small>{t("settings.assistantNameHint")}</small></div>
              <input className="input" value={config.assistant_name} readOnly />
            </div>
            <div className="field">
              <div className="label">{t("settings.workspaceDir")}<small>{t("settings.workspaceDirHint")}</small></div>
              <input className="input mono" value={config.workspace_dir} readOnly />
            </div>
            <div className="field" style={{ display: "block" }}>
              <div className="label" style={{ marginBottom: 8 }}>
                {t("settings.soulMd")}<small>{t("settings.soulMdHint")}</small>
              </div>
              <textarea
                className="input mono"
                value={soulDraft}
                onChange={(e) => setSoulDraft(e.target.value)}
                style={{ width: "100%", minHeight: 320, fontSize: 12, lineHeight: 1.5 }}
              />
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
                <button className="btn primary" onClick={saveSoul}>{t("settings.saveSoul")}</button>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>
                  {soulStatus || config.soul_path}
                </span>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.streamReasoning")}<small>{t("settings.streamReasoningHint")}</small></div>
              <div className={"toggle " + (toggles.streamThinking ? "on" : "")} onClick={() => toggleKey("streamThinking")}></div>
            </div>
          </div>
        )}

        {section === "models" && (
          <div className="settings-pane">
            <h2>{t("settings.models")}</h2>
            <p className="subtitle">{t("settings.modelsSubtitle")}</p>
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
              <div className="label">{t("settings.apiEndpoint")}<small>{t("settings.apiEndpointHint")}</small></div>
              <input className="input mono" value={baseUrl}
                     onChange={function(e) { setBaseUrl(e.target.value); }}
                     placeholder="https://api.deepseek.com/v1" style={{ maxWidth: 480 }} />
            </div>
            <div className="settings-actions">
              <button className="btn primary" onClick={saveModels}>{t("settings.saveApply")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{modelsSaved}</span>
            </div>
            <h3 style={{ marginTop: 16, marginBottom: 8, fontSize: 13 }}>{t("settings.addModel")}</h3>
            <div className="inline-form-grid">
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
              <button className="btn" onClick={addModel}>{t("settings.add")}</button>
            </div>
          </div>
        )}

        {section === "agents" && (
          <div className="settings-pane">
            <h2>{t("settings.agents")}</h2>
            <p className="subtitle">{t("settings.agentsSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.spawnPolicy")}<small>{t("settings.spawnPolicyHint")}</small></div>
              <select className="select" style={{ maxWidth: 240 }} defaultValue="conservative">
                <option value="aggressive">{t("settings.aggressive")}</option>
                <option value="conservative">{t("settings.conservative")}</option>
                <option value="off">{t("settings.off")}</option>
              </select>
            </div>
          </div>
        )}

        {section === "tools" && (
          <div className="settings-pane">
            <h2>{t("settings.tools")}</h2>
            <p className="subtitle">{t("settings.toolsSubtitle")}</p>
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
            <div className="settings-actions">
              <button className="btn primary" onClick={saveTools}>{t("settings.saveTools")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)", marginLeft: 8 }}>{toolsSaved}</span>
            </div>
          </div>
        )}

        {section === "search" && (
          <div className="settings-pane">
            <h2>{t("settings.webSearch")}</h2>
            <p className="subtitle">{t("settings.webSearchSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.searchBackend")}<small>{t("settings.searchBackendHint")}</small></div>
              <div className="seg">
                <button
                  className={"seg-btn " + (searchMode === "builtin" ? "active" : "")}
                  onClick={() => setSearchMode("builtin")}>
                  {t("settings.builtin")}
                </button>
                <button
                  className={"seg-btn " + (searchMode === "external" ? "active" : "")}
                  onClick={() => setSearchMode("external")}>
                  {t("settings.external")}
                </button>
                <button
                  className={"seg-btn " + (searchMode === "fallback" ? "active" : "")}
                  onClick={() => setSearchMode("fallback")}>
                  {t("settings.fallbackOnly")}
                </button>
              </div>
            </div>
            {searchMode === "external" && (
              <div className="field">
                <div className="label">{t("settings.externalUrl")}<small>{t("settings.externalUrlHint")}</small></div>
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
                <div className="label">{t("settings.builtinStatus")}<small>{t("settings.builtinStatusHint", { port: config.search_port || "8888" })}</small></div>
                <input className="input mono" value={t("settings.autoStarted")} readOnly style={{ maxWidth: 420 }} />
              </div>
            )}
            {searchMode === "fallback" && (
              <div className="field">
                <div className="label">{t("settings.fallbackEngines")}<small>{t("settings.fallbackEnginesHint")}</small></div>
                <input className="input mono" value={t("settings.fallbackDesc")} readOnly style={{ maxWidth: 420 }} />
              </div>
            )}
            <div className="settings-actions">
              <button className="btn primary" onClick={saveSearch}>{t("settings.saveSearch")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{searchSaved}</span>
            </div>
          </div>
        )}

        {section === "mcp" && (
          <div className="settings-pane">
            <h2>{t("settings.mcpServers")}</h2>
            <p className="subtitle">{t("settings.mcpSubtitle")}</p>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {mcpConfigs.map(function(s) {
                var live = mcpServers.find(function(ls) { return ls.name === s.name; });
                var statusText = live ? live.status : "disconnected";
                var toolCount = live ? live.tool_count : 0;
                return (
                  <div key={s.name} className="model-card" style={{ flexWrap: "wrap" }}>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div className="model-name">
                        {s.name}
                        <span style={{ fontSize: 10, marginLeft: 6, color: "var(--text-4)" }}>
                          ({s.transport})
                        </span>
                      </div>
                      <div className="model-desc">
                        {s.transport === "stdio" ? s.command + " " + (s.args || []).join(" ") : s.url}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 11, color: statusText === "connected" ? "var(--green)" : "var(--text-4)" }}>
                        {t("settings." + statusText)}{toolCount > 0 ? " · " + t("settings.toolsCount", { n: toolCount }) : ""}
                      </span>
                      <div className={"toggle " + (s.enabled !== false ? "on" : "")}
                           onClick={function() { toggleMcpServer(s.name); }}></div>
                      <button className="iconbtn" title={"Remove " + s.name}
                              onClick={function() { removeMcpServer(s.name); }}
                              style={{ color: "var(--text-4)" }}>
                        <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                          <path d="M5 5 L15 15 M15 5 L5 15" />
                        </svg>
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="settings-actions">
              <button className="btn primary" onClick={saveMcpServers}>{t("settings.saveRestartMcp")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)", marginLeft: 8 }}>{mcpSaved}</span>
            </div>
            <h3 style={{ marginTop: 16, marginBottom: 8, fontSize: 13 }}>{t("settings.addMcpServer")}</h3>
            <div className="inline-form-grid">
              <input className="input mono" placeholder="name" value={newMcpServer.name}
                     onChange={function(e) { setNewMcpServer({ ...newMcpServer, name: e.target.value }); }}
                     style={{ maxWidth: 140 }} />
              <select className="select" style={{ maxWidth: 100 }} value={newMcpServer.transport}
                      onChange={function(e) { setNewMcpServer({ ...newMcpServer, transport: e.target.value }); }}>
                <option value="stdio">stdio</option>
                <option value="sse">SSE</option>
              </select>
              {newMcpServer.transport === "stdio" ? (
                <>
                  <input className="input mono" placeholder="command (e.g. npx)" value={newMcpServer.command}
                         onChange={function(e) { setNewMcpServer({ ...newMcpServer, command: e.target.value }); }}
                         style={{ maxWidth: 140 }} />
                  <input className="input mono" placeholder="args (space-separated)" value={newMcpServer.args}
                         onChange={function(e) { setNewMcpServer({ ...newMcpServer, args: e.target.value }); }}
                         style={{ maxWidth: 240 }} />
                </>
              ) : (
                <input className="input mono" placeholder="URL (e.g. http://localhost:3000/mcp)" value={newMcpServer.url}
                       onChange={function(e) { setNewMcpServer({ ...newMcpServer, url: e.target.value }); }}
                       style={{ maxWidth: 360 }} />
              )}
              <button className="btn" onClick={addMcpServer}>{t("settings.add")}</button>
            </div>
          </div>
        )}

        {section === "keys" && (
          <div className="settings-pane">
            <h2>{t("settings.apiKeys")}</h2>
            <p className="subtitle">{t("settings.apiKeysSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.llmEndpoint")}<small>{t("settings.llmEndpointHint")}</small></div>
              <input className="input mono" value={keys.OPENAI_BASE_URL || config.base_url || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_BASE_URL: e.target.value })}
                     placeholder="https://api.deepseek.com/v1" style={{ maxWidth: 480 }} />
            </div>
            <div className="field">
              <div className="label">{t("settings.modelName")}<small>{t("settings.modelNameHint")}</small></div>
              <input className="input mono" value={keys.OPENAI_MODEL || config.model || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_MODEL: e.target.value })}
                     placeholder="deepseek-chat" style={{ maxWidth: 320 }} />
            </div>
            <div className="field">
              <div className="label">{t("settings.apiKey")}<small>{t("settings.apiKeyHint")}</small></div>
              <input className="input mono" type="password"
                     value={keys.OPENAI_API_KEY || ""}
                     onChange={(e) => setKeys({ ...keys, OPENAI_API_KEY: e.target.value })}
                     placeholder="sk-…" style={{ maxWidth: 480 }} />
            </div>
            <div className="field">
              <div className="label">{t("settings.telegramToken")}<small>{t("settings.telegramTokenHint")}</small></div>
              <input className="input mono" type="password"
                     value={keys.TELEGRAM_BOT_TOKEN || ""}
                     onChange={(e) => setKeys({ ...keys, TELEGRAM_BOT_TOKEN: e.target.value })}
                     placeholder="(optional)" style={{ maxWidth: 480 }} />
            </div>
            <div className="settings-actions">
              <button className="btn primary" onClick={saveKeys}>{t("settings.saveApiKeys")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{keysSaved}</span>
            </div>
            <div className="field" style={{ marginTop: 16 }}>
              <div className="label">{t("settings.redactSecrets")}<small>{t("settings.redactSecretsHint")}</small></div>
              <div className={"toggle " + (toggles.redactSecrets ? "on" : "")} onClick={() => toggleKey("redactSecrets")}></div>
            </div>
          </div>
        )}

        {section === "appearance" && (
          <div className="settings-pane">
            <h2>{t("settings.appearance")}</h2>
            <p className="subtitle">{t("settings.appearanceSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.theme")}<small>{t("settings.themeHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.theme === "system" ? "active" : "")}
                        onClick={() => setTweak && setTweak("theme", "system")}>{t("settings.system")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.theme === "light" ? "active" : "")}
                        onClick={() => setTweak && setTweak("theme", "light")}>{t("settings.light")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.theme === "dark" ? "active" : "")}
                        onClick={() => setTweak && setTweak("theme", "dark")}>{t("settings.dark")}</button>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.themeColor")}<small>{t("settings.themeColorHint", { theme: actualTheme || t("settings.system") })}</small></div>
              <div className="appearance-swatches">
                {(accentPresets || []).map((color, index) => (
                  <button
                    key={color}
                    className={"appearance-swatch " + (tweaks && tweaks.accent === color ? "active" : "")}
                    style={{ "--swatch-color": color }}
                    onClick={() => setTweak && setTweak("accent", color)}
                    title={t("settings.accentN", { n: index + 1 })}
                  >
                    <span className="appearance-swatch-dot"></span>
                  </button>
                ))}
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.textSize")}<small>{t("settings.textSizeHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "default" ? "active" : "")}
                        onClick={() => setTweak && setTweak("textSize", "default")}>
                  <span style={{ fontSize: 11 }}>A</span> {t("settings.default")}
                </button>
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "large" ? "active" : "")}
                        onClick={() => setTweak && setTweak("textSize", "large")}>
                  <span style={{ fontSize: 15 }}>A</span> {t("settings.large")}
                </button>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.density")}<small>{t("settings.densityHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.density === "cozy" ? "active" : "")}
                        onClick={() => setTweak && setTweak("density", "cozy")}>{t("settings.cozy")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.density === "compact" ? "active" : "")}
                        onClick={() => setTweak && setTweak("density", "compact")}>{t("settings.compact")}</button>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.language")}<small>{t("settings.languageHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (lang === "en" ? "active" : "")}
                        onClick={() => setLang("en")}>English</button>
                <button className={"seg-btn " + (lang === "zh" ? "active" : "")}
                        onClick={() => setLang("zh")}>中文</button>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.flowchartOrientation")}<small>{t("settings.flowchartOrientationHint")}</small></div>
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
                  {t("settings.horizontal")}
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
                  {t("settings.vertical")}
                </button>
              </div>
            </div>
            <div className="field">
              <div className="label">{t("settings.canvasLegend")}<small>{t("settings.canvasLegendHint")}</small></div>
              <div className={"toggle " + (tweaks && tweaks.showLegend ? "on" : "")}
                   onClick={() => setTweak && setTweak("showLegend", !(tweaks && tweaks.showLegend))}></div>
            </div>
            <div className="field">
              <div className="label">{t("settings.pulseAnimation")}<small>{t("settings.pulseAnimationHint")}</small></div>
              <div className={"toggle " + (tweaks && tweaks.animatePulse ? "on" : "")}
                   onClick={() => setTweak && setTweak("animatePulse", !(tweaks && tweaks.animatePulse))}></div>
            </div>
          </div>
        )}

        {section === "danger" && (
          <div className="settings-pane danger-pane">
            <h2>{t("settings.dangerZone")}</h2>
            <p className="subtitle">{t("settings.dangerSubtitle")}</p>
            <div className="field">
              <div className="label">{t("settings.clearSession")}<small>{t("settings.clearSessionHint")}</small></div>
              <button className="btn danger" onClick={clearSession}>{t("settings.clearSessionBtn")}</button>
            </div>
            <div className="field">
              <div className="label">{t("settings.soulPath")}<small>{t("settings.soulPathHint")}</small></div>
              <input className="input mono" value={config.soul_path} readOnly />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

window.SettingsPage = SettingsPage;
