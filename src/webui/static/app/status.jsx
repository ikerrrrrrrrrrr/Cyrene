// Status page
function Sparkline({ data, color = "var(--accent)" }) {
  const w = 220, h = 50, pad = 4;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const d = "M " + pts.map((p) => p.join(" ")).join(" L ");
  const area =
    `M ${pts[0][0]} ${h - pad} L ` +
    pts.map((p) => p.join(" ")).join(" L ") +
    ` L ${pts[pts.length - 1][0]} ${h - pad} Z`;
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <path d={area} fill={color} opacity="0.12" />
      <path d={d} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

function StatusPage() {
  useDataVersion();
  const { t } = useI18n();
  const s = DATA.status;

  // 进化模式：显示状态标语，内容留白
  if (s.phase === "evolve") {
    return (
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
        height: "60vh", gap: 16, color: "var(--text-3)"
      }}>
        <div style={{ fontSize: 32, fontWeight: 700, color: "var(--accent)" }}>
          {s.state || "evolve"}
        </div>
        <div style={{ fontSize: 14, fontFamily: "var(--mono)" }}>
          /ˈiːvɒlv/
        </div>
        <div style={{ fontSize: 12, maxWidth: 360, textAlign: "center", lineHeight: 1.6 }}>
          {t("status.evolveHint")}
        </div>
      </div>
    );
  }

  if (!s.sparkData || !s.sparkData.length) s.sparkData = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];
  return (
    <div className="status-grid">
      {/* metric tiles */}
      {s.metrics.map((m, i) => (
        <div className="card" key={i} style={{ gridColumn: "span 3" }}>
          <div className="card-head">
            <span className="card-title">{m.label}</span>
            <span className="dot"></span>
          </div>
          <div className="metric-big">
            {m.value}<span className="metric-unit">{m.unit}</span>
          </div>
          <div className="metric-sub">
            <span className={m.delta === "up" ? "delta-up" : m.delta === "dn" ? "delta-dn" : ""}>
              {m.sub}
            </span>
          </div>
          <Sparkline
            data={s.sparkData.map((v) => v + Math.sin(i + v) * 2)}
            color={m.delta === "dn" ? "var(--err)" : "var(--accent)"}
          />
        </div>
      ))}

      {/* workers table */}
      <div className="card" style={{ gridColumn: "span 8" }}>
        <div className="card-head">
          <span className="card-title">{t("status.workers")}</span>
          <span className="card-action">{t("status.viewAll")}</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>{t("status.id")}</th><th>{t("status.role")}</th><th>{t("status.status")}</th>
              <th>{t("status.host")}</th><th>{t("status.uptime")}</th><th style={{ textAlign: "right" }}>{t("status.tokens")}</th>
              <th style={{ textAlign: "right" }}>{t("status.spend")}</th>
            </tr>
          </thead>
          <tbody>
            {s.workers.map((w) => (
              <tr key={w.id}>
                <td style={{ color: "var(--text)" }}>{w.id}</td>
                <td>{w.role}</td>
                <td>
                  <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                    <span className={"sa-dot " + w.status} style={{ marginTop: 0, width: 6, height: 6 }}></span>
                    {w.status}
                  </span>
                </td>
                <td>{w.host}</td>
                <td>{w.uptime}</td>
                <td style={{ textAlign: "right" }}>{w.tokens}</td>
                <td style={{ textAlign: "right", color: "var(--text)" }}>{w.spend}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* services */}
      <div className="card" style={{ gridColumn: "span 4" }}>
        <div className="card-head">
          <span className="card-title">{t("status.services")}</span>
          <span className="card-action">{t("status.refresh")}</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {s.services.map((svc, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "8px 10px", border: "1px solid var(--line)",
              borderRadius: "var(--r-m)", background: "var(--bg-2)", boxShadow: "var(--shadow-sm)"
            }}>
              <span className={"sa-dot " + (svc.status === "warn" ? "running" : "done")}
                    style={{ marginTop: 0 }}></span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text)" }}>
                {svc.name}
              </span>
              <span style={{
                marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 11,
                color: svc.status === "warn" ? "var(--warn)" : "var(--text-3)"
              }}>
                {svc.latency}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* logs */}
      <div className="card" style={{ gridColumn: "span 8" }}>
        <div className="card-head">
          <span className="card-title">{t("status.activityLog")}</span>
          <span className="card-action">{t("status.livePause")}</span>
        </div>
        <div style={{ maxHeight: 360, overflowY: "auto" }}>
          {s.logs.map((l, i) => (
            <div className="log-row" key={i}>
              <span className="t">{l.t}</span>
              <span className={"lvl " + l.lvl}>{l.lvl}</span>
              <span className="msg">{l.msg}</span>
            </div>
          ))}
        </div>
      </div>

      {/* config card (replaces hardcoded budget) */}
      <div className="card" style={{ gridColumn: "span 4" }}>
        <div className="card-head">
          <span className="card-title">{t("status.configuration")}</span>
        </div>
        <div style={{
          marginTop: 8, fontFamily: "var(--mono)", fontSize: 11.5,
          color: "var(--text-3)", display: "flex", flexDirection: "column", gap: 6
        }}>
          <div style={{ display: "flex" }}>
            <span>{t("status.model")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.model || "—"}</span>
          </div>
          <div style={{ display: "flex" }}>
            <span>{t("status.baseUrl")}</span>
            <span style={{ marginLeft: "auto", color: "var(--text)", maxWidth: 180,
                           overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {s.base_url || "—"}
            </span>
          </div>
          <div style={{ display: "flex" }}>
            <span>{t("status.soulMd")}</span>
            <span style={{ marginLeft: "auto", color: s.soul_exists ? "var(--accent)" : "var(--warn)" }}>
              {s.soul_exists ? t("status.loaded") : t("status.missing")}
            </span>
          </div>
          <div style={{ display: "flex" }}>
            <span>{t("status.scheduledTasks")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.scheduled_tasks ?? 0}</span>
          </div>
          <div style={{ display: "flex" }}>
            <span>{t("status.shortTermEntries")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.short_term_entries ?? 0}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

window.StatusPage = StatusPage;
