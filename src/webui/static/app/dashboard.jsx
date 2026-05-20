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

function DashboardTokenChart({ timeline }) {
  const data = Array.isArray(timeline) ? timeline : [];
  const w = 760;
  const h = 264;
  const padX = 34;
  const padTop = 20;
  const padBottom = 34;
  const maxValue = data.reduce((max, item) => Math.max(max, (item.prompt || 0) + (item.completion || 0)), 1);
  const range = maxValue || 1;
  const promptCoords = data.map((item, index) => {
    const x = padX + (index / Math.max(1, data.length - 1)) * (w - padX * 2);
    const prompt = item.prompt || 0;
    const completion = item.completion || 0;
    const stacked = prompt + completion;
    const yStacked = h - padBottom - (stacked / range) * (h - padTop - padBottom);
    const yPrompt = h - padBottom - (prompt / range) * (h - padTop - padBottom);
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

  return (
    <div className="dashboard-token-chart">
      <div className="dashboard-legend">
        <span><i className="swatch prompt"></i>Input</span>
        <span><i className="swatch completion"></i>Output</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        {[0.25, 0.5, 0.75, 1].map((ratio) => {
          const y = h - padBottom - ratio * (h - padTop - padBottom);
          return <line key={ratio} x1={padX} y1={y} x2={w - padX} y2={y} className="dashboard-grid-line" />;
        })}
        {stackedArea ? <path d={stackedArea} className="dashboard-area completion" /> : null}
        {promptArea ? <path d={promptArea} className="dashboard-area prompt" /> : null}
        {stackedLine ? <path d={stackedLine} className="dashboard-line completion" /> : null}
        {promptLine ? <path d={promptLine} className="dashboard-line prompt" /> : null}
        {promptCoords.map((point) => (
          <g key={point.item.date}>
            <circle cx={point.x} cy={point.yStacked} r="3.5" className="dashboard-point completion" />
            <circle cx={point.x} cy={point.yPrompt} r="3.5" className="dashboard-point prompt" />
            <text x={point.x} y={h - 8} textAnchor="middle" className="dashboard-axis-label">
              {formatRelativeDateLabel(point.item.date)}
            </text>
          </g>
        ))}
      </svg>
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
          cache hit
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
            <small>{usage.tokens || "—"}</small>
          </div>
          <DashboardTokenChart timeline={timeline} />
          <div className="dashboard-token-footer">
            <div className="dashboard-stat-pair"><span>{t("dashboard.requests")}</span><strong>{usage.requests ?? "—"}</strong></div>
            <div className="dashboard-stat-pair"><span>{t("dashboard.spend")}</span><strong>{usage.spend || "—"}</strong></div>
            <div className="dashboard-stat-pair"><span>Input</span><strong>{compactNumber(usage.prompt_tokens || 0)}</strong></div>
            <div className="dashboard-stat-pair"><span>Output</span><strong>{compactNumber(usage.completion_tokens || 0)}</strong></div>
          </div>
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
      </section>
    </div>
  );
}

window.DashboardPage = DashboardPage;
