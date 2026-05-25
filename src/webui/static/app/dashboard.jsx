// Dashboard page — data-first homepage with charts, usage, reminders, and memory activity
function formatRelativeDateLabel(dateStr) {
  if (!dateStr) return "—";
  const dt = new Date(dateStr);
  if (Number.isNaN(dt.getTime())) return dateStr;
  return dt.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
}

function formatWeekdayLabel(dateStr) {
  if (!dateStr) return "—";
  const dt = new Date(dateStr);
  if (Number.isNaN(dt.getTime())) return dateStr;
  return dt.toLocaleDateString(undefined, { weekday: "short" });
}

function formatDateTimeLabel(dateStr) {
  if (!dateStr) return "—";
  const dt = new Date(dateStr);
  if (Number.isNaN(dt.getTime())) return dateStr;
  return dt.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function compactNumber(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(Math.round(n));
}

function Sparkline({ data, color }) {
  if (!data || data.length < 2) return null;
  const w = 220, h = 50, pad = 4;
  const min = Math.min.apply(null, data), max = Math.max.apply(null, data);
  const range = max - min || 1;
  const pts = data.map(function(v, i) {
    var x = pad + (i / (data.length - 1)) * (w - pad * 2);
    var y = h - pad - ((v - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const d = "M " + pts.map(function(p) { return p.join(" "); }).join(" L ");
  const area = "M " + pts[0][0] + " " + (h - pad) + " L " + pts.map(function(p) { return p.join(" "); }).join(" L ") + " L " + pts[pts.length - 1][0] + " " + (h - pad) + " Z";
  return (
    <svg className="spark" viewBox={"0 0 " + w + " " + h} preserveAspectRatio="none">
      <path d={area} fill={color || "var(--accent)"} opacity="0.12" />
      <path d={d} fill="none" stroke={color || "var(--accent)"} strokeWidth="1.5" />
    </svg>
  );
}

function DashboardTokenChart({ timeline }) {
  const { t } = useI18n();
  const data = Array.isArray(timeline) ? timeline : [];
  const w = 760;
  const h = 264;
  const padX = 34;
  const padTop = 20;
  const padBottom = 40;
  const plotHeight = h - padTop - padBottom;
  const maxValue = data.reduce((max, item) => Math.max(max, (item.prompt || 0) + (item.completion || 0)), 1);
  const floorPadding = Math.max(maxValue * 0.08, 1);
  const chartMax = maxValue + floorPadding * 0.18;
  const chartRange = chartMax + floorPadding;
  const promptCoords = data.map((item, index) => {
    const x = padX + (index / Math.max(1, data.length - 1)) * (w - padX * 2);
    const prompt = item.prompt || 0;
    const completion = item.completion || 0;
    const stacked = prompt + completion;
    const yStacked = h - padBottom - ((stacked + floorPadding) / chartRange) * plotHeight;
    const yPrompt = h - padBottom - ((prompt + floorPadding) / chartRange) * plotHeight;
    return { x, yStacked, yPrompt, item };
  });
  const stackedLine = promptCoords.length ? "M " + promptCoords.map((p) => `${p.x} ${p.yStacked}`).join(" L ") : "";
  const promptLine = promptCoords.length ? "M " + promptCoords.map((p) => `${p.x} ${p.yPrompt}`).join(" L ") : "";
  const stackedArea = promptCoords.length
    ? `M ${promptCoords[0].x} ${h - padBottom} L ${promptCoords.map((p) => `${p.x} ${p.yStacked}`).join(" L ")} L ${promptCoords[promptCoords.length - 1].x} ${h - padBottom} Z`
    : "";
  const promptArea = promptCoords.length
    ? `M ${promptCoords[0].x} ${h - padBottom} L ${promptCoords.map((p) => `${p.x} ${p.yPrompt}`).join(" L ")} L ${promptCoords[promptCoords.length - 1].x} ${h - padBottom} Z`
    : "";
  const maxDateLabels = 6;
  const labelStep = Math.max(1, Math.ceil((promptCoords.length - 1) / Math.max(1, maxDateLabels - 1)));
  function shouldShowDateLabel(index) {
    if (promptCoords.length <= maxDateLabels) return true;
    return index === 0 || index === promptCoords.length - 1 || index % labelStep === 0;
  }

  return (
    <div className="dashboard-token-chart">
      <div className="dashboard-legend">
        <span><i className="swatch prompt"></i>{t("dashboard.input")}</span>
        <span><i className="swatch completion"></i>{t("dashboard.output")}</span>
      </div>
      <div className="dashboard-token-stage">
        <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
          {[0.25, 0.5, 0.75, 1].map((ratio) => {
            const y = h - padBottom - ratio * (h - padTop - padBottom);
            return <line key={ratio} x1={padX} y1={y} x2={w - padX} y2={y} className="dashboard-grid-line" />;
          })}
          {stackedArea ? <path d={stackedArea} className="dashboard-area completion" /> : null}
          {promptArea ? <path d={promptArea} className="dashboard-area prompt" /> : null}
          {stackedLine ? <path d={stackedLine} className="dashboard-line completion" /> : null}
          {promptLine ? <path d={promptLine} className="dashboard-line prompt" /> : null}
        </svg>
        {promptCoords.map((point, index) => (
          <div key={point.item.date} className="dashboard-token-overlay">
            <span
              className="dashboard-point completion"
              style={{ left: `${(point.x / w) * 100}%`, top: `${(point.yStacked / h) * 100}%` }}
            ></span>
            <span
              className="dashboard-point prompt"
              style={{ left: `${(point.x / w) * 100}%`, top: `${(point.yPrompt / h) * 100}%` }}
            ></span>
            {shouldShowDateLabel(index) && (
              <span
                className="dashboard-axis-label dashboard-axis-label-token"
                style={{ left: `${(point.x / w) * 100}%`, top: `${((h - 18) / h) * 100}%` }}
              >
                {formatRelativeDateLabel(point.item.date)}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function DashboardEmotionChart({ series }) {
  const points = Array.isArray(series) ? series : [];
  const w = 760;
  const h = 190;
  const padX = 28;
  const padY = 18;
  const min = -5;
  const max = 5;
  const range = max - min;
  const baseline = h - padY - ((0 - min) / range) * (h - padY * 2);
  const coords = points.map((item, index) => {
    const x = padX + (index / Math.max(1, points.length - 1)) * (w - padX * 2);
    const y = h - padY - (((item.value || 0) - min) / range) * (h - padY * 2);
    return { x, y, item };
  });
  const line = coords.length ? "M " + coords.map((p) => `${p.x} ${p.y}`).join(" L ") : "";
  return (
    <div className="dashboard-emotion-chart">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        <line x1={padX} y1={baseline} x2={w - padX} y2={baseline} className="dashboard-grid-line baseline" />
        {[0.2, 0.4, 0.6, 0.8].map((ratio) => {
          const y = padY + ratio * (h - padY * 2);
          return <line key={ratio} x1={padX} y1={y} x2={w - padX} y2={y} className="dashboard-grid-line" />;
        })}
        {line ? <path d={line} className="dashboard-line emotion" /> : null}
        {coords.map((point) => (
          <g key={point.item.date}>
            <circle cx={point.x} cy={point.y} r="4" className="dashboard-point emotion" />
            <text x={point.x} y={h - 2} textAnchor="middle" className="dashboard-axis-label">
              {formatRelativeDateLabel(point.item.date)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function DashboardRadialBreakdown({ hit, miss }) {
  const { t } = useI18n();
  const hitValue = Number(hit || 0);
  const missValue = Number(miss || 0);
  const total = Math.max(1, hitValue + missValue);
  const r = 52;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - hitValue / total);
  return (
    <div className="dashboard-radial">
      <svg viewBox="0 0 140 140">
        <circle cx="70" cy="70" r={r} className="dashboard-radial-track" />
        <circle cx="70" cy="70" r={r} className="dashboard-radial-hit" strokeDasharray={c} strokeDashoffset={offset} transform="rotate(-90 70 70)" />
        <circle cx="70" cy="70" r="34" className="dashboard-radial-hole" />
        <text x="70" y="64" textAnchor="middle" className="dashboard-radial-value">
          {Math.round((hitValue / total) * 100)}%
        </text>
        <text x="70" y="82" textAnchor="middle" className="dashboard-radial-label">
          {t("dashboard.cacheHit")}
        </text>
      </svg>
      <div className="dashboard-radial-meta">
        <div><i className="swatch hit"></i>{compactNumber(hitValue)}</div>
        <div><i className="swatch miss"></i>{compactNumber(missValue)}</div>
      </div>
    </div>
  );
}

function DashboardTopicBars({ topics }) {
  const data = (topics || []).slice(0, 7);
  const max = data.reduce((value, item) => Math.max(value, item.count || 0), 1);
  return (
    <div className="dashboard-topic-bars">
      {data.length ? data.map((item) => (
        <div key={item.term} className="dashboard-topic-row">
          <span className="dashboard-topic-label">{item.term}</span>
          <div className="dashboard-topic-track">
            <div className="dashboard-topic-fill" style={{ width: ((item.count || 0) / max) * 100 + "%" }}></div>
          </div>
          <span className="dashboard-topic-value">{item.count}</span>
        </div>
      )) : <div className="dashboard-empty">—</div>}
    </div>
  );
}

function DashboardHeatmap({ data }) {
  const days = Array.isArray(data && data.days) ? data.days : [];
  const rows = Array.isArray(data && data.rows) ? data.rows : [];
  const max = rows.reduce((value, row) => Math.max(value, ...(row.values || [0])), 1);
  return (
    <div className="dashboard-heatmap">
      <div className="dashboard-heatmap-days">
        <span></span>
        {days.map((day) => (
          <span key={day} className="dashboard-heatmap-day">
            <strong>{formatWeekdayLabel(day)}</strong>
            <small>{formatRelativeDateLabel(day)}</small>
          </span>
        ))}
      </div>
      <div className="dashboard-heatmap-body">
        {rows.map((row) => (
          <div key={row.label} className="dashboard-heatmap-row">
            <span className="dashboard-heatmap-time">{row.label}</span>
            {(row.values || []).map((value, index) => {
              const ratio = max ? value / max : 0;
              return (
                <div
                  key={row.label + ":" + days[index]}
                  className="dashboard-heat-cell"
                  title={(days[index] || "") + " " + row.label + " · " + value}
                  style={{ opacity: 0.12 + ratio * 0.88 }}
                ></div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function DashboardPage() {
  useDataVersion();
  const { t } = useI18n();
  const dash = DATA.dashboard || {};
  const today = dash.today || {};
  const soul = dash.soul || {};
  const topics = Array.isArray(dash.topic_cloud) ? dash.topic_cloud : [];
  const emotion = Array.isArray(dash.emotion) ? dash.emotion : [];
  const usage = dash.usage || {};
  const timeline = Array.isArray(usage.timeline) ? usage.timeline : [];
  const reminders = Array.isArray(dash.reminders) ? dash.reminders : [];
  const memories = Array.isArray(dash.recent_memories) ? dash.recent_memories : [];
  const archive = Array.isArray(dash.recent_archive) ? dash.recent_archive : [];
  const heatmap = dash.activity_heatmap || { days: [], rows: [] };
  const s = DATA.status || {};
  const modelStats = Array.isArray(dash.model_stats) ? dash.model_stats : [];
  var modelUsageEl = modelStats.length > 0 ? React.createElement("div", {className:"dashboard-model-usage", style:{marginTop:6,borderTop:"1px solid var(--line)",paddingTop:8}},
    React.createElement("div", {style:{fontSize:10.5,color:"var(--text-4)",marginBottom:6,fontFamily:"var(--mono)",textTransform:"uppercase",letterSpacing:"0.04em"}}, "By model"),
    modelStats.map(function(r,i){return React.createElement("div", {key:r.model||i, style:{display:"flex",alignItems:"center",gap:6,padding:"2px 0",fontSize:12}},
      React.createElement("span", {style:{flex:1,fontFamily:"var(--mono)",fontSize:11,color:"var(--text-2)",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}, r.model),
      React.createElement("span", {style:{fontFamily:"var(--mono)",fontSize:11,color:"var(--text-3)",minWidth:48,textAlign:"right"}}, r.requests),
      React.createElement("span", {style:{fontFamily:"var(--mono)",fontSize:11,color:"var(--text-3)",minWidth:64,textAlign:"right"}}, (Number(r.prompt_tokens||0)+Number(r.completion_tokens||0)))
    )})
  ) : null;

  return (
    <div className="dashboard-shell">
      <section className="dashboard-top">
        <div className="dashboard-hero-panel">
          <div className="dashboard-topline">{DATA.assistantName || "Cyrene"}</div>
          <h1>{t("dashboard.title", { name: DATA.assistantName || "Cyrene" })}</h1>
          <div className="dashboard-metric-row">
            <div className="dashboard-metric-card">
              <span>{t("dashboard.learnedToday")}</span>
              <strong>{today.learned_count || 0}</strong>
            </div>
            <div className="dashboard-metric-card">
              <span>{t("dashboard.requests")}</span>
              <strong>{usage.requests ?? 0}</strong>
            </div>
            <div className="dashboard-metric-card">
              <span>{t("dashboard.tokensLabel")}</span>
              <strong>{compactNumber(usage.total_tokens || 0)}</strong>
            </div>
            <div className="dashboard-metric-card">
              <span>{t("dashboard.archiveDays")}</span>
              <strong>{today.archive_days || 0}</strong>
            </div>
          </div>
          {(s.metrics || []).length ? <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            {s.metrics.map(function(m, i) {
              var labelKey = "status.metric." + m.label.replace(/[^a-zA-Z0-9]/g, "");
              var label = t(labelKey);
              if (label === labelKey) label = m.label;
              var subText = m.sub;
              var match = m.sub.match(/^(\d+)\s+(.+)/);
              if (match) {
                var subKey = "status.metricSub." + match[2].replace(/[^a-zA-Z0-9]/g, "");
                var subTrans = t(subKey);
                subText = match[1] + " " + (subTrans !== subKey ? subTrans : match[2]);
              } else {
                var subKey = "status.metricSub." + m.sub.replace(/[^a-zA-Z0-9]/g, "");
                var subTrans = t(subKey);
                if (subTrans !== subKey) subText = subTrans;
              }
              return (
                <div className="card" key={i} style={{ gridColumn: "span 3", flex: 1 }}>
                  <div className="card-head"><span className="card-title">{label}</span><span className="dot"></span></div>
                  <div className="metric-big">{m.value}<span className="metric-unit">{m.unit}</span></div>
                  <div className="metric-sub"><span className={m.delta === "up" ? "delta-up" : m.delta === "dn" ? "delta-dn" : ""}>{subText}</span></div>
                  <Sparkline data={(s.sparkData || []).map(function(v) { return v + Math.sin(i + v) * 2; })} color={m.delta === "dn" ? "var(--err)" : "var(--accent)"} />
                </div>
              );
            })}
          </div> : null}
        </div>

        <aside className="dashboard-side-panel">
          <div className="dashboard-card-head">
            <span>{t("dashboard.soulRecent")}</span>
            <small>{formatDateTimeLabel(soul.updated_at)}</small>
          </div>
          <div className="dashboard-change-log">
            {(soul.recent_items || []).length ? soul.recent_items.map((item, index) => (
              <div key={index} className="dashboard-change-item">
                <i></i>
                <span>{item.replace(/^- /, "")}</span>
              </div>
            )) : <div className="dashboard-empty">{t("dashboard.emptySoul")}</div>}
          </div>
        </aside>
      </section>

      <section className="dashboard-main-grid">
        <div className="dashboard-panel token" style={{ gridArea: "token" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.usage")}</span>
            <small>{compactNumber(usage.prompt_tokens || 0)} {t("dashboard.input")} / {compactNumber(usage.completion_tokens || 0)} {t("dashboard.output")} / {compactNumber(usage.total_tokens || 0)} {t("dashboard.total")}</small>
          </div>
          <DashboardTokenChart timeline={timeline} />
          <div className="dashboard-token-footer">
            <div className="dashboard-stat-pair"><span>{t("dashboard.requests")}</span><strong>{usage.requests ?? "—"}</strong></div>
            <div className="dashboard-stat-pair"><span>{t("dashboard.spend")}</span><strong>{usage.spend || "—"}</strong></div>
            <div className="dashboard-stat-pair"><span>{t("dashboard.input")}</span><strong>{compactNumber(usage.prompt_tokens || 0)}</strong></div>
            <div className="dashboard-stat-pair"><span>{t("dashboard.output")}</span><strong>{compactNumber(usage.completion_tokens || 0)}</strong></div>
          </div>
          {modelUsageEl}
        </div>

        <div className="dashboard-panel breakdown" style={{ gridArea: "breakdown" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.cache")}</span>
            <small>{compactNumber((usage.cache_hit_tokens || 0) + (usage.cache_miss_tokens || 0))}</small>
          </div>
          <DashboardRadialBreakdown hit={usage.cache_hit_tokens} miss={usage.cache_miss_tokens} />
        </div>

        <div className="dashboard-panel emotion" style={{ gridArea: "emotion" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.emotion")}</span>
          </div>
          <DashboardEmotionChart series={emotion} />
        </div>

        <div className="dashboard-panel topics" style={{ gridArea: "topics" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.topicCloud")}</span>
          </div>
          <DashboardTopicBars topics={topics} />
        </div>

        <div className="dashboard-panel activity" style={{ gridArea: "activity" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.activity")}</span>
          </div>
          <DashboardHeatmap data={heatmap} />
        </div>

        <div className="dashboard-panel reminders" style={{ gridArea: "reminders" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.reminders")}</span>
          </div>
          <div className="dashboard-reminder-list">
            {reminders.length ? reminders.map((item) => (
              <div key={item.id} className="dashboard-reminder-item">
                <time>{formatDateTimeLabel(item.next_run)}</time>
                <span>{item.prompt}</span>
              </div>
            )) : <div className="dashboard-empty">{t("dashboard.emptyReminders")}</div>}
          </div>
        </div>

        <div className="dashboard-panel memories" style={{ gridArea: "memories" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.recentMemories")}</span>
          </div>
          <div className="dashboard-feed">
            {memories.length ? memories.slice(0, 5).map((entry, index) => (
              <div key={index} className="dashboard-feed-item">
                <div className="dashboard-feed-meta">
                  <span>{formatRelativeDateLabel(entry.last_mentioned)}</span>
                  <span>{t("dashboard.mentions", { n: entry.mention_count || 0 })}</span>
                </div>
                <p>{entry.content}</p>
              </div>
            )) : <div className="dashboard-empty">{t("dashboard.emptyMemories")}</div>}
          </div>
        </div>

        <div className="dashboard-panel today" style={{ gridArea: "today" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.todayState")}</span>
          </div>
          <div className="dashboard-feed">
            {(today.learned || []).length ? today.learned.map((entry, index) => (
              <div key={index} className="dashboard-feed-item compact">
                <div className="dashboard-feed-meta">
                  <span className={"type-chip " + (entry.type || "fact")}>{entry.type || "fact"}</span>
                  <span>{t("dashboard.valence", { n: entry.emotional_valence || 0 })}</span>
                </div>
                <p>{entry.content}</p>
              </div>
            )) : <div className="dashboard-empty">{t("dashboard.emptyToday")}</div>}
          </div>
        </div>

        <div className="dashboard-panel archive" style={{ gridArea: "archive" }}>
          <div className="dashboard-card-head">
            <span>{t("dashboard.recentArchive")}</span>
          </div>
          <div className="dashboard-feed">
            {archive.length ? archive.slice(0, 4).map((item, index) => (
              <div key={index} className="dashboard-feed-item compact">
                <div className="dashboard-feed-meta">
                  <span>{item.title || t("dashboard.archiveFallback")}</span>
                  <span>{formatRelativeDateLabel(item.date)}</span>
                </div>
                <p>{item.user || item.assistant}</p>
              </div>
            )) : <div className="dashboard-empty">{t("dashboard.emptyArchive")}</div>}
          </div>
        </div>
        {/* Status: services */}
        <div className="dashboard-panel" style={{ gridArea: "status" }}>
          <div className="dashboard-card-head">
            <span>{t("status.services")}</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {(s.services || []).length ? s.services.map(function(svc, i) {
              return (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 8px", border: "1px solid var(--line)",
                  borderRadius: "var(--r-m)", background: "var(--bg-2)"
                }}>
                  <span className={"sa-dot " + (svc.status === "warn" ? "running" : "done")}
                        style={{ marginTop: 0 }}></span>
                  <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text)" }}>{svc.name}</span>
                  <span style={{ marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 11, color: svc.status === "warn" ? "var(--warn)" : "var(--text-3)" }}>
                    {svc.latency}
                  </span>
                </div>
              );
            }) : <div className="dashboard-empty">—</div>}
          </div>
          {(s.workers || []).length ? <div style={{ marginTop: 10 }}>
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-4)", marginBottom: 6 }}>{t("status.workers")}</div>
            <table className="table">
              <thead><tr>
                <th>{t("status.id")}</th><th>{t("status.role")}</th><th>{t("status.status")}</th>
                <th>{t("status.host")}</th><th>{t("status.uptime")}</th><th style={{ textAlign: "right" }}>{t("status.tokens")}</th>
                <th style={{ textAlign: "right" }}>{t("status.spend")}</th>
              </tr></thead>
              <tbody>
                {s.workers.map(function(w) {
                  return (
                    <tr key={w.id}>
                      <td style={{ color: "var(--text)" }}>{w.id}</td>
                      <td>{w.role}</td>
                      <td><span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                        <span className={"sa-dot " + w.status} style={{ marginTop: 0, width: 6, height: 6 }}></span>{w.status}
                      </span></td>
                      <td>{w.host}</td>
                      <td>{w.uptime}</td>
                      <td style={{ textAlign: "right" }}>{w.tokens}</td>
                      <td style={{ textAlign: "right", color: "var(--text)" }}>{w.spend}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div> : null}
        </div>

        {/* Status: config */}
        <div className="dashboard-panel" style={{ gridArea: "config" }}>
          <div className="dashboard-card-head">
            <span>{t("status.configuration")}</span>
          </div>
          <div style={{ marginTop: 6, fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-3)", display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex" }}>
              <span>{t("status.model")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.model || "—"}</span>
            </div>
            <div style={{ display: "flex" }}>
              <span>{t("status.baseUrl")}</span>
              <span style={{ marginLeft: "auto", color: "var(--text)", maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.base_url || "—"}</span>
            </div>
            <div style={{ display: "flex" }}>
              <span>{t("status.soulMd")}</span>
              <span style={{ marginLeft: "auto", color: s.soul_exists ? "var(--accent)" : "var(--warn)" }}>{s.soul_exists ? t("status.loaded") : t("status.missing")}</span>
            </div>
            <div style={{ display: "flex" }}>
              <span>{t("status.scheduledTasks")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.scheduled_tasks ?? 0}</span>
            </div>
            <div style={{ display: "flex" }}>
              <span>{t("status.shortTermEntries")}</span><span style={{ marginLeft: "auto", color: "var(--text)" }}>{s.short_term_entries ?? 0}</span>
            </div>
          </div>
        </div>

        {/* Status: logs */}
        <div className="dashboard-panel" style={{ gridArea: "logs", maxHeight: 380, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div className="dashboard-card-head">
            <span>{t("status.activityLog")}</span>
          </div>
          <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
            {(s.logs || []).length ? s.logs.slice(-40).map(function(l, i) {
              return (
                <div className="log-row" key={i}>
                  <span className="t">{l.t}</span>
                  <span className={"lvl " + l.lvl}>{l.lvl}</span>
                  <span className="msg">{l.msg}</span>
                </div>
              );
            }) : <div className="dashboard-empty">—</div>}
          </div>
        </div>
      </section>
    </div>
  );
}

window.DashboardPage = DashboardPage;
