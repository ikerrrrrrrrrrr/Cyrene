// Workbench create-flows + project initialization.
//
// Fully independent from the legacy (`--agent`) chat UI: these components live
// only in the workbench shell and talk to the workbench `/api/projects` +
// `/api/task-sessions` endpoints via `window.WorkbenchModel`.
//
// Exposes on `window`:
//   - WorkbenchNewProjectModal  — multi-step "新建项目" wizard
//   - WorkbenchNewTaskModal     — "新建任务" dialog
//   - WorkbenchInitView         — the agent-led "初始化项目" onboarding session
//   - WorkbenchInitProgress     — the right-panel "初始化进度" tracker
(function () {
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  function T(key, params, fallback) {
    return window.WorkbenchI18n && window.WorkbenchI18n.t ? window.WorkbenchI18n.t(key, params, fallback) : (fallback || key);
  }

  function Svg(props) {
    return React.createElement(
      "svg",
      {
        viewBox: "0 0 24 24",
        width: props.size || 18,
        height: props.size || 18,
        fill: props.fill || "none",
        stroke: props.fill ? "none" : "currentColor",
        strokeWidth: props.sw || 1.7,
        strokeLinecap: "round",
        strokeLinejoin: "round",
      },
      props.children
    );
  }

  // ── project icon + color + template metadata ─────────────────────────
  var PROJECT_ICONS = {
    spark: <Svg fill="currentColor"><path d="M12 2.5 13.7 9 20 10.7 13.7 12.4 12 19l-1.7-6.6L4 10.7 10.3 9Z" /></Svg>,
    briefcase: <Svg><rect x="3" y="7.5" width="18" height="12" rx="2" /><path d="M8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5M3 12.5h18" /></Svg>,
    rocket: <Svg><path d="M5 15c-1.5 1.5-2 5-2 5s3.5-.5 5-2M9 11a9 9 0 0 1 9-9c1.5 0 2 .5 2 2a9 9 0 0 1-9 9M9 11l4 4M9 11l-4-1 2.5-2.5M13 15l1 4 2.5-2.5" /></Svg>,
    doc: <Svg><path d="M6 3.5h7l5 5V20a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 20Z" /><path d="M13 3.5V8a1 1 0 0 0 1 1h4M9 13h6M9 16.5h6" /></Svg>,
    people: <Svg><circle cx="9" cy="8.5" r="3" /><path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 6.2a3 3 0 0 1 0 5.6M20.5 19a5.5 5.5 0 0 0-3.5-5.1" /></Svg>,
    code: <Svg><path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 6l-3 12" /></Svg>,
    more: <Svg fill="currentColor"><circle cx="6" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="18" cy="12" r="1.6" /></Svg>,
  };
  var PROJECT_ICON_ORDER = ["spark", "briefcase", "rocket", "doc", "people", "code", "more"];
  var PROJECT_COLORS = ["#7c6cf0", "#3b82f6", "#22c08a", "#f5a623", "#ef4d57", "#a855f7"];

  var TEMPLATES = [
    { id: "blank", titleKey: "create.template.blank.title", descKey: "create.template.blank.desc",
      icon: <Svg><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M12 9v6M9 12h6" /></Svg> },
    { id: "product", titleKey: "create.template.product.title", descKey: "create.template.product.desc",
      icon: <Svg><path d="M12 3 20 7.5v9L12 21 4 16.5v-9Z" /><path d="M12 3v18M4 7.5l8 4.5 8-4.5" /></Svg> },
    { id: "pm", titleKey: "create.template.pm.title", descKey: "create.template.pm.desc",
      icon: <Svg><rect x="4" y="4" width="16" height="16" rx="3" /><path d="m8 12 2.5 2.5L16 9" /></Svg> },
    { id: "knowledge", titleKey: "create.template.knowledge.title", descKey: "create.template.knowledge.desc",
      icon: <Svg><path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5Z" /><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19" /></Svg> },
    { id: "ai", titleKey: "create.template.ai.title", descKey: "create.template.ai.desc",
      icon: <Svg><rect x="5" y="7" width="14" height="12" rx="3" /><path d="M12 3v4M9 12h.01M15 12h.01M9.5 16h5" /></Svg> },
    { id: "import", titleKey: "create.template.import.title", descKey: "create.template.import.desc",
      icon: <Svg><path d="M12 3v11m0 0 4-4m-4 4-4-4" /><path d="M5 16.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-1.5" /></Svg> },
  ];
  function templateLabel(id) {
    for (var i = 0; i < TEMPLATES.length; i++) if (TEMPLATES[i].id === id) return T(TEMPLATES[i].titleKey);
    return T("create.template.blank.title");
  }

  var XIcon = <Svg sw={1.9}><path d="m6 6 12 12M18 6 6 18" /></Svg>;
  var SparkIcon = <Svg fill="currentColor"><path d="M12 2.5 13.7 9 20 10.7 13.7 12.4 12 19l-1.7-6.6L4 10.7 10.3 9Z" /></Svg>;

  function ModalScrim(props) {
    return (
      <div className="wb-create-scrim" onMouseDown={function (e) { if (e.target === e.currentTarget) props.onClose(); }}>
        {props.children}
      </div>
    );
  }

  // ── New Project wizard ───────────────────────────────────────────────
  function WorkbenchNewProjectModal(props) {
    window.useWorkbenchI18n();
    var [step, setStep] = useState(0); // 0 = config (basics + template), 1 = finish
    var [name, setName] = useState("");
    var [description, setDescription] = useState("");
    var [icon, setIcon] = useState("spark");
    var [color, setColor] = useState(PROJECT_COLORS[0]);
    var [template, setTemplate] = useState("blank");
    var [workspacePath, setWorkspacePath] = useState(props.defaultWorkspacePath || "");
    var [advancedOpen, setAdvancedOpen] = useState(false);
    var [busy, setBusy] = useState(false);
    var [error, setError] = useState("");
    var customColorRef = useRef(null);

    var trimmedName = name.trim();
    var stepDots = [
      { labelKey: "create.project.step.basic" },
      { labelKey: "create.project.step.template" },
      { labelKey: "create.project.step.finish" },
    ];
    function dotState(index) {
      if (step === 0) return index < 2 ? "current" : "idle";
      return index < 2 ? "done" : "current";
    }

    function next() {
      if (!trimmedName) { setError(T("create.project.error.nameRequired")); return; }
      setError("");
      setStep(1);
    }
    function create() {
      setBusy(true);
      setError("");
      Promise.resolve(props.onCreate({
        name: trimmedName,
        description: description.trim(),
        icon: icon,
        color: color,
        template: template,
        workspacePath: workspacePath.trim() || undefined,
      })).catch(function (e) {
        setError((e && e.message) || String(e));
        setBusy(false);
        setStep(0);
      });
    }
    async function pickWorkspacePath() {
      if (busy) return;
      setError("");
      try {
        var r = await fetch("/api/context/pick-directory", { method: "POST" });
        var data = await r.json().catch(function () { return {}; });
        if (!r.ok) throw new Error(data.error || data.detail || ("HTTP " + r.status));
        if (data.path) {
          setWorkspacePath(data.path);
        } else if (data.error) {
          setError(data.error);
        }
      } catch (e) {
        setError((e && e.message) || String(e));
      }
    }

    var previewStyle = { background: color || "#7c6cf0" };

    return (
      <ModalScrim onClose={props.onClose}>
        <div className="wb-create-modal wb-create-project" role="dialog" aria-modal="true">
          <div className="wb-create-head">
            <b>{T("create.project.title")}</b>
            <button type="button" className="wb-create-x" onClick={props.onClose} title={T("common.close")}>{XIcon}</button>
          </div>
          <div className="wb-create-steps">
            {stepDots.map(function (s, i) {
              return (
                <React.Fragment key={i}>
                  {i > 0 && <span className="wb-create-step-line" />}
                  <span className={"wb-create-step " + dotState(i)}>
                    <span className="wb-create-step-dot">{dotState(i) === "done" ? <Svg size={13} sw={2.4}><path d="m5 12.5 4.5 4.5L19 7" /></Svg> : i + 1}</span>
                    <span className="wb-create-step-label">{T(s.labelKey)}</span>
                  </span>
                </React.Fragment>
              );
            })}
          </div>

          <div className="wb-create-body">
            {step === 0 ? (
              <div className="wb-cp-cols">
                <div className="wb-cp-left">
                  <label className="wb-cp-label">{T("create.project.name")} <i className="wb-cp-req">*</i></label>
                  <div className="wb-cp-field">
                    <input
                      className="wb-cp-input"
                      value={name}
                      maxLength={50}
                      autoFocus
                      placeholder={T("create.project.namePlaceholder")}
                      onChange={function (e) { setName(e.target.value); }}
                    />
                    <span className="wb-cp-counter">{name.length}/50</span>
                  </div>

                  <label className="wb-cp-label">{T("create.project.description")}</label>
                  <div className="wb-cp-field">
                    <textarea
                      className="wb-cp-textarea"
                      value={description}
                      maxLength={200}
                      rows={3}
                      placeholder={T("create.project.descriptionPlaceholder")}
                      onChange={function (e) { setDescription(e.target.value); }}
                    />
                    <span className="wb-cp-counter">{description.length}/200</span>
                  </div>

                  <label className="wb-cp-label">{T("create.project.icon")}</label>
                  <div className="wb-cp-icons">
                    {PROJECT_ICON_ORDER.map(function (id) {
                      return (
                        <button
                          type="button"
                          key={id}
                          className={"wb-cp-icon" + (icon === id ? " active" : "")}
                          onClick={function () { setIcon(id); }}
                        >{PROJECT_ICONS[id]}</button>
                      );
                    })}
                  </div>

                  <label className="wb-cp-label">{T("create.project.color")}</label>
                  <div className="wb-cp-colors">
                    {PROJECT_COLORS.map(function (c) {
                      return (
                        <button
                          type="button"
                          key={c}
                          className={"wb-cp-color" + (color === c ? " active" : "")}
                          style={{ background: c }}
                          onClick={function () { setColor(c); }}
                        >{color === c ? <Svg size={13} sw={2.6}><path d="m5 12.5 4.5 4.5L19 7" /></Svg> : null}</button>
                      );
                    })}
                    <button type="button" className="wb-cp-color custom" onClick={function () { if (customColorRef.current) customColorRef.current.click(); }} title={T("create.project.customColor")}>
                      <Svg size={14} sw={2}><path d="M12 5v14M5 12h14" /></Svg>
                      <input ref={customColorRef} type="color" value={color} onChange={function (e) { setColor(e.target.value); }} />
                    </button>
                  </div>

                  <button type="button" className="wb-cp-advanced-toggle" onClick={function () { setAdvancedOpen(!advancedOpen); }}>
                    <span>{T("create.project.advanced")}</span>
                    <span className="wb-cp-advanced-state">{advancedOpen ? T("common.collapse") : T("common.expand")} <i className={"wb-cp-caret" + (advancedOpen ? " up" : "")}>⌄</i></span>
                  </button>
                  {advancedOpen && (
                    <div className="wb-cp-advanced">
                      <label className="wb-cp-label">{T("create.project.workspacePath")}</label>
                      <button type="button" className="wb-cp-path-button" disabled={busy} onClick={pickWorkspacePath}>
                        <span className={"wb-cp-path-text" + (!workspacePath.trim() ? " empty" : "")}>{workspacePath.trim() || T("create.project.selectWorkspacePath")}</span>
                        <span className="wb-cp-path-action">{T("create.project.choosePath")}</span>
                      </button>
                    </div>
                  )}
                </div>

                <div className="wb-cp-right">
                  <div className="wb-cp-right-title">{T("create.project.step.template")}</div>
                  <div className="wb-cp-templates">
                    {TEMPLATES.map(function (t) {
                      var on = template === t.id;
                      return (
                        <button type="button" key={t.id} className={"wb-cp-template" + (on ? " active" : "")} onClick={function () { setTemplate(t.id); }}>
                          <span className="wb-cp-template-ico">{t.icon}</span>
                          <span className="wb-cp-template-meta">
                            <b>{T(t.titleKey)}</b>
                            <small>{T(t.descKey)}</small>
                          </span>
                          <span className={"wb-cp-template-check" + (on ? " on" : "")}>
                            {on ? <Svg size={14} sw={2.4}><path d="m5 12.5 4.5 4.5L19 7" /></Svg> : null}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            ) : (
              <div className="wb-cp-finish">
                <div className="wb-cp-finish-icon" style={previewStyle}>{PROJECT_ICONS[icon]}</div>
                <h3>{trimmedName || T("create.project.untitled")}</h3>
                {description.trim() && <p className="wb-cp-finish-desc">{description.trim()}</p>}
                <div className="wb-cp-finish-meta">
                  <span><i>{T("create.project.template")}</i>{templateLabel(template)}</span>
                  <span><i>{T("create.project.path")}</i>{workspacePath.trim() || T("create.project.defaultWorkspace")}</span>
                </div>
                <p className="wb-cp-finish-hint">{T("create.project.finishHint")}</p>
              </div>
            )}
          </div>

          {error && <div className="wb-create-error">{error}</div>}

          <div className="wb-create-foot">
            {step === 0 ? (
              <React.Fragment>
                <button type="button" className="wb-btn ghost" onClick={props.onClose}>{T("common.cancel")}</button>
                <button type="button" className="wb-btn primary" disabled={!trimmedName} onClick={next}>{T("common.next")}</button>
              </React.Fragment>
            ) : (
              <React.Fragment>
                <button type="button" className="wb-btn ghost" disabled={busy} onClick={function () { setStep(0); }}>{T("common.previous")}</button>
                <button type="button" className="wb-btn primary" disabled={busy} onClick={create}>{busy ? T("common.creating") : T("create.project.create")}</button>
              </React.Fragment>
            )}
          </div>
        </div>
      </ModalScrim>
    );
  }

  // ── New Task dialog ──────────────────────────────────────────────────
  var PRIORITIES = [{ id: "high", labelKey: "priority.high" }, { id: "medium", labelKey: "priority.medium" }, { id: "low", labelKey: "priority.low" }];
  function WorkbenchNewTaskModal(props) {
    window.useWorkbenchI18n();
    var [title, setTitle] = useState("");
    var [goal, setGoal] = useState("");
    var [priority, setPriority] = useState("medium");
    var [busy, setBusy] = useState(false);
    var [error, setError] = useState("");
    var trimmed = title.trim();

    function create() {
      if (!trimmed) { setError(T("create.task.error.nameRequired")); return; }
      setBusy(true);
      setError("");
      Promise.resolve(props.onCreate({ title: trimmed, goal: goal.trim(), priority: priority }))
        .catch(function (e) { setError((e && e.message) || String(e)); setBusy(false); });
    }

    return (
      <ModalScrim onClose={props.onClose}>
        <div className="wb-create-modal wb-create-task" role="dialog" aria-modal="true">
          <div className="wb-create-head">
            <b>{T("create.task.title")}</b>
            <button type="button" className="wb-create-x" onClick={props.onClose} title={T("common.close")}>{XIcon}</button>
          </div>
          <div className="wb-create-body wb-ct-body">
            <label className="wb-cp-label">{T("create.task.name")} <i className="wb-cp-req">*</i></label>
            <input
              className="wb-cp-input"
              value={title}
              maxLength={80}
              autoFocus
              placeholder={T("create.task.namePlaceholder")}
              onChange={function (e) { setTitle(e.target.value); }}
              onKeyDown={function (e) { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) create(); }}
            />
            <label className="wb-cp-label">{T("create.task.goal")}</label>
            <textarea
              className="wb-cp-textarea"
              value={goal}
              rows={3}
              placeholder={T("create.task.goalPlaceholder")}
              onChange={function (e) { setGoal(e.target.value); }}
            />
            <label className="wb-cp-label">{T("create.task.priority")}</label>
            <div className="wb-cp-seg">
              {PRIORITIES.map(function (p) {
                return (
                  <button type="button" key={p.id} className={"wb-cp-seg-btn" + (priority === p.id ? " on" : "")} onClick={function () { setPriority(p.id); }}>{T(p.labelKey)}</button>
                );
              })}
            </div>
          </div>
          {error && <div className="wb-create-error">{error}</div>}
          <div className="wb-create-foot">
            <button type="button" className="wb-btn ghost" onClick={props.onClose}>{T("common.cancel")}</button>
            <button type="button" className="wb-btn primary" disabled={busy || !trimmed} onClick={create}>{busy ? T("common.creating") : T("create.task.create")}</button>
          </div>
        </div>
      </ModalScrim>
    );
  }

  // ── "初始化项目" onboarding session ──────────────────────────────────
  function answeredValue(value) {
    if (Array.isArray(value)) return value.length > 0;
    return String(value == null ? "" : value).trim().length > 0;
  }
  function sectionComplete(section, answers) {
    var qs = (section && section.questions) || [];
    if (!qs.length) return false;
    return qs.every(function (q) { return answeredValue(answers[q.id]); });
  }

  function InitQuestion(props) {
    var q = props.q;
    var value = props.value;
    var control;
    if (q.type === "single") {
      control = (
        <div className="wb-init-chips">
          {(q.options || []).map(function (opt) {
            return (
              <button type="button" key={opt} className={"wb-init-chip" + (value === opt ? " on" : "")} onClick={function () { props.onText(q.id, value === opt ? "" : opt); }}>{opt}</button>
            );
          })}
        </div>
      );
    } else if (q.type === "multi") {
      var arr = Array.isArray(value) ? value : [];
      control = (
        <div className="wb-init-chips">
          {(q.options || []).map(function (opt) {
            return (
              <button type="button" key={opt} className={"wb-init-chip" + (arr.indexOf(opt) >= 0 ? " on" : "")} onClick={function () { props.onToggle(q.id, opt); }}>{opt}</button>
            );
          })}
        </div>
      );
    } else if (q.type === "textarea") {
      control = (
        <textarea className="wb-init-textarea" rows={2} value={value || ""} placeholder={q.placeholder || ""} onChange={function (e) { props.onText(q.id, e.target.value); }} />
      );
    } else {
      control = (
        <input className="wb-init-input" value={value || ""} placeholder={q.placeholder || ""} onChange={function (e) { props.onText(q.id, e.target.value); }} />
      );
    }
    return (
      <div className="wb-init-q">
        <div className="wb-init-q-label">{q.label}</div>
        <div className="wb-init-q-row">
          <div className="wb-init-q-field">{control}</div>
          <span className={"wb-init-q-num" + (answeredValue(value) ? " done" : "")}>{props.n}</span>
        </div>
      </div>
    );
  }

  function linesToList(text) {
    return String(text || "").split(/\n+/).map(function (line) { return line.trim(); }).filter(Boolean);
  }
  function listToLines(value) {
    return Array.isArray(value) ? value.join("\n") : "";
  }

  function InitTaskPlan(props) {
    var tasks = Array.isArray(props.tasks) ? props.tasks : [];
    function updateTask(index, patch) {
      props.onChange(tasks.map(function (task, i) {
        return i === index ? Object.assign({}, task, patch) : task;
      }));
    }
    function removeTask(index) {
      props.onChange(tasks.filter(function (_, i) { return i !== index; }));
    }
    function addTask() {
      props.onChange(tasks.concat([{
        id: "draft_" + Date.now(),
        title: "新步骤",
        goal: "",
        priority: "medium",
        constraints: [],
        acceptanceCriteria: [],
      }]));
    }
    if (!tasks.length) {
      return (
        <div className="wb-init-plan">
          <div className="wb-init-plan-empty">还没有任务计划。完成问题后，初始化 Agent 会先生成大任务计划。</div>
          <button type="button" className="wb-btn ghost" onClick={addTask}>手动添加任务</button>
        </div>
      );
    }
    return (
      <div className="wb-init-plan">
        <div className="wb-init-plan-head">
          <div>
            <b>大任务计划</b>
            <p>每个大任务会在确认后创建为一个独立 session。</p>
          </div>
          <button type="button" className="wb-btn ghost" onClick={addTask}>添加任务</button>
        </div>
        <div className="wb-init-plan-list">
          {tasks.map(function (task, index) {
            return (
              <div className="wb-init-plan-card" key={task.id || index}>
                <div className="wb-init-plan-card-head">
                  <span>{index + 1}</span>
                  <input
                    className="wb-init-input"
                    value={task.title || ""}
                    placeholder="任务标题"
                    onChange={function (e) { updateTask(index, { title: e.target.value }); }}
                  />
                  <select
                    className="wb-init-select"
                    value={task.priority || "medium"}
                    onChange={function (e) { updateTask(index, { priority: e.target.value }); }}
                  >
                    <option value="high">高</option>
                    <option value="medium">中</option>
                    <option value="low">低</option>
                  </select>
                  <button type="button" className="wb-btn ghost" onClick={function () { removeTask(index); }}>删除</button>
                </div>
                <textarea
                  className="wb-init-textarea"
                  rows={3}
                  value={task.goal || ""}
                  placeholder="这个 session 要完成的目标、边界和上下文"
                  onChange={function (e) { updateTask(index, { goal: e.target.value }); }}
                />
                <div className="wb-init-plan-cols">
                  <label>
                    <span>约束</span>
                    <textarea
                      className="wb-init-textarea"
                      rows={2}
                      value={listToLines(task.constraints)}
                      placeholder="一行一条"
                      onChange={function (e) { updateTask(index, { constraints: linesToList(e.target.value) }); }}
                    />
                  </label>
                  <label>
                    <span>验收标准</span>
                    <textarea
                      className="wb-init-textarea"
                      rows={2}
                      value={listToLines(task.acceptanceCriteria)}
                      placeholder="一行一条"
                      onChange={function (e) { updateTask(index, { acceptanceCriteria: linesToList(e.target.value) }); }}
                    />
                  </label>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  function WorkbenchInitView(props) {
    var model = window.WorkbenchModel;
    var project = props.project;
    var session = props.session;
    var sid = session ? session.id : "";
    var init = (session && session.init) || {};
    var sections = Array.isArray(init.sections) ? init.sections : [];
    var completed = !!init.completed;

    var [answers, setAnswers] = useState(init.answers || {});
    var [taskPlan, setTaskPlan] = useState(Array.isArray(init.taskPlan) ? init.taskPlan : []);
    var [feedback, setFeedback] = useState("");
    var [expanded, setExpanded] = useState(sections[0] ? sections[0].id : "");
    var [busy, setBusy] = useState(false);
    var [generating, setGenerating] = useState(false);
    var [planning, setPlanning] = useState(false);
    var genRef = useRef({});
    var saveTimer = useRef(null);

    // Re-sync local answers / expanded section when the session changes.
    useEffect(function () {
      var nextInit = (session && session.init) || {};
      setAnswers(nextInit.answers || {});
      setTaskPlan(Array.isArray(nextInit.taskPlan) ? nextInit.taskPlan : []);
      var secs = Array.isArray(nextInit.sections) ? nextInit.sections : [];
      setExpanded(secs[0] ? secs[0].id : "");
    }, [sid, init.answers, init.sections, init.taskPlan]);

    // Ask the agent to generate questions once per init session.
    useEffect(function () {
      if (!project || !session || completed) return;
      if (init.generated || genRef.current[sid]) return;
      genRef.current[sid] = true;
      setGenerating(true);
      model.generateInitForm(project.id)
        .then(function (next) { props.onRefresh && props.onRefresh(next); })
        .catch(function () {})
        .finally(function () { setGenerating(false); });
    }, [sid, init.generated, completed]);

    function persist(nextAnswers) {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(function () {
        model.patchSession(sid, { init: { answers: nextAnswers } }).catch(function () {});
      }, 600);
    }
    function setAnswer(qid, value) {
      setAnswers(function (prev) {
        var nextAnswers = Object.assign({}, prev);
        nextAnswers[qid] = value;
        persist(nextAnswers);
        return nextAnswers;
      });
    }
    function toggleMulti(qid, opt) {
      setAnswers(function (prev) {
        var arr = Array.isArray(prev[qid]) ? prev[qid].slice() : [];
        var i = arr.indexOf(opt);
        if (i >= 0) arr.splice(i, 1); else arr.push(opt);
        var nextAnswers = Object.assign({}, prev);
        nextAnswers[qid] = arr;
        persist(nextAnswers);
        return nextAnswers;
      });
    }

    function regenerate() {
      if (!project || generating) return;
      setGenerating(true);
      model.generateInitForm(project.id)
        .then(function (next) { props.onRefresh && props.onRefresh(next); })
        .catch(function (e) { window.alert((e && e.message) || String(e)); })
        .finally(function () { setGenerating(false); });
    }
    function complete() {
      if (busy) return;
      setBusy(true);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      model.submitInit(sid, answers)
        .then(function (next) { props.onRefresh && props.onRefresh(next); })
        .catch(function (e) { window.alert((e && e.message) || String(e)); })
        .finally(function () { setBusy(false); });
    }
    function saveCompletedAnswers() {
      if (busy) return;
      setBusy(true);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      model.patchSession(sid, { init: { answers: answers } })
        .then(function (next) { props.onRefresh && props.onRefresh(next); })
        .catch(function (e) { window.alert((e && e.message) || String(e)); })
        .finally(function () { setBusy(false); });
    }
    function revisePlan() {
      if (planning || completed) return;
      setPlanning(true);
      model.reviseInitPlan(sid, feedback)
        .then(function (next) { setFeedback(""); props.onRefresh && props.onRefresh(next); })
        .catch(function (e) { window.alert((e && e.message) || String(e)); })
        .finally(function () { setPlanning(false); });
    }
    function confirmPlan() {
      if (busy || completed || !taskPlan.length) return;
      setBusy(true);
      model.confirmInitPlan(sid, taskPlan)
        .then(function (next) { props.onRefresh && props.onRefresh(next); })
        .catch(function (e) { window.alert((e && e.message) || String(e)); })
        .finally(function () { setBusy(false); });
    }

    var greetingLines = String(init.greeting || "").split("\n");
    var planReady = !!init.planReady || taskPlan.length > 0;
    var showPlan = planReady && !completed;

    return (
      <div className="wb-init">
        <div className="wb-init-head">
          <div className="wb-init-head-main">
            <h1>初始化项目</h1>
            <span className={"workbench-status-pill " + (completed ? "green" : "blue")}>{completed ? "已完成" : "初始化中"}</span>
            <span className="wb-init-head-project">{project ? project.name : ""}</span>
          </div>
          {!completed && !planReady && (
            <button type="button" className="wb-btn ghost" disabled={generating} onClick={regenerate}>{generating ? "生成中…" : "重新生成问题"}</button>
          )}
        </div>

        <div className="wb-init-scroll">
          {!init.generated && generating && (
            <div className="wb-init-generating"><span className="wb-spinner" /> Agent 正在初始化项目中…</div>
          )}

          {init.generated && (
            <React.Fragment>
              <div className="wb-init-greeting">
                <span className="wb-init-greeting-ico">{SparkIcon}</span>
                <div className="wb-init-greeting-body">
                  {greetingLines.map(function (line, i) { return <p key={i}>{line || " "}</p>; })}
                </div>
              </div>

              {generating && (
                <div className="wb-init-generating"><span className="wb-spinner" /> 正在重新生成问题…</div>
              )}

              {!showPlan && (
            <div className="wb-init-sections">
              {sections.map(function (section, sIdx) {
                var open = expanded === section.id;
                var done = sectionComplete(section, answers);
                return (
                  <div className={"wb-init-section" + (open ? " open" : "")} key={section.id}>
                    <button type="button" className="wb-init-section-head" onClick={function () { setExpanded(open ? "" : section.id); }}>
                      <span className="wb-init-section-n">{sIdx + 1}.</span>
                      <b>{section.title}</b>
                      {done && <span className="wb-init-section-done">已完成</span>}
                      <i className="wb-init-chevron">{open ? "⌃" : "⌄"}</i>
                    </button>
                    {open && (
                      <div className="wb-init-section-body">
                        {(section.questions || []).map(function (q, qi) {
                          return <InitQuestion key={q.id} q={q} n={qi + 1} value={answers[q.id]} onText={setAnswer} onToggle={toggleMulti} />;
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {showPlan && (
            <React.Fragment>
              <InitTaskPlan tasks={taskPlan} onChange={setTaskPlan} />
              {!completed && (
                <div className="wb-init-feedback">
                  <textarea
                    className="wb-init-textarea"
                    rows={2}
                    value={feedback}
                    placeholder="告诉初始化 Agent 如何调整计划，例如：先做核心部分，其他后续迭代。"
                    onChange={function (e) { setFeedback(e.target.value); }}
                  />
                  <button type="button" className="wb-btn ghost" disabled={planning} onClick={revisePlan}>{planning ? "调整中…" : "让 Agent 调整计划"}</button>
                </div>
              )}
            </React.Fragment>
          )}
            </React.Fragment>
          )}
        </div>

        <div className="wb-init-foot">
          <div className="wb-init-foot-hint">
            {completed ? "项目初始化已完成。你仍可以修改问题答案并保存。"
              : planReady ? "确认后会按拆分好的步骤逐步推进。"
                : "完成问题后会生成可执行的计划。"}
          </div>
          {completed && <button type="button" className="wb-btn primary" disabled={busy} onClick={saveCompletedAnswers}>{busy ? "保存中…" : "保存修改"}</button>}
          {!completed && !planReady && <button type="button" className="wb-btn primary" disabled={busy} onClick={complete}>{busy ? "生成计划中…" : "完成问题并生成计划"}</button>}
          {!completed && planReady && <button type="button" className="wb-btn primary" disabled={busy || !taskPlan.length} onClick={confirmPlan}>{busy ? "创建中…" : "确认计划并创建 sessions"}</button>}
        </div>
      </div>
    );
  }

  // Right-panel "初始化进度" tracker.
  function WorkbenchInitProgress(props) {
    var session = props.session;
    var init = (session && session.init) || {};
    var sections = Array.isArray(init.sections) ? init.sections : [];
    var answers = init.answers || {};
    var firstIncomplete = -1;
    var rows = sections.map(function (section, i) {
      var done = sectionComplete(section, answers);
      if (!done && firstIncomplete === -1) firstIncomplete = i;
      return { label: section.title, done: done };
    });
    rows.forEach(function (row, i) { row.active = i === firstIncomplete; });
    rows.push({
      label: init.planReady ? "确认计划并创建 sessions" : "生成大任务计划",
      done: !!init.completed,
      active: firstIncomplete === -1 && !init.completed,
    });
    return (
      <div className="wb-init-progress">
        {rows.map(function (row, i) {
          return (
            <div className={"wb-init-progress-row" + (row.active ? " active" : "") + (row.done ? " done" : "")} key={i}>
              <span className="wb-init-progress-dot" />
              <span>{row.label}</span>
            </div>
          );
        })}
      </div>
    );
  }

  window.WorkbenchNewProjectModal = WorkbenchNewProjectModal;
  window.WorkbenchNewTaskModal = WorkbenchNewTaskModal;
  window.WorkbenchInitView = WorkbenchInitView;
  window.WorkbenchInitProgress = WorkbenchInitProgress;
})();
