"""Map pin tools — allows the agent to mark locations and connect them on a map."""

import json
import uuid


def _state_get(state: dict, key: str, default=None):
    """Access a key in the session state dict with lazy imports."""
    from cyrene.agent.session import _load_session_state, _write_session_state

    state = _load_session_state()
    val = state.get(key, default or [])
    return state, val


PIN_LOCATION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "pin_location",
        "description": (
            "在地图上标记一个地点。标记后会出现在右侧边栏地图上。"
            "之后再使用 connect_pins 工具在两个标记之间建立路线连接。"
            "支持添加 Markdown 注释。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {
                    "type": "number",
                    "description": "纬度，例如 39.9042",
                },
                "lng": {
                    "type": "number",
                    "description": "经度，例如 116.4074",
                },
                "name": {
                    "type": "string",
                    "description": "地点名称，例如 北京",
                },
                "note": {
                    "type": "string",
                    "description": "关于该地点的 Markdown 注释（可选），用户点击标记会看到此内容",
                },
            },
            "required": ["lat", "lng", "name"],
        },
    },
}

CONNECT_PINS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "connect_pins",
        "description": (
            "在两个已有的标记点之间创建路线连接。"
            "标记点必须已通过 pin_location 创建，通过名称引用。"
            "支持添加交通方式和 Markdown 说明，用户点击路线会看到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "起点标记的名称，必须与 pin_location 创建的 name 一致",
                },
                "to_name": {
                    "type": "string",
                    "description": "终点标记的名称，必须与 pin_location 创建的 name 一致",
                },
                "transport": {
                    "type": "string",
                    "description": "交通方式（可选），例如 飞机、高铁、驾车、步行",
                },
                "route_note": {
                    "type": "string",
                    "description": "路线的 Markdown 说明（可选），用户点击路线会看到",
                },
            },
            "required": ["from_name", "to_name"],
        },
    },
}


async def _tool_pin_location(
    args: dict,
    bot=None,
    chat_id=None,
    db_path=None,
    notify_state: dict | None = None,
) -> str:
    from cyrene.agent.session import _load_session_state, _write_session_state
    from cyrene import debug

    lat = float(args["lat"])
    lng = float(args["lng"])
    name = str(args.get("name", ""))
    note = str(args.get("note", ""))

    state = _load_session_state()
    pins: list[dict] = state.get("map_pins", [])
    routes: list[dict] = state.get("map_routes", [])

    pin = {
        "id": f"pin_{uuid.uuid4().hex[:8]}",
        "lat": lat,
        "lng": lng,
        "name": name,
        "note_md": note,
        "order": len(pins),
    }

    pins.append(pin)
    state["map_pins"] = pins
    _write_session_state(state)

    await debug.publish_event({
        "type": "map_pin",
        "pins": pins,
        "routes": routes,
    })

    return json.dumps(
        {
            "status": "ok",
            "pin_id": pin["id"],
            "total_pins": len(pins),
            "name": name,
        },
        ensure_ascii=False,
    )


async def _tool_connect_pins(
    args: dict,
    bot=None,
    chat_id=None,
    db_path=None,
    notify_state: dict | None = None,
) -> str:
    from cyrene.agent.session import _load_session_state, _write_session_state
    from cyrene import debug

    from_name = str(args["from_name"])
    to_name = str(args["to_name"])
    transport = str(args.get("transport", ""))
    route_note = str(args.get("route_note", ""))

    state = _load_session_state()
    pins: list[dict] = state.get("map_pins", [])
    routes: list[dict] = state.get("map_routes", [])

    # Validate that both pins exist.
    pin_names = {p["name"] for p in pins if p.get("name")}
    if from_name not in pin_names:
        return json.dumps({"status": "error", "message": f"未找到起点标记「{from_name}」"}, ensure_ascii=False)
    if to_name not in pin_names:
        return json.dumps({"status": "error", "message": f"未找到终点标记「{to_name}」"}, ensure_ascii=False)

    route = {
        "id": f"route_{uuid.uuid4().hex[:8]}",
        "from_name": from_name,
        "to_name": to_name,
        "transport": transport,
        "note_md": route_note,
    }
    routes.append(route)
    state["map_routes"] = routes
    _write_session_state(state)

    await debug.publish_event({
        "type": "map_pin",
        "pins": pins,
        "routes": routes,
    })

    return json.dumps(
        {
            "status": "ok",
            "route_id": route["id"],
            "from": from_name,
            "to": to_name,
        },
        ensure_ascii=False,
    )


def register_to(tool_defs: list, tool_handlers: dict) -> None:
    tool_defs.append(PIN_LOCATION_TOOL_DEF)
    tool_handlers["pin_location"] = _tool_pin_location
    tool_defs.append(CONNECT_PINS_TOOL_DEF)
    tool_handlers["connect_pins"] = _tool_connect_pins
