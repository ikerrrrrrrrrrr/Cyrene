// Four-column Project / Task Session workbench.
var {
  useState: useWorkbenchState,
  useEffect: useWorkbenchEffect,
  useMemo: useWorkbenchMemo,
  useRef: useWorkbenchRef,
} = React;

function WorkbenchApp({ onOpenLegacy, theme, actualTheme, onToggleTheme }) {
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
  var [settingsOpen, setSettingsOpen] = useWorkbenchState(false);

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

  function handleOpenPage(page) {
    setFullPage(function (prev) { return prev === page ? null : page; });
  }

  // The 知识库 and 日程 views keep the ProjectRail (so you can navigate while
  // viewing them); other pages take over the full screen.
  var isKnowledge = fullPage === "knowledge";
  var isSchedule = fullPage === "schedule";
  var isModulePage = isKnowledge || isSchedule;
  var fullPageConfig = fullPage && !isModulePage ? workbenchFullPageConfig(fullPage, setFullPage, onOpenLegacy, store) : null;

  return (
    <div className="workbench-shell" data-screen-label="Cyrene · workbench">
      <WorkbenchTopbar
        project={store.activeProject}
        session={store.activeSession}
        activePage={fullPage}
        onSearch={function () { setSearchOpen(true); }}
        onSettings={function () { setSettingsOpen(true); }}
        theme={theme}
        actualTheme={actualTheme}
        onToggleTheme={onToggleTheme}
      />
      {fullPageConfig ? (
        <WorkbenchFullPage config={fullPageConfig} onClose={function () { setFullPage(null); }} />
      ) : (
        <div className={"workbench-grid" + (isKnowledge ? " is-knowledge" : "") + (isSchedule ? " is-schedule" : "")}>
          <ProjectRail
            projects={store.projects}
            activeProjectId={store.activeProjectId}
            activePage={fullPage}
            onSelectProject={selectProject}
            onCreateProject={createProject}
            onOpenPage={handleOpenPage}
            onOpenLegacy={onOpenLegacy}
          />
          {isKnowledge ? (
            React.createElement(window.WorkbenchKnowledgePage || function () { return <div className="workbench-empty">知识库加载中...</div>; }, { project: store.activeProject, onBack: function () { setFullPage(null); } })
          ) : isSchedule ? (
            React.createElement(window.WorkbenchSchedulePage || function () { return <div className="workbench-empty">日程加载中...</div>; }, { project: store.activeProject, onBack: function () { setFullPage(null); } })
          ) : (
          <>
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
            onRefresh={function (nextStore) {
              setStore(function (prev) {
                // Preserve expandedStepId, rightTab, etc. from current UI state
                // but replace project/session data from the server response
                var merged = { ...prev };
                if (nextStore && nextStore.activeProject) {
                  merged.activeProject = nextStore.activeProject;
                  merged.activeProjectId = nextStore.activeProjectId || merged.activeProjectId;
                }
                if (nextStore && nextStore.activeSession) {
                  merged.activeSession = nextStore.activeSession;
                  merged.activeSessionId = nextStore.activeSessionId || merged.activeSessionId;
                }
                // Also refresh the projects + sessions lists
                if (nextStore && Array.isArray(nextStore.projects)) {
                  merged.projects = nextStore.projects;
                }
                return merged;
              });
            }}
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
          </>
          )}
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
      {settingsOpen && React.createElement(
        window.SettingsOverlay || function () { return null; },
        {
          onClose: function () { setSettingsOpen(false); },
          theme: theme,
          actualTheme: actualTheme,
          onToggleTheme: onToggleTheme,
        }
      )}
    </div>
  );
}

function WorkbenchTopbar({ project, session, activePage, onSearch, onSettings, theme, actualTheme, onToggleTheme }) {
  var title = project ? project.name : "Project";
  var pageLabels = { chat: "对话", knowledge: "知识库", schedule: "日程", memory: "记忆" };
  var sessionTitle = activePage && pageLabels[activePage] ? pageLabels[activePage] : (session ? session.title : "Task");
  var themeTitle = theme === "system" ? "跟随系统" : actualTheme === "dark" ? "深色模式" : "浅色模式";
  var themeIcon = theme === "system" ? (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor" stroke="none"/></svg>
  ) : actualTheme === "dark" ? (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/></svg>
  ) : (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  );
  return (
    <div className="workbench-topbar">
      <div className="workbench-brand">
        <div className="workbench-traffic-space"></div>
        <div className="brand-mark"></div>
        <strong>Cyrene</strong>
      </div>
      <div className="workbench-crumbs">
        <span>{title}</span>
        <span>/</span>
        <b>{sessionTitle}</b>
      </div>
      <div className="workbench-top-actions">
        <button type="button" className="workbench-search-box" onClick={onSearch} title="搜索">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>
          <span>搜索</span>
        </button>
        <button type="button" className="workbench-icon-btn workbench-notif-btn" title="通知">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>
          <span className="workbench-notif-badge"></span>
        </button>
        <button type="button" className="workbench-icon-btn" onClick={onToggleTheme} title={themeTitle}>{themeIcon}</button>
        <button type="button" className="workbench-icon-btn" title="帮助">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
        </button>
        <button type="button" className="workbench-icon-btn" onClick={onSettings} title="设置">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
        <div className="workbench-avatar">{WorkbenchModel.initials(DATA.user && DATA.user.name)}</div>
      </div>
    </div>
  );
}

function ProjectRail({ projects, activeProjectId, activePage, onSelectProject, onCreateProject, onOpenPage, onOpenLegacy }) {
  var navItems = [
    { id: "chat", label: "对话", icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 11.5a8.5 8.5 0 0 1-12.2 7.6L3 21l1.9-5.8A8.5 8.5 0 1 1 21 11.5Z"/></svg>
    ), action: function () { onOpenPage("chat"); } },
    { id: "knowledge", label: "知识库", icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M5 4.5A2.5 2.5 0 0 1 7.5 2H20v15H7.5A2.5 2.5 0 0 0 5 19.5Z"/><path d="M5 19.5A2.5 2.5 0 0 0 7.5 22H20"/></svg>
    ), action: function () { onOpenPage("knowledge"); } },
    { id: "schedule", label: "日程", icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/></svg>
    ), action: function () { onOpenPage("schedule"); } },
    { id: "memory", label: "记忆", icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 4 13.6 10.4 20 12 13.6 13.6 12 20 10.4 13.6 4 12 10.4 10.4Z"/></svg>
    ), action: function () { onOpenPage("memory"); } },
  ];
  return (
    <aside className="workbench-project-rail">
      <div className="workbench-rail-head">
        <span>项目</span>
        <button type="button" className="workbench-add-btn" onClick={onCreateProject}>
          <span>
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>
          </span>
          <span>新建项目</span>
        </button>
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
              <span
                className="workbench-project-icon"
                style={{ background: WorkbenchModel.projectGradient(project.id || project.name) }}
              >{WorkbenchModel.initials(project.name)}</span>
              <span className="workbench-project-meta">
                <b>{project.name}</b>
                <small>{project.workspacePath || "—"}</small>
              </span>
            </button>
          );
        })}
      </div>
      <div className="workbench-global-nav">
        {navItems.map(function (item) {
          return (
            <button key={item.id} type="button" className={"workbench-nav-button" + (activePage === item.id ? " active" : "")} onClick={item.action}>
              <span className="workbench-nav-icon">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
        <button type="button" className="workbench-nav-button legacy" onClick={onOpenLegacy}>
          <span className="workbench-nav-icon">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5A5.5 5.5 0 0 1 20 14.5 5.5 5.5 0 0 1 14.5 20H8"/></svg>
          </span>
          <span>旧界面</span>
        </button>
      </div>
      <div className="workbench-account">
        <div className="workbench-avatar photo">{WorkbenchModel.initials(DATA.user && DATA.user.name)}</div>
        <div className="workbench-account-meta">
          <div className="workbench-account-name">
            <b>{DATA.user && DATA.user.name || "User"}</b>
            <span className="workbench-pro-badge">Pro</span>
          </div>
          <small>{(DATA.sessions && DATA.sessions[0] && DATA.sessions[0].model) || DATA.appVersion || "model"}</small>
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
              <span className="workbench-task-top">
                <span className={"workbench-status-dot " + tone}></span>
                <b>{session.title}</b>
              </span>
              <span className="workbench-task-bottom">
                <span className={"workbench-task-status " + tone}>
                  {tone === "muted" && <i className="wb-status-ico">◷</i>}
                  {WorkbenchModel.statusText(session.status)}
                </span>
                <time>{WorkbenchModel.formatTime(session.updatedAt || session.createdAt)}</time>
              </span>
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
  var isNewSession = !session.plan || session.plan.length === 0;
  var hasTaskStructure = Boolean(session.agentReply || (session.plan && session.plan.length));
  return (
    <main className="workbench-main">
      <TaskHeader project={project} session={session} />
      {props.error && <div className="workbench-error">{props.error}</div>}
      {isNewSession ? (
        <SimpleChatView session={session} onRefresh={props.onRefresh} />
      ) : hasTaskStructure ? (
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
      ) : (
        <InitialTaskConversation session={session} onCreateRun={props.onCreateRun} />
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

function SimpleChatView({ session, onRefresh }) {
  var model = window.WorkbenchModel;
  var [draft, setDraft] = useWorkbenchState("");
  var [busy, setBusy] = useWorkbenchState(false);
  var taRef = useWorkbenchRef(null);

  function syncHeight() {
    var ta = taRef.current;
    if (ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; }
  }

  function handleSend() {
    var msg = draft.trim();
    if (!msg || busy) return;
    setBusy(true);
    model.sendChat(session.id, msg)
      .then(function (next) {
        setDraft("");
        if (taRef.current) taRef.current.style.height = "";
        onRefresh && onRefresh(next);
      })
      .catch(function (err) {
        window.alert(err.message || String(err));
      })
      .finally(function () {
        setBusy(false);
      });
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      handleSend();
    }
  }

  var reply = String(session && session.agentReply || "").trim();
  var hints = [
    "解释一下这个项目的代码结构",
    "帮我检查代码中的潜在问题",
    "分析这个项目的依赖关系",
  ];

  return (
    <div className="wb-chat-view">
      {reply ? (
        <section className="workbench-agent-reply">
          <div className="workbench-panel-title">
            <span>✦</span>
            <b>Agent 回复</b>
          </div>
          <div className="workbench-agent-body">
            {reply.split("\n").map(function (line, index) {
              return <p key={index}>{line}</p>;
            })}
          </div>
        </section>
      ) : (
        <div className="wb-chat-empty">
          <div className="wb-chat-empty-icon">
            <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          </div>
          <p>开始一段新的对话，Agent 会直接回答你的问题。</p>
        </div>
      )}
      {!reply && (
        <div className="wb-chat-greeting">
          <h3>有什么我可以帮你的？</h3>
          <p>输入一个问题或任务，Agent 会直接回复，不涉及执行流程。</p>
        </div>
      )}
      <div className="wb-chat-composer">
        <div className="wb-chat-input-row">
          <textarea
            ref={taRef}
            value={draft}
            onChange={function (e) { setDraft(e.target.value); syncHeight(); }}
            onKeyDown={handleKeyDown}
            placeholder="发送消息…"
            rows={1}
            disabled={busy}
          />
          <button
            type="button"
            className="wb-chat-send"
            onClick={handleSend}
            disabled={busy || !draft.trim()}
          >
            {busy ? (
              <span className="wb-spinner" />
            ) : (
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4Z"/></svg>
            )}
          </button>
        </div>
        <div className="wb-chat-hints">
          {hints.map(function (hint, index) {
            return (
              <button
                key={index}
                type="button"
                className="wb-chat-hint-chip"
                onClick={function () {
                  setDraft(hint);
                  syncHeight();
                  if (taRef.current) taRef.current.focus();
                }}
              >
                {hint}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function AgentReplyPanel({ session }) {
  var reply = String(session && session.agentReply || "").trim();
  return (
    <section className="workbench-agent-reply">
      <div className="workbench-panel-title">
        <span>✦</span>
        <b>Agent 回复</b>
      </div>
      {reply && (
        <div className="workbench-agent-body">
          {reply.split("\n").map(function (line, index) {
            return <p key={index}>{line}</p>;
          })}
        </div>
      )}
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
  if (page === "memory") {
    return { title: "记忆", render: function () { return React.createElement(window.MemoryPage || function () { return <div className="workbench-empty">记忆加载中...</div>; }); } };
  }
  return { title: page, render: function () { return <div className="workbench-empty">未找到页面。</div>; } };
}

window.WorkbenchApp = WorkbenchApp;
