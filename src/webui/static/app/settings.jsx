// Settings page
const { useState: useStateSet, useEffect } = React;

function readStoredTweak(key, fallback) {
  try { var v = localStorage.getItem("cyrene-tweak-" + key); return v !== null ? JSON.parse(v) : fallback; } catch(e) { return fallback; }
}

const REPO_URL = "https://github.com/ikerrrrrrrrrrr/Cyrene";
const REPO_ISSUES_URL = REPO_URL + "/issues/new";
const DEFAULT_MODEL_BASE_URL = "https://api.deepseek.com/v1";
const VALID_SETTINGS_SECTIONS = new Set([
  "general",
  "channels",
  "models",
  "agents",
  "appearance",
  "capabilities",
  "data",
  "about",
]);

function readStoredSettingsSection() {
  try {
    const section = localStorage.getItem("cyrene-settings-section");
    return VALID_SETTINGS_SECTIONS.has(section) ? section : "general";
  } catch (e) {
    return "general";
  }
}

function readStoredDesktopNotificationsEnabled() {
  try {
    return localStorage.getItem("cyrene-desktop-notifications") === "1";
  } catch (e) {
    return false;
  }
}

function createEmptyModelCandidate() {
  return {
    id: "candidate-" + Date.now() + "-" + Math.random().toString(16).slice(2, 6),
    name: "",
    model: "",
    desc: "",
    ctx: "",
    price: "",
    api_key: "",
    base_url: DEFAULT_MODEL_BASE_URL,
  };
}

function normalizeModelCandidate(raw, index, fallbackBaseUrl, fallbackApiKey) {
  const modelIdentifier = String(raw && (raw.model || raw.name || raw.id) || "").trim();
  return {
    id: String(raw && raw.id || ("candidate-" + (index + 1))).trim() || ("candidate-" + (index + 1)),
    name: modelIdentifier,
    model: modelIdentifier,
    desc: String(raw && raw.desc || "").trim(),
    ctx: String(raw && raw.ctx || "").trim(),
    price: String(raw && raw.price || "").trim(),
    api_key: String(raw && raw.api_key || fallbackApiKey || "").trim(),
    base_url: String(raw && raw.base_url || fallbackBaseUrl || DEFAULT_MODEL_BASE_URL).trim() || DEFAULT_MODEL_BASE_URL,
  };
}

function UpdateSection({ compact }) {
  const { t } = useI18n();
  const [checking, setChecking] = useStateSet(false);
  const [info, setInfo] = useStateSet(null);
  const [downloading, setDownloading] = useStateSet(false);
  const [progress, setProgress] = useStateSet({ downloaded: 0, total: 0, done: false });
  const [downloaded, setDownloaded] = useStateSet(false);
  const [error, setError] = useStateSet("");

  const checkUpdate = async () => {
    setChecking(true);
    setError("");
    try {
      const res = await fetch("/api/update/check");
      const data = await res.json();
      setInfo(data);
    } catch (e) {
      setError(t("settings.updateCheckFailed"));
    } finally {
      setChecking(false);
    }
  };

  const startDownload = async () => {
    setDownloading(true);
    setError("");
    try {
      const res = await fetch("/api/update/download", { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        setDownloaded(true);
      } else {
        setError(data.error || t("settings.updateDownloadFailed"));
      }
    } catch (e) {
      setError(t("settings.updateDownloadFailed"));
    } finally {
      setDownloading(false);
    }
  };

  const pollProgress = async () => {
    try {
      const res = await fetch("/api/update/progress");
      const data = await res.json();
      setProgress(data);
      return data.done;
    } catch (e) {
      return true;
    }
  };

  useEffect(() => {
    let timer;
    if (downloading) {
      timer = setInterval(async () => {
        const done = await pollProgress();
        if (done) clearInterval(timer);
      }, 500);
    }
    return () => { if (timer) clearInterval(timer); };
  }, [downloading]);

  const restart = async () => {
    try {
      await fetch("/api/update/restart", { method: "POST" });
    } catch (e) {
      // Process exit can race the request.
    }
  };

  useEffect(() => { checkUpdate(); }, []);

  const fmtSize = (bytes) => {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  };

  const currentVersionLabel = info && info.current_version
    ? "v" + info.current_version
    : (DATA.appVersion || "—");
  const latestVersionLabel = info && info.latest_version
    ? "v" + info.latest_version
    : "";

  const primaryAction = (() => {
    if (downloaded) {
      return {
        label: t("settings.updateRestartNow"),
        onClick: restart,
        disabled: false,
      };
    }
    if (downloading) {
      return {
        label: t("settings.updateDownloading") + " " + fmtSize(progress.downloaded) + " / " + fmtSize(progress.total),
        onClick: null,
        disabled: true,
      };
    }
    if (info && info.update_available) {
      return {
        label: t("settings.updateToVersion", { version: latestVersionLabel }),
        onClick: startDownload,
        disabled: false,
      };
    }
    return {
      label: checking ? t("settings.updateChecking") : t("settings.checkForUpdates"),
      onClick: checkUpdate,
      disabled: checking,
    };
  })();

  return (
    <div className={compact ? "settings-update-card" : "settings-subpane"}>
      <div className={compact ? "settings-update-stack" : "field"} style={{ flexDirection: "column", alignItems: "flex-start", gap: 8 }}>
        {compact ? (
          <div className="settings-update-compact-head">
            <strong>{info && info.update_available ? t("settings.updateToVersion", { version: latestVersionLabel }) : t("settings.checkForUpdates")}</strong>
            <small>
              {info
                ? (info.update_available ? t("settings.updateAvailable") : t("settings.upToDate"))
                : t("settings.updateChecking")}
            </small>
          </div>
        ) : (
          <div className="label">
            {t("settings.updates")}
            <small>
              {info
                ? currentVersionLabel + (info.update_available ? (" → " + latestVersionLabel + " " + t("settings.updateAvailable")) : (" (" + t("settings.upToDate") + ")"))
                : t("settings.updateChecking")}
            </small>
          </div>
        )}

        {error ? <div className="hint" style={{ color: "var(--red)" }}>{error}</div> : null}

        <button className="btn" disabled={primaryAction.disabled} onClick={primaryAction.onClick || undefined}>
          {primaryAction.label}
        </button>

        {downloading && progress.total > 0 ? (
          <div className="progress-bar" style={{ width: "100%", height: 4, background: "var(--border)", borderRadius: 2 }}>
            <div
              style={{
                width: Math.round((progress.downloaded / progress.total) * 100) + "%",
                height: "100%",
                background: "var(--accent)",
                borderRadius: 2,
                transition: "width 0.3s",
              }}
            />
          </div>
        ) : null}

        {downloaded ? (
          <span className="hint" style={{ color: "var(--green)" }}>{t("settings.updateDownloaded")}</span>
        ) : null}

        {info && info.update_available && !downloading && !downloaded ? (
          <span className="hint">
            {t("settings.updateAsset", { name: info.asset_name, size: fmtSize(info.asset_size) })}
          </span>
        ) : null}

        {info && !info.update_available ? (
          <span className="hint">{t("settings.updateLatest")}</span>
        ) : null}
      </div>
    </div>
  );
}

function SettingsPage({ tweaks, setTweak, actualTheme, accentPresets }) {
  useDataVersion();
  const { t, lang, setLang } = useI18n();
  const [section, setSection] = useStateSet(readStoredSettingsSection);
  const [config, setConfig] = useStateSet({
    model: "—",
    base_url: "—",
    assistant_name: "—",
    base_dir: "—",
    data_dir: "—",
    soul_path: "—",
    workspace_dir: "—",
    soul_content: "",
    search_mode: "builtin",
    search_external_url: "",
    spawn_policy: "conservative",
  });
  const [soulDraft, setSoulDraft] = useStateSet("");
  const [soulStatus, setSoulStatus] = useStateSet("");
  const [capabilityToggles, setCapabilityToggles] = useStateSet({
    streamThinking: readStoredTweak("cap-streamThinking", true),
    redactSecrets: readStoredTweak("cap-redactSecrets", true),
  });
  const [searchMode, setSearchMode] = useStateSet("builtin");
  const [searchExternalUrl, setSearchExternalUrl] = useStateSet("");
  const [searchSaved, setSearchSaved] = useStateSet("");
  const [models, setModels] = useStateSet([]);
  const [newModel, setNewModel] = useStateSet(createEmptyModelCandidate());
  const [visionModels, setVisionModels] = useStateSet([]);
  const [newVisionModel, setNewVisionModel] = useStateSet(createEmptyModelCandidate());
  const [modelsSaved, setModelsSaved] = useStateSet("");
  const [toolList, setToolList] = useStateSet([]);
  const [toolsSaved, setToolsSaved] = useStateSet("");
  const [mcpServers, setMcpServers] = useStateSet([]);
  const [mcpConfigs, setMcpConfigs] = useStateSet([]);
  const [mcpSaved, setMcpSaved] = useStateSet("");
  const [newMcpServer, setNewMcpServer] = useStateSet({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
  const [agentsSaved, setAgentsSaved] = useStateSet("");
  const [resetDataStatus, setResetDataStatus] = useStateSet("");
  const [resettingData, setResettingData] = useStateSet(false);
  const [backupList, setBackupList] = useStateSet([]);
  const [backupMsg, setBackupMsg] = useStateSet("");

  function formatBytes(bytes) {
    var n = Number(bytes || 0);
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  function formatDate(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
  }
  async function loadBackups() {
    try {
      var r = await fetch("/api/backup/list");
      var d = await r.json();
      if (d.ok) setBackupList(d.backups || []);
    } catch (e) { setBackupMsg("Failed to load backups: " + e.message); }
  }
  useEffect(function () { loadBackups(); }, []);
  const [desktopSettings, setDesktopSettings] = useStateSet({
    launchAtLogin: false,
    runInBackground: false,
    supportsLaunchAtLogin: false,
    platform: "",
  });
  const [desktopSettingsSaved, setDesktopSettingsSaved] = useStateSet("");
  const [telegramToken, setTelegramToken] = useStateSet("");
  const [telegramTokenSaved, setTelegramTokenSaved] = useStateSet("");
  const [desktopNotificationsEnabled, setDesktopNotificationsEnabled] = useStateSet(readStoredDesktopNotificationsEnabled);
  const [desktopNotificationStatus, setDesktopNotificationStatus] = useStateSet("");
  const [toolsExpanded, setToolsExpanded] = useStateSet(false);

  function toggleCapability(key) {
    var next = !capabilityToggles[key];
    setCapabilityToggles({ ...capabilityToggles, [key]: next });
    localStorage.setItem("cyrene-tweak-cap-" + key, JSON.stringify(next));
  }

  function updateModel(id, field, value) {
    setModels(models.map(function (model) {
      return model.id === id
        ? { ...model, [field]: value, name: field === "model" ? value : model.name }
        : model;
    }));
  }

  function moveModel(id, direction) {
    const index = models.findIndex(function (model) { return model.id === id; });
    const target = index + direction;
    if (index < 0 || target < 0 || target >= models.length) return;
    const next = models.slice();
    const current = next[index];
    next[index] = next[target];
    next[target] = current;
    setModels(next);
  }

  function deleteModel(id) {
    if (models.length <= 1) return;
    setModels(models.filter(function (model) { return model.id !== id; }));
  }

  function addModel() {
    const candidate = normalizeModelCandidate(newModel, models.length, "", "");
    if (!candidate.model) return;
    setModels(models.concat(candidate));
    setNewModel(createEmptyModelCandidate());
  }

  function addMcpServer() {
    const name = (newMcpServer.name || "").trim();
    if (!name) return;
    const server = {
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
    setMcpConfigs(mcpConfigs.filter(function (server) { return server.name !== name; }));
  }

  function toggleMcpServer(name) {
    setMcpConfigs(mcpConfigs.map(function (server) {
      return server.name === name ? { ...server, enabled: !server.enabled } : server;
    }));
  }

  function toggleTool(name) {
    setToolList(toolList.map(function (tool) {
      return tool.name === name ? { ...tool, enabled: !tool.enabled } : tool;
    }));
  }

  useEffect(() => {
    try {
      localStorage.setItem("cyrene-settings-section", section);
    } catch (e) {}
  }, [section]);

  useEffect(() => {
    try {
      localStorage.setItem("cyrene-desktop-notifications", desktopNotificationsEnabled ? "1" : "0");
    } catch (e) {}
  }, [desktopNotificationsEnabled]);

  useEffect(() => {
    fetch("/api/settings/config").then((r) => r.json()).then((payload) => {
      setConfig(payload);
      setSoulDraft(payload.soul_content || "");
      if (payload.search_mode) setSearchMode(payload.search_mode);
      if (payload.search_external_url !== undefined) setSearchExternalUrl(payload.search_external_url);
    }).catch(() => {});
    fetch("/api/settings/models").then((r) => r.json()).then((payload) => {
      const fallbackApiKey = "";
      const normalized = (payload.primary_candidates || payload.models || []).map(function (model, index) {
        return normalizeModelCandidate(model, index, payload.base_url || DEFAULT_MODEL_BASE_URL, fallbackApiKey);
      });
      const normalizedVision = (payload.vision_candidates || payload.vision_models || []).map(function (model, index) {
        return normalizeModelCandidate(model, index, payload.base_url || DEFAULT_MODEL_BASE_URL, fallbackApiKey);
      });
      setModels(normalized.length ? normalized : [normalizeModelCandidate({}, 0, payload.base_url || DEFAULT_MODEL_BASE_URL, "")]);
      setVisionModels(normalizedVision.length ? normalizedVision : [normalizeModelCandidate({}, 0, payload.base_url || DEFAULT_MODEL_BASE_URL, "")]);
    }).catch(() => {});
    fetch("/api/settings/tools").then((r) => r.json()).then((payload) => {
      setToolList(payload.tools || []);
    }).catch(() => {});
    fetch("/api/settings/mcp").then((r) => r.json()).then((payload) => {
      setMcpServers(payload.servers || []);
      setMcpConfigs(payload.configs || []);
    }).catch(() => {});
    fetch("/api/settings/keys").then((r) => r.json()).then((payload) => {
      const tokenMeta = (payload.keys || []).find(function (item) { return item.key === "TELEGRAM_BOT_TOKEN"; });
      setTelegramToken(tokenMeta && tokenMeta.value ? tokenMeta.value : "");
    }).catch(() => {});
    if (window.cyrene && typeof window.cyrene.getDesktopSettings === "function") {
      window.cyrene.getDesktopSettings().then((payload) => {
        if (payload) setDesktopSettings(payload);
      }).catch(() => {});
    }
  }, []);

  async function saveSoul() {
    setSoulStatus(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/soul", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: soulDraft }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setSoulStatus(t("settings.saved"));
      setTimeout(() => setSoulStatus(""), 1500);
    } catch (e) {
      setSoulStatus(t("settings.error") + ": " + e.message);
    }
  }

  async function saveSearch() {
    setSearchSaved(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/search", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ search_mode: searchMode, search_external_url: searchExternalUrl }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setSearchSaved(t("settings.saved"));
      setTimeout(() => setSearchSaved(""), 1500);
    } catch (e) {
      setSearchSaved(t("settings.error") + ": " + e.message);
    }
  }

  async function saveModels() {
    const normalized = models
      .map(function (model, index) {
        return normalizeModelCandidate(model, index, config.base_url || DEFAULT_MODEL_BASE_URL, "");
      })
      .filter(function (model) { return model.model; });
    const normalizedVision = visionModels
      .map(function (model, index) {
        return normalizeModelCandidate(model, index, config.base_url || DEFAULT_MODEL_BASE_URL, "");
      })
      .filter(function (model) { return model.model; });
    if (!normalized.length) {
      setModelsSaved(t("settings.modelCandidateRequired"));
      return;
    }
    if (!normalizedVision.length) {
      setModelsSaved(t("settings.modelCandidateRequired"));
      return;
    }
    setModelsSaved(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: normalized, vision_models: normalizedVision }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      const nextModels = (payload.primary_candidates || payload.models || normalized).map(function (model, index) {
        return normalizeModelCandidate(model, index, payload.base_url || DEFAULT_MODEL_BASE_URL, "");
      });
      const nextVisionModels = (payload.vision_candidates || payload.vision_models || normalizedVision).map(function (model, index) {
        return normalizeModelCandidate(model, index, payload.base_url || DEFAULT_MODEL_BASE_URL, "");
      });
      setModels(nextModels);
      setVisionModels(nextVisionModels);
      setConfig(function (previous) {
        return {
          ...previous,
          model: payload.active_model_name || previous.model,
          base_url: payload.base_url || previous.base_url,
        };
      });
      setModelsSaved(t("settings.saved"));
      setTimeout(() => setModelsSaved(""), 1500);
    } catch (e) {
      setModelsSaved(t("settings.error") + ": " + e.message);
    }
  }

  function updateVisionModel(id, field, value) {
    setVisionModels(visionModels.map(function (model) {
      return model.id === id
        ? { ...model, [field]: value, name: field === "model" ? value : model.name }
        : model;
    }));
  }

  function moveVisionModel(id, direction) {
    const index = visionModels.findIndex(function (model) { return model.id === id; });
    const target = index + direction;
    if (index < 0 || target < 0 || target >= visionModels.length) return;
    const next = visionModels.slice();
    const current = next[index];
    next[index] = next[target];
    next[target] = current;
    setVisionModels(next);
  }

  function deleteVisionModel(id) {
    if (visionModels.length <= 1) return;
    setVisionModels(visionModels.filter(function (model) { return model.id !== id; }));
  }

  function addVisionModel() {
    const candidate = normalizeModelCandidate(newVisionModel, visionModels.length, "", "");
    if (!candidate.model) return;
    setVisionModels(visionModels.concat(candidate));
    setNewVisionModel(createEmptyModelCandidate());
  }

  async function saveTools() {
    setToolsSaved(t("settings.saving"));
    try {
      const map = {};
      toolList.forEach(function (tool) { map[tool.name] = tool.enabled; });
      const response = await fetch("/api/settings/tools", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tools: map }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setToolsSaved(t("settings.saved"));
      setTimeout(() => setToolsSaved(""), 1500);
    } catch (e) {
      setToolsSaved(t("settings.error") + ": " + e.message);
    }
  }

  async function saveAgents() {
    setAgentsSaved(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spawn_policy: config.spawn_policy || "conservative" }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setAgentsSaved(t("settings.saved"));
      setTimeout(() => setAgentsSaved(""), 1500);
    } catch (e) {
      setAgentsSaved(t("settings.error") + ": " + e.message);
    }
  }

  async function saveDesktopPreferences(updates) {
    if (!window.cyrene || typeof window.cyrene.updateDesktopSettings !== "function") return;
    setDesktopSettingsSaved(t("settings.saving"));
    try {
      const next = await window.cyrene.updateDesktopSettings(updates);
      setDesktopSettings(next || desktopSettings);
      setDesktopSettingsSaved(t("settings.saved"));
      setTimeout(() => setDesktopSettingsSaved(""), 1500);
    } catch (e) {
      setDesktopSettingsSaved(t("settings.error"));
    }
  }

  async function saveTelegramToken() {
    if (!telegramToken || telegramToken.startsWith("••")) {
      setTelegramTokenSaved(t("settings.noChanges"));
      setTimeout(() => setTelegramTokenSaved(""), 1500);
      return;
    }
    setTelegramTokenSaved(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/keys", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ TELEGRAM_BOT_TOKEN: telegramToken }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setTelegramTokenSaved(t("settings.saved"));
      setTimeout(() => setTelegramTokenSaved(""), 1500);
    } catch (e) {
      setTelegramTokenSaved(t("settings.error") + ": " + e.message);
    }
  }

  async function saveMcpServers() {
    setMcpSaved(t("settings.saving"));
    try {
      const response = await fetch("/api/settings/mcp", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ servers: mcpConfigs }),
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      setMcpSaved(t("settings.saved"));
      fetch("/api/settings/mcp").then((r) => r.json()).then((payload) => {
        setMcpServers(payload.servers || []);
        setMcpConfigs(payload.configs || []);
      }).catch(() => {});
      setTimeout(() => setMcpSaved(""), 1500);
    } catch (e) {
      setMcpSaved(t("settings.error") + ": " + e.message);
    }
  }

  async function toggleDesktopNotifications() {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setDesktopNotificationStatus(t("settings.notificationsUnsupported"));
      return;
    }
    if (desktopNotificationsEnabled) {
      setDesktopNotificationsEnabled(false);
      setDesktopNotificationStatus(t("settings.notificationsDisabled"));
      setTimeout(() => setDesktopNotificationStatus(""), 1500);
      return;
    }
    if (Notification.permission === "granted") {
      setDesktopNotificationsEnabled(true);
      setDesktopNotificationStatus(t("settings.notificationsEnabled"));
      setTimeout(() => setDesktopNotificationStatus(""), 1500);
      return;
    }
    if (Notification.permission === "denied") {
      setDesktopNotificationStatus(t("settings.notificationsDenied"));
      return;
    }
    try {
      const permission = await Notification.requestPermission();
      if (permission === "granted") {
        setDesktopNotificationsEnabled(true);
        setDesktopNotificationStatus(t("settings.notificationsEnabled"));
      } else {
        setDesktopNotificationStatus(t("settings.notificationsDenied"));
      }
      setTimeout(() => setDesktopNotificationStatus(""), 1500);
    } catch (e) {
      setDesktopNotificationStatus(t("settings.notificationsUnsupported"));
    }
  }

  async function clearSession() {
    if (!confirm(t("settings.confirmClearSession"))) return;
    await fetch("/api/chat/clear", { method: "POST" });
    if (window.refreshSessions) window.refreshSessions();
    alert(t("settings.sessionCleared"));
  }

  async function resetAppData() {
    if (!confirm(t("settings.confirmResetAppData"))) return;
    setResettingData(true);
    setResetDataStatus(t("settings.resettingData"));
    try {
      const response = await fetch("/api/settings/reset-data", { method: "POST" });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || ("HTTP " + response.status));
      }
      try {
        Object.keys(localStorage).forEach(function (key) {
          if (key.indexOf("cyrene-") === 0) localStorage.removeItem(key);
        });
      } catch (e) {}
      window.location.reload();
    } catch (e) {
      setResetDataStatus(t("settings.resetAppDataFailed") + ": " + e.message);
      setResettingData(false);
    }
  }

  function openExternal(url) {
    window.open(url, "_blank", "noopener,noreferrer");
  }

  const sections = [
    { id: "general", label: t("section.general") },
    { id: "channels", label: t("section.channels") },
    { id: "models", label: t("section.models") },
    { id: "agents", label: t("section.agents") },
    { id: "appearance", label: t("section.appearance") },
    { id: "capabilities", label: t("section.capabilities") },
    { id: "data", label: t("section.data") },
    { id: "about", label: t("section.about") },
  ];
  const desktopIntegrationAvailable = !!(window.cyrene && typeof window.cyrene.updateDesktopSettings === "function");

  return (
    <div className="settings-layout">
      <div className="settings-nav">
        <div className="nav-section">{t("nav.settings")}</div>
        {sections.map(function (item) {
          return (
            <div
              key={item.id}
              className={"nav-item " + (section === item.id ? "active" : "")}
              onClick={() => setSection(item.id)}
            >
              {item.label}
            </div>
          );
        })}
      </div>

      <div className="settings-content">
        {section === "general" ? (
          <div className="settings-pane">
            <h2>{t("settings.general")}</h2>
            <p className="subtitle">{t("settings.generalSubtitle")}</p>

            <div className="settings-simple-list">
              <div className="field field--compact">
                <div className="label">{t("settings.launchAtLogin")}<small>{t("settings.launchAtLoginHint")}</small></div>
                <div className="settings-control-stack">
                  <div
                    className={"toggle " + (desktopSettings.launchAtLogin ? "on" : "") + ((!desktopIntegrationAvailable || !desktopSettings.supportsLaunchAtLogin) ? " disabled" : "")}
                    onClick={() => {
                      if (!desktopIntegrationAvailable || !desktopSettings.supportsLaunchAtLogin) return;
                      saveDesktopPreferences({ launchAtLogin: !desktopSettings.launchAtLogin });
                    }}
                    style={(!desktopIntegrationAvailable || !desktopSettings.supportsLaunchAtLogin) ? { opacity: 0.45, cursor: "not-allowed" } : {}}
                  ></div>
                  {!desktopIntegrationAvailable ? (
                    <span className="hint">{t("settings.desktopOnlySettingHint")}</span>
                  ) : (!desktopSettings.supportsLaunchAtLogin ? (
                    <span className="hint">{t("settings.launchAtLoginUnsupported")}</span>
                  ) : null)}
                </div>
              </div>

              <div className="field field--compact">
                <div className="label">{t("settings.runInBackground")}<small>{t("settings.runInBackgroundHint")}</small></div>
                <div className="settings-control-stack">
                  <div
                    className={"toggle " + (desktopSettings.runInBackground ? "on" : "") + (!desktopIntegrationAvailable ? " disabled" : "")}
                    onClick={() => {
                      if (!desktopIntegrationAvailable) return;
                      saveDesktopPreferences({ runInBackground: !desktopSettings.runInBackground });
                    }}
                    style={!desktopIntegrationAvailable ? { opacity: 0.45, cursor: "not-allowed" } : {}}
                  ></div>
                </div>
              </div>

              <div className="field field--compact">
                <div className="label">{t("settings.desktopNotifications")}<small>{t("settings.desktopNotificationsHint")}</small></div>
                <div className="settings-control-stack">
                  <div className={"toggle " + (desktopNotificationsEnabled ? "on" : "")} onClick={toggleDesktopNotifications}></div>
                  {desktopNotificationStatus ? <span className="hint">{desktopNotificationStatus}</span> : null}
                </div>
              </div>

              <div className="field field--compact">
                <div className="label">{t("settings.language")}<small>{t("settings.languageHint")}</small></div>
                <div className="seg">
                  <button className={"seg-btn " + (lang === "en" ? "active" : "")} onClick={() => setLang("en")}>English</button>
                  <button className={"seg-btn " + (lang === "zh" ? "active" : "")} onClick={() => setLang("zh")}>中文</button>
                </div>
              </div>
            </div>

            <div className="settings-actions">
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{desktopSettingsSaved}</span>
            </div>
          </div>
        ) : null}

        {section === "channels" ? (
          <div className="settings-pane">
            <div className="settings-block-head settings-block-head--channels">
              <div>
                <h3 style={{ margin: 0 }}>{t("settings.channels")}</h3>
                <p>{t("settings.channelsSubtitle")}</p>
              </div>
            </div>

            <div className="settings-channel-grid">
              <section className="settings-channel-card">
                <div className="settings-block-head settings-channel-card__head">
                  <div className="settings-channel-title">
                    <span className="settings-channel-icon" aria-hidden="true">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.6 }}>
                        <path d="M21.5 2.5L2.5 9.5L9.5 13.5" />
                        <path d="M21.5 2.5L14.5 21.5L9.5 13.5" />
                      </svg>
                    </span>
                    <div>
                      <h3>{t("settings.telegram")}</h3>
                      <p>{t("settings.telegramTokenHint")}</p>
                    </div>
                  </div>
                </div>

                <div className="settings-channel-card__body">
                  <label className="settings-inline-label" htmlFor="telegram-token-input">
                    {t("settings.telegramToken")}
                  </label>
                  <div className="settings-channel-input-row">
                    <input
                      id="telegram-token-input"
                      className="input mono settings-channel-input"
                      type="password"
                      value={telegramToken}
                      onChange={(e) => setTelegramToken(e.target.value)}
                      placeholder={t("settings.placeholderOptional")}
                    />
                    <button className="btn primary" onClick={saveTelegramToken}>{t("settings.saveNotification")}</button>
                  </div>
                  <div className="settings-channel-meta">
                    <span>{t("settings.placeholderOptional")}</span>
                    {telegramTokenSaved ? <span className="settings-saved-msg">{telegramTokenSaved}</span> : null}
                  </div>
                </div>
              </section>

              <WeChatPanel />
            </div>
          </div>
        ) : null}

        {section === "models" ? (
          <div className="settings-pane">
            <h2>{t("settings.models")}</h2>
            <p className="subtitle">{t("settings.modelsSubtitle")}</p>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.primaryModelSlot")}</h3>
                </div>
                <span className="settings-rank-chip">{t("settings.primaryModelRuntimeHint")}</span>
              </div>

              {models[0] ? (
                <div className="settings-primary-model-card">
                  <div className="settings-card-head">
                    <div>
                      <div className="model-name">{t("settings.primaryCandidateLive")}</div>
                      <div className="model-desc">{t("settings.primaryCandidateLiveHint")}</div>
                    </div>
                  </div>

                  <div className="settings-model-lines">
                    <div className="settings-model-line">
                      <span>{t("settings.modelIdentifierLabel")}</span>
                      <input
                        className="input mono"
                        value={models[0].model}
                        onChange={(e) => updateModel(models[0].id, "model", e.target.value)}
                        placeholder={t("settings.placeholderModelIdentifier")}
                      />
                    </div>
                    <div className="settings-model-line">
                      <span>{t("settings.apiKey")}</span>
                      <input
                        className="input mono"
                        type="password"
                        value={models[0].api_key}
                        onChange={(e) => updateModel(models[0].id, "api_key", e.target.value)}
                        placeholder="sk-..."
                      />
                    </div>
                    <div className="settings-model-line">
                      <span>{t("settings.baseUrlLabel")}</span>
                      <input
                        className="input mono"
                        value={models[0].base_url}
                        onChange={(e) => updateModel(models[0].id, "base_url", e.target.value)}
                        placeholder="https://api.deepseek.com/v1"
                      />
                    </div>
                  </div>

                  <div className="settings-form-grid settings-form-grid--meta">
                    <div className="settings-mini-field">
                      <span>{t("settings.descriptionLabel")}</span>
                      <input
                        className="input mono"
                        value={models[0].desc}
                        onChange={(e) => updateModel(models[0].id, "desc", e.target.value)}
                        placeholder={t("settings.placeholderDesc")}
                      />
                    </div>
                    <div className="settings-mini-field">
                      <span>{t("settings.contextLabel")}</span>
                      <input
                        className="input mono"
                        value={models[0].ctx}
                        onChange={(e) => updateModel(models[0].id, "ctx", e.target.value)}
                        placeholder={t("settings.placeholderCtx")}
                      />
                    </div>
                    <div className="settings-mini-field">
                      <span>{t("settings.priceLabel")}</span>
                      <input
                        className="input mono"
                        value={models[0].price}
                        onChange={(e) => updateModel(models[0].id, "price", e.target.value)}
                        placeholder={t("settings.placeholderPrice")}
                      />
                    </div>
                  </div>
                </div>
              ) : null}

              <div className="settings-subpane settings-subpane--spaced">
                <div className="settings-block-head">
                  <div>
                    <h3>{t("settings.fallbackCandidates")}</h3>
                  </div>
                  <button className="btn" onClick={addModel}>{t("settings.addFallbackCandidate")}</button>
                </div>

                <div className="settings-fallback-list">
                  {models.slice(1).concat([newModel]).map(function (model, index) {
                    const order = index + 1;
                    const isDraft = index === models.slice(1).length;
                    return (
                      <div key={model.id || "draft-fallback"} className={"settings-fallback-row" + (isDraft ? " is-draft" : "")}>
                        <div className="settings-fallback-head">
                          <div>
                            <div className="model-name">{t("settings.primaryCandidateFallback", { n: order })}</div>
                          </div>
                          {isDraft ? null : (
                          <div className="settings-card-actions">
                            <button className="iconbtn" title={t("settings.moveUp")} onClick={() => moveModel(model.id, -1)}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M10 15 V5" />
                                <path d="M6 9 L10 5 L14 9" />
                              </svg>
                            </button>
                            <button className="iconbtn" title={t("settings.moveDown")} onClick={() => moveModel(model.id, 1)} disabled={models.findIndex(function (item) { return item.id === model.id; }) === models.length - 1}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M10 5 V15" />
                                <path d="M6 11 L10 15 L14 11" />
                              </svg>
                            </button>
                            <button className="iconbtn" title={t("settings.deleteModel", { name: model.model || model.id })} onClick={() => deleteModel(model.id)}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                                <path d="M5 5 L15 15 M15 5 L5 15" />
                              </svg>
                            </button>
                          </div>
                          )}
                        </div>

                        <div className="settings-form-grid settings-form-grid--compact">
                          <div className="settings-mini-field">
                            <span>{t("settings.modelIdentifierLabel")}</span>
                            <input className="input mono" value={model.model} onChange={(e) => isDraft ? setNewModel({ ...newModel, model: e.target.value, name: e.target.value }) : updateModel(model.id, "model", e.target.value)} placeholder={t("settings.placeholderModelIdentifier")} />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.apiKey")}</span>
                            <input className="input mono" type="password" value={model.api_key} onChange={(e) => isDraft ? setNewModel({ ...newModel, api_key: e.target.value }) : updateModel(model.id, "api_key", e.target.value)} placeholder="sk-..." />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.baseUrlLabel")}</span>
                            <input className="input mono" value={model.base_url} onChange={(e) => isDraft ? setNewModel({ ...newModel, base_url: e.target.value }) : updateModel(model.id, "base_url", e.target.value)} placeholder="https://api.deepseek.com/v1" />
                          </div>
                        </div>

                        <div className="settings-form-grid settings-form-grid--meta settings-form-grid--compact">
                          <div className="settings-mini-field">
                            <span>{t("settings.descriptionLabel")}</span>
                            <input className="input mono" value={model.desc} onChange={(e) => isDraft ? setNewModel({ ...newModel, desc: e.target.value }) : updateModel(model.id, "desc", e.target.value)} placeholder={t("settings.placeholderDesc")} />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.contextLabel")}</span>
                            <input className="input mono" value={model.ctx} onChange={(e) => isDraft ? setNewModel({ ...newModel, ctx: e.target.value }) : updateModel(model.id, "ctx", e.target.value)} placeholder={t("settings.placeholderCtx")} />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.priceLabel")}</span>
                            <input className="input mono" value={model.price} onChange={(e) => isDraft ? setNewModel({ ...newModel, price: e.target.value }) : updateModel(model.id, "price", e.target.value)} placeholder={t("settings.placeholderPrice")} />
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="settings-actions">
                <button className="btn primary" onClick={saveModels}>{t("settings.saveApply")}</button>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{modelsSaved}</span>
              </div>
            </div>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.secondaryModelSlot")}</h3>
                  <p>{t("settings.placeholderSlotHint")}</p>
                </div>
              </div>
              <div className="settings-placeholder-card">{t("settings.secondaryModelPlaceholder")}</div>
            </div>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.visionModelSlot")}</h3>
                  <p>{t("settings.primaryCandidateLiveHint")}</p>
                </div>
              </div>

              {visionModels[0] ? (
                <div className="settings-primary-model-card">
                  <div className="settings-card-head">
                    <div>
                      <div className="model-name">{t("settings.primaryCandidateLive")}</div>
                      <div className="model-desc">{t("settings.primaryCandidateLiveHint")}</div>
                    </div>
                  </div>

                  <div className="settings-model-lines">
                    <div className="settings-model-line">
                      <span>{t("settings.modelIdentifierLabel")}</span>
                      <input className="input mono" value={visionModels[0].model} onChange={(e) => updateVisionModel(visionModels[0].id, "model", e.target.value)} placeholder={t("settings.placeholderModelIdentifier")} />
                    </div>
                    <div className="settings-model-line">
                      <span>{t("settings.apiKey")}</span>
                      <input className="input mono" type="password" value={visionModels[0].api_key} onChange={(e) => updateVisionModel(visionModels[0].id, "api_key", e.target.value)} placeholder="sk-..." />
                    </div>
                    <div className="settings-model-line">
                      <span>{t("settings.baseUrlLabel")}</span>
                      <input className="input mono" value={visionModels[0].base_url} onChange={(e) => updateVisionModel(visionModels[0].id, "base_url", e.target.value)} placeholder="https://api.deepseek.com/v1" />
                    </div>
                  </div>
                </div>
              ) : null}

              <div className="settings-subpane settings-subpane--spaced">
                <div className="settings-block-head">
                  <div>
                    <h3>{t("settings.fallbackCandidates")}</h3>
                  </div>
                  <button className="btn" onClick={addVisionModel}>{t("settings.addFallbackCandidate")}</button>
                </div>

                <div className="settings-fallback-list">
                  {visionModels.slice(1).concat([newVisionModel]).map(function (model, index) {
                    const order = index + 1;
                    const isDraft = index === visionModels.slice(1).length;
                    return (
                      <div key={model.id || "draft-vision-fallback"} className={"settings-fallback-row" + (isDraft ? " is-draft" : "")}>
                        <div className="settings-fallback-head">
                          <div>
                            <div className="model-name">{t("settings.primaryCandidateFallback", { n: order })}</div>
                          </div>
                          {isDraft ? null : (
                          <div className="settings-card-actions">
                            <button className="iconbtn" title={t("settings.moveUp")} onClick={() => moveVisionModel(model.id, -1)}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M10 15 V5" /><path d="M6 9 L10 5 L14 9" /></svg>
                            </button>
                            <button className="iconbtn" title={t("settings.moveDown")} onClick={() => moveVisionModel(model.id, 1)} disabled={visionModels.findIndex(function (item) { return item.id === model.id; }) === visionModels.length - 1}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M10 5 V15" /><path d="M6 11 L10 15 L14 11" /></svg>
                            </button>
                            <button className="iconbtn" title={t("settings.deleteModel", { name: model.model || model.id })} onClick={() => deleteVisionModel(model.id)}>
                              <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M5 5 L15 15 M15 5 L5 15" /></svg>
                            </button>
                          </div>
                          )}
                        </div>

                        <div className="settings-form-grid settings-form-grid--compact">
                          <div className="settings-mini-field">
                            <span>{t("settings.modelIdentifierLabel")}</span>
                            <input className="input mono" value={model.model} onChange={(e) => isDraft ? setNewVisionModel({ ...newVisionModel, model: e.target.value, name: e.target.value }) : updateVisionModel(model.id, "model", e.target.value)} placeholder={t("settings.placeholderModelIdentifier")} />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.apiKey")}</span>
                            <input className="input mono" type="password" value={model.api_key} onChange={(e) => isDraft ? setNewVisionModel({ ...newVisionModel, api_key: e.target.value }) : updateVisionModel(model.id, "api_key", e.target.value)} placeholder="sk-..." />
                          </div>
                          <div className="settings-mini-field">
                            <span>{t("settings.baseUrlLabel")}</span>
                            <input className="input mono" value={model.base_url} onChange={(e) => isDraft ? setNewVisionModel({ ...newVisionModel, base_url: e.target.value }) : updateVisionModel(model.id, "base_url", e.target.value)} placeholder="https://api.deepseek.com/v1" />
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {section === "agents" ? (
          <div className="settings-pane">
            <h2>{t("settings.agents")}</h2>
            <p className="subtitle">{t("settings.agentsSubtitle")}</p>

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
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{soulStatus || config.soul_path}</span>
              </div>
            </div>

            <div className="field">
              <div className="label">{t("settings.spawnPolicy")}<small>{t("settings.spawnPolicyHint")}</small></div>
              <select
                className="select"
                style={{ maxWidth: 320 }}
                value={config.spawn_policy || "conservative"}
                onChange={(e) => setConfig({ ...config, spawn_policy: e.target.value })}
              >
                <option value="aggressive">{t("settings.aggressive")}</option>
                <option value="conservative">{t("settings.conservative")}</option>
                <option value="off">{t("settings.off")}</option>
              </select>
            </div>

            <div className="field">
              <div className="label">{t("settings.streamReasoning")}<small>{t("settings.streamReasoningHint")}</small></div>
              <div className={"toggle " + (capabilityToggles.streamThinking ? "on" : "")} onClick={() => toggleCapability("streamThinking")}></div>
            </div>

            <div className="settings-actions">
              <button className="btn primary" onClick={saveAgents}>{t("settings.saveApply")}</button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{agentsSaved}</span>
            </div>
          </div>
        ) : null}

        {section === "appearance" ? (
          <div className="settings-pane">
            <h2>{t("settings.appearance")}</h2>
            <p className="subtitle">{t("settings.appearanceSubtitle")}</p>

            <div className="field">
              <div className="label">{t("settings.theme")}<small>{t("settings.themeHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.theme === "system" ? "active" : "")} onClick={() => setTweak && setTweak("theme", "system")}>{t("settings.system")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.theme === "light" ? "active" : "")} onClick={() => setTweak && setTweak("theme", "light")}>{t("settings.light")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.theme === "dark" ? "active" : "")} onClick={() => setTweak && setTweak("theme", "dark")}>{t("settings.dark")}</button>
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
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "default" ? "active" : "")} onClick={() => setTweak && setTweak("textSize", "default")}>
                  <span style={{ fontSize: 11 }}>A</span> {t("settings.default")}
                </button>
                <button className={"seg-btn " + (tweaks && tweaks.textSize === "large" ? "active" : "")} onClick={() => setTweak && setTweak("textSize", "large")}>
                  <span style={{ fontSize: 15 }}>A</span> {t("settings.large")}
                </button>
              </div>
            </div>

            <div className="field">
              <div className="label">{t("settings.density")}<small>{t("settings.densityHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.density === "cozy" ? "active" : "")} onClick={() => setTweak && setTweak("density", "cozy")}>{t("settings.cozy")}</button>
                <button className={"seg-btn " + (tweaks && tweaks.density === "compact" ? "active" : "")} onClick={() => setTweak && setTweak("density", "compact")}>{t("settings.compact")}</button>
              </div>
            </div>

            <div className="field">
              <div className="label">{t("settings.flowchartOrientation")}<small>{t("settings.flowchartOrientationHint")}</small></div>
              <div className="seg">
                <button className={"seg-btn " + (tweaks && tweaks.orientation === "horizontal" ? "active" : "")} onClick={() => setTweak && setTweak("orientation", "horizontal")}>
                  <svg width="22" height="14" viewBox="0 0 22 14" fill="none" stroke="currentColor" strokeWidth="1.4">
                    <rect x="1" y="4" width="5" height="6" rx="1" />
                    <rect x="9" y="4" width="5" height="6" rx="1" />
                    <rect x="17" y="4" width="4" height="6" rx="1" />
                    <path d="M6 7 L9 7 M14 7 L17 7" />
                  </svg>
                  {t("settings.horizontal")}
                </button>
                <button className={"seg-btn " + (tweaks && tweaks.orientation === "vertical" ? "active" : "")} onClick={() => setTweak && setTweak("orientation", "vertical")}>
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
              <div className={"toggle " + (tweaks && tweaks.showLegend ? "on" : "")} onClick={() => setTweak && setTweak("showLegend", !(tweaks && tweaks.showLegend))}></div>
            </div>

            <div className="field">
              <div className="label">{t("settings.pulseAnimation")}<small>{t("settings.pulseAnimationHint")}</small></div>
              <div className={"toggle " + (tweaks && tweaks.animatePulse ? "on" : "")} onClick={() => setTweak && setTweak("animatePulse", !(tweaks && tweaks.animatePulse))}></div>
            </div>
          </div>
        ) : null}

        {section === "capabilities" ? (
          <div className="settings-pane">
            <h2>{t("settings.capabilities")}</h2>
            <p className="subtitle">{t("settings.capabilitiesSubtitle")}</p>

            <div className="field">
              <div className="label">{t("settings.browserTools")}<small>{t("settings.browserToolsHint")}</small></div>
              <div className={"toggle " + (capabilityToggles.browserTools !== false ? "on" : "")} onClick={() => toggleCapability("browserTools")}></div>
            </div>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.webSearch")}</h3>
                  <p>{t("settings.webSearchSubtitle")}</p>
                </div>
              </div>

              <div className="field">
                <div className="label">{t("settings.searchBackend")}<small>{t("settings.searchBackendHint")}</small></div>
                <div className="seg">
                  <button className={"seg-btn " + (searchMode === "builtin" ? "active" : "")} onClick={() => setSearchMode("builtin")}>{t("settings.builtin")}</button>
                  <button className={"seg-btn " + (searchMode === "external" ? "active" : "")} onClick={() => setSearchMode("external")}>{t("settings.external")}</button>
                  <button className={"seg-btn " + (searchMode === "fallback" ? "active" : "")} onClick={() => setSearchMode("fallback")}>{t("settings.fallbackOnly")}</button>
                </div>
              </div>

              {searchMode === "external" ? (
                <div className="field">
                  <div className="label">{t("settings.externalUrl")}<small>{t("settings.externalUrlHint")}</small></div>
                  <input className="input mono" value={searchExternalUrl} onChange={(e) => setSearchExternalUrl(e.target.value)} placeholder="http://localhost:8888" style={{ maxWidth: 420 }} />
                </div>
              ) : null}

              {searchMode === "builtin" ? (
                <div className="field">
                  <div className="label">{t("settings.builtinStatus")}<small>{t("settings.builtinStatusHint", { port: config.search_port || "8888" })}</small></div>
                  <input className="input mono" value={t("settings.autoStarted")} readOnly style={{ maxWidth: 420 }} />
                </div>
              ) : null}

              {searchMode === "fallback" ? (
                <div className="field">
                  <div className="label">{t("settings.fallbackEngines")}<small>{t("settings.fallbackEnginesHint")}</small></div>
                  <input className="input mono" value={t("settings.fallbackDesc")} readOnly style={{ maxWidth: 420 }} />
                </div>
              ) : null}

              <div className="settings-actions">
                <button className="btn primary" onClick={saveSearch}>{t("settings.saveSearch")}</button>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{searchSaved}</span>
              </div>
            </div>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.mcpServers")}</h3>
                  <p>{t("settings.mcpSubtitle")}</p>
                </div>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {mcpConfigs.map(function (server) {
                  const live = mcpServers.find(function (item) { return item.name === server.name; });
                  const statusText = live ? live.status : "disconnected";
                  const toolCount = live ? live.tool_count : 0;
                  return (
                    <div key={server.name} className="model-card" style={{ flexWrap: "wrap" }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div className="model-name">
                          {server.name}
                          <span style={{ fontSize: 10, marginLeft: 6, color: "var(--text-4)" }}>({server.transport})</span>
                        </div>
                        <div className="model-desc">
                          {server.transport === "stdio" ? server.command + " " + (server.args || []).join(" ") : server.url}
                        </div>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontSize: 11, color: statusText === "connected" ? "var(--green)" : "var(--text-4)" }}>
                          {t("settings." + statusText)}{toolCount > 0 ? " · " + t("settings.toolsCount", { n: toolCount }) : ""}
                        </span>
                        <div className={"toggle " + (server.enabled !== false ? "on" : "")} onClick={() => toggleMcpServer(server.name)}></div>
                        <button className="iconbtn" title={t("settings.removeMcpServer", { name: server.name })} onClick={() => removeMcpServer(server.name)} style={{ color: "var(--text-4)" }}>
                          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
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
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{mcpSaved}</span>
              </div>

              <div className="settings-inline-form settings-inline-form--block">
                <input className="input mono" placeholder={t("settings.placeholderName")} value={newMcpServer.name} onChange={(e) => setNewMcpServer({ ...newMcpServer, name: e.target.value })} />
                <select className="select" value={newMcpServer.transport} onChange={(e) => setNewMcpServer({ ...newMcpServer, transport: e.target.value })}>
                  <option value="stdio">stdio</option>
                  <option value="sse">SSE</option>
                </select>
                {newMcpServer.transport === "stdio" ? (
                  <>
                    <input className="input mono" placeholder={t("settings.placeholderCommand")} value={newMcpServer.command} onChange={(e) => setNewMcpServer({ ...newMcpServer, command: e.target.value })} />
                    <input className="input mono" placeholder={t("settings.placeholderArgs")} value={newMcpServer.args} onChange={(e) => setNewMcpServer({ ...newMcpServer, args: e.target.value })} />
                  </>
                ) : (
                  <input className="input mono" placeholder={t("settings.placeholderMcpUrl")} value={newMcpServer.url} onChange={(e) => setNewMcpServer({ ...newMcpServer, url: e.target.value })} />
                )}
                <button className="btn" onClick={addMcpServer}>{t("settings.add")}</button>
              </div>
            </div>

            <div className="settings-subpane">
              <button className="settings-collapse-head" onClick={() => setToolsExpanded(!toolsExpanded)}>
                <div>
                  <h3>{t("settings.tools")}</h3>
                </div>
                <div className="settings-collapse-meta">
                  <span className="settings-collapse-label">{toolsExpanded ? t("settings.collapseTools") : t("settings.expandTools", { count: toolList.length })}</span>
                  <span className={"settings-collapse-icon" + (toolsExpanded ? " open" : "")}>⌄</span>
                </div>
              </button>

              {toolsExpanded ? (
                <>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {toolList.map(function (tool) {
                      return (
                        <div className="field" key={tool.name}>
                          <div className="label">
                            <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--accent)" }}>{tool.name}</span>
                            <small>{function (key) { const translated = t(key); return translated === key ? tool.desc : translated; }("tool.desc." + tool.name)}</small>
                          </div>
                          <div className={"toggle " + (tool.enabled ? "on" : "")} onClick={() => toggleTool(tool.name)}></div>
                        </div>
                      );
                    })}
                  </div>

                  <div className="settings-actions">
                    <button className="btn primary" onClick={saveTools}>{t("settings.saveTools")}</button>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{toolsSaved}</span>
                  </div>
                </>
              ) : null}
            </div>
          </div>
        ) : null}

        {section === "data" ? (
          <div className="settings-pane">
            <h2>{t("settings.data")}</h2>
            <p className="subtitle">{t("settings.dataSubtitle")}</p>

            <div className="field">
              <div className="label">{t("settings.redactSecrets")}<small>{t("settings.redactSecretsHint")}</small></div>
              <div className={"toggle " + (capabilityToggles.redactSecrets ? "on" : "")} onClick={() => toggleCapability("redactSecrets")}></div>
            </div>

            <div className="field">
              <div className="label">{t("settings.clearSession")}<small>{t("settings.clearSessionHint")}</small></div>
              <button className="btn danger" onClick={clearSession}>{t("settings.clearSessionBtn")}</button>
            </div>

            <div className="field">
              <div className="label">{t("settings.resetAppData")}<small>{t("settings.resetAppDataHint")}</small></div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <button className="btn danger" onClick={resetAppData} disabled={resettingData}>
                  {resettingData ? t("settings.resettingData") : t("settings.resetAppDataBtn")}
                </button>
                {resetDataStatus ? <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)" }}>{resetDataStatus}</span> : null}
              </div>
            </div>

            <div className="settings-subpane">
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.pathInfo")}</h3>
                  <p>{t("settings.pathInfoHint")}</p>
                </div>
              </div>

              <div className="field">
                <div className="label">{t("settings.baseDir")}<small>{t("settings.baseDirHint")}</small></div>
                <input className="input mono" value={config.base_dir} readOnly />
              </div>

              <div className="field">
                <div className="label">{t("settings.dataDir")}<small>{t("settings.dataDirHint")}</small></div>
                <input className="input mono" value={config.data_dir} readOnly />
              </div>

              <div className="field">
                <div className="label">{t("settings.workspaceDir")}<small>{t("settings.workspaceDirHint")}</small></div>
                <input className="input mono" value={config.workspace_dir} readOnly />
              </div>

              <div className="field">
                <div className="label">{t("settings.soulPath")}<small>{t("settings.soulPathHint")}</small></div>
                <input className="input mono" value={config.soul_path} readOnly />
              </div>
            </div>

            <div style={{ paddingTop: 16 }}>
              <div className="settings-block-head">
                <div>
                  <h3>{t("settings.backup")}</h3>
                  <p>{t("settings.backupSubtitle")}</p>
                </div>
              </div>

              <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
                <button className="btn primary" onClick={async function () {
                  setBackupMsg(t("settings.backupExporting"));
                  try {
                    var r = await fetch("/api/backup/export", {method:"POST"});
                    var d = await r.json();
                    if (d.ok) {
                      setBackupMsg(t("settings.backupExported",{n:d.entries.length,size:formatBytes(d.size)}));
                      loadBackups();
                      // Trigger browser download so the user can save to any location
                      var name = d.path.split("/").pop() || "cyrene_backup.zip";
                      var a = document.createElement("a");
                      a.href = "/api/backup/download/" + encodeURIComponent(name);
                      a.download = name;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                    } else throw new Error(d.error);
                  } catch(e) { setBackupMsg(t("settings.failed")+": "+e.message); }
                }}>{t("settings.backupExportBtn")}</button>
                <button className="btn" onClick={async function () {
                  if (!backupList.length) { setBackupMsg(t("settings.backupNoBackups")); return; }
                  var last = backupList[0];
                  try {
                    var r = await fetch("/api/backup/restore", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:last.path})});
                    var d = await r.json();
                    if (d.ok) setBackupMsg(t("settings.backupRestored",{n:d.restored.length}));
                    else throw new Error(d.error);
                  } catch(e) { setBackupMsg(t("settings.backupRestoreFailed")+": "+e.message); }
                }}>{t("settings.backupRestoreBtn")}</button>
              </div>

              {backupMsg ? <div style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--text-3)",marginBottom:16}}>{backupMsg}</div> : null}

              <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:8}}>
                <span style={{fontSize:13,color:"var(--text-3)"}}>{t("settings.backupHistoryHint")}</span>
                <button className="btn" style={{padding:"2px 10px",fontSize:11}} onClick={loadBackups}>{t("settings.refresh")}</button>
              </div>

              {backupList.length === 0 ? (
                <div style={{padding:"12px 0",color:"var(--text-4)",fontSize:13}}>{t("settings.backupNoBackups")}</div>
              ) : (
                <div style={{display:"flex",flexDirection:"column",gap:4}}>
                  {backupList.map(function(b){return <div key={b.name} style={{display:"flex",alignItems:"center",gap:10,padding:"8px 10px",borderRadius:"var(--r-m)",border:"1px solid var(--surface-border-flat)"}}>
                    <span style={{fontFamily:"var(--mono)",fontSize:12,flex:1,color:"var(--text-2)"}}>{b.name}</span>
                    <span style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--text-4)"}}>{formatBytes(b.size)}</span>
                    <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text-4)"}}>{formatDate(b.modified)}</span>
                    <a className="btn" href={"/api/backup/download/"+encodeURIComponent(b.name)} download style={{textDecoration:"none"}}>{t("settings.download")}</a>
                    <button className="btn danger" style={{padding:"2px 8px",fontSize:12}} onClick={async function(){if(!confirm(t("settings.backupDeleteConfirm"))) return; await fetch("/api/backup/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:b.name})}); loadBackups();}}>{t("settings.delete")}</button>
                  </div>})}
                </div>
              )}
            </div>
          </div>
        ) : null}

        {section === "about" ? (
          <div className="settings-pane">
            <h2>{t("settings.about")}</h2>
            <p className="subtitle">{t("settings.aboutSubtitle")}</p>

            <div className="settings-about-hero">
              <div>
                <div className="settings-about-eyebrow">Cyrene</div>
                <h3>{t("settings.aboutHeroTitle")}</h3>
                <p>{t("settings.aboutHeroCopy")}</p>
              </div>
              <div className="settings-about-actions">
                <button className="btn" onClick={() => openExternal(REPO_URL)}>{t("settings.openGithub")}</button>
                <button className="btn primary" onClick={() => openExternal(REPO_ISSUES_URL)}>{t("settings.reportIssue")}</button>
              </div>
            </div>

            <div className="settings-about-grid">
              <div className="settings-about-card">
                <span>{t("settings.projectName")}</span>
                <strong>Cyrene</strong>
                <small>{t("settings.projectNameHint")}</small>
              </div>
              <div className="settings-about-card">
                <span>{t("settings.version")}</span>
                <strong>{DATA.appVersion || "—"}</strong>
                <small>{t("settings.versionHint")}</small>
              </div>
              <div className="settings-about-card">
                <span>{t("settings.updates")}</span>
                <UpdateSection compact />
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
