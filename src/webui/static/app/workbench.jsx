// Four-column Project / Task Session workbench.
var {
  useState: useWorkbenchState,
  useEffect: useWorkbenchEffect,
  useMemo: useWorkbenchMemo,
  useRef: useWorkbenchRef,
} = React;

function WorkbenchApp({ onOpenLegacy }) {
  useDataVersion();
  var model = window.WorkbenchModel;
  var [store, setStore] = useWorkbenchState(function () {
    return model.normalizeStore({ projects: [] });
  });
  var [loading, setLoading] = useWorkbenchState(true);
  var [error, setError] = useWorkbenchState("");
  var [fullPage, setFullPage] = useWorkbenchState(null);
  var [rightTab, setRightTab] = useWorkbenchState("context");
  var [expandedStepId, setExpandedStepId] = useWorkbenchState("");
  var [searchOpen, setSearchOpen] = useWorkbenchState(false);

  function reloadWorkbench(nextProjectId, nextSessionId) {
    setLoading(true);
    setError("");
    return model.fetchProjects()
      .then(function (next) {
        if (nextProjectId) next.activeProjectId = nextProjectId;
        var project = next.projects.find(function (item) { return item.id === next.activeProjectId; }) || next.activeProject;
        if (project) {
          next.activeProject = project;
          if (nextSessionId) next.activeSessionId = nextSessionId;
          next.activeSession = project.sessions.find(function (item) { return item.id === next.activeSessionId; }) || project.sessions[0] || null;
          next.activeSessionId = next.activeSession ? next.activeSession.id : "";
        }
        setStore(next);
        return next;
      })
      .catch(function (err) {
        setError(err.message || String(err));
      })
      .finally(function () {
        setLoading(false);
      });
  }

  useWorkbenchEffect(function () {
    reloadWorkbench();
  }, []);

  function selectProject(projectId) {
    var project = store.projects.find(function (item) { return item.id === projectId; });
    if (!project) return;
    setStore(function (prev) {
      var next = { ...prev };
      next.activeProjectId = project.id;
      next.activeProject = project;
      next.activeSession = project.sessions[0] || null;
      next.activeSessionId = next.activeSession ? next.activeSession.id : "";
      return next;
    });
    setExpandedStepId("");
  }

  function selectSession(sessionId) {
    var project = store.activeProject;
    if (!project) return;
    var session = project.sessions.find(function (item) { return item.id === sessionId; });
    if (!session) return;
    setStore(function (prev) {
      return { ...prev, activeSessionId: session.id, activeSession: session };
    });
    setExpandedStepId("");
  }

  function createProject() {
    var name = window.prompt("项目名称", "New Project");
    if (!name) return;
    var workspacePath = window.prompt("工作区路径", store.activeProject && store.activeProject.workspacePath || "");
    model.createProject({ name: name, workspacePath: workspacePath || undefined })
      .then(function (next) {
        setStore(next);
        setExpandedStepId("");
      })
      .catch(function (err) { setError(err.message || String(err)); });
  }

  function createSession() {
    if (!store.activeProject) return;
    var title = window.prompt("任务名称", "新任务");
    if (!title) return;
    model.createSession(store.activeProject.id, { title: title, goal: title })
      .then(function (next) {
        setStore(next);
        setExpandedStepId("");
      })
      .catch(function (err) { setError(err.message || String(err)); });
  }

  function handleRunCreated(next) {
    setStore(next);
    setExpandedStepId(next.activeSession && next.activeSession.plan[0] ? next.activeSession.plan[0].id : "");
    setRightTab("context");
  }

  var fullPageConfig = fullPage ? workbenchFullPageConfig(fullPage, setFullPage, onOpenLegacy, store) : null;

  return (
    <div className="workbench-shell" data-screen-label="Cyrene · workbench">
      <WorkbenchTopbar
        project={store.activeProject}
        session={store.activeSession}
        onSearch={function () { setSearchOpen(true); }}
        onSettings={function () { setFullPage("settings"); }}
      />
      {fullPageConfig ? (
        <WorkbenchFullPage config={fullPageConfig} onClose={function () { setFullPage(null); }} />
      ) : (
        <div className="workbench-grid">
          <ProjectRail
            projects={store.projects}
            activeProjectId={store.activeProjectId}
            onSelectProject={selectProject}
            onCreateProject={createProject}
            onOpenPage={setFullPage}
            onOpenLegacy={onOpenLegacy}
          />
          <TaskRail
            project={store.activeProject}
            activeSessionId={store.activeSessionId}
            onSelectSession={selectSession}
            onCreateSession={createSession}
            loading={loading}
          />
          <TaskWorkArea
            project={store.activeProject}
            session={store.activeSession}
            expandedStepId={expandedStepId}
            onToggleStep={function (stepId) { setExpandedStepId(expandedStepId === stepId ? "" : stepId); }}
            onCreateRun={handleRunCreated}
            onRightTab={setRightTab}
            error={error}
            loading={loading}
          />
          <RightContextPanel
            project={store.activeProject}
            session={store.activeSession}
            expandedStepId={expandedStepId}
            tab={rightTab}
            onTabChange={setRightTab}
          />
        </div>
      )}
      {searchOpen && React.createElement(
        window.SearchOverlay || function () { return null; },
        {
          onClose: function () { setSearchOpen(false); },
          onOpenSession: function () {
            setSearchOpen(false);
            setFullPage("chat");
          },
        }
      )}
    </div>
  );
}

function WorkbenchTopbar({ project, session, onSearch, onSettings }) {
  var title = project ? project.name : "Project";
  var sessionTitle = session ? session.title : "Task";
  return (
    <div className="workbench-topbar">
      <div className="workbench-brand">
        <div className="workbench-traffic-space"></div>
        <div className="brand-mark"></div>
        <strong>Cyrene</strong>
        <span>Agent</span>
      </div>
      <div className="workbench-crumbs">
        <span>{title}</span>
        <span>/</span>
        <b>{sessionTitle}</b>
      </div>
      <div className="workbench-top-actions">
        <button type="button" className="workbench-icon-btn wide" onClick={onSearch} title="搜索">
          <span>⌕</span><span>搜索</span>
        </button>
        <button type="button" className="workbench-icon-btn" title="通知">♢</button>
        <button type="button" className="workbench-icon-btn" onClick={onSettings} title="设置">⚙</button>
        <div className="workbench-avatar">{WorkbenchModel.initials(DATA.user && DATA.user.name)}</div>
      </div>
    </div>
  );
}

function ProjectRail({ projects, activeProjectId, onSelectProject, onCreateProject, onOpenPage, onOpenLegacy }) {
  return (
    <aside className="workbench-project-rail">
      <div className="workbench-rail-head">
        <span>项目</span>
        <button type="button" onClick={onCreateProject}>+ 新建项目</button>
      </div>
      <div className="workbench-project-list">
        {projects.map(function (project) {
          var active = project.id === activeProjectId;
          return (
            <button
              type="button"
              key={project.id}
              className={"workbench-project-card" + (active ? " active" : "")}
              onClick={function () { onSelectProject(project.id); }}
              title={project.workspacePath}
            >
              <span className="workbench-project-icon">{WorkbenchModel.initials(project.name)}</span>
              <span className="workbench-project-meta">
                <b>{project.name}</b>
                <small>{project.workspacePath || "—"}</small>
              </span>
            </button>
          );
        })}
      </div>
      <div className="workbench-global-nav">
        <button type="button" onClick={function () { onOpenPage("chat"); }}>对话</button>
        <button type="button" onClick={function () { onOpenPage("knowledge"); }}>知识库</button>
        <button type="button" onClick={function () { onOpenPage("schedule"); }}>日程</button>
        <button type="button" onClick={function () { onOpenPage("memory"); }}>记忆</button>
        <button type="button" onClick={onOpenLegacy}>旧界面</button>
      </div>
      <div className="workbench-account">
        <div className="workbench-avatar photo">{WorkbenchModel.initials(DATA.user && DATA.user.name)}</div>
        <div>
          <b>{DATA.user && DATA.user.name || "User"}</b>
          <small>{(DATA.sessions && DATA.sessions[0] && DATA.sessions[0].model) || DATA.appVersion || "model"} · Pro</small>
        </div>
      </div>
    </aside>
  );
}

function TaskRail({ project, activeSessionId, onSelectSession, onCreateSession, loading }) {
  var sessions = project && Array.isArray(project.sessions) ? project.sessions : [];
  return (
    <aside className="workbench-task-rail">
      <div className="workbench-rail-head">
        <span>任务</span>
        <button type="button" onClick={onCreateSession} disabled={!project}>+ 新建任务</button>
      </div>
      {loading && <div className="workbench-muted">加载任务中...</div>}
      {!loading && sessions.length === 0 && <div className="workbench-muted">暂无任务</div>}
      <div className="workbench-task-list">
        {sessions.map(function (session) {
          var tone = WorkbenchModel.statusTone(session.status);
          return (
            <button
              type="button"
              key={session.id}
              className={"workbench-task-card" + (session.id === activeSessionId ? " active" : "")}
              onClick={function () { onSelectSession(session.id); }}
            >
              <span className={"workbench-status-dot " + tone}></span>
              <span className="workbench-task-main">
                <b>{session.title}</b>
                <small>{WorkbenchModel.statusText(session.status)}</small>
              </span>
              <time>{WorkbenchModel.formatTime(session.updatedAt || session.createdAt)}</time>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function TaskWorkArea(props) {
  var project = props.project;
  var session = props.session;
  if (props.loading) {
    return <main className="workbench-main"><div className="workbench-empty">正在加载工作台...</div></main>;
  }
  if (!project || !session) {
    return <main className="workbench-main"><div className="workbench-empty">请选择项目和任务。</div></main>;
  }
  var hasTaskStructure = Boolean(session.agentReply || (session.plan && session.plan.length));
  return (
    <main className="workbench-main">
      <TaskHeader project={project} session={session} />
      {props.error && <div className="workbench-error">{props.error}</div>}
      {!hasTaskStructure ? (
        <InitialTaskConversation session={session} onCreateRun={props.onCreateRun} />
      ) : (
        <>
          <AgentReplyPanel session={session} />
          <TaskStepList
            session={session}
            expandedStepId={props.expandedStepId}
            onToggleStep={props.onToggleStep}
            onRightTab={props.onRightTab}
          />
          <TaskComposer session={session} onCreateRun={props.onCreateRun} compact={true} />
        </>
      )}
    </main>
  );
}

function TaskHeader({ project, session }) {
  var tone = WorkbenchModel.statusTone(session.status);
  return (
    <div className="workbench-task-header">
      <div>
        <h1>{session.title}</h1>
        <p>{session.goal || "先通过对话明确任务目标、约束和验收标准。"}</p>
      </div>
      <div className="workbench-task-actions">
        <span className={"workbench-status-pill " + tone}>{WorkbenchModel.statusText(session.status)}</span>
        <span>优先级 {session.priority || "medium"}</span>
        <span>{project.name}</span>
      </div>
    </div>
  );
}

function InitialTaskConversation({ session, onCreateRun }) {
  return (
    <section className="workbench-initial-chat">
      <div className="workbench-initial-copy">
        <h2>描述这个任务的目标</h2>
        <p>输入任务目标、边界和验收标准。Agent 会先把它整理成结构化任务，而不是直接展开一长串聊天。</p>
      </div>
      <TaskComposer session={session} onCreateRun={onCreateRun} compact={false} />
    </section>
  );
}

function AgentReplyPanel({ session }) {
  return (
    <section className="workbench-agent-reply">
      <div className="workbench-panel-title">
        <span>✦</span>
        <b>Agent 回复</b>
      </div>
      <div className="workbench-agent-body">
        {(session.agentReply || "任务结构已创建。").split("\n").map(function (line, index) {
          return <p key={index}>{line}</p>;
        })}
      </div>
    </section>
  );
}

function TaskStepList({ session, expandedStepId, onToggleStep, onRightTab }) {
  var steps = Array.isArray(session.plan) ? session.plan : [];
  return (
    <section className="workbench-flow">
      <div className="workbench-flow-head">
        <b>执行流程</b>
        <span>{steps.filter(function (step) { return step.status === "completed" || step.status === "done"; }).length}/{steps.length}</span>
      </div>
      {steps.map(function (step, index) {
        var expanded = expandedStepId === step.id;
        var tone = WorkbenchModel.statusTone(step.status);
        return (
          <div key={step.id} className={"workbench-step" + (expanded ? " expanded" : "")}>
            <button type="button" className="workbench-step-row" onClick={function () { onToggleStep(step.id); }}>
              <span className={"workbench-status-dot " + tone}></span>
              <span className="workbench-step-index">{index + 1}.</span>
              <b>{step.title}</b>
              <small>{WorkbenchModel.statusText(step.status)}</small>
              <span>{step.updatedAt ? WorkbenchModel.formatTime(step.updatedAt) : ""}</span>
              <i>{expanded ? "⌃" : "⌄"}</i>
            </button>
            {expanded && (
              <div className="workbench-step-detail">
                <div>
                  <label>正在执行</label>
                  <p>{step.currentAction || step.description || "等待 Agent 更新这个步骤的进展。"}</p>
                </div>
                <div>
                  <label>相关文件</label>
                  {(step.relatedFiles || []).length === 0 ? (
                    <p className="workbench-muted">暂无相关文件</p>
                  ) : (
                    <div className="workbench-file-list">
                      {step.relatedFiles.map(function (file) {
                        return <button key={file.path || file.name} type="button" onClick={function () { onRightTab("files"); }}>{file.path || file.name}</button>;
                      })}
                    </div>
                  )}
                </div>
                <div>
                  <label>最近进展</label>
                  <ul>
                    {(step.progressEvents || []).slice(-4).map(function (event, eventIndex) {
                      return <li key={event.id || eventIndex}>{WorkbenchModel.formatTime(event.time || event.createdAt)} · {event.text || event.body}</li>;
                    })}
                    {(step.progressEvents || []).length === 0 && <li>暂无进展事件</li>}
                  </ul>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}

function TaskComposer({ session, onCreateRun, compact }) {
  var model = window.WorkbenchModel;
  var [draft, setDraft] = useWorkbenchState("");
  var [busy, setBusy] = useWorkbenchState(false);
  var textareaRef = useWorkbenchRef(null);

  function submit() {
    var text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    model.createRun(session.id, text)
      .then(function (next) {
        setDraft("");
        onCreateRun && onCreateRun(next);
      })
      .catch(function (err) {
        window.alert(err.message || String(err));
      })
      .finally(function () {
        setBusy(false);
      });
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className={"workbench-composer" + (compact ? " compact" : "")}>
      <div className="workbench-composer-box">
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={function (event) { setDraft(event.target.value); }}
          onKeyDown={onKeyDown}
          placeholder="输入你的想法、补充约束，或粘贴文件/错误上下文"
          rows={compact ? 2 : 5}
        />
        <div className="workbench-composer-actions">
          <span>@ 当前任务</span>
          <button type="button" onClick={submit} disabled={busy || !draft.trim()}>{busy ? "处理中" : "发送"}</button>
        </div>
      </div>
    </div>
  );
}

function RightContextPanel({ project, session, expandedStepId, tab, onTabChange }) {
  var steps = session && Array.isArray(session.plan) ? session.plan : [];
  var activeStep = steps.find(function (step) { return step.id === expandedStepId; }) || null;
  var tabs = [
    { id: "context", label: "上下文" },
    { id: "files", label: "文件变更" },
    { id: "logs", label: "运行日志" },
    { id: "acceptance", label: "验收标准" },
    { id: "artifacts", label: "产物" },
  ];
  return (
    <aside className="workbench-right-panel">
      <div className="workbench-right-tabs">
        {tabs.map(function (item) {
          return <button key={item.id} type="button" className={tab === item.id ? "active" : ""} onClick={function () { onTabChange(item.id); }}>{item.label}</button>;
        })}
      </div>
      <div className="workbench-right-body">
        {tab === "context" && <ContextTab project={project} session={session} activeStep={activeStep} />}
        {tab === "files" && <FilesTab session={session} activeStep={activeStep} />}
        {tab === "logs" && <LogsTab session={session} />}
        {tab === "acceptance" && <AcceptanceTab session={session} />}
        {tab === "artifacts" && <ArtifactsTab session={session} />}
      </div>
    </aside>
  );
}

function ContextTab({ project, session, activeStep }) {
  var constraints = session && session.constraints || [];
  return (
    <div className="workbench-side-stack">
      <SideSection title="任务概况">
        <p>{session && session.goal || "暂无任务目标"}</p>
        {activeStep && <p>当前步骤：{activeStep.title}</p>}
      </SideSection>
      <SideSection title="项目上下文">
        <p>{project && project.context && project.context.summary || project && project.workspacePath || "—"}</p>
      </SideSection>
      <SideSection title={"任务约束 (" + constraints.length + ")"}>
        {constraints.length ? constraints.map(function (item) { return <div className="workbench-check" key={item}>● {item}</div>; }) : <p className="workbench-muted">暂无约束</p>}
      </SideSection>
    </div>
  );
}

function FilesTab({ session, activeStep }) {
  var files = [];
  if (activeStep && Array.isArray(activeStep.relatedFiles)) files = files.concat(activeStep.relatedFiles);
  (session && session.artifacts || []).forEach(function (artifact) {
    if (artifact.type === "file_change") files.push(artifact);
  });
  return (
    <div className="workbench-side-stack">
      <SideSection title={"相关文件 (" + files.length + ")"}>
        {files.length ? files.map(function (file) {
          return <div className="workbench-file-row" key={file.id || file.path || file.name}><span>{file.path || file.name}</span><small>{file.status || file.type || ""}</small></div>;
        }) : <p className="workbench-muted">当前任务还没有记录文件变更。</p>}
      </SideSection>
    </div>
  );
}

function LogsTab({ session }) {
  var events = session && Array.isArray(session.events) ? session.events : [];
  return (
    <div className="workbench-side-stack">
      <SideSection title={"运行日志 (" + events.length + ")"}>
        {events.length ? events.slice().reverse().slice(0, 18).map(function (event) {
          return <div className="workbench-log-row" key={event.id}><time>{WorkbenchModel.formatTime(event.createdAt)}</time><span>{event.type}</span><p>{event.body || (event.stepCount != null ? ("步骤数 " + event.stepCount) : "")}</p></div>;
        }) : <p className="workbench-muted">暂无运行日志</p>}
      </SideSection>
    </div>
  );
}

function AcceptanceTab({ session }) {
  var items = session && Array.isArray(session.acceptanceCriteria) ? session.acceptanceCriteria : [];
  return (
    <div className="workbench-side-stack">
      <SideSection title={"验收标准 (" + items.length + ")"}>
        {items.length ? items.map(function (item) {
          return <div className="workbench-check" key={item.id}><span className={"workbench-status-dot " + WorkbenchModel.statusTone(item.status)}></span>{item.text}</div>;
        }) : <p className="workbench-muted">任务明确后会自动生成验收标准。</p>}
      </SideSection>
    </div>
  );
}

function ArtifactsTab({ session }) {
  var artifacts = session && Array.isArray(session.artifacts) ? session.artifacts : [];
  return (
    <div className="workbench-side-stack">
      <SideSection title={"产物 (" + artifacts.length + ")"}>
        {artifacts.length ? artifacts.map(function (artifact) {
          return <div className="workbench-artifact-row" key={artifact.id}><b>{artifact.name}</b><small>{artifact.type} · {artifact.status}</small><p>{artifact.summary || ""}</p></div>;
        }) : <p className="workbench-muted">暂无产物</p>}
      </SideSection>
    </div>
  );
}

function SideSection({ title, children }) {
  return (
    <section className="workbench-side-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function WorkbenchFullPage({ config, onClose }) {
  return (
    <div className="workbench-fullscreen">
      <div className="workbench-fullscreen-head">
        <button type="button" onClick={onClose}>← 返回工作台</button>
        <b>{config.title}</b>
      </div>
      <div className="workbench-fullscreen-body">
        {config.render()}
      </div>
    </div>
  );
}

function workbenchFullPageConfig(page, setFullPage, onOpenLegacy, store) {
  if (page === "chat") {
    return {
      title: "对话",
      render: function () {
        return React.createElement(window.ChatPage || function () { return <div className="workbench-empty">对话界面加载中...</div>; }, {
          selectedSessionId: null,
          onSelectSession: function () {},
          rightSidebarCollapsed: false,
          setRightSidebarCollapsed: function () {},
          rightSidebarView: "overview",
          setRightSidebarView: function () {},
        });
      },
    };
  }
  if (page === "knowledge") {
    return { title: "知识库", render: function () { return React.createElement(window.KnowledgePage || function () { return <div className="workbench-empty">知识库加载中...</div>; }); } };
  }
  if (page === "schedule") {
    return { title: "日程", render: function () { return React.createElement(window.ScheduledTasksPage || function () { return <div className="workbench-empty">日程加载中...</div>; }); } };
  }
  if (page === "memory") {
    return { title: "记忆", render: function () { return React.createElement(window.MemoryPage || function () { return <div className="workbench-empty">记忆加载中...</div>; }); } };
  }
  if (page === "settings") {
    return {
      title: "设置",
      render: function () {
        return React.createElement(window.SettingsPage || function () { return <div className="workbench-empty">设置加载中...</div>; }, {
          tweaks: {},
          setTweak: function () {},
          actualTheme: "dark",
          accentPresets: [],
        });
      },
    };
  }
  return { title: page, render: function () { return <div className="workbench-empty">未找到页面。</div>; } };
}

window.WorkbenchApp = WorkbenchApp;
