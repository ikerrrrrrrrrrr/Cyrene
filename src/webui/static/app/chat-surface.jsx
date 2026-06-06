// Modern chat surface. ChatPage owns data flow; this file owns message UI.
var { useState: useModernState, useEffect: useModernEffect, useMemo: useModernMemo } = React;

var CYRENE_CHAT_PHRASES = {
  zh: {
    welcome: [
      "想干嘛？",
      "随便聊聊。",
      "做啥都行。",
      "我们应该做些什么？",
      "有什么创意/点子？",
      "想实现点什么？",
      "有什么需要帮忙？",
      "我是牛马，尽管说！",
      "来点不一样的？",
      "今天搞点什么？",
      "把想法扔过来。",
      "要不先拆个问题？",
      "有活就说。",
      "今天想推进哪件事？",
      "想写点、查点，还是做点？"
    ],
    thinking: [
      "还得想一下",
      "让我想想",
      "先别急",
      "在做了",
      "差不多了",
      "要仔细想想",
      "我瞧瞧什么个事",
      "让我捋一捋",
      "这事得盘一下",
      "先过一遍细节",
      "我再确认下",
      "脑子转一下",
      "马上给你整明白",
      "得把边界看清楚",
      "我查一下细节",
      "先对齐一下信息",
      "再核一遍",
      "我把线索串一下",
      "稍微推一下",
      "看下哪里最稳",
      "我确认下结果",
      "先把上下文过一遍",
      "这块要算清楚",
      "我再想深一点",
      "别急，快好了"
    ],
    preparing: [
      "整理结果中",
      "准备输出",
      "马上说清楚",
      "把内容排一下",
      "收个尾"
    ],
    toolPrefix: "正在使用"
  },
  en: {
    welcome: [
      "What are we doing?",
      "Say anything.",
      "What should we build?",
      "Got an idea?",
      "Need a hand?",
      "Want to make something?",
      "Throw me a problem.",
      "Let's sketch it out.",
      "What should move today?",
      "Research, write, or build?"
    ],
    thinking: [
      "Still thinking",
      "Let me think",
      "Hold on",
      "Working on it",
      "Almost there",
      "Checking the details",
      "Let me inspect this",
      "One more pass",
      "Untangling it",
      "Verifying the edge cases",
      "Checking the context",
      "Following the thread",
      "Reviewing the evidence",
      "Confirming the result",
      "Sorting this out",
      "Tightening the answer",
      "Looking for the stable path",
      "Cross-checking",
      "Thinking it through",
      "Almost ready"
    ],
    preparing: [
      "Preparing the reply",
      "Organizing the result",
      "Wrapping this up",
      "Getting it ready",
      "Almost ready"
    ],
    toolPrefix: "Using"
  }
};

var CYRENE_TOOL_NAME_I18N = {
  zh: {
    AnalyzeAttachment: "分析附件",
    ApproveScript: "启用脚本",
    Bash: "执行命令",
    CheckClaudeCode: "检查 Claude Code",
    CloseShell: "关闭 Shell",
    CodeReview: "代码审查",
    DeepReflect: "深度反思",
    Edit: "编辑文件",
    FindReferences: "查找引用",
    FormatCode: "格式化代码",
    GetFileSymbols: "获取文件符号",
    GitBranch: "管理 Git 分支",
    GitCommit: "提交 Git",
    GitDiff: "查看 Git 差异",
    GitLog: "查看 Git 日志",
    GitStatus: "查看 Git 状态",
    Glob: "查找文件",
    Grep: "搜索文本",
    IndexCodebase: "索引代码库",
    InstallSkill: "安装技能",
    LearnPatterns: "学习模式",
    LearnSkill: "学习技能",
    LintCode: "检查代码",
    ListScripts: "列出脚本",
    ListShells: "列出 Shell",
    ListSkills: "列出技能",
    PromptClaudeCode: "提示 Claude Code",
    Read: "读取文件",
    RecallMemory: "回忆记忆",
    RejectScript: "停用脚本",
    RunScript: "运行脚本",
    SearchKnowledge: "搜索知识库",
    SearchSymbol: "搜索符号",
    SendShell: "发送 Shell 命令",
    StartClaudeCode: "启动 Claude Code",
    StartShell: "启动 Shell",
    UninstallSkill: "卸载技能",
    WebFetch: "获取网页",
    WebSearch: "搜索网页",
    Write: "写入文件",
    ask_user: "询问用户",
    broadcast_agent_message: "广播代理消息",
    browser_click: "点击网页",
    browser_navigate: "打开网页",
    browser_request_takeover: "请求浏览器接管",
    browser_screenshot: "截取网页",
    browser_type: "输入网页内容",
    cancel_task: "取消计划",
    connect_pins: "连接地图标记",
    delete_entity: "删除实体",
    list_entities: "列出实体",
    list_tasks: "列出计划",
    pause_task: "暂停计划",
    pin_location: "标记地图位置",
    query_entities: "查询实体",
    query_round: "查询回合",
    quit: "结束任务",
    resume_task: "恢复计划",
    schedule_task: "创建计划",
    send_agent_message: "发送代理消息",
    send_file: "发送文件",
    send_message: "发送进度消息",
    send_message_to_user: "发送用户消息",
    send_notification: "发送通知",
    send_telegram: "发送 Telegram",
    send_wechat_file: "发送微信文件",
    spawn_subagent: "创建子代理",
    track_entity: "跟踪实体",
    update_entity: "更新实体",
    use_tools: "启用工具"
  },
  en: {
    AnalyzeAttachment: "Analyze attachment",
    ApproveScript: "Approve script",
    Bash: "Run command",
    CheckClaudeCode: "Check Claude Code",
    CloseShell: "Close shell",
    CodeReview: "Review code",
    DeepReflect: "Deep reflect",
    Edit: "Edit file",
    FindReferences: "Find references",
    FormatCode: "Format code",
    GetFileSymbols: "Get file symbols",
    GitBranch: "Manage Git branch",
    GitCommit: "Commit changes",
    GitDiff: "Show Git diff",
    GitLog: "Show Git log",
    GitStatus: "Show Git status",
    Glob: "Find files",
    Grep: "Search text",
    IndexCodebase: "Index codebase",
    InstallSkill: "Install skill",
    LearnPatterns: "Learn patterns",
    LearnSkill: "Learn skill",
    LintCode: "Lint code",
    ListScripts: "List scripts",
    ListShells: "List shells",
    ListSkills: "List skills",
    PromptClaudeCode: "Prompt Claude Code",
    Read: "Read file",
    RecallMemory: "Recall memory",
    RejectScript: "Reject script",
    RunScript: "Run script",
    SearchKnowledge: "Search knowledge",
    SearchSymbol: "Search symbols",
    SendShell: "Send shell command",
    StartClaudeCode: "Start Claude Code",
    StartShell: "Start shell",
    UninstallSkill: "Uninstall skill",
    WebFetch: "Fetch web page",
    WebSearch: "Search web",
    Write: "Write file",
    ask_user: "Ask user",
    broadcast_agent_message: "Broadcast agent message",
    browser_click: "Click browser",
    browser_navigate: "Navigate browser",
    browser_request_takeover: "Request browser takeover",
    browser_screenshot: "Take screenshot",
    browser_type: "Type in browser",
    cancel_task: "Cancel task",
    connect_pins: "Connect map pins",
    delete_entity: "Delete entity",
    list_entities: "List entities",
    list_tasks: "List tasks",
    pause_task: "Pause task",
    pin_location: "Pin map location",
    query_entities: "Query entities",
    query_round: "Query round",
    quit: "Finish task",
    resume_task: "Resume task",
    schedule_task: "Schedule task",
    send_agent_message: "Send agent message",
    send_file: "Send file",
    send_message: "Send progress message",
    send_message_to_user: "Send user message",
    send_notification: "Send notification",
    send_telegram: "Send Telegram message",
    send_wechat_file: "Send WeChat file",
    spawn_subagent: "Spawn subagent",
    track_entity: "Track entity",
    update_entity: "Update entity",
    use_tools: "Enable tools"
  }
};

function modernPhraseSet(kind, lang) {
  var bucket = CYRENE_CHAT_PHRASES[lang === "zh" ? "zh" : "en"] || CYRENE_CHAT_PHRASES.en;
  return bucket[kind] || [];
}

function ModernRotatingText({ items, interval = 2600, className = "" }) {
  var safeItems = Array.isArray(items) && items.length ? items : [""];
  var isSingleItem = safeItems.length <= 1;
  var [index, setIndex] = useModernState(0);
  useModernEffect(function () {
    if (isSingleItem) return;
    var timer = window.setInterval(function () {
      setIndex(function (value) { return value + 1; });
    }, interval);
    return function () { window.clearInterval(timer); };
  }, [safeItems.join("|"), interval, isSingleItem]);
  var text = safeItems[index % safeItems.length] || "";
  return <span key={isSingleItem ? "static" : text} className={"modern-rotating-text " + className + (isSingleItem ? " static" : "")}>{text}</span>;
}

function modernToolLabel(name, lang) {
  var raw = String(name || "").trim();
  if (!raw) return lang === "zh" ? "工具" : "tool";
  var map = CYRENE_TOOL_NAME_I18N[lang === "zh" ? "zh" : "en"] || {};
  return map[raw] || raw.replace(/_/g, " ");
}

function latestToolProgress(progressEntries) {
  var entries = Array.isArray(progressEntries) ? progressEntries : [];
  for (var i = entries.length - 1; i >= 0; i--) {
    if (entries[i] && entries[i].type === "tool_call" && entries[i].tool) return entries[i];
  }
  return null;
}

function modernMessageHasTools(msg) {
  return Boolean(msg && Array.isArray(msg.tools) && msg.tools.length > 0);
}

function modernMessageHasMapTool(msg) {
  var tools = Array.isArray(msg && msg.tools) ? msg.tools : [];
  return tools.some(function (tool) {
    var name = String(tool && tool.name || "").trim();
    return name === "pin_location" || name === "connect_pins";
  });
}

function modernIsFinalAgentReply(msg) {
  return Boolean(
    msg
    && msg.role === "agent"
    && msg.body
    && !msg.streamingReply
    && !msg.intermediateReply
    && !msg.questionPrompt
    && !msg.runtimeTrace
  );
}

function splitUnifiedDiffByFile(diffText) {
  var source = String(diffText || "");
  if (!source.trim()) return [];
  var lines = source.split("\n");
  var files = [];
  var current = null;

  function pushCurrent() {
    if (!current) return;
    current.diff = current.lines.join("\n").replace(/\n+$/, "") + "\n";
    files.push(current);
  }

  lines.forEach(function (line) {
    if (line.indexOf("diff --git ") === 0) {
      pushCurrent();
      var parts = line.split(/\s+/);
      var file = parts[3] || parts[2] || "";
      file = file.replace(/^b\//, "").replace(/^a\//, "");
      current = { file: file || "diff", added: 0, removed: 0, lines: [line], diff: "" };
      return;
    }
    if (!current && (line.indexOf("--- ") === 0 || line.indexOf("+++ ") === 0 || line.indexOf("@@") === 0)) {
      current = { file: "changes", added: 0, removed: 0, lines: [], diff: "" };
    }
    if (!current) return;
    current.lines.push(line);
    if (line.indexOf("+++ ") === 0) {
      var plusFile = line.slice(4).trim().replace(/^b\//, "");
      if (plusFile && plusFile !== "/dev/null") current.file = plusFile;
      return;
    }
    if (line.indexOf("+") === 0 && line.indexOf("+++") !== 0) current.added += 1;
    if (line.indexOf("-") === 0 && line.indexOf("---") !== 0) current.removed += 1;
  });
  pushCurrent();
  return files.filter(function (file) { return file.added > 0 || file.removed > 0 || file.diff.trim(); });
}

function shortDiffFileName(filePath) {
  var raw = String(filePath || "changes");
  var parts = raw.split("/");
  return parts[parts.length - 1] || raw;
}

function ModernModifiedFiles({ diffText, onOpenDiff }) {
  var files = useModernMemo(function () {
    return splitUnifiedDiffByFile(diffText);
  }, [diffText]);
  if (!files.length) return null;
  return (
    <div className="modern-modified-files">
      {files.map(function (file, index) {
        return (
          <button
            type="button"
            className="modern-modified-file"
            key={file.file + ":" + index}
            onClick={function () { onOpenDiff && onOpenDiff(file.diff, file.file); }}
            title={file.file}
          >
            <span className="modern-modified-name">{shortDiffFileName(file.file)}</span>
            <span className="modern-diff-stat add">+{file.added}</span>
            <span className="modern-diff-stat del">-{file.removed}</span>
          </button>
        );
      })}
    </div>
  );
}

function ModernWelcome({ lang }) {
  return (
    <div className="modern-welcome">
      <ModernRotatingText items={modernPhraseSet("welcome", lang)} interval={5000} className="welcome-phrase" />
    </div>
  );
}

function ModernRuntimeStatus({ visible, progressEntries, lang, diffText, onOpenDiff, preparingReply }) {
  var latestTool = latestToolProgress(progressEntries);
  var prefix = (CYRENE_CHAT_PHRASES[lang === "zh" ? "zh" : "en"] || CYRENE_CHAT_PHRASES.en).toolPrefix;
  var statusText = latestTool && !preparingReply
    ? prefix + (lang === "zh" ? "：" : ": ") + modernToolLabel(latestTool.tool, lang)
    : "";
  var hasDiff = Boolean(String(diffText || "").trim());
  if (!visible && !hasDiff) return null;
  return (
    <div className={"modern-status-stack" + (!visible ? " files-only" : "")}>
      {visible && (
        <div className="modern-runtime-status">
          {statusText ? (
            <ModernRotatingText items={[statusText]} interval={2600} className="status-phrase tool-status-phrase" />
          ) : preparingReply ? (
            <ModernRotatingText items={modernPhraseSet("preparing", lang)} interval={2300} className="status-phrase" />
          ) : (
            <ModernRotatingText items={modernPhraseSet("thinking", lang)} interval={2300} className="status-phrase" />
          )}
        </div>
      )}
      <ModernModifiedFiles diffText={diffText} onOpenDiff={onOpenDiff} />
    </div>
  );
}

function ModernConversation({
  scrollRef,
  onScroll,
  renderedMessageEntries,
  renderedMessages,
  pendingQuestion,
  visibleSending,
  hasStreamingReply,
  activeRequestId,
  visibleLiveProgress,
  visibleNotice,
  mutationDiff,
  mutationDiffsByRequest,
  assistantName,
  isLiveSession,
  onRetryMessage,
  onOpenDiff,
  onShowHtml,
  onShowPdf,
  onShowPpt,
  onShowMap,
  onShowCode,
  onShowMarkdown
}) {
  var i18n = useI18n();
  var lang = i18n.lang;
  var entries = Array.isArray(renderedMessageEntries) ? renderedMessageEntries : [];
  var messages = Array.isArray(renderedMessages) ? renderedMessages : [];
  var visibleEntries = [];
  entries.forEach(function (entry, index) {
    if (pendingQuestion && entry.msg && entry.msg.questionPrompt) return;
    var runtime = getChatRuntime();
    var isRetired = entry.msg && entry.msg.clientRequestId && runtime.retiredRequestIds.indexOf(entry.msg.clientRequestId) !== -1;
    if (isRetired) return;
    visibleEntries.push({ entry: entry, originalIndex: index });
  });
  var hasConversation = visibleEntries.some(function (item) {
    var msg = item.entry && item.entry.msg;
    return msg && (msg.body || (Array.isArray(msg.attachments) && msg.attachments.length) || msg.kind === "compacted");
  });
  var showWelcome = !hasConversation && !visibleSending && !pendingQuestion && !visibleNotice;
  var preparingReply = messages.some(function (msg) {
    return msg && msg.streamingReply && !String(msg.body || "").trim();
  });
  var statusRequestId = String(activeRequestId || "");
  var statusVisible = Boolean(visibleSending && !hasStreamingReply);
  var diffMap = mutationDiffsByRequest && typeof mutationDiffsByRequest === "object" ? mutationDiffsByRequest : {};
  var hasAgentByRequest = {};
  visibleEntries.forEach(function (item) {
    var msg = item.entry && item.entry.msg;
    var requestId = String(msg && msg.clientRequestId || "");
    if (requestId && msg && (msg.role === "agent" || msg.role === "system")) hasAgentByRequest[requestId] = true;
  });
  var insertedStatusRequests = {};

  function diffForRequest(requestId) {
    var key = String(requestId || "");
    var stored = key ? diffMap[key] : null;
    if (stored && String(stored.diff || "").trim()) return stored;
    if (key && mutationDiff && String(mutationDiff.signature || "").split(":")[0] === key && String(mutationDiff.diff || "").trim()) {
      return mutationDiff;
    }
    return null;
  }

  function runtimeStatusNode(requestId, key) {
    var requestDiff = diffForRequest(requestId);
    var isActiveStatus = Boolean(statusVisible && (!statusRequestId || requestId === statusRequestId || requestId === "__active__"));
    return (
      <ModernRuntimeStatus
        key={key}
        visible={isActiveStatus}
        progressEntries={visibleLiveProgress}
        lang={lang}
        diffText={requestDiff && requestDiff.diff}
        onOpenDiff={onOpenDiff}
        preparingReply={preparingReply}
      />
    );
  }

  function retryDataFor(originalIndex) {
    var entry = entries[originalIndex];
    if (!entry || !entry.msg) return null;
    if (
      entry.msg.role !== "agent"
      || !entry.msg.body
      || entry.msg.intermediateReply
      || entry.msg.questionPrompt
      || entry.msg.runtimeTrace
    ) return null;
    var replyRequestId = String(entry.msg.clientRequestId || "");
    if (!replyRequestId) return null;
    for (var laterIndex = originalIndex + 1; laterIndex < entries.length; laterIndex++) {
      var later = entries[laterIndex] && entries[laterIndex].msg;
      if (
        later
        && later.role === "agent"
        && String(later.clientRequestId || "") === replyRequestId
        && String(later.body || "").trim()
        && !later.intermediateReply
        && !later.questionPrompt
        && !later.runtimeTrace
      ) return null;
    }
    for (var i = originalIndex - 1; i >= 0; i--) {
      var prev = entries[i] && entries[i].msg;
      if (prev && prev.role === "user" && prev.body && String(prev.clientRequestId || "") === replyRequestId) {
        return {
          text: prev.body,
          attachments: prev.attachments || [],
          roundId: prev.roundId || "",
          requestId: replyRequestId
        };
      }
    }
    return null;
  }

  var mapToolByRequest = {};
  var mapToolByRound = {};
  visibleEntries.forEach(function (item) {
    var msg = item.entry && item.entry.msg;
    if (!modernMessageHasMapTool(msg)) return;
    var requestId = String(msg && msg.clientRequestId || "");
    var roundId = String(msg && msg.roundId || "");
    if (requestId) mapToolByRequest[requestId] = true;
    if (roundId) mapToolByRound[roundId] = true;
  });

  var conversationNodes = [];
  if (!showWelcome) {
    visibleEntries.forEach(function (item) {
      var retryData = retryDataFor(item.originalIndex);
      var msg = item.entry && item.entry.msg;
      var msgRequestId = String(msg && msg.clientRequestId || "");
      var hasRequestDiff = Boolean(diffForRequest(msgRequestId));
      var shouldRenderForRequest = Boolean(msgRequestId && ((statusVisible && msgRequestId === statusRequestId) || hasRequestDiff));
      var isStatusUser = Boolean(shouldRenderForRequest && msg && msg.role === "user");
      var isStatusAgent = Boolean(shouldRenderForRequest && msg && (msg.role === "agent" || msg.role === "system"));
      var isActivePartialAgent = Boolean(
        isStatusAgent
        && visibleSending
        && msgRequestId
        && msgRequestId === statusRequestId
        && String(msg && msg.body || "").trim()
      );
      var isIntermediateAgent = Boolean(isStatusAgent && (
        msg.intermediateReply
        || isActivePartialAgent
        || (modernMessageHasTools(msg) && !retryData)
      ));
      var msgRoundId = String(msg && msg.roundId || "");
      var showMapAction = Boolean(
        retryData
        && msg
        && msg.role === "agent"
        && (modernMessageHasMapTool(msg) || mapToolByRequest[msgRequestId] || mapToolByRound[msgRoundId])
      );
      var messageNode = (
        <ModernMessage
          key={item.entry.renderKey}
          msg={msg}
          assistantName={assistantName}
          archived={!isLiveSession}
          showActions={Boolean(retryData)}
          showMapAction={showMapAction}
          onRetry={retryData && retryData.requestId ? function () { onRetryMessage && onRetryMessage(retryData); } : null}
          onShowHtml={onShowHtml}
          onShowPdf={onShowPdf}
          onShowPpt={onShowPpt}
          onShowMap={onShowMap}
          onShowCode={onShowCode}
          onShowMarkdown={onShowMarkdown}
        />
      );
      if (isIntermediateAgent && !insertedStatusRequests[msgRequestId]) {
        insertedStatusRequests[msgRequestId] = true;
        conversationNodes.push(
          <React.Fragment key={"with-status-after-" + item.entry.renderKey}>
            {messageNode}
            {runtimeStatusNode(msgRequestId, "runtime-status-after-" + item.entry.renderKey)}
          </React.Fragment>
        );
        return;
      }
      if (isStatusAgent && !insertedStatusRequests[msgRequestId]) {
        insertedStatusRequests[msgRequestId] = true;
        conversationNodes.push(
          <React.Fragment key={"with-status-before-" + item.entry.renderKey}>
            {runtimeStatusNode(msgRequestId, "runtime-status-before-" + item.entry.renderKey)}
            {messageNode}
          </React.Fragment>
        );
        return;
      }
      conversationNodes.push(messageNode);
      if (isStatusUser && !hasAgentByRequest[msgRequestId] && !insertedStatusRequests[msgRequestId]) {
        insertedStatusRequests[msgRequestId] = true;
        conversationNodes.push(runtimeStatusNode(msgRequestId, "runtime-status-after-" + item.entry.renderKey));
      }
    });
    if (statusVisible && (!statusRequestId || !insertedStatusRequests[statusRequestId])) {
      conversationNodes.push(runtimeStatusNode(statusRequestId || "__active__", "runtime-status-fallback"));
    }
  }

  return (
    <div className="chat-scroll modern-chat-scroll" ref={scrollRef} onScroll={onScroll}>
      <div className={"modern-chat-stage" + (showWelcome ? " empty" : "")}>
        {conversationNodes}
        {showWelcome && <ModernWelcome lang={lang} />}
        {visibleNotice && <div className="modern-system-notice">{visibleNotice}</div>}
      </div>
    </div>
  );
}

function ModernComposerHint({ visibleSending, pendingQuestion, hasSelectedGuideRound, mentionedAgents, runningSubagents, assistantName }) {
  var t = useI18n().t;
  var guided = Boolean(hasSelectedGuideRound || (Array.isArray(mentionedAgents) && mentionedAgents.length > 0));
  var leftText = visibleSending
    ? (guided ? t("chat.watchingRunGuide") : t("chat.watchingRunNew"))
    : pendingQuestion
    ? t("chat.waitingForAnswer")
    : guided
    ? t("chat.guidanceMode")
    : t("chat.agentPlansActs", { name: assistantName || DATA.assistantName });
  var count = Number(runningSubagents) || 0;
  return (
    <div className="composer-hint modern-composer-hint">
      <span>{leftText}</span>
      <span>
        {visibleSending ? t("chat.running") + " · " : ""}
        {t("chat.activeSubagents", { n: count, pl: count !== 1 ? "s" : "" })}
      </span>
    </div>
  );
}

function ModernMessage({ msg, archived, showActions, showMapAction, onRetry, onShowHtml, onShowPdf, onShowPpt, onShowMap, onShowCode, onShowMarkdown }) {
  var i18n = useI18n();
  var t = i18n.t;
  if (!msg) return null;
  if (msg.kind === "compacted") {
    return <div className="modern-context-divider"><span>{t("chat.compactedContext") || "较早上下文已压缩"}</span></div>;
  }

  var role = msg.role || "system";
  var attachments = Array.isArray(msg && msg.attachments) ? msg.attachments : [];
  var isAgentLike = role === "agent" || role === "system";
  var isFinalAgentReply = Boolean(showActions && modernIsFinalAgentReply(msg));
  if (isAgentLike && !msg.body && attachments.length === 0) return null;

  var renderMarkdownBody = isAgentLike && msg.body && !msg.streamingReply;
  var bodyNode = null;
  if (renderMarkdownBody) {
    var extracted = extractHtmlBlocks(msg.body);
    if (!extracted.hasBlocks) {
      bodyNode = <div className="modern-msg-body markdown" dangerouslySetInnerHTML={{ __html: renderMarkdown(injectAttachmentLinks(msg.body, attachments)) }} />;
    } else {
      bodyNode = (
        <div className="modern-msg-body markdown">
          {extracted.parts.map(function (part, idx) {
            if (part.type === "markdown" && part.content.trim()) {
              return <div key={idx} dangerouslySetInnerHTML={{ __html: renderMarkdown(injectAttachmentLinks(part.content, attachments)) }} />;
            }
            if (part.type === "html" && part.content) {
              return <div key={idx} className="html-block-placeholder"><button className="html-show-btn" onClick={function () { onShowHtml && onShowHtml(part.content); }}>{t("chat.html.showBtn")}</button></div>;
            }
            return null;
          })}
        </div>
      );
    }
  } else if (msg.body || msg.streamingReply) {
    bodyNode = <div className={"modern-msg-body" + (msg.streamingReply ? " streaming-reply" : "")}>{msg.body}</div>;
  } else if (attachments.length > 0) {
    bodyNode = (
      <div className="modern-msg-body attach-caption">
        {attachments.map(function (file, idx) {
          return <span key={file.id || (file.name + "_" + idx)}>{file.name || "file"}</span>;
        })}
      </div>
    );
  }

  return (
    <div className={"modern-message " + role + (archived ? " archived" : "") + (msg.streamingReply ? " streaming" : "")}>
      <div className="modern-message-inner">
        {bodyNode}
        <ModernAttachments
          attachments={attachments}
          onShowHtml={onShowHtml}
          onShowPdf={onShowPdf}
          onShowPpt={onShowPpt}
          onShowMap={onShowMap}
          onShowCode={onShowCode}
          onShowMarkdown={onShowMarkdown}
        />
        {showMapAction && (
          <div className="modern-attachments">
            <button type="button" className="modern-attachment action" onClick={function () { onShowMap && onShowMap(); }}>{t("chat.map.showBtn")}</button>
          </div>
        )}
        {isFinalAgentReply && (
          <div className="modern-msg-actions">
            <button type="button" className="modern-msg-action" onClick={function () { navigator.clipboard.writeText(msg.body); }} title={t("chat.copyAction") || "复制"}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            {onRetry && (
              <button type="button" className="modern-msg-action" onClick={onRetry} title={t("chat.retryAction") || "重试"}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ModernAttachments({ attachments, onShowHtml, onShowPdf, onShowPpt, onShowMap, onShowCode, onShowMarkdown }) {
  var t = useI18n().t;
  if (!Array.isArray(attachments) || attachments.length === 0) return null;
  return (
    <div className="modern-attachments">
      {attachments.map(function (file, index) {
        var contentType = String(file.content_type || "");
        var isImage = contentType.startsWith("image/");
        var isPdf = contentType === "application/pdf";
        var isPpt = contentType === "application/vnd.ms-powerpoint" || contentType === "application/vnd.openxmlformats-officedocument.presentationml.presentation";
        var isDocx = contentType === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" || contentType === "application/msword";
        var isHtml = contentType === "text/html" || contentType === "application/xhtml+xml";
        var isMap = file.kind === "map" || contentType === "application/geo+json" || contentType === "application/vnd.geo+json";
        var ext = String(file.name || "").split(".").pop().toLowerCase();
        var isMarkdown = file.kind === "markdown" || ext === "md" || ext === "markdown";
        var isCode = !isMarkdown && (file.kind === "code" || (_SIDEBAR_CODE_EXTS.has(ext) && !isImage && !isPdf && !isPpt && !isDocx && !isHtml && !isMap));
        var label = String(file.name || "file");
        if (isImage && file.url) {
          return (
            <a className="modern-attachment image" href={file.url} target="_blank" rel="noreferrer" key={file.id || label + "_" + index}>
              <img src={file.url} alt={attachmentAltText(file)} style={attachmentThumbStyle(file, 360, 260)} />
            </a>
          );
        }
        if (isPdf && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowPdf && onShowPdf(file.url, file.name); }}>{t("chat.pdf.showBtn")}</button>;
        }
        if (isPpt && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowPpt && onShowPpt(file.url, file.name); }}>{t("chat.ppt.showBtn")}</button>;
        }
        if (isDocx && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowPpt && onShowPpt(file.url, file.name); }}>{t("chat.docx.showBtn")}</button>;
        }
        if (isHtml && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { fetch(file.url).then(function (r) { return r.text(); }).then(function (html) { onShowHtml && onShowHtml(html); }).catch(function () {}); }}>{t("chat.html.showBtn")}</button>;
        }
        if (isMap) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowMap && onShowMap(); }}>{t("chat.map.showBtn")}</button>;
        }
        if (isMarkdown && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowMarkdown && onShowMarkdown(file.url, file.name); }}>{t("chat.md.showBtn")}</button>;
        }
        if (isCode && file.url) {
          return <button type="button" className="modern-attachment action" key={file.id || label + "_" + index} onClick={function () { onShowCode && onShowCode(file.url, file.name); }}>{t("chat.code.showBtn")}</button>;
        }
        return <a className="modern-attachment file" href={file.url || "#"} download={label} target="_blank" rel="noreferrer" key={file.id || label + "_" + index}>{label}</a>;
      })}
    </div>
  );
}

function ModernQuestionComposer({ pendingQuestion, draft, onDraftChange, onOptionSelect, onSubmit, onKeyDown, answering, optionCount }) {
  var t = useI18n().t;
  var [expanded, setExpanded] = useModernState(false);
  if (!pendingQuestion) return null;
  var options = Array.isArray(pendingQuestion.options) ? pendingQuestion.options : [];
  var questionText = String(pendingQuestion.text || "");
  var canCollapse = questionText.length > 260;
  return (
    <div className="modern-question-box">
      <div className="modern-question-copy">
        <div className={"modern-question-text" + (expanded ? " expanded" : "")}>{questionText}</div>
        {canCollapse && <button type="button" className="modern-question-toggle" onClick={function () { setExpanded(function (value) { return !value; }); }}>{expanded ? t("chat.showLess") : t("chat.showMore")}</button>}
      </div>
      {options.length > 0 && (
        <div className="modern-question-options">
          {options.map(function (option) {
            return <button type="button" key={option.id} disabled={answering} onClick={function () { onOptionSelect && onOptionSelect(option.label); }}>{option.label}</button>;
          })}
        </div>
      )}
      <div className="modern-question-answer">
        <textarea
          value={draft}
          onChange={function (e) { onDraftChange && onDraftChange(e.target.value); }}
          onKeyDown={onKeyDown}
          disabled={answering}
          placeholder={optionCount ? t("chat.typeYourAnswer") : t("chat.customAnswer")}
        />
        <button type="button" disabled={answering || !String(draft || "").trim()} onClick={onSubmit}>{t("chat.answer")}</button>
      </div>
    </div>
  );
}

function ModernChatComposer(props) {
  var t = useI18n().t;
  if (!props.isLiveSession) {
    return (
      <div className="composer modern-composer archived">
        <div className="modern-archive-composer">
          {t("chat.archivedSessionMessage")}
          <button type="button" onClick={props.onReturnLive}>{t("chat.liveSessionLink")}</button>
          {t("chat.toSendMessages")}
        </div>
      </div>
    );
  }

  if (props.pendingQuestion) {
    return (
      <div className="composer modern-composer" ref={props.composerRef}>
        <ModernQuestionComposer
          pendingQuestion={props.pendingQuestion}
          draft={props.questionDraft}
          onDraftChange={props.onQuestionDraftChange}
          onOptionSelect={props.onQuestionOptionSelect}
          onSubmit={props.onQuestionSubmit}
          onKeyDown={props.onQuestionKeyDown}
          answering={props.answeringQuestion}
          optionCount={props.questionOptionCount}
        />
        <ModernComposerHint
          visibleSending={props.visibleSending}
          pendingQuestion={props.pendingQuestion}
          hasSelectedGuideRound={props.hasSelectedGuideRound}
          mentionedAgents={props.mentionedAgents}
          runningSubagents={props.runningSubagents}
          assistantName={props.assistantName}
        />
      </div>
    );
  }

  var command = props.command;
  var commandData = command && props.findCommand ? props.findCommand(command) : null;
  return (
    <div className={"composer modern-composer" + (props.visibleSending ? " watching" : "")} ref={props.composerRef}>
      <div className="modern-composer-box">
        {(props.visibleChips.length > 0 || props.hasSelectedGuideRound || commandData || props.hasAddable) && (
          <div className="modern-composer-chips">
            {props.visibleChips.map(function (c, i) {
              return <span className="chip" key={i}>{c.icon} {props.contextDisplayLabel(c)} <span className="x" onClick={function () { props.removeContext(props.contextKey(c)); }}>x</span></span>;
            })}
            {props.hasSelectedGuideRound && (
              <span className="chip chip-guide">
                {t("chat.guidanceChipPrefix")} {props.currentGuideRoundTitle}
                <span className="x" onClick={function () { props.setSelectedGuideRoundId(""); props.setSelectedGuideRoundTitle(""); props.setContextPickerOpen(false); }}>x</span>
              </span>
            )}
            {commandData && (
              <span className="chip chip-command">
                {commandData.icon} {commandData.label}
                <span className="x" onClick={function () { props.setCommand(""); }}>x</span>
              </span>
            )}
            <span
              className={"chip chip-add-context" + (props.hasAddable ? "" : " disabled")}
              onClick={function () { if (props.hasAddable) props.setContextPickerOpen(!props.contextPickerOpen); }}
            >
              + {t("chat.addContext")}
            </span>
          </div>
        )}

        {props.mentionedAgents.length > 0 && (
          <div className="modern-composer-mentions">
            {props.mentionedAgents.map(function (agentId) {
              var agent = props.session.subagents.find(function (a) { return a.id === agentId; });
              if (!agent) return null;
              return (
                <span className="chip chip-mention" key={"mention-" + agentId}>
                  <span className={"sa-dot " + agent.status} /> @{agent.name}
                  <span className="x" onClick={function () { props.setMentionedAgents(function (prev) { return prev.filter(function (id) { return id !== agentId; }); }); }}>x</span>
                </span>
              );
            })}
          </div>
        )}

        {props.contextPickerOpen && props.hasAddable && (
          <ModernContextPicker
            addableContexts={props.addableContexts}
            contextDisplayLabel={props.contextDisplayLabel}
            addContext={props.addContext}
            workspaceHistory={props.workspaceHistory}
            pickWorkspaceDir={props.pickWorkspaceDir}
            liveRounds={props.liveRounds}
            selectedGuideRoundId={props.selectedGuideRoundId}
            setSelectedGuideRoundId={props.setSelectedGuideRoundId}
            setSelectedGuideRoundTitle={props.setSelectedGuideRoundTitle}
            setContextPickerOpen={props.setContextPickerOpen}
          />
        )}

        <textarea
          ref={props.taRef}
          value={props.draft}
          onChange={props.onDraftChange}
          onKeyDown={props.onKeyDown}
          placeholder={commandData ? commandData.placeholder : t("chat.messagePlaceholder", { name: DATA.assistantName })}
        />

        {props.attachments.length > 0 && (
          <div className="modern-composer-attachments">
            {props.attachments.map(function (file, index) {
              var isImage = String(file.content_type || "").startsWith("image/");
              return (
                <div className={"composer-attachment-card" + (isImage ? " image" : "")} key={file.id || (file.name + "_" + index)}>
                  {isImage && file.url ? (
                    <div className="composer-attachment-thumb">
                      <img src={file.url} alt={attachmentAltText(file)} style={attachmentThumbStyle(file, 112, 88)} />
                    </div>
                  ) : <div className="composer-attachment-file" aria-label={t("chat.uploadedFile")}></div>}
                  <span className="x" onClick={function () { props.removeAttachment(index); }}>x</span>
                </div>
              );
            })}
          </div>
        )}

        <div className="modern-composer-actions">
          <input ref={props.fileInputRef} type="file" multiple style={{ display: "none" }} onChange={props.onAttachmentPick} />
          <button type="button" className="iconbtn" title={props.uploadingAttachments ? t("chat.uploading") : t("chat.attach")} disabled={props.uploadingAttachments} onClick={function () { if (props.fileInputRef.current) props.fileInputRef.current.click(); }}>
            {props.uploadingAttachments ? "..." : "+"}
          </button>
          <span className="modern-popover-anchor">
            <button type="button" className={"iconbtn" + (command || props.slashMenuOpen ? " active" : "")} title={t("chat.slashCommand")} onClick={function () { props.setSlashMenuOpen(!props.slashMenuOpen); }}>/</button>
            {props.slashMenuOpen && props.filteredCommands.length > 0 && (
              <ModernSlashMenu
                filteredCommands={props.filteredCommands}
                slashIndex={props.slashIndex}
                command={props.command}
                setCommand={props.setCommand}
                setSlashMenuOpen={props.setSlashMenuOpen}
                setSlashIndex={props.setSlashIndex}
              />
            )}
          </span>
          <span className="modern-popover-anchor">
            <button type="button" className={"iconbtn" + (props.mentionedAgents.length > 0 ? " active" : "")} title={t("chat.mention")} disabled={props.session.subagents.length === 0} onClick={function () { props.setMentionMenuOpen(!props.mentionMenuOpen); }}>@</button>
            {props.mentionMenuOpen && (
              <ModernMentionMenu
                session={props.session}
                mentionedAgents={props.mentionedAgents}
                setMentionedAgents={props.setMentionedAgents}
              />
            )}
          </span>
          <span className="modern-composer-spacer"></span>
          <span className="modern-composer-model">{props.session.model}</span>
          {props.visibleSending && (
            <button type="button" className="modern-send secondary" disabled={(!props.draft.trim() && props.attachments.length === 0)} onClick={props.openNextDialogue}>
              {(props.hasSelectedGuideRound || props.mentionedAgents.length > 0) ? t("chat.guide") : t("chat.newDialogue")}
            </button>
          )}
          <button
            type="button"
            className={"modern-send" + (props.visibleSending ? " stop" : "")}
            disabled={!props.visibleSending && !props.draft.trim() && props.attachments.length === 0 && props.command !== "deep-reflect"}
            onClick={props.visibleSending ? props.stopActiveRun : props.send}
          >
            {props.visibleSending ? t("chat.stop") : ((props.hasSelectedGuideRound || props.mentionedAgents.length > 0) ? t("chat.guide") : t("chat.send"))}
          </button>
        </div>
      </div>
      <ModernComposerHint
        visibleSending={props.visibleSending}
        pendingQuestion={props.pendingQuestion}
        hasSelectedGuideRound={props.hasSelectedGuideRound}
        mentionedAgents={props.mentionedAgents}
        runningSubagents={props.runningSubagents}
        assistantName={props.assistantName}
      />
    </div>
  );
}

function ModernContextPicker(props) {
  var t = useI18n().t;
  return (
    <div className="context-picker modern-context-picker">
      {props.addableContexts.length > 0 && (
        <div>
          <div className="context-picker-head">{t("chat.context")}</div>
          {props.addableContexts.map(function (ctx) {
            return (
              <button key={ctx.key} type="button" className="context-option" onClick={function () { props.addContext(ctx.key, ""); }}>
                <span>{ctx.icon}</span> {props.contextDisplayLabel(ctx)}
              </button>
            );
          })}
          {props.addableContexts.some(function (c) { return c.hasPicker; }) && (
            <div className="modern-picker-section">
              <div className="context-picker-head">{t("chat.workspaceDirectories")}</div>
              {props.workspaceHistory.map(function (p) {
                return <button key={p} type="button" className="context-option mono" onClick={function () { props.addContext("workspace", p); }}>{p}</button>;
              })}
              <button type="button" className="context-option" onClick={props.pickWorkspaceDir}>{t("chat.chooseDirectory")}</button>
            </div>
          )}
        </div>
      )}
      {props.liveRounds.length > 0 && (
        <div>
          <div className="context-picker-head">{t("chat.runningRounds")}</div>
          {props.liveRounds.map(function (round) {
            var active = props.selectedGuideRoundId === round.id;
            return (
              <button key={round.id} type="button" className={"context-option" + (active ? " active" : "")} onClick={function () {
                props.setSelectedGuideRoundId(round.id);
                props.setSelectedGuideRoundTitle(round.title || round.id);
                props.setContextPickerOpen(false);
              }}>
                <span className={"sa-dot " + round.status}></span>
                <span className="context-option-body">
                  <span className="context-option-title">{round.title}</span>
                  <span className="context-option-meta">{round.elapsed} · {round.runningSubagents}/{round.subagentCount} {t("chat.subagents")}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ModernSlashMenu(props) {
  var t = useI18n().t;
  return (
    <div className="slash-menu modern-menu">
      <div className="slash-menu-head">{t("chat.commands")}</div>
      {props.filteredCommands.map(function (cmd, idx) {
        var active = props.command === cmd.id;
        var highlighted = props.slashIndex === idx;
        return (
          <button
            type="button"
            key={cmd.id}
            className={"slash-option" + (active ? " active" : "") + (highlighted ? " highlighted" : "")}
            onClick={function () {
              props.setCommand(active ? "" : cmd.id);
              props.setSlashMenuOpen(false);
            }}
            onMouseEnter={function () { props.setSlashIndex(idx); }}
          >
            <span className="slash-option-icon">{cmd.icon}</span>
            <span className="slash-option-body">
              <span className="slash-option-label">{cmd.label}</span>
              <span className="slash-option-desc">{cmd.desc}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function ModernMentionMenu(props) {
  var t = useI18n().t;
  return (
    <div className="mention-menu modern-menu">
      <div className="mention-menu-head">{t("chat.mentionMenuHead")}</div>
      {props.session.subagents.length === 0 && <div className="mention-option-empty">{t("chat.noSubagentsAvailable")}</div>}
      {props.session.subagents.map(function (agent) {
        var selected = props.mentionedAgents.indexOf(agent.id) !== -1;
        return (
          <button key={agent.id} type="button" className={"mention-option" + (selected ? " active" : "")} onClick={function () {
            props.setMentionedAgents(function (prev) {
              return prev.indexOf(agent.id) !== -1
                ? prev.filter(function (id) { return id !== agent.id; })
                : prev.concat([agent.id]);
            });
          }}>
            <span className={"sa-dot " + agent.status}></span>
            <span className="mention-option-body">
              <span className="mention-option-name">@{agent.name}</span>
              <span className="mention-option-task">{agent.task || agent.status}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
