// Four-column Project / Task Session workbench.
var {
  useState: useWorkbenchState,
  useEffect: useWorkbenchEffect,
  useMemo: useWorkbenchMemo,
  useRef: useWorkbenchRef,
} = React;

function WorkbenchApp({ theme, actualTheme, onToggleTheme }) {
  useDataVersion();
  var workbenchI18n = window.useWorkbenchI18n();
  var t = workbenchI18n.t;
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
  var [newProjectOpen, setNewProjectOpen] = useWorkbenchState(false);
  var [newTaskOpen, setNewTaskOpen] = useWorkbenchState(false);
  var [editProject, setEditProject] = useWorkbenchState(null);
  var [chatCrumb, setChatCrumb] = useWorkbenchState("");
  var [notifications, setNotifications] = useWorkbenchState({ items: [], counts: { all: 0, mention: 0, comment: 0, system: 0 }, unreadByTab: { all: 0, mention: 0, comment: 0, system: 0 }, unreadCount: 0 });

  function reloadNotifications(tab, limit) {
    return model.fetchNotifications(tab || "all", limit || 80).then(function (payload) {
      setNotifications({
        items: Array.isArray(payload.items) ? payload.items : [],
        counts: payload.counts || { all: 0, mention: 0, comment: 0, system: 0 },
        unreadByTab: payload.unreadByTab || { all: 0, mention: 0, comment: 0, system: 0 },
        unreadCount: Number(payload.unreadCount || 0),
      });
      return payload;
    }).catch(function () {});
  }

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
    reloadNotifications();
  }, []);

  useWorkbenchEffect(function () {
    function handleEvent(data) {
      if (!data || data.type !== "notification") return;
      reloadNotifications();
    }
    if (window.__sseHandlers && window.__sseHandlers.add) {
      window.__sseHandlers.add(handleEvent);
      return function () {
        window.__sseHandlers.delete(handleEvent);
      };
    }
    return undefined;
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

  // New project / task creation now goes through dedicated workbench modals
  // (WorkbenchNewProjectModal / WorkbenchNewTaskModal). These handlers perform
  // the actual API calls; the rail buttons just open the modals.
  function createProject() { setNewProjectOpen(true); }
  function createSession() { if (store.activeProject) setNewTaskOpen(true); }

  function handleCreateProject(input) {
    // The backend opens the new project onto its agent-led init session and
    // returns it as the active session, so we just adopt the new store.
    return model.createProject(input).then(function (next) {
      setStore(next);
      setExpandedStepId("");
      setRightTab("context");
      return next;
    });
  }

  function handleCreateSession(input) {
    if (!store.activeProject) return Promise.resolve();
    return model.createSession(store.activeProject.id, input).then(function (next) {
      setStore(next);
      setExpandedStepId("");
      return next;
    });
  }

  function handleUpdateProject(projectId, input) {
    return model.updateProject(projectId, input).then(function (next) {
      setStore(next);
      return next;
    });
  }

  function handleDeleteProject(project) {
    if (!project) return Promise.resolve();
    if (!window.confirm(wbT("project.confirmDelete", "Delete project \"{name}\"? Data inside the project will also be deleted.", { name: project.name }))) return Promise.resolve();
    return model.deleteProject(project.id).then(function (next) {
      setStore(next);
      setFullPage(null);
      setExpandedStepId("");
      return next;
    }).catch(function (err) {
      setError(err.message || String(err));
    });
  }

  function handleRunCreated(next) {
    setStore(next);
    setExpandedStepId(next.activeSession && next.activeSession.plan[0] ? next.activeSession.plan[0].id : "");
    setRightTab("context");
  }

  function handleOpenPage(page) {
    if (page === "task") { setFullPage(null); return; }
    setFullPage(function (prev) { return prev === page ? null : page; });
  }

  // Conversation → task promotion: the chat page returns the refreshed store
  // (active = the new task session); adopt it and jump back to the task view.
  function handleChatToTask(payload) {
    var next = model.normalizeStore(payload);
    setStore(next);
    setFullPage(null);
    setExpandedStepId("");
    setRightTab("context");
  }

  // The 知识库 / 日程 / 记忆 / 对话 views keep the ProjectRail (so you can
  // navigate while viewing them); other pages take over the full screen.
  var isKnowledge = fullPage === "knowledge";
  var isSchedule = fullPage === "schedule";
  var isMemory = fullPage === "memory";
  var isChat = fullPage === "chat";
  var isModulePage = isKnowledge || isSchedule || isMemory || isChat;
  var fullPageConfig = fullPage && !isModulePage ? workbenchFullPageConfig(fullPage, setFullPage, store) : null;

  return (
    <div className="workbench-shell" data-screen-label="Cyrene · workbench">
      <WorkbenchTopbar
        project={store.activeProject}
        session={store.activeSession}
        activePage={fullPage}
        chatCrumb={chatCrumb}
        notifications={notifications}
        onReloadNotifications={reloadNotifications}
        onSearch={function () { setSearchOpen(true); }}
        onSettings={function () { setSettingsOpen(true); }}
        theme={theme}
        actualTheme={actualTheme}
        onToggleTheme={onToggleTheme}
      />
      {fullPageConfig ? (
        <WorkbenchFullPage config={fullPageConfig} onClose={function () { setFullPage(null); }} />
      ) : (
        <div className={"workbench-grid" + (isKnowledge ? " is-knowledge" : "") + (isSchedule ? " is-schedule" : "") + (isMemory ? " is-memory" : "") + (isChat ? " is-chat" : "")}>
          <ProjectRail
            projects={store.projects}
            activeProjectId={store.activeProjectId}
            activePage={fullPage}
            onSelectProject={selectProject}
            onCreateProject={createProject}
            onEditProject={setEditProject}
            onDeleteProject={handleDeleteProject}
            onOpenPage={handleOpenPage}
          />
          {isChat ? (
            React.createElement(window.WorkbenchChatPage || function () { return <div className="workbench-empty">{t("workbench.chatLoading")}</div>; }, {
              project: store.activeProject,
              onOpenTask: handleChatToTask,
              onActiveChatChange: setChatCrumb,
            })
          ) : isKnowledge ? (
            React.createElement(window.WorkbenchKnowledgePage || function () { return <div className="workbench-empty">{t("workbench.knowledgeLoading")}</div>; }, { project: store.activeProject, onBack: function () { setFullPage(null); } })
          ) : isSchedule ? (
            React.createElement(window.WorkbenchSchedulePage || function () { return <div className="workbench-empty">{t("workbench.scheduleLoading")}</div>; }, { project: store.activeProject, onBack: function () { setFullPage(null); } })
          ) : isMemory ? (
            React.createElement(window.WorkbenchMemoryPage || function () { return <div className="workbench-empty">{t("workbench.memoryLoading")}</div>; }, { project: store.activeProject, onBack: function () { setFullPage(null); } })
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
            onSelectSession={selectSession}
            onCreateSession={createSession}
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
            onRefresh={function (nextStore) {
              setStore(function (prev) {
                var merged = { ...prev };
                if (nextStore && nextStore.activeProject) merged.activeProject = nextStore.activeProject;
                if (nextStore && nextStore.activeSession) merged.activeSession = nextStore.activeSession;
                if (nextStore && Array.isArray(nextStore.projects)) merged.projects = nextStore.projects;
                return merged;
              });
            }}
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
      {newProjectOpen && window.WorkbenchNewProjectModal && React.createElement(
        window.WorkbenchNewProjectModal,
        {
          defaultWorkspacePath: store.activeProject && store.activeProject.workspacePath,
          onClose: function () { setNewProjectOpen(false); },
          onCreate: function (input) {
            return handleCreateProject(input).then(function () { setNewProjectOpen(false); });
          },
        }
      )}
      {editProject && (
        <WorkbenchEditProjectModal
          project={editProject}
          onClose={function () { setEditProject(null); }}
          onSave={function (input) {
            return handleUpdateProject(editProject.id, input).then(function () { setEditProject(null); });
          }}
        />
      )}
      {newTaskOpen && window.WorkbenchNewTaskModal && React.createElement(
        window.WorkbenchNewTaskModal,
        {
          onClose: function () { setNewTaskOpen(false); },
          onCreate: function (input) {
            return handleCreateSession(input).then(function () { setNewTaskOpen(false); });
          },
        }
      )}
    </div>
  );
}

function WorkbenchTopbar({ project, session, activePage, chatCrumb, notifications, onReloadNotifications, onSearch, onSettings, theme, actualTheme, onToggleTheme }) {
  var { t } = window.useWorkbenchI18n();
  var title = project ? project.name : "Project";
  var pageLabels = { chat: t("workbench.page.chat"), knowledge: t("workbench.page.knowledge"), schedule: t("workbench.page.schedule"), memory: t("workbench.page.memory") };
  var sessionTitle = activePage && pageLabels[activePage] ? pageLabels[activePage] : (session ? session.title : t("workbench.page.task"));
  var chatTail = activePage === "chat" ? String(chatCrumb || "").trim() : "";
  var themeTitle = theme === "system" ? t("workbench.theme.system") : actualTheme === "dark" ? t("workbench.theme.dark") : t("workbench.theme.light");
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
        {chatTail ? (
          <>
            <span>{sessionTitle}</span>
            <span>/</span>
            <b>{chatTail}</b>
          </>
        ) : (
          <b>{sessionTitle}</b>
        )}
      </div>
      <div className="workbench-top-actions">
        <button type="button" className="workbench-search-box" onClick={onSearch} title={t("workbench.search")}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>
          <span>{t("workbench.search")}</span>
        </button>
        <WorkbenchNotificationCenter notifications={notifications} onReload={onReloadNotifications} onSettings={onSettings} />
        <button type="button" className="workbench-icon-btn" onClick={onToggleTheme} title={themeTitle}>{themeIcon}</button>
        <button type="button" className="workbench-icon-btn" title={t("workbench.help")}>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
        </button>
        <button type="button" className="workbench-icon-btn" onClick={onSettings} title={t("nav.settings")}>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
        <div className="workbench-avatar">{WorkbenchModel.initials(DATA.user && DATA.user.name)}</div>
      </div>
    </div>
  );
}

function WorkbenchNotificationCenter({ notifications, onReload, onSettings }) {
  var { t } = window.useWorkbenchI18n();
  var model = window.WorkbenchModel;
  var [open, setOpen] = useWorkbenchState(false);
  var [tab, setTab] = useWorkbenchState("all");
  var [busy, setBusy] = useWorkbenchState(false);
  var rootRef = useWorkbenchRef(null);
  var items = notifications && Array.isArray(notifications.items) ? notifications.items : [];
  var unreadCount = notifications && notifications.unreadCount ? notifications.unreadCount : 0;
  var counts = notifications && notifications.counts ? notifications.counts : { all: 0, mention: 0, comment: 0, system: 0 };

  useWorkbenchEffect(function () {
    if (!open) return undefined;
    function handlePointer(event) {
      if (rootRef.current && !rootRef.current.contains(event.target)) setOpen(false);
    }
    function handleKey(event) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return function () {
      document.removeEventListener("mousedown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  useWorkbenchEffect(function () {
    if (!open) return;
    onReload && onReload(tab, 80);
  }, [open, tab]);

  function markRead(ids, markAll) {
    setBusy(true);
    return model.markNotificationsRead(ids, markAll).then(function (payload) {
      if (onReload) onReload(tab, 80);
      return payload;
    }).finally(function () {
      setBusy(false);
    });
  }

  return (
    <div className={"workbench-notif-anchor" + (open ? " open" : "")} ref={rootRef}>
      <button type="button" className={"workbench-icon-btn workbench-notif-btn" + (open ? " active" : "")} title={t("notifications.title")} onClick={function () { setOpen(!open); }}>
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>
        {unreadCount > 0 ? <span className="workbench-notif-badge">{unreadCount > 99 ? "99+" : unreadCount}</span> : null}
      </button>
      {open ? (
        <div className="workbench-notif-popover">
          <div className="workbench-notif-popover-arrow"></div>
          <div className="workbench-notif-head">
            <b>{t("notifications.title")}</b>
            <button type="button" className="workbench-notif-markread" disabled={busy || !unreadCount} onClick={function () { markRead([], true); }}>{t("notifications.markAllRead")}</button>
          </div>
          <div className="workbench-notif-tabs">
            {[
              { id: "all", label: t("notifications.tab.all") },
              { id: "mention", label: t("notifications.tab.mention") },
              { id: "comment", label: t("notifications.tab.comment") },
              { id: "system", label: t("notifications.tab.system") },
            ].map(function (item) {
              return (
                <button key={item.id} type="button" className={"workbench-notif-tab" + (tab === item.id ? " active" : "")} onClick={function () { setTab(item.id); }}>
                  <span>{item.label}</span>
                </button>
              );
            })}
            <button type="button" className="workbench-notif-settings" onClick={onSettings} title={t("notifications.settings")}>
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          </div>
          <div className="workbench-notif-list">
            {!items.length ? <div className="workbench-notif-empty">{t("notifications.empty")}</div> : items.map(function (item) {
              return <WorkbenchNotificationItem key={item.id} item={item} onOpen={function () { markRead([item.id], false); }} />;
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function WorkbenchNotificationItem({ item, onOpen }) {
  var { t } = window.useWorkbenchI18n();
  var tab = String(item && item.tab || "system");
  var iconClass = "system";
  var icon = null;
  if (tab === "mention") {
    iconClass = "mention";
    icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4.5"/><path d="M16.5 12v1a2.5 2.5 0 0 0 5 0V12a9.5 9.5 0 1 0-3 6.9"/></svg>;
  } else if (tab === "comment") {
    iconClass = "comment";
    icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor"><path d="M12 2.5 13.7 9 20 10.7 13.7 12.4 12 19l-1.7-6.6L4 10.7 10.3 9Z"/></svg>;
  } else {
    var src = String(item && item.source || "");
    if (src.indexOf("knowledge") === 0) {
      iconClass = "upload";
      icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V6"/><path d="m8.5 9.5 3.5-3.5 3.5 3.5"/><path d="M20 16.5a4 4 0 0 1-4 4H8a4 4 0 1 1 .9-7.9A5 5 0 0 1 18 10a4 4 0 0 1 2 6.5Z"/></svg>;
    } else if (src.indexOf("schedule") === 0 || src.indexOf("scheduled") === 0) {
      iconClass = "schedule";
      icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/></svg>;
    } else if (src.indexOf("task") === 0) {
      iconClass = "success";
      icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.7 2.7L16 9.4"/></svg>;
    } else {
      iconClass = "system";
      icon = <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>;
    }
  }
  return (
    <button type="button" className={"workbench-notif-item" + (item.read ? "" : " unread")} onClick={onOpen}>
      <span className={"workbench-notif-item-icon " + iconClass}>{icon}</span>
      <span className="workbench-notif-item-main">
        <span className="workbench-notif-item-top">
          <b>{item.title}</b>
          <time>{window.WorkbenchModel.formatRelativeTime(item.createdAt)}</time>
        </span>
        {item.body ? <span className="workbench-notif-item-body">{item.body}</span> : null}
        <span className="workbench-notif-item-meta">{item.sourceLabel || item.projectName || item.linkLabel || t("notifications.title")}</span>
      </span>
    </button>
  );
}

function WorkbenchEditProjectModal({ project, onClose, onSave }) {
  var { t } = window.useWorkbenchI18n();
  var [name, setName] = useWorkbenchState(project.name || "");
  var [description, setDescription] = useWorkbenchState(project.description || "");
  var [workspacePath, setWorkspacePath] = useWorkbenchState(project.workspacePath || "");
  var [color, setColor] = useWorkbenchState(project.color || "#22b07a");
  var [busy, setBusy] = useWorkbenchState(false);
  var [error, setError] = useWorkbenchState("");
  function save() {
    var trimmed = name.trim();
    if (!trimmed) { setError(t("create.project.error.nameRequired")); return; }
    setBusy(true);
    setError("");
    Promise.resolve(onSave({
      name: trimmed,
      description: description.trim(),
      workspacePath: workspacePath.trim(),
      color: color,
    })).catch(function (err) {
      setBusy(false);
      setError(err.message || String(err));
    });
  }
  return (
    <div className="workbench-modal-scrim" onMouseDown={function (e) { if (e.target === e.currentTarget) onClose(); }}>
      <div className="workbench-project-edit-modal" role="dialog" aria-modal="true">
        <div className="workbench-project-edit-head">
          <b>{t("rail.editProject")}</b>
          <button type="button" className="workbench-icon-btn" onClick={onClose} title={t("common.close")}>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <div className="workbench-project-edit-body">
          <label>{t("create.project.name")}</label>
          <input value={name} maxLength={60} onChange={function (e) { setName(e.target.value); }} />
          <label>{t("create.project.description")}</label>
          <textarea value={description} rows={3} maxLength={240} onChange={function (e) { setDescription(e.target.value); }} />
          <label>{t("create.project.workspacePath")}</label>
          <input value={workspacePath} onChange={function (e) { setWorkspacePath(e.target.value); }} />
          <label>{t("create.project.color")}</label>
          <input className="workbench-project-color-input" type="color" value={color || "#22b07a"} onChange={function (e) { setColor(e.target.value); }} />
        </div>
        {error && <div className="workbench-project-edit-error">{error}</div>}
        <div className="workbench-project-edit-foot">
          <button type="button" className="wb-btn ghost" disabled={busy} onClick={onClose}>{t("common.cancel")}</button>
          <button type="button" className="wb-btn primary" disabled={busy} onClick={save}>{busy ? t("settings.saving") : t("common.save")}</button>
        </div>
      </div>
    </div>
  );
}

function ProjectRail({ projects, activeProjectId, activePage, onSelectProject, onCreateProject, onEditProject, onDeleteProject, onOpenPage }) {
  var { t } = window.useWorkbenchI18n();
  var [menuProjectId, setMenuProjectId] = useWorkbenchState("");

  useWorkbenchEffect(function () {
    if (!menuProjectId) return undefined;
    function closeMenu(event) {
      if (event.key && event.key !== "Escape") return;
      if (!event.key && event.target && event.target.closest && event.target.closest(".workbench-project-card.menu-open")) return;
      setMenuProjectId("");
    }
    document.addEventListener("mousedown", closeMenu);
    document.addEventListener("keydown", closeMenu);
    return function () {
      document.removeEventListener("mousedown", closeMenu);
      document.removeEventListener("keydown", closeMenu);
    };
  }, [menuProjectId]);

  var navItems = [
    { id: "task", label: t("workbench.page.task"), icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1.5"/><path d="M9 14 10.5 15.5 15 11"/></svg>
    ), action: function () { onOpenPage("task"); } },
    { id: "chat", label: t("workbench.page.chat"), icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 11.5a8.5 8.5 0 0 1-12.2 7.6L3 21l1.9-5.8A8.5 8.5 0 1 1 21 11.5Z"/></svg>
    ), action: function () { onOpenPage("chat"); } },
    { id: "knowledge", label: t("workbench.page.knowledge"), icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M5 4.5A2.5 2.5 0 0 1 7.5 2H20v15H7.5A2.5 2.5 0 0 0 5 19.5Z"/><path d="M5 19.5A2.5 2.5 0 0 0 7.5 22H20"/></svg>
    ), action: function () { onOpenPage("knowledge"); } },
    { id: "schedule", label: t("workbench.page.schedule"), icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/></svg>
    ), action: function () { onOpenPage("schedule"); } },
    { id: "memory", label: t("workbench.page.memory"), icon: (
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 4 13.6 10.4 20 12 13.6 13.6 12 20 10.4 13.6 4 12 10.4 10.4Z"/></svg>
    ), action: function () { onOpenPage("memory"); } },
  ];
  return (
    <aside className="workbench-project-rail">
      <div className="workbench-rail-head">
        <span>{t("rail.projects")}</span>
        <button type="button" className="workbench-add-btn" onClick={onCreateProject}>
          <span>
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>
          </span>
          <span>{t("rail.newProject")}</span>
        </button>
      </div>
      <div className="workbench-project-list">
        {projects.map(function (project) {
          var active = project.id === activeProjectId;
          var isCyrene = project.dataKey === "default" || project.name === "Cyrene";
          var menuOpen = menuProjectId === project.id;
          return (
            <div
              key={project.id}
              className={"workbench-project-card" + (active ? " active" : "") + (menuOpen ? " menu-open" : "")}
              title={project.workspacePath}
            >
              <button type="button" className="workbench-project-main" onClick={function () { onSelectProject(project.id); setMenuProjectId(""); }}>
                <span
                  className={"workbench-project-icon" + (isCyrene ? " logo" : "")}
                  style={isCyrene ? null : { background: project.color || WorkbenchModel.projectGradient(project.id || project.name) }}
                >{isCyrene ? <span className="brand-mark" aria-hidden="true"></span> : WorkbenchModel.initials(project.name)}</span>
                <span className="workbench-project-meta">
                  <b>{project.name}</b>
                  <small title={project.workspacePath || ""}>{WorkbenchModel.pathLabel(project.workspacePath, project.name)}</small>
                </span>
              </button>
              <button
                type="button"
                className="workbench-project-menu-btn"
                title={t("rail.projectActions")}
                onClick={function (e) {
                  e.stopPropagation();
                  setMenuProjectId(menuOpen ? "" : project.id);
                }}
              >
                <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>
              </button>
              {menuOpen && (
                <div className="workbench-project-menu">
                  <button type="button" onClick={function () { setMenuProjectId(""); onEditProject(project); }}>{t("rail.editProject")}</button>
                  <button type="button" className="danger" onClick={function () { setMenuProjectId(""); onDeleteProject(project); }}>{t("rail.deleteProject")}</button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="workbench-global-nav">
        {navItems.map(function (item) {
          return (
            <button key={item.id} type="button" className={"workbench-nav-button" + ((activePage === item.id || (item.id === "task" && !activePage)) ? " active" : "")} onClick={item.action}>
              <span className="workbench-nav-icon">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
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
  var { t } = window.useWorkbenchI18n();
  var sessions = project && Array.isArray(project.sessions) ? project.sessions : [];
  return (
    <aside className="workbench-task-rail">
      <div className="workbench-rail-head">
        <span>{t("rail.tasks")}</span>
        <button type="button" onClick={onCreateSession} disabled={!project}>+ {t("rail.newTask")}</button>
      </div>
      {loading && <div className="workbench-muted">{t("rail.loadingTasks")}</div>}
      {!loading && sessions.length === 0 && <div className="workbench-muted">{t("rail.noTasks")}</div>}
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

// ===================================================================
// Task execution console — the Subtask state machine.
// idle → planning → waiting_for_approval → running → review →
// completed, with paused / failed / cancelled branches. Driven from
// the client via model.patchSession(); real agent work via createRun().
// ===================================================================

var ICONS = {
  target: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/></svg>,
  spark: <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2.5 13.7 9 20 10.7 13.7 12.4 12 19l-1.7-6.6L4 10.7 10.3 9Z"/></svg>,
  shield: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3 5 6v5c0 4.2 2.8 7.7 7 9 4.2-1.3 7-4.8 7-9V6Z"/><path d="m9.2 12 2 2 3.6-3.8"/></svg>,
  pause: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M9 5v14M15 5v14"/></svg>,
  dots: <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><circle cx="5.5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="18.5" cy="12" r="1.7"/></svg>,
  edit: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>,
  alert: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M10.3 4 2.5 18a1.5 1.5 0 0 0 1.3 2.3h16.4A1.5 1.5 0 0 0 21.5 18L13.7 4a1.5 1.5 0 0 0-3.4 0Z"/><path d="M12 9v4.5M12 17h.01"/></svg>,
  check: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.4 2.4 4.6-4.8"/></svg>,
  x: <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>,
  attach: <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m21.44 11.05-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>,
  slash: <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7.5 9.5 2.5 2.5-2.5 2.5"/><path d="M12.5 15h4"/></svg>,
  send: <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4Z"/></svg>,
  stop: <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>,
  modeDefault: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3 5 6v5c0 4.2 2.8 7.7 7 9 4.2-1.3 7-4.8 7-9V6Z"/></svg>,
  modeAuto: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg>,
  modePlan: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9.5 3.5h5v3h-5z"/><path d="M9 11h6M9 15h4"/></svg>,
  modeFull: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.6-1.7"/></svg>,
  cmdQuick: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg>,
  cmdResearch: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>,
  cmdReflect: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1v.2h6v-.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2Z"/></svg>,
  cmdDecide: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3v18M5 7h14M8 21h8"/><path d="M5 7 2.5 13a3.5 3.5 0 0 0 5 0ZM19 7l-2.5 6a3.5 3.5 0 0 0 5 0Z"/></svg>,
  cmdLearn: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2Z"/><path d="M4 19a2 2 0 0 0 2 2h13"/></svg>,
  cmdReview: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/></svg>,
  cmdCompare: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M7 4 3 8l4 4M3 8h13M17 20l4-4-4-4M21 16H8"/></svg>,
  cmdCode: <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="m8 8-4 4 4 4M16 8l4 4-4 4"/></svg>,
  checkSmall: <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12.5 4.5 4.5L19 7"/></svg>,
};

function wbT(key, fallback, params) {
  if (window.WorkbenchI18n && typeof window.WorkbenchI18n.t === "function") {
    return window.WorkbenchI18n.t(key, params, fallback);
  }
  if (params && fallback) {
    Object.keys(params).forEach(function (name) {
      fallback = fallback.split("{" + name + "}").join(String(params[name]));
    });
  }
  return fallback || key;
}

// Legacy chat slash commands, surfaced in the composer's "/" menu. Selecting one
// sets `command`, which is passed to the agent run at execution time.
var WB_SLASH_COMMANDS = [
  { id: "quick-answer", labelKey: "workbenchChat.command.quick-answer.label", descKey: "workbenchChat.command.quick-answer.desc", icon: ICONS.cmdQuick },
  { id: "deep-research", labelKey: "workbenchChat.command.deep-research.label", descKey: "workbenchChat.command.deep-research.desc", icon: ICONS.cmdResearch },
  { id: "deep-reflect", labelKey: "workbenchChat.command.deep-reflect.label", descKey: "workbenchChat.command.deep-reflect.desc", icon: ICONS.cmdReflect },
  { id: "help-me-decide", labelKey: "workbenchChat.command.help-me-decide.label", descKey: "workbenchChat.command.help-me-decide.desc", icon: ICONS.cmdDecide },
  { id: "learning-plan", labelKey: "workbenchChat.command.learning-plan.label", descKey: "workbenchChat.command.learning-plan.desc", icon: ICONS.cmdLearn },
  { id: "daily-review", labelKey: "workbenchChat.command.daily-review.label", descKey: "workbenchChat.command.daily-review.desc", icon: ICONS.cmdReview },
  { id: "deep-compare", labelKey: "workbenchChat.command.deep-compare.label", descKey: "workbenchChat.command.deep-compare.desc", icon: ICONS.cmdCompare },
  { id: "claude-code", labelKey: "workbenchChat.command.claude-code.label", descKey: "workbenchChat.command.claude-code.desc", icon: ICONS.cmdCode },
];

function wbCommandMeta(id) {
  for (var i = 0; i < WB_SLASH_COMMANDS.length; i++) {
    if (WB_SLASH_COMMANDS[i].id === id) {
      var c = WB_SLASH_COMMANDS[i];
      return { ...c, label: wbT(c.labelKey, c.id), desc: wbT(c.descKey, "") };
    }
  }
  return null;
}

// Permission modes for the composer mode-switcher (mirrors the legacy chat
// modes; the workbench default is "auto" since it executes tasks).
var WB_MODES = [
  { id: "default", labelKey: "workbenchChat.mode.default.label", descKey: "workbenchChat.mode.default.desc", icon: ICONS.modeDefault },
  { id: "auto", labelKey: "workbenchChat.mode.auto.label", descKey: "workbenchChat.mode.auto.desc", icon: ICONS.modeAuto },
  { id: "plan", labelKey: "workbenchChat.mode.plan.label", descKey: "workbenchChat.mode.plan.desc", icon: ICONS.modePlan },
  { id: "full_access", labelKey: "workbenchChat.mode.full_access.label", descKey: "workbenchChat.mode.full_access.desc", icon: ICONS.modeFull },
];

function wbModeMeta(id) {
  var meta = WB_MODES[1];
  for (var i = 0; i < WB_MODES.length; i++) {
    if (WB_MODES[i].id === id) meta = WB_MODES[i];
  }
  return { ...meta, label: wbT(meta.labelKey, meta.id), desc: wbT(meta.descKey, "") };
}

function isDoneStepStatus(status) {
  return status === "completed" || status === "done";
}

function isRunningStepStatus(status) {
  return status === "running";
}

function stepExecutionPrompt(session, step) {
  var lines = [
    "请为当前任务计划中的这个步骤生成一个 subagent 执行，并在完成后汇总结果。",
    "当前任务：" + String((session && (session.goal || session.title)) || "").trim(),
    "步骤：" + String((step && step.title) || "").trim(),
  ];
  if (step && step.description) lines.push("步骤说明：" + String(step.description).trim());
  return lines.filter(Boolean).join("\n");
}

function formatDurationSec(sec) {
  if (!Number.isFinite(sec) || sec < 1) return "";
  sec = Math.max(1, Math.round(sec));
  if (sec < 60) return sec + "s";
  var min = Math.floor(sec / 60);
  var rest = sec % 60;
  if (min < 60) return rest ? (min + "m " + rest + "s") : (min + "m");
  var hour = Math.floor(min / 60);
  var remMin = min % 60;
  return remMin ? (hour + "h " + remMin + "m") : (hour + "h");
}

// Duration of a step, in priority order: an explicit recorded `durationSec`,
// then the startedAt→completedAt/updatedAt span, then the first/last
// progress-event timestamps. Returns "" when nothing reliable is known.
function stepDurationText(step) {
  if (!step) return "";
  if (Number.isFinite(step.durationSec)) return formatDurationSec(step.durationSec);
  var startMs = step.startedAt ? Date.parse(step.startedAt) : NaN;
  var endMs = (step.completedAt || step.updatedAt) ? Date.parse(step.completedAt || step.updatedAt) : NaN;
  if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) {
    return formatDurationSec((endMs - startMs) / 1000);
  }
  if (Array.isArray(step.progressEvents) && step.progressEvents.length >= 2) {
    var first = Date.parse(step.progressEvents[0] && step.progressEvents[0].time || "");
    var last = Date.parse(step.progressEvents[step.progressEvents.length - 1] && step.progressEvents[step.progressEvents.length - 1].time || "");
    if (Number.isFinite(first) && Number.isFinite(last) && last > first) {
      return formatDurationSec((last - first) / 1000);
    }
  }
  return "";
}

function stepMetaText(step) {
  var duration = stepDurationText(step);
  if (duration) return duration;
  if (!step) return "";
  if (isRunningStepStatus(step.status)) return "进行中";
  if (isDoneStepStatus(step.status)) return "已完成";
  if (step.status === "failed") return "需处理";
  if (step.status === "paused") return "已暂停";
  return "";
}

function useTaskController(session, onRefresh, runtime) {
  var model = window.WorkbenchModel;
  var [busy, setBusy] = useWorkbenchState(false);
  var runAbortRef = useWorkbenchRef(null);
  var interruptedRef = useWorkbenchRef(false);
  var sid = session ? session.id : "";

  function apply(next) { if (onRefresh && next) onRefresh(next); return next; }
  function fail(err) { window.alert((err && err.message) || String(err)); }
  function patch(p) { return model.patchSession(sid, p); }
  function run(promise) {
    setBusy(true);
    return promise.then(apply).catch(fail).finally(function () { setBusy(false); });
  }

  var ctrl = {
    busy: busy,
    applyStore: apply,

    // idle → planning. Generate the plan + acceptance from the goal first
    // ("执行前必须有计划"); no agent work runs yet.
    start: function (goalText) {
      var goal = (goalText != null ? String(goalText) : (session.goal || "")).trim();
      var constraints = session.constraints || [];
      var plan = (session.plan && session.plan.length) ? session.plan : model.buildPlanSteps(goal, constraints);
      var accept = (session.acceptanceCriteria && session.acceptanceCriteria.length) ? session.acceptanceCriteria : model.buildAcceptance(goal, constraints);
      var events = model.withEvent(session, "PlanGenerated", "生成执行计划，共 " + plan.length + " 步。");
      return run(patch({
        status: "planning",
        goal: goal || session.goal || "",
        plan: plan,
        acceptanceCriteria: accept,
        agentReply: "我将按以下步骤执行当前任务，请你先确认计划。",
        events: events,
      }));
    },

    modifyPlan: function (text) {
      var events = model.withEvent(session, "PlanRevised", "按用户要求调整计划：" + text);
      return run(patch({ status: "planning", agentReply: "已根据你的要求调整计划：\n" + text, events: events }));
    },

    regeneratePlan: function () {
      var plan = model.buildPlanSteps(session.goal || "", session.constraints || []);
      var events = model.withEvent(session, "PlanGenerated", "重新生成执行计划。");
      return run(patch({ status: "planning", plan: plan, agentReply: "已重新生成执行计划，请确认。", events: events }));
    },

    // planning → waiting_for_approval — the 需要你确认 gate before any change.
    approvePlan: function () {
      var events = model.withEvent(session, "PlanApproved", "用户批准执行计划。");
      return run(patch({ status: "waiting_for_approval", agentReply: "执行前请确认下面的操作。", events: events }));
    },

    reject: function () {
      var events = model.withEvent(session, "ActionRejected", "用户拒绝了当前操作。");
      return run(patch({ status: "planning", agentReply: "操作已取消。你可以修改要求，或让我重新规划。", events: events }));
    },

    // waiting → running → (real agent) → review. Reused by resume / retry.
    // Sends the composer's attachments + permission mode, and is abortable.
    execute: function (inputOverride) {
      setBusy(true);
      var runStartMs = Date.now();
      interruptedRef.current = false;
      var ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
      runAbortRef.current = ac;
      var startPlan = model.markStep(session.plan, 0, "running", "正在执行第一步…");
      var startEvents = model.withEvent(session, "ExecutionStarted", "开始执行任务。");
      return patch({ status: "running", plan: startPlan, agentReply: "正在执行当前任务…", events: startEvents })
        .then(apply)
        .then(function () {
          var input = String(inputOverride || session.goal || session.title || "").trim();
          return model.createRun(sid, input || "执行当前任务", {
            attachments: (runtime && runtime.attachments) || [],
            mode: (runtime && runtime.mode) || undefined,
            command: (runtime && runtime.command) || undefined,
            signal: ac ? ac.signal : undefined,
          });
        })
        .then(function (next) {
          var s2 = next.activeSession || session;
          var donePlan = model.markAllSteps(s2.plan, "completed");
          // Agent ran the plan as one unit; spread the real elapsed time across
          // steps with no measured duration so each row still shows a 时长.
          var totalSec = Math.max(donePlan.length, Math.round((Date.now() - runStartMs) / 1000));
          var perStep = Math.max(1, Math.round(totalSec / Math.max(1, donePlan.length)));
          donePlan = donePlan.map(function (st) {
            return (st && st.durationSec != null) ? st : Object.assign({}, st, { durationSec: perStep });
          });
          var passed = model.markAllAcceptance(s2.acceptanceCriteria, "passed");
          var artifacts = model.ensureArtifacts(s2);
          var events2 = model.withEvent(s2, "ExecutionFinished", "Agent 执行完成，等待你验收。");
          if (runtime && runtime.clearAttachments) runtime.clearAttachments();
          if (runtime && runtime.clearCommand) runtime.clearCommand();
          return model.patchSession(sid, {
            status: "review", plan: donePlan, acceptanceCriteria: passed, artifacts: artifacts, events: events2,
          });
        })
        .then(apply)
        .catch(function (err) {
          // Interrupted by the user → interrupt() already moved it to paused.
          if (interruptedRef.current || (err && err.name === "AbortError")) return;
          var msg = (err && err.message) || String(err);
          // Don't leave the first step spinning as "running" after a failure.
          var failedPlan = Array.isArray(startPlan) ? startPlan.map(function (s) {
            return (s && s.status === "running") ? Object.assign({}, s, { status: "failed", error: msg, updatedAt: new Date().toISOString() }) : s;
          }) : startPlan;
          return model.patchSession(sid, {
            status: "failed",
            plan: failedPlan,
            agentReply: "执行失败：" + msg,
            events: model.withEvent(session, "ExecutionFailed", msg),
          }).then(apply).catch(fail);
        })
        .finally(function () { runAbortRef.current = null; setBusy(false); });
    },

    // Stop the in-flight run (abort the fetch + server-side interrupt) → paused.
    // A running STEP must also drop out of "running" — otherwise the plan card
    // keeps the step spinning with a live 停止 button and the click looks dead.
    // Reset startedAt so a later re-run times the step fresh.
    interrupt: function () {
      interruptedRef.current = true;
      if (runAbortRef.current) { try { runAbortRef.current.abort(); } catch (e) {} }
      model.interruptSession(sid);
      var now = new Date().toISOString();
      var stoppedPlan = Array.isArray(session.plan) ? session.plan.map(function (s) {
        if (!s || s.status !== "running") return s;
        return Object.assign({}, s, { status: "pending", startedAt: null, currentAction: "已停止，可重新执行。", updatedAt: now });
      }) : session.plan;
      return model.patchSession(sid, {
        status: "paused",
        plan: stoppedPlan,
        agentReply: "执行已被你中断，可继续或调整后重试。",
        events: model.withEvent(session, "Paused", "用户中断了执行。"),
      }).then(apply).catch(fail);
    },

    pause: function () {
      return run(patch({ status: "paused", events: model.withEvent(session, "Paused", "任务已暂停。") }));
    },

    runStep: function (step, index) {
      if (!step || index < 0) return Promise.resolve();
      setBusy(true);
      interruptedRef.current = false;
      var ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
      runAbortRef.current = ac;
      var stepTitle = String(step.title || ("步骤 " + (index + 1))).trim();
      var startPlan = model.markStep(session.plan, index, "running", "已派发给 subagent 执行…");
      var startEvents = model.withEvent(session, "ExecutionStarted", "开始执行步骤：" + stepTitle, { stepId: step.id || "" });
      return patch({ status: "running", plan: startPlan, agentReply: "正在执行步骤：" + stepTitle, events: startEvents })
        .then(apply)
        .then(function () {
          return model.createRun(sid, stepExecutionPrompt(session, step), {
            attachments: (runtime && runtime.attachments) || [],
            mode: (runtime && runtime.mode) || undefined,
            command: (runtime && runtime.command) || undefined,
            stepId: step.id || undefined,
            stepTitle: stepTitle,
            action: "spawn_subagent",
            meta: { scope: "plan_step" },
            signal: ac ? ac.signal : undefined,
          });
        })
        .then(function (next) {
          var s2 = next.activeSession || session;
          var returnedPlan = Array.isArray(s2.plan) && s2.plan.length ? s2.plan : (session.plan || []);
          var completedPlan = model.markStep(returnedPlan, index, "completed", "subagent 已完成该步骤。");
          var doneCount = completedPlan.filter(function (item) { return isDoneStepStatus(item && item.status); }).length;
          var fullyDone = doneCount >= completedPlan.length && completedPlan.length > 0;
          var events2 = model.withEvent(s2, "ExecutionFinished", "步骤「" + stepTitle + "」执行完成。", { stepId: step.id || "" });
          var finalPatch = {
            status: fullyDone ? "review" : "paused",
            plan: completedPlan,
            events: events2,
          };
          if (fullyDone) {
            finalPatch.acceptanceCriteria = model.markAllAcceptance(s2.acceptanceCriteria, "passed");
            finalPatch.artifacts = model.ensureArtifacts(s2);
          }
          if (runtime && runtime.clearAttachments) runtime.clearAttachments();
          if (runtime && runtime.clearCommand) runtime.clearCommand();
          return model.patchSession(sid, finalPatch);
        })
        .then(apply)
        .catch(function (err) {
          if (interruptedRef.current || (err && err.name === "AbortError")) return;
          var msg = (err && err.message) || String(err);
          return model.patchSession(sid, {
            status: "failed",
            plan: model.markStep(session.plan, index, "failed", msg),
            agentReply: "步骤执行失败：" + msg,
            events: model.withEvent(session, "ExecutionFailed", "步骤「" + stepTitle + "」执行失败：" + msg, { stepId: step.id || "" }),
          }).then(apply).catch(fail);
        })
        .finally(function () { runAbortRef.current = null; setBusy(false); });
    },

    resume: function () {
      return model.patchSession(sid, { events: model.withEvent(session, "Resumed", "继续执行任务。") })
        .then(apply).then(function () { return ctrl.execute(); });
    },

    retry: function () { return ctrl.execute(); },

    skipStep: function () {
      var plan = model.markAllSteps(session.plan, "completed");
      var events = model.withEvent(session, "StepSkipped", "跳过失败步骤，继续验收。");
      return run(patch({ status: "review", plan: plan, agentReply: "已跳过该步骤，请验收当前结果。", events: events }));
    },

    markComplete: function () {
      var passed = model.markAllAcceptance(session.acceptanceCriteria, "passed");
      var events = model.withEvent(session, "TaskCompleted", "用户确认任务完成。");
      return run(patch({ status: "completed", acceptanceCriteria: passed, events: events }));
    },

    reopen: function () {
      var events = model.withEvent(session, "Reopened", "重新打开任务。");
      return run(patch({ status: "planning", agentReply: "任务已重新打开，请确认计划后继续。", events: events }));
    },

    cancel: function () {
      if (!window.confirm("确定取消这个任务吗？当前进度会被保留。")) return Promise.resolve();
      return run(patch({ status: "cancelled", events: model.withEvent(session, "Cancelled", "任务已取消。") }));
    },

    createFollowUp: function (title) {
      var name = title || window.prompt("后续任务标题", (session.title || "任务") + " · 后续");
      if (!name) return Promise.resolve();
      return run(model.createSession(session.projectId, { title: name, goal: "" }));
    },
  };
  return ctrl;
}

function TaskWorkArea(props) {
  var project = props.project;
  var session = props.session;
  var [attachments, setAttachments] = useWorkbenchState([]);
  var [mode, setMode] = useWorkbenchState("auto");
  var [command, setCommand] = useWorkbenchState("");
  var sid = session ? session.id : "";
  // Pending attachments / command belong to the task being composed — reset on switch.
  useWorkbenchEffect(function () { setAttachments([]); setCommand(""); }, [sid]);
  var controller = useTaskController(session, props.onRefresh, {
    attachments: attachments,
    mode: mode,
    command: command,
    clearAttachments: function () { setAttachments([]); },
    clearCommand: function () { setCommand(""); },
  });
  if (props.loading) {
    return <main className="workbench-main"><div className="workbench-empty">正在加载工作台...</div></main>;
  }
  if (!project || !session) {
    return <main className="workbench-main"><div className="workbench-empty">请选择项目和任务。</div></main>;
  }
  // "初始化项目" onboarding sessions take over the whole work area with their
  // own agent-led question flow (WorkbenchInitView), bypassing the task state
  // machine, plan list and composer below.
  if (session.kind === "init" && window.WorkbenchInitView) {
    return (
      <main className="workbench-main">
        {React.createElement(window.WorkbenchInitView, {
          project: project,
          session: session,
          onRefresh: props.onRefresh,
        })}
      </main>
    );
  }
  var status = String(session.status || "idle");
  var showPlan = ["planning", "waiting_for_approval", "waiting_for_user", "running", "review", "paused", "failed", "done", "completed"].indexOf(status) >= 0
    && Array.isArray(session.plan) && session.plan.length > 0;
  return (
    <main className="workbench-main">
      <TaskHeader project={project} session={session} controller={controller} onRightTab={props.onRightTab} />
      {props.error && <div className="workbench-error">{props.error}</div>}
      <div className="workbench-stage">
        <StateCard
          session={session}
          project={project}
          controller={controller}
          onRightTab={props.onRightTab}
          onSelectSession={props.onSelectSession}
        />
        {showPlan && (
          <TaskPlanList
            session={session}
            expandedStepId={props.expandedStepId}
            onToggleStep={props.onToggleStep}
            onRightTab={props.onRightTab}
            controller={controller}
          />
        )}
      </div>
      <TaskComposer
        session={session}
        controller={controller}
        onRightTab={props.onRightTab}
        attachments={attachments}
        onAttachmentsChange={setAttachments}
        mode={mode}
        onModeChange={setMode}
        command={command}
        onCommandChange={setCommand}
      />
    </main>
  );
}

// Picks the primary middle card for the current task status.
function StateCard(props) {
  var status = String(props.session.status || "idle");
  if (status === "planning") return <AgentPlanCard {...props} />;
  if (status === "waiting_for_approval" || status === "waiting_for_user" || status === "blocked") return <ConfirmCard {...props} />;
  if (status === "running") return <AgentActivityCard {...props} />;
  if (status === "paused") return <PausedCard {...props} />;
  if (status === "failed") return <FailedCard {...props} />;
  if (status === "review" || status === "done") return <CompletionCard {...props} />;
  if (status === "completed") return <CompletionCard {...props} confirmed={true} />;
  if (status === "cancelled") return <CancelledCard {...props} />;
  return <TaskBriefCard {...props} />; // idle / pending / unknown
}

function priorityText(p) {
  var raw = String(p || "medium");
  return ({ high: wbT("priority.high", "High"), medium: wbT("priority.medium", "Medium"), low: wbT("priority.low", "Low") })[raw] || raw;
}

function focusComposer() {
  window.dispatchEvent(new CustomEvent("wb-focus-composer"));
}

function openNextSession(session, project, onSelectSession) {
  if (!project || !onSelectSession) return;
  var sessions = Array.isArray(project.sessions) ? project.sessions : [];
  var idx = sessions.findIndex(function (s) { return s.id === session.id; });
  var next = sessions[idx + 1] || sessions[0];
  if (next && next.id !== session.id) onSelectSession(next.id);
}

function compactText(value, limit) {
  var text = String(value || "").replace(/\s+/g, " ").trim();
  var max = limit || 120;
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1) + "..." : text;
}

function sessionSummaryText(session) {
  if (!session) return "";
  var summary = session.summary;
  if (summary && typeof summary === "object") {
    summary = summary.text || summary.body || summary.content || summary.summary || "";
  }
  return compactText(summary || session.goal || session.agentReply || "Agent 会在执行过程中生成这个 session 的内容总结。", 128);
}

function TaskHeader({ project, session, controller, onRightTab }) {
  var tone = WorkbenchModel.statusTone(session.status);
  var status = String(session.status || "idle");
  var [editing, setEditing] = useWorkbenchState(false);
  var [draftTitle, setDraftTitle] = useWorkbenchState(session.title || "");
  var [savingTitle, setSavingTitle] = useWorkbenchState(false);
  var [menuOpen, setMenuOpen] = useWorkbenchState(false);
  var titleInputRef = useWorkbenchRef(null);

  useWorkbenchEffect(function () {
    setDraftTitle(session.title || "");
    setEditing(false);
    setMenuOpen(false);
  }, [session.id]);

  useWorkbenchEffect(function () {
    if (editing && titleInputRef.current) {
      titleInputRef.current.focus();
      titleInputRef.current.select();
    }
  }, [editing]);

  function saveTitle() {
    var nextTitle = String(draftTitle || "").trim();
    if (!nextTitle || nextTitle === session.title) {
      setDraftTitle(session.title || "");
      setEditing(false);
      return;
    }
    setSavingTitle(true);
    window.WorkbenchModel.patchSession(session.id, { title: nextTitle })
      .then(function (next) {
        if (controller && controller.applyStore) controller.applyStore(next);
      })
      .catch(function (err) {
        window.alert((err && err.message) || String(err));
        setDraftTitle(session.title || "");
      })
      .finally(function () {
        setSavingTitle(false);
        setEditing(false);
      });
  }

  return (
    <div className="workbench-task-header">
      <div className="wb-th-main">
        <div className="wb-th-title-row">
          {editing ? (
            <input
              ref={titleInputRef}
              className="wb-th-title-input"
              value={draftTitle}
              disabled={savingTitle}
              onChange={function (e) { setDraftTitle(e.target.value); }}
              onBlur={saveTitle}
              onKeyDown={function (e) {
                if (e.key === "Enter") saveTitle();
                if (e.key === "Escape") { setDraftTitle(session.title || ""); setEditing(false); }
              }}
              aria-label="任务标题"
            />
          ) : (
            <h1 title={session.title}>{session.title}</h1>
          )}
          {!editing && (
            <button type="button" className="wb-th-iconbtn" onClick={function () { setEditing(true); }} title="修改标题">
              {ICONS.edit}
            </button>
          )}
        </div>
        <p className="wb-th-summary">
          <span className={"wb-th-inline-status " + tone}>{WorkbenchModel.statusText(session.status)}</span>
          <span className="wb-th-summary-text">{sessionSummaryText(session)}</span>
        </p>
        <div className="wb-th-meta">
          <span>优先级 {priorityText(session.priority)}</span>
          <span>{project.name}</span>
        </div>
      </div>
      <div className="wb-th-action-wrap">
        <HeaderActions status={status} controller={controller} onRightTab={onRightTab} />
        <button
          type="button"
          className="wb-th-control-btn wb-th-pause"
          disabled={controller.busy || status === "paused" || status === "completed" || status === "cancelled"}
          onClick={function () { status === "running" ? controller.interrupt() : controller.pause(); }}
          title="暂停任务"
          aria-label="暂停任务"
        >
          {ICONS.pause}
        </button>
        <div className="wb-th-menu-wrap">
          <button type="button" className="wb-th-control-btn wb-th-menu-btn" onClick={function () { setMenuOpen(!menuOpen); }} title="详情菜单" aria-label="详情菜单">
            {ICONS.dots}
          </button>
          {menuOpen && (
            <>
              <div className="wb-th-menu-scrim" onClick={function () { setMenuOpen(false); }}></div>
              <div className="wb-th-menu">
                <button type="button" onClick={function () { setMenuOpen(false); onRightTab && onRightTab("context"); }}>查看上下文</button>
                <button type="button" onClick={function () { setMenuOpen(false); onRightTab && onRightTab("logs"); }}>运行日志</button>
                <button type="button" onClick={function () { setMenuOpen(false); onRightTab && onRightTab("acceptance"); }}>验收标准</button>
                <button type="button" onClick={function () { setMenuOpen(false); focusComposer(); }}>编辑任务内容</button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// Top-right action buttons; the set changes with the task status.
function HeaderActions({ status, controller, onRightTab }) {
  var btns = [];
  if (status === "idle" || status === "pending") {
    btns = [];
  } else if (status === "planning") {
    btns = [["批准执行", "primary", function () { controller.approvePlan(); }], ["取消", "ghost", function () { controller.cancel(); }]];
  } else if (status === "waiting_for_approval" || status === "waiting_for_user" || status === "blocked") {
    btns = [["批准", "primary", function () { controller.execute(); }], ["拒绝", "ghost", function () { controller.reject(); }]];
  } else if (status === "running") {
    btns = [["查看日志", "ghost", function () { onRightTab && onRightTab("logs"); }, true]];
  } else if (status === "paused") {
    btns = [["继续任务", "primary", function () { controller.resume(); }], ["取消", "ghost", function () { controller.cancel(); }]];
  } else if (status === "failed") {
    btns = [["重试", "primary", function () { controller.retry(); }], ["取消", "ghost", function () { controller.cancel(); }]];
  } else if (status === "review" || status === "done") {
    btns = [["标记完成", "primary", function () { controller.markComplete(); }], ["创建后续任务", "ghost", function () { controller.createFollowUp(); }]];
  } else if (status === "completed") {
    btns = [["重新打开", "ghost", function () { controller.reopen(); }], ["创建后续任务", "ghost", function () { controller.createFollowUp(); }]];
  } else if (status === "cancelled") {
    btns = [["重新打开", "primary", function () { controller.reopen(); }]];
  }
  if (!btns.length) return null;
  return (
    <div className="wb-th-actions">
      {btns.map(function (b, i) {
        return <button key={i} type="button" className={"wb-btn " + b[1]} disabled={b[3] ? false : controller.busy} onClick={b[2]}>{b[0]}</button>;
      })}
    </div>
  );
}

// ---- Shared card primitives ------------------------------------------------

function WbCard({ tone, icon, title, badge, children }) {
  return (
    <section className={"wb-card" + (tone ? " " + tone : "")}>
      <div className="wb-card-head">
        <span className="wb-card-icon">{icon}</span>
        <b>{title}</b>
        {badge}
      </div>
      {children}
    </section>
  );
}

function WbActions({ children }) {
  return <div className="wb-card-actions">{children}</div>;
}

function WbBtn({ kind, onClick, disabled, children }) {
  return (
    <button type="button" className={"wb-btn" + (kind ? " " + kind : "")} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

function AgentReplyBlock({ text }) {
  var reply = String(text || "").trim();
  if (!reply) return null;
  return (
    <div className="wb-agent-body">
      {reply.split("\n").map(function (line, i) { return <p key={i}>{line || " "}</p>; })}
    </div>
  );
}

// ---- State cards -----------------------------------------------------------

// idle / pending — task detail + 开始执行.
function TaskBriefCard({ session, controller }) {
  var goal = String(session.goal || "").trim();
  var constraints = Array.isArray(session.constraints) ? session.constraints : [];
  var accept = Array.isArray(session.acceptanceCriteria) ? session.acceptanceCriteria : [];
  var hasGoal = !!goal;
  return (
    <WbCard tone="brief" icon={ICONS.target} title="任务详情">
      {hasGoal ? (
        <div className="wb-brief">
          <div className="wb-brief-row"><label>任务目标</label><p>{goal}</p></div>
          {constraints.length > 0 && (
            <div className="wb-brief-row"><label>执行约束</label>
              <ul className="wb-bullet">{constraints.map(function (c, i) { return <li key={i}>{c}</li>; })}</ul>
            </div>
          )}
          {accept.length > 0 && (
            <div className="wb-brief-row"><label>验收标准</label>
              <ul className="wb-bullet">{accept.map(function (a) { return <li key={a.id}>{a.text}</li>; })}</ul>
            </div>
          )}
        </div>
      ) : (
        <p className="wb-card-hint">先在下方输入框描述这个任务的目标、边界和验收标准，Agent 会把它整理成结构化任务，然后再开始执行。</p>
      )}
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy || !hasGoal} onClick={function () { controller.start(); }}>开始执行</WbBtn>
        {accept.length > 0 && <WbBtn kind="ghost" disabled={controller.busy} onClick={function () { controller.regeneratePlan(); }}>重新规划</WbBtn>}
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>编辑任务</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// planning — Agent 回复 with the proposed plan.
function AgentPlanCard({ session, controller, onRightTab }) {
  var plan = Array.isArray(session.plan) ? session.plan : [];
  return (
    <WbCard tone="agent" icon={ICONS.spark} title="Agent 回复">
      <AgentReplyBlock text={session.agentReply || "我将按以下步骤执行当前任务。"} />
      <div className="wb-brief-row"><label>执行步骤</label>
        <ol className="wb-ordered">{plan.map(function (s) { return <li key={s.id}>{s.title}</li>; })}</ol>
      </div>
      <p className="wb-card-hint">是否继续？批准后会进入确认环节，再开始执行。</p>
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.approvePlan(); }}>批准执行</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>修改计划</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={function () { controller.regeneratePlan(); }}>重新生成</WbBtn>
        <WbBtn kind="danger" disabled={controller.busy} onClick={function () { controller.cancel(); }}>取消任务</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// waiting_for_approval — the 需要你确认 card before a sensitive run.
function ConfirmCard({ session, controller, onRightTab }) {
  var summary = window.WorkbenchModel.confirmSummary(session);
  var riskTone = summary.risk === "高" ? "red" : summary.risk === "中" ? "amber" : "green";
  return (
    <WbCard tone="confirm" icon={ICONS.shield} title="需要你确认"
      badge={<span className={"wb-risk " + riskTone}>风险 {summary.risk}</span>}>
      <p className="wb-card-hint">Agent 计划进行以下操作：</p>
      <ol className="wb-ordered">{summary.actions.map(function (a, i) { return <li key={i}>{a}</li>; })}</ol>
      <div className="wb-brief-row"><label>影响范围</label>
        <ul className="wb-bullet">{summary.scope.map(function (s, i) { return <li key={i}>{s}</li>; })}</ul>
      </div>
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.execute(); }}>批准执行</WbBtn>
        <WbBtn kind="ghost" onClick={function () { onRightTab && onRightTab("context"); }}>查看详情</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>修改要求</WbBtn>
        <WbBtn kind="danger" disabled={controller.busy} onClick={function () { controller.reject(); }}>拒绝</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// running — Agent 正在处理.
function AgentActivityCard({ session, controller, onRightTab }) {
  var plan = Array.isArray(session.plan) ? session.plan : [];
  var done = plan.filter(function (s) { return s.status === "completed" || s.status === "done"; }).length;
  var current = plan.filter(function (s) { return s.status === "running"; })[0] || plan[done] || null;
  var pct = plan.length ? Math.round((done / plan.length) * 100) : 0;
  return (
    <WbCard tone="running" icon={<span className="wb-spinner" />} title="Agent 正在处理"
      badge={<span className="wb-progress-badge">{done} / {plan.length}</span>}>
      <p className="wb-running-stage">当前阶段：{current ? current.title : "执行中"}</p>
      <AgentReplyBlock text={session.agentReply || "正在处理当前任务，请稍候…"} />
      <div className="wb-progress"><span style={{ width: pct + "%" }} /></div>
      <ul className="wb-step-mini">
        {plan.map(function (s, i) {
          var st = (s.status === "completed" || s.status === "done") ? "done" : s.status === "running" ? "active" : "todo";
          return <li key={s.id} className={st}>{i + 1}. {s.title}</li>;
        })}
      </ul>
      <WbActions>
        <WbBtn kind="danger" onClick={function () { controller.interrupt(); }}>停止执行</WbBtn>
        <WbBtn kind="ghost" onClick={function () { onRightTab && onRightTab("logs"); }}>查看日志</WbBtn>
        <WbBtn kind="ghost" onClick={function () { onRightTab && onRightTab("files"); }}>查看变更</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// paused.
function PausedCard({ session, controller }) {
  var plan = Array.isArray(session.plan) ? session.plan : [];
  var done = plan.filter(function (s) { return s.status === "completed" || s.status === "done"; }).length;
  var current = plan[done] || plan[plan.length - 1] || null;
  return (
    <WbCard tone="paused" icon={ICONS.pause} title="任务已暂停">
      <p className="wb-card-hint">当前停在：第 {Math.min(done + 1, plan.length || 1)} 步{current ? "：" + current.title : ""}。</p>
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.resume(); }}>继续任务</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>修改要求</WbBtn>
        <WbBtn kind="danger" disabled={controller.busy} onClick={function () { controller.cancel(); }}>取消任务</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// failed.
function FailedCard({ session, controller }) {
  var plan = Array.isArray(session.plan) ? session.plan : [];
  var failedIdx = plan.findIndex(function (s) { return s.status === "failed"; });
  return (
    <WbCard tone="failed" icon={ICONS.alert} title="任务执行失败">
      <AgentReplyBlock text={session.agentReply || "执行过程中出现错误。"} />
      {failedIdx >= 0 && <p className="wb-card-hint">失败位置：第 {failedIdx + 1} 步：{plan[failedIdx].title}</p>}
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.retry(); }}>重试</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>修改要求</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={function () { controller.skipStep(); }}>跳过此步骤</WbBtn>
        <WbBtn kind="danger" disabled={controller.busy} onClick={function () { controller.cancel(); }}>取消任务</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// review (awaiting confirm) / completed (confirmed) — 任务完成.
function CompletionCard({ session, controller, onRightTab, onSelectSession, project, confirmed }) {
  var accept = Array.isArray(session.acceptanceCriteria) ? session.acceptanceCriteria : [];
  var passed = accept.filter(function (a) { return a.status === "passed" || a.status === "done"; }).length;
  var artifacts = Array.isArray(session.artifacts) ? session.artifacts : [];
  return (
    <WbCard tone="done" icon={ICONS.check} title={confirmed ? "任务已完成" : "Agent 已完成，待你确认"}>
      <AgentReplyBlock text={session.agentReply || "已完成当前任务。"} />
      <div className="wb-done-grid">
        <button type="button" className="wb-done-stat" onClick={function () { onRightTab && onRightTab("acceptance"); }}>
          <b>{passed} / {accept.length || 0}</b><small>验收通过</small>
        </button>
        <button type="button" className="wb-done-stat" onClick={function () { onRightTab && onRightTab("artifacts"); }}>
          <b>{artifacts.length}</b><small>产物</small>
        </button>
      </div>
      <WbActions>
        {!confirmed && <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.markComplete(); }}>标记完成</WbBtn>}
        <WbBtn kind="ghost" disabled={controller.busy} onClick={focusComposer}>继续修改</WbBtn>
        <WbBtn kind="ghost" disabled={controller.busy} onClick={function () { controller.createFollowUp(); }}>创建后续任务</WbBtn>
        <WbBtn kind="ghost" onClick={function () { openNextSession(session, project, onSelectSession); }}>打开下一个任务</WbBtn>
      </WbActions>
    </WbCard>
  );
}

// cancelled.
function CancelledCard({ session, controller }) {
  return (
    <WbCard tone="cancelled" icon={ICONS.x} title="任务已取消">
      <p className="wb-card-hint">这个任务已被取消，当前进度仍然保留。你可以重新打开它继续。</p>
      <WbActions>
        <WbBtn kind="primary" disabled={controller.busy} onClick={function () { controller.reopen(); }}>重新打开</WbBtn>
      </WbActions>
    </WbCard>
  );
}

var ICON_CLOCK = (
  <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ flexShrink: 0 }}>
    <circle cx="8" cy="8" r="6.5" /><path d="M8 5v3.2l1.8 1.8" />
  </svg>
);

var ICON_CHEVRON = (
  <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <path d="M5 7l3 3 3-3" />
  </svg>
);

var ICON_FILE = (
  <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
    <path d="M9 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6L9 2z" /><path d="M9 2v4h4" />
  </svg>
);

// The 执行计划 list — collapsible steps with per-step status + progress.
// Visual: timeline rail (done = green check, running = spinner ring, idle =
// hollow dot) + a status / time / duration row. Click a row to expand detail.
function TaskPlanList({ session, expandedStepId, onToggleStep, onRightTab, controller }) {
  var steps = Array.isArray(session.plan) ? session.plan : [];
  return (
    <section className="workbench-flow wbp">
      <div className="wbp-head">
        <b>执行计划</b>
      </div>
      <div className="wbp-list">
        {steps.map(function (step, index) {
          var expanded = expandedStepId === step.id;
          var doneStep = isDoneStepStatus(step.status);
          var runningStep = isRunningStepStatus(step.status);
          var failedStep = step.status === "failed";
          var state = doneStep ? "done" : runningStep ? "current" : failedStep ? "failed" : "idle";
          var statusLabel = doneStep ? "已完成" : runningStep ? "进行中" : failedStep ? "需处理" : "等待执行";
          var doneStamp = step.completedAt || step.updatedAt || "";
          var time = doneStep && doneStamp ? WorkbenchModel.formatTime(doneStamp) : "";
          var duration = doneStep ? stepDurationText(step) : "";
          var estimate = runningStep && step.estimate ? String(step.estimate) : "";
          var hasFiles = Array.isArray(step.relatedFiles) && step.relatedFiles.length > 0;
          var progressText = step.currentAction || step.description || "";
          var isLast = index === steps.length - 1;
          return (
            <div key={step.id} className={"wbp-step " + state + (expanded ? " expanded" : "")}>
              {/* Timeline rail: node + connector line */}
              <div className="wbp-rail">
                <button
                  type="button"
                  className={"wbp-node " + state}
                  onClick={function () { onToggleStep(step.id); }}
                  aria-label={expanded ? "收起步骤" : "展开步骤"}
                >
                  {doneStep ? ICONS.checkSmall : null}
                </button>
                {!isLast && <span className={"wbp-line" + (doneStep ? " done" : "")} />}
              </div>

              {/* Row (click to expand) */}
              <div
                className="wbp-row"
                onClick={function () { onToggleStep(step.id); }}
                role="button"
                tabIndex={0}
                onKeyDown={function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggleStep(step.id); } }}
              >
                <div className="wbp-line-main">
                  <div className="wbp-copy">
                    <span className="wbp-idx">{index + 1}.</span>
                    <span className="wbp-title">{step.title}</span>
                  </div>
                  <span className={"wbp-status " + state}>{statusLabel}</span>
                  <time className="wbp-time">{time}</time>
                  <span className="wbp-dur">
                    {duration ? <>{ICON_CLOCK}<span>{duration}</span></> : estimate ? <span className="wbp-estimate">预计 {estimate}</span> : null}
                  </span>
                  <span className={"wbp-caret" + (expanded ? " open" : "")}>{ICON_CHEVRON}</span>
                </div>

                {/* Expanded detail */}
                {expanded && (
                  <div className="wbp-detail" onClick={function (e) { e.stopPropagation(); }}>
                    <div className="wbp-detail-grid">
                      <div className="wbp-detail-card">
                        <div className="wbp-detail-label">步骤进展</div>
                        <p className="wbp-detail-body">{progressText || "等待 Agent 更新这个步骤的进展。"}</p>
                        {Array.isArray(step.progressEvents) && step.progressEvents.length > 0 && (
                          <ul className="wbp-events">
                            {step.progressEvents.slice(-3).map(function (ev, i) {
                              return <li key={i}>{ev.body || ev.text || ev.message || String(ev)}</li>;
                            })}
                          </ul>
                        )}
                      </div>
                      <div className="wbp-detail-card">
                        <div className="wbp-detail-label">
                          {ICON_FILE}<span>相关文件</span>
                          {hasFiles && <span className="wbp-file-count">{step.relatedFiles.length}</span>}
                        </div>
                        {hasFiles ? (
                          <div className="wbp-file-chips">
                            {step.relatedFiles.map(function (file) {
                              return (
                                <button
                                  key={file.path || file.name}
                                  type="button"
                                  className="wbp-file-chip"
                                  onClick={function () { onRightTab("files"); }}
                                >
                                  {(file.path || file.name || "").split("/").pop()}
                                </button>
                              );
                            })}
                          </div>
                        ) : (
                          <p className="wbp-detail-empty">暂无相关文件</p>
                        )}
                      </div>
                    </div>
                    {!doneStep && (
                      <div className="wbp-detail-actions">
                        {runningStep ? (
                          <button type="button" className="wb-btn danger" onClick={function () { controller.interrupt(); }}>停止执行</button>
                        ) : (
                          <button type="button" className="wb-btn primary" disabled={controller.busy} onClick={function () { controller.runStep(step, index); }}>执行此步骤</button>
                        )}
                        <button type="button" className="wb-btn ghost" onClick={function () { onRightTab("logs"); }}>查看日志</button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function composerPlaceholder(status) {
  if (status === "idle" || status === "pending") return wbT("task.placeholder.idle", "Describe this task's goal, boundaries, and acceptance criteria...");
  if (status === "running") return wbT("task.placeholder.running", "The agent is running; input is temporarily disabled...");
  if (status === "planning") return wbT("task.placeholder.planning", "Add to or revise the execution plan...");
  if (status === "waiting_for_approval" || status === "waiting_for_user") return wbT("task.placeholder.waiting", "Revise requirements, or approve execution...");
  if (status === "failed") return wbT("task.placeholder.failed", "Explain how to fix it, or revise the request...");
  return wbT("task.placeholder.default", "Add requirements, request changes, or continue this task...");
}

// Quick-action chips below the composer; the set changes with status.
// `guard:false` chips stay enabled while the controller is busy (read-only).
function composerChips(status, controller, onRightTab) {
  if (status === "idle" || status === "pending") {
    return [];
  }
  if (status === "planning") {
    return [
      { label: wbT("task.action.approveExecution", "Approve execution"), onClick: function () { controller.approvePlan(); } },
      { label: wbT("task.action.editPlan", "Edit plan"), onClick: focusComposer },
      { label: wbT("task.action.regenerate", "Regenerate"), onClick: function () { controller.regeneratePlan(); } },
    ];
  }
  if (status === "waiting_for_approval" || status === "waiting_for_user" || status === "blocked") {
    return [
      { label: wbT("task.action.approveExecution", "Approve execution"), onClick: function () { controller.execute(); } },
      { label: wbT("task.action.viewDetails", "View details"), guard: false, onClick: function () { onRightTab && onRightTab("context"); } },
      { label: wbT("task.action.reject", "Reject"), onClick: function () { controller.reject(); } },
    ];
  }
  if (status === "running") {
    return [
      { label: wbT("task.action.stopExecution", "Stop execution"), guard: false, onClick: function () { controller.interrupt(); } },
      { label: wbT("task.action.viewLogs", "View logs"), guard: false, onClick: function () { onRightTab && onRightTab("logs"); } },
      { label: wbT("task.action.viewChanges", "View changes"), guard: false, onClick: function () { onRightTab && onRightTab("files"); } },
    ];
  }
  if (status === "paused") {
    return [
      { label: wbT("task.action.resumeTask", "Resume task"), onClick: function () { controller.resume(); } },
      { label: wbT("task.action.reviseRequest", "Revise request"), onClick: focusComposer },
      { label: wbT("task.action.cancelTask", "Cancel task"), onClick: function () { controller.cancel(); } },
    ];
  }
  if (status === "failed") {
    return [
      { label: wbT("task.action.retry", "Retry"), onClick: function () { controller.retry(); } },
      { label: wbT("task.action.reviseRequest", "Revise request"), onClick: focusComposer },
      { label: wbT("task.action.skipStep", "Skip this step"), onClick: function () { controller.skipStep(); } },
    ];
  }
  if (status === "review" || status === "done") {
    return [
      { label: wbT("task.action.markComplete", "Mark complete"), onClick: function () { controller.markComplete(); } },
      { label: wbT("task.action.continueEditing", "Continue editing"), onClick: focusComposer },
      { label: wbT("task.action.createFollowUp", "Create follow-up task"), onClick: function () { controller.createFollowUp(); } },
    ];
  }
  if (status === "completed") {
    return [
      { label: wbT("task.action.createFollowUp", "Create follow-up task"), onClick: function () { controller.createFollowUp(); } },
      { label: wbT("task.action.reopen", "Reopen"), onClick: function () { controller.reopen(); } },
    ];
  }
  if (status === "cancelled") {
    return [{ label: wbT("task.action.reopen", "Reopen"), onClick: function () { controller.reopen(); } }];
  }
  return [];
}

// Composer is always bound to the current task. Behaviour + quick-chips depend
// on the task status. Action row: attachments / slash commands / permission
// mode / send · stop — mirroring the legacy chat composer's capabilities.
function TaskComposer({ session, controller, onRightTab, attachments, onAttachmentsChange, mode, onModeChange, command, onCommandChange }) {
  var model = window.WorkbenchModel;
  var [draft, setDraft] = useWorkbenchState("");
  var [scopePrompt, setScopePrompt] = useWorkbenchState(null);
  var [slashOpen, setSlashOpen] = useWorkbenchState(false);
  var [modeOpen, setModeOpen] = useWorkbenchState(false);
  var [uploading, setUploading] = useWorkbenchState(false);
  var taRef = useWorkbenchRef(null);
  var fileRef = useWorkbenchRef(null);
  var status = String(session.status || "idle");
  var running = status === "running";
  attachments = attachments || [];

  useWorkbenchEffect(function () {
    function onFocus() { if (taRef.current) taRef.current.focus(); }
    window.addEventListener("wb-focus-composer", onFocus);
    return function () { window.removeEventListener("wb-focus-composer", onFocus); };
  }, []);

  // Reset transient composer state when switching tasks.
  useWorkbenchEffect(function () { setScopePrompt(null); setSlashOpen(false); setModeOpen(false); }, [session.id]);

  function syncHeight() {
    var ta = taRef.current;
    if (ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; }
  }
  function resetDraft() {
    setDraft("");
    if (taRef.current) taRef.current.style.height = "";
  }

  function dispatch(text) {
    resetDraft();
    if (status === "idle" || status === "pending") {
      controller.start(text); // typed text becomes the goal, then the plan
    } else if (!running) {
      controller.modifyPlan(text); // refine → back to planning from any structured state
    }
  }

  function submit() {
    if (running) { controller.interrupt(); return; }
    var text = draft.trim();
    if ((!text && attachments.length === 0) || controller.busy) return;
    // Rule 2 — keep the agent inside the current task.
    if (status !== "idle" && status !== "pending" && model.looksOutOfScope(text)) {
      setScopePrompt({ text: text });
      return;
    }
    dispatch(text);
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submit(); }
    else if (event.key === "Escape") { setSlashOpen(false); setModeOpen(false); }
  }

  function pickFiles() { if (fileRef.current) fileRef.current.click(); }
  function onFilePick(event) {
    var files = event.target.files;
    if (!files || !files.length) return;
    setUploading(true);
    model.uploadAttachments(files)
      .then(function (uploaded) { onAttachmentsChange(attachments.concat(uploaded)); })
      .catch(function (err) { window.alert(wbT("workbenchChat.uploadFailed", "Upload failed: {error}", { error: err.message || String(err) })); })
      .finally(function () { setUploading(false); if (fileRef.current) fileRef.current.value = ""; });
  }
  function removeAttachment(index) {
    onAttachmentsChange(attachments.filter(function (_a, i) { return i !== index; }));
  }

  // Slash menu = the legacy agent commands, filtered by the text after "/".
  var slashQuery = draft.indexOf("/") === 0 ? draft.slice(1).toLowerCase() : "";
  var translatedCommands = WB_SLASH_COMMANDS.map(function (c) { return wbCommandMeta(c.id); }).filter(Boolean);
  var translatedModes = WB_MODES.map(function (m) { return wbModeMeta(m.id); });
  var slashItems = translatedCommands.filter(function (c) {
    return !slashQuery || c.id.indexOf(slashQuery) !== -1 || c.label.toLowerCase().indexOf(slashQuery) !== -1;
  });
  var showSlash = (slashOpen || (draft.indexOf("/") === 0 && draft.indexOf(" ") === -1)) && slashItems.length > 0 && !running;
  function pickCommand(id) {
    onCommandChange(id);
    setSlashOpen(false);
    if (draft.indexOf("/") === 0) resetDraft();
    if (taRef.current) taRef.current.focus();
  }
  var activeCommand = command ? wbCommandMeta(command) : null;

  var chips = composerChips(status, controller, onRightTab);
  var disabled = controller.busy || running;
  var current = wbModeMeta(mode || "auto");
  var sendDisabled = running ? false : (disabled || (!draft.trim() && attachments.length === 0));

  return (
    <div className="workbench-composer compact">
      {scopePrompt && (
        <div className="wb-scope-prompt">
          <p>{wbT("task.scopePrompt", "This is outside the current task. Create it as a new follow-up task?")}</p>
          <div className="wb-card-actions">
            <button type="button" className="wb-btn primary" onClick={function () { controller.createFollowUp(scopePrompt.text.slice(0, 40)); setScopePrompt(null); resetDraft(); }}>{wbT("task.createNewTask", "Create new task")}</button>
            <button type="button" className="wb-btn ghost" onClick={function () { var t = scopePrompt.text; setScopePrompt(null); dispatch(t); }}>{wbT("task.mergeCurrent", "Merge into current task")}</button>
            <button type="button" className="wb-btn ghost" onClick={function () { setScopePrompt(null); }}>{wbT("common.cancel", "Cancel")}</button>
          </div>
        </div>
      )}
      {chips.length > 0 && (
        <div className="wb-composer-chips">
          {chips.map(function (c, i) {
            return <button key={i} type="button" className="wb-chip" disabled={controller.busy && c.guard !== false} onClick={c.onClick}>{c.label}</button>;
          })}
        </div>
      )}
      {activeCommand && (
        <div className="wb-command-chip-row">
          <span className="wb-command-chip">
            <span className="wb-command-chip-ico">{activeCommand.icon}</span>
            {activeCommand.label}
            <button type="button" className="wb-command-chip-x" onClick={function () { onCommandChange(""); }} aria-label={wbT("workbenchChat.removeCommand", "Remove command")}>{ICONS.x}</button>
          </span>
        </div>
      )}
      <div className="workbench-composer-box">
        {attachments.length > 0 && (
          <div className="wb-attach-row">
            {attachments.map(function (file, i) {
              var isImg = file.kind === "image" || String(file.content_type || "").indexOf("image") === 0;
              return (
                <div className={"wb-attach-card" + (isImg ? " image" : "")} key={file.id || i}>
                  {isImg && file.url
                    ? <img src={file.url} alt={file.name || "image"} />
                    : <span className="wb-attach-name" title={file.name}>{file.name || "file"}</span>}
                  <button type="button" className="wb-attach-x" onClick={function () { removeAttachment(i); }} aria-label={wbT("workbenchChat.removeAttachment", "Remove attachment")}>{ICONS.x}</button>
                </div>
              );
            })}
          </div>
        )}
        <textarea
          ref={taRef}
          value={draft}
          onChange={function (event) { setDraft(event.target.value); syncHeight(); }}
          onKeyDown={onKeyDown}
          placeholder={activeCommand ? ("（" + activeCommand.label + "）" + composerPlaceholder(status)) : composerPlaceholder(status)}
          rows={2}
          disabled={disabled}
        />
        <div className="workbench-composer-actions">
          <input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={onFilePick} />
          <button type="button" className="wb-composer-icon" title={uploading ? wbT("workbenchChat.uploading", "Uploading...") : wbT("workbenchChat.addAttachment", "Add attachment")} disabled={uploading || running} onClick={pickFiles}>
            {uploading ? <span className="wb-spinner" /> : ICONS.attach}
          </button>
          <span className="wb-popover-anchor">
            <button type="button" className={"wb-composer-icon" + (showSlash || command ? " active" : "")} title={wbT("workbenchChat.commands", "Commands")} disabled={running} onClick={function () { setSlashOpen(!slashOpen); setModeOpen(false); }}>{ICONS.slash}</button>
            {showSlash && (
              <div className="wb-popmenu wb-mode-menu wb-slash-menu">
                <div className="wb-menu-head">{wbT("workbenchChat.commands", "Commands")}</div>
                {slashItems.map(function (c) {
                  var on = command === c.id;
                  return (
                    <button key={c.id} type="button" className={"wb-mode-item" + (on ? " active" : "")} onClick={function () { pickCommand(on ? "" : c.id); }}>
                      <span className="wb-mode-item-ico">{c.icon}</span>
                      <span className="wb-mode-item-body">
                        <span className="wb-mode-item-label">{c.label}</span>
                        <span className="wb-mode-item-desc">{c.desc}</span>
                      </span>
                      <span className="wb-mode-item-check">{on ? ICONS.checkSmall : null}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </span>
          <span className="wb-popover-anchor">
            <button type="button" className={"wb-composer-icon mode" + (modeOpen ? " active" : "")} title={wbT("workbenchChat.permissionMode", "Permission mode")} onClick={function () { setModeOpen(!modeOpen); setSlashOpen(false); }}>
              <span className="wb-mode-ico">{current.icon}</span>
              <span className="wb-mode-label">{current.label}</span>
            </button>
            {modeOpen && (
              <div className="wb-popmenu wb-mode-menu">
                <div className="wb-menu-head">{wbT("workbenchChat.permissionMode", "Permission mode")}</div>
                {translatedModes.map(function (m) {
                  var active = (mode || "auto") === m.id;
                  return (
                    <button key={m.id} type="button" className={"wb-mode-item" + (active ? " active" : "")} onClick={function () { onModeChange(m.id); setModeOpen(false); }}>
                      <span className="wb-mode-item-ico">{m.icon}</span>
                      <span className="wb-mode-item-body">
                        <span className="wb-mode-item-label">{m.label}</span>
                        <span className="wb-mode-item-desc">{m.desc}</span>
                      </span>
                      <span className="wb-mode-item-check">{active ? ICONS.checkSmall : null}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </span>
          <span className="wb-composer-spacer" />
          <button
            type="button"
            className={"wb-composer-send" + (running ? " stop" : "")}
            onClick={submit}
            disabled={sendDisabled}
            title={running ? wbT("workbenchChat.stop", "Stop") : wbT("workbenchChat.send", "Send")}
          >
            {running ? ICONS.stop : (controller.busy ? <span className="wb-spinner" /> : ICONS.send)}
            <span className="wb-composer-send-label">{running ? wbT("workbenchChat.stop", "Stop") : wbT("workbenchChat.send", "Send")}</span>
          </button>
        </div>
      </div>
      <ComposerDisclaimer />
    </div>
  );
}

// AI-generated content disclaimer shown under the composer (i18n).
function ComposerDisclaimer() {
  var t = window.useWorkbenchI18n().t;
  return <div className="wb-composer-disclaimer">{t("workbench.composerDisclaimer")}</div>;
}

function RightContextPanel({ project, session, expandedStepId, tab, onTabChange, onRefresh }) {
  var steps = session && Array.isArray(session.plan) ? session.plan : [];
  var activeStep = steps.find(function (step) { return step.id === expandedStepId; }) || null;
  var isInit = !!(session && session.kind === "init");
  var tabs = isInit ? [
    { id: "context", label: "上下文" },
  ] : [
    { id: "context", label: "上下文" },
    { id: "files", label: "文件变更" },
    { id: "logs", label: "运行日志" },
    { id: "acceptance", label: "验收标准" },
    { id: "artifacts", label: "产物" },
  ];
  if (!session) {
    return <aside className="workbench-right-panel"><div className="workbench-right-body"><p className="workbench-muted">请选择一个任务。</p></div></aside>;
  }
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
        {tab === "acceptance" && <AcceptanceTab session={session} onRefresh={onRefresh} />}
        {tab === "artifacts" && <ArtifactsTab session={session} />}
      </div>
    </aside>
  );
}

function ContextTab({ project, session, activeStep }) {
  var constraints = (session && session.constraints) || [];
  var isInit = !!(session && session.kind === "init");
  if (isInit && window.WorkbenchInitProgress) {
    return (
      <div className="workbench-side-stack">
        <SideSection title="初始化进度">
          {React.createElement(window.WorkbenchInitProgress, { session: session })}
        </SideSection>
      </div>
    );
  }
  return (
    <div className="workbench-side-stack">
      <SideSection title="任务概况">
        <div className="wb-kv"><span>状态</span><b>{WorkbenchModel.statusText(session.status)}</b></div>
        {!isInit && <div className="wb-kv"><span>优先级</span><b>{priorityText(session.priority)}</b></div>}
        <p>{session.goal || "暂无任务目标"}</p>
      </SideSection>
      <SideSection title="项目上下文">
        <div className="wb-kv"><span>项目</span><b>{project ? project.name : "—"}</b></div>
        <p className="workbench-muted">{(project && project.workspacePath) || "—"}</p>
        {project && project.context && project.context.summary && !isInit && <p>{project.context.summary}</p>}
      </SideSection>
      <SideSection title={"任务约束 (" + constraints.length + ")"}>
        {constraints.length
          ? constraints.map(function (item, i) { return <div className="workbench-check" key={i}><span className="workbench-status-dot amber"></span>{item}</div>; })
          : <p className="workbench-muted">暂无约束。在任务里用“不要…”“只…”等表达，会被自动识别为约束。</p>}
      </SideSection>
      {isInit && window.WorkbenchInitProgress ? (
        <SideSection title="初始化进度">
          {React.createElement(window.WorkbenchInitProgress, { session: session })}
        </SideSection>
      ) : (
        <SideSection title="依赖任务">
          <p className="workbench-muted">暂无依赖任务。</p>
        </SideSection>
      )}
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
      <SideSection title={"文件变更 (" + files.length + ")"}>
        {files.length ? files.map(function (file, i) {
          return <div className="workbench-file-row" key={file.id || file.path || file.name || i}><span>{file.path || file.name}</span><small>{file.status || file.changeType || file.type || ""}</small></div>;
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
        {events.length ? events.slice().reverse().slice(0, 40).map(function (event, i) {
          return <div className="workbench-log-row" key={event.id || i}><time>{WorkbenchModel.formatTime(event.createdAt)}</time><span>{WorkbenchModel.eventLabel(event.type)}</span><p>{event.body || (event.stepCount != null ? ("步骤数 " + event.stepCount) : "")}</p></div>;
        }) : <p className="workbench-muted">暂无运行日志。</p>}
      </SideSection>
    </div>
  );
}

function AcceptanceTab({ session, onRefresh }) {
  var [busy, setBusy] = useWorkbenchState(false);
  var items = session && Array.isArray(session.acceptanceCriteria) ? session.acceptanceCriteria : [];
  var passed = items.filter(function (a) { return a.status === "passed" || a.status === "done"; }).length;
  function generate() {
    setBusy(true);
    var accept = window.WorkbenchModel.buildAcceptance(session.goal || "", session.constraints || []);
    window.WorkbenchModel.patchSession(session.id, { acceptanceCriteria: accept })
      .then(function (next) { onRefresh && onRefresh(next); })
      .catch(function (err) { window.alert(err.message || String(err)); })
      .finally(function () { setBusy(false); });
  }
  return (
    <div className="workbench-side-stack">
      <SideSection title={"验收标准" + (items.length ? " (" + passed + "/" + items.length + ")" : "")}>
        {items.length ? items.map(function (item) {
          var dot = (item.status === "passed" || item.status === "done") ? "green" : item.status === "failed" ? "red" : "muted";
          return <div className="workbench-check" key={item.id}><span className={"workbench-status-dot " + dot}></span>{item.text}</div>;
        }) : (
          <div className="wb-empty-action">
            <p className="workbench-muted">暂无验收标准。</p>
            <button type="button" className="wb-btn ghost" disabled={busy} onClick={generate}>{busy ? "生成中…" : "让 Agent 生成验收标准"}</button>
          </div>
        )}
      </SideSection>
    </div>
  );
}

function ArtifactsTab({ session }) {
  var artifacts = session && Array.isArray(session.artifacts) ? session.artifacts : [];
  return (
    <div className="workbench-side-stack">
      <SideSection title={"产物 (" + artifacts.length + ")"}>
        {artifacts.length ? artifacts.map(function (artifact, i) {
          return <div className="workbench-artifact-row" key={artifact.id || i}><b>{artifact.name}</b><small>{artifact.type} · {WorkbenchModel.statusText(artifact.status)}</small><p>{artifact.summary || ""}</p></div>;
        }) : <p className="workbench-muted">当前任务尚未生成产物。</p>}
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

function workbenchFullPageConfig(page, setFullPage, store) {
  return { title: page, render: function () { return <div className="workbench-empty">未找到页面。</div>; } };
}

window.WorkbenchApp = WorkbenchApp;
