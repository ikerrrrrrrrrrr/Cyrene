// Workbench Settings Overlay — floating panel (like search)
var {
  useState: useStateSt,
  useEffect: useEffectSt,
  useRef: useRefSt,
  useMemo: useMemoSt,
} = React;

var REPO_URL = "https://github.com/ikerrrrrrrrrrr/Cyrene";
var REPO_ISSUES_URL = REPO_URL + "/issues/new";
var DEFAULT_MODEL_BASE_URL = "https://api.deepseek.com/v1";

function readTweak(key, fallback) {
  try { var v = localStorage.getItem("cyrene-tweak-" + key); return v !== null ? JSON.parse(v) : fallback; } catch (e) { return fallback; }
}

function readCapability(key, fallback) {
  try {
    var v = localStorage.getItem("cyrene-tweak-cap-" + key);
    return v !== null ? JSON.parse(v) : fallback;
  } catch (e) {
    return fallback;
  }
}

function createEmptyModel() {
  return {
    id: "candidate-" + Date.now() + "-" + Math.random().toString(16).slice(2, 6),
    name: "", model: "", desc: "", ctx: "", price: "", api_key: "", base_url: DEFAULT_MODEL_BASE_URL,
  };
}

function normalizeModel(raw, idx, fbBaseUrl, fbKey) {
  var m = String(raw && (raw.model || raw.name || raw.id) || "").trim();
  return {
    id: String(raw && raw.id || "candidate-" + (idx + 1)).trim() || "candidate-" + (idx + 1),
    name: m, model: m,
    desc: String(raw && raw.desc || "").trim(),
    ctx: String(raw && raw.ctx || "").trim(),
    price: String(raw && raw.price || "").trim(),
    api_key: String(raw && raw.api_key || fbKey || "").trim(),
    base_url: String(raw && raw.base_url || fbBaseUrl || DEFAULT_MODEL_BASE_URL).trim() || DEFAULT_MODEL_BASE_URL,
  };
}

// ── Tab definitions ──
var TABS = [
  { id: "general", labelKey: "settings.general" },
  { id: "models", labelKey: "settings.models" },
  { id: "channels", labelKey: "settings.channels" },
  { id: "agents", labelKey: "settings.agents" },
  { id: "appearance", labelKey: "settings.appearance" },
  { id: "capabilities", labelKey: "settings.capabilities" },
  { id: "data", labelKey: "settings.data" },
  { id: "about", labelKey: "settings.about" },
];

// ── Settings Overlay ──
function SettingsOverlay({
  onClose,
  theme: initialTheme,
  actualTheme,
  onToggleTheme,
}) {
  var { t, lang, setLang } = useWorkbenchI18n();
  var [tab, setTab] = useStateSt("general");

  // ── General state ──
  var [developerMode, setDeveloperMode] = useStateSt(function () {
    try { return localStorage.getItem("cyrene-developer-mode") === "1"; } catch (e) { return false; }
  });
  var [desktopNotifications, setDesktopNotifications] = useStateSt(function () {
    try { return localStorage.getItem("cyrene-desktop-notifications") === "1"; } catch (e) { return false; }
  });
  var [mapProvider, setMapProvider] = useStateSt(function () {
    try { return localStorage.getItem("cyrene-tweak-map-provider") || "direct"; } catch (e) { return "direct"; }
  });
  var [amapKey, setAmapKey] = useStateSt("");
  var [amapKeySaved, setAmapKeySaved] = useStateSt("");

  // ── Models state ──
  var [models, setModels] = useStateSt([]);
  var [draftModel, setDraftModel] = useStateSt(createEmptyModel());
  var [visionModels, setVisionModels] = useStateSt([]);
  var [draftVision, setDraftVision] = useStateSt(createEmptyModel());
  var [secondaryModel, setSecondaryModel] = useStateSt(null);
  var [modelsSaved, setModelsSaved] = useStateSt("");

  // ── Config state ──
  var [config, setConfig] = useStateSt({
    model: "—", base_url: "—", assistant_name: "—",
    base_dir: "—", data_dir: "—", soul_path: "—",
    workspace_dir: "—", soul_content: "", spawn_policy: "conservative",
    heartbeat_interval: 1800, max_tool_rounds: 15,
    search_port: "8888",
  });
  var [configLoading, setConfigLoading] = useStateSt(true);
  var [soulDraft, setSoulDraft] = useStateSt("");
  var [soulStatus, setSoulStatus] = useStateSt("");
  var [agentProactive, setAgentProactive] = useStateSt(true);

  // ── Channels state ──
  var [telegramToken, setTelegramToken] = useStateSt("");
  var [telegramSaved, setTelegramSaved] = useStateSt("");
  var [wechatToken, setWechatToken] = useStateSt("");
  var [wechatSaved, setWechatSaved] = useStateSt("");
  var [notifyTelegram, setNotifyTelegram] = useStateSt(true);
  var [notifyWechat, setNotifyWechat] = useStateSt(true);

  // ── Capabilities state ──
  var [browserTools, setBrowserTools] = useStateSt(function () { return readCapability("browserTools", true); });
  var [redactSecrets, setRedactSecrets] = useStateSt(function () { return readCapability("redactSecrets", true); });
  var [mcpConfigs, setMcpConfigs] = useStateSt([]);
  var [mcpServers, setMcpServers] = useStateSt([]);
  var [mcpSaved, setMcpSaved] = useStateSt("");
  var [newMcpServer, setNewMcpServer] = useStateSt({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
  var [toolList, setToolList] = useStateSt([]);
  var [toolsExpanded, setToolsExpanded] = useStateSt(false);
  var [toolsSaved, setToolsSaved] = useStateSt("");

  // ── Data state ──
  var [resetStatus, setResetStatus] = useStateSt("");
  var [resetting, setResetting] = useStateSt(false);
  var [backupList, setBackupList] = useStateSt([]);
  var [backupMsg, setBackupMsg] = useStateSt("");
  var [exportSid, setExportSid] = useStateSt("");
  var [exportFmt, setExportFmt] = useStateSt("markdown");
  var [exportMsg, setExportMsg] = useStateSt("");

  // ── Tweak helpers ──
  var tweaks = {
    theme: initialTheme,
    accent: readTweak("accent", null),
    textSize: readTweak("textSize", "default"),
    density: readTweak("density", "cozy"),
    animatePulse: readTweak("animatePulse", true),
  };

  function setTweak(key, val) {
    try { localStorage.setItem("cyrene-tweak-" + key, JSON.stringify(val)); } catch (e) {}
    if (key === "density") document.documentElement.dataset.density = val;
    if (key === "textSize") document.documentElement.dataset.textSize = val || "default";
    if (key === "animatePulse") document.documentElement.dataset.animPulse = val ? "on" : "off";
    window.dispatchEvent(new Event("cyrene-tweak-" + key + "-change"));
  }

  function setCapability(key, val) {
    try { localStorage.setItem("cyrene-tweak-cap-" + key, JSON.stringify(val)); } catch (e) {}
  }

  // ── Keyboard: Escape to close ──
  useEffectSt(function () {
    function onKeyDown(e) {
      if (e.key === "Escape") { e.preventDefault(); onClose && onClose(); }
    }
    window.addEventListener("keydown", onKeyDown);
    return function () { window.removeEventListener("keydown", onKeyDown); };
  }, [onClose]);

  // Persist dev mode / desktop notifications
  useEffectSt(function () {
    try { localStorage.setItem("cyrene-developer-mode", developerMode ? "1" : "0"); window.dispatchEvent(new Event("cyrene-developer-mode-change")); } catch (e) {}
  }, [developerMode]);
  useEffectSt(function () {
    try { localStorage.setItem("cyrene-desktop-notifications", desktopNotifications ? "1" : "0"); } catch (e) {}
  }, [desktopNotifications]);

  // Load settings
  useEffectSt(function () {
    document.documentElement.dataset.density = tweaks.density;
    document.documentElement.dataset.textSize = tweaks.textSize || "default";
    document.documentElement.dataset.animPulse = tweaks.animatePulse ? "on" : "off";

    setConfigLoading(true);
    fetch("/api/settings/config").then(function (r) { return r.ok ? r.json() : Promise.reject("HTTP " + r.status); })
      .then(function (p) {
        setConfig(p);
        setSoulDraft(p.soul_content || "");
        if (p.notify_telegram !== undefined) setNotifyTelegram(p.notify_telegram);
        if (p.notify_wechat !== undefined) setNotifyWechat(p.notify_wechat);
        if (p.redact_secrets !== undefined) setRedactSecrets(!!p.redact_secrets);
        if (p.agent_proactive !== undefined) setAgentProactive(p.agent_proactive);
        setConfigLoading(false);
      }).catch(function () { setConfigLoading(false); });

    fetch("/api/settings/models").then(function (r) { return r.json(); }).then(function (p) {
      var fb = p.base_url || DEFAULT_MODEL_BASE_URL;
      var norm = function (raw, i) { return normalizeModel(raw, i, fb, ""); };
      var ms = (p.models || p.primary_candidates || []).map(norm);
      var vs = (p.vision_models || p.vision_candidates || []).map(norm);
      if (!ms.length) ms = [norm({}, 0)];
      if (!vs.length) vs = [norm({}, 0)];
      setModels(ms);
      setVisionModels(vs);
      setSecondaryModel({
        id: "secondary", model: (p.secondary_model && p.secondary_model.model) || "",
        api_key: (p.secondary_model && p.secondary_model.api_key) || "",
        base_url: (p.secondary_model && p.secondary_model.base_url) || fb,
        name: (p.secondary_model && (p.secondary_model.name || p.secondary_model.model)) || "",
        ctx_limit: (p.secondary_model && Number(p.secondary_model.ctx_limit)) || 0,
        max_concurrency: (p.secondary_model && Number(p.secondary_model.max_concurrency)) || 0,
      });
    }).catch(function () {});

    fetch("/api/settings/tools").then(function (r) { return r.json(); }).then(function (p) {
      var tools = p.tools || [];
      var browserToolNames = ["browser_navigate", "browser_screenshot", "browser_click", "browser_type", "browser_request_takeover"];
      setToolList(tools);
      if (tools.length) {
        var browserToolsList = tools.filter(function (tool) { return browserToolNames.indexOf(tool.name) >= 0; });
        if (browserToolsList.length) setBrowserTools(browserToolsList.every(function (tool) { return tool.enabled !== false; }));
      }
    }).catch(function () {});
    fetch("/api/settings/mcp").then(function (r) { return r.json(); }).then(function (p) { setMcpServers(p.servers || []); setMcpConfigs(p.configs || []); }).catch(function () {});
    fetch("/api/settings/keys").then(function (r) { return r.json(); }).then(function (p) {
      var tk = (p.keys || []).find(function (item) { return item.key === "TELEGRAM_BOT_TOKEN"; });
      if (tk) setTelegramToken(tk.value || "");
      var wk = (p.keys || []).find(function (item) { return item.key === "WECHAT_BOT_TOKEN"; });
      if (wk) setWechatToken(wk.value || "");
      var ak = (p.keys || []).find(function (item) { return item.key === "AMAP_API_KEY"; });
      if (ak) setAmapKey(ak.value || "");
    }).catch(function () {});

    fetch("/api/backup/list").then(function (r) { return r.json(); }).then(function (d) { if (d.ok) setBackupList(d.backups || []); }).catch(function () {});
  }, []);

  function saveSoul() {
    setSoulStatus(t("settings.saving"));
    fetch("/api/settings/soul", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: soulDraft }) })
      .then(function (r) { return r.ok ? setSoulStatus(t("settings.saved")) : Promise.reject(); })
      .catch(function () { setSoulStatus(t("settings.error")); });
    setTimeout(function () { setSoulStatus(""); }, 1500);
  }

  function saveModels() {
    var norm = models.map(function (m, i) { return normalizeModel(m, i, config.base_url || DEFAULT_MODEL_BASE_URL, ""); }).filter(function (m) { return m.model; });
    var vNorm = visionModels.map(function (m, i) { return normalizeModel(m, i, config.base_url || DEFAULT_MODEL_BASE_URL, ""); }).filter(function (m) { return m.model; });
    if (!norm.length || !vNorm.length) { setModelsSaved(t("settings.modelCandidateRequired")); return; }
    setModelsSaved(t("settings.saving"));
    fetch("/api/settings/models", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        models: norm, vision_models: vNorm,
        secondary_model: secondaryModel ? {
          model: secondaryModel.model, name: secondaryModel.name,
          api_key: secondaryModel.api_key, base_url: secondaryModel.base_url,
          ctx_limit: Number(secondaryModel.ctx_limit) || 0,
          max_concurrency: Number(secondaryModel.max_concurrency) || 0,
        } : null,
      }),
    }).then(function (r) { return r.json(); }).then(function (p) {
      var fb = p.base_url || config.base_url || DEFAULT_MODEL_BASE_URL;
      setModels(((p.models || p.primary_candidates || [])).map(function (m, i) { return normalizeModel(m, i, fb, ""); }));
      setVisionModels(((p.vision_models || p.vision_candidates || [])).map(function (m, i) { return normalizeModel(m, i, fb, ""); }));
      setSecondaryModel({
        id: "secondary", model: (p.secondary_model && p.secondary_model.model) || "",
        api_key: (p.secondary_model && p.secondary_model.api_key) || "",
        base_url: (p.secondary_model && p.secondary_model.base_url) || fb,
        name: (p.secondary_model && (p.secondary_model.name || p.secondary_model.model)) || "",
        ctx_limit: (p.secondary_model && Number(p.secondary_model.ctx_limit)) || 0,
        max_concurrency: (p.secondary_model && Number(p.secondary_model.max_concurrency)) || 0,
      });
      setModelsSaved(t("settings.saved"));
      setTimeout(function () { setModelsSaved(""); }, 1500);
    }).catch(function (e) {
      setModelsSaved(t("settings.error") + ": " + (e.message || ""));
    });
  }

  function saveTools() {
    setToolsSaved(t("settings.saving"));
    var map = {};
    toolList.forEach(function (t) { map[t.name] = t.enabled; });
    fetch("/api/settings/tools", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tools: map }) })
      .then(function (r) { return r.ok ? (setToolsSaved(t("settings.saved")), setTimeout(function () { setToolsSaved(""); }, 1500)) : Promise.reject(); })
      .catch(function () { setToolsSaved(t("settings.error")); });
  }

  function saveAgents() {
    fetch("/api/settings/config", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        spawn_policy: config.spawn_policy || "conservative",
        heartbeat_interval: Number(config.heartbeat_interval) || 1800,
        agent_proactive: agentProactive,
        max_tool_rounds: Number(config.max_tool_rounds) || 15,
      }),
    }).catch(function () {});
  }

  function saveBrowserTools(nextEnabled) {
    var browserToolNames = ["browser_navigate", "browser_screenshot", "browser_click", "browser_type", "browser_request_takeover"];
    var nextToolList = toolList.map(function (tool) {
      return browserToolNames.indexOf(tool.name) >= 0 ? { ...tool, enabled: nextEnabled } : tool;
    });
    var map = {};
    nextToolList.forEach(function (tool) { map[tool.name] = tool.enabled; });
    setBrowserTools(nextEnabled);
    setToolList(nextToolList);
    fetch("/api/settings/tools", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tools: map }) }).catch(function () {});
  }

  function saveRedactSecrets(nextEnabled) {
    setRedactSecrets(nextEnabled);
    setCapability("redactSecrets", nextEnabled);
    fetch("/api/settings/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ redact_secrets: nextEnabled }) }).catch(function () {});
  }

  function saveMcp() {
    setMcpSaved(t("settings.saving"));
    fetch("/api/settings/mcp", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ servers: mcpConfigs }) })
      .then(function () {
        setMcpSaved(t("settings.saved"));
        setTimeout(function () { setMcpSaved(""); }, 1500);
        fetch("/api/settings/mcp").then(function (r) { return r.json(); }).then(function (p) { setMcpServers(p.servers || []); setMcpConfigs(p.configs || []); }).catch(function () {});
      }).catch(function () { setMcpSaved(t("settings.error")); });
  }

  function toggleDesktopNotifications() {
    if (typeof Notification === "undefined") return;
    if (desktopNotifications) { setDesktopNotifications(false); return; }
    if (Notification.permission === "granted") { setDesktopNotifications(true); return; }
    if (Notification.permission !== "denied") { Notification.requestPermission().then(function (p) { setDesktopNotifications(p === "granted"); }); }
  }

  function loadBackups() {
    fetch("/api/backup/list").then(function (r) { return r.json(); }).then(function (d) { if (d.ok) setBackupList(d.backups || []); }).catch(function () {});
  }

  function formatBytes(n) { n = Number(n || 0); if (n < 1024) return n + " B"; if (n < 1048576) return (n / 1024).toFixed(1) + " KB"; return (n / 1048576).toFixed(1) + " MB"; }
  function formatDate(iso) { if (!iso) return "—"; try { return new Date(iso).toLocaleString(); } catch (e) { return iso; } }

  // ── Render helpers ──
  function onChange(key, stateFn) { return function (e) { stateFn(e.target.value); }; }

  return React.createElement("div", {
    className: "settings-overlay",
    onClick: function (e) { if (e.target === e.currentTarget) onClose && onClose(); },
  },
    React.createElement("div", { className: "settings-overlay-panel", onClick: function (e) { e.stopPropagation(); } },
      // Header
      React.createElement("div", { className: "settings-overlay-header" },
        React.createElement("span", { className: "settings-overlay-icon" }, "⚙"),
        React.createElement("strong", null, t("nav.settings")),
        React.createElement("button", { className: "settings-overlay-close", onClick: onClose }, "ESC"),
      ),

      // Body: sidebar + content
      React.createElement("div", { className: "settings-overlay-body" },
        // Sidebar tabs
        React.createElement("div", { className: "settings-overlay-nav" },
          TABS.map(function (item) {
            return React.createElement("button", {
              key: item.id,
              className: "settings-overlay-tab" + (tab === item.id ? " active" : ""),
              onClick: function () { setTab(item.id); },
            }, t(item.labelKey));
          }),
        ),

        // Content area
        React.createElement("div", { className: "settings-overlay-content" },
          tab === "general" && GeneralPanel({ t, lang, setLang, developerMode, setDeveloperMode, desktopNotifications, toggleDesktopNotifications, mapProvider, setMapProvider, amapKey, setAmapKey, amapKeySaved, setAmapKeySaved }),
          tab === "models" && ModelsPanel({ t, models, setModels, draftModel, setDraftModel, visionModels, setVisionModels, draftVision, setDraftVision, secondaryModel, setSecondaryModel, modelsSaved, saveModels, config }),
          tab === "channels" && ChannelsPanel({ t, telegramToken, setTelegramToken, telegramSaved, setTelegramSaved, wechatToken, setWechatToken, wechatSaved, setWechatSaved, notifyTelegram, setNotifyTelegram, notifyWechat, setNotifyWechat }),
          tab === "agents" && AgentsPanel({ t, config, setConfig, configLoading, soulDraft, setSoulDraft, soulStatus, saveSoul, agentProactive, setAgentProactive, saveAgents }),
          tab === "appearance" && AppearancePanel({ t, tweaks, setTweak, actualTheme, theme: initialTheme }),
          tab === "capabilities" && CapabilitiesPanel({ t, browserTools, saveBrowserTools, mcpConfigs, setMcpConfigs, mcpServers, toolList, toolsExpanded, setToolsExpanded, toolsSaved, saveTools, newMcpServer, setNewMcpServer, mcpSaved, saveMcp, config }),
          tab === "data" && DataPanel({ t, redactSecrets, saveRedactSecrets, config, configLoading, resetStatus, setResetStatus, resetting, setResetting, backupList, backupMsg, setBackupMsg, loadBackups, exportSid, setExportSid, exportFmt, setExportFmt, exportMsg, setExportMsg, formatBytes, formatDate }),
          tab === "about" && AboutPanel({ t, config }),
        ),
      ),
    ),
  );
}

// ── General Panel ──
function GeneralPanel(p) {
  var { t, lang, setLang, developerMode, setDeveloperMode, desktopNotifications, toggleDesktopNotifications, mapProvider, setMapProvider, amapKey, setAmapKey, amapKeySaved, setAmapKeySaved } = p;

  function saveAmapKey() {
    if (!amapKey || amapKey.startsWith("••")) { setAmapKeySaved(t("settings.noChanges")); setTimeout(function () { setAmapKeySaved(""); }, 1500); return; }
    setAmapKeySaved(t("settings.saving"));
    fetch("/api/settings/keys", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ AMAP_API_KEY: amapKey }) })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function () {
        fetch("/api/amap/verify").then(function (r) { return r.json(); }).then(function (vd) {
          if (vd.valid) { setAmapKeySaved(t("settings.amapKeySaved")); localStorage.setItem("cyrene-tweak-map-provider", "amap"); }
          else { setAmapKeySaved(t("settings.amapKeyVerifyFail") + " " + (vd.error || "")); }
        }).catch(function () { setAmapKeySaved(t("settings.saved")); });
        setTimeout(function () { setAmapKeySaved(""); }, 3000);
      }).catch(function () { setAmapKeySaved(t("settings.error")); setTimeout(function () { setAmapKeySaved(""); }, 3000); });
  }

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.general")),
    FieldRow(t("settings.language"), t("settings.languageHint"),
      React.createElement("div", { className: "wb-seg" },
        React.createElement("button", { className: "wb-seg-btn" + (lang === "en" ? " active" : ""), onClick: function () { setLang("en"); } }, "English"),
        React.createElement("button", { className: "wb-seg-btn" + (lang === "zh" ? " active" : ""), onClick: function () { setLang("zh"); } }, "中文"),
      ),
    ),
    FieldRow(t("settings.developerMode"), t("settings.developerModeHint"),
      Toggle(developerMode, function () { setDeveloperMode(!developerMode); }),
    ),
    FieldRow(t("settings.desktopNotifications"), t("settings.desktopNotificationsHint"),
      Toggle(desktopNotifications, toggleDesktopNotifications),
    ),
    FieldRow(t("settings.mapProvider"), t("settings.mapProviderHint"),
      React.createElement("div", { className: "wb-seg" },
        React.createElement("button", { className: "wb-seg-btn" + (mapProvider === "direct" ? " active" : ""), onClick: function () { setMapProvider("direct"); localStorage.setItem("cyrene-tweak-map-provider", "direct"); } }, t("settings.mapProviderDirect")),
        React.createElement("button", { className: "wb-seg-btn" + (mapProvider === "amap" ? " active" : ""), onClick: function () { setMapProvider("amap"); } }, t("settings.mapProviderAmap")),
      ),
    ),
    mapProvider === "amap" && FieldRow(t("settings.amapKey"), t("settings.amapKeyHint"),
      React.createElement("div", { className: "wb-inline-row" },
        React.createElement("input", { className: "wb-input mono", type: "password", value: amapKey, onChange: function (e) { setAmapKey(e.target.value); }, placeholder: "高德 Web 服务 Key" }),
        React.createElement("button", { className: "wb-btn primary", onClick: saveAmapKey }, t("settings.save")),
      ),
      amapKeySaved && React.createElement("span", { className: "wb-hint saved" }, amapKeySaved),
    ),
  );
}

// ── Models Panel ──
function ModelsPanel(p) {
  var { t, models, setModels, draftModel, setDraftModel, visionModels, setVisionModels, draftVision, setDraftVision, secondaryModel, setSecondaryModel, modelsSaved, saveModels, config } = p;

  function updateModel(id, field, val) {
    setModels(models.map(function (m) { return m.id === id ? { ...m, [field]: val, name: field === "model" ? val : m.name } : m; }));
  }
  function moveModel(id, dir) {
    var idx = models.findIndex(function (m) { return m.id === id; });
    var tgt = idx + dir;
    if (idx < 0 || tgt < 0 || tgt >= models.length) return;
    var next = models.slice();
    var cur = next[idx]; next[idx] = next[tgt]; next[tgt] = cur;
    setModels(next);
  }
  function deleteModel(id) { if (models.length > 1) setModels(models.filter(function (m) { return m.id !== id; })); }
  function addModel() { var c = normalizeModel(draftModel, models.length, "", ""); if (!c.model) return; setModels(models.concat(c)); setDraftModel(createEmptyModel()); }

  function updateVisionModel(id, field, val) {
    setVisionModels(visionModels.map(function (m) { return m.id === id ? { ...m, [field]: val, name: field === "model" ? val : m.name } : m; }));
  }
  function moveVisionModel(id, dir) {
    var idx = visionModels.findIndex(function (m) { return m.id === id; });
    var tgt = idx + dir;
    if (idx < 0 || tgt < 0 || tgt >= visionModels.length) return;
    var next = visionModels.slice();
    var cur = next[idx]; next[idx] = next[tgt]; next[tgt] = cur;
    setVisionModels(next);
  }
  function deleteVisionModel(id) { if (visionModels.length > 1) setVisionModels(visionModels.filter(function (m) { return m.id !== id; })); }
  function addVisionModel() { var c = normalizeModel(draftVision, visionModels.length, "", ""); if (!c.model) return; setVisionModels(visionModels.concat(c)); setDraftVision(createEmptyModel()); }

  function updateSecondary(field, val) { setSecondaryModel(function (prev) { return prev ? { ...prev, [field]: val, name: field === "model" ? val : prev.name } : prev; }); }

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.models"), t("settings.modelsSubtitle")),

    // Primary model
    SectionBlock(t("settings.primaryModelSlot"), null,
      models[0] && ModelCard([
        ModelField(t("settings.modelIdentifierLabel"), React.createElement("input", { className: "wb-input mono", value: models[0].model, onChange: function (e) { updateModel(models[0].id, "model", e.target.value); }, placeholder: t("settings.placeholderModelIdentifier") })),
        ModelField(t("settings.apiKey"), React.createElement("input", { className: "wb-input mono", type: "password", value: models[0].api_key, onChange: function (e) { updateModel(models[0].id, "api_key", e.target.value); }, placeholder: "sk-..." })),
        ModelField(t("settings.baseUrlLabel"), React.createElement("input", { className: "wb-input mono", value: models[0].base_url, onChange: function (e) { updateModel(models[0].id, "base_url", e.target.value); }, placeholder: DEFAULT_MODEL_BASE_URL })),
        React.createElement("div", { className: "wb-model-meta" },
          React.createElement("div", null, React.createElement("small", null, t("settings.descriptionLabel")), React.createElement("input", { className: "wb-input mono small", value: models[0].desc, onChange: function (e) { updateModel(models[0].id, "desc", e.target.value); }, placeholder: t("settings.placeholderDesc") })),
          React.createElement("div", null, React.createElement("small", null, t("settings.contextLabel")), React.createElement("input", { className: "wb-input mono small", value: models[0].ctx, onChange: function (e) { updateModel(models[0].id, "ctx", e.target.value); }, placeholder: t("settings.placeholderCtx") })),
          React.createElement("div", null, React.createElement("small", null, t("settings.priceLabel")), React.createElement("input", { className: "wb-input mono small", value: models[0].price, onChange: function (e) { updateModel(models[0].id, "price", e.target.value); }, placeholder: t("settings.placeholderPrice") })),
        ),
      ]),
    ),

    // Fallback candidates
    SectionBlock(t("settings.fallbackCandidates"), React.createElement("button", { className: "wb-btn", onClick: addModel }, t("settings.addFallbackCandidate")),
      models.slice(1).map(function (m) {
        return ModelCard([
          React.createElement("div", { className: "wb-model-actions" },
            React.createElement("button", { className: "wb-icon-btn-small", onClick: function () { moveModel(m.id, -1); } }, "▲"),
            React.createElement("button", { className: "wb-icon-btn-small", onClick: function () { moveModel(m.id, 1); } }, "▼"),
            React.createElement("button", { className: "wb-icon-btn-small danger", onClick: function () { deleteModel(m.id); } }, "✖"),
          ),
          ModelField(t("settings.modelIdentifierLabel"), React.createElement("input", { className: "wb-input mono", value: m.model, onChange: function (e) { updateModel(m.id, "model", e.target.value); }, placeholder: t("settings.placeholderModelIdentifier") })),
          ModelField(t("settings.apiKey"), React.createElement("input", { className: "wb-input mono", type: "password", value: m.api_key, onChange: function (e) { updateModel(m.id, "api_key", e.target.value); }, placeholder: "sk-..." })),
          ModelField(t("settings.baseUrlLabel"), React.createElement("input", { className: "wb-input mono", value: m.base_url, onChange: function (e) { updateModel(m.id, "base_url", e.target.value); }, placeholder: DEFAULT_MODEL_BASE_URL })),
        ]);
      }),
    ),
    modelDraftField(draftModel, setDraftModel, addModel, t),

    SectionBlock(t("settings.secondaryModelSlot"), t("settings.secondaryModelHint"),
      secondaryModel && ModelCard([
        ModelField(t("settings.modelIdentifierLabel"), React.createElement("input", { className: "wb-input mono", value: secondaryModel.model, onChange: function (e) { updateSecondary("model", e.target.value); }, placeholder: t("settings.placeholderModelIdentifier") })),
        ModelField(t("settings.apiKey"), React.createElement("input", { className: "wb-input mono", type: "password", value: secondaryModel.api_key, onChange: function (e) { updateSecondary("api_key", e.target.value); }, placeholder: "sk-..." })),
        ModelField(t("settings.baseUrlLabel"), React.createElement("input", { className: "wb-input mono", value: secondaryModel.base_url, onChange: function (e) { updateSecondary("base_url", e.target.value); }, placeholder: DEFAULT_MODEL_BASE_URL })),
        React.createElement("div", { className: "wb-model-meta" },
          React.createElement("div", null, React.createElement("small", null, t("settings.secondaryModelCtxLimit")), React.createElement("input", { className: "wb-input mono small", type: "number", min: "0", value: secondaryModel.ctx_limit, onChange: function (e) { updateSecondary("ctx_limit", e.target.value); }, placeholder: "0" })),
          React.createElement("div", null, React.createElement("small", null, t("settings.secondaryModelConcurrency")), React.createElement("input", { className: "wb-input mono small", type: "number", min: "0", value: secondaryModel.max_concurrency, onChange: function (e) { updateSecondary("max_concurrency", e.target.value); }, placeholder: "0" })),
        ),
      ]),
    ),

    // Vision model
    SectionBlock(t("settings.visionModelSlot"), null,
      visionModels[0] && ModelCard([
        ModelField(t("settings.modelIdentifierLabel"), React.createElement("input", { className: "wb-input mono", value: visionModels[0].model, onChange: function (e) { updateVisionModel(visionModels[0].id, "model", e.target.value); }, placeholder: t("settings.placeholderModelIdentifier") })),
        ModelField(t("settings.apiKey"), React.createElement("input", { className: "wb-input mono", type: "password", value: visionModels[0].api_key, onChange: function (e) { updateVisionModel(visionModels[0].id, "api_key", e.target.value); }, placeholder: "sk-..." })),
        ModelField(t("settings.baseUrlLabel"), React.createElement("input", { className: "wb-input mono", value: visionModels[0].base_url, onChange: function (e) { updateVisionModel(visionModels[0].id, "base_url", e.target.value); }, placeholder: DEFAULT_MODEL_BASE_URL })),
      ]),
      visionModels.slice(1).map(function (m) {
        return ModelCard([
          React.createElement("div", { className: "wb-model-actions" },
            React.createElement("button", { className: "wb-icon-btn-small", onClick: function () { moveVisionModel(m.id, -1); } }, "▲"),
            React.createElement("button", { className: "wb-icon-btn-small", onClick: function () { moveVisionModel(m.id, 1); } }, "▼"),
            React.createElement("button", { className: "wb-icon-btn-small danger", onClick: function () { deleteVisionModel(m.id); } }, "✖"),
          ),
          ModelField(t("settings.modelIdentifierLabel"), React.createElement("input", { className: "wb-input mono", value: m.model, onChange: function (e) { updateVisionModel(m.id, "model", e.target.value); }, placeholder: t("settings.placeholderModelIdentifier") })),
          ModelField(t("settings.apiKey"), React.createElement("input", { className: "wb-input mono", type: "password", value: m.api_key, onChange: function (e) { updateVisionModel(m.id, "api_key", e.target.value); }, placeholder: "sk-..." })),
          ModelField(t("settings.baseUrlLabel"), React.createElement("input", { className: "wb-input mono", value: m.base_url, onChange: function (e) { updateVisionModel(m.id, "base_url", e.target.value); }, placeholder: DEFAULT_MODEL_BASE_URL })),
        ]);
      }),
    ),
    modelDraftField(draftVision, setDraftVision, addVisionModel, t),

    React.createElement("div", { className: "wb-save-actions" },
      React.createElement("button", { className: "wb-btn primary", onClick: saveModels }, t("settings.saveApply")),
      modelsSaved && React.createElement("span", { className: "wb-hint saved" }, modelsSaved),
    ),
  );
}

function modelDraftField(draft, setDraft, onAdd, t) {
  return React.createElement("div", { className: "wb-model-draft" },
    React.createElement("input", { className: "wb-input mono", value: draft.model, onChange: function (e) { setDraft({ ...draft, model: e.target.value, name: e.target.value }); }, placeholder: t("settings.placeholderModelIdentifier") }),
    React.createElement("input", { className: "wb-input mono", type: "password", value: draft.api_key, onChange: function (e) { setDraft({ ...draft, api_key: e.target.value }); }, placeholder: "sk-..." }),
    React.createElement("input", { className: "wb-input mono", value: draft.base_url, onChange: function (e) { setDraft({ ...draft, base_url: e.target.value }); }, placeholder: DEFAULT_MODEL_BASE_URL }),
    React.createElement("button", { className: "wb-btn", onClick: onAdd }, t("settings.add")),
  );
}

// ── Channels Panel ──
function ChannelsPanel(p) {
  var { t, telegramToken, setTelegramToken, telegramSaved, setTelegramSaved, wechatToken, setWechatToken, wechatSaved, setWechatSaved, notifyTelegram, setNotifyTelegram, notifyWechat, setNotifyWechat } = p;

  function saveTelegram() {
    if (!telegramToken || telegramToken.startsWith("••")) { setTelegramSaved(t("settings.noChanges")); setTimeout(function () { setTelegramSaved(""); }, 1500); return; }
    setTelegramSaved(t("settings.saving"));
    fetch("/api/settings/keys", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ TELEGRAM_BOT_TOKEN: telegramToken }) })
      .then(function () { setTelegramSaved(t("settings.saved")); setTimeout(function () { setTelegramSaved(""); }, 1500); })
      .catch(function () { setTelegramSaved(t("settings.error")); });
  }

  function saveWechat() {
    if (!wechatToken || wechatToken.startsWith("••")) { setWechatSaved(t("settings.noChanges")); setTimeout(function () { setWechatSaved(""); }, 1500); return; }
    setWechatSaved(t("settings.saving"));
    fetch("/api/settings/keys", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ WECHAT_BOT_TOKEN: wechatToken }) })
      .then(function () { setWechatSaved(t("settings.saved")); setTimeout(function () { setWechatSaved(""); }, 1500); })
      .catch(function () { setWechatSaved(t("settings.error")); });
  }

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.channels"), t("settings.channelsSubtitle")),

    React.createElement("div", { className: "wb-channel-card" },
      React.createElement("div", { className: "wb-channel-head" },
        React.createElement("span", { className: "wb-channel-icon" }, "⌖"),
        React.createElement("b", null, t("settings.telegram")),
      ),
      React.createElement("p", { className: "wb-channel-desc" }, t("settings.telegramTokenHint")),
      FieldRow(t("settings.telegramToken"), null,
        React.createElement("div", { className: "wb-inline-row" },
          React.createElement("input", { className: "wb-input mono", type: "password", value: telegramToken, onChange: function (e) { setTelegramToken(e.target.value); }, placeholder: t("settings.placeholderOptional") }),
          React.createElement("button", { className: "wb-btn primary", onClick: saveTelegram }, t("settings.saveNotification")),
        ),
        telegramSaved && React.createElement("span", { className: "wb-hint saved" }, telegramSaved),
      ),
      FieldRow(t("settings.notifyTelegram"), t("settings.notifyTelegramHint"),
        Toggle(notifyTelegram, function () {
          var next = !notifyTelegram;
          setNotifyTelegram(next);
          fetch("/api/settings/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notify_telegram: next }) }).catch(function () {});
        }),
      ),
    ),

    // WeChat
    React.createElement("div", { className: "wb-channel-card" },
      React.createElement("div", { className: "wb-channel-head" },
        React.createElement("span", { className: "wb-channel-icon" }, "⌖"),
        React.createElement("b", null, "微信 / WeChat"),
      ),
      React.createElement("p", { className: "wb-channel-desc" }, "通过 WeChat token 接入本地微信通道，用于通知与消息投递。"),
      FieldRow("WeChat Bot Token", null,
        React.createElement("div", { className: "wb-inline-row" },
          React.createElement("input", { className: "wb-input mono", type: "password", value: wechatToken, onChange: function (e) { setWechatToken(e.target.value); }, placeholder: "WECHAT_BOT_TOKEN" }),
          React.createElement("button", { className: "wb-btn primary", onClick: saveWechat }, t("settings.save")),
        ),
        wechatSaved && React.createElement("span", { className: "wb-hint saved" }, wechatSaved),
      ),
      FieldRow("通知推送", null, Toggle(notifyWechat, function () {
        var next = !notifyWechat;
        setNotifyWechat(next);
        fetch("/api/settings/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notify_wechat: next }) }).catch(function () {});
      })),
    ),
  );
}

// ── Agents Panel ──
function AgentsPanel(p) {
  var { t, config, setConfig, configLoading, soulDraft, setSoulDraft, soulStatus, saveSoul, agentProactive, setAgentProactive, saveAgents } = p;

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.agents"), t("settings.agentsSubtitle")),

    // SOUL.md
    React.createElement("div", { className: "wb-field wb-field-stack wb-field-soul" },
      React.createElement("div", { className: "wb-label" }, t("settings.soulMd"), React.createElement("small", null, t("settings.soulMdHint"))),
      React.createElement("textarea", { className: "wb-input mono wb-textarea-soul", value: soulDraft, onChange: function (e) { setSoulDraft(e.target.value); } }),
      React.createElement("div", { className: "wb-inline-row wb-inline-row-start", style: { marginTop: 8 } },
        React.createElement("button", { className: "wb-btn primary", onClick: saveSoul }, t("settings.saveSoul")),
        React.createElement("span", { className: "wb-hint" }, soulStatus || (configLoading ? t("settings.pathLoading") : config.soul_path)),
      ),
    ),

    FieldRow(t("settings.spawnPolicy"), t("settings.spawnPolicyHint"),
      React.createElement("select", { className: "wb-select", value: config.spawn_policy || "conservative", onChange: function (e) { setConfig({ ...config, spawn_policy: e.target.value }); } },
        React.createElement("option", { value: "aggressive" }, t("settings.aggressive")),
        React.createElement("option", { value: "conservative" }, t("settings.conservative")),
        React.createElement("option", { value: "off" }, t("settings.off")),
      ),
    ),
    FieldRow(t("settings.agentProactive"), t("settings.agentProactiveHint"), Toggle(agentProactive, function () { setAgentProactive(!agentProactive); })),
    FieldRow(t("settings.heartbeatInterval"), t("settings.heartbeatIntervalHint"),
      React.createElement("input", { className: "wb-input mono", type: "number", min: "60", step: "60", value: config.heartbeat_interval, onChange: function (e) { setConfig({ ...config, heartbeat_interval: Number(e.target.value) || 1800 }); }, style: { maxWidth: 120 } }),
    ),
    FieldRow(t("settings.maxToolRounds"), t("settings.maxToolRoundsHint"),
      React.createElement("input", { className: "wb-input mono", type: "number", min: "5", max: "200", step: "1", value: config.max_tool_rounds, onChange: function (e) { setConfig({ ...config, max_tool_rounds: Number(e.target.value) || 15 }); }, style: { maxWidth: 120 } }),
    ),
    React.createElement("div", { className: "wb-save-actions" },
      React.createElement("button", { className: "wb-btn primary", onClick: saveAgents }, t("settings.saveApply")),
    ),
  );
}

// ── Appearance Panel ──
function AppearancePanel(p) {
  var { t, tweaks, setTweak, actualTheme, theme } = p;
  var accentPresets = ["#4378ff", "#8b5cf6", "#e8796b", "#34b8a0", "#f4a93e", "#e5488b", "#6b8cff", "#a78bfa"];

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.appearance"), t("settings.appearanceSubtitle")),
    FieldRow(t("settings.theme"), t("settings.themeHint"),
      React.createElement("div", { className: "wb-seg" },
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.theme === "system" ? " active" : ""), onClick: function () { setTweak("theme", "system"); } }, t("settings.system")),
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.theme === "light" ? " active" : ""), onClick: function () { setTweak("theme", "light"); } }, t("settings.light")),
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.theme === "dark" ? " active" : ""), onClick: function () { setTweak("theme", "dark"); } }, t("settings.dark")),
      ),
    ),
    FieldRow(t("settings.themeColor"), t("settings.themeColorHint", { theme: actualTheme || t("settings.system") }),
      React.createElement("div", { className: "wb-color-swatches" },
        accentPresets.map(function (color, idx) {
          return React.createElement("button", {
            key: color,
            className: "wb-color-swatch" + (tweaks.accent === color ? " active" : ""),
            style: { "--swatch": color },
            onClick: function () { setTweak("accent", color); },
            title: t("settings.accentN", { n: idx + 1 }),
          });
        }),
      ),
    ),
    FieldRow(t("settings.textSize"), t("settings.textSizeHint"),
      React.createElement("div", { className: "wb-seg" },
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.textSize === "default" ? " active" : ""), onClick: function () { setTweak("textSize", "default"); } }, React.createElement("span", { style: { fontSize: 11 } }, "A"), " ", t("settings.default")),
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.textSize === "large" ? " active" : ""), onClick: function () { setTweak("textSize", "large"); } }, React.createElement("span", { style: { fontSize: 15 } }, "A"), " ", t("settings.large")),
      ),
    ),
    FieldRow(t("settings.density"), t("settings.densityHint"),
      React.createElement("div", { className: "wb-seg" },
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.density === "cozy" ? " active" : ""), onClick: function () { setTweak("density", "cozy"); } }, t("settings.cozy")),
        React.createElement("button", { className: "wb-seg-btn" + (tweaks.density === "compact" ? " active" : ""), onClick: function () { setTweak("density", "compact"); } }, t("settings.compact")),
      ),
    ),
    FieldRow(t("settings.pulseAnimation"), t("settings.pulseAnimationHint"), Toggle(tweaks.animatePulse, function () { setTweak("animatePulse", !tweaks.animatePulse); })),
  );
}

// ── Capabilities Panel ──
function CapabilitiesPanel(p) {
  var { t, browserTools, saveBrowserTools, mcpConfigs, setMcpConfigs, mcpServers, toolList, toolsExpanded, setToolsExpanded, toolsSaved, saveTools, newMcpServer, setNewMcpServer, mcpSaved, saveMcp, config } = p;

  function addMcp() {
    var name = (newMcpServer.name || "").trim();
    if (!name) return;
    setMcpConfigs(mcpConfigs.concat({
      name: name, transport: newMcpServer.transport || "stdio",
      command: newMcpServer.command || "",
      args: (newMcpServer.args || "").split(" ").filter(Boolean),
      url: newMcpServer.url || "",
      enabled: newMcpServer.enabled !== false,
    }));
    setNewMcpServer({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
  }

  function removeMcp(name) { setMcpConfigs(mcpConfigs.filter(function (s) { return s.name !== name; })); }
  function toggleMcp(name) { setMcpConfigs(mcpConfigs.map(function (s) { return s.name === name ? { ...s, enabled: !s.enabled } : s; })); }
  function toggleTool(name) { setToolList(toolList.map(function (t) { return t.name === name ? { ...t, enabled: !t.enabled } : t; })); }

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.capabilities"), t("settings.capabilitiesSubtitle")),
    FieldRow(t("settings.browserTools"), t("settings.browserToolsHint"), Toggle(browserTools, function () { saveBrowserTools(!browserTools); })),

    // Web Search (read only)
    SectionBlock(t("settings.webSearch"), null,
      FieldRow(t("settings.searchBackend"), null, React.createElement("input", { className: "wb-input mono", value: t("settings.builtin"), readOnly: true, style: { maxWidth: 240 } })),
      FieldRow(t("settings.builtinStatus"), null, React.createElement("input", { className: "wb-input mono", value: t("settings.autoStarted"), readOnly: true, style: { maxWidth: 240 } })),
      FieldRow(t("settings.searchProxy"), null, React.createElement("input", { className: "wb-input mono", value: t("settings.searchProxyAuto"), readOnly: true, style: { maxWidth: 240 } })),
    ),

    // MCP
    SectionBlock(t("settings.mcpServers"), null,
      mcpConfigs.map(function (server) {
        var live = mcpServers.find(function (s) { return s.name === server.name; });
        var st = live ? live.status : "disconnected";
        var tc = live ? live.tool_count : 0;
        return React.createElement("div", { className: "wb-mcp-row", key: server.name },
          React.createElement("div", { className: "wb-mcp-info" },
            React.createElement("b", null, server.name),
            React.createElement("small", null, server.transport === "stdio" ? server.command + " " + (server.args || []).join(" ") : server.url),
          ),
          React.createElement("div", { className: "wb-mcp-status" },
            React.createElement("span", { className: "wb-mcp-indicator " + st }, st === "connected" ? "● " + t("settings.connected") : "○ " + t("settings.disconnected")),
            tc > 0 && React.createElement("small", null, t("settings.toolsCount", { n: tc })),
            Toggle(server.enabled !== false, function () { toggleMcp(server.name); }),
            React.createElement("button", { className: "wb-icon-btn-small danger", onClick: function () { removeMcp(server.name); } }, "✖"),
          ),
        );
      }),
      React.createElement("div", { className: "wb-mcp-add" },
        React.createElement("input", { className: "wb-input mono", placeholder: t("settings.placeholderName"), value: newMcpServer.name, onChange: function (e) { setNewMcpServer({ ...newMcpServer, name: e.target.value }); } }),
        React.createElement("select", { className: "wb-select", value: newMcpServer.transport, onChange: function (e) { setNewMcpServer({ ...newMcpServer, transport: e.target.value }); } },
          React.createElement("option", { value: "stdio" }, "stdio"),
          React.createElement("option", { value: "sse" }, "SSE"),
        ),
        newMcpServer.transport === "stdio"
          ? React.createElement(React.Fragment, null,
              React.createElement("input", { className: "wb-input mono", placeholder: t("settings.placeholderCommand"), value: newMcpServer.command, onChange: function (e) { setNewMcpServer({ ...newMcpServer, command: e.target.value }); } }),
              React.createElement("input", { className: "wb-input mono", placeholder: t("settings.placeholderArgs"), value: newMcpServer.args, onChange: function (e) { setNewMcpServer({ ...newMcpServer, args: e.target.value }); } }),
            )
          : React.createElement("input", { className: "wb-input mono", placeholder: t("settings.placeholderMcpUrl"), value: newMcpServer.url, onChange: function (e) { setNewMcpServer({ ...newMcpServer, url: e.target.value }); } }),
        React.createElement("button", { className: "wb-btn", onClick: addMcp }, t("settings.add")),
      ),
      React.createElement("div", { className: "wb-save-actions" },
        React.createElement("button", { className: "wb-btn primary", onClick: saveMcp }, t("settings.saveRestartMcp")),
        mcpSaved && React.createElement("span", { className: "wb-hint saved" }, mcpSaved),
      ),
    ),

    // Tools
    React.createElement("div", null,
      React.createElement("button", { className: "wb-collapse-head", onClick: function () { setToolsExpanded(!toolsExpanded); } },
        React.createElement("b", null, t("settings.tools")),
        React.createElement("span", { className: "wb-collapse-icon" + (toolsExpanded ? " open" : "") }, "⌖ ".concat(toolsExpanded ? t("settings.collapseTools") : t("settings.expandTools", { count: toolList.length }))),
      ),
      toolsExpanded && React.createElement("div", { className: "wb-tool-list" },
        toolList.map(function (tool) {
          return FieldRow(React.createElement("span", { className: "mono" }, tool.name), tool.desc, Toggle(tool.enabled, function () { toggleTool(tool.name); }));
        }),
        React.createElement("div", { className: "wb-save-actions" },
          React.createElement("button", { className: "wb-btn primary", onClick: saveTools }, t("settings.saveTools")),
          toolsSaved && React.createElement("span", { className: "wb-hint saved" }, toolsSaved),
        ),
      ),
    ),
  );
}

// ── Data Panel ──
function DataPanel(p) {
  var { t, redactSecrets, saveRedactSecrets, config, configLoading, resetStatus, setResetStatus, resetting, setResetting, backupList, backupMsg, setBackupMsg, loadBackups, exportSid, setExportSid, exportFmt, setExportFmt, exportMsg, setExportMsg, formatBytes, formatDate } = p;

  var DATA = window.DATA || {};

  function clearSession() {
    fetch("/api/chat/clear", { method: "POST" }).then(function () { if (window.refreshSessions) window.refreshSessions(); }).catch(function () {});
  }

  function resetData() {
    setResetting(true);
    setResetStatus(t("settings.resettingData"));
    fetch("/api/settings/reset-data", { method: "POST" }).then(function (r) { return r.json(); }).then(function (p) {
      if (p.ok) {
        try { Object.keys(localStorage).forEach(function (k) { if (k.indexOf("cyrene-") === 0) localStorage.removeItem(k); }); } catch (e) {}
        window.location.reload();
      } else { setResetStatus(t("settings.resetAppDataFailed")); setResetting(false); }
    }).catch(function (e) { setResetStatus(t("settings.resetAppDataFailed") + ": " + e.message); setResetting(false); });
  }

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.data"), t("settings.dataSubtitle")),
    FieldRow(t("settings.redactSecrets"), t("settings.redactSecretsHint"), Toggle(redactSecrets, function () { saveRedactSecrets(!redactSecrets); })),
    FieldRow(t("settings.clearSession"), t("settings.clearSessionHint"),
      React.createElement("button", { className: "wb-btn muted", onClick: clearSession }, t("settings.clearSessionBtn")),
    ),
    React.createElement("div", { className: "wb-field wb-field-stack wb-field-danger" },
      React.createElement("div", { className: "wb-label" },
        t("settings.resetAppData"),
        React.createElement("small", null, t("settings.resetAppDataHint")),
      ),
      React.createElement("div", { className: "wb-controls" },
        React.createElement("div", { className: "wb-inline-row wb-inline-row-start" },
          React.createElement("button", { className: "wb-btn danger", onClick: resetData, disabled: resetting }, resetting ? t("settings.resettingData") : t("settings.resetAppDataBtn")),
          resetStatus && React.createElement("span", { className: "wb-hint" }, resetStatus),
        ),
      ),
    ),

    // Path info
    SectionBlock(t("settings.pathInfo"), null,
      FieldRow(t("settings.baseDir"), null, React.createElement("input", { className: "wb-input mono", value: configLoading ? t("settings.pathLoading") : config.base_dir, readOnly: true })),
      FieldRow(t("settings.dataDir"), null, React.createElement("input", { className: "wb-input mono", value: configLoading ? t("settings.pathLoading") : config.data_dir, readOnly: true })),
      FieldRow(t("settings.workspaceDir"), null, React.createElement("input", { className: "wb-input mono", value: configLoading ? t("settings.pathLoading") : config.workspace_dir, readOnly: true })),
      FieldRow(t("settings.soulPath"), null, React.createElement("input", { className: "wb-input mono", value: configLoading ? t("settings.pathLoading") : config.soul_path, readOnly: true })),
    ),

    // Backup
    SectionBlock(t("settings.backup"), null,
      React.createElement("div", { className: "wb-inline-row" },
        React.createElement("button", { className: "wb-btn primary", onClick: function () {
          setBackupMsg(t("settings.backupExporting"));
          fetch("/api/backup/export", { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.ok) { setBackupMsg(t("settings.backupExported", { n: d.entries.length, size: formatBytes(d.size) })); loadBackups(); }
            else throw new Error(d.error);
          }).catch(function (e) { setBackupMsg(t("settings.failed") + ": " + e.message); });
        } }, t("settings.backupExportBtn")),
        React.createElement("button", { className: "wb-btn", onClick: function () {
          if (!backupList.length) { setBackupMsg(t("settings.backupNoBackups")); return; }
          var last = backupList[0];
          fetch("/api/backup/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: last.path }) })
            .then(function (r) { return r.json(); }).then(function (d) { if (d.ok) setBackupMsg(t("settings.backupRestored", { n: d.restored.length })); else throw new Error(d.error); })
            .catch(function (e) { setBackupMsg(t("settings.backupRestoreFailed") + ": " + e.message); });
        } }, t("settings.backupRestoreBtn")),
      ),
      backupMsg && React.createElement("p", { className: "wb-hint" }, backupMsg),
      backupList.map(function (b) {
        return React.createElement("div", { className: "wb-backup-row", key: b.name },
          React.createElement("span", { className: "wb-backup-name" }, b.name),
          React.createElement("span", { className: "wb-backup-meta" }, formatBytes(b.size), " · ", formatDate(b.modified)),
        );
      }),
    ),

    // Session export
    SectionBlock(t("settings.sessionExport"), null,
      DATA.sessions && DATA.sessions.length > 0 ? React.createElement("div", { className: "wb-export-area" },
        React.createElement("select", { className: "wb-select", value: exportSid, onChange: function (e) { setExportSid(e.target.value); setExportMsg(""); }, style: { maxWidth: 300 } },
          React.createElement("option", { value: "" }, t("settings.sessionExportSelectPlaceholder")),
          DATA.sessions.map(function (s) {
            return React.createElement("option", { key: s.id, value: s.id }, s.title || s.id);
          }),
        ),
        React.createElement("div", { className: "wb-seg" },
          React.createElement("button", { className: "wb-seg-btn" + (exportFmt === "markdown" ? " active" : ""), onClick: function () { setExportFmt("markdown"); } }, "Markdown"),
          React.createElement("button", { className: "wb-seg-btn" + (exportFmt === "json" ? " active" : ""), onClick: function () { setExportFmt("json"); } }, "JSON"),
        ),
        React.createElement("div", { className: "wb-inline-row" },
          React.createElement("button", { className: "wb-btn primary", disabled: !exportSid, onClick: function () {
            if (!exportSid) return;
            var url = "/api/sessions/" + encodeURIComponent(exportSid) + "/export?format=" + exportFmt;
            var a = document.createElement("a"); a.href = url; document.body.appendChild(a); a.click(); document.body.removeChild(a);
            setExportMsg("✓"); setTimeout(function () { setExportMsg(""); }, 2000);
          } }, t("settings.sessionExportBtn")),
          exportMsg && React.createElement("span", { className: "wb-hint" }, exportMsg),
        ),
      ) : React.createElement("p", { className: "wb-hint" }, t("settings.sessionExportNoSessions")),
    ),
  );
}

// ── About Panel ──
function AboutPanel(p) {
  var { t, config } = p;

  return React.createElement("div", { className: "settings-panel" },
    SectionTitle(t("settings.about"), t("settings.aboutSubtitle")),

    React.createElement("div", { className: "wb-about-hero" },
      React.createElement("div", { className: "wb-about-logo", "aria-hidden": "true" },
        React.createElement("div", { className: "brand-mark" }),
      ),
      React.createElement("h3", null, "Cyrene"),
      React.createElement("p", null, t("settings.aboutHeroCopy")),
    ),

    React.createElement("div", { className: "wb-about-grid" },
      React.createElement("div", { className: "wb-about-card" },
        React.createElement("span", { className: "wb-about-label" }, t("settings.projectName")),
        React.createElement("strong", null, "Cyrene"),
        React.createElement("a", { className: "wb-btn", href: REPO_URL, target: "_blank", rel: "noopener noreferrer" },
          React.createElement("svg", { width: "14", height: "14", viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": "true" },
            React.createElement("path", { d: "M8 0C3.58 0 0 3.58 0 8C0 11.54 2.29 14.53 5.47 15.59C5.87 15.66 6.02 15.42 6.02 15.21C6.02 15.02 6.01 14.39 6.01 13.56C4 13.93 3.48 13.07 3.32 12.62C3.23 12.39 2.84 11.68 2.5 11.49C2.22 11.34 1.82 10.96 2.49 10.95C3.12 10.94 3.57 11.53 3.72 11.76C4.44 12.97 5.59 12.63 6.05 12.42C6.12 11.9 6.33 11.55 6.56 11.35C4.78 11.15 2.92 10.46 2.92 7.4C2.92 6.53 3.23 5.82 3.74 5.26C3.66 5.06 3.38 4.24 3.82 3.13C3.82 3.13 4.49 2.92 6.02 3.95C6.66 3.77 7.34 3.68 8.02 3.68C8.7 3.68 9.38 3.77 10.02 3.95C11.55 2.91 12.22 3.13 12.22 3.13C12.66 4.24 12.38 5.06 12.3 5.26C12.8 5.82 13.12 6.52 13.12 7.4C13.12 10.47 11.25 11.15 9.47 11.35C9.76 11.6 10.01 12.08 10.01 12.83C10.01 13.9 10 14.76 10 15.21C10 15.42 10.15 15.67 10.56 15.59C13.7277 14.5265 16.0087 11.5363 16 8C16 3.58 12.42 0 8 0Z" })
          ),
          "GitHub"
        ),
      ),
      React.createElement("div", { className: "wb-about-card" },
        React.createElement("span", { className: "wb-about-label" }, t("settings.version")),
        React.createElement("strong", null, DATA.appVersion || "—"),
        React.createElement("a", { className: "wb-btn primary", href: REPO_ISSUES_URL, target: "_blank", rel: "noopener noreferrer" }, t("settings.reportIssue")),
      ),
      React.createElement("div", { className: "wb-about-card" },
        React.createElement("span", { className: "wb-about-label" }, t("settings.updates")),
        React.createElement(UpdateSection, { t: t }),
      ),
    ),
  );
}

// ── Update Section (inlined) ──
function UpdateSection({ t }) {
  var [checking, setChecking] = useStateSt(false);
  var [info, setInfo] = useStateSt(null);
  var [downloading, setDownloading] = useStateSt(false);
  var [progress, setProgress] = useStateSt({ downloaded: 0, total: 0, done: false });
  var [downloaded, setDownloaded] = useStateSt(false);
  var [error, setError] = useStateSt("");

  useEffectSt(function () { checkUpdate(); }, []);

  function checkUpdate() {
    setChecking(true); setError("");
    fetch("/api/update/check").then(function (r) { return r.json(); }).then(function (d) { setInfo(d); }).catch(function () { setError(t("settings.updateCheckFailed")); }).finally(function () { setChecking(false); });
  }

  function startDownload() {
    setDownloading(true); setError("");
    fetch("/api/update/download", { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) { if (d.ok) setDownloaded(true); else setError(d.error || t("settings.updateDownloadFailed")); }).catch(function () { setError(t("settings.updateDownloadFailed")); }).finally(function () { setDownloading(false); });
  }

  useEffectSt(function () {
    if (!downloading) return;
    var timer = setInterval(function () {
      fetch("/api/update/progress").then(function (r) { return r.json(); }).then(function (d) { setProgress(d); if (d.done) clearInterval(timer); }).catch(function () { clearInterval(timer); });
    }, 500);
    return function () { clearInterval(timer); };
  }, [downloading]);

  var lv = info && info.latest_version ? "v" + info.latest_version : "";
  var statusText = checking
    ? t("settings.updateChecking")
    : (info && info.update_available
      ? t("settings.updateAvailable")
      : (info ? t("settings.upToDate") : "—"));

  return React.createElement("div", { className: "wb-update-section" },
    React.createElement("strong", { className: "wb-update-status" }, statusText),
    error && React.createElement("p", { className: "wb-hint", style: { color: "var(--wb-red)" } }, error),
    React.createElement("button", {
      className: "wb-btn" + (downloaded ? " primary" : ""),
      disabled: checking || downloading,
      onClick: downloaded ? function () { fetch("/api/update/restart", { method: "POST" }).catch(function () {}); } : (info && info.update_available ? startDownload : checkUpdate),
    }, downloaded ? t("settings.updateRestartNow") : (checking ? t("settings.updateChecking") : (info && info.update_available ? t("settings.updateToVersion", { version: lv }) : t("settings.checkForUpdates")))),
    downloading && React.createElement("div", { className: "wb-progress-bar", style: { width: "100%" } },
      React.createElement("div", { style: { width: progress.total > 0 ? Math.round((progress.downloaded / progress.total) * 100) + "%" : "0%", height: 4, background: "var(--wb-blue)", borderRadius: 2, transition: "width 0.3s" } }),
    ),
  );
}

// ── Shared UI helpers ──

function SectionTitle(title, subtitle) {
  return React.createElement("div", { className: "wb-section-title" },
    React.createElement("h3", null, title),
    subtitle && React.createElement("p", null, subtitle),
  );
}

function SectionBlock(title, extra, children) {
  return React.createElement("div", { className: "wb-section-block" },
    React.createElement("div", { className: "wb-section-block-head" },
      React.createElement("b", null, title),
      typeof extra === "string" ? React.createElement("small", null, extra) : (extra || null),
    ),
    children,
  );
}

function FieldRow(label, hint, controls) {
  return React.createElement("div", { className: "wb-field" },
    React.createElement("div", { className: "wb-label" },
      label,
      hint && React.createElement("small", null, hint),
    ),
    React.createElement("div", { className: "wb-controls" }, controls),
  );
}

function Toggle(on, onClick) {
  return React.createElement("div", {
    className: "wb-toggle" + (on ? " on" : ""),
    onClick: onClick,
  });
}

function ModelCard(children) {
  return React.createElement("div", { className: "wb-model-card" }, children);
}

function ModelField(label, input) {
  return React.createElement("div", { className: "wb-model-line" },
    React.createElement("span", null, label),
    input,
  );
}

// ── Export ──
window.SettingsOverlay = SettingsOverlay;
