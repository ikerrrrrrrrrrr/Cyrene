// WeChat channel settings panel — full lifecycle in the frontend
const { useState: useStateSet, useEffect, useRef } = React;

const QR_MODAL_STYLE = {
  position: "fixed", inset: 0, zIndex: 9999,
  display: "flex", alignItems: "center", justifyContent: "center",
  background: "rgba(0,0,0,0.55)",
};
const QR_MODAL_BOX_STYLE = {
  background: "var(--bg, #fff)", borderRadius: 12, padding: 32,
  textAlign: "center", minWidth: 320, position: "relative",
};

function WeChatPanel() {
  const [connected, setConnected] = useStateSet(false);
  const [running, setRunning] = useStateSet(false);
  const [ownerWxid, setOwnerWxid] = useStateSet("");
  const [qrCode, setQrCode] = useStateSet(null);
  const [qrStatus, setQrStatus] = useStateSet("");
  const [wechatNotifyScheduled, setWechatNotifyScheduled] = useStateSet(true);
  const cancelRef = useRef(false);

  async function refreshStatus() {
    try {
      const r = await fetch("/api/wechat/status");
      const d = await r.json();
      setConnected(d.connected);
      setRunning(d.running);
      setOwnerWxid(d.owner_wxid || "");
    } catch (_) {}
  }

  useEffect(() => {
    refreshStatus();
    fetch("/api/settings/config").then((r) => r.json()).then((cfg) => {
      if (cfg && cfg.wechat_notify_scheduled !== undefined) {
        setWechatNotifyScheduled(cfg.wechat_notify_scheduled);
      }
    }).catch(() => {});
  }, []);

  // ── QR modal ────────────────────────────────────────────

  function closeModal() {
    cancelRef.current = true;
    setQrCode(null);
    setQrStatus("");
  }

  async function startLogin() {
    cancelRef.current = false;
    setQrStatus("正在获取二维码...");
    try {
      const r = await fetch("/api/wechat/qr-login", { method: "POST" });
      const d = await r.json();
      setQrCode("https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=" + encodeURIComponent(d.qrcode_img));
      setQrStatus("请使用微信扫描二维码");
      pollLogin(d.qrcode_id);
    } catch (e) {
      setQrStatus("获取二维码失败: " + e.message);
    }
  }

  async function pollLogin(qrcodeId) {
    for (let i = 0; i < 40; i++) {
      if (cancelRef.current) return;
      await new Promise((r) => setTimeout(r, 3000));
      if (cancelRef.current) return;
      try {
        const r = await fetch("/api/wechat/poll-login", {
          method: "POST",
          body: JSON.stringify({ qrcode_id: qrcodeId }),
          headers: { "Content-Type": "application/json" },
        });
        const d = await r.json();
        if (cancelRef.current) return;
        if (d.ok) {
          setQrStatus("登录成功！正在启动...");
          await fetch("/api/wechat/start", { method: "POST" });
          await refreshStatus();
          setQrCode(null);
          setQrStatus("");
          return;
        }
        if (d.expired) {
          setQrStatus("二维码已过期，请重新扫描");
          return;
        }
      } catch (e) {
        if (!cancelRef.current) {
          setQrStatus("连接失败: " + e.message);
        }
        return;
      }
    }
    if (!cancelRef.current) {
      setQrStatus("二维码已过期，请重试");
    }
  }

  // ── Manual start / stop ─────────────────────────────────

  async function handleStart() {
    try {
      const r = await fetch("/api/wechat/start", { method: "POST" });
      if ((await r.json()).ok) await refreshStatus();
    } catch (_) {}
  }

  async function handleStop() {
    try {
      await fetch("/api/wechat/stop", { method: "POST" });
      await refreshStatus();
    } catch (_) {}
  }

  // ── WeChat notification toggle ─────────────────────────

  async function toggleWechatNotify() {
    const next = !wechatNotifyScheduled;
    setWechatNotifyScheduled(next);
    try {
      await fetch("/api/settings/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wechat_notify_scheduled: next }),
      });
    } catch (_) {}
  }

  // ── Render ──────────────────────────────────────────────

  return (
    <section className="settings-channel-card settings-channel-card--wechat">
      <div className="settings-block-head settings-channel-card__head">
        <div className="settings-channel-title">
          <span className="settings-channel-icon" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.6 }}>
              <path d="M17 12.5C17 16.09 14.09 19 10.5 19C9.41 19 8.39 18.81 7.46 18.48L4 20L5.52 16.54C5.19 15.61 5 14.59 5 13.5C5 9.91 7.91 7 11.5 7C12.59 7 13.61 7.19 14.54 7.52" />
              <path d="M19 9.5C19 12.54 17.04 15.12 14.27 16.21L15.27 18.5L13 17.27C11.83 17.68 10.56 17.92 9.24 17.96" />
              <path d="M10 10.5C10 10.78 9.78 11 9.5 11C9.22 11 9 10.78 9 10.5C9 10.22 9.22 10 9.5 10C9.78 10 10 10.22 10 10.5Z" />
              <path d="M14 10.5C14 10.78 13.78 11 13.5 11C13.22 11 13 10.78 13 10.5C13 10.22 13.22 10 13.5 10C13.78 10 14 10.22 14 10.5Z" />
            </svg>
          </span>
          <div>
            <h3>微信</h3>
            <p>通过扫码连接本地微信桥接，适合日常通知和消息投递。</p>
          </div>
        </div>
        {running ? (
          <span className="settings-rank-chip wechat-chip--running">运行中</span>
        ) : connected ? (
          <span className="settings-rank-chip wechat-chip--stopped">已停止</span>
        ) : null}
      </div>

      <div className="settings-channel-card__body">
        <div className="settings-channel-status-row">
          <div className="settings-channel-status-copy">
            <span className="settings-inline-label">当前状态</span>
            <div className="settings-channel-status-value">
              <span className={"channel-status-dot " + (running ? "running" : (connected ? "stopped" : "off"))}></span>
              <strong>{connected ? (running ? "已连接并运行中" : "已连接，等待启动") : "未连接"}</strong>
            </div>
          </div>

          <div className="settings-channel-actions">
            {connected && running ? (
              <button className="btn danger" onClick={handleStop}>停止</button>
            ) : null}
            {connected && !running ? (
              <button className="btn primary" onClick={handleStart}>启动微信</button>
            ) : null}
            {!connected ? (
              <button className="btn primary" onClick={startLogin}>扫描二维码连接</button>
            ) : null}
          </div>
        </div>

        {qrStatus ? (
          <div className="wechat-status">{qrStatus}</div>
        ) : null}

        {/* ── Scheduled task notification toggle ─────── */}
        <div className="settings-channel-toggle-row">
          <label>发送定时任务通知到微信</label>
          <div
            className={"toggle " + (wechatNotifyScheduled ? "on" : "")}
            onClick={toggleWechatNotify}
            role="switch"
            aria-checked={wechatNotifyScheduled}
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") toggleWechatNotify(); }}
          ></div>
        </div>
      </div>

      {/* ── QR modal overlay ─────────────────────────── */}
      {qrCode ? (
        <div style={QR_MODAL_STYLE} onClick={closeModal}>
          <div style={QR_MODAL_BOX_STYLE} onClick={(e) => e.stopPropagation()}>
            <button
              onClick={closeModal}
              className="wechat-modal-close"
              title="关闭"
            >✕</button>
            <h4 style={{ margin: "0 0 16px" }}>扫描二维码连接微信</h4>
            <img src={qrCode} className="wechat-qr" alt="微信二维码" />
            <p style={{ marginTop: 16, color: "var(--text-2, #888)" }}>{qrStatus}</p>
          </div>
        </div>
      ) : null}
    </section>
  );
}

window.WeChatPanel = WeChatPanel;
