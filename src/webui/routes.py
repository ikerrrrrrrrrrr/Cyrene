"""Route handlers for the Cyrene Web UI (SPA backend)."""

import asyncio
import base64
import difflib
import getpass
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image
from fastapi import APIRouter, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from cyrene.cc_bridge import get_cc_preview, get_cc_status
from cyrene.cc_learner import analyze_session, learn_from_session
from cyrene.cc_terminal import CCTerminalSession
from cyrene import debug
from webui.routes_map import register_map_routes
from webui.routes_amap import register_amap_routes
from webui.routes_entities import register_entity_routes
from webui.routes_knowledge import register_knowledge_routes
from webui.routes_workbench_knowledge import register_workbench_knowledge_routes
from webui.routes_workbench_memory import register_workbench_memory_routes, schedule_capture
from webui.routes_workbench_schedule import register_workbench_schedule_routes
from webui.routes_workbench_chat import register_workbench_chat_routes
from webui.workbench_notifications import append_notification, list_notifications, mark_notifications_read
from webui.routes_code import router as code_router
from cyrene.call_llm import _format_httpx_error as format_httpx_error
from cyrene.attachments import (
    EXPORTS_DIR as _EXPORTS_DIR,
    attachment_kind_from_meta,
    build_public_attachment_payload,
    model_supports_multimodal,
    run_vision_chat,
)
from cyrene.config import _strip_wrapping_quotes
from cyrene.agent.state import _conversation_source, _attachment_paths_by_name
from cyrene.agent import (
    _AWAITING_USER_SENTINEL,
    _append_session_message,
    _call_llm,
    _publish_runtime_event,
    _remove_messages_by_request_id,
    _reply_stream_writer,
    answer_pending_question,
    append_system_message,
    clear_session_id,
    get_pending_question,
    get_live_rounds,
    get_session_labels,
    interrupt_active_run,
    queue_round_guidance,
    run_agent,
)
from cyrene.config import (
    ASSISTANT_NAME,
    BASE_DIR,
    DATA_DIR,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_OPENAI_MODEL,
    DB_PATH,
    PATTERNS_DIR,
    SEARXNG_HOST,
    SEARXNG_PORT,
    SOUL_PATH,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.conversations import CONVERSATIONS_DIR, archive_exchange, search_conversations, search_conversations_structured
from cyrene.onboarding import (
    get_onboarding_status,
    reset_onboarding_state,
    save_and_test_llm_setup,
    save_personality_setup,
)
from cyrene.scheduler import reset_lottery
from cyrene.settings_store import get_all as get_web_settings
from cyrene.skills_registry import (
    build_skills as _build_skills,
    install_skill_from_path,
    skill_payload_from_record as _skill_payload_from_record,
    toggle_skill as _toggle_skill,
    uninstall_skill as _uninstall_skill,
)
from cyrene.shells import list_shells as list_live_shells
from cyrene.shells import set_cc_since
from cyrene.short_term import load_entries
from cyrene.soul import get_default_soul_content, read_soul, get_soul_path
from cyrene.version import get_version_label

logger = logging.getLogger(__name__)
_CC_PROJECT_DIR = WORKSPACE_DIR.parent

_bot: Any = None
_db_path: str = ""
_CHAT_ID = -1

_STATIC_DIR = Path(__file__).parent / "static"
_APP_DIR = _STATIC_DIR / "app"
_UPLOADS_DIR = DATA_DIR / "webui_uploads"
_WORKBENCH_STORE = DATA_DIR / "workbench_projects.json"
_SERVER_STARTED_AT = time.time()
_WORKBENCH_LEGACY_DATA_KEY = "default"


def _safe_workbench_data_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return _WORKBENCH_LEGACY_DATA_KEY
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return cleaned or _WORKBENCH_LEGACY_DATA_KEY


def _workbench_default_project_name() -> str:
    if WORKSPACE_DIR.name == "workspace" and WORKSPACE_DIR.parent.name:
        return WORKSPACE_DIR.parent.name
    return WORKSPACE_DIR.name or "Cyrene"


def _workbench_project_data_key(project: dict[str, Any] | None) -> str:
    if not project:
        return _WORKBENCH_LEGACY_DATA_KEY
    return _safe_workbench_data_key(project.get("dataKey") or project.get("id"))


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _live_llm_config() -> tuple[str, str]:
    from cyrene import config as cy_config

    return cy_config.OPENAI_MODEL, cy_config.OPENAI_BASE_URL


def _get_model() -> str:
    from cyrene import config as cy_config
    return cy_config.OPENAI_MODEL


def _get_base_url() -> str:
    from cyrene import config as cy_config
    return cy_config.OPENAI_BASE_URL


def _parse_ctx_limit(ctx_str: str) -> int:
    """Parse human-readable context limit like '128K', '1M', '200K' to int."""
    ctx_str = (ctx_str or "").strip().upper()
    if not ctx_str:
        return 0
    try:
        if ctx_str.endswith("M"):
            return int(float(ctx_str[:-1]) * 1_000_000)
        if ctx_str.endswith("K"):
            return int(float(ctx_str[:-1]) * 1_000)
        return int(ctx_str)
    except (ValueError, TypeError):
        return 0


def _get_current_model_ctx_limit() -> int:
    """Look up the current model's context window limit from settings."""
    from cyrene.config_store import get_models, get_vision_models
    model_name = _get_model()
    ctx_limit = 0

    for model in get_models() or []:
        if model.get("model") == model_name or model.get("name") == model_name:
            ctx_limit = _parse_ctx_limit(model.get("ctx", ""))
            break

    if not ctx_limit:
        for model in get_vision_models() or []:
            if model.get("model") == model_name or model.get("name") == model_name:
                ctx_limit = _parse_ctx_limit(model.get("ctx", ""))
                break

    # Fallback: known model context windows when not explicitly configured
    if not ctx_limit:
        model_lower = model_name.lower()
        if any(x in model_lower for x in ("claude-opus-4", "opus-4")):
            ctx_limit = 200_000
        elif any(x in model_lower for x in ("claude-sonnet-4", "sonnet-4")):
            ctx_limit = 200_000
        elif any(x in model_lower for x in ("claude-haiku-4", "haiku-4")):
            ctx_limit = 200_000
        elif "gpt-4" in model_lower or "gpt-4o" in model_lower:
            ctx_limit = 128_000
        elif "gpt-3.5" in model_lower:
            ctx_limit = 16_000
        elif "deepseek" in model_lower:
            ctx_limit = 128_000
        elif "qwen" in model_lower:
            ctx_limit = 128_000
        elif "gemini" in model_lower:
            ctx_limit = 1_000_000

    return ctx_limit


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _workbench_default_project() -> dict[str, Any]:
    now = _utc_now_iso()
    project_id = _short_id("project")
    workspace_name = _workbench_default_project_name()
    initial_session = _workbench_new_session(project_id, "新任务", "通过对话明确当前任务目标。", now)
    return {
        "projects": [
            {
                "id": project_id,
                "name": workspace_name,
                "dataKey": _WORKBENCH_LEGACY_DATA_KEY,
                "workspacePath": str(WORKSPACE_DIR),
                "status": "active",
                "model": _get_model(),
                "accountTier": "Pro",
                "context": {
                    "summary": f"Workspace at {WORKSPACE_DIR}",
                    "stack": [],
                    "decisions": [],
                    "knowledgeDocumentIds": [],
                },
                "createdAt": now,
                "updatedAt": now,
                "sessions": [initial_session],
                "sharedArtifacts": [],
            }
        ],
        "activeProjectId": project_id,
        "activeSessionId": initial_session["id"],
    }


def _workbench_new_session(
    project_id: str,
    title: str,
    goal: str = "",
    now: str | None = None,
    *,
    kind: str = "task",
    status: str = "idle",
) -> dict[str, Any]:
    now = now or _utc_now_iso()
    session_id = _short_id("session")
    return {
        "id": session_id,
        "projectId": project_id,
        "kind": kind,
        "title": str(title or "新任务").strip()[:80] or "新任务",
        "goal": str(goal or "").strip(),
        "constraints": [],
        "status": status,
        "priority": "medium",
        "createdAt": now,
        "updatedAt": now,
        "agentReply": "",
        "plan": [],
        "events": [],
        "runs": [],
        "artifacts": [],
        "acceptanceCriteria": [],
        "summary": None,
    }


# ---- Project initialization (the "初始化项目" onboarding session) ------------

# The default onboarding form. Also doubles as the schema the LLM is asked to
# mirror, and as the fallback whenever agent generation is unavailable.
def _workbench_default_init_form(project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic onboarding form for empty-workspace projects.

    The section structure and questions are chosen by template so users get
    scoping questions that fit their project type. ``project`` is optional so
    callers that don't have it yet get the generic blank form.
    """
    template = str(project.get("template") or "blank").strip() if project else "blank"
    greeting = (
        "你好！我是你的项目初始化助理。"
        "我先从几个关键问题开始，以便更好地理解你的需求。"
    )

    FORMS: dict[str, dict] = {
        "blank": {
            "sections": [
                {
                    "id": "basics", "title": "项目概览",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "你想做什么？期望达成什么目标？",
                         "placeholder": "例如：写一份市场分析报告、开发一个博客网站、完成期末作业"},
                        {"id": "description", "type": "textarea", "label": "具体描述一下要做的事情，包括背景和期望的结果",
                         "placeholder": "例如：分析 Q3 的销售数据，输出一份包含图表的 PDF 报告"},
                    ],
                },
                {
                    "id": "scope", "title": "范围与要求",
                    "questions": [
                        {"id": "requirements", "type": "textarea", "label": "有哪些具体要求或内容需要包含？",
                         "placeholder": "例如：数据分析图表、Python 后端、中英文双语输出"},
                        {"id": "out_of_scope", "type": "textarea", "label": "有哪些明确不需要的或排除在外的？",
                         "placeholder": "例如：不需要用户界面、不需要实时更新"},
                    ],
                },
                {
                    "id": "resources", "title": "资源与约束",
                    "questions": [
                        {"id": "resource", "type": "text", "label": "有哪些可用的资源或输入材料？",
                         "placeholder": "例如：项目代码仓库、数据集、参考文档、设计稿"},
                        {"id": "tech", "type": "text", "label": "是否有偏好的工具、技术栈或平台？",
                         "placeholder": "例如：Python、LaTeX、Figma、GitHub Pages"},
                    ],
                },
                {
                    "id": "timeline", "title": "时间计划",
                    "questions": [
                        {"id": "deadline", "type": "text", "label": "期望什么时候完成？有没有关键时间点？",
                         "placeholder": "例如：下周五之前"},
                        {"id": "milestones", "type": "textarea", "label": "有哪些阶段性的交付节点？",
                         "placeholder": "例如：周三前出初稿、周五前完成终版"},
                    ],
                },
            ],
        },
        "product": {
            "sections": [
                {
                    "id": "basics", "title": "产品概览",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "这个产品的核心目标是什么？",
                         "placeholder": "例如：打造一个团队协作工具，提高跨部门任务管理效率"},
                        {"id": "problem", "type": "textarea", "label": "要解决用户的什么痛点？",
                         "placeholder": "例如：任务分散、进度不透明、沟通成本高"},
                        {"id": "users", "type": "text", "label": "目标用户是谁？",
                         "placeholder": "例如：中小团队的 PM 和开发者"},
                    ],
                },
                {
                    "id": "scope", "title": "功能规划",
                    "questions": [
                        {"id": "features", "type": "textarea", "label": "核心功能有哪些？优先级如何？",
                         "placeholder": "例如：任务看板（P0）、进度报表（P1）、消息通知（P2）"},
                        {"id": "mvp", "type": "textarea", "label": "MVP 需要包含哪些功能？",
                         "placeholder": "例如：用户登录、任务创建与指派、看板视图"},
                    ],
                },
                {
                    "id": "resources", "title": "资源与时间",
                    "questions": [
                        {"id": "team", "type": "text", "label": "团队规模和角色是怎样的？",
                         "placeholder": "例如：2 前端 + 2 后端 + 1 设计"},
                        {"id": "tech", "type": "text", "label": "确定的技术栈是什么？",
                         "placeholder": "例如：React、Node.js、PostgreSQL"},
                        {"id": "deadline", "type": "text", "label": "计划什么时候上线？",
                         "placeholder": "例如：8 周内交付 MVP"},
                    ],
                },
                {
                    "id": "quality", "title": "质量与验收",
                    "questions": [
                        {"id": "standard", "type": "textarea", "label": "有哪些质量要求或验收标准？",
                         "placeholder": "例如：页面加载 < 2s、核心流程覆盖测试、WCAG 无障碍"},
                    ],
                },
            ],
        },
        "pm": {
            "sections": [
                {
                    "id": "basics", "title": "项目概览",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "这个项目的目标是什么？",
                         "placeholder": "例如：完成公司官网改版，提升品牌形象和转化率"},
                        {"id": "stakeholders", "type": "text", "label": "关键干系人或合作方有哪些？",
                         "placeholder": "例如：市场部、设计团队、外包开发"},
                    ],
                },
                {
                    "id": "scope", "title": "范围与任务",
                    "questions": [
                        {"id": "deliverables", "type": "textarea", "label": "主要交付物或产出有哪些？",
                         "placeholder": "例如：新版官网页面、CMS 后台、部署文档"},
                        {"id": "deps", "type": "textarea", "label": "有哪些外部依赖或前置条件？",
                         "placeholder": "例如：需要设计团队先输出视觉稿、第三方 API 密钥"},
                    ],
                },
                {
                    "id": "team", "title": "团队与协作",
                    "questions": [
                        {"id": "team", "type": "text", "label": "团队如何组成？协作方式是什么？",
                         "placeholder": "例如：5 人内部团队 + 外部顾问，每日站会 + 周报"},
                        {"id": "tools", "type": "text", "label": "使用的协作工具和平台有哪些？",
                         "placeholder": "例如：Jira、Confluence、Slack、GitHub"},
                    ],
                },
                {
                    "id": "timeline", "title": "时间与风险",
                    "questions": [
                        {"id": "deadline", "type": "text", "label": "关键里程碑和截止日期是什么？",
                         "placeholder": "例如：第 4 周设计定稿、第 8 周上线"},
                        {"id": "risks", "type": "textarea", "label": "已知的风险或阻塞项有哪些？",
                         "placeholder": "例如：设计资源紧张、第三方 API 稳定性未知"},
                    ],
                },
            ],
        },
        "knowledge": {
            "sections": [
                {
                    "id": "direction", "title": "研究方向",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "你当前想研究的具体方向是什么？",
                         "placeholder": "例如：基于大语言模型的分子动力学模拟方法优化"},
                        {"id": "scenario", "type": "textarea", "label": "这个方向主要面向什么任务、场景或应用？",
                         "placeholder": "例如：药物分子筛选中的构象采样效率提升"},
                    ],
                },
                {
                    "id": "problem", "title": "问题定位",
                    "questions": [
                        {"id": "problem", "type": "textarea", "label": "你希望优先解决什么问题？",
                         "placeholder": "例如：现有 MD 模拟方法在长时程构象变化上的采样效率不足"},
                        {"id": "gap", "type": "textarea", "label": "你认为现有方法最明显的不足是什么？",
                         "placeholder": "例如：计算成本高、对稀有事件的采样不足、缺乏可解释性"},
                    ],
                },
                {
                    "id": "conditions", "title": "现有条件",
                    "questions": [
                        {"id": "basis", "type": "textarea", "label": "你目前已有的信息或基础是什么？",
                         "placeholder": "例如：论文、想法、数据、代码、实验结果"},
                        {"id": "resources", "type": "text", "label": "你有哪些可用资源或限制？",
                         "placeholder": "例如：数据、算力、时间、工具、投稿目标"},
                    ],
                },
                {
                    "id": "output", "title": "最终产出",
                    "questions": [
                        {"id": "outcome", "type": "textarea", "label": "你希望最终形成什么成果？",
                         "placeholder": "例如：研究方案、实验结果、论文初稿、代码原型"},
                        {"id": "min_requirement", "type": "textarea", "label": "你对结果有什么最低要求？",
                         "placeholder": "例如：指标提升、可复现实验、能投稿、能开题"},
                    ],
                },
            ],
        },
        "ai": {
            "sections": [
                {
                    "id": "basics", "title": "项目概览",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "你想构建什么？它的核心能力是什么？",
                         "placeholder": "例如：一个代码审查助手，能自动检查 PR 并给出改进建议"},
                        {"id": "users", "type": "text", "label": "谁会用？在什么场景下使用？",
                         "placeholder": "例如：开发团队，在提 PR 时自动触发"},
                    ],
                },
                {
                    "id": "capability", "title": "能力设计",
                    "questions": [
                        {"id": "tools", "type": "textarea", "label": "需要具备哪些能力或工具调用？",
                         "placeholder": "例如：读取代码文件、调用 Lint 工具、查询文档、评论 PR"},
                        {"id": "knowledge", "type": "textarea", "label": "需要参考哪些知识或上下文？",
                         "placeholder": "例如：项目编码规范、API 文档、历史 PR 模式"},
                    ],
                },
                {
                    "id": "resources", "title": "开发资源",
                    "questions": [
                        {"id": "model", "type": "text", "label": "使用什么模型或推理服务？",
                         "placeholder": "例如：Claude API、本地开源模型、Azure OpenAI"},
                        {"id": "tech", "type": "text", "label": "技术栈和运行环境是什么？",
                         "placeholder": "例如：Python、Docker、GitHub Actions"},
                    ],
                },
                {
                    "id": "timeline", "title": "计划与交付",
                    "questions": [
                        {"id": "deadline", "type": "text", "label": "期望什么时候可用？",
                         "placeholder": "例如：2 周出原型、6 周正式上线"},
                        {"id": "milestones", "type": "textarea", "label": "有哪些重要的交付节点？",
                         "placeholder": "例如：第 2 周核心逻辑完成、第 4 周集成测试、第 6 周上线"},
                    ],
                },
            ],
        },
        "import": {
            "sections": [
                {
                    "id": "basics", "title": "导入概览",
                    "questions": [
                        {"id": "goal", "type": "textarea", "label": "导入的项目或内容是什么？",
                         "placeholder": "例如：从 GitHub 导入一个开源博客系统"},
                        {"id": "source", "type": "text", "label": "来源是什么？目前的状态如何？",
                         "placeholder": "例如：GitHub 仓库、本地文件夹、导出文件"},
                    ],
                },
                {
                    "id": "scope", "title": "导入范围",
                    "questions": [
                        {"id": "parts", "type": "textarea", "label": "需要导入全部内容还是部分内容？",
                         "placeholder": "例如：只导入源码和文档，不需要导入历史提交"},
                        {"id": "adapt", "type": "textarea", "label": "导入后需要做哪些适配或改造？",
                         "placeholder": "例如：修改配置为本地环境、更新依赖版本"},
                    ],
                },
                {
                    "id": "resources", "title": "环境与工具",
                    "questions": [
                        {"id": "tech", "type": "text", "label": "项目使用的技术栈是什么？",
                         "placeholder": "例如：React、Express、MongoDB"},
                        {"id": "env", "type": "textarea", "label": "运行需要哪些环境或配置？",
                         "placeholder": "例如：Node 18+、Docker、MySQL 8.0"},
                    ],
                },
                {
                    "id": "timeline", "title": "后续计划",
                    "questions": [
                        {"id": "next", "type": "textarea", "label": "导入完成后的下一步计划是什么？",
                         "placeholder": "例如：修复已知 bug、补充测试、部署上线"},
                        {"id": "deadline", "type": "text", "label": "期望什么时候完成导入和适配？",
                         "placeholder": "例如：本周内完成导入，下周完成适配"},
                    ],
                },
            ],
        },
    }

    form = FORMS.get(template, FORMS["blank"])
    return {
        "generated": False,
        "completed": False,
        "greeting": greeting,
        "sections": form["sections"],
        "answers": {},
    }


def _workbench_new_init_session(
    project_id: str,
    project: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    now = now or _utc_now_iso()
    session = _workbench_new_session(
        project_id,
        "初始化项目",
        "完成项目的基础设置与初始规划。",
        now,
        kind="init",
        status="initializing",
    )
    form = _workbench_default_init_form(project)
    session["init"] = form
    session["agentReply"] = form["greeting"]
    return session


_WORKBENCH_TEMPLATE_LABELS = {
    "blank": "空白项目",
    "product": "产品开发",
    "pm": "项目管理",
    "knowledge": "科学研究",
    "ai": "AI 应用开发",
    "import": "导入项目",
}

_INIT_QUESTION_TYPES = {"text", "textarea", "single", "multi"}


def _workbench_coerce_init_form(raw: Any, base: dict[str, Any]) -> dict[str, Any] | None:
    """Validate/normalize an LLM-produced init form into our schema.

    Returns ``None`` when the payload is unusable so the caller can keep the
    deterministic fallback.
    """
    if not isinstance(raw, dict):
        return None
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return None
    sections: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for s_index, section in enumerate(raw_sections):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        raw_questions = section.get("questions")
        if not title or not isinstance(raw_questions, list):
            continue
        sid = str(section.get("id") or "").strip() or f"section_{s_index + 1}"
        while sid in used_ids:
            sid = f"{sid}_{s_index + 1}"
        used_ids.add(sid)
        questions: list[dict[str, Any]] = []
        used_q_ids: set[str] = set()
        for q_index, question in enumerate(raw_questions):
            if not isinstance(question, dict):
                continue
            label = str(question.get("label") or question.get("question") or "").strip()
            if not label:
                continue
            qtype = str(question.get("type") or "text").strip().lower()
            if qtype not in _INIT_QUESTION_TYPES:
                qtype = "text"
            qid = str(question.get("id") or "").strip() or f"{sid}_q{q_index + 1}"
            while qid in used_q_ids:
                qid = f"{qid}_{q_index + 1}"
            used_q_ids.add(qid)
            item: dict[str, Any] = {"id": qid, "type": qtype, "label": label[:160]}
            placeholder = str(question.get("placeholder") or "").strip()
            if placeholder:
                item["placeholder"] = placeholder[:160]
            if qtype in ("single", "multi"):
                options = [str(o).strip() for o in question.get("options", []) if str(o).strip()]
                if not options:
                    qtype = "text"
                    item["type"] = "text"
                else:
                    item["options"] = options[:8]
            questions.append(item)
        if questions:
            sections.append({"id": sid, "title": title[:60], "questions": questions[:6]})
    if not sections:
        return None
    greeting = str(raw.get("greeting") or "").strip() or base.get("greeting", "")
    return {
        "generated": True,
        "completed": bool(base.get("completed")),
        "greeting": greeting,
        "sections": sections[:6],
        "answers": base.get("answers") if isinstance(base.get("answers"), dict) else {},
    }


_WORKBENCH_EMPTY_WORKSPACE_SKIP_DIRS = frozenset({
    ".git", ".github", ".vscode", ".idea", "__pycache__",
    "node_modules", ".venv", "venv", ".tox", ".egg-info",
    "dist", "build", "target", ".next", ".nuxt", ".cache",
})


def _is_workspace_empty(workspace_root: Path | None) -> bool:
    """Return True when the workspace directory is missing, empty, or only
    contains hidden / build-artifact metadata (no actual source files)."""
    if not workspace_root or not workspace_root.is_dir():
        return True
    try:
        for p in workspace_root.iterdir():
            if p.name.startswith(".") or p.name in _WORKBENCH_EMPTY_WORKSPACE_SKIP_DIRS:
                continue
            if p.name in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
                continue
            return False
    except OSError:
        pass
    return True


# Read-only workspace-exploration tools shared by the init-form agent, the task
# plan generator, and the init task-plan agent. Scoped to a project workspace.
_WORKBENCH_EXPLORE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出工作区指定路径下的文件和目录。返回文件名/目录名列表，不递归。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作区根目录的路径，例如 '.'（根目录）或 'src'。默认 '.'",
                        "default": ".",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区中指定文本文件的内容（最多 4000 字符）。二进制文件会提示不可读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作区根目录的文件路径，例如 'README.md' 或 'src/main.py'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "按通配符模式搜索工作区中的文件路径。支持 ** 递归匹配。例如：'**/*.py' 查找所有 Python 文件，'*.toml' 查找根目录下的 TOML 文件，'src/**/*.tsx' 查找 src 下所有 React 组件。自动跳过隐藏文件。最多返回 50 条结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 搜索模式，相对于工作区根目录",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


def _workbench_parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from an LLM reply, tolerating prose / code fences.

    Models often wrap the JSON in a ```json … ``` fence and/or prefix it with
    prose ("以下是总结：…"), so try several extractions before giving up.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    # Content inside a ```json … ``` (or plain ```) fence.
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence and fence.group(1).strip():
        candidates.append(fence.group(1).strip())
    # Greedy first-brace-to-last-brace span (handles prose around the object).
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _workbench_exec_explore_tool(tc: dict, workspace_root: Path | None) -> str:
    """Execute one workspace-exploration tool call, confined to workspace_root."""
    name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return "Error: invalid tool arguments"

    rel_path = str(args.get("path") or ".").strip()
    if not workspace_root or not workspace_root.is_dir():
        return "Error: workspace directory does not exist or is inaccessible"

    target = (workspace_root / rel_path).resolve()
    if not str(target).startswith(str(workspace_root)):
        return "Error: path is outside the workspace directory"

    try:
        if name == "list_directory":
            entries: list[str] = []
            for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                suffix = "/" if p.is_dir() else ""
                entries.append(f"{p.name}{suffix}")
            if not entries:
                return "(empty directory)"
            return "\n".join(entries)

        elif name == "read_file":
            if not target.is_file():
                return "Error: not a file or does not exist"
            if target.stat().st_size > 256 * 1024:
                return "Error: file too large (>256KB)"
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
                if len(text) > 4000:
                    text = text[:4000] + "\n\n...(truncated)"
                return text
            except (UnicodeDecodeError, LookupError):
                return "Error: binary file (cannot read as text)"

        elif name == "glob":
            pattern = str(args.get("pattern") or "").strip()
            if not pattern:
                return "Error: missing glob pattern"
            it = workspace_root.rglob(pattern.lstrip("/"))
            matches: list[str] = []
            for p in sorted(it):
                rel = str(p.relative_to(workspace_root))
                suffix = "/" if p.is_dir() else ""
                matches.append(f"{rel}{suffix}")
            if len(matches) > 50:
                matches = matches[:50] + [f"... and {len(matches) - 50} more"]
            return "\n".join(matches) if matches else "(no matches)"

    except PermissionError:
        return "Error: permission denied"
    except OSError as e:
        return f"Error: {e}"

    return f"Error: unknown tool '{name}'"


async def _workbench_run_explore_agent(
    workspace_root: Path | None,
    prompt: str,
    *,
    max_turns: int = 8,
    max_tokens: int = 3000,
    timeout: float = 90,
    secondary: bool = False,
) -> dict[str, Any] | None:
    """Run an LLM that may explore the workspace (list_directory/read_file/glob)
    before answering, and return the JSON object it emits (or None on failure).

    Rich workspaces can tempt the model to keep exploring past the turn budget,
    so after ``max_turns`` of tool use we force one final answer WITHOUT tools —
    the model must return the JSON from what it has already gathered.
    """
    messages = [{"role": "user", "content": prompt}]
    for turn in range(max_turns):
        try:
            response = await asyncio.wait_for(
                _call_llm(
                    messages,
                    tools=_WORKBENCH_EXPLORE_TOOLS,
                    max_tokens=max_tokens,
                    secondary=secondary,
                    thinking="disabled",
                ),
                timeout=timeout,
            )
        except Exception:
            logger.exception("Workbench explore-agent failed (turn %d)", turn + 1)
            return None
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            return _workbench_parse_json_object(response.get("content") or "")
        # The assistant tool-call message MUST be appended before the tool
        # results — a 'tool' message has to follow an assistant message carrying
        # its tool_calls, otherwise the next request is malformed and rejected.
        assistant_entry: dict[str, Any] = {"role": "assistant", "content": response.get("content") or "", "tool_calls": tool_calls}
        if response.get("reasoning_content"):
            assistant_entry["reasoning_content"] = response["reasoning_content"]
        messages.append(assistant_entry)
        for tc in tool_calls:
            result = await _workbench_exec_explore_tool(tc, workspace_root)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    # Turn budget exhausted while still exploring — force a final answer with no
    # tools available, so the model has to emit the JSON now.
    messages.append({
        "role": "user",
        "content": "请停止探索。基于你已经了解到的信息，现在只返回最终的 JSON 对象本身，不要再调用任何工具，也不要任何额外说明或 Markdown 代码块标记。",
    })
    try:
        final = await asyncio.wait_for(
            _call_llm(messages, tools=None, max_tokens=max_tokens, secondary=secondary, thinking="disabled"),
            timeout=timeout,
        )
    except Exception:
        logger.exception("Workbench explore-agent final answer failed")
        return None
    return _workbench_parse_json_object(final.get("content") or "")


async def _workbench_generate_init_form(
    project: dict[str, Any],
    lang: str = "",
) -> dict[str, Any] | None:
    """Ask an agent (with file-exploration tools) to produce onboarding
    questions tailored to this project.

    If the workspace is empty (no real source files), returns a lightweight
    deterministic form directly — no point asking the LLM to explore nothing.

    ``lang`` is the user's UI language code (e.g. ``"zh"``, ``"en"``) —
    defaults to ``"zh"`` when empty so the prompt instructs the LLM in the
    right language without hardcoding.

    Returns a normalized init form, or ``None`` when generation is unavailable
    (the caller then keeps the deterministic fallback form).
    """
    name = str(project.get("name") or "新项目").strip()
    description = str(project.get("description") or "").strip()
    template = str(project.get("template") or "").strip()
    template_label = _WORKBENCH_TEMPLATE_LABELS.get(template, template)
    base_form = _workbench_default_init_form(project)

    # Map language code to the human-readable name used in the prompt.
    _LANG_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}
    language = _LANG_NAMES.get(lang, _LANG_NAMES.get("zh"))

    details = [f"项目名称：{name}"]
    if description:
        details.append(f"项目描述：{description}")
    if template_label:
        details.append(f"项目类型：{template_label}")
    details_block = "\n".join(details)

    workspace_path = str(project.get("workspacePath") or "").strip()
    workspace_root = Path(workspace_path).expanduser().resolve() if workspace_path else None

    # ── Empty / no real files → skip the LLM entirely ──────────────────
    if _is_workspace_empty(workspace_root):
        logger.info(
            "Workspace %s is empty — using fixed init form for project %s",
            workspace_path or "(none)", project.get("id"),
        )
        # Return a computed form so the caller doesn't fall back to the
        # full default form which suggests the LLM *might* generate.
        empty_form = _workbench_default_init_form(project)
        empty_form["generated"] = True
        # Override greeting to reflect that there's no existing codebase.
        if language == "English":
            empty_form["greeting"] = (
                "Hi! I'm your project initialization assistant. It looks like this is a "
                "brand-new project with no code in the workspace yet. Let's start with a few "
                "key questions to help you plan the direction and scope."
            )
        else:
            empty_form["greeting"] = (
                "你好！我是你的项目初始化助理。看起来这是一个全新的项目，工作区还没有代码。"
                "我们先从几个关键问题开始，帮你规划好方向和范围。"
            )
        return empty_form

    # ── Has real files → agent explores thoroughly ─────────────────────
    prompt = (
        "你是一个项目初始化助理。用户刚刚创建了一个新项目，工作区已有文件。"
        "你需要深度探索工作区，了解项目的内容、结构和现状，"
        "然后设计一组贴合实际的引导式问题，帮助用户完成项目初始化。\n\n"
        f"项目信息：\n{details_block}\n\n"
        "你可以使用 list_directory、read_file 和 glob 工具深度探索工作区。\n\n"
        "请多花几轮仔细探索，推荐的探索步骤：\n"
        "1. list_directory('.') — 先了解顶层结构\n"
        "2. glob('**/*') 或按文件类型了解内容分布\n"
        "3. 读 README、配置文件或关键入口文件了解项目概况\n"
        "4. 如果文件较多，深入看几个关键目录的内容\n\n"
        "充分了解后再生成 JSON，不要过早下结论。\n\n"
        "最后只返回一个 JSON 对象，不要包含任何额外说明或 Markdown 代码块标记。"
        "JSON 结构如下：\n"
        "{\n"
        '  "greeting": "一句友好的开场白，说明你将协助完成项目初始化",\n'
        '  "sections": [\n'
        "    {\n"
        '      "id": "英文小写下划线短标识",\n'
        f'      "title": "分组标题（{language}，简洁）",\n'
        '      "questions": [\n'
        '        {"id": "英文标识", "type": "text|textarea|single|multi", '
        f'"label": "问题（{language}）", "placeholder": "示例答案（text/textarea 适用）", '
        '"options": ["选项1", "选项2"]}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "要求：\n"
        "- 根据工作区的实际情况，自主决定需要几个分组以及覆盖哪些方向；\n"
        "- 每个分组 2-4 个问题，问题要贴合项目实际情况，避免空泛；\n"
        "- 优先围绕项目已有的内容提问（如需要完善的地方、可以补充的方向、后续步骤等）；\n"
        "- 多数问题用 text 或 textarea；涉及阶段/选择类的用 single 或 multi 并给出 options；\n"
        f"- 全部使用{language}，语气友好专业。最后只返回 JSON。"
    )

    parsed = await _workbench_run_explore_agent(workspace_root, prompt, max_tokens=6000, timeout=120)
    if not parsed:
        return None
    return _workbench_coerce_init_form(parsed, base_form)


def _workbench_init_brief(project: dict[str, Any], form: dict[str, Any]) -> str:
    """Render the collected onboarding answers into a Markdown project brief."""
    answers = form.get("answers") if isinstance(form.get("answers"), dict) else {}
    lines = [f"# {project.get('name') or '项目'} · 初始化总结", ""]
    for section in form.get("sections", []):
        section_lines: list[str] = []
        for question in section.get("questions", []):
            qid = question.get("id")
            value = answers.get(qid)
            if isinstance(value, list):
                value = "、".join(str(v) for v in value if str(v).strip())
            text = str(value or "").strip()
            if text:
                section_lines.append(f"- **{question.get('label')}** {text}")
        if section_lines:
            lines.append(f"## {section.get('title')}")
            lines.extend(section_lines)
            lines.append("")
    return "\n".join(lines).strip()


def _workbench_answer_text(form: dict[str, Any], key: str) -> str:
    answers = form.get("answers") if isinstance(form.get("answers"), dict) else {}
    value = answers.get(key)
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _workbench_fallback_init_task_plan(project: dict[str, Any], form: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a useful deterministic task plan from onboarding answers."""
    goal = _workbench_answer_text(form, "goal") or str(project.get("description") or "").strip()
    requirements = _workbench_answer_text(form, "requirements")
    tech = _workbench_answer_text(form, "tech")
    out_of_scope = _workbench_answer_text(form, "out_of_scope")
    deadline = _workbench_answer_text(form, "deadline")

    constraints: list[str] = []
    if out_of_scope:
        constraints.append(f"范围限制：{out_of_scope}")
    if deadline:
        constraints.append(f"时间约束：{deadline}")
    if tech:
        constraints.append(f"偏好工具或平台：{tech}")

    base_goal = goal or f"推进 {project.get('name') or '项目'}。"
    tasks = [
        {
            "title": "明确目标与范围",
            "goal": f"整理项目目标、背景和边界，形成清晰的范围定义。{(' 重点覆盖：' + requirements) if requirements else ''}".strip(),
            "priority": "high",
            "constraints": constraints[:],
            "acceptanceCriteria": ["目标清晰", "范围已定义", "优先级已确认"],
        },
        {
            "title": "制定执行方案",
            "goal": f"基于项目信息设计具体执行方案和计划。项目总目标：{base_goal}",
            "priority": "high",
            "constraints": constraints[:],
            "acceptanceCriteria": ["执行方案已形成", "步骤可追踪", "依赖已记录"],
        },
        {
            "title": "推进执行与交付",
            "goal": f"按计划推进执行，完成项目目标。项目总目标：{base_goal}",
            "priority": "medium",
            "constraints": constraints[:],
            "acceptanceCriteria": ["项目目标已完成", "结果可验证", "符合预期要求"],
        },
    ]
    return tasks


def _workbench_coerce_init_task_plan(raw: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        return fallback
    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        goal = str(item.get("goal") or item.get("description") or "").strip()
        if not title and goal:
            title = goal[:40]
        if not title:
            continue
        priority = str(item.get("priority") or "medium").strip().lower()
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        constraints = [
            str(value).strip()
            for value in item.get("constraints", [])
            if str(value).strip()
        ] if isinstance(item.get("constraints"), list) else []
        acceptance = item.get("acceptanceCriteria")
        if not isinstance(acceptance, list):
            acceptance = item.get("acceptance")
        acceptance_items = [
            str(value).strip()
            for value in acceptance
            if str(value).strip()
        ] if isinstance(acceptance, list) else []
        tasks.append({
            "id": str(item.get("id") or "").strip() or _short_id("init_task"),
            "title": title[:80],
            "goal": goal[:1200] or title,
            "priority": priority,
            "constraints": constraints[:8],
            "acceptanceCriteria": acceptance_items[:8],
            "order": index + 1,
        })
    return tasks[:8] or fallback


async def _workbench_generate_init_task_plan(
    project: dict[str, Any],
    form: dict[str, Any],
    feedback: str = "",
    current_plan: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Ask the initialization agent to split the project into major task sessions.

    Returns ``(plan, from_llm)`` — ``from_llm`` is False when generation failed
    and the deterministic fallback was used, so callers can tell the user the
    truth instead of pretending the feedback was applied.

    When ``current_plan`` is given (a revision), it is shown to the agent so the
    output adjusts the existing plan rather than regenerating from scratch, and
    it becomes the fallback so an LLM failure preserves the user's edits.
    """
    fallback = _workbench_fallback_init_task_plan(project, form)
    if isinstance(current_plan, list) and current_plan:
        fallback = current_plan
    brief = _workbench_init_brief(project, form)
    feedback = str(feedback or "").strip()
    workspace_path = str(project.get("workspacePath") or "").strip()
    workspace_root = Path(workspace_path).expanduser().resolve() if workspace_path else None
    current_plan_block = ""
    if isinstance(current_plan, list) and current_plan:
        try:
            slim = [
                {
                    "title": str(item.get("title") or ""),
                    "goal": str(item.get("goal") or ""),
                    "priority": str(item.get("priority") or "medium"),
                    "constraints": item.get("constraints") or [],
                    "acceptanceCriteria": item.get("acceptanceCriteria") or [],
                }
                for item in current_plan
                if isinstance(item, dict)
            ]
            current_plan_block = (
                "当前任务计划（请在此基础上按反馈调整，保留未被反馈提到的部分，"
                "不要无故重排或删除）：\n"
                + json.dumps(slim, ensure_ascii=False)
                + "\n\n"
            )
        except Exception:
            current_plan_block = ""
    prompt = (
        "你是项目初始化 Agent。用户已经完成初始化问答。请把项目拆解成若干个"
        "可独立推进的大任务，每个大任务后续会创建为一个 workbench session。\n\n"
        f"项目名称：{project.get('name') or '项目'}\n"
        f"项目类型：{_WORKBENCH_TEMPLATE_LABELS.get(str(project.get('template') or ''), str(project.get('template') or ''))}\n"
        f"初始化总结：\n{brief or '暂无'}\n"
        f"{('用户对计划的修改反馈：' + feedback) if feedback else ''}\n\n"
        f"{current_plan_block}"
        "工作区已有文件，你可以使用 list_directory、read_file、glob 工具先探索项目，"
        "让大任务贴合项目实际（尽量引用真实的文件/目录/模块），不要套用空泛模板。\n\n"
        "充分了解后再返回 JSON，只返回一个 JSON 对象，不要 Markdown。结构：\n"
        "{\n"
        '  "tasks": [\n'
        "    {\n"
        '      "title": "大任务标题，中文，动宾短语",\n'
        '      "goal": "这个 session 要完成的目标、边界和上下文",\n'
        '      "priority": "high|medium|low",\n'
        '      "constraints": ["约束"],\n'
        '      "acceptanceCriteria": ["验收标准"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "要求：生成 3-6 个大任务；每个任务要能对应一个独立 session；避免过细的步骤；"
        "保留初始化回答中的时间、范围、技术约束。"
    )
    parsed = await _workbench_run_explore_agent(
        workspace_root, prompt, max_tokens=4000, timeout=120, secondary=True,
    )
    if not isinstance(parsed, dict):
        logger.warning("Workbench init task-plan generation returned no JSON for project %s", project.get("id"))
        return fallback, False
    return _workbench_coerce_init_task_plan(parsed, fallback), True


def _workbench_create_sessions_from_init_plan(
    project: dict[str, Any],
    plan: list[dict[str, Any]],
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Initialization-agent tool: create task sessions from confirmed major tasks."""
    now = now or _utc_now_iso()
    created: list[dict[str, Any]] = []
    sessions = project.setdefault("sessions", [])
    for item in plan:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        session = _workbench_new_session(
            str(project.get("id") or ""),
            title,
            str(item.get("goal") or title).strip(),
            now,
            kind="task",
            status="idle",
        )
        priority = str(item.get("priority") or "medium").strip().lower()
        if priority in ("high", "medium", "low"):
            session["priority"] = priority
        if isinstance(item.get("constraints"), list):
            session["constraints"] = [str(value).strip() for value in item["constraints"] if str(value).strip()][:8]
        if isinstance(item.get("acceptanceCriteria"), list):
            session["acceptanceCriteria"] = [
                {"id": _short_id("accept"), "text": str(value).strip(), "status": "pending"}
                for value in item["acceptanceCriteria"]
                if str(value).strip()
            ][:8]
        session["events"] = [{
            "id": _short_id("event"),
            "type": "CreatedFromInitPlan",
            "createdAt": now,
            "body": "由初始化计划确认后创建。",
        }]
        created.append(session)
    for session in reversed(created):
        sessions.insert(0, session)
    return created


def _read_workbench_store() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _WORKBENCH_STORE.exists():
        payload = _workbench_default_project()
        _write_workbench_store(payload)
        return payload
    try:
        raw = json.loads(_WORKBENCH_STORE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("projects"), list):
            if not raw["projects"]:
                payload = _workbench_default_project()
                _write_workbench_store(payload)
                return payload
            _workbench_ensure_invariants(raw)
            return raw
    except Exception:
        logger.exception("Failed to read workbench store")
    payload = _workbench_default_project()
    _write_workbench_store(payload)
    return payload


def _write_workbench_store(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _WORKBENCH_STORE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(_WORKBENCH_STORE)


def _workbench_ensure_invariants(payload: dict[str, Any]) -> None:
    changed = False
    projects = payload.setdefault("projects", [])
    now = _utc_now_iso()
    for project in projects:
        project.setdefault("id", _short_id("project"))
        project.setdefault("name", "Workspace")
        project.setdefault("description", "")
        project.setdefault("icon", "spark")
        project.setdefault("color", "")
        project.setdefault("template", "blank")
        project.setdefault("workspacePath", str(WORKSPACE_DIR))
        project.setdefault("status", "active")
        project.setdefault("model", _get_model())
        project.setdefault("accountTier", "Pro")
        project.setdefault("context", {"summary": "", "stack": [], "decisions": [], "knowledgeDocumentIds": []})
        project.setdefault("createdAt", now)
        project.setdefault("updatedAt", now)
        if not project.get("dataKey"):
            is_legacy_default = (
                str(project.get("name") or "").strip().lower() == "workspace"
                and str(project.get("workspacePath") or "") == str(WORKSPACE_DIR)
                and str((project.get("context") or {}).get("summary") or "").startswith("Workspace at ")
            )
            project["dataKey"] = _WORKBENCH_LEGACY_DATA_KEY if is_legacy_default else _safe_workbench_data_key(project.get("id"))
            changed = True
        if _workbench_project_data_key(project) == _WORKBENCH_LEGACY_DATA_KEY:
            default_name = _workbench_default_project_name()
            if str(project.get("name") or "") in ("", "Workspace", "workspace"):
                project["name"] = default_name
                changed = True
            if not str(project.get("workspacePath") or "").strip():
                project["workspacePath"] = str(WORKSPACE_DIR)
                changed = True
        sessions = project.setdefault("sessions", [])
        if not sessions:
            sessions.append(_workbench_new_session(project["id"], "新任务", "通过对话明确当前任务目标。", now))
            changed = True
        for session in sessions:
            session.setdefault("projectId", project["id"])
            session.setdefault("kind", "task")
            session.setdefault("status", "idle")
            session.setdefault("priority", "medium")
            session.setdefault("createdAt", now)
            session.setdefault("updatedAt", now)
            session.setdefault("agentReply", "")
            session.setdefault("plan", [])
            session.setdefault("events", [])
            session.setdefault("runs", [])
            session.setdefault("artifacts", [])
            session.setdefault("acceptanceCriteria", [])
            session.setdefault("summary", None)
    if projects and not payload.get("activeProjectId"):
        payload["activeProjectId"] = projects[0].get("id")
        changed = True
    if projects and not payload.get("activeSessionId"):
        first_sessions = projects[0].get("sessions") or []
        payload["activeSessionId"] = first_sessions[0].get("id") if first_sessions else ""
        changed = True
    if changed:
        _write_workbench_store(payload)


def _workbench_find_project(payload: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    for project in payload.get("projects", []):
        if str(project.get("id") or "") == project_id:
            return project
    return None


def _workbench_find_session(payload: dict[str, Any], session_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for project in payload.get("projects", []):
        for session in project.get("sessions", []):
            if str(session.get("id") or "") == session_id:
                return project, session
    return None, None


def _workbench_extract_constraints(text: str) -> list[str]:
    source = str(text or "")
    constraints: list[str] = []
    patterns = [
        r"不[^\n，。；;,.]{1,32}",
        r"只[^\n，。；;,.]{1,32}",
        r"保留[^\n，。；;,.]{1,32}",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, source):
            item = match.strip()
            if item and item not in constraints:
                constraints.append(item)
    return constraints[:6]


def _workbench_new_plan_step(title: str, description: str, order: int, task_id: str = "") -> dict[str, Any]:
    """A single execution-plan step — always starts pending (no pre-completion)."""
    return {
        "id": _short_id("step"),
        "taskId": task_id,
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
        "status": "pending",
        "order": order,
        "currentAction": "",
        "relatedFiles": [],
        "progressEvents": [],
        "toolCalls": [],
        "artifacts": [],
        "error": None,
    }


def _workbench_plan_from_input(user_input: str, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic FALLBACK plan, used only when LLM plan generation is
    unavailable. Every step starts ``pending`` — nothing is pre-marked done."""
    existing = session.get("plan") if isinstance(session.get("plan"), list) else []
    if existing:
        return existing
    base_steps = [
        "理解目标与约束",
        "收集相关信息和上下文",
        "分析现有内容",
        "制定执行方案",
        "推进执行",
        "验证结果并总结",
    ]
    task_id = session.get("id", "")
    return [
        _workbench_new_plan_step(title, "由兜底计划生成，请按需编辑。", index + 1, task_id)
        for index, title in enumerate(base_steps)
    ]


def _workbench_coerce_plan_steps(raw: Any, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an LLM plan reply (``{"steps": [...]}`` or a bare list) into
    execution-plan steps. All steps start ``pending``."""
    items: list[Any] = []
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
        items = raw["steps"]
    elif isinstance(raw, list):
        items = raw
    task_id = session.get("id", "")
    steps: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or "").strip()
            description = str(item.get("description") or item.get("detail") or "").strip()
        else:
            title = str(item or "").strip()
            description = ""
        if not title:
            continue
        steps.append(_workbench_new_plan_step(title, description, len(steps) + 1, task_id))
        if len(steps) >= 12:
            break
    return steps


def _workbench_plan_title_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _workbench_plan_reset_requested(feedback: str) -> bool:
    return bool(re.search(r"(重新生成|重新规划|重排|重做|从头|替换|清空|不要原计划|不保留原计划)", str(feedback or "")))


def _workbench_existing_plan_block(session: dict[str, Any]) -> str:
    plan = session.get("plan") if isinstance(session.get("plan"), list) else []
    rows: list[str] = []
    for index, step in enumerate(plan[:12], 1):
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or "").strip()
        if not title:
            continue
        status = str(step.get("status") or "pending").strip()
        description = str(step.get("description") or "").strip()
        suffix = f" — {description}" if description else ""
        rows.append(f"{index}. [{status}] {title}{suffix}")
    if not rows:
        return ""
    return "\n当前已有执行计划（除非用户明确要求删除/重排，请保留并在此基础上调整）：\n" + "\n".join(rows)


def _workbench_reconcile_revised_plan(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    feedback: str,
) -> list[dict[str, Any]]:
    if not existing or not feedback or _workbench_plan_reset_requested(feedback):
        return generated
    if not generated:
        return existing

    existing_keys = [_workbench_plan_title_key(step.get("title")) for step in existing if isinstance(step, dict)]
    generated_keys = [_workbench_plan_title_key(step.get("title")) for step in generated if isinstance(step, dict)]
    overlap = sum(1 for key in existing_keys if key and key in generated_keys)
    if existing_keys and overlap >= max(1, len(existing_keys) // 2):
        return generated

    merged = [dict(step) for step in existing if isinstance(step, dict)]
    seen = {_workbench_plan_title_key(step.get("title")) for step in merged}
    next_order = len(merged) + 1
    for step in generated:
        if not isinstance(step, dict):
            continue
        key = _workbench_plan_title_key(step.get("title"))
        if not key or key in seen:
            continue
        next_step = dict(step)
        next_step["order"] = next_order
        merged.append(next_step)
        seen.add(key)
        next_order += 1
    return merged


async def _workbench_generate_plan_steps(
    session: dict[str, Any],
    project: dict[str, Any],
    feedback: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Generate a REAL execution plan for a task session from its goal +
    constraints, exploring the project workspace. Returns ``(steps, from_llm)``;
    ``from_llm`` is False when generation failed and the fallback was used."""
    goal = str(session.get("goal") or session.get("title") or "").strip()
    existing_plan = session.get("plan") if isinstance(session.get("plan"), list) else []
    feedback = str(feedback or "").strip()
    fallback = existing_plan if feedback and existing_plan else _workbench_plan_from_input(goal, {"id": session.get("id", "")})
    if not goal:
        return fallback, False

    constraints = [str(c).strip() for c in (session.get("constraints") or []) if str(c).strip()]
    workspace_path = str(project.get("workspacePath") or "").strip()
    workspace_root = Path(workspace_path).expanduser().resolve() if workspace_path else None

    constraints_block = ("\n约束：\n" + "\n".join(f"- {c}" for c in constraints)) if constraints else ""
    feedback_block = ("\n用户对计划的修改反馈（请据此调整）：" + feedback) if feedback else ""
    existing_plan_block = _workbench_existing_plan_block(session) if feedback else ""
    prompt = (
        "你是任务执行规划 Agent。请把下面这个任务拆解成清晰、有顺序、可逐步执行的步骤。\n"
        "工作区已有文件，你可以使用 list_directory、read_file、glob 工具先探索再规划，"
        "让步骤贴合项目实际（尽量引用真实的文件/目录/模块），不要套用空泛模板。\n\n"
        f"任务目标：{goal}{constraints_block}{existing_plan_block}{feedback_block}\n\n"
        "充分了解后再返回 JSON，只返回一个 JSON 对象，不要 Markdown 代码块标记。结构：\n"
        "{\n"
        '  "steps": [\n'
        '    {"title": "动宾短语的步骤标题（中文，简洁）", "description": "这一步具体做什么、涉及哪些文件或模块"}\n'
        "  ]\n"
        "}\n\n"
        "要求：生成 3-7 个步骤；顺序合理、彼此衔接；每个步骤聚焦一件可执行的事；"
        "尽量引用工作区里的真实文件或模块；全部使用简体中文。"
        "如果是修改已有计划，必须返回完整的修订后计划，保留未被反馈明确要求删除的原步骤，"
        "只调整相关步骤或追加必要的新步骤。"
    )
    parsed = await _workbench_run_explore_agent(workspace_root, prompt, max_tokens=4000, timeout=120)
    if not isinstance(parsed, dict):
        return fallback, False
    steps = _workbench_coerce_plan_steps(parsed, session)
    if not steps:
        return fallback, False
    if feedback:
        steps = _workbench_reconcile_revised_plan(existing_plan, steps, feedback)
    return steps, True


def _workbench_acceptance_from_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    existing = session.get("acceptanceCriteria")
    if isinstance(existing, list) and existing:
        return existing
    constraints = session.get("constraints") if isinstance(session.get("constraints"), list) else []
    items = [str(item) for item in constraints if str(item).strip()]
    if not items:
        items = ["任务目标已明确", "计划已生成", "执行进度可追踪", "最终总结已生成"]
    return [
        {"id": _short_id("accept"), "text": item, "status": "pending"}
        for item in items[:8]
    ]


def _workbench_normalize_attachments(attachments: Any) -> list[dict[str, Any]]:
    """Mirror the /api/chat attachment normalization for workbench runs."""
    items = attachments if isinstance(attachments, list) else []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not str(item.get("path") or "").strip():
            continue
        norm: dict[str, Any] = {
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "file"),
            "path": str(item.get("path") or ""),
            "content_type": str(item.get("content_type") or "application/octet-stream"),
            "size": int(item.get("size") or 0),
            "kind": str(item.get("kind") or "file"),
        }
        if str(item.get("width", "")).strip().isdigit():
            norm["width"] = int(item.get("width"))
        if str(item.get("height", "")).strip().isdigit():
            norm["height"] = int(item.get("height"))
        out.append(norm)
    return out


def _tool_args_preview(args: Any) -> str:
    """One-line compact preview of tool arguments (≤80 chars)."""
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for v in args.values():
        if v is None or v == "":
            continue
        sv = str(v).strip()
        if not sv:
            continue
        sv = sv.replace("\n", " ").replace("\r", "")
        if len(sv) > 50:
            sv = sv[:47] + "…"
        parts.append(sv)
        if len(parts) >= 2:
            break
    result = "  ".join(parts)
    return result[:80]


def _workbench_workspace_root(project: dict[str, Any] | None) -> Path | None:
    workspace_path = str((project or {}).get("workspacePath") or "").strip()
    if not workspace_path:
        return None
    try:
        return Path(workspace_path).expanduser().resolve()
    except OSError:
        return None


def _workbench_display_path(path_value: Any, workspace_root: Path | None = None) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    try:
        path = Path(raw).expanduser()
        if path.is_absolute():
            resolved = path.resolve()
            if workspace_root:
                try:
                    workspace_root = workspace_root.resolve()
                except OSError:
                    pass
                try:
                    return resolved.relative_to(workspace_root).as_posix()
                except ValueError:
                    pass
            return resolved.as_posix()
        return path.as_posix().lstrip("./")
    except Exception:
        return raw


def _workbench_file_change(path_value: Any, status: str, workspace_root: Path | None = None, source: str = "") -> dict[str, Any] | None:
    path = _workbench_display_path(path_value, workspace_root)
    if not path:
        return None
    return {
        "id": _short_id("file"),
        "path": path,
        "status": status,
        "changeType": status,
        "source": source,
    }


def _workbench_file_changes_from_tool_event(event: dict[str, Any], workspace_root: Path | None = None) -> list[dict[str, Any]]:
    tool = str(event.get("tool") or "").strip()
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    result = str(event.get("result") or "")
    changes: list[dict[str, Any]] = []

    if tool == "Write" and isinstance(args, dict):
        change = _workbench_file_change(args.get("path"), "created/updated", workspace_root, tool)
        if change:
            changes.append(change)
    elif tool == "Edit" and isinstance(args, dict):
        change = _workbench_file_change(args.get("path"), "modified", workspace_root, tool)
        if change:
            changes.append(change)

    # Tool output is a useful fallback for older/remote tool names and for
    # cases where the arguments were redacted or shaped differently.
    for match in re.finditer(r"\b(Wrote|Edited)\s+([^\n]+?)(?:\. Replacements:.*)?$", result, flags=re.MULTILINE):
        verb = match.group(1)
        path_text = match.group(2).strip()
        status = "modified" if verb == "Edited" else "created/updated"
        change = _workbench_file_change(path_text, status, workspace_root, tool)
        if change:
            changes.append(change)
    return _workbench_merge_file_changes(changes)


def _workbench_merge_file_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    rank = {"created": 4, "modified": 3, "deleted": 3, "renamed": 3, "created/updated": 2}
    for item in changes:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        key = path
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        old = merged[key]
        new_status = str(item.get("status") or item.get("changeType") or "")
        old_status = str(old.get("status") or old.get("changeType") or "")
        if rank.get(new_status, 0) > rank.get(old_status, 0):
            old["status"] = new_status
            old["changeType"] = new_status
        if item.get("source") and not old.get("source"):
            old["source"] = item.get("source")
    return [merged[key] for key in order]


def _workbench_git_status_snapshot(workspace_root: Path | None) -> dict[str, str]:
    if not workspace_root:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    snapshot: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            snapshot[path] = code
    return snapshot


def _workbench_git_status_change_type(code: str) -> str:
    if "D" in code:
        return "deleted"
    if "R" in code:
        return "renamed"
    if "A" in code or code == "??":
        return "created"
    return "modified"


def _workbench_git_status_delta(before: dict[str, str], after: dict[str, str], workspace_root: Path | None = None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path, code in after.items():
        if before.get(path) == code:
            continue
        change = _workbench_file_change(path, _workbench_git_status_change_type(code), workspace_root, "git")
        if change:
            changes.append(change)
    return changes


def _workbench_resolve_workspace_file(workspace_root: Path | None, path_value: Any) -> Path:
    if not workspace_root:
        raise ValueError("workspace directory is not configured")
    root = workspace_root.resolve()
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError("path is required")
    path = Path(raw).expanduser()
    target = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("path is outside the workspace directory")
    return target


def _workbench_unified_diff(left_text: str, right_text: str, left_label: str, right_label: str) -> str:
    return "".join(difflib.unified_diff(
        left_text.splitlines(keepends=True),
        right_text.splitlines(keepends=True),
        fromfile=left_label,
        tofile=right_label,
    ))


async def _workbench_git_diff_for_path(workspace_root: Path | None, path_value: Any) -> dict[str, Any]:
    target = _workbench_resolve_workspace_file(workspace_root, path_value)
    root = workspace_root.resolve() if workspace_root else None
    rel = target.relative_to(root).as_posix() if root else str(path_value)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--",
            rel,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        raise TimeoutError("git diff timed out")
    except FileNotFoundError:
        raise RuntimeError("git not available")

    if proc.returncode not in (0, 1):
        raise RuntimeError(stderr.decode("utf-8", errors="replace") or "git diff failed")

    diff = stdout.decode("utf-8", errors="replace")
    if not diff.strip() and target.is_file():
        tracked = await asyncio.create_subprocess_exec(
            "git",
            "ls-files",
            "--error-unmatch",
            "--",
            rel,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await tracked.communicate()
        if tracked.returncode != 0:
            try:
                right_text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                right_text = ""
            if right_text:
                diff = _workbench_unified_diff("", right_text, "/dev/null", f"b/{rel}")

    return {"path": rel, "diff": diff, "has_changes": bool(diff.strip())}


def _workbench_apply_step_file_changes(session: dict[str, Any], step_id: str, file_changes: list[dict[str, Any]]) -> None:
    if not step_id or not file_changes:
        return
    plan = session.get("plan") if isinstance(session.get("plan"), list) else []
    for step in plan:
        if not isinstance(step, dict) or str(step.get("id") or "") != step_id:
            continue
        existing = step.get("relatedFiles") if isinstance(step.get("relatedFiles"), list) else []
        step["relatedFiles"] = _workbench_merge_file_changes([*existing, *file_changes])
        break


def _collect_run_tool_events(session_id: str, run_start_ts: str, run_id: str, workspace_root: Path | None = None) -> list[dict[str, Any]]:
    """Return ToolCallEvent dicts for tool calls published during an agent run."""
    return [
        event for event in _collect_run_activity_events(session_id, run_start_ts, run_id, workspace_root)
        if event.get("type") == "ToolCallEvent"
    ]


def _workbench_actor_label(caller: Any, agent_id: Any = "") -> str:
    raw_agent = str(agent_id or "").strip()
    raw = str(caller or "").strip()
    if raw_agent:
        return raw_agent
    if raw.startswith("subagent_"):
        return raw.replace("subagent_", "", 1) or raw
    if raw == "main_agent":
        return "main agent"
    return raw or "agent"


def _workbench_subagent_status_text(status: Any) -> str:
    mapping = {
        "running": "正在执行",
        "resumed": "恢复执行",
        "waiting": "等待其他 subagent",
        "done": "已完成",
        "timeout": "已超时",
    }
    return mapping.get(str(status or "").strip(), str(status or "").strip() or "状态更新")


def _collect_run_activity_events(session_id: str, run_start_ts: str, run_id: str, workspace_root: Path | None = None) -> list[dict[str, Any]]:
    """Return workbench log events for runtime activity published during a run."""
    from cyrene.debug import get_recent_events

    raw = get_recent_events(500)
    out: list[dict[str, Any]] = []
    for e in raw:
        if str(e.get("session_id") or "") != session_id:
            continue
        ts = str(e.get("timestamp") or "")
        if ts and ts < run_start_ts:
            continue
        event_type = str(e.get("type") or "")
        created_at = ts or run_start_ts

        if event_type == "tool_call":
            tool_name = str(e.get("tool") or "").strip()
            if not tool_name:
                continue
            actor = _workbench_actor_label(e.get("caller"))
            file_changes = _workbench_file_changes_from_tool_event(e, workspace_root)
            out.append({
                "id": _short_id("event"),
                "type": "ToolCallEvent",
                "runId": run_id,
                "createdAt": created_at,
                "tool": tool_name,
                "actor": actor,
                "argsPreview": _tool_args_preview(e.get("args")),
                "fileChanges": file_changes,
                "body": f"{actor} 调用工具 {tool_name}",
            })
        elif event_type == "llm_call":
            actor = _workbench_actor_label(e.get("caller"))
            phase = str(e.get("phase") or "").strip()
            duration = e.get("duration_ms")
            duration_text = ""
            try:
                if duration is not None:
                    duration_text = f"，耗时 {float(duration) / 1000:.1f}s"
            except (TypeError, ValueError):
                duration_text = ""
            tools = e.get("tools") if isinstance(e.get("tools"), list) else []
            tool_text = f"，可用工具 {len(tools)} 个" if tools else ""
            phase_text = f"（{phase}）" if phase else ""
            out.append({
                "id": _short_id("event"),
                "type": "LlmCallEvent",
                "runId": run_id,
                "createdAt": created_at,
                "actor": actor,
                "phase": phase,
                "model": str(e.get("model") or ""),
                "body": f"{actor} 完成一轮思考{phase_text}{tool_text}{duration_text}",
            })
        elif event_type == "subagent_update":
            actor = _workbench_actor_label("", e.get("agent_id"))
            status_text = _workbench_subagent_status_text(e.get("status"))
            task = str(e.get("task") or "").strip()
            body = f"{actor} {status_text}" + (f"：{task[:120]}" if task else "")
            out.append({
                "id": _short_id("event"),
                "type": "SubagentStatusEvent",
                "runId": run_id,
                "createdAt": created_at,
                "actor": actor,
                "status": str(e.get("status") or ""),
                "body": body,
            })
    out.sort(key=lambda item: str(item.get("createdAt") or ""))
    return out


async def _workbench_agent_reply(
    user_input: str,
    session: dict[str, Any],
    constraints: list[str],
    attachments: Any = None,
    permission_mode: str = "auto",
    command: str = "",
    project_workspace: str = "",
) -> str:
    """Execute a real agent run for a workbench session.

    Mirrors the /api/chat pipeline for attachments + permission mode + slash
    command so the new workbench composer matches the legacy chat composer.

    ``project_workspace`` confines the agent's file tools + Bash cwd to the
    project's workspacePath; empty → the global WORKSPACE_DIR (legacy behaviour).
    """
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        return str(user_input or "").strip()
    # Confine this run's file operations to the project's workspace.
    workspace_dir = ""
    ws_raw = str(project_workspace or "").strip()
    if ws_raw:
        try:
            ws_path = Path(ws_raw).expanduser()
            ws_path.mkdir(parents=True, exist_ok=True)
            workspace_dir = str(ws_path.resolve())
        except OSError:
            logger.warning("Workbench workspace unavailable, using global: %s", ws_raw)
            workspace_dir = ""
    from cyrene.agent.state import PERMISSION_MODES
    mode = str(permission_mode or "auto").strip().lower()
    if mode not in PERMISSION_MODES:
        mode = "auto"
    normalized = _workbench_normalize_attachments(attachments)
    public_attachments = [build_public_attachment_payload(item) for item in normalized] or None
    message = str(user_input or "")
    if normalized:
        message = (message or "[Attachment upload]") + _attachment_prompt_block(normalized)
        # Auto-allow uploaded files for tool read guards (same as /api/chat).
        att_map: dict[str, str] = {}
        for item in normalized:
            full_path = str(item.get("path") or "").strip()
            if not full_path:
                continue
            uuid_name = Path(full_path).name
            att_map[uuid_name] = full_path
            parts = uuid_name.split("_", 1)
            if len(parts) == 2:
                att_map[parts[1]] = full_path
        _attachment_paths_by_name.set(att_map)
    try:
        return await run_agent(
            user_message=message,
            bot=_bot,
            chat_id=_CHAT_ID,
            db_path=_db_path,
            session_id=session_id,
            permission_mode=mode,
            command=str(command or "").strip(),
            public_user_message=(str(user_input or "") or None),
            public_attachments=public_attachments,
            workspace_dir=workspace_dir,
        )
    except Exception:
        logger.exception("Workbench agent run failed for session %s", session_id)
        return str(user_input or "").strip()


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


async def _reset_app_data() -> dict[str, Any]:
    """Wipe user-modifiable runtime data and restore first-run defaults."""
    from cyrene import agent as cy_agent
    from cyrene.config import write_env_keys
    from cyrene.db import init_db
    from cyrene.inbox import clear_all_inboxes
    from cyrene.settings_store import reset_all as reset_web_settings

    await clear_session_id()

    for task in list(cy_agent._pending_compressors):
        task.cancel()
    cy_agent._pending_compressors.clear()
    await asyncio.sleep(0)

    reset_lottery()
    await clear_all_inboxes()
    reset_web_settings()
    reset_onboarding_state()

    for path in (
        STATE_FILE,
        DATA_DIR / "short_term.json",
        DATA_DIR / "lottery_state.json",
        DATA_DIR / "web_settings.json",
        DATA_DIR / "onboarding_state.json",
        DATA_DIR / ".setup_done",
    ):
        _remove_path(path)

    for path in (
        CONVERSATIONS_DIR,
        _UPLOADS_DIR,
        _EXPORTS_DIR,
        PATTERNS_DIR,
    ):
        _remove_path(path)

    db_path = Path(_db_path or str(DB_PATH))
    _remove_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(str(db_path))

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    soul_path = get_soul_path()
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(get_default_soul_content(), encoding="utf-8")

    write_env_keys({
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": DEFAULT_OPENAI_BASE_URL,
        "OPENAI_MODEL": DEFAULT_OPENAI_MODEL,
        "TELEGRAM_BOT_TOKEN": "",
    })

    return {
        "ok": True,
        "onboarding": get_onboarding_status(),
        "sessions": _build_sessions(),
    }


def _reply_stream_chunks(text: str, target_chars: int = 36) -> list[str]:
    source = str(text or "")
    if not source:
        return []

    chunks: list[str] = []
    for block in re.split(r"(\n\n+)", source):
        if not block:
            continue
        if block.startswith("\n"):
            chunks.append(block)
            continue
        remaining = block
        while remaining:
            if len(remaining) <= target_chars:
                chunks.append(remaining)
                break
            split_at = target_chars
            lower_bound = max(0, target_chars - 14)
            for index in range(target_chars - 1, lower_bound - 1, -1):
                if remaining[index] in "，。！？；：,.!?;: ":
                    split_at = index + 1
                    break
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
    return [chunk for chunk in chunks if chunk]


def _consume_cc_input_buffer(buffer: str, data: str) -> tuple[str, list[str]]:
    current = str(buffer or "")
    submitted: list[str] = []
    if not data:
        return current, submitted

    index = 0
    while index < len(data):
        char = data[index]
        if char == "\x1b":
            break
        if char in ("\r", "\n"):
            text = current.strip()
            if text:
                submitted.append(text)
            current = ""
        elif char in ("\x7f", "\b"):
            current = current[:-1]
        elif char == "\t":
            current += "\t"
        elif ord(char) >= 32:
            current += char
        index += 1
    return current, submitted


async def _publish_cc_learning(text: str, tmux_session: str = "") -> None:
    prompt = str(text or "").strip()
    if not prompt:
        return

    status = get_cc_status(_CC_PROJECT_DIR)
    latest_jsonl = str(status.get("latest_jsonl") or "").strip()
    await debug.publish_event(
        {
            "type": "cc_learning",
            "phase": "started",
            "tmux_session": tmux_session,
            "user_input": prompt[:200],
            "latest_jsonl": latest_jsonl,
        }
    )
    if not latest_jsonl:
        return

    try:
        result = await asyncio.to_thread(learn_from_session, Path(latest_jsonl))
    except Exception:
        logger.exception("Failed learning from Claude Code transcript %s", latest_jsonl)
        await debug.publish_event(
            {
                "type": "cc_learning",
                "phase": "error",
                "tmux_session": tmux_session,
                "user_input": prompt[:200],
                "latest_jsonl": latest_jsonl,
            }
        )
        return

    summary = result.get("summary", {})
    await debug.publish_event(
        {
            "type": "cc_learning",
            "phase": "completed",
            "tmux_session": tmux_session,
            "user_input": prompt[:200],
            "latest_jsonl": latest_jsonl,
            "highlights": summary.get("highlights", []),
            "top_tools": summary.get("top_tools", []),
            "top_tasks": summary.get("top_tasks", []),
        }
    )


async def _stream_reply_payload(response_text: str) -> StreamingResponse:
    async def event_stream():
        yield _ndjson_line({"type": "reply_start"})
        for chunk in _reply_stream_chunks(response_text):
            yield _ndjson_line({"type": "reply_delta", "delta": chunk})
            await asyncio.sleep(0)
        yield _ndjson_line({"type": "reply_done", "response": response_text})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


def _stream_agent_reply(run_coro_factory, user_message: str) -> StreamingResponse:
    async def event_stream():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        saw_reply_events = False
        run_failed = False

        async def publish_reply_event(event: dict[str, Any]) -> None:
            await queue.put(dict(event))

        token = _reply_stream_writer.set(publish_reply_event)
        task = asyncio.create_task(run_coro_factory())
        _reply_stream_writer.reset(token)

        # Broadcast running status so the topbar status light updates in real-time
        await debug.publish_event({"type": "session_update", "status": "running"})

        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if str(event.get("type") or "").startswith("reply_"):
                    saw_reply_events = True
                yield _ndjson_line(event)

            try:
                response = await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A model-call failure (timeout, 5xx, rate limit, network error)
                # must surface to the client. Without this the NDJSON stream just
                # ends silently and the round renders as a bland "done" — the
                # exact symptom of issue #7 ("model failure only replies done").
                run_failed = True
                logger.exception("Streaming chat run failed: %s", format_httpx_error(exc))
                yield _ndjson_line({
                    "type": "error",
                    "error": "model_call_failed",
                    "message": str(exc).strip() or exc.__class__.__name__,
                })
                await debug.publish_event({"type": "session_update", "status": "error"})
                return
            if response == _AWAITING_USER_SENTINEL:
                yield _ndjson_line({"type": "awaiting_user", "awaiting_user": True, "pending_question": get_pending_question()})
                return

            # Stream the response text FIRST — before any I/O (archive_exchange)
            # or SSE events, so the frontend gets reply_delta events without delay
            # and avoids the race where refreshSessions() clears pending messages
            # before the stream completes.
            if not saw_reply_events:
                yield _ndjson_line({"type": "reply_start"})
                for chunk in _reply_stream_chunks(response):
                    yield _ndjson_line({"type": "reply_delta", "delta": chunk})
                yield _ndjson_line({"type": "reply_done", "response": response})

            # Archive the exchange after streaming — file I/O must not delay
            # response delivery to the frontend.
            labels = get_session_labels()
            await archive_exchange(
                user_message,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )

            # Signal done last, so the SSE-triggered refreshSessions() call
            # runs after the NDJSON stream has already delivered reply_done.
            await debug.publish_event({"type": "session_update", "status": "done"})
        finally:
            if not task.done():
                task.cancel()
            # Publish "done" on success/cancellation. On a model-call failure we
            # already published "error" above and must not overwrite it with a
            # misleading "done" (issue #7).
            if not run_failed:
                await debug.publish_event({"type": "session_update", "status": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


def _safe_upload_name(filename: str) -> str:
    raw = Path(str(filename or "upload.bin")).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return sanitized or "upload.bin"


def _retry_safe_guide_round_id(guide_round_id: str, retry: bool) -> str:
    """A retry regenerates a reply; it must not target the old completed round."""
    return "" if retry else str(guide_round_id or "").strip()


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def _attachment_prompt_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = [
        "",
        "[Uploaded attachments]",
        "The user uploaded the following files into the local workspace-accessible runtime data directory.",
        "Before answering anything about these files, you MUST inspect the relevant attachment with AnalyzeAttachment.",
        "Do not answer from the filename, extension, or metadata alone.",
        "After AnalyzeAttachment returns extracted content, use that extracted content to answer the user.",
    ]
    for item in items:
        lines.append(f'- {item["name"]} ({item["content_type"]}): {item["path"]}')
    return "\n".join(lines)


async def _chat_with_uploaded_images(message: str, attachments: list[dict[str, Any]]) -> str:
    prompt = str(message or "").strip() or "Describe the uploaded image in detail and extract any visible text."
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in attachments:
        path = Path(str(item.get("path") or "")).resolve()
        mime = str(item.get("content_type") or mimetypes.guess_type(str(path))[0] or "image/png")
        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}})
    try:
        response = await _call_llm([{"role": "user", "content": content}], tools=None, max_tokens=None)
    except httpx.HTTPError as exc:
        detail = format_httpx_error(exc).lower()
        if any(token in detail for token in ("image", "vision", "multimodal", "unsupported", "invalid content")):
            result = await run_vision_chat(content, content_prompt=prompt)
            return str(result.get("vision_text") or "").strip() or "The vision fallback model returned no usable image analysis."
        raise
    response_text = str((response.get("content") if isinstance(response.get("content"), str) else "") or "").strip()
    if response_text:
        return response_text
    parts: list[str] = []
    if isinstance(response.get("content"), list):
        for item in response.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    merged = "".join(parts).strip()
    return merged or "The model returned no usable image analysis."


async def _persist_direct_image_chat(
    message: str,
    response: str,
    public_attachments: list[dict[str, Any]],
    client_request_id: str,
) -> None:
    round_id = f"round_{int(time.time() * 1000)}"
    user_entry: dict[str, Any] = {
        "role": "user",
        "content": str(message or ""),
        "attachments": [dict(item) for item in public_attachments],
        "round_id": round_id,
    }
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    await _append_session_message(user_entry)
    await append_system_message(
        response,
        message_meta={
            "system_initiated": False,
            "round_id": round_id,
            **({"client_request_id": client_request_id} if client_request_id else {}),
        },
        publish_event={
            "type": "chat_message",
            "round_id": round_id,
            "client_request_id": client_request_id,
        },
    )


def register_routes(app, bot: Any, db_path: str) -> None:
    global _bot, _db_path
    _bot = bot
    _db_path = db_path

    router = APIRouter()
    register_map_routes(router)
    register_amap_routes(router)
    register_entity_routes(router, db_path)
    register_knowledge_routes(router)
    register_workbench_knowledge_routes(router)
    register_workbench_memory_routes(router)
    register_workbench_schedule_routes(router, db_path)
    register_workbench_chat_routes(router, bot, db_path)
    router.include_router(code_router)

    # ---- SPA root ----

    @router.get("/", response_class=HTMLResponse)
    async def spa_root(request: Request):
        ui_mode = getattr(request.app.state, "ui_mode", "workbench")
        if ui_mode == "legacy" and request.query_params.get("shell") != "legacy":
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/?shell=legacy")
        return FileResponse(
            _APP_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # ---- UI bootstrap data ----

    @router.get("/api/ui-data")
    async def api_ui_data(tz: str = ""):
        return await _build_ui_data(tz)

    # ---- Chat API ----

    @router.post("/api/chat/upload")
    async def api_chat_upload(files: list[UploadFile]):
        if not files:
            return JSONResponse({"error": "no files uploaded"}, status_code=400)

        _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        uploaded: list[dict[str, Any]] = []

        for file in files:
            safe_name = _safe_upload_name(file.filename or "")
            target = _UPLOADS_DIR / f"{uuid.uuid4().hex}_{safe_name}"
            file_size = 0
            try:
                with target.open("wb") as f:
                    while chunk := await file.read(65536):
                        f.write(chunk)
                        file_size += len(chunk)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            content_type = str(file.content_type or mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            kind = attachment_kind_from_meta(content_type, target.name)
            width, height = _image_dimensions(target) if kind == "image" else (None, None)
            uploaded.append({
                "id": target.name,
                "name": file.filename or safe_name,
                "path": str(target.resolve()),
                "content_type": content_type,
                "size": file_size,
                "kind": kind,
                "url": f"/api/chat/upload/{target.name}",
                **({"width": width} if isinstance(width, int) else {}),
                **({"height": height} if isinstance(height, int) else {}),
            })

            # Register document in knowledge base
            try:
                from cyrene.config import get_knowledge_db_path
                from cyrene.knowledge import store, ingest

                _kb_db_path = str(get_knowledge_db_path())
                content_hash = store.content_hash_file(target)
                doc = await store.upsert_document_by_path(
                    _kb_db_path,
                    path=str(target.resolve()),
                    source="chat_upload",
                    name=file.filename or safe_name,
                    content_type=content_type,
                    kind=kind,
                    size=file_size,
                    content_hash=content_hash,
                )
                if doc.get("path") and str(Path(doc["path"]).resolve()) != str(target.resolve()):
                    target.unlink(missing_ok=True)
                if doc.get("status") in {"pending", "error"}:
                    asyncio.create_task(ingest.index_document(_kb_db_path, doc["id"]))
            except Exception as e:
                logger.debug(f"Failed to register document in knowledge base: {e}")

        return {"files": uploaded}

    @router.get("/api/chat/upload/{upload_id}")
    async def api_chat_upload_file(upload_id: str):
        safe_upload_id = _safe_upload_name(upload_id)
        target = (_UPLOADS_DIR / safe_upload_id).resolve()
        uploads_root = _UPLOADS_DIR.resolve()
        if target != uploads_root and uploads_root not in target.parents:
            return JSONResponse({"error": "invalid upload path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "upload not found"}, status_code=404)
        return FileResponse(target)

    @router.get("/api/chat/export/{export_id}")
    async def api_chat_export_file(export_id: str):
        safe_export_id = _safe_upload_name(export_id)
        target = (_EXPORTS_DIR / safe_export_id).resolve()
        exports_root = _EXPORTS_DIR.resolve()
        if target != exports_root and exports_root not in target.parents:
            return JSONResponse({"error": "invalid export path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "export not found"}, status_code=404)
        return FileResponse(target)

    @router.post("/api/chat")
    async def api_chat(request: Request):
        _conversation_source.set("webui")
        body = await request.json()
        message = (body.get("message") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        guide_round_id = str(body.get("guide_round_id") or "").strip()
        client_request_id = str(body.get("client_request_id") or "").strip()
        wants_stream = bool(body.get("stream"))
        lang = str(body.get("lang") or "").strip()
        command = str(body.get("command") or "").strip()
        from cyrene.agent.state import PERMISSION_MODES
        permission_mode = str(body.get("mode") or "default").strip().lower()
        if permission_mode not in PERMISSION_MODES:
            permission_mode = "default"
        from cyrene.agent.commands import DEEP_REFLECT_COMMAND_ID, parse_deep_reflect_command
        deep_reflect_parse = parse_deep_reflect_command(message)
        if deep_reflect_parse.get("matched"):
            command = DEEP_REFLECT_COMMAND_ID
        if command == DEEP_REFLECT_COMMAND_ID and not message:
            message = "/deep-reflect"
        mentions = body.get("mentions") if isinstance(body.get("mentions"), list) else []
        retry = bool(body.get("retry"))
        retry_request_id = str(body.get("retry_request_id") or "").strip()
        if retry and retry_request_id:
            await _remove_messages_by_request_id(retry_request_id)
        guide_round_id = _retry_safe_guide_round_id(guide_round_id, retry)
        normalized_attachments = [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "file"),
                "path": str(item.get("path") or ""),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "size": int(item.get("size") or 0),
                "kind": str(item.get("kind") or "file"),
                **({"width": int(item.get("width"))} if str(item.get("width", "")).strip().isdigit() else {}),
                **({"height": int(item.get("height"))} if str(item.get("height", "")).strip().isdigit() else {}),
            }
            for item in attachments
            if str(item.get("path") or "").strip()
        ]
        public_attachments = [build_public_attachment_payload(item) for item in normalized_attachments]
        if not message and not normalized_attachments and command != DEEP_REFLECT_COMMAND_ID:
            return JSONResponse({"error": "empty message"}, status_code=400)
        all_images = bool(normalized_attachments) and all(str(item.get("kind") or "") == "image" for item in normalized_attachments)
        message_with_attachments = (message or "[Attachment upload]") + _attachment_prompt_block(normalized_attachments)

        # Populate attachment path map so tool read guards auto-allow uploaded files
        # without requiring a permission prompt, even when the agent derives a wrong
        # path (e.g. /tmp/filename instead of the webui_uploads path).
        if normalized_attachments:
            att_map: dict[str, str] = {}
            for item in normalized_attachments:
                full_path = str(item.get("path") or "").strip()
                if not full_path:
                    continue
                from pathlib import Path as _Path
                uuid_name = _Path(full_path).name
                att_map[uuid_name] = full_path
                # Strip uuid prefix (format: "<hex>_<original>") to also match by original name
                parts = uuid_name.split("_", 1)
                if len(parts) == 2:
                    att_map[parts[1]] = full_path
            _attachment_paths_by_name.set(att_map)

        reset_lottery()
        if mentions and message:
            from cyrene.inbox import send_message
            from cyrene.subagent import _registry, reactivate, get_raw_messages, _spawn_subagent_task, _run_subagent

            valid_mentions = []
            for agent_id in mentions:
                agent_id = str(agent_id).strip()
                if not agent_id:
                    continue
                info = _registry.get(agent_id)
                if info is None:
                    continue
                valid_mentions.append(agent_id)
                status = str(info.get("status", "")).strip()
                if status in ("done", "timeout"):
                    mention_text = f"User sent you a new task. This is a round — complete it and report your result via quit.\n\n{message}"
                    await send_message("user", agent_id, "guidance", mention_text)
                    reactivated = await reactivate(agent_id)
                    if reactivated:
                        raw_msgs = await get_raw_messages(agent_id)
                        _spawn_subagent_task(
                            _run_subagent(agent_id, str(info.get("task") or ""), _bot, _CHAT_ID, _db_path, resume_messages=raw_msgs),
                            agent_id,
                        )
                else:
                    mention_text = (
                        f"[DIRECT_MESSAGE]\n"
                        f"The user has sent you guidance. This takes priority over your current approach — "
                        f"adjust your work accordingly. Use send_message_to_user ONCE to acknowledge and "
                        f"briefly say what you will change. Then continue working with the adjusted approach.\n\n"
                        f"User guidance:\n{message}"
                    )
                    await send_message("user", agent_id, "guidance", mention_text)

            if not valid_mentions:
                return JSONResponse({"error": "none of the mentioned agents exist"}, status_code=400)

            names = ", ".join(["@" + aid for aid in valid_mentions])
            response_text = f"Message sent to {names}."
            mention_prefix = " ".join(["@" + aid for aid in valid_mentions]) + " "

            user_entry = {
                "role": "user",
                "content": mention_prefix + message,
                "mentions": valid_mentions,
            }
            if normalized_attachments:
                user_entry["attachments"] = public_attachments
            if client_request_id:
                user_entry["client_request_id"] = client_request_id
            await _append_session_message(user_entry)

            if wants_stream:
                return StreamingResponse(
                    iter([_ndjson_line({"type": "reply_done", "response": response_text})]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache"},
                )
            return {"response": response_text}
        if guide_round_id:
            try:
                item = await queue_round_guidance(
                    guide_round_id,
                    message_with_attachments,
                    _bot,
                    _CHAT_ID,
                    _db_path,
                    client_request_id=client_request_id,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            payload = {
                "response": f"Sent to the main-agent inbox for {guide_round_id}. It will run after the current main-agent output finishes.",
                "queued": True,
                "guide_round_id": guide_round_id,
                "guide_request_id": item.get("id", ""),
            }
            if wants_stream:
                return StreamingResponse(
                    iter([_ndjson_line({"type": "queued", **payload})]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache"},
                )
            return payload

        try:
            if all_images and command != DEEP_REFLECT_COMMAND_ID:
                async def _run_direct_image_chat() -> str:
                    response_text = await _chat_with_uploaded_images(message, normalized_attachments)
                    await _persist_direct_image_chat(message, response_text, public_attachments, client_request_id)
                    labels = get_session_labels()
                    await archive_exchange(
                        message,
                        response_text,
                        _CHAT_ID,
                        session_title=labels.get("session_title", ""),
                        round_title=labels.get("round_title", ""),
                        round_id=labels.get("round_id", ""),
                        archive_session_id=labels.get("archive_session_id", ""),
                    )
                    return response_text

                if wants_stream:
                    return _stream_agent_reply(_run_direct_image_chat, message or "")
                return {"response": await _run_direct_image_chat()}
            if wants_stream:
                return _stream_agent_reply(
                    lambda: run_agent(
                        message_with_attachments,
                        _bot,
                        _CHAT_ID,
                        _db_path,
                        client_request_id=client_request_id,
                        lang=lang,
                        command=command,
                        public_user_message=message,
                        public_attachments=public_attachments,
                        permission_mode=permission_mode,
                    ),
                    message or "",
                )
            response = await run_agent(
                message_with_attachments,
                _bot,
                _CHAT_ID,
                _db_path,
                client_request_id=client_request_id,
                lang=lang,
                command=command,
                public_user_message=message,
                public_attachments=public_attachments,
                permission_mode=permission_mode,
            )
            if response == _AWAITING_USER_SENTINEL:
                return {"awaiting_user": True, "pending_question": get_pending_question()}
            labels = get_session_labels()
            await archive_exchange(
                message,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )
            return {"response": response}
        except httpx.TimeoutException as exc:
            logger.exception(
                "Chat request timed out while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            logger.exception(
                "Chat request failed while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model request failed", "detail": str(exc)},
                status_code=502,
            )
        except Exception as exc:
            logger.exception("Chat request crashed")
            return JSONResponse(
                {"error": "internal server error", "detail": str(exc)},
                status_code=500,
            )

    @router.post("/api/chat/answer-question")
    async def api_answer_question(request: Request):
        _conversation_source.set("webui")
        body = await request.json()
        question_id = str(body.get("question_id") or "").strip()
        selected_option = str(body.get("selected_option") or "").strip()
        answer_text = str(body.get("answer") or "").strip() or selected_option
        client_request_id = str(body.get("client_request_id") or "").strip()
        wants_stream = bool(body.get("stream"))
        if not question_id:
            return JSONResponse({"error": "missing question_id"}, status_code=400)
        if not answer_text:
            return JSONResponse({"error": "empty answer"}, status_code=400)

        try:
            if wants_stream:
                return _stream_agent_reply(
                    lambda: answer_pending_question(
                        question_id,
                        answer_text,
                        _bot,
                        _CHAT_ID,
                        _db_path,
                        client_request_id=client_request_id,
                    ),
                    answer_text,
                )
            response = await answer_pending_question(
                question_id,
                answer_text,
                _bot,
                _CHAT_ID,
                _db_path,
                client_request_id=client_request_id,
            )
            if response == _AWAITING_USER_SENTINEL:
                return {"awaiting_user": True, "pending_question": get_pending_question()}
            labels = get_session_labels()
            await archive_exchange(
                answer_text,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )
            return {"response": response}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            logger.exception(
                "Question-answer request timed out while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            logger.exception(
                "Question-answer request failed while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model request failed", "detail": str(exc)},
                status_code=502,
            )
        except Exception as exc:
            logger.exception("Question-answer request crashed")
            return JSONResponse(
                {"error": "internal server error", "detail": str(exc)},
                status_code=500,
            )

    @router.get("/api/chat/history")
    async def api_chat_history():
        return {"messages": _load_messages()}

    @router.get("/api/chat/state")
    async def api_chat_state():
        """Return raw session state (with round_id, tool_calls, etc.)."""
        from cyrene.config import STATE_FILE as _STATE_FILE
        if _STATE_FILE.exists():
            import json as _json
            try:
                data = _json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                msgs = data.get("messages", [])
                return {"messages": msgs if isinstance(msgs, list) else []}
            except Exception:
                pass
        return {"messages": []}

    @router.post("/api/chat/interrupt")
    async def api_interrupt_chat(session_id: str = ""):
        return {"ok": True, "interrupted": interrupt_active_run(session_id=session_id)}

    @router.post("/api/chat/clear")
    async def api_clear_session():
        await clear_session_id()
        return {"ok": True}

    @router.get("/api/subagents")
    async def api_subagents(session_id: str = ""):
        from cyrene.subagent import _registry  # noqa: WPS437
        items = []
        for agent_id, info in _registry.items():
            if session_id and str(info.get("session_id", "")) != session_id:
                continue
            items.append({
                "id": agent_id,
                "name": agent_id,
                "task": info.get("task", ""),
                "status": info.get("status", "running"),
                "result": info.get("result", ""),
            })
        return {"subagents": items}

    @router.get("/api/rounds/live")
    async def api_live_rounds():
        return {"rounds": get_live_rounds()}

    # ---- Group chat ----

    @router.get("/api/chat/agent-chat-messages")
    async def api_agent_chat_messages(round_id: str = ""):
        from cyrene.subagent import build_group_chat_messages

        if not round_id:
            return {"messages": [], "agents": []}
        return await build_group_chat_messages(round_id)

    @router.post("/api/chat/send-to-agents")
    async def api_send_to_agents(body: dict[str, Any]):
        from cyrene.subagent import _registry as _sub_reg
        from cyrene.inbox import send_message as _send_inbox, clear_inbox as _clear_inbox
        from cyrene import debug as _debug_comm

        round_id = str(body.get("round_id", "") or "").strip()
        text = str(body.get("text", "") or "").strip()
        mentions = body.get("mentions")
        attachments = body.get("attachments") or []

        if not round_id or not text:
            return {"ok": False, "error": "round_id and text are required"}

        # Build the full message text (append file references)
        full_text = text
        for att in attachments:
            path = str(att.get("path", "") or "").strip()
            name = str(att.get("name", "") or "").strip()
            if path:
                full_text += f"\n\n[{name}]({path})" if name else f"\n\n{path}"

        # Determine target agents
        if mentions and isinstance(mentions, list):
            targets = [str(m).strip() for m in mentions if str(m).strip()]
        else:
            # Send to all active subagents in this round
            from cyrene.subagent import _lock as _reg_lock

            async with _reg_lock:
                targets = [
                    aid for aid, info in _sub_reg.items()
                    if round_id and str(info.get("round_id", "") or "").strip() == round_id
                    and aid != "main"
                ]

        if not targets:
            return {"ok": False, "error": "No target agents found"}

        sent_to: list[str] = []
        first_msg_id = ""
        for target in targets:
            info = _sub_reg.get(target)
            is_done_timeout = info and str(info.get("status", "")).strip() in ("done", "timeout")

            if is_done_timeout:
                wrapped = f"User sent you a new task. This is a round — complete it and report your result via quit.\n\n{full_text}"
            else:
                wrapped = (
                    f"[DIRECT_MESSAGE]\n"
                    f"The user has sent you guidance. This takes priority over your current approach — "
                    f"adjust your work accordingly. Use send_message_to_user ONCE to acknowledge and "
                    f"briefly say what you will change. Then continue working with the adjusted approach.\n\n"
                    f"User guidance:\n{full_text}"
                )

            # 清空 inbox 确保 subagent 只看到这条用户消息
            await _clear_inbox(target)

            msg_id = await _send_inbox(
                from_agent="user",
                to_agent=target,
                msg_type="guidance",
                content=wrapped,
                round_id=round_id,
            )
            if msg_id:
                sent_to.append(target)
                if not first_msg_id:
                    first_msg_id = msg_id
                # Handle DONE/TIMEOUT agents: reactivate + spawn new task
                if is_done_timeout:
                    from cyrene.subagent import (
                        reactivate as _reactivate,
                        get_raw_messages as _get_raw,
                        _spawn_subagent_task,
                        _run_subagent,
                    )

                    reactivated = await _reactivate(target)
                    if reactivated:
                        raw_msgs = await _get_raw(target)
                        _spawn_subagent_task(
                            _run_subagent(target, str(info.get("task") or ""), _bot, _CHAT_ID, _db_path, resume_messages=raw_msgs),
                            target,
                        )

        # Publish SSE event for real-time group-chat update
        await _debug_comm.publish_event({
            "type": "agent_chat_user_message",
            "round_id": round_id,
            "message": {
                "id": first_msg_id or f"user_msg_{int(time.time() * 1000)}",
                "type": "user_message",
                "from": "user",
                "to": "all" if not mentions else ",".join(mentions),
                "content": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "round_id": round_id,
            },
        })

        return {"ok": True, "sent_to": sent_to}

    # ---- SSE ----

    @router.get("/api/events")
    async def api_events(request: Request, session_id: str = ""):
        from cyrene.debug import subscribe

        async def event_stream():
            async for event in subscribe(session_id=session_id):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/events/list")
    async def api_events_list(session_id: str = ""):
        """List recent event IDs."""
        from cyrene.debug import get_recent_events
        events = get_recent_events(50)
        result = []
        for e in events:
            if session_id and e.get("session_id") not in (session_id, ""):
                continue
            eid = e.get("event_id", "")
            if eid:
                result.append({"id": eid, "type": e.get("type", "?"), "caller": e.get("caller", "?")})
        return {"events": result}

    @router.get("/api/events/{event_id}")
    async def api_event_detail(event_id: str):
        from cyrene.debug import get_full_event
        event = get_full_event(event_id)
        if event is None:
            return JSONResponse({"error": "event not found"}, status_code=404)
        return event

    @router.get("/api/context-debug/events")
    async def api_context_debug_events(request: Request):
        """List recent LLM calls that have context trace metadata."""
        try:
            limit = int(request.query_params.get("limit") or "120")
        except ValueError:
            limit = 120
        limit = max(1, min(limit, 500))
        events_by_id: dict[str, dict[str, Any]] = {}

        def add_event(raw: dict[str, Any], log_file: str = "") -> None:
            if raw.get("type") != "llm_call":
                return
            event_id = str(raw.get("event_id") or "").strip()
            if not event_id:
                return
            trace = raw.get("context_trace") if isinstance(raw.get("context_trace"), dict) else {}
            included = trace.get("included") if isinstance(trace.get("included"), list) else []
            events_by_id[event_id] = {
                "id": event_id,
                "timestamp": raw.get("timestamp") or "",
                "caller": raw.get("caller") or "",
                "phase": raw.get("phase") or "",
                "model": raw.get("model") or "",
                "duration_ms": raw.get("duration_ms"),
                "total_tokens_est": int(trace.get("total_tokens_est") or 0),
                "block_count": len(included),
                "message_count": len(raw.get("messages") or []),
                "token_by_type": trace.get("token_by_type") or {},
                "source_log": log_file,
            }

        for event in debug.get_recent_events(500):
            add_event(event)

        if DATA_DIR.exists():
            for log_file in sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)[:20]:
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                add_event(json.loads(line), log_file.name)
                            except Exception:
                                continue
                except Exception:
                    continue

        events = sorted(
            events_by_id.values(),
            key=lambda item: str(item.get("timestamp") or ""),
            reverse=True,
        )[:limit]
        return {"events": events}

    @router.get("/api/context-debug/events/{event_id}")
    async def api_context_debug_event_detail(event_id: str):
        event = debug.get_full_event(event_id)
        if event is None or event.get("type") != "llm_call":
            return JSONResponse({"error": "event not found"}, status_code=404)
        return event

    # ---- Claude Code terminal / learning ----

    @router.get("/api/cc/status")
    async def api_cc_status():
        return get_cc_status(_CC_PROJECT_DIR)

    @router.get("/api/status")
    async def api_status():
        return await _build_status()

    async def _build_cc_learning_snapshot() -> dict[str, Any]:
        status = get_cc_status(_CC_PROJECT_DIR)
        latest_jsonl = str(status.get("latest_jsonl") or "").strip()
        if not latest_jsonl:
            return {
                "available": False,
                "reason": "No Claude transcript found for learning.",
                "summary": {"highlights": [], "top_tools": [], "top_tasks": []},
            }
        analysis = await asyncio.to_thread(analyze_session, Path(latest_jsonl))
        return {
            "available": True,
            **analysis,
        }

    @router.get("/api/cc/learning")
    async def api_cc_learning():
        return await _build_cc_learning_snapshot()

    @router.post("/api/cc/learn")
    async def api_cc_learn():
        status = get_cc_status(_CC_PROJECT_DIR)
        latest_jsonl = str(status.get("latest_jsonl") or "").strip()
        if not latest_jsonl:
            return JSONResponse({"error": "no Claude transcript found"}, status_code=404)
        result = await asyncio.to_thread(learn_from_session, Path(latest_jsonl))
        await debug.publish_event(
            {
                "type": "cc_learning",
                "phase": "completed",
                "user_input": "",
                "latest_jsonl": latest_jsonl,
                "highlights": result.get("summary", {}).get("highlights", []),
                "top_tools": result.get("summary", {}).get("top_tools", []),
                "top_tasks": result.get("summary", {}).get("top_tasks", []),
            }
        )
        return result

    @router.websocket("/ws/cc-terminal/{tmux_session}")
    async def ws_cc_terminal(websocket: WebSocket, tmux_session: str):
        await websocket.accept()
        session = CCTerminalSession(tmux_session)
        input_buffer = ""

        try:
            await session.start()
        except Exception:
            logger.exception("Failed to attach CC terminal to tmux session %s", tmux_session)
            await websocket.send_text("\r\n[Cyrene] Failed to attach to tmux session.\r\n")
            await websocket.close(code=1011)
            return

        stream_task = asyncio.create_task(session.stream_to_ws(websocket))
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                message_type = str(payload.get("type") or "").strip()
                if message_type == "input":
                    data = str(payload.get("data") or "")
                    await session.handle_input(data)
                    input_buffer, submitted = _consume_cc_input_buffer(input_buffer, data)
                    for prompt in submitted:
                        asyncio.create_task(_publish_cc_learning(prompt, tmux_session=tmux_session))
                elif message_type == "resize":
                    await session.handle_resize(int(payload.get("cols") or 80), int(payload.get("rows") or 24))
        except WebSocketDisconnect:
            pass
        finally:
            stream_task.cancel()
            await session.stop()

    @router.websocket("/ws/browser")
    async def ws_browser(websocket: WebSocket):
        """Live screencast of the agent's browser session.

        Streams CDP JPEG frames to the chat-side browser panel. The control
        channel (start/stop/set_quality) is reserved for later; login takeover
        (M3) happens in the native window, not over this socket.
        """
        await websocket.accept()
        from cyrene import browser as _browser

        if _browser._ensure_playwright() is None:
            await websocket.send_json({"type": "error", "error": _browser.browser_runtime_unavailable_message()})
            await websocket.close()
            return

        try:
            session = await _browser.get_session()
        except Exception as exc:
            await websocket.send_json({"type": "error", "error": f"Browser launch failed: {_browser.browser_runtime_unavailable_message(exc)}"})
            await websocket.close()
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        await session.start_screencast(queue)

        async def _pump() -> None:
            try:
                while True:
                    frame = await queue.get()
                    await websocket.send_json({"type": "frame", **frame})
            except Exception:
                return

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    json.loads(raw)  # reserved control messages — parsed, no-op for now
                except json.JSONDecodeError:
                    continue
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            await session.stop_screencast(queue)

    # ---- Sessions API ----

    @router.get("/api/sessions")
    async def api_sessions():
        from cyrene import db as cy_db
        try:
            now_local = datetime.now(timezone.utc).astimezone()
            day_from = (now_local - timedelta(days=27)).strftime("%Y-%m-%d")
            day_to = now_local.strftime("%Y-%m-%d")
            model_stats = await cy_db.get_model_stats_range(_db_path, day_from, day_to)
        except Exception:
            model_stats = []
        return {"sessions": _build_sessions(), "model_stats": model_stats}

    @router.post("/api/sessions")
    async def api_create_session():
        """Start a new session by clearing current state.

        Compresses the existing conversation into short-term memory first
        (handled inside clear_session_id), then wipes state.json so the
        next message starts a fresh context window.
        """
        await clear_session_id()
        return {"ok": True, "sessions": _build_sessions()}

    @router.get("/api/sessions/archive-context")
    async def api_archive_context(cursor: str = ""):
        """Return the next archive session after *cursor*.

        Cursor is a full archive session id (``archive_YYYY-MM-DD_<id>``).
        When empty, returns the most recent archive session.
        Each message has ``isArchivedContext: true`` so the frontend can
        style it as read‑only historical context.

        Skips the current live session's own archive to avoid showing
        the same messages that are already in the live view.
        """
        # Skip the archive that belongs to the current live session
        current_skip_ids: set[str] = set()
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                caid = str(state.get("archive_session_id", "")).strip()
                cad = datetime.now().astimezone().strftime("%Y-%m-%d")
                if caid:
                    current_skip_ids.add(f"{cad}:{caid}")
            except Exception:
                pass

        archives = _build_archive_sessions(skip_archive_ids=current_skip_ids)
        if not archives:
            return {"messages": [], "hasMore": False}

        start = 0
        if cursor.strip():
            for idx, a in enumerate(archives):
                if a.get("id") == cursor.strip():
                    start = idx + 1
                    break
            else:
                return {"messages": [], "hasMore": False}

        if start >= len(archives):
            return {"messages": [], "hasMore": False}

        target = archives[start]
        raw_messages = target.get("chat", {}).get("messages", [])
        for msg in raw_messages:
            msg["isArchivedContext"] = True

        return {
            "messages": raw_messages,
            "id": target["id"],
            "archiveSessionId": target.get("archiveSessionId", ""),
            "archiveDate": target.get("archiveDate", ""),
            "title": target.get("title", ""),
            "hasMore": (start + 1) < len(archives),
        }

    @router.delete("/api/sessions/{session_id}")
    async def api_delete_session(session_id: str):
        """Delete a session.

        - run_live: same as create (clear current state).
        - archive_YYYY-MM-DD_<session_id>: deletes one archived session from that day.
        """
        if session_id == "run_live":
            await clear_session_id()
            return {"ok": True, "sessions": _build_sessions()}

        if session_id.startswith("archive_"):
            suffix = session_id[len("archive_"):]
            date_str, _, archive_session_id = suffix.partition("_")
            filepath = CONVERSATIONS_DIR / f"{date_str}.md"
            if not filepath.exists():
                return JSONResponse({"error": "session not found"}, status_code=404)
            try:
                content = filepath.read_text(encoding="utf-8")
                sections = _parse_archive_sections(content)
                kept_sections = [
                    section for section in sections
                    if str(section.get("archive_session_id", "")).strip() != archive_session_id
                ]
                if len(kept_sections) == len(sections):
                    return JSONResponse({"error": "session not found"}, status_code=404)
                _write_archive_sections(filepath, date_str, kept_sections)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            return {"ok": True, "sessions": _build_sessions()}

        return JSONResponse({"error": "unknown session id"}, status_code=400)

    @router.get("/api/sessions/{session_id}/export")
    async def api_export_session(session_id: str, format: str = "markdown"):
        """Export a session as Markdown or JSON.

        session_id: 'run_live' or 'archive_YYYY-MM-DD_<archive_session_id>'
        format: 'markdown' (default) or 'json'
        """
        fmt = format.strip().lower()
        if fmt not in ("markdown", "json"):
            return JSONResponse({"error": "format must be 'markdown' or 'json'"}, status_code=400)

        if session_id == "run_live":
            # Read current session from state.json
            raw_msgs: list[dict] = []
            session_title = "current session"
            created_at = ""
            if STATE_FILE.exists():
                try:
                    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                    raw_msgs = state.get("messages", []) or []
                    session_title = str(state.get("session_title", "")).strip() or "current session"
                    created_at = datetime.now().astimezone().strftime("%Y-%m-%d")
                except Exception:
                    pass
            messages = []
            for m in raw_msgs:
                role = str(m.get("role", "")).strip()
                if role not in ("user", "assistant"):
                    continue
                content = str(m.get("content") or "").strip()
                if not content:
                    continue
                messages.append({
                    "role": role,
                    "content": content,
                    "time": str(m.get("created_at", "") or "").strip(),
                })
            updated_at = created_at

        elif session_id.startswith("archive_"):
            suffix = session_id[len("archive_"):]
            date_str, _, archive_session_id = suffix.partition("_")
            filepath = CONVERSATIONS_DIR / f"{date_str}.md"
            if not filepath.exists():
                return JSONResponse({"error": "session not found"}, status_code=404)
            try:
                content = filepath.read_text(encoding="utf-8")
                sections = _parse_archive_sections(content)
                matching = [
                    s for s in sections
                    if str(s.get("archive_session_id", "")).strip() == archive_session_id
                ]
                if not matching and archive_session_id.startswith("legacy_"):
                    matching = [
                        s for s in sections
                        if not str(s.get("archive_session_id", "")).strip()
                    ]
                if not matching:
                    return JSONResponse({"error": "session not found"}, status_code=404)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

            session_title = next(
                (str(s.get("session_title", "")).strip() for s in matching if s.get("session_title")),
                "",
            ) or date_str
            created_at = date_str
            timestamps = [str(s.get("timestamp", "")).strip() for s in matching if s.get("timestamp")]
            updated_at = timestamps[-1] if timestamps else date_str
            messages = []
            for s in matching:
                ts = str(s.get("timestamp", "")).strip()
                user_body = str(s.get("user_body", "")).strip()
                assistant_body = str(s.get("assistant_body", "")).strip()
                if user_body:
                    messages.append({"role": "user", "content": user_body, "time": ts})
                if assistant_body:
                    messages.append({"role": "assistant", "content": assistant_body, "time": ts})
        else:
            return JSONResponse({"error": "unknown session id"}, status_code=400)

        safe_title = re.sub(r"[^\w\-. ]+", "_", session_title or session_id, flags=re.ASCII)[:60].strip("_. ") or "session"

        if fmt == "json":
            import json as _json
            payload = {
                "id": session_id,
                "title": session_title,
                "created_at": created_at,
                "updated_at": updated_at,
                "message_count": len(messages),
                "messages": messages,
            }
            content_bytes = _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            filename = f"{safe_title}.json"
            return StreamingResponse(
                iter([content_bytes]),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        # Markdown format
        lines: list[str] = [f"# {session_title}", ""]
        lines.append(f"**Session ID**: `{session_id}`")
        lines.append(f"**Date**: {created_at}")
        lines.append(f"**Messages**: {len(messages)}")
        lines.append("")
        lines.append("---")
        lines.append("")

        i = 0
        while i < len(messages):
            msg = messages[i]
            ts = msg.get("time", "")
            role = msg.get("role", "user")
            content_text = msg.get("content", "")

            if role == "user":
                if ts:
                    lines.append(f"## {ts}")
                    lines.append("")
                lines.append(f"**User**: {content_text}")
                lines.append("")
                # Look for the following assistant message at the same timestamp
                if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant":
                    assistant_content = messages[i + 1].get("content", "")
                    lines.append(f"**Cyrene**: {assistant_content}")
                    lines.append("")
                    lines.append("---")
                    lines.append("")
                    i += 2
                    continue
            else:
                lines.append(f"**Cyrene**: {content_text}")
                lines.append("")
                lines.append("---")
                lines.append("")
            i += 1

        md_text = "\n".join(lines)
        content_bytes = md_text.encode("utf-8")
        filename = f"{safe_title}.md"
        return StreamingResponse(
            iter([content_bytes]),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---- Evolution API ----

    @router.get("/api/evolution")
    async def api_evolution():
        """Aggregated data for the Evolution page."""
        from cyrene import pattern as _pattern
        status, scripts, patterns, learned_skills, cc_learning = await asyncio.gather(
            _build_status(),
            _pattern.list_scripts("all"),
            _pattern.list_patterns("all"),
            _pattern.list_learned_skills(),
            _build_cc_learning_snapshot(),
        )
        return {
            "phase": status.get("phase", ""),
            "state": status.get("state", ""),
            "scripts": scripts,
            "patterns": patterns,
            "learned_skills": learned_skills,
            "cc_learning": cc_learning,
        }

    @router.get("/api/scripts")
    async def api_scripts(status: str = "all"):
        from cyrene import pattern as _pattern
        return {"scripts": await _pattern.list_scripts(status)}

    @router.get("/api/patterns")
    async def api_patterns(status: str = "all"):
        from cyrene import pattern as _pattern
        return {"patterns": await _pattern.list_patterns(status)}

    @router.get("/api/learned-skills")
    async def api_learned_skills():
        from cyrene import pattern as _pattern
        return {"skills": await _pattern.list_learned_skills()}

    @router.get("/api/learned-skills/{skill_id}")
    async def api_learned_skill_detail(skill_id: str):
        from cyrene import pattern as _pattern
        skill = await _pattern.get_learned_skill(skill_id)
        if skill is None:
            return JSONResponse({"error": "skill not found"}, status_code=404)
        return {"skill": skill}

    @router.get("/api/learned-skills/{skill_id}/versions")
    async def api_learned_skill_versions(skill_id: str):
        from cyrene import pattern as _pattern
        return {"versions": await _pattern.list_learned_skill_versions(skill_id)}

    @router.get("/api/learned-skills/{skill_id}/patches")
    async def api_learned_skill_patches(skill_id: str, status: str = "all"):
        from cyrene import pattern as _pattern
        return {"patches": await _pattern.list_learned_skill_patches(skill_id, status)}

    @router.get("/api/learned-skills/{skill_id}/runs")
    async def api_learned_skill_runs(skill_id: str, limit: int = 50):
        from cyrene import pattern as _pattern
        return {"runs": await _pattern.list_learned_skill_runs(skill_id, limit)}

    @router.get("/api/learned-skills/{skill_id}/replay-tests")
    async def api_learned_skill_replay_tests(skill_id: str):
        from cyrene import pattern as _pattern
        return {"tests": await _pattern.list_skill_replay_tests(skill_id)}

    @router.post("/api/learned-skills/{skill_id}/update")
    async def api_update_learned_skill(skill_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        updates = payload.get("updates") if isinstance(payload, dict) else None
        reason = str((payload or {}).get("reason") or "Manual skill edit.")
        result = await _pattern.update_learned_skill(skill_id, updates if isinstance(updates, dict) else {}, reason=reason)
        if result is None:
            return JSONResponse({"error": "skill not found or invalid payload"}, status_code=404)
        return {"ok": True, "skill": result}

    @router.post("/api/learned-skills/{skill_id}/rollback")
    async def api_rollback_learned_skill(skill_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        version = int((payload or {}).get("version") or 0)
        result = await _pattern.rollback_learned_skill(skill_id, version)
        if not result.get("ok"):
            return JSONResponse(result, status_code=404)
        return result

    @router.post("/api/learned-skills/{skill_id}/replay-tests/run")
    async def api_run_learned_skill_replay_tests(skill_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_skill_replay_tests(skill_id)
        return {"ok": True, "result": result}

    @router.post("/api/learned-skills/{skill_id}/patches/{patch_id}/apply")
    async def api_apply_learned_skill_patch(skill_id: str, patch_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.apply_skill_patch(skill_id, patch_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/learned-skills/{skill_id}/patches/{patch_id}/reject")
    async def api_reject_learned_skill_patch(skill_id: str, patch_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_skill_patch(skill_id, patch_id)
        if not ok:
            return JSONResponse({"error": "patch not found"}, status_code=404)
        return {"ok": True}

    @router.post("/api/learned-skills/{skill_id}/activate")
    async def api_activate_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.approve_script(skill_id)
        return {"ok": ok}

    @router.post("/api/learned-skills/{skill_id}/deprecate")
    async def api_deprecate_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_script(skill_id)
        return {"ok": ok}

    @router.post("/api/learned-skills/{skill_id}/run")
    async def api_run_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_script(skill_id)
        return {"ok": True, "result": result}

    @router.post("/api/scripts/{script_id}/approve")
    async def api_approve_script(script_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.approve_script(script_id)
        return {"ok": ok}

    @router.post("/api/scripts/{script_id}/reject")
    async def api_reject_script(script_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_script(script_id)
        return {"ok": ok}

    @router.post("/api/scripts/{script_id}/run")
    async def api_run_script(script_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_script(script_id)
        return {"ok": True, "result": result}

    @router.post("/api/patterns/learn")
    async def api_patterns_learn():
        from cyrene import pattern as _pattern

        stats = await _pattern.scan_for_manual_learn()
        return {
            "ok": True,
            "stats": stats,
            "patterns": await _pattern.list_patterns("all"),
            "learned_skills": await _pattern.list_learned_skills(),
            "scripts": await _pattern.list_scripts("all"),
        }

    @router.post("/api/patterns/rebuild")
    async def api_patterns_rebuild():
        from cyrene import pattern as _pattern

        result = await _pattern.rebuild_learning_state(reprocess_all_turns=True)
        return {
            "ok": True,
            "result": result,
            "patterns": await _pattern.list_patterns("all"),
            "learned_skills": await _pattern.list_learned_skills(),
            "scripts": await _pattern.list_scripts("all"),
        }

    @router.get("/api/vocabulary")
    async def api_vocabulary():
        from cyrene import pattern as _pattern
        return await _pattern.vocabulary_snapshot()

    @router.post("/api/vocabulary/labels")
    async def api_create_vocabulary_label(request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.create_vocabulary_label(
                label_type=str((payload or {}).get("label_type") or ""),
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                domain=str((payload or {}).get("domain") or ""),
                parent_label=str((payload or {}).get("parent_label") or ""),
                raw_description=str((payload or {}).get("raw_description") or ""),
                status=str((payload or {}).get("status") or "active"),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "label": result}

    @router.post("/api/vocabulary/aliases")
    async def api_create_vocabulary_alias(request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.create_vocabulary_alias(
                label_type=str((payload or {}).get("label_type") or ""),
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                alias_label=str((payload or {}).get("alias_label") or ""),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "alias": result}

    @router.post("/api/vocabulary/unknown/{unknown_id}/promote")
    async def api_promote_unknown_label(unknown_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.promote_unknown_label(
                unknown_id,
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                alias_label=str((payload or {}).get("alias_label") or ""),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "unknown": result}

    @router.post("/api/vocabulary/unknown/{unknown_id}/dismiss")
    async def api_dismiss_unknown_label(unknown_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.dismiss_unknown_label(unknown_id)
        if not ok:
            return JSONResponse({"error": "unknown label not found"}, status_code=404)
        return {"ok": True}

    # ---- Skills install API ----

    @router.get("/api/skills/installed")
    async def api_installed_skills():
        return {"skills": _build_skills()}

    @router.post("/api/skills/install")
    async def api_install_skill(request: Request):
        body = await request.json()
        source_path = Path(str(body.get("path") or "")).expanduser()
        if not source_path.exists():
            return JSONResponse({"ok": False, "error": "invalid skill source path"}, status_code=400)
        result = install_skill_from_path(source_path)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/skills/install-upload")
    async def api_install_skill_upload(request: Request):
        """Install a skill from an uploaded file (browser file picker path)."""
        import tempfile

        try:
            form = await request.form()
            file = form.get("file")
            if not file:
                return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)
            content = await file.read()
            if len(content) > 8 * 1024 * 1024:  # 8 MB (matches _MAX_SKILL_ARCHIVE_BYTES)
                return JSONResponse({"ok": False, "error": "File too large (max 8 MB)"}, status_code=400)
            suffix = Path(file.filename or "skill.tmp").suffix or ".tmp"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                result = install_skill_from_path(Path(tmp_path))
                if not result.get("ok", False):
                    return JSONResponse(result, status_code=400)
                return result
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @router.post("/api/skills/install-picker")
    async def api_install_skill_picker():
        import platform
        import subprocess

        system = platform.system()
        if system != "Darwin":
            return JSONResponse({"ok": False, "error": f"Skill picker not supported on {system}"}, status_code=400)

        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Select skill folder containing SKILL.md")'],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "error": "Picker timed out — please try again"}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Picker error: {e}"}, status_code=400)

        stderr = (result.stderr or "").strip()
        if stderr and "User cancelled" not in stderr:
            return JSONResponse({"ok": False, "error": f"Picker error: {stderr}"}, status_code=400)

        selected = result.stdout.strip()
        if not selected:
            return {"ok": False, "cancelled": True}

        source_path = Path(selected).expanduser()
        if not source_path.exists():
            return JSONResponse({"ok": False, "error": "selected skill source is invalid"}, status_code=400)

        result = install_skill_from_path(source_path)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/skills/{skill_id}/toggle")
    async def api_toggle_skill(skill_id: str):
        if not _toggle_skill(skill_id):
            return JSONResponse({"ok": False, "error": "skill not found"}, status_code=404)
        return {"ok": True}

    @router.post("/api/skills/{skill_id}/uninstall")
    async def api_uninstall_skill(skill_id: str):
        if not _uninstall_skill(skill_id):
            return JSONResponse({"ok": False, "error": "skill not found"}, status_code=404)
        return {"ok": True}

    # ---- Search API ----

    @router.get("/api/search/conversations")
    async def api_search_conversations(q: str = "", limit: int = 30):
        if not q.strip():
            return {"ok": False, "error": "query is required"}
        results = await search_conversations_structured(q.strip(), limit=max(1, min(limit, 100)))
        return {"ok": True, "results": results}

    # ---- Token Usage API ----

    @router.get("/api/usage/tokens")
    async def api_token_usage(days: int = 7, model: str = ""):
        from cyrene.db import get_token_usage_stats
        stats = await get_token_usage_stats(str(DB_PATH), days=max(1, min(days, 90)), model=model.strip())
        return {"ok": True, "stats": stats}

    # ---- Backup API ----

    @router.get("/api/backup/list")
    async def api_backup_list():
        from cyrene.backup import list_backups
        return {"ok": True, "backups": list_backups()}

    @router.post("/api/backup/export")
    async def api_backup_export():
        from cyrene.backup import export_backup
        result = await export_backup()
        return result

    @router.post("/api/backup/restore")
    async def api_backup_restore(request: Request):
        from cyrene.backup import restore_backup
        body = await request.json()
        path = str(body.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        result = await restore_backup(path)
        return result

    @router.post("/api/backup/delete")
    async def api_backup_delete(request: Request):
        from cyrene.backup import delete_backup
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        ok = await delete_backup(name)
        return {"ok": ok}

    @router.post("/api/backup/download/{backup_name}")
    async def api_backup_download(backup_name: str):
        from cyrene.backup import _BACKUP_DIR
        target = (_BACKUP_DIR / backup_name).resolve()
        backups_root = _BACKUP_DIR.resolve()
        if backups_root not in target.parents:
            return JSONResponse({"error": "invalid backup path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "backup not found"}, status_code=404)
        return FileResponse(target, filename=backup_name, media_type="application/zip")

    # ---- Notification API ----

    @router.post("/api/notifications/send")
    async def api_notifications_send(request: Request):
        from cyrene.notifications import notify
        body = await request.json()
        title = str(body.get("title") or "Cyrene").strip()
        text = str(body.get("text") or "").strip()
        channel = str(body.get("channel") or "auto").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        result = await notify(title, text, channel=channel)
        return result

    # ---- Browser API ----

    @router.post("/api/browser/navigate")
    async def api_browser_navigate(request: Request):
        from cyrene.browser import navigate
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        result = await navigate(url)
        return result

    # ---- Memory API ----

    @router.get("/api/memory")
    async def api_memory():
        return await _build_memory()

    # ---- Skills API ----

    @router.get("/api/skills")
    async def api_skills():
        return {"skills": _build_skills()}

    # ---- Settings API ----

    @router.get("/api/onboarding")
    async def api_get_onboarding():
        return {"onboarding": get_onboarding_status()}

    @router.post("/api/onboarding/llm")
    async def api_onboarding_llm(request: Request):
        body = await request.json()
        try:
            return await save_and_test_llm_setup(
                str(body.get("api_key") or ""),
                str(body.get("base_url") or ""),
                str(body.get("model") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )

    @router.post("/api/onboarding/personality")
    async def api_onboarding_personality(request: Request):
        body = await request.json()
        try:
            return await save_personality_setup(
                str(body.get("mode") or ""),
                name=str(body.get("name") or ""),
                content=str(body.get("content") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )

    # ---- Context management (SOUL.md / workspace chips) ----

    @router.get("/api/context/state")
    async def api_context_state():
        from cyrene.settings_store import is_workspace_active, is_soul_active, get_workspace_history
        return {
            "soul_active": is_soul_active(),
            "workspace_active": is_workspace_active(),
            "workspace_dir": str(WORKSPACE_DIR),
            "workspace_history": get_workspace_history(),
        }

    @router.post("/api/context/remove-soul")
    async def api_remove_soul():
        from cyrene.settings_store import set_soul_active
        set_soul_active(False)
        return {"ok": True}

    @router.post("/api/context/add-soul")
    async def api_add_soul():
        from cyrene.settings_store import set_soul_active
        set_soul_active(True)
        return {"ok": True}

    @router.post("/api/context/remove-workspace")
    async def api_remove_workspace():
        from cyrene.settings_store import set_workspace_active
        set_workspace_active(False)
        return {"ok": True}

    @router.post("/api/context/add-workspace")
    async def api_add_workspace(request: Request):
        from cyrene.settings_store import set_workspace_active, add_workspace_to_history
        body = await request.json()
        path = str(body.get("path", "")).strip()
        set_workspace_active(True)
        if path:
            add_workspace_to_history(path)
        return {"ok": True}

    @router.post("/api/context/pick-directory")
    async def api_pick_directory():
        import platform
        import subprocess
        system = platform.system()
        if system == "Darwin":
            result = subprocess.run(
                ['osascript', '-e', 'POSIX path of (choose folder with prompt "Select workspace directory")'],
                capture_output=True, text=True, timeout=30,
            )
            path = result.stdout.strip()
            if path:
                return {"path": path}
            return {"path": "", "cancelled": True}
        return {"path": "", "error": f"Directory picker not supported on {system}"}

    @router.get("/api/settings/soul")
    async def api_get_soul():
        return {"content": _read_soul()}

    @router.put("/api/settings/soul")
    async def api_update_soul(request: Request):
        body = await request.json()
        SOUL_PATH.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    @router.get("/api/settings/keys")
    async def api_get_keys():
        from cyrene.config import get_env_keys_meta
        return {"keys": get_env_keys_meta()}

    @router.put("/api/settings/keys")
    async def api_update_keys(request: Request):
        from cyrene.config import write_env_keys, _EDITABLE_KEYS
        body = await request.json()
        updates = {}
        for key, meta in _EDITABLE_KEYS.items():
            value = body.get(key, "")
            if not value:
                continue
            # 跳过未修改的 masked 值（全为 • 或太短）
            if meta["masked"] and (value.startswith("••") or len(value) <= 8):
                continue
            updates[key] = value
        if not updates:
            return JSONResponse({"error": "no valid keys provided"}, status_code=400)
        write_env_keys(updates)
        return {"ok": True, "updated": list(updates.keys())}

    @router.get("/api/settings/models")
    async def api_get_models():
        from cyrene.settings_store import get_models, get_vision_models, get_secondary_model
        from cyrene.config import OPENAI_API_KEY, DEFAULT_OPENAI_BASE_URL, read_env_file

        def _normalize_candidates(raw_items: list[dict[str, Any]] | None, fallback_api_key: str, fallback_base_url: str) -> list[dict[str, Any]]:
            normalized_items: list[dict[str, Any]] = []
            for index, model in enumerate(raw_items or []):
                model_identifier = str(
                    model.get("model")
                    or model.get("name")
                    or model.get("id")
                    or ""
                ).strip()
                if not model_identifier:
                    continue
                model_base_url = str(model.get("base_url") or fallback_base_url or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
                raw_model_api_key = _strip_wrapping_quotes(str(model.get("api_key") or "").strip())
                if raw_model_api_key:
                    model_api_key = raw_model_api_key
                elif model_base_url.rstrip("/") == (fallback_base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/"):
                    model_api_key = fallback_api_key
                else:
                    model_api_key = ""
                normalized_items.append(
                    {
                        "id": str(model.get("id") or f"candidate-{index + 1}").strip() or f"candidate-{index + 1}",
                        "name": str(model.get("name") or model_identifier).strip() or model_identifier,
                        "model": model_identifier,
                        "desc": str(model.get("desc") or "").strip(),
                        "ctx": str(model.get("ctx") or "").strip(),
                        "price": str(model.get("price") or "").strip(),
                        "api_key": model_api_key,
                        "base_url": model_base_url,
                    }
                )
            return normalized_items

        raw_models = get_models()
        raw_vision_models = get_vision_models()
        raw_secondary = get_secondary_model()
        active_model_name, base_url = _live_llm_config()
        env_keys = read_env_file()
        active_api_key = _strip_wrapping_quotes(str(env_keys.get("OPENAI_API_KEY") or OPENAI_API_KEY or "").strip())
        normalized = _normalize_candidates(raw_models, active_api_key, base_url)
        normalized_vision = _normalize_candidates(raw_vision_models, active_api_key, base_url)

        # Normalize secondary model (single item)
        sec_model = str(raw_secondary.get("model") or "").strip()
        ctx_limit = int(raw_secondary.get("ctx_limit") or 0)
        max_concurrency = int(raw_secondary.get("max_concurrency") or 0)
        if sec_model:
            sec_base_url = str(raw_secondary.get("base_url") or base_url or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
            sec_raw_api_key = _strip_wrapping_quotes(str(raw_secondary.get("api_key") or "").strip())
            if sec_raw_api_key:
                sec_api_key = sec_raw_api_key
            elif sec_base_url.rstrip("/") == (base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/"):
                sec_api_key = active_api_key
            else:
                sec_api_key = ""
            normalized_secondary = {
                "id": "secondary",
                "name": str(raw_secondary.get("name") or sec_model).strip(),
                "model": sec_model,
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": sec_api_key,
                "base_url": sec_base_url,
                "ctx_limit": ctx_limit,
                "max_concurrency": max_concurrency,
            }
        else:
            normalized_secondary = {
                "id": "secondary",
                "name": "",
                "model": "",
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": "",
                "base_url": base_url or DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": 0,
                "max_concurrency": 0,
            }

        if not normalized:
            normalized = [
                {
                    "id": "candidate-1",
                    "name": active_model_name or "deepseek-v4-flash",
                    "model": active_model_name or "deepseek-v4-flash",
                    "desc": "",
                    "ctx": "",
                    "price": "",
                    "api_key": active_api_key,
                    "base_url": base_url or DEFAULT_OPENAI_BASE_URL,
                }
            ]
        if not normalized_vision:
            normalized_vision = [
                {
                    "id": "vision-candidate-1",
                    "name": normalized[0]["model"],
                    "model": normalized[0]["model"],
                    "desc": "",
                    "ctx": "",
                    "price": "",
                    "api_key": normalized[0]["api_key"],
                    "base_url": normalized[0]["base_url"],
                }
            ]

        active_model_id = next(
            (
                str(model.get("id") or "").strip()
                for model in normalized
                if str(model.get("model") or "").strip() == active_model_name
                or str(model.get("name") or "").strip() == active_model_name
                or str(model.get("id") or "").strip() == active_model_name
            ),
            str(normalized[0].get("id") or "candidate-1"),
        )
        return {
            "models": normalized,
            "primary_candidates": normalized,
            "vision_models": normalized_vision,
            "vision_candidates": normalized_vision,
            "secondary_model": normalized_secondary,
            "active": active_model_id,
            "active_model_name": active_model_name,
            "base_url": base_url,
        }

    @router.put("/api/settings/models")
    async def api_update_models(request: Request):
        from cyrene.settings_store import save_models, save_vision_models, save_secondary_model, get_secondary_model
        from cyrene.config import DEFAULT_OPENAI_BASE_URL, write_env_keys
        from cyrene.onboarding import _test_llm_connection
        body = await request.json()
        raw_models = body.get("models")
        raw_vision_models = body.get("vision_models")
        raw_secondary = body.get("secondary_model")
        if not isinstance(raw_models, list) or len(raw_models) == 0:
            return JSONResponse({"error": "models must be a non-empty list"}, status_code=400)
        if raw_vision_models is not None and (not isinstance(raw_vision_models, list) or len(raw_vision_models) == 0):
            return JSONResponse({"error": "vision_models must be a non-empty list"}, status_code=400)

        def _normalize_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            normalized_items: list[dict[str, Any]] = []
            for index, model in enumerate(raw_items):
                model_identifier = str(
                    model.get("model")
                    or model.get("name")
                    or model.get("id")
                    or ""
                ).strip()
                if not model_identifier:
                    continue
                normalized_items.append(
                    {
                        "id": str(model.get("id") or f"candidate-{index + 1}").strip() or f"candidate-{index + 1}",
                        "name": model_identifier,
                        "model": model_identifier,
                        "desc": str(model.get("desc") or "").strip(),
                        "ctx": str(model.get("ctx") or "").strip(),
                        "price": str(model.get("price") or "").strip(),
                        "api_key": _strip_wrapping_quotes(str(model.get("api_key") or "").strip()),
                        "base_url": str(model.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                    }
                )
            return normalized_items

        normalized = _normalize_candidates(raw_models)
        normalized_vision = _normalize_candidates(raw_vision_models if isinstance(raw_vision_models, list) else [])

        if not normalized:
            return JSONResponse({"error": "models must contain at least one valid model"}, status_code=400)
        if raw_vision_models is not None and not normalized_vision:
            return JSONResponse({"error": "vision_models must contain at least one valid model"}, status_code=400)

        primary = normalized[0]
        primary_model = str(primary.get("model") or "").strip()
        primary_base_url = str(primary.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
        primary_api_key = _strip_wrapping_quotes(str(primary.get("api_key") or "").strip())

        try:
            await _test_llm_connection(primary_api_key, primary_base_url, primary_model)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        save_models(normalized)
        if raw_vision_models is not None:
            save_vision_models(normalized_vision)
        if isinstance(raw_secondary, dict):
            save_secondary_model(raw_secondary)
        write_env_keys(
            {
                "OPENAI_MODEL": primary_model,
                "OPENAI_BASE_URL": primary_base_url,
                "OPENAI_API_KEY": primary_api_key,
            }
        )
        saved_secondary = get_secondary_model()
        sec_model = str(saved_secondary.get("model") or "").strip()
        ctx_limit = int(saved_secondary.get("ctx_limit") or 0)
        max_concurrency = int(saved_secondary.get("max_concurrency") or 0)
        if sec_model:
            normalized_secondary = {
                "id": "secondary",
                "name": str(saved_secondary.get("name") or sec_model).strip(),
                "model": sec_model,
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": _strip_wrapping_quotes(str(saved_secondary.get("api_key") or "").strip()),
                "base_url": str(saved_secondary.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": ctx_limit,
                "max_concurrency": max_concurrency,
            }
        else:
            normalized_secondary = {
                "id": "secondary",
                "name": "",
                "model": "",
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": "",
                "base_url": DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": 0,
                "max_concurrency": 0,
            }
        return {
            "ok": True,
            "models": normalized,
            "primary_candidates": normalized,
            "vision_models": normalized_vision if raw_vision_models is not None else None,
            "vision_candidates": normalized_vision if raw_vision_models is not None else None,
            "secondary_model": normalized_secondary,
            "active": str(primary.get("id") or "candidate-1"),
            "active_model_name": primary_model,
            "base_url": primary_base_url,
        }

    @router.get("/api/settings/tools")
    async def api_get_tools():
        from cyrene.settings_store import get_enabled_tools
        from cyrene.tools import TOOL_DEFS
        enabled = get_enabled_tools()
        tools = []
        for td in TOOL_DEFS:
            name = td["function"]["name"]
            tools.append({
                "name": name,
                "desc": td["function"]["description"],
                "enabled": enabled.get(name, True),
            })
        # Include MCP tools from connected servers
        try:
            from cyrene.mcp_manager import get_manager as _get_mcp_mgr
            manager = _get_mcp_mgr()
            for mcp_td in manager.get_tool_defs():
                name = mcp_td["function"]["name"]
                tools.append({
                    "name": name,
                    "desc": mcp_td["function"]["description"],
                    "enabled": enabled.get(name, True),
                    "source": "mcp",
                })
        except Exception:
            pass
        return {"tools": tools}

    @router.put("/api/settings/tools")
    async def api_update_tools(request: Request):
        from cyrene.settings_store import save_enabled_tools
        body = await request.json()
        updates = body.get("tools", {})
        if not isinstance(updates, dict) or len(updates) == 0:
            return JSONResponse({"error": "tools must be a non-empty dict"}, status_code=400)
        save_enabled_tools(updates)
        return {"ok": True, "updated": list(updates.keys())}

    @router.get("/api/settings/config")
    async def api_get_config():
        return _build_config()

    @router.put("/api/settings/config")
    async def api_update_config(request: Request):
        from cyrene.settings_store import set_ as set_setting
        body = await request.json()
        changed = []
        if "spawn_policy" in body:
            value = str(body.get("spawn_policy") or "").strip().lower()
            if value not in {"aggressive", "conservative", "off"}:
                return JSONResponse({"error": "invalid spawn_policy"}, status_code=400)
            set_setting("spawn_policy", value)
            changed.append("spawn_policy")
        if "heartbeat_interval" in body:
            value = int(body.get("heartbeat_interval") or 0)
            if value < 60:
                return JSONResponse({"error": "heartbeat_interval must be at least 60"}, status_code=400)
            set_setting("heartbeat_interval", value)
            changed.append("heartbeat_interval")
        if "agent_proactive" in body:
            set_setting("agent_proactive", bool(body["agent_proactive"]))
            changed.append("agent_proactive")
        if "max_tool_rounds" in body:
            value = int(body.get("max_tool_rounds") or 15)
            if value < 5 or value > 200:
                return JSONResponse({"error": "max_tool_rounds must be between 5 and 200"}, status_code=400)
            set_setting("max_tool_rounds", value)
            changed.append("max_tool_rounds")
        if "notify_telegram" in body:
            set_setting("notify_telegram", bool(body["notify_telegram"]))
            changed.append("notify_telegram")
        if "notify_wechat" in body:
            set_setting("notify_wechat", bool(body["notify_wechat"]))
            changed.append("notify_wechat")
        if "redact_secrets" in body:
            set_setting("redact_secrets", bool(body["redact_secrets"]))
            changed.append("redact_secrets")
        return {"ok": True, "changed": changed}

    @router.post("/api/settings/reset-data")
    async def api_reset_data():
        return await _reset_app_data()

    @router.get("/api/settings/search")
    async def api_get_search():
        return {"search": _build_search_config()}

    @router.put("/api/settings/search")
    async def api_update_search(request: Request):
        from cyrene.settings_store import set_ as set_setting
        await request.json()
        set_setting("search_mode", "builtin")
        set_setting("search_external_url", "")
        return {"ok": True, "changed": ["search_mode", "search_external_url"]}

    # ---- MCP Servers API ----

    @router.get("/api/settings/mcp")
    async def api_get_mcp_servers():
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr, get_mcp_servers as _get_servers
        manager = _get_mcp_mgr()
        return {
            "servers": manager.get_server_status(),
            "configs": _get_servers(),
        }

    @router.put("/api/settings/mcp")
    async def api_update_mcp_servers(request: Request):
        from cyrene.mcp_manager import save_mcp_servers as _save_servers, restart_mcp as _restart_mcp
        body = await request.json()
        servers = body.get("servers", [])
        _save_servers(servers)
        await _restart_mcp()
        return {"ok": True}

    # ---- Workbench projects / task sessions ----

    @router.get("/api/projects")
    async def api_workbench_projects():
        return _read_workbench_store()

    @router.get("/api/workbench/notifications")
    async def api_workbench_notifications(tab: str = "all", limit: int = 80):
        return list_notifications(tab=tab, limit=limit)

    @router.post("/api/workbench/notifications/read")
    async def api_workbench_notifications_read(request: Request):
        body = await request.json()
        ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        mark_all = bool(body.get("markAll"))
        result = mark_notifications_read(ids, mark_all=mark_all)
        return {**result, **list_notifications(limit=80)}

    @router.patch("/api/workbench/activate")
    async def api_workbench_activate(request: Request):
        body = await request.json()
        payload = _read_workbench_store()
        pid = str(body.get("projectId") or "").strip()
        if pid:
            payload["activeProjectId"] = pid
        sid = str(body.get("sessionId") or "").strip()
        if sid:
            payload["activeSessionId"] = sid
        _write_workbench_store(payload)
        return {"ok": True, **payload}

    @router.post("/api/projects")
    async def api_workbench_create_project(request: Request):
        body = await request.json()
        payload = _read_workbench_store()
        now = _utc_now_iso()
        workspace_path = str(body.get("workspacePath") or body.get("workspace_path") or WORKSPACE_DIR)
        name = str(body.get("name") or Path(workspace_path).name or "New Project").strip()
        description = str(body.get("description") or "").strip()
        project_id = _short_id("project")
        project = {
            "id": project_id,
            "name": name,
            "dataKey": _safe_workbench_data_key(project_id),
            "description": description,
            "icon": str(body.get("icon") or "spark").strip() or "spark",
            "color": str(body.get("color") or "").strip(),
            "template": str(body.get("template") or "blank").strip() or "blank",
            "workspacePath": workspace_path,
            "status": "active",
            "model": _get_model(),
            "accountTier": str(body.get("accountTier") or "Pro"),
            "context": {
                "summary": str(body.get("summary") or description or f"Workspace at {workspace_path}"),
                "stack": body.get("stack") if isinstance(body.get("stack"), list) else [],
                "decisions": [],
                "knowledgeDocumentIds": [],
            },
            "createdAt": now,
            "updatedAt": now,
            "sessions": [],
            "sharedArtifacts": [],
        }
        # New projects open onto an agent-led "初始化项目" onboarding session.
        initial_session = _workbench_new_init_session(project_id, project, now)
        project["sessions"] = [initial_session]
        payload.setdefault("projects", []).insert(0, project)
        payload["activeProjectId"] = project_id
        payload["activeSessionId"] = initial_session["id"]
        _write_workbench_store(payload)
        append_notification(
            title="项目创建完成",
            body=f"已创建 workspace「{name}」。",
            tab="system",
            project_ref=project_id,
            source="project_created",
            source_label="Workspace",
            link_label=name,
        )
        return {"ok": True, "project": project, "session": initial_session, **payload}

    @router.patch("/api/projects/{project_id}")
    async def api_workbench_update_project(project_id: str, request: Request):
        body = await request.json()
        payload = _read_workbench_store()
        project = _workbench_find_project(payload, project_id)
        if not project:
            return JSONResponse({"error": "project not found"}, status_code=404)
        for field in ("name", "description", "icon", "color", "template", "workspacePath", "status", "model", "accountTier"):
            if field in body:
                project[field] = body[field]
        if isinstance(body.get("context"), dict):
            project["context"] = {**(project.get("context") or {}), **body["context"]}
        project["updatedAt"] = _utc_now_iso()
        _write_workbench_store(payload)
        return {"ok": True, "project": project, **payload}

    @router.delete("/api/projects/{project_id}")
    async def api_workbench_delete_project(project_id: str):
        payload = _read_workbench_store()
        projects = payload.get("projects", [])
        # Collect session IDs before filtering so we can clean up agent state
        doomed_project = next((p for p in projects if str(p.get("id") or "") == project_id), None)
        if doomed_project:
            doomed_data_key = _workbench_project_data_key(doomed_project)
            for s in (doomed_project.get("sessions") or []):
                sid = str(s.get("id") or "").strip()
                if sid:
                    await clear_session_id(session_id=sid)
            # Also drop the project's workbench conversations (chat-kind sessions).
            try:
                from webui.routes_workbench_chat import remove_project_chats
                await remove_project_chats(project_id)
            except Exception:
                logger.exception("Failed to remove chats for project %s", project_id)
            if doomed_data_key != _WORKBENCH_LEGACY_DATA_KEY:
                try:
                    from cyrene.config import get_knowledge_db_path, STORE_DIR
                    _remove_path(get_knowledge_db_path(doomed_data_key))
                    _remove_path(STORE_DIR / f"wb_memory_{doomed_data_key}.json")
                    import aiosqlite
                    async with aiosqlite.connect(_db_path) as db:
                        await db.execute(
                            "DELETE FROM scheduled_tasks WHERE COALESCE(project_id, 'default') = ?",
                            (doomed_data_key,),
                        )
                        await db.commit()
                except Exception:
                    logger.exception("Failed to remove project-scoped data for %s", project_id)
        next_projects = [project for project in projects if str(project.get("id") or "") != project_id]
        if len(next_projects) == len(projects):
            return JSONResponse({"error": "project not found"}, status_code=404)
        payload["projects"] = next_projects
        if not next_projects:
            payload = _workbench_default_project()
        else:
            payload["activeProjectId"] = next_projects[0].get("id")
            sessions = next_projects[0].get("sessions") or []
            payload["activeSessionId"] = sessions[0].get("id") if sessions else ""
        _write_workbench_store(payload)
        return {"ok": True, **payload}

    @router.get("/api/projects/{project_id}/sessions")
    async def api_workbench_project_sessions(project_id: str):
        payload = _read_workbench_store()
        project = _workbench_find_project(payload, project_id)
        if not project:
            return JSONResponse({"error": "project not found"}, status_code=404)
        return {"sessions": project.get("sessions", [])}

    @router.post("/api/projects/{project_id}/sessions")
    async def api_workbench_create_session(project_id: str, request: Request):
        body = await request.json()
        payload = _read_workbench_store()
        project = _workbench_find_project(payload, project_id)
        if not project:
            return JSONResponse({"error": "project not found"}, status_code=404)
        title = str(body.get("title") or body.get("goal") or "新任务").strip() or "新任务"
        session = _workbench_new_session(project_id, title, str(body.get("goal") or "").strip())
        if str(body.get("priority") or "").strip() in ("high", "medium", "low"):
            session["priority"] = str(body.get("priority")).strip()
        project.setdefault("sessions", []).insert(0, session)
        project["updatedAt"] = session["createdAt"]
        payload["activeProjectId"] = project_id
        payload["activeSessionId"] = session["id"]
        _write_workbench_store(payload)
        append_notification(
            title="新任务已创建",
            body=f"任务「{title}」已加入 {project.get('name') or 'workspace'}。",
            tab="comment",
            project_ref=project_id,
            source="task_created",
            source_label="任务",
            link_label=title,
            meta={"sessionId": session["id"]},
        )
        return {"ok": True, "session": session, **payload}

    @router.post("/api/projects/{project_id}/init/generate")
    async def api_workbench_generate_init(project_id: str, request: Request):
        """(Re)generate the onboarding questions for a project's init session.

        Runs the agent against the project's metadata and workspace files; on
        any failure it keeps the deterministic fallback form. Either way the
        form is marked as ``generated`` so the client only requests this once.
        """
        body = await request.json()
        lang = str(body.get("lang") or "").strip()
        payload = _read_workbench_store()
        project = _workbench_find_project(payload, project_id)
        if not project:
            return JSONResponse({"error": "project not found"}, status_code=404)
        session = next(
            (s for s in project.get("sessions", []) if str(s.get("kind") or "") == "init"),
            None,
        )
        if not session:
            return JSONResponse({"error": "init session not found"}, status_code=404)
        current = session.get("init") if isinstance(session.get("init"), dict) else _workbench_default_init_form(project)
        generated = await _workbench_generate_init_form(project, lang=lang)
        if generated:
            # Preserve any answers the user already entered.
            generated["answers"] = current.get("answers") if isinstance(current.get("answers"), dict) else {}
            generated["completed"] = bool(current.get("completed"))
            session["init"] = generated
            session["agentReply"] = generated.get("greeting") or session.get("agentReply") or ""
        else:
            # Generation failed (LLM error / unparseable output). Keep the
            # deterministic fallback but DON'T mark it generated — the client
            # guards re-entry per mount (genRef), so leaving generated=False lets
            # it self-heal on the next open instead of permanently sticking the
            # generic form. The user can also press 重新生成问题 to retry now.
            current["generated"] = False
            session["init"] = current
        now = _utc_now_iso()
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session.get("id")
        _write_workbench_store(payload)
        return {"ok": True, "project": project, "session": session, **payload}

    @router.get("/api/task-sessions/{session_id}")
    async def api_workbench_get_session(session_id: str):
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return {"project": project, "session": session}

    @router.get("/api/task-sessions/{session_id}/files/diff")
    async def api_workbench_file_diff(session_id: str, path: str = ""):
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        try:
            result = await _workbench_git_diff_for_path(_workbench_workspace_root(project), path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except TimeoutError as exc:
            return JSONResponse({"error": str(exc)}, status_code=504)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return result

    @router.get("/api/task-sessions/{session_id}/workspace/exists")
    async def api_workbench_workspace_exists(session_id: str, path: str = ""):
        """Validate a context-file path for the per-step '相关文件' editor: confirm
        it resolves INSIDE the project workspace and exists. Returns the workspace-
        relative path so the client stores a normalized reference."""
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        root = _workbench_workspace_root(project)
        if not root:
            return JSONResponse({"error": "no workspace configured"}, status_code=400)
        raw = str(path or "").strip()
        if not raw:
            return {"exists": False, "path": "", "isDir": False}
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            rel = resolved.relative_to(root).as_posix()
        except (ValueError, OSError):
            return JSONResponse({"exists": False, "path": raw, "error": "路径不在工作区内"}, status_code=400)
        exists = resolved.exists()
        return {"exists": exists, "path": rel, "isDir": resolved.is_dir() if exists else False}

    @router.patch("/api/task-sessions/{session_id}")
    async def api_workbench_update_session(session_id: str, request: Request):
        body = await request.json()
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        prev_status = str(session.get("status") or "")
        for field in ("title", "goal", "status", "priority", "agentReply", "summary", "kind"):
            if field in body:
                session[field] = body[field]
        for field in ("constraints", "plan", "events", "runs", "artifacts", "acceptanceCriteria"):
            if isinstance(body.get(field), list):
                session[field] = body[field]
        if isinstance(body.get("init"), dict):
            session["init"] = {**(session.get("init") or {}), **body["init"]}
        now = _utc_now_iso()
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        next_status = str(session.get("status") or "")
        if next_status != prev_status and next_status in ("done", "completed", "failed", "blocked", "paused", "review"):
            status_titles = {
                "done": "任务完成",
                "completed": "任务完成",
                "failed": "任务失败",
                "blocked": "任务阻塞",
                "paused": "任务已暂停",
                "review": "任务待验收",
            }
            status_labels = {
                "done": "已完成",
                "completed": "已完成",
                "failed": "失败",
                "blocked": "阻塞",
                "paused": "已暂停",
                "review": "待验收",
            }
            append_notification(
                title=status_titles.get(next_status, "任务状态更新"),
                body=f"任务「{session.get('title') or '未命名任务'}」当前状态：{status_labels.get(next_status, next_status)}。",
                tab="system" if next_status != "review" else "comment",
                project_ref=project.get("id"),
                source="task_status",
                source_label="任务",
                link_label=str(session.get("title") or ""),
                meta={"sessionId": session_id, "status": next_status},
            )
        return {"ok": True, "project": project, "session": session, **payload}

    @router.delete("/api/task-sessions/{session_id}")
    async def api_workbench_delete_session(session_id: str):
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        project["sessions"] = [s for s in project.get("sessions", []) if str(s.get("id") or "") != session_id]
        now = _utc_now_iso()
        project["updatedAt"] = now
        if str(payload.get("activeSessionId") or "") == session_id:
            remaining = project.get("sessions") or []
            payload["activeSessionId"] = remaining[0]["id"] if remaining else ""
        _write_workbench_store(payload)
        return {"ok": True, **payload}

    @router.post("/api/task-sessions/{session_id}/plan/generate")
    async def api_workbench_generate_plan(session_id: str, request: Request):
        """Generate a REAL execution plan for a task session.

        The agent reads the session goal + constraints and explores the project
        workspace, then returns ordered steps (all ``pending`` — nothing is run
        or pre-completed here). Drives the idle → planning transition.
        """
        body = await request.json()
        goal = str(body.get("goal") or "").strip()
        feedback = str(body.get("feedback") or "").strip()
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)

        if goal:
            session["goal"] = goal
            merged = list(session.get("constraints") or [])
            for item in _workbench_extract_constraints(goal):
                if item not in merged:
                    merged.append(item)
            session["constraints"] = merged

        steps, from_llm = await _workbench_generate_plan_steps(session, project, feedback=feedback)
        session["plan"] = steps
        session["acceptanceCriteria"] = _workbench_acceptance_from_session(session)
        session["status"] = "planning"
        if from_llm:
            session["agentReply"] = "我已结合工作区里的实际内容拆解出执行计划。你可以直接编辑，或逐步执行（顺序由你决定）。"
        else:
            session["agentReply"] = "计划生成服务暂时不可用，我先给出一份基础计划，你可以编辑后逐步执行，或稍后让我重新拆解。"
        now = _utc_now_iso()
        session["events"] = list(session.get("events") or []) + [{
            "id": _short_id("event"),
            "type": "PlanGenerated",
            "createdAt": now,
            "body": f"生成执行计划，共 {len(steps)} 步。" + ("" if from_llm else "（兜底计划）"),
        }]
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        return {"ok": True, "project": project, "session": session, "planSource": "llm" if from_llm else "fallback", **payload}

    @router.post("/api/task-sessions/{session_id}/runs")
    async def api_workbench_create_run(session_id: str, request: Request):
        body = await request.json()
        user_input = str(body.get("input") or body.get("message") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        mode = str(body.get("mode") or "auto")
        command = str(body.get("command") or "")
        if not user_input and not attachments:
            return JSONResponse({"error": "input is required"}, status_code=400)
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)

        # A per-step run (from runStep) executes one already-planned step — it must
        # NOT rebuild the plan / acceptance / goal / status; the client drives those.
        step_id = str(body.get("stepId") or "").strip()
        action = str(body.get("action") or "").strip()
        is_step_run = bool(step_id) or action == "spawn_subagent"

        now = _utc_now_iso()
        if not is_step_run:
            constraints = _workbench_extract_constraints(user_input)
            merged_constraints = list(session.get("constraints") or [])
            for item in constraints:
                if item not in merged_constraints:
                    merged_constraints.append(item)
            if not session.get("goal") or session.get("status") == "idle":
                session["goal"] = user_input
            session["constraints"] = merged_constraints
            session["plan"] = _workbench_plan_from_input(user_input, session)
            session["acceptanceCriteria"] = _workbench_acceptance_from_session(session)
        else:
            constraints = []
        run_start_ts = _utc_now_iso()
        workspace_root = _workbench_workspace_root(project)
        git_status_before = _workbench_git_status_snapshot(workspace_root)
        agent_reply = await _workbench_agent_reply(user_input, session, constraints, attachments=attachments, permission_mode=mode, command=command, project_workspace=str(project.get("workspacePath") or ""))
        git_status_after = _workbench_git_status_snapshot(workspace_root)
        session["agentReply"] = agent_reply
        # Sink durable memories from this exchange into the project's workspace store.
        if not command:
            schedule_capture(_workbench_project_data_key(project), user_input, agent_reply)
        if not is_step_run:
            session["status"] = "planning" if session.get("status") in ("idle", "pending") else session.get("status", "planning")
        session["updatedAt"] = now
        project["updatedAt"] = now

        normalized_attachments = _workbench_normalize_attachments(attachments)
        public_attachments = [build_public_attachment_payload(item) for item in normalized_attachments]
        run_id = _short_id("run")
        activity_events = _collect_run_activity_events(session_id, run_start_ts, run_id, workspace_root)
        tool_call_events = [event for event in activity_events if event.get("type") == "ToolCallEvent"]
        file_changes = _workbench_merge_file_changes([
            *[change for event in tool_call_events for change in (event.get("fileChanges") or [])],
            *_workbench_git_status_delta(git_status_before, git_status_after, workspace_root),
        ])
        if is_step_run and step_id:
            _workbench_apply_step_file_changes(session, step_id, file_changes)
        events = [
            {"id": _short_id("event"), "type": "UserMessageEvent", "runId": run_id, "createdAt": now, "body": user_input or "[附件]", "attachments": public_attachments},
            *activity_events,
            {"id": _short_id("event"), "type": "AgentResponseEvent", "runId": run_id, "createdAt": now, "body": agent_reply},
            {"id": _short_id("event"), "type": "PlanUpdatedEvent", "runId": run_id, "createdAt": now, "stepCount": len(session.get("plan") or [])},
        ]
        run = {
            "id": run_id,
            "taskId": session_id,
            "userInput": user_input,
            "agentResponse": agent_reply,
            "status": "completed",
            "startedAt": now,
            "endedAt": now,
            "contextPackId": _short_id("ctx"),
            "events": events,
            "fileChanges": file_changes,
            "toolCalls": [{"tool": e["tool"], "argsPreview": e["argsPreview"]} for e in tool_call_events],
            "artifacts": [],
            "attachments": public_attachments,
            "mode": mode,
            "error": None,
        }
        session.setdefault("runs", []).append(run)
        session.setdefault("events", []).extend(events)
        if not session.get("artifacts"):
            session["artifacts"] = [
                {
                    "id": _short_id("artifact"),
                    "type": "task_brief",
                    "name": "task-brief.md",
                    "status": "draft",
                    "createdAt": now,
                    "summary": "任务目标、约束、计划和验收标准的结构化记录。",
                }
            ]
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        append_notification(
            title="任务回复完成",
            body=f"Agent 已更新任务「{session.get('title') or '未命名任务'}」。",
            tab="comment",
            project_ref=project.get("id"),
            source="task_reply",
            source_label="任务",
            link_label=str(session.get("title") or ""),
            meta={"sessionId": session_id, "runId": run_id},
        )
        return {"ok": True, "project": project, "session": session, "run": run, **payload}

    @router.post("/api/task-sessions/{session_id}/chat")
    async def api_workbench_session_chat(session_id: str, request: Request):
        """Simple chat mode — returns agent reply without generating plans/steps."""
        body = await request.json()
        message = str(body.get("message") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        mode = str(body.get("mode") or "auto")
        command = str(body.get("command") or "")
        if not message and not attachments:
            return JSONResponse({"error": "message is required"}, status_code=400)
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        chat_run_start_ts = _utc_now_iso()
        agent_reply = await _workbench_agent_reply(message, session, [], attachments=attachments, permission_mode=mode, command=command, project_workspace=str(project.get("workspacePath") or ""))
        session["agentReply"] = agent_reply
        # Sink durable memories from this exchange into the project's workspace store.
        if not command:
            schedule_capture(_workbench_project_data_key(project), message, agent_reply)
        session["status"] = "completed"
        now = _utc_now_iso()
        session["updatedAt"] = now
        project["updatedAt"] = now
        chat_run_id = _short_id("run")
        chat_tool_events = _collect_run_tool_events(session_id, chat_run_start_ts, chat_run_id)
        if chat_tool_events:
            session.setdefault("events", []).extend(chat_tool_events)
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        append_notification(
            title="Agent 回复完成",
            body=f"Agent 在「{session.get('title') or '对话'}」中回复了你。",
            tab="mention",
            project_ref=project.get("id"),
            source="chat_reply",
            source_label="对话",
            link_label=str(session.get("title") or ""),
            meta={"sessionId": session_id},
        )
        return {"ok": True, "project": project, "session": session, **payload}

    @router.post("/api/task-sessions/{session_id}/init/submit")
    async def api_workbench_submit_init(session_id: str, request: Request):
        """Finalize project initialization.

        Persists the onboarding answers, writes a project brief into the project
        context, and asks the initialization agent to draft the major task plan.
        Confirming that plan is a separate step that creates task sessions.
        """
        body = await request.json()
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if str(session.get("kind") or "") != "init":
            return JSONResponse({"error": "not an init session"}, status_code=400)
        init_state = session.get("init") if isinstance(session.get("init"), dict) else {}
        if bool(init_state.get("completed")):
            return JSONResponse({"error": "init already completed"}, status_code=409)
        form = session.get("init") if isinstance(session.get("init"), dict) else _workbench_default_init_form(project)
        if isinstance(body.get("answers"), dict):
            merged = form.get("answers") if isinstance(form.get("answers"), dict) else {}
            merged.update(body["answers"])
            form["answers"] = merged

        brief = _workbench_init_brief(project, form)
        answers = form.get("answers") if isinstance(form.get("answers"), dict) else {}
        goal = str(answers.get("goal") or "").strip()
        now = _utc_now_iso()
        # Fold the onboarding into the project's durable context.
        context = project.get("context") if isinstance(project.get("context"), dict) else {}
        if brief:
            context["summary"] = brief
        project["context"] = context
        if not str(project.get("description") or "").strip() and goal:
            project["description"] = goal[:200]
        task_plan, plan_from_llm = await _workbench_generate_init_task_plan(project, form)
        form["taskPlan"] = task_plan
        form["planReady"] = True
        form["completed"] = False
        form["planSource"] = "llm" if plan_from_llm else "fallback"
        session["init"] = form
        session["status"] = "waiting_for_user"
        if plan_from_llm:
            session["agentReply"] = "我已根据你的初始化回答拆解出大任务计划。你可以直接编辑，或继续告诉我如何调整；确认后我会把每个大任务创建为独立 session。"
        else:
            session["agentReply"] = "计划生成服务暂时不可用，我先按你的回答生成了一份基础计划。你可以直接编辑后确认，或稍后让我重新拆解。"
        session["summary"] = brief or session.get("summary")
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        return {"ok": True, "project": project, "session": session, **payload}

    @router.post("/api/task-sessions/{session_id}/init/plan")
    async def api_workbench_revise_init_plan(session_id: str, request: Request):
        """Revise the initialization task plan from user feedback."""
        body = await request.json()
        feedback = str(body.get("feedback") or body.get("message") or "").strip()
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if str(session.get("kind") or "") != "init":
            return JSONResponse({"error": "not an init session"}, status_code=400)
        form = session.get("init") if isinstance(session.get("init"), dict) else _workbench_default_init_form(project)
        if bool(form.get("completed")):
            return JSONResponse({"error": "init already completed"}, status_code=409)
        # The client sends the plan currently on screen (incl. manual edits) so
        # the agent adjusts THAT plan; fall back to the persisted one.
        incoming_plan = body.get("taskPlan") if isinstance(body.get("taskPlan"), list) else None
        current_plan = _workbench_coerce_init_task_plan(incoming_plan, []) if incoming_plan else None
        if not current_plan:
            existing = form.get("taskPlan")
            current_plan = existing if isinstance(existing, list) and existing else None
        task_plan, plan_from_llm = await _workbench_generate_init_task_plan(
            project, form, feedback=feedback, current_plan=current_plan,
        )
        if plan_from_llm:
            form["taskPlan"] = task_plan
            form["planSource"] = "llm"
            session["agentReply"] = "我已按你的反馈更新任务计划。你可以继续修改，或确认创建 sessions。"
        else:
            # 生成失败时保留当前计划——直接用兜底计划覆盖会吞掉用户反馈，
            # 还会把之前 LLM 生成的计划（或用户的手动编辑）替换成模板。
            if current_plan:
                form["taskPlan"] = current_plan
            elif not (isinstance(form.get("taskPlan"), list) and form.get("taskPlan")):
                form["taskPlan"] = task_plan
                form["planSource"] = "fallback"
            session["agentReply"] = "计划生成服务暂时不可用，这次的反馈还没有应用，当前计划保持不变。你可以稍后重试，或直接手动编辑计划。"
        form["planReady"] = True
        session["init"] = form
        session["status"] = "waiting_for_user"
        now = _utc_now_iso()
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = session_id
        _write_workbench_store(payload)
        return {"ok": True, "project": project, "session": session, **payload}

    @router.post("/api/task-sessions/{session_id}/init/confirm")
    async def api_workbench_confirm_init_plan(session_id: str, request: Request):
        """Create task sessions from the confirmed initialization plan."""
        body = await request.json()
        payload = _read_workbench_store()
        project, session = _workbench_find_session(payload, session_id)
        if not session or not project:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if str(session.get("kind") or "") != "init":
            return JSONResponse({"error": "not an init session"}, status_code=400)
        form = session.get("init") if isinstance(session.get("init"), dict) else _workbench_default_init_form(project)
        if bool(form.get("completed")):
            existing_ids = form.get("createdSessionIds") if isinstance(form.get("createdSessionIds"), list) else []
            existing = [
                s for s in project.get("sessions", [])
                if str(s.get("id") or "") in {str(item) for item in existing_ids}
            ]
            return {"ok": True, "project": project, "session": existing[0] if existing else session, "initSession": session, "createdSessions": existing, **payload}
        incoming_plan = body.get("taskPlan") if isinstance(body.get("taskPlan"), list) else form.get("taskPlan")
        fallback = _workbench_fallback_init_task_plan(project, form)
        task_plan = _workbench_coerce_init_task_plan(incoming_plan, fallback)
        if not task_plan:
            return JSONResponse({"error": "task plan is empty"}, status_code=400)
        now = _utc_now_iso()
        created = _workbench_create_sessions_from_init_plan(project, task_plan, now)
        if not created:
            return JSONResponse({"error": "no sessions created"}, status_code=400)
        form["taskPlan"] = task_plan
        form["planReady"] = True
        form["completed"] = True
        form["createdSessionIds"] = [item["id"] for item in created]
        session["init"] = form
        session["status"] = "completed"
        session["agentReply"] = f"初始化已完成。我已根据确认后的计划创建 {len(created)} 个任务 session。"
        session["updatedAt"] = now
        project["updatedAt"] = now
        payload["activeProjectId"] = project.get("id")
        payload["activeSessionId"] = created[0]["id"]
        _write_workbench_store(payload)
        append_notification(
            title="初始化任务已生成",
            body=f"{project.get('name') or 'Workspace'} 已创建 {len(created)} 个任务 session。",
            tab="system",
            project_ref=project.get("id"),
            source="init_confirmed",
            source_label="系统",
            link_label=str(project.get("name") or ""),
            meta={"createdSessionIds": [item["id"] for item in created]},
        )
        return {"ok": True, "project": project, "session": created[0], "initSession": session, "createdSessions": created, **payload}

    @router.get("/api/task-sessions/{session_id}/events")
    async def api_workbench_session_events(session_id: str):
        payload = _read_workbench_store()
        _project, session = _workbench_find_session(payload, session_id)
        if not session:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return {"events": session.get("events", [])}

    @router.get("/api/task-sessions/{session_id}/artifacts")
    async def api_workbench_session_artifacts(session_id: str):
        payload = _read_workbench_store()
        _project, session = _workbench_find_session(payload, session_id)
        if not session:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return {"artifacts": session.get("artifacts", [])}

    # ---- Scheduled tasks ----

    @router.get("/api/tasks")
    async def api_list_tasks():
        from cyrene import db as cy_db
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"tasks": tasks}

    @router.post("/api/tasks")
    async def api_create_task(request: Request):
        from cyrene import db as cy_db
        from cyrene.schedule_spec import compute_next_run
        body = await request.json()
        stype = body["schedule_type"]
        svalue = body["schedule_value"]
        # REST API 端不允许创建 full_access 任务 ——
        # 用户需通过 chat agent 的 schedule_task 工具创建（会弹出确认对话框）
        permission_mode = "workspace_only"

        # Compute next_run if not provided by the frontend. An invalid schedule
        # is a 400 — never silently schedule for "now".
        next_run = body.get("next_run", "")
        if not next_run:
            try:
                next_run = compute_next_run(stype, svalue)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        task_id = await cy_db.create_task(
            _db_path,
            chat_id=body.get("chat_id", _CHAT_ID),
            prompt=body["prompt"],
            schedule_type=stype,
            schedule_value=svalue,
            next_run=next_run,
            permission_mode=permission_mode,
        )
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "id": task_id, "tasks": tasks}

    @router.put("/api/tasks/{task_id}")
    async def api_update_task(task_id: str, request: Request):
        from cyrene import db as cy_db
        from cyrene.schedule_spec import compute_next_run
        body = await request.json()
        # Build SET clause dynamically from provided fields
        sets = []
        vals = []

        # If schedule_type or schedule_value changed, recalculate next_run.
        # An invalid schedule is a 400 rather than a silently-dropped update.
        stype = body.get("schedule_type")
        svalue = body.get("schedule_value")
        if stype and svalue and "next_run" not in body:
            try:
                body["next_run"] = compute_next_run(stype, svalue)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        # permission_mode 不可通过 REST API 修改 ——
        # 需通过 chat agent 的 schedule_task 工具重新创建（会弹出确认对话框）
        for field in ("prompt", "schedule_type", "schedule_value", "next_run", "status"):
            if field in body:
                sets.append(f"{field} = ?")
                vals.append(body[field])
        if sets:
            import aiosqlite
            async with aiosqlite.connect(_db_path) as db:
                await db.execute(
                    f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
                    (*vals, task_id),
                )
                await db.commit()
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "tasks": tasks}

    @router.delete("/api/tasks/{task_id}")
    async def api_delete_task(task_id: str):
        from cyrene import db as cy_db
        await cy_db.delete_task(_db_path, task_id)
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "tasks": tasks}

    @router.post("/api/shutdown")
    async def api_shutdown():
        """Shutdown the daemon."""
        import os as _os
        _os._exit(0)

    # ---- Update checker ----

    @router.get("/api/update/check")
    async def api_update_check():
        """Check for updates via GitHub Releases."""
        from cyrene.updater import check_for_update, set_cached_update_info

        info = await check_for_update()
        set_cached_update_info(info)

        return {
            "update_available": info.available,
            "current_version": info.current_version,
            "latest_version": info.latest_version,
            "download_url": info.download_url,
            "release_notes": info.release_notes,
            "asset_name": info.asset_name,
            "asset_size": info.asset_size,
        }

    @router.post("/api/update/download")
    async def api_update_download():
        """下载更新包。返回下载状态。"""
        from cyrene.updater import (
            get_cached_update_info,
            download_update,
            _download_progress,
        )

        info = get_cached_update_info()
        if not info or not info.download_url:
            return {"ok": False, "error": "No update available"}

        def _progress(downloaded: int, total: int) -> None:
            _download_progress["downloaded"] = downloaded
            _download_progress["total"] = total

        _download_progress["downloaded"] = 0
        _download_progress["total"] = info.asset_size
        _download_progress["done"] = False

        dest = await download_update(info.download_url, _progress)
        _download_progress["done"] = True
        _download_progress["path"] = str(dest) if dest else ""

        if dest:
            return {
                "ok": True,
                "path": str(dest),
                "size": _download_progress["downloaded"],
            }
        return {"ok": False, "error": "Download failed"}

    @router.get("/api/update/progress")
    async def api_update_progress():
        """查询下载进度。"""
        from cyrene.updater import get_download_progress
        return get_download_progress()

    @router.post("/api/update/restart")
    async def api_update_restart():
        """写入重启脚本并退出进程（安装更新后调用）。

        无论更新文件是否存在，都通过退出码 42 通知 Electron 重启，
        避免因提前返回导致进程继续运行、关闭时误弹"崩溃"对话框。
        """
        from cyrene.updater import get_restart_script, _download_progress
        import subprocess as _sp

        dest_str = _download_progress.get("path", "")
        if dest_str:
            dest = Path(dest_str)
            if dest.exists():
                try:
                    script = get_restart_script(dest)
                    if sys.platform == "win32":
                        script_path = dest.parent / "update.bat"
                        script_path.write_text(script)
                        _sp.Popen(
                            ["cmd", "/c", str(script_path)],
                            creationflags=(
                                0x00000200 |  # CREATE_NEW_PROCESS_GROUP
                                0x00000008    # DETACHED_PROCESS
                            ),
                        )
                    else:
                        script_path = dest.parent / "update.sh"
                        script_path.write_text(script)
                        script_path.chmod(0o755)
                        _sp.Popen(
                            ["bash", str(script_path)], start_new_session=True
                        )
                except Exception:
                    logger.warning(
                        "Failed to spawn updater script", exc_info=True
                    )
            else:
                logger.warning("Update file vanished: %s", dest)
        else:
            logger.warning("Restart called but no downloaded update found")

        # 始终用退出码 42 退出，通知 Electron 释放 single-instance lock
        import os as _os
        _os._exit(42)

    app.include_router(router)


# ---------------------------------------------------------------------------
# UI data builders
# ---------------------------------------------------------------------------


def _resolve_ui_tz(tz_name: str = ""):
    name = str(tz_name or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


async def _build_ui_data(tz_name: str = "") -> dict:
    """Assemble the full DATA payload the SPA expects."""
    sessions = _build_sessions()
    if not sessions:
        sessions = [_empty_session()]
    ui_tz = _resolve_ui_tz(tz_name)
    return {
        "user": _build_user(),
        "assistantName": ASSISTANT_NAME,
        "appVersion": get_version_label(),
        "dashboard": await _build_dashboard(ui_tz),
        "sessions": sessions,
        "status": await _build_status(),
        "skills": _build_skills(),
        "settings": _build_settings_meta(),
        "onboarding": get_onboarding_status(),
        "entities": await _build_entities_summary(),
    }


async def _build_entities_summary() -> list:
    """Return active entities for the SPA bootstrap payload."""
    try:
        from cyrene.entities import list_entities
        return await list_entities(_db_path, status="active", limit=100)
    except Exception:
        logger.exception("Failed to build entities summary")
        return []


def _build_user() -> dict:
    """User identity from environment or workspace owner."""
    name = _resolve_local_username()
    handle = re.sub(r"[^a-z0-9._-]+", "", name.lower().replace(" ", "")) or "user"
    parts = [part for part in re.split(r"[\s._-]+", name) if part]
    initials = "".join(part[0].upper() for part in parts[:2]) or name[:2].upper() or "U"
    return {"name": name, "handle": handle, "initials": initials}


def _resolve_local_username() -> str:
    """Best-effort local account name for the current machine."""
    candidates = [
        os.environ.get("USER"),
        os.environ.get("USERNAME"),
        os.environ.get("LOGNAME"),
    ]
    try:
        candidates.append(getpass.getuser())
    except Exception:
        pass

    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()

    return "user"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


# Per-session CC preview cache — archived sessions keep their initial snapshot
_cc_preview_cache: dict[str, list] = {}

def _build_sessions() -> list[dict]:
    """Build session list — current state.json + parsed conversation archives."""
    sessions: list[dict] = []

    # 1. Current active session from state.json
    current = _build_current_session()
    if current:
        sessions.append(current)

    # 2. Historical sessions from conversation archives (one per day, most recent first)
    skip_archive_ids: set[str] = set()
    current_archive_session_id = str(current.get("archiveSessionId", "")).strip() if current else ""
    current_archive_date = str(current.get("archiveDate", "")).strip() if current else ""
    if current_archive_session_id and current_archive_date:
        skip_archive_ids.add(f"{current_archive_date}:{current_archive_session_id}")

    archive_sessions = _build_archive_sessions(skip_archive_ids=skip_archive_ids)
    sessions.extend(archive_sessions)

    # Per-session CC preview: live session always fresh, archives use cached snapshot
    for session in sessions:
        sid = session["id"]
        for shell in session.get("shells", []):
            if shell.get("kind") == "cc":
                if sid == "run_live":
                    _cc_preview_cache[sid] = list(shell.get("lines", []))
                elif sid in _cc_preview_cache:
                    shell["lines"] = list(_cc_preview_cache[sid])
                else:
                    _cc_preview_cache[sid] = list(shell.get("lines", []))

    return sessions


def _build_summary(raw_msgs: list[dict]) -> dict:
    usage = _usage_totals(raw_msgs)
    return {
        "tokens": _format_tokens(usage),
        "spend": _calc_spend(usage),
        "toolCalls": _count_tool_calls(raw_msgs),
        "requests": usage["requests"],
        "total_tokens": usage["total_tokens"],
    }


def _ui_pending_question(raw_pending: Any) -> dict[str, Any] | None:
    if not isinstance(raw_pending, dict):
        return None
    question_id = str(raw_pending.get("id", "")).strip()
    text = str(raw_pending.get("text", "")).strip()
    if not question_id or not text:
        return None
    options_out = []
    raw_options = raw_pending.get("options", [])
    if isinstance(raw_options, list):
        for item in raw_options:
            if isinstance(item, dict):
                option_id = str(item.get("id", "")).strip()
                label = str(item.get("label", "")).strip()
            else:
                option_id = ""
                label = str(item or "").strip()
            if not label:
                continue
            options_out.append({
                "id": option_id or f"option_{len(options_out) + 1}",
                "label": label,
            })
    return {
        "id": question_id,
        "text": text,
        "askedAt": str(raw_pending.get("asked_at", "")).strip(),
        "roundId": str(raw_pending.get("round_id", "")).strip(),
        "roundTitle": str(raw_pending.get("round_title", "")).strip(),
        "clientRequestId": str(raw_pending.get("client_request_id", "")).strip(),
        "allowCustom": bool(raw_pending.get("allow_custom", True)),
        "hideAnswerInChat": bool(raw_pending.get("hide_answer_in_chat")),
        "kind": str((raw_pending.get("meta") or {}).get("kind", "")).strip(),
        "options": options_out,
    }


def _has_recent_main_agent_activity(recent: list[dict], now_ts: datetime) -> bool:
    """Return whether recent runtime events indicate an unfinished main-agent run."""
    cutoff_ts = now_ts - timedelta(seconds=30)
    active = False
    for event in recent:
        try:
            event_ts = datetime.fromisoformat(str(event.get("timestamp") or ""))
            if event_ts <= cutoff_ts:
                continue
        except (ValueError, TypeError):
            continue

        event_type = str(event.get("type") or "")
        if event_type == "session_update":
            active = str(event.get("status") or "") == "running"
            continue
        if event_type == "phase_transition":
            active = True
            continue
        if event_type in ("llm_call", "tool_call") and str(event.get("caller") or "") == "main_agent":
            active = True
    return active


def _build_current_session() -> dict | None:
    """Build a session object from state.json + live subagents.

    Always returns a run_live entry — when state.json is missing or empty,
    returns an empty placeholder so the Chat page shows a clean "start a new
    conversation" view instead of falling back to an old archive.
    """
    state: dict[str, Any] = {}
    raw_msgs: list[dict] = []
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
            raw_msgs = state.get("messages", []) or []
        except Exception:
            raw_msgs = []
            state = {}

    pending_question = _ui_pending_question(state.get("pending_question", {}))
    messages = _convert_messages(raw_msgs) if raw_msgs else []
    current_round_id = _latest_round_id_from_messages(raw_msgs)
    current_round_title = next(
        (
            str(msg.get("round_title", "")).strip()
            for msg in reversed(raw_msgs)
            if str(msg.get("round_id", "")).strip() == current_round_id and msg.get("round_title")
        ),
        "",
    )

    from cyrene.subagent import _registry  # noqa: WPS437
    subagent_registry = _infer_subagent_entries(raw_msgs, _registry)
    subagents = []
    for agent_id, info in subagent_registry.items():
        status = info.get("status", "running")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        subagents.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "roundId": str(info.get("round_id", "")).strip(),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })

    subagents.sort(key=lambda item: (item.get("createdAt") == "—", item.get("createdAt"), item["name"]))
    live_rounds = get_live_rounds()

    session_start = _session_started_at(raw_msgs)
    started_at = datetime.fromtimestamp(session_start, tz=timezone.utc).strftime("%H:%M")
    duration = _format_duration(time.time() - session_start)
    last_msg = messages[-1] if messages else None

    is_empty = not messages
    if live_rounds and any(str(item.get("status", "")) == "running" for item in live_rounds):
        live_status = "running"
    elif pending_question:
        live_status = "queued"
    elif live_rounds and any(int(item.get("pendingGuidance", 0) or 0) > 0 for item in live_rounds):
        live_status = "queued"
    elif is_empty:
        live_status = "idle"  # nothing happening yet — fresh session
    else:
        # Check if the main agent is actively processing (no live_rounds exist
        # during Phase 1/2 of the main agent loop)
        recent = debug.get_recent_events(200)
        now_ts = datetime.now(timezone.utc)
        if _has_recent_main_agent_activity(recent, now_ts):
            live_status = "running"
        else:
            live_status = "done"

    live_summary = _build_summary(raw_msgs)
    # Save main-agent-only total_tokens BEFORE merging subagent usage
    main_agent_total_tokens = live_summary.get("total_tokens")
    subagent_usage = _merge_usage_totals(*[
        _usage_totals(info.get("messages", []))
        for info in subagent_registry.values()
    ])
    combined_live_usage = _merge_usage_totals(_usage_totals(raw_msgs), subagent_usage)
    if combined_live_usage.get("requests") is not None:
        live_summary["requests"] = combined_live_usage.get("requests")
        live_summary["tokens"] = _format_tokens(combined_live_usage)
        live_summary["spend"] = _calc_spend(combined_live_usage)
        live_summary["toolCalls"] = live_summary["toolCalls"] + sum(
            _count_tool_calls(info.get("messages", []))
            for info in subagent_registry.values()
        )
        live_summary["total_tokens"] = combined_live_usage.get("total_tokens")

    # Set timestamp filter so CC preview only shows entries from this session
    set_cc_since(started_at)

    visible_shells = [] if is_empty else list_live_shells(include_exited=False)

    return {
        "id": "run_live",
        "title": str(state.get("session_title", "")).strip() or ("new session" if is_empty else "current session"),
        "status": live_status,
        "started": started_at,
        "archiveDate": datetime.now().astimezone().strftime("%Y-%m-%d"),
        "archiveSessionId": str(state.get("archive_session_id", "")).strip(),
        "dur": duration,
        "preview": (last_msg["body"][:80] + "…") if last_msg and last_msg.get("body") else "—",
        "model": _get_model(),
        "ctx_limit": _get_current_model_ctx_limit(),
        "currentRoundId": current_round_id,
        "currentRoundTitle": current_round_title,
        "pendingQuestion": pending_question,
        "summary": live_summary,
        "main_agent_total_tokens": main_agent_total_tokens,
        "main_agent_context_tokens": _last_request_context_tokens(raw_msgs),
        "chat": {
            "contextChips": _build_context_chips(),
            "messages": messages,
        },
        "liveRounds": live_rounds,
        "shells": visible_shells,
        "subagents": subagents,
        "flow": _build_live_flow(raw_msgs, messages, subagents, subagent_registry),
    }


def _build_archive_sessions(
    skip_dates: set[str] | None = None,
    skip_archive_ids: set[str] | None = None,
) -> list[dict]:
    """Build session entries from conversation archives (one per archived session)."""
    if not CONVERSATIONS_DIR.exists():
        return []

    sessions = []
    files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)
    for filepath in files[:10]:  # cap at 10 most recent days
        date_str = filepath.stem
        if skip_dates and date_str in skip_dates:
            continue
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue
        sections = _parse_archive_sections(content)
        if not sections:
            continue

        file_session_title = _parse_archive_session_title(content)
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for index, section in enumerate(sections):
            archive_session_id = str(section.get("archive_session_id", "")).strip() or f"legacy_{date_str}"
            if archive_session_id not in groups:
                groups[archive_session_id] = []
                order.append(archive_session_id)
            groups[archive_session_id].append({**section, "_order": index})

        for archive_session_id in reversed(order):
            archive_key = f"{date_str}:{archive_session_id}"
            if skip_archive_ids and archive_key in skip_archive_ids:
                continue
            group_sections = groups[archive_session_id]
            messages = _messages_from_archive_sections(group_sections)
            if not messages:
                continue
            last_user = next((m for m in messages if m["role"] == "user"), None)
            group_session_title = next(
                (str(section.get("session_title", "")).strip() for section in group_sections if section.get("session_title")),
                "",
            )
            is_legacy = archive_session_id.startswith("legacy_")
            title = group_session_title or (file_session_title if is_legacy else "") or ((last_user["body"][:60] + ("…" if len(last_user["body"]) > 60 else "")) if last_user else date_str)
            preview = messages[-1].get("body", "")[:80] if messages else ""
            current_round_id = next((str(m.get("round_id", "")).strip() for m in reversed(messages) if m.get("round_id")), "")
            current_round_title = next(
                (
                    str(m.get("round_title", "")).strip()
                    for m in reversed(messages)
                    if str(m.get("round_id", "")).strip() == current_round_id and m.get("round_title")
                ),
                "",
            )

            sessions.append({
                "id": f"archive_{date_str}_{archive_session_id}",
                "title": title,
                "status": "done",
                "started": date_str,
                "dur": "—",
                "preview": preview,
                "model": _get_model(),
                "currentRoundId": current_round_id,
                "currentRoundTitle": current_round_title,
                "summary": {
                    "tokens": f"{len(messages)} msgs",
                    "spend": "—",
                    "toolCalls": 0,
                },
                "chat": {
                    "contextChips": [{"icon": "📅", "label": date_str}],
                    "messages": messages,
                },
                "liveRounds": [],
                "shells": [],
                "subagents": [],
                "flow": _build_simple_flow(messages),
            })
    return sessions


def _parse_archive_meta(section: str, key: str) -> str:
    match = re.search(rf"<!--\s*{re.escape(key)}:\s*(.*?)\s*-->", section)
    return match.group(1).strip() if match else ""


def _parse_archive_session_title(content: str) -> str:
    return _parse_archive_meta(content, "session_title")


def _split_archive_entry_blocks(content: str) -> list[str]:
    blocks: list[str] = []
    matches = list(re.finditer(r"(?m)^##\s+\S+\s+UTC\s*$", content))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        block = re.sub(r"\n+---\s*\Z", "", block).strip()
        if block:
            blocks.append(block)
    return blocks


def _parse_archive_sections(content: str) -> list[dict[str, Any]]:
    """Parse a conversations/YYYY-MM-DD.md file into archive sections with metadata."""
    sections_out: list[dict[str, Any]] = []
    round_index = 0

    for section in _split_archive_entry_blocks(content):
        if "**User**:" not in section:
            continue
        ts_match = re.search(r"##\s*(\S+\s+UTC)", section)
        dialogue_match = re.search(r"\*\*User\*\*:\s*(.*?)\n+\*\*[^*]+\*\*:\s*(.*)\Z", section, re.DOTALL)
        if not ts_match or not dialogue_match:
            continue

        ts = ts_match.group(1).strip()
        user_body = dialogue_match.group(1).strip()
        assistant_body = dialogue_match.group(2).strip()
        round_id = _parse_archive_meta(section, "round_id") or f"archive_round_{round_index}"
        round_title = _parse_archive_meta(section, "round_title")
        archive_session_id = _parse_archive_meta(section, "archive_session_id")
        session_title = _parse_archive_meta(section, "session_title")
        body_start = section.find("## ")
        raw_entry = section[body_start:].strip() if body_start >= 0 else section.strip()
        sections_out.append({
            "timestamp": ts,
            "user_body": user_body,
            "assistant_body": assistant_body,
            "round_id": round_id,
            "round_title": round_title,
            "archive_session_id": archive_session_id,
            "session_title": session_title,
            "raw_entry": raw_entry,
        })
        round_index += 1
    return sections_out


def _messages_from_archive_sections(sections: list[dict[str, Any]]) -> list[dict]:
    messages: list[dict] = []
    for index, section in enumerate(sections):
        messages.append({
            "id": f"m{index}u",
            "role": "user",
            "time": section["timestamp"],
            "body": section["user_body"],
            "round_id": section["round_id"],
            "round_title": section["round_title"],
        })
        messages.append({
            "id": f"m{index}a",
            "role": "agent",
            "time": section["timestamp"],
            "body": section["assistant_body"],
            "round_id": section["round_id"],
            "round_title": section["round_title"],
        })
    return messages


def _parse_archive_file(content: str) -> list[dict]:
    """Parse a conversations/YYYY-MM-DD.md file into UI-formatted messages."""
    return _messages_from_archive_sections(_parse_archive_sections(content))


def _write_archive_sections(filepath: Path, date_str: str, sections: list[dict[str, Any]]) -> None:
    if not sections:
        if filepath.exists():
            filepath.unlink()
        return
    first_session_title = next((str(section.get("session_title", "")).strip() for section in sections if section.get("session_title")), "")
    content = _upsert_archive_session_title(f"# Conversations - {date_str}\n\n", date_str, first_session_title)
    content += "\n---\n\n".join(section["raw_entry"] for section in sections if section.get("raw_entry")) + "\n\n---\n"
    filepath.write_text(content, encoding="utf-8")


def _upsert_archive_session_title(content: str, date_str: str, session_title: str) -> str:
    header = f"# Conversations - {date_str}\n\n"
    if not content:
        content = header
    elif not content.startswith("# Conversations - "):
        content = header + content
    if not session_title:
        return content
    marker = f"<!-- session_title: {session_title} -->\n\n"
    pattern = re.compile(r"^(# Conversations - .*?\n\n)(?:<!-- session_title: .*? -->\n\n)?", re.DOTALL)
    if pattern.search(content):
        return pattern.sub(lambda match: match.group(1) + marker, content, count=1)
    return header + marker + content[len(header):]


def _is_hidden_internal_message(message: dict[str, Any]) -> bool:
    if bool(message.get("hidden_from_ui")):
        return True
    role = str(message.get("role", "")).strip()
    content = str(message.get("content", "") or "").strip()
    if role != "user" or not content:
        return False
    return (
        content.startswith("## Research Materials\n\nBelow are the research findings gathered on this question.")
        or content.startswith("[Decision-phase correction] You attempted unavailable tool(s):")
    )


def _convert_messages(raw_msgs: list[dict]) -> list[dict]:
    """Convert state.json raw messages → UI message format."""
    out = []
    compacted_marker_emitted = False
    tool_outputs = _tool_output_map(raw_msgs)
    for i, m in enumerate(raw_msgs):
        if _is_hidden_internal_message(m):
            continue
        if isinstance(m, dict) and m.get("compacted_block"):
            if not compacted_marker_emitted:
                cid = str(m.get("message_id", "")).strip() or ("compacted" + str(i))
                out.append({"id": cid, "messageId": cid, "role": "system", "kind": "compacted", "compacted": True})
                compacted_marker_emitted = True
            continue
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        has_live_detail = bool(m.get("reasoning_content") or m.get("tool_calls"))
        has_attachments = isinstance(m.get("attachments"), list) and bool(m.get("attachments"))
        if role == "user" and not content and not m.get("attachments"):
            continue
        if role == "assistant" and not content and not has_live_detail and not has_attachments:
            continue
        ui_role = "user" if role == "user" else "agent"
        message_id = str(m.get("message_id", "")).strip() or f"m{i}"
        ui_msg = {"id": message_id, "messageId": message_id, "role": ui_role, "time": "—"}
        if content:
            ui_msg["body"] = content
        if isinstance(m.get("attachments"), list):
            ui_msg["attachments"] = [
                {
                    "id": str(item.get("id") or "").strip(),
                    "name": str(item.get("name") or "file"),
                    "content_type": str(item.get("content_type") or "application/octet-stream"),
                    "size": int(item.get("size") or 0),
                    "kind": str(item.get("kind") or "file"),
                    "url": str(item.get("url") or "").strip(),
                    **({"width": int(item.get("width"))} if str(item.get("width", "")).strip().isdigit() else {}),
                    **({"height": int(item.get("height"))} if str(item.get("height", "")).strip().isdigit() else {}),
                }
                for item in m.get("attachments")
                if isinstance(item, dict)
            ]
        if bool(m.get("intermediate_reply")):
            ui_msg["intermediateReply"] = True
        if bool(m.get("question_prompt")):
            ui_msg["questionPrompt"] = True
        question_id = str(m.get("question_id", "")).strip()
        if question_id:
            ui_msg["questionId"] = question_id
        round_id = str(m.get("round_id", "")).strip()
        if round_id:
            ui_msg["roundId"] = round_id
        client_request_id = str(m.get("client_request_id", "")).strip()
        if client_request_id:
            ui_msg["clientRequestId"] = client_request_id
        queued_guidance_id = str(m.get("queued_guidance_id", "")).strip()
        if queued_guidance_id:
            ui_msg["queuedGuidanceId"] = queued_guidance_id
        guidance_ack_for_guidance_id = str(m.get("guidance_ack_for_guidance_id", "")).strip()
        if guidance_ack_for_guidance_id:
            ui_msg["guidanceAckForGuidanceId"] = guidance_ack_for_guidance_id
        in_reply_to_guidance_id = str(m.get("in_reply_to_guidance_id", "")).strip()
        if in_reply_to_guidance_id:
            ui_msg["inReplyToGuidanceId"] = in_reply_to_guidance_id
        if m.get("reasoning_content"):
            ui_msg["thinking"] = m["reasoning_content"]
        if m.get("tool_calls"):
            tools = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "")
                parsed_args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
                args = raw_args
                if isinstance(args, str) and len(args) > 80:
                    args = args[:80] + "…"
                tool_call_id = str(tc.get("id") or "")
                tools.append({
                    "name": fn.get("name", "?"),
                    "arg": str(args)[:120],
                    "status": "done",
                    "out": tool_outputs.get(tool_call_id, ""),
                    "toolCallId": tool_call_id,
                    "rawArgs": parsed_args if parsed_args is not None else raw_args,
                })
            ui_msg["tools"] = tools
        out.append(ui_msg)
    return _collapse_duplicate_user_messages(
        _merge_adjacent_trace_only_messages(_dedupe_repeated_messages(out))
    )


def _is_trace_only_agent_message(msg: dict[str, Any]) -> bool:
    return (
        msg.get("role") == "agent"
        and not str(msg.get("body", "")).strip()
        and (bool(msg.get("thinking")) or bool(msg.get("tools")))
    )


def _dedupe_repeated_messages(messages: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_ids: set[tuple[str, str]] = set()
    seen_tool_keys: dict[tuple, int] = {}
    for msg in messages:
        message_id = str(msg.get("messageId", "")).strip() or str(msg.get("id", "")).strip()
        tool_key = _ui_tool_message_key(msg)
        if tool_key and tool_key in seen_tool_keys:
            deduped[seen_tool_keys[tool_key]] = msg
            continue
        if tool_key:
            seen_tool_keys[tool_key] = len(deduped)
        if message_id:
            dedupe_key = (str(msg.get("role", "")).strip(), message_id)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
        deduped.append(msg)
    return deduped


def _ui_tool_message_key(msg: dict) -> tuple | None:
    if not isinstance(msg, dict):
        return None
    role = str(msg.get("role", "")).strip()
    round_id = str(msg.get("roundId", "")).strip()
    if role == "agent":
        tools = msg.get("tools") if isinstance(msg.get("tools"), list) else []
        tool_ids = tuple(
            str(tool.get("toolCallId", "")).strip()
            for tool in tools
            if isinstance(tool, dict) and str(tool.get("toolCallId", "")).strip()
        )
        if tool_ids and len(tool_ids) == len(tools):
            return ("agent_tools", round_id, tool_ids)
    return None


def _merge_adjacent_trace_only_messages(messages: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for msg in messages:
        if not merged:
            merged.append(msg)
            continue
        prev = merged[-1]
        prev_request_id = str(prev.get("clientRequestId", "")).strip()
        next_request_id = str(msg.get("clientRequestId", "")).strip()
        compatible_request = (
            not prev_request_id
            or not next_request_id
            or prev_request_id == next_request_id
        )
        compatible_round = str(prev.get("roundId", "")).strip() == str(msg.get("roundId", "")).strip()
        compatible_guidance = (
            not str(prev.get("queuedGuidanceId", "")).strip()
            and not str(msg.get("queuedGuidanceId", "")).strip()
            and not str(prev.get("guidanceAckForGuidanceId", "")).strip()
            and not str(msg.get("guidanceAckForGuidanceId", "")).strip()
            and not str(prev.get("inReplyToGuidanceId", "")).strip()
            and not str(msg.get("inReplyToGuidanceId", "")).strip()
        )
        if (
            _is_trace_only_agent_message(prev)
            and _is_trace_only_agent_message(msg)
            and compatible_round
            and compatible_request
            and compatible_guidance
        ):
            prev_thinking = str(prev.get("thinking", "")).strip()
            next_thinking = str(msg.get("thinking", "")).strip()
            if next_thinking:
                if prev_thinking and next_thinking != prev_thinking:
                    prev["thinking"] = prev_thinking + "\n\n" + next_thinking
                elif not prev_thinking:
                    prev["thinking"] = next_thinking
            prev_tools = list(prev.get("tools") or [])
            next_tools = list(msg.get("tools") or [])
            if next_tools:
                prev["tools"] = prev_tools + next_tools
            continue
        if (
            _is_trace_only_agent_message(prev)
            and msg.get("role") == "agent"
            and compatible_round
            and compatible_request
            and compatible_guidance
            and (
                str(msg.get("body", "")).strip()
                or str(msg.get("thinking", "")).strip()
                or bool(msg.get("tools"))
            )
        ):
            merged_msg = dict(msg)
            prev_thinking = str(prev.get("thinking", "")).strip()
            next_thinking = str(merged_msg.get("thinking", "")).strip()
            if prev_thinking:
                if next_thinking and next_thinking != prev_thinking:
                    merged_msg["thinking"] = prev_thinking + "\n\n" + next_thinking
                elif not next_thinking:
                    merged_msg["thinking"] = prev_thinking
            prev_tools = list(prev.get("tools") or [])
            next_tools = list(merged_msg.get("tools") or [])
            if prev_tools or next_tools:
                merged_msg["tools"] = prev_tools + next_tools
            if not str(merged_msg.get("clientRequestId", "")).strip() and prev_request_id:
                merged_msg["clientRequestId"] = prev_request_id
            merged[-1] = merged_msg
            continue
        merged.append(msg)
    return merged


def _collapse_duplicate_user_messages(messages: list[dict]) -> list[dict]:
    collapsed: list[dict] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.get("role") != "user":
            collapsed.append(msg)
            index += 1
            continue

        block_end = index
        while block_end < len(messages) and messages[block_end].get("role") == "user":
            block_end += 1

        block = messages[index:block_end]
        seen_bodies: set[str] = set()
        kept_reversed: list[dict] = []
        for block_msg in reversed(block):
            body = str(block_msg.get("body", "")).strip()
            if body and body in seen_bodies:
                continue
            if body:
                seen_bodies.add(body)
            kept_reversed.append(block_msg)
        collapsed.extend(reversed(kept_reversed))
        index = block_end
    return collapsed


def _count_tool_calls(raw_msgs: list[dict]) -> int:
    count = sum(len(m.get("tool_calls") or []) for m in raw_msgs)
    if count == 0:
        count = sum(1 for m in raw_msgs if m.get("role") == "tool")
    return count


def _session_started_at(raw_msgs: list[dict]) -> float:
    for m in raw_msgs:
        round_id = str(m.get("round_id", "")).strip()
        match = re.fullmatch(r"round_(\d+)", round_id)
        if match:
            return int(match.group(1)) / 1000.0
    return _SERVER_STARTED_AT


def _build_simple_flow(messages: list[dict]) -> dict:
    """Archive flow grouped by conversation round, without live tool traces."""
    rounds: list[list[dict]] = []
    current: list[dict] = []
    current_round_id = ""

    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip() or current_round_id or "archive_round_0"
        if current and round_id != current_round_id:
            rounds.append(current)
            current = []
        current.append(msg)
        current_round_id = round_id
    if current:
        rounds.append(current)

    nodes: list[dict] = []
    edges: list[dict] = []
    y_offset = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_msgs in enumerate(rounds or [messages]):
        prefix = f"r{round_index}_" if multiple_rounds else ""
        last_user = next((m for m in round_msgs if m["role"] == "user"), None)
        last_agent = next((m for m in reversed(round_msgs) if m["role"] == "agent"), None)
        round_title = next((str(m.get("round_title", "")).strip() for m in round_msgs if m.get("round_title")), "") or "user request"
        user_id = f"{prefix}n_user"
        main_id = f"{prefix}n_main"
        out_id = f"{prefix}n_out"

        nodes.extend([
            {
                "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
                "title": round_title, "status": "done",
                "detail": {
                    "role": "User",
                    "text": last_user["body"] if last_user else "",
                    "tokens": 0,
                    "time": last_user["time"] if last_user else "—",
                },
            },
            {
                "id": main_id, "kind": "main", "x": 320, "y": y_offset + 70,
                "title": f"main agent · {ASSISTANT_NAME}",
                "subtitle": "archive",
                "status": "done",
                "model": _get_model(),
                "detail": {
                    "systemPrompt": f"You are {ASSISTANT_NAME}, an AI companion. Use SOUL.md to maintain persona.",
                    "reasoning": "Loaded session from archive — no live reasoning trace.",
                    "tokensIn": 0, "tokensOut": 0,
                    "model": _get_model(), "temp": 0.2,
                },
            },
            {
                "id": out_id, "kind": "output", "x": 660, "y": y_offset + 90,
                "title": "response", "status": "done",
                "detail": {
                    "kind": "Output",
                    "content": (last_agent["body"][:600] if last_agent else "—"),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "glob",
                    "description": "按通配符模式搜索工作区中的文件路径。例如：'**/*.py' 查找所有 Python 文件，'**/*.tsx' 查找所有 React 组件。自动跳过 node_modules、.git、__pycache__ 等目录。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "glob 搜索模式，相对于工作区根目录，例如 '**/*.py' 或 'src/**/*.ts'",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
        ])
        edges.extend([
            {"from": user_id, "to": main_id},
            {"from": main_id, "to": out_id},
        ])
        y_offset += 180

    return {"nodes": nodes, "edges": edges}


def _build_live_flow(raw_msgs: list[dict], messages: list[dict], subagents: list[dict], registry: dict[str, dict]) -> dict:
    """Build a richer flow for the current session, stacked by conversation round."""
    rounds = _split_raw_rounds(raw_msgs)
    recent_events = debug.get_recent_events(250)
    if not rounds and raw_msgs:
        rounds = [raw_msgs]
    if not rounds:
        synthetic_round = _synthetic_live_round(registry, recent_events)
        if synthetic_round:
            rounds = [synthetic_round]
    if not rounds:
        return {"nodes": [], "edges": []}

    rounds, active_round_index = _prune_flow_rounds(rounds)
    if not rounds:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    next_y = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_raw in enumerate(rounds):
        is_current_round = round_index == active_round_index
        round_messages = _convert_messages(round_raw)
        round_id = _latest_round_id_from_messages(round_raw)
        round_registry = _round_registry_for_flow(round_raw, registry if is_current_round else {})
        related_agents = _related_round_agent_names(set(round_registry), round_id=round_id)
        if is_current_round and subagents:
            candidate_subagents = [
                sa for sa in subagents
                if _subagent_matches_round(sa, round_id) and (not round_registry or sa["name"] in related_agents)
            ]
            for sa in candidate_subagents:
                entry = round_registry.setdefault(sa["name"], {
                    "task": sa.get("task", ""),
                    "status": "done",
                    "result": sa.get("result", ""),
                    "messages": [],
                    "created_at": None,
                    "updated_at": None,
                    "round_id": round_id,
                })
                entry["task"] = entry.get("task") or sa.get("task", "")
                entry["status"] = _registry_status_from_ui(sa.get("status", entry.get("status", "done")))
                entry["result"] = entry.get("result") or sa.get("result", "")
        if is_current_round and not round_registry and registry:
            round_registry = {
                agent_id: dict(info)
                for agent_id, info in registry.items()
                if not round_id or info.get("round_id") in ("", round_id)
            }
        round_subagents = _subagent_cards_from_registry(round_registry)
        round_recent_events = _events_for_round(recent_events, round_id) if is_current_round else []
        prefix = f"r{round_index}_" if multiple_rounds else ""
        round_nodes, round_edges, round_bottom = _build_live_flow_round(
            prefix=prefix,
            raw_msgs=round_raw,
            messages=round_messages,
            subagents=round_subagents,
            registry=round_registry,
            recent_events=round_recent_events,
            y_offset=next_y,
            round_id=round_id,
        )
        nodes.extend(round_nodes)
        edges.extend(round_edges)
        next_y = round_bottom + 180

    return {"nodes": nodes, "edges": edges}


def _synthetic_live_round(registry: dict[str, dict], recent_events: list[dict]) -> list[dict]:
    if not registry:
        return []
    round_id = next((str(info.get("round_id", "")).strip() for info in registry.values() if info.get("round_id")), "")
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    prompt = (
        latest_phase.get("detail")
        if latest_phase and latest_phase.get("detail")
        else latest_llm.get("response")
        if latest_llm and latest_llm.get("response")
        else "Live round in progress"
    )
    entry: dict[str, Any] = {"role": "user", "content": prompt}
    if round_id:
        entry["round_id"] = round_id
    return [entry]


def _split_raw_rounds(raw_msgs: list[dict]) -> list[list[dict]]:
    rounds: list[list[dict]] = []
    current: list[dict] = []
    current_key = ""
    anonymous_round_index = 0
    for msg in raw_msgs:
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            next_key = f"round:{round_id}"
        elif msg.get("role") == "user":
            anonymous_round_index += 1
            next_key = f"anon:{anonymous_round_index}"
        else:
            next_key = current_key or f"anon:{max(anonymous_round_index, 1)}"

        if current and next_key != current_key:
            rounds.append(current)
            current = []

        if not current:
            current_key = next_key
            current = [msg]
            continue

        current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _round_has_activity(raw_msgs: list[dict]) -> bool:
    return any(str(msg.get("role", "")) != "user" for msg in raw_msgs)


def _prune_flow_rounds(rounds: list[list[dict]]) -> tuple[list[list[dict]], int]:
    """Keep substantive rounds plus the latest pending user-only round.

    This prevents interrupted trailing user messages from stretching the flow
    into multiple empty rounds while still preserving the latest pending input.
    """
    if not rounds:
        return [], -1

    substantive_indices = [i for i, round_raw in enumerate(rounds) if _round_has_activity(round_raw)]
    if not substantive_indices:
        return [rounds[-1]], 0

    keep_indices = set(substantive_indices)
    latest_substantive = substantive_indices[-1]
    tail_pending = [
        i for i in range(latest_substantive + 1, len(rounds))
        if not _round_has_activity(rounds[i])
    ]
    if tail_pending:
        keep_indices.add(tail_pending[-1])

    pruned: list[list[dict]] = []
    index_map: dict[int, int] = {}
    for original_index, round_raw in enumerate(rounds):
        if original_index not in keep_indices:
            continue
        index_map[original_index] = len(pruned)
        pruned.append(round_raw)

    return pruned, index_map[latest_substantive]


def _round_registry_for_flow(raw_msgs: list[dict], live_registry: dict[str, dict]) -> dict[str, dict]:
    round_id = _latest_round_id_from_messages(raw_msgs)
    entries: dict[str, dict] = _snapshot_entries_from_messages(raw_msgs, round_id=round_id)
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            live = dict(live_registry.get(agent_id, {}))
            if round_id and live.get("round_id") and live.get("round_id") != round_id:
                live = {}
            task = str(args.get("task") or live.get("task") or "")
            _merge_subagent_record(entries, agent_id, {
                "task": task,
                "status": live.get("status", entries.get(agent_id, {}).get("status", "done")),
                "result": live.get("result", entries.get(agent_id, {}).get("result", "")),
                "messages": list(live.get("messages", [])) or list(entries.get(agent_id, {}).get("messages", [])),
                "created_at": live.get("created_at", entries.get(agent_id, {}).get("created_at")),
                "updated_at": live.get("updated_at", entries.get(agent_id, {}).get("updated_at")),
                "round_id": round_id or live.get("round_id", entries.get(agent_id, {}).get("round_id", "")),
            })
    for agent_id, live in live_registry.items():
        live_round_id = str(live.get("round_id", "")).strip()
        if round_id and live_round_id and live_round_id != round_id:
            continue
        _merge_subagent_record(entries, agent_id, {
            "task": live.get("task", ""),
            "status": live.get("status", "done"),
            "result": live.get("result", ""),
            "messages": list(live.get("messages", [])),
            "created_at": live.get("created_at"),
            "updated_at": live.get("updated_at"),
            "round_id": round_id or live_round_id,
        })
    return entries


def _related_round_agent_names(seed_ids: set[str], round_id: str = "") -> set[str]:
    if not seed_ids:
        return set()
    related = set(seed_ids)
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return related

    changed = True
    while changed:
        changed = False
        for msg_file in inbox_root.glob("*/*.json"):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if from_agent in related or to_agent in related:
                size_before = len(related)
                if from_agent:
                    related.add(from_agent)
                if to_agent:
                    related.add(to_agent)
                changed = changed or len(related) != size_before
    return related


def _round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in raw_msgs:
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _latest_round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in reversed(raw_msgs):
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _events_for_round(recent_events: list[dict], round_id: str) -> list[dict]:
    if not round_id:
        return list(recent_events)
    return [
        event for event in recent_events
        if str(event.get("round_id", "")).strip() == round_id
    ]


def _subagent_matches_round(subagent: dict[str, Any], round_id: str) -> bool:
    if not round_id:
        return True
    subagent_round_id = str(subagent.get("roundId") or subagent.get("round_id") or "").strip()
    return not subagent_round_id or subagent_round_id == round_id


def _registry_status_from_ui(status: str) -> str:
    return {
        "running": "running",
        "queued": "waiting",
        "done": "done",
        "err": "timeout",
    }.get(status, status)


def _is_summary_agent_id(agent_id: str) -> bool:
    return str(agent_id or "").startswith("agent_summary_")


def _iter_flow_snapshots(raw_msgs: list[dict], round_id: str = "") -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for msg in raw_msgs:
        snapshot = msg.get("subagent_flow_snapshot")
        if not isinstance(snapshot, dict):
            continue
        snapshot_round_id = str(snapshot.get("round_id", "")).strip() or str(msg.get("round_id", "")).strip()
        if round_id and snapshot_round_id and snapshot_round_id != round_id:
            continue
        snapshots.append(snapshot)
    return snapshots


def _merge_subagent_record(entries: dict[str, dict[str, Any]], agent_id: str, meta: dict[str, Any]) -> None:
    incoming = dict(meta)
    incoming_round_id = str(incoming.get("round_id", "")).strip()
    existing = entries.get(agent_id)
    if existing is None:
        entries[agent_id] = incoming
        return

    existing_round_id = str(existing.get("round_id", "")).strip()
    if incoming_round_id and existing_round_id and incoming_round_id != existing_round_id:
        entries[agent_id] = incoming
        return

    merged = dict(existing)
    for key, value in incoming.items():
        if key == "messages":
            if value:
                merged["messages"] = value
            else:
                merged.setdefault("messages", [])
            continue
        if value not in (None, "", []):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    entries[agent_id] = merged


def _snapshot_entries_from_messages(raw_msgs: list[dict], round_id: str = "") -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for snapshot in _iter_flow_snapshots(raw_msgs, round_id=round_id):
        agents = snapshot.get("agents") or {}
        if not isinstance(agents, dict):
            continue
        snapshot_round_id = str(snapshot.get("round_id", "")).strip()
        for agent_id, info in agents.items():
            if not isinstance(info, dict):
                continue
            meta = dict(info)
            meta.setdefault("round_id", snapshot_round_id)
            meta.setdefault("messages", [])
            _merge_subagent_record(entries, str(agent_id), meta)
    return entries


def _snapshot_comm_messages_from_messages(raw_msgs: list[dict], round_id: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for snapshot in _iter_flow_snapshots(raw_msgs, round_id=round_id):
        comm_messages = snapshot.get("comm_messages") or []
        if not isinstance(comm_messages, list):
            continue
        for item in comm_messages:
            if not isinstance(item, dict):
                continue
            from_agent = str(item.get("from", "")).strip()
            to_agent = str(item.get("to", "")).strip()
            body = str(item.get("content", ""))
            message_id = str(item.get("message_id") or "").strip()
            dedupe_key = (message_id, from_agent, to_agent, body)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(dict(item))
    items.sort(key=lambda item: str(item.get("timestamp") or ""))
    return items


def _subagent_cards_from_registry(round_registry: dict[str, dict]) -> list[dict]:
    cards: list[dict] = []
    for agent_id, info in round_registry.items():
        status = info.get("status", "done")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        cards.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })
    return cards


def _build_live_flow_round(
    prefix: str,
    raw_msgs: list[dict],
    messages: list[dict],
    subagents: list[dict],
    registry: dict[str, dict],
    recent_events: list[dict],
    y_offset: int,
    round_id: str,
) -> tuple[list[dict], list[dict], int]:
    main_x = 320
    main_y = y_offset + 70
    main_tool_x = 600
    subagent_x = 900
    subagent_tool_x = 1220
    output_x = 1540
    subagent_base_y = y_offset + 40
    subagent_gap_y = 220

    last_user = next((m for m in messages if m["role"] == "user"), None)
    latest_main_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_agent = next((m for m in reversed(messages) if m["role"] == "agent"), None)
    latest_assistant_raw = next((m for m in reversed(raw_msgs) if m.get("role") == "assistant"), None)
    round_title = next((str(m.get("round_title", "")).strip() for m in raw_msgs if m.get("round_title")), "") or "user request"
    system_initiated = any(bool(m.get("system_initiated")) for m in raw_msgs if isinstance(m, dict))
    if system_initiated and round_title == "user request":
        round_title = "proactive check-in"
    main_usage = _usage_totals(raw_msgs)
    main_tool_base_y = main_y + 150

    main_id = f"{prefix}n_main"
    user_id = f"{prefix}n_user"
    output_id = f"{prefix}n_out"
    main_completed = bool(latest_agent)

    _llm_resp = latest_main_llm.get("response") if latest_main_llm else None
    _llm_text = (
        str(_llm_resp.get("reasoning_content") or _llm_resp.get("content") or "")
        if isinstance(_llm_resp, dict) else ""
    )
    _main_reasoning = (
        str(latest_assistant_raw.get("reasoning_content") or "")
        if latest_assistant_raw and latest_assistant_raw.get("reasoning_content")
        else _llm_text
        if _llm_text
        else str(latest_phase.get("detail") or "")
        if latest_phase and latest_phase.get("detail")
        else "Session step completed."
    )

    tool_nodes, tool_edges = _build_tool_nodes_for_owner(
        owner_node_id=main_id,
        owner_title=f"main agent · {ASSISTANT_NAME}",
        owner_x=main_x,
        owner_y=main_y,
        raw_messages=raw_msgs,
        recent_events=recent_events,
        caller_prefix="main_agent",
        x=main_tool_x,
        base_y=main_tool_base_y,
        owner_completed=main_completed,
    )
    main_status = (
        "running"
        if any(sa["status"] == "running" for sa in subagents) or any(node["status"] == "running" for node in tool_nodes)
        else ("done" if main_completed else "queued")
    )

    nodes = [
        {
            "id": main_id, "kind": "main", "x": main_x, "y": main_y,
            "title": f"main agent · {ASSISTANT_NAME}",
            "subtitle": latest_phase["to"] if latest_phase and latest_phase.get("to") else "orchestrator",
            "status": main_status,
            "model": _get_model(),
            "detail": {
                "systemPrompt": (
                    f"You are {ASSISTANT_NAME}. Two-phase loop: lightweight tool decision, "
                    "then full tool loop with subagent spawn. Chat filter applies SOUL.md voice."
                ),
                "reasoning": _main_reasoning,
                "tokensIn": main_usage.get("prompt_tokens") or "—",
                "tokensOut": main_usage.get("completion_tokens") or "—",
                "model": _get_model(), "temp": 0.2,
            },
        },
    ]
    edges: list[dict[str, Any]] = []
    if last_user and not system_initiated:
        user_text = str(last_user.get("body") or "").strip() or (
            "[Uploaded attachment]"
            if last_user.get("attachments")
            else "—"
        )
        nodes.insert(0, {
            "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
            "title": round_title, "status": "done",
            "detail": {
                "role": "User",
                "text": user_text,
                "tokens": 0,
                "time": last_user["time"] if last_user else "—",
            },
        })
        edges.append({"from": user_id, "to": main_id, "kind": "active" if main_status == "running" else None})
    nodes.extend(tool_nodes)
    edges.extend(tool_edges)

    agent_node_ids: dict[str, str] = {}
    subagent_bottoms: list[int] = []
    subagent_y = subagent_base_y
    for i, sa in enumerate(subagents):
        nid = f"{prefix}n_sa_{i}"
        agent_node_ids[sa["name"]] = nid
        is_summary_agent = _is_summary_agent_id(sa["name"])
        info = registry.get(sa["name"], {})
        agent_messages = info.get("messages", [])
        latest_subassistant = next((m for m in reversed(agent_messages) if m.get("role") == "assistant"), None)
        sub_usage = _usage_totals(agent_messages)
        sub_tool_count = _count_tool_nodes_for_owner(
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
        )
        nodes.append({
            "id": nid, "kind": "subagent",
            "x": subagent_x, "y": subagent_y,
            "title": f"{'summary subagent' if is_summary_agent else 'subagent'} · {sa['name']}",
            "subtitle": ("synthesizer" if is_summary_agent else sa["task"][:30]),
            "status": sa["status"],
            "detail": {
                "name": sa["name"],
                "task": sa["task"],
                "parent": "main agent",
                "role": "summary" if is_summary_agent else "worker",
                "spawnedAt": sa.get("createdAt", "—"),
                "tokensIn": sub_usage.get("prompt_tokens") or "—",
                "tokensOut": sub_usage.get("completion_tokens") or "—",
                "model": _get_model(),
                "reasoning": latest_subassistant.get("reasoning_content") if latest_subassistant else "",
                "result": sa.get("result", ""),
            },
        })
        edges.append({
            "from": main_id,
            "to": nid,
            "kind": "dashed" if is_summary_agent else ("active" if sa["status"] == "running" else None),
        })

        sub_nodes, sub_edges = _build_tool_nodes_for_owner(
            owner_node_id=nid,
            owner_title=f"subagent · {sa['name']}",
            owner_x=subagent_x,
            owner_y=subagent_y,
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
            x=subagent_tool_x,
            base_y=subagent_y,
            owner_completed=sa["status"] in {"done", "err"},
        )
        nodes.extend(sub_nodes)
        edges.extend(sub_edges)
        lane_height = _agent_lane_height(sub_tool_count)
        subagent_bottoms.append(subagent_y + lane_height)
        subagent_y += lane_height + subagent_gap_y

    summary_agent_name = next((name for name in agent_node_ids if _is_summary_agent_id(name)), "")
    if summary_agent_name:
        summary_node_id = agent_node_ids[summary_agent_name]
        for agent_name, node_id in agent_node_ids.items():
            if agent_name == summary_agent_name:
                continue
            edges.append({"from": node_id, "to": summary_node_id, "kind": "dashed"})

    edges.extend(_build_comm_edges(
        agent_node_ids,
        agent_entries=registry,
        round_id=round_id,
        persisted_messages=_snapshot_comm_messages_from_messages(raw_msgs, round_id=round_id),
    ))

    output_content = str(latest_agent.get("body") or "") if latest_agent else ""
    output_status = "done" if output_content else ("running" if subagents else "queued")
    if output_content or subagents:
        flow_bottom = max(subagent_bottoms) if subagent_bottoms else (main_tool_base_y + _agent_lane_height(max(1, len(tool_nodes))))
        output_y = y_offset + 90 if not subagents else max(y_offset + 90, int((main_y + flow_bottom) / 2) - 43)
        nodes.append({
            "id": output_id, "kind": "output", "x": output_x, "y": output_y,
            "title": "response", "status": output_status,
            "detail": {
                "kind": "Output",
                "content": output_content or "Waiting for subagent synthesis…",
            },
        })
        edges.append({
            "from": main_id,
            "to": output_id,
            "kind": "active" if output_status == "running" else None,
        })
        if summary_agent_name:
            edges.append({
                "from": agent_node_ids[summary_agent_name],
                "to": output_id,
                "kind": "dashed",
            })

    bottom = max((node["y"] + 86) for node in nodes) if nodes else y_offset
    return nodes, edges, bottom


def _empty_session() -> dict:
    """Placeholder when no real session exists yet."""
    return {
        "id": "run_empty",
        "title": "no active session",
        "status": "queued",
        "started": "—",
        "dur": "—",
        "preview": "Send a message to start a session.",
        "model": _get_model(),
        "summary": {"tokens": "0", "spend": "$0.00", "toolCalls": 0},
        "chat": {
            "contextChips": _build_context_chips(),
            "messages": [],
        },
        "liveRounds": [],
        "shells": [],
        "subagents": [],
        "flow": {
            "nodes": [
                {
                    "id": "n_main", "kind": "main", "x": 200, "y": 80,
                    "title": f"main agent · {ASSISTANT_NAME}",
                    "subtitle": "idle", "status": "queued",
                    "model": _get_model(),
                    "detail": {
                        "systemPrompt": f"You are {ASSISTANT_NAME}.",
                        "reasoning": "Waiting for user input.",
                        "tokensIn": 0, "tokensOut": 0,
                        "model": _get_model(), "temp": 0.2,
                    },
                }
            ],
            "edges": [],
        },
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def _build_status() -> dict:
    """Status data for the Status / Dashboard page."""
    return {
        "phase": "evolve",
        "state": "进化",
        "metrics": [],
        "sparkData": [],
        "workers": [],
        "logs": [],
        "services": [],
        "model": _get_model(),
        "base_url": _get_base_url(),
        "short_term_entries": 0,
        "session_messages": 0,
        "scheduled_tasks": 0,
        "soul_exists": SOUL_PATH.exists(),
    }


async def _build_memory() -> dict:
    """Assemble full memory state for the Memory page."""
    import re
    from datetime import datetime, timezone

    # --- SOUL.md ---
    soul_content = read_soul()
    soul_exists = bool(soul_content)
    sections: list[dict] = []
    current_section: dict | None = None
    temporary_count = 0
    temporary_expired = 0
    now = datetime.now(timezone.utc)

    for line in soul_content.splitlines() if soul_content else []:
        trimmed = line.strip()
        if trimmed.startswith("## ") and not trimmed.startswith("### "):
            if current_section:
                sections.append(current_section)
            name = trimmed[3:].strip()
            current_section = {"name": name, "entries": [], "entry_count": 0}
        elif current_section is not None:
            if trimmed and not trimmed.startswith("<!--"):
                current_section["entries"].append(trimmed)
                current_section["entry_count"] += 1
                if current_section["name"] == "TEMPORARY":
                    temporary_count += 1
                    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", trimmed)
                    if date_match:
                        try:
                            item_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            if (now - item_date).days >= 1:
                                temporary_expired += 1
                        except ValueError:
                            pass
    if current_section:
        sections.append(current_section)

    # --- Short-term memory ---
    st_entries = load_entries()
    short_term = {
        "entries": sorted(st_entries, key=lambda e: e.get("last_mentioned", ""), reverse=True),
        "total": len(st_entries),
    }

    # --- Context window ---
    session_msgs: list = []
    if STATE_FILE.exists():
        try:
            session_msgs = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            session_msgs = []
    from cyrene.config_store import get_current_ctx_limit
    from cyrene.call_llm import _message_token_estimate
    _ctx_limit = get_current_ctx_limit()
    context_window = {
        "messages": len(session_msgs),
        "max": 40,
        "tokens": sum(_message_token_estimate(m) for m in session_msgs) if session_msgs else 0,
        "ctx_limit": _ctx_limit,
        "trigger_tokens": int(_ctx_limit * 0.6) if _ctx_limit else 0,
        "compacted_blocks": sum(1 for m in session_msgs if isinstance(m, dict) and m.get("compacted_block")),
    }

    # --- Conversation archive ---
    archive_days = 0
    today_exchanges = 0
    if CONVERSATIONS_DIR.exists():
        archive_files = sorted(CONVERSATIONS_DIR.glob("*.md"))
        archive_days = len(archive_files)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_file = CONVERSATIONS_DIR / f"{today_str}.md"
        if today_file.exists():
            try:
                raw = today_file.read_text(encoding="utf-8")
                today_exchanges = raw.count("## ") - 1
            except Exception:
                pass

    return {
        "soul": {
            "exists": soul_exists,
            "path": str(get_soul_path()),
            "sections": sections,
            "temporary_count": temporary_count,
            "temporary_expired": temporary_expired,
        },
        "short_term": short_term,
        "context_window": context_window,
        "archive": {
            "days": archive_days,
            "today_exchanges": max(0, today_exchanges),
        },
    }


async def _build_dashboard(ui_tz=None) -> dict:
    """Aggregate homepage data from memory, soul, archive, and scheduler state."""
    from cyrene import db as cy_db
    from cyrene.subagent import _registry  # noqa: WPS437

    ui_tz = ui_tz or (datetime.now().astimezone().tzinfo or timezone.utc)
    now_local = datetime.now(ui_tz)

    st_entries = load_entries()
    try:
        tasks = await cy_db.get_all_tasks(_db_path)
    except Exception:
        tasks = []

    today = now_local.strftime("%Y-%m-%d")
    soul_content = read_soul()
    soul_path = get_soul_path()
    soul_stat = soul_path.stat() if soul_path.exists() else None
    soul_lines = [line.strip() for line in soul_content.splitlines() if line.strip().startswith("- ")]
    recent_soul_items = soul_lines[-3:]
    recent_memories = sorted(
        st_entries,
        key=lambda entry: (str(entry.get("last_mentioned", "")), int(entry.get("mention_count", 0))),
        reverse=True,
    )[:6]

    today_entries = [
        entry for entry in st_entries
        if str(entry.get("last_mentioned", "")).strip() == today
    ]
    learned_today = sorted(
        today_entries,
        key=lambda entry: (int(entry.get("mention_count", 0)), abs(int(entry.get("emotional_valence", 0)))),
        reverse=True,
    )[:4]

    session_msgs: list[dict[str, Any]] = []
    if STATE_FILE.exists():
        try:
            session_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            session_msgs = session_state.get("messages", []) if isinstance(session_state, dict) else []
        except Exception:
            session_msgs = []
    session_usage = _usage_totals(session_msgs)
    subagent_usage = _merge_usage_totals(*[
        _usage_totals(info.get("messages", []))
        for info in _registry.values()
    ])
    combined_usage = _merge_usage_totals(session_usage, subagent_usage)

    reminder_items = []
    for task in sorted(tasks, key=lambda item: str(item.get("next_run") or "")):
        next_run = str(task.get("next_run") or "").strip()
        status = str(task.get("status") or "").strip()
        if not next_run or status not in {"active", "paused"}:
            continue
        reminder_items.append({
            "id": str(task.get("id") or ""),
            "prompt": str(task.get("prompt") or "").strip(),
            "next_run": next_run,
            "schedule_type": str(task.get("schedule_type") or "").strip(),
            "status": status,
        })
    reminder_items = reminder_items[:6]

    archive_snippets: list[dict[str, Any]] = []
    for filepath in sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)[:7]:
        date_str = filepath.stem
        try:
            sections = _parse_archive_sections(filepath.read_text(encoding="utf-8"))
        except Exception:
            continue
        for section in reversed(sections):
            user_body = str(section.get("user_body", "")).strip()
            assistant_body = str(section.get("assistant_body", "")).strip()
            if user_body or assistant_body:
                archive_snippets.append({
                    "date": date_str,
                    "title": str(section.get("round_title") or section.get("session_title") or "").strip(),
                    "user": user_body,
                    "assistant": assistant_body,
                })
    archive_snippets = archive_snippets[:6]

    hist_days = 27
    day_from = (now_local - timedelta(days=hist_days)).strftime("%Y-%m-%d")
    day_to = today
    stats_rows = await cy_db.get_daily_stats_range(_db_path, day_from, day_to)
    stats_by_day = {
        str(row.get("day") or ""): row
        for row in stats_rows
        if str(row.get("day") or "").strip()
    }
    model_stats_rows = await cy_db.get_model_stats_range(_db_path, day_from, day_to)
    topic_rows = await cy_db.get_topic_counts_range(_db_path, day_from, day_to, limit=18)
    archive_day_count = await cy_db.count_stat_days(_db_path)

    # 从 daily_stats 汇总全量历史数据（与 timeline 同源）
    historical_prompt = sum((r.get("prompt_tokens") or 0) for r in stats_by_day.values())
    historical_completion = sum((r.get("completion_tokens") or 0) for r in stats_by_day.values())
    historical_total = sum((r.get("total_tokens") or 0) for r in stats_by_day.values())
    historical_cache_hit = sum((r.get("cache_hit_tokens") or 0) for r in stats_by_day.values())
    historical_cache_miss = sum((r.get("cache_miss_tokens") or 0) for r in stats_by_day.values())
    historical_requests = sum((r.get("llm_requests") or 0) for r in stats_by_day.values())

    # 按模型计算总花费（不同模型定价不同）
    total_spend = 0.0
    for row in model_stats_rows:
        mdl = str(row.get("model") or "").strip().lower()
        pt = int(row.get("prompt_tokens") or 0)
        ct = int(row.get("completion_tokens") or 0)
        if "opus-4" in mdl:
            total_spend += (pt / 1_000_000) * 15.0 + (ct / 1_000_000) * 75.0
        elif "sonnet-4" in mdl:
            total_spend += (pt / 1_000_000) * 3.0 + (ct / 1_000_000) * 15.0
        elif "haiku-4" in mdl:
            total_spend += (pt / 1_000_000) * 0.25 + (ct / 1_000_000) * 1.25
        elif "deepseek-v4-flash" in mdl:
            total_spend += (pt / 1_000_000) * 0.14 + (ct / 1_000_000) * 0.28
        elif "deepseek-reasoner" in mdl:
            total_spend += (pt / 1_000_000) * 0.55 + (ct / 1_000_000) * 2.19
        elif "deepseek" in mdl or "deepseek-chat" in mdl:
            total_spend += (pt / 1_000_000) * 0.14 + (ct / 1_000_000) * 0.28
        else:
            total_spend += (pt / 1_000_000) * 1.0 + (ct / 1_000_000) * 2.0
    spend_str = "<$0.01" if total_spend < 0.01 else f"${total_spend:.2f}"

    # 情感数据从 short_term 条目按 last_mentioned 日期聚合，不依赖数据库
    emotion_by_day: dict[str, list[float]] = {}
    for entry in st_entries:
        day = str(entry.get("last_mentioned", "")).strip()
        if day:
            valence = int(entry.get("emotional_valence", 0) or 0)
            emotion_by_day.setdefault(day, []).append(valence)

    emotion_series = []
    for offset in range(hist_days, -1, -1):
        day = (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        vals = emotion_by_day.get(day, [])
        avg = round(sum(vals) / len(vals), 2) if vals else 0.0
        emotion_series.append({
            "date": day,
            "value": avg,
            "count": len(vals),
        })

    token_timeline: dict[str, dict[str, int]] = {}
    for offset in range(hist_days, -1, -1):
        day = (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day) or {}
        token_timeline[day] = {
            "prompt": int(row.get("prompt_tokens") or 0),
            "completion": int(row.get("completion_tokens") or 0),
            "requests": int(row.get("llm_requests") or 0),
        }

    heatmap_days = [
        (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(hist_days, -1, -1)
    ]
    heatmap_row_defs = [
        ("00:00", 0, 4),
        ("04:00", 4, 8),
        ("08:00", 8, 12),
        ("12:00", 12, 16),
        ("16:00", 16, 20),
        ("20:00", 20, 24),
    ]
    heatmap_column_map = {
        "00:00": "activity_00_04",
        "04:00": "activity_04_08",
        "08:00": "activity_08_12",
        "12:00": "activity_12_16",
        "16:00": "activity_16_20",
        "20:00": "activity_20_24",
    }
    heatmap_buckets: dict[str, list[int]] = {}
    for label, _, _ in heatmap_row_defs:
        column = heatmap_column_map[label]
        heatmap_buckets[label] = [
            int((stats_by_day.get(day) or {}).get(column) or 0)
            for day in heatmap_days
        ]

    activity_heatmap = {
        "days": heatmap_days,
        "rows": [
            {"label": label, "values": heatmap_buckets[label]}
            for label, _, _ in heatmap_row_defs
        ],
    }

    return {
        "today": {
            "learned": learned_today,
            "learned_count": len(today_entries),
            "memory_count": len(st_entries),
            "archive_days": archive_day_count,
        },
        "soul": {
            "path": str(soul_path),
            "updated_at": datetime.fromtimestamp(soul_stat.st_mtime, tz=timezone.utc).isoformat() if soul_stat else "",
            "recent_items": recent_soul_items,
            "section_count": soul_content.count("\n## ") + (1 if soul_content.strip().startswith("# ") else 0),
        },
        "topic_cloud": topic_rows,
        "emotion": emotion_series,
        "usage": {
            "requests": historical_requests,
            "tokens": _format_tokens({
                "prompt_tokens": historical_prompt,
                "completion_tokens": historical_completion,
                "total_tokens": historical_total,
            }),
            "spend": spend_str,
            "prompt_tokens": historical_prompt,
            "completion_tokens": historical_completion,
            "total_tokens": historical_total,
            "cache_hit_tokens": historical_cache_hit,
            "cache_miss_tokens": historical_cache_miss,
            "total_messages": (session_usage.get("requests") or 0) + (subagent_usage.get("requests") or 0),
            "active_days": sum(1 for row in stats_by_day.values() if int(row.get("llm_requests") or 0) > 0),
            "current_streak": _calc_current_streak(stats_by_day, today),
            "longest_streak": _calc_longest_streak(stats_by_day),
            "peak_hour": _calc_peak_hour(stats_by_day),
            "timeline": [
                {
                    "date": day,
                    "prompt": values["prompt"],
                    "completion": values["completion"],
                    "requests": values["requests"],
                }
                for day, values in token_timeline.items()
            ],
        },
        "reminders": reminder_items,
        "recent_memories": recent_memories,
        "recent_archive": archive_snippets,
        "activity_heatmap": activity_heatmap,
        "model_stats": model_stats_rows,
    }


def _extract_topic_terms(text: str, limit: int = 12) -> list[str]:
    """Extract simple high-signal topic terms from mixed Chinese/English text."""
    source = (text or "").lower()
    english_stop = {
        "the", "and", "for", "that", "this", "with", "from", "have", "about",
        "what", "when", "your", "just", "into", "then", "they", "them", "their",
        "would", "could", "should", "there", "here", "been", "were", "will",
        "some", "more", "than", "after", "before", "need", "want", "like",
        "today", "yesterday", "tomorrow", "really", "also", "maybe", "because",
        "http", "https", "assistant", "cyrene", "user",
    }
    chinese_stop = {
        "今天", "最近", "这个", "那个", "一下", "已经", "我们", "你们", "然后",
        "需要", "可以", "还是", "就是", "一个", "没有", "什么", "怎么", "如果",
        "现在", "自己", "因为", "所以", "以及", "但是", "进行", "相关", "问题",
        "工作", "页面", "功能", "内容",
    }
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_-]{2,}", source)
    results: list[str] = []
    for token in tokens:
        if token in english_stop or token in chinese_stop:
            continue
        if token.isascii() and len(token) < 4:
            continue
        results.append(token)
        if len(results) >= limit:
            break
    return results


def _read_recent_logs() -> list[dict]:
    """Read the most recent debug log file and convert to status log rows."""
    from cyrene.config import DATA_DIR
    if not DATA_DIR.exists():
        return _placeholder_logs()
    log_files = sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)
    if not log_files:
        return _placeholder_logs()
    latest = log_files[0]
    rows: list[dict] = []
    try:
        with open(latest, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return _placeholder_logs()
    for line in lines[-40:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        kind = entry.get("type", "info")
        ts = entry.get("timestamp", "")[11:19]
        if kind == "llm_call":
            caller = entry.get("caller", "?")
            phase = entry.get("phase", "?")
            duration = entry.get("duration_ms", 0)
            rows.append({"t": ts, "lvl": "info", "msg": f"{caller} · {phase} · {duration}ms"})
        elif kind == "tool_call":
            caller = entry.get("caller", "?")
            tool = entry.get("tool", "?")
            rows.append({"t": ts, "lvl": "ok", "msg": f"{caller} → {tool}"})
        elif kind == "session_start":
            rows.append({"t": ts, "lvl": "info", "msg": "session started"})
    return list(reversed(rows[-20:]))


def _placeholder_logs() -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return [{"t": now, "lvl": "info", "msg": "no debug logs yet — verbose mode is enabled, logs appear after agent runs"}]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _build_settings_meta() -> dict:
    return {
        "sections": [
            {"id": "general", "label": "General"},
            {"id": "channels", "label": "Channels"},
            {"id": "models", "label": "Models"},
            {"id": "agents", "label": "Agents"},
            {"id": "appearance", "label": "Appearance"},
            {"id": "capabilities", "label": "Capabilities"},
            {"id": "data", "label": "Data"},
            {"id": "about", "label": "About"},
        ],
    }


def _build_config() -> dict:
    settings = get_web_settings()
    live_model, live_base_url = _live_llm_config()
    return {
        "model": live_model,
        "base_url": live_base_url,
        "assistant_name": ASSISTANT_NAME,
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "soul_path": str(SOUL_PATH),
        "workspace_dir": str(WORKSPACE_DIR),
        "soul_content": _read_soul(),
        "search_mode": "builtin",
        "search_external_url": "",
        "spawn_policy": settings.get("spawn_policy", "conservative"),
        "heartbeat_interval": settings.get("heartbeat_interval", 1800),
        "agent_proactive": settings.get("agent_proactive", True),
        "max_tool_rounds": settings.get("max_tool_rounds", 15),
        "notify_telegram": settings.get("notify_telegram", True),
        "notify_wechat": settings.get("notify_wechat", True),
        "redact_secrets": settings.get("redact_secrets", True),
        "search_port": str(SEARXNG_PORT),
        "search_host": SEARXNG_HOST,
    }


def _build_context_chips() -> list[dict]:
    """Build context chips reflecting current SOUL.md and workspace state."""
    from cyrene.settings_store import is_workspace_active, is_soul_active
    chips = []
    if is_soul_active():
        chips.append({"icon": "🧠", "label": "SOUL.md", "key": "soul"})
    if is_workspace_active():
        chips.append({"icon": "📁", "label": "workspace", "key": "workspace"})
    return chips


def _build_search_config() -> dict:
    return {
        "search_mode": "builtin",
        "search_external_url": "",
        "auto_start_enabled": os.getenv("SEARXNG_AUTO_START", "1") not in ("0", "false", "no"),
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _load_messages() -> list[dict]:
    msgs = _load_state_messages()
    if msgs:
        result = []
        for m in msgs:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if not content or not content.strip():
                continue
            result.append({"role": role, "content": content})
        if result:
            return result

    archive_msgs = _parse_conversation_archive()
    if archive_msgs:
        return archive_msgs

    return []


def _load_state_messages() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("messages", []) or []
    except Exception:
        return []


def _infer_subagent_entries(raw_msgs: list[dict], registry: dict[str, dict]) -> dict[str, dict]:
    entries: dict[str, dict] = _snapshot_entries_from_messages(raw_msgs)
    for agent_id, info in registry.items():
        _merge_subagent_record(entries, agent_id, dict(info))
    for entry in entries.values():
        entry.setdefault("messages", [])

    spawned: dict[str, dict[str, str]] = {}
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            spawned[agent_id] = {
                "task": str(args.get("task") or ""),
                "round_id": str(msg.get("round_id", "")).strip(),
            }

    for agent_id, meta in spawned.items():
        entry = entries.setdefault(agent_id, {})
        meta_round_id = str(meta.get("round_id", "")).strip()
        existing_round_id = str(entry.get("round_id", "")).strip()
        if meta_round_id and existing_round_id and meta_round_id != existing_round_id:
            # Treat a reused agent ID in a later round as a fresh live subagent.
            entry["task"] = meta["task"] or entry.get("task", "")
            entry["round_id"] = meta_round_id
            entry["status"] = "running"
            entry["result"] = ""
            entry["messages"] = []
            entry["created_at"] = None
            entry["updated_at"] = None
            continue
        entry.setdefault("task", meta["task"])
        entry.setdefault("round_id", meta_round_id)
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        entry.setdefault("messages", [])
        entry.setdefault("created_at", None)
        entry.setdefault("updated_at", None)

    inbox_meta = _scan_inbox_agents()
    for agent_id, meta in inbox_meta.items():
        entry = entries.setdefault(agent_id, {})
        entry.setdefault("task", spawned.get(agent_id, {}).get("task", "Discuss with other subagents"))
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        if not entry.get("messages"):
            entry["messages"] = [{}] * int(meta.get("message_count") or 0)
        if meta.get("created_at") and not entry.get("created_at"):
            entry["created_at"] = meta["created_at"]
        if meta.get("updated_at") and not entry.get("updated_at"):
            entry["updated_at"] = meta["updated_at"]
        if meta.get("round_id") and not entry.get("round_id"):
            entry["round_id"] = meta["round_id"]

    return entries


def _parse_conversation_archive() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = CONVERSATIONS_DIR / f"{today}.md"
    if not filepath.exists():
        return []
    content = filepath.read_text(encoding="utf-8")
    messages = []
    current_user = None
    current_lines: list[str] = []
    in_assistant = False
    for line in content.split("\n"):
        if line.startswith("**User**: "):
            if current_user and current_lines:
                messages.append({"role": "user", "content": current_user})
                messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
            current_user = line[len("**User**: "):].strip()
            current_lines = []
            in_assistant = False
        elif line.startswith("**") and "**: " in line and not line.startswith("**User**"):
            in_assistant = True
            idx = line.index("**: ")
            current_lines = [line[idx + len("**: "):]]
        elif in_assistant:
            if line.strip() == "---":
                if current_user and current_lines:
                    messages.append({"role": "user", "content": current_user})
                    messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
                current_user = None
                current_lines = []
                in_assistant = False
            else:
                current_lines.append(line)
    if current_user and current_lines:
        messages.append({"role": "user", "content": current_user})
        messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
    return messages


def _read_soul() -> str:
    try:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


def _status_progress(status: str) -> float:
    return {
        "running": 0.45,
        "resumed": 0.65,
        "waiting": 0.82,
        "done": 1.0,
        "timeout": 1.0,
    }.get(status, 0.5)


def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except Exception:
        return "—"


def _elapsed_since(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return _format_duration((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return "—"


def _safe_json_loads(value: str) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def _summarize_text(value: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _tool_output_map(raw_messages: list[dict]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for msg in raw_messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            outputs[str(msg["tool_call_id"])] = str(msg.get("content") or "")
    return outputs


def _tool_output_ids(raw_messages: list[dict]) -> set[str]:
    return {
        str(msg["tool_call_id"])
        for msg in raw_messages
        if msg.get("role") == "tool" and msg.get("tool_call_id")
    }


def _tool_args_signature(value: Any) -> str:
    parsed = _safe_json_loads(value) if isinstance(value, str) else value
    normalized = parsed if parsed is not None else value
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(str(normalized), ensure_ascii=False)


def _usage_totals(raw_messages: list[dict]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "requests": 0,
    }
    found = False
    for msg in raw_messages:
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        totals["requests"] = int(totals["requests"] or 0) + 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] = int(totals[key] or 0) + value
                found = True
    if not found and not totals["requests"]:
        return {key: None for key in totals}
    if not totals["total_tokens"] and (totals["prompt_tokens"] or totals["completion_tokens"]):
        totals["total_tokens"] = int(totals["prompt_tokens"] or 0) + int(totals["completion_tokens"] or 0)
    return totals


def _last_request_context_tokens(raw_msgs: list[dict]) -> int | None:
    """Tokens of the most recent LLM request — approximates current context occupancy.

    Unlike _usage_totals (which sums every request in the session), this returns the
    last request's own token count, so it reflects how full the context window is now.
    """
    for msg in reversed(raw_msgs):
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            return total
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int) and prompt > 0:
            completion = usage.get("completion_tokens")
            return prompt + (completion if isinstance(completion, int) else 0)
    return None


def _merge_usage_totals(*usage_items: dict[str, int | None]) -> dict[str, int | None]:
    merged = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "requests": 0,
    }
    found = False
    for usage in usage_items:
        if not isinstance(usage, dict):
            continue
        for key in merged:
            value = usage.get(key)
            if isinstance(value, int):
                merged[key] += value
                found = True
    if not found:
        return {key: None for key in merged}
    if not merged["total_tokens"] and (merged["prompt_tokens"] or merged["completion_tokens"]):
        merged["total_tokens"] = merged["prompt_tokens"] + merged["completion_tokens"]
    return merged


def _format_tokens(usage: dict[str, int | None] | None) -> str:
    if not isinstance(usage, dict):
        return "—"
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    parts: list[str] = []
    if prompt_tokens is not None:
        parts.append(f"{_fmt_tok(prompt_tokens)} in")
    if completion_tokens is not None:
        parts.append(f"{_fmt_tok(completion_tokens)} out")
    if total_tokens is not None:
        parts.append(f"{_fmt_tok(total_tokens)} total")
    return " / ".join(parts) if parts else "—"


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _model_pricing() -> dict[str, float] | None:
    """Return token pricing metadata for known models, or None."""
    model_lower = _get_model().lower()
    if "opus-4" in model_lower or "claude-opus-4" in model_lower:
        return {"input": 15.0, "output": 75.0}
    if "sonnet-4" in model_lower or "claude-sonnet-4" in model_lower:
        return {"input": 3.0, "output": 15.0}
    if "haiku-4" in model_lower or "claude-haiku-4" in model_lower:
        return {"input": 0.25, "output": 1.25}
    if "deepseek-v4-flash" in model_lower:
        return {"input": 0.14, "output": 0.28, "cache_hit": 0.0}
    if "deepseek-v4" in model_lower or "deepseek-chat" in model_lower:
        return {"input": 0.14, "output": 0.28, "cache_hit": 0.05}
    if "deepseek-reasoner" in model_lower:
        return {"input": 0.55, "output": 2.19, "cache_hit": 0.14}
    return None


def _calc_spend(usage: dict[str, int | None] | None) -> str:
    if not isinstance(usage, dict):
        return "—"
    pricing = _model_pricing()
    if pricing is None:
        return "—"
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    input_price = pricing["input"]
    output_price = pricing["output"]
    cache_hit_price = pricing.get("cache_hit", input_price)
    cost = 0.0
    if isinstance(cache_hit_tokens, int) and isinstance(cache_miss_tokens, int) and (cache_hit_tokens or cache_miss_tokens):
        cost += (cache_hit_tokens / 1_000_000) * cache_hit_price
        cost += (cache_miss_tokens / 1_000_000) * input_price
    elif prompt_tokens is not None:
        cost += (prompt_tokens / 1_000_000) * input_price
    if completion_tokens is not None:
        cost += (completion_tokens / 1_000_000) * output_price
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _calc_current_streak(stats_by_day: dict[str, dict], today: str) -> int:
    streak = 0
    for offset in range(366):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day)
        if row and int(row.get("llm_requests") or 0) > 0:
            streak += 1
        else:
            break
    return streak


def _calc_longest_streak(stats_by_day: dict[str, dict]) -> int:
    longest = 0
    current = 0
    for offset in range(365):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day)
        if row and int(row.get("llm_requests") or 0) > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


_ACTIVITY_COLUMNS = [
    ("activity_00_04", "00:00-04:00"),
    ("activity_04_08", "04:00-08:00"),
    ("activity_08_12", "08:00-12:00"),
    ("activity_12_16", "12:00-16:00"),
    ("activity_16_20", "16:00-20:00"),
    ("activity_20_24", "20:00-24:00"),
]


def _calc_peak_hour(stats_by_day: dict[str, dict]) -> str:
    totals: dict[str, int] = {}
    for col, _label in _ACTIVITY_COLUMNS:
        totals[col] = sum(int(row.get(col) or 0) for row in stats_by_day.values())
    best_col = max(totals, key=totals.get) if any(totals.values()) else ""
    for col, label in _ACTIVITY_COLUMNS:
        if col == best_col:
            return label
    return "—"


def _build_shells_from_messages(raw_msgs: list[dict]) -> list[dict]:
    """Extract bash/shell tool calls from raw messages and build shell entries."""
    shells: list[dict] = []
    tool_results: dict[str, str] = {}
    for msg in raw_msgs:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_results[str(msg["tool_call_id"])] = str(msg.get("content") or "")

    shell_index = 0
    for msg in raw_msgs:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name.lower() not in ("bash", "shell", "cmd", "terminal"):
                continue
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            cmd = args.get("command") or args.get("cmd") or json.dumps(args)
            cwd = args.get("cwd") or args.get("workdir") or "workspace/"
            result = tool_results.get(str(tc.get("id")), "")
            lines: list[dict] = [
                {"kind": "shell-prompt", "text": f"$ {cmd}"},
            ]
            if result:
                for line in result.strip().split("\n")[:30]:
                    lines.append({"kind": "shell-out", "text": line})
            else:
                lines.append({"kind": "shell-out", "text": "(running…)"})

            shells.append({
                "id": f"shell_{shell_index}",
                "cwd": cwd,
                "pid": "—",
                "lines": lines,
            })
            shell_index += 1

    return shells


def _build_tool_nodes_for_owner(
    owner_node_id: str,
    owner_title: str,
    owner_x: int,
    owner_y: int,
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
    x: int,
    base_y: int,
    owner_completed: bool = False,
) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    tool_outputs = _tool_output_map(raw_messages)
    tool_output_ids = _tool_output_ids(raw_messages)
    tool_index = 0

    for msg_index, msg in enumerate(raw_messages):
        tool_calls = msg.get("tool_calls") or []
        for call_index, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            parsed_args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
            tool_call_id = str(tc.get("id") or "")
            output = tool_outputs.get(tool_call_id, "")
            has_output = tool_call_id in tool_output_ids
            has_followup = any(
                later.get("role") in {"assistant", "tool", "user"}
                for later in raw_messages[msg_index + 1:]
            )
            status = "done" if has_output or has_followup or owner_completed else "running"
            if has_output:
                output_detail = output or "Completed with no captured output."
            elif status == "done":
                output_detail = "Completed after follow-up activity; no tool output was captured."
            else:
                output_detail = "Running…"
            nid = f"{owner_node_id}_tool_{msg_index}_{call_index}"
            nodes.append({
                "id": nid,
                "kind": "tool",
                "x": x,
                "y": base_y + tool_index * 112,
                "title": fn.get("name", "tool"),
                "subtitle": _summarize_text(str(raw_args), 36) if raw_args else "",
                "status": status,
                "detail": {
                    "name": fn.get("name", "tool"),
                    "owner": owner_title,
                    "input": parsed_args if parsed_args is not None else raw_args,
                    "output": output_detail,
                    "duration": "—",
                },
            })
            edges.append({
                "from": owner_node_id,
                "to": nid,
                "kind": "active" if status == "running" else None,
            })
            tool_index += 1

    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    for event_index, event in enumerate(overlay_events):
        event_signature = _tool_args_signature(event.get("args", {}))
        if any(
            node["detail"].get("name") == event.get("tool")
            and _tool_args_signature(node["detail"].get("input", {})) == event_signature
            for node in nodes
        ):
            continue
        nid = f"{owner_node_id}_live_tool_{event_index}"
        nodes.append({
            "id": nid,
            "kind": "tool",
            "x": x,
            "y": base_y + tool_index * 112,
            "title": event.get("tool", "tool"),
            "subtitle": _summarize_text(json.dumps(event.get("args", {}), ensure_ascii=False), 36),
            "status": "done",
            "detail": {
                "name": event.get("tool", "tool"),
                "owner": owner_title,
                "input": event.get("args", {}),
                "output": event.get("result_preview", "Completed."),
                "duration": "recent",
                "eventKey": f"{event.get('tool')}::{event_signature}",
            },
        })
        edges.append({"from": owner_node_id, "to": nid})
        tool_index += 1

    return nodes, edges


def _count_tool_nodes_for_owner(
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
) -> int:
    count = sum(len(msg.get("tool_calls") or []) for msg in raw_messages)
    message_keys = {
        (
            tc.get("function", {}).get("name", "tool"),
            json.dumps(
                _safe_json_loads(tc.get("function", {}).get("arguments") or "{}")
                if isinstance(tc.get("function", {}).get("arguments"), str)
                else (tc.get("function", {}).get("arguments") or {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for msg in raw_messages
        for tc in (msg.get("tool_calls") or [])
    }
    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    overlay_count = 0
    for event in overlay_events:
        event_key = (
            event.get("tool", "tool"),
            json.dumps(event.get("args", {}), ensure_ascii=False, sort_keys=True),
        )
        if event_key in message_keys:
            continue
        overlay_count += 1
    return count + overlay_count


def _agent_lane_height(tool_count: int) -> int:
    base_height = 86
    if tool_count <= 0:
        return base_height
    return max(base_height, base_height + (tool_count - 1) * 112)


def _build_comm_edges(
    agent_node_ids: dict[str, str],
    agent_entries: dict[str, dict[str, Any]] | None = None,
    round_id: str = "",
    persisted_messages: list[dict[str, Any]] | None = None,
) -> list[dict]:
    edges: list[dict] = []
    if not agent_node_ids:
        return edges

    # Track per-pair messages for threading and weight
    pair_messages: dict[tuple[str, str], list[dict]] = {}
    pair_index: dict[tuple[str, str, str, str], int] = {}
    # Map to deduplicate: (from_agent, to_agent, content[:80]) -> edge_index
    content_index: dict[tuple[str, str, str], int] = {}

    def _add_message_to_pair(
        from_agent: str,
        to_agent: str,
        body: str,
        *,
        label: str = "chat",
        timestamp: str = "",
        source: str = "",
        summary: str = "",
        priority: str = "normal",
        raw_timestamp: str = "",
    ) -> None:
        if from_agent not in agent_node_ids or to_agent not in agent_node_ids:
            return
        if not body.strip():
            return

        pair_key = (from_agent, to_agent)
        content_key = (from_agent, to_agent, body[:80])

        if content_key in content_index:
            # Update existing edge with richer metadata
            idx = content_index[content_key]
            existing_msg = edges[idx].setdefault("message", {})
            if (not existing_msg.get("time") or existing_msg.get("time") == "—") and timestamp:
                existing_msg["time"] = _short_time(timestamp)
            if summary and not existing_msg.get("summary"):
                existing_msg["summary"] = summary
            if priority == "high":
                existing_msg["priority"] = "high"
            # Increment weight even for duplicates (counts total messages)
            edges[idx]["weight"] = edges[idx].get("weight", 1) + 1
            pair_messages.setdefault(pair_key, []).append({
                "from": from_agent,
                "to": to_agent,
                "body": body,
                "label": label,
                "time": _short_time(timestamp) if timestamp else "—",
                "summary": summary,
                "priority": priority,
                "source": source,
            })
            return

        edge_summary = summary if summary else _summarize_text(body, 90)
        edge_label = label
        if priority == "high":
            edge_label = label + " !"

        edge_entry = {
            "from": agent_node_ids[from_agent],
            "to": agent_node_ids[to_agent],
            "kind": "comm",
            "label": edge_label,
            "weight": 1,
            "message": {
                "time": _short_time(timestamp) if timestamp else "—",
                "raw_timestamp": raw_timestamp or timestamp or "",
                "summary": edge_summary,
                "body": body,
                "source": source or "tool_call",
                "msg_type": label,
                "priority": priority,
            },
        }
        edges.append(edge_entry)
        content_index[content_key] = len(edges) - 1
        pair_messages.setdefault(pair_key, []).append({
            "from": from_agent,
            "to": to_agent,
            "body": body,
            "label": label,
            "time": _short_time(timestamp) if timestamp else "—",
            "raw_timestamp": raw_timestamp or timestamp or "",
            "summary": edge_summary,
            "priority": priority,
            "source": source,
        })

    for agent_name, info in (agent_entries or {}).items():
        if agent_name not in agent_node_ids:
            continue
        messages = info.get("messages", []) or []
        tool_outputs = {
            str(msg.get("tool_call_id") or ""): str(msg.get("content") or "")
            for msg in messages
            if isinstance(msg, dict) and msg.get("role") == "tool" and msg.get("tool_call_id")
        }
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                tool_name = str(fn.get("name") or "").strip()
                if tool_name not in ("send_agent_message", "broadcast_agent_message"):
                    continue
                args = _safe_json_loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    continue
                output = tool_outputs.get(str(tc.get("id") or ""), "")
                output_lower = output.lower()
                if output and "message sent to" not in output_lower and "broadcast sent to" not in output_lower:
                    continue
                body = str(args.get("content") or "")
                if tool_name == "broadcast_agent_message":
                    # Broadcast edges go to each peer
                    peer_ids = [aid for aid in agent_node_ids if aid != agent_name]
                    for peer_id in peer_ids:
                        _add_message_to_pair(agent_name, peer_id, body, label="progress", source="tool_call")
                else:
                    to_agent = str(args.get("to") or "").strip()
                    _add_message_to_pair(agent_name, to_agent, body, source="tool_call")

    for payload in persisted_messages or []:
        if not isinstance(payload, dict):
            continue
        if round_id and str(payload.get("round_id", "")).strip() != round_id:
            continue
        _add_message_to_pair(
            str(payload.get("from", "")).strip(),
            str(payload.get("to", "")).strip(),
            str(payload.get("content", "")),
            label=str(payload.get("type", "chat") or "chat"),
            timestamp=str(payload.get("timestamp", "") or ""),
            source="snapshot_log",
            summary=str(payload.get("summary", "") or ""),
            priority=str(payload.get("priority", "normal") or "normal"),
        )

    for agent_name in agent_node_ids:
        inbox_dir = DATA_DIR / "inbox" / agent_name
        if not inbox_dir.exists():
            continue
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            _add_message_to_pair(
                from_agent,
                to_agent,
                str(payload.get("content", "")),
                label=str(payload.get("type", "chat") or "chat"),
                timestamp=str(payload.get("timestamp", "") or ""),
                source="inbox_log",
                summary=str(payload.get("summary", "") or ""),
                priority=str(payload.get("priority", "normal") or "normal"),
            )

    # Attach all messages for each pair to the edge
    for i, edge in enumerate(edges):
        pair = None
        for (f, t), msgs in pair_messages.items():
            if edge["from"] == agent_node_ids.get(f) and edge["to"] == agent_node_ids.get(t):
                pair = (f, t)
                edge["messages"] = msgs
                break
        if pair:
            edge["weight"] = len(pair_messages.get(pair, []))

    return edges


def _scan_inbox_agents() -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return agents

    for inbox_dir in sorted(path for path in inbox_root.iterdir() if path.is_dir()):
        agent_id = inbox_dir.name
        timestamps: list[str] = []
        round_ids: list[str] = []
        msg_count = 0
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            msg_count += 1
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                timestamps.append(timestamp)
            round_id = str(payload.get("round_id", "")).strip()
            if round_id:
                round_ids.append(round_id)

        if msg_count == 0:
            continue

        timestamps.sort()
        agents[agent_id] = {
            "message_count": msg_count,
            "created_at": timestamps[0] if timestamps else None,
            "updated_at": timestamps[-1] if timestamps else None,
            "round_id": round_ids[-1] if round_ids else "",
        }

    return agents
