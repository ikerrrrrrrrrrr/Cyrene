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

  useEffect(() => { refreshStatus(); }, []);

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

  // ── Render ──────────────────────────────────────────────

  return (
    <div className="settings-subpane">
      <div className="settings-block-head">
        <div>
          <h3>微信</h3>
          <p>连接微信后，Agent 的回复会自动发送到微信，无需重启服务器。</p>
        </div>
        {running ? (
          <span className="settings-rank-chip wechat-chip--running">运行中</span>
        ) : connected ? (
          <span className="settings-rank-chip wechat-chip--stopped">已停止</span>
        ) : null}
      </div>

      {/* Connected + running */}
      {connected && running ? (
        <div className="field">
          <div className="label">已连接</div>
          <div className="settings-field-stack">
            <div className="wechat-wxid">{ownerWxid}</div>
            <div className="settings-actions">
              <button className="btn btn-danger" onClick={handleStop}>停止</button>
            </div>
          </div>
        </div>
      ) : null}

      {/* Connected but not running */}
      {connected && !running ? (
        <div className="field">
          <div className="label">Token 已就绪，未启动</div>
          <div className="settings-actions">
            <button className="btn" onClick={handleStart}>启动微信</button>
          </div>
        </div>
      ) : null}

      {/* Not connected */}
      {!connected ? (
        <div className="field">
          <div className="settings-actions">
            <button className="btn" onClick={startLogin}>扫描二维码连接</button>
          </div>
        </div>
      ) : null}

      {/* Inline status (expired, error, etc.) */}
      {qrStatus ? (
        <div className="wechat-status">{qrStatus}</div>
      ) : null}

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
    </div>
  );
}

window.WeChatPanel = WeChatPanel;
