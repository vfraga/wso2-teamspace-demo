from typing import Any

MEETING_PROPERTY_KEYS: tuple[str, ...] = (
    "topic",
    "date",
    "start_time",
    "duration",
    "time_zone",
)

MEETING_BASE_ARGS: dict[str, str] = {
    "topic": "Test Meeting",
    "date": "2026-05-22",
    "start_time": "14:00",
    "duration": "60",
    "time_zone": "America/Sao_Paulo",
}

_MCP_SCHEDULE_PREVIEW_PROPS: dict[str, dict[str, str]] = {
    "topic": {"type": "string", "description": "Meeting topic"},
    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
    "start_time": {"type": "string", "description": "Start time in HH:MM format"},
    "duration": {"type": "string", "description": "Duration in minutes"},
    "time_zone": {"type": "string", "description": "IANA timezone"},
}

_MCP_NO_DESCRIPTION_PROPS: dict[str, dict[str, str]] = {
    "topic": {"type": "string"},
    "date": {"type": "string"},
    "start_time": {"type": "string"},
    "duration": {"type": "string"},
    "time_zone": {"type": "string"},
}

_GENAI_SCHEDULE_PREVIEW_PROPS: dict[str, dict[str, str]] = {
    "topic": {"type": "string", "description": "Meeting topic"},
    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
    "start_time": {"type": "string", "description": "Start time in HH:MM 24h format"},
    "duration": {"type": "string", "description": "Duration in minutes"},
    "time_zone": {"type": "string", "description": "IANA timezone"},
}

_GENAI_UPDATE_PREVIEW_PROPS: dict[str, dict[str, str]] = {
    "topic": {"type": "string", "description": "Meeting topic"},
    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
    "start_time": {"type": "string", "description": "Start time in HH:MM 24h format"},
    "duration": {"type": "string", "description": "Duration in minutes"},
    "time_zone": {"type": "string", "description": "IANA timezone"},
}

_GENAI_NO_DESCRIPTION_PROPS: dict[str, dict[str, str]] = {
    "topic": {"type": "string"},
    "date": {"type": "string"},
    "start_time": {"type": "string"},
    "duration": {"type": "string"},
    "time_zone": {"type": "string"},
}

_VARIANT_BASES_RICH = {
    "mcp_schedule_preview": _MCP_SCHEDULE_PREVIEW_PROPS,
    "genai_schedule_preview": _GENAI_SCHEDULE_PREVIEW_PROPS,
    "genai_update_preview": _GENAI_UPDATE_PREVIEW_PROPS,
}

_VARIANT_BASES_BRIEF = {
    "mcp_schedule_preview": _MCP_NO_DESCRIPTION_PROPS,
    "genai_schedule_preview": _GENAI_NO_DESCRIPTION_PROPS,
    "genai_update_preview": _GENAI_NO_DESCRIPTION_PROPS,
}


def make_meeting_schema(
    *,
    brief: bool = False,
    with_thread_id: bool = False,
    with_meeting_id: bool = False,
    variant: str = "mcp_schedule_preview",
) -> dict[str, Any]:
    bases = _VARIANT_BASES_BRIEF if brief else _VARIANT_BASES_RICH
    if variant not in bases:
        raise ValueError(f"Unknown schema variant: {variant!r}")
    base = bases[variant]

    required: list[str] = []
    properties: dict[str, dict[str, str]] = {}

    if with_meeting_id:
        if brief:
            properties["meeting_id"] = {"type": "string"}
        elif variant.startswith("genai_"):
            properties["meeting_id"] = {
                "type": "string",
                "description": "ID of the meeting to update",
            }
        else:
            properties["meeting_id"] = {"type": "string"}
        required.append("meeting_id")

    for key in MEETING_PROPERTY_KEYS:
        properties[key] = base[key]
        required.append(key)

    if with_thread_id:
        if brief:
            properties["thread_id"] = {"type": "string"}
        else:
            properties["thread_id"] = {
                "type": "string",
                "description": "Active session thread ID",
            }
        required.append("thread_id")

    return {"type": "object", "properties": properties, "required": required}
