import asyncio
import html
import logging
import re
import threading
from datetime import datetime

from google import genai
from google.genai import types

from common.constants import DEFAULT_AGENT_NAME
from agent.tools import TOOL_DEFINITIONS, dispatch_tool
from agent.config import settings
from agent.state_manager import StateManager, FlowState


logger = logging.getLogger(__name__)


class GeminiClientFactory:
    _client: genai.Client | None = None
    _lock = threading.Lock()

    @classmethod
    def get_default(cls) -> genai.Client | None:
        if cls._client is None and settings.GEMINI_API_KEY:
            with cls._lock:
                if cls._client is None and settings.GEMINI_API_KEY:
                    cls._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return cls._client

# Bilingual message catalog — one place for en/pt copy.
MESSAGES: dict[str, dict[str, str]] = {
    "greeting": {
        "pt": "<p>Olá! Sou o Assistente de Reuniões do Teamspace. Experimente dizer <em>'Agendar uma reunião para amanhã às 15h'</em>.</p>",
        "en": "<p>Hi there! I am the Teamspace Meeting Assistant. Try saying <em>'Schedule a meeting for tomorrow at 2 PM'</em>.</p>",
    },
    "no_meetings": {
        "pt": "<p>Você não possui reuniões agendadas.</p>",
        "en": "<p>You have no scheduled meetings.</p>",
    },
    "not_authorized_prompt": {
        "pt": "<p>Não encontrei sua autorização. Por favor, autorize-me pelo link primeiro.</p>",
        "en": "<p>I couldn't find your authorization. Please authorize me using the link first.</p>",
    },
    "deleted_ok": {
        "pt": "<p>O agendamento foi deletado com sucesso.</p>",
        "en": "<p>I have successfully deleted the meeting.</p>",
    },
    "delete_failed": {
        "pt": "<p>Falha ao deletar: {message}</p>",
        "en": "<p>Deletion failed: {message}</p>",
    },
    "update_ok": {
        "pt": "<p>Agendamento atualizado com sucesso! Tópico: <strong>{topic}</strong>.</p>",
        "en": "<p>I have updated the meeting successfully! Topic: <strong>{topic}</strong>.</p>",
    },
    "update_failed": {
        "pt": "<p>Falha na atualização: {message}</p>",
        "en": "<p>Update failed: {message}</p>",
    },
    "delete_preview_failed": {
        "pt": "<p>Desculpe, falha ao preparar a prévia de exclusão: {message}</p>",
        "en": "<p>Sorry, I failed to prepare the deletion preview: {message}</p>",
    },
    "update_preview_failed": {
        "pt": "<p>Desculpe, falha ao preparar a prévia de atualização: {message}</p>",
        "en": "<p>Sorry, I failed to prepare the update preview: {message}</p>",
    },
    "schedule_preview_failed": {
        "pt": "<p>Desculpe, falha ao preparar a prévia de agendamento: {message}</p>",
        "en": "<p>Sorry, I failed to prepare the meeting preview: {message}</p>",
    },
    "auth_list_intro": {
        "pt": "<p>Por favor, autorize {agent_name} a visualizar suas reuniões clicando no link abaixo:</p>",
        "en": "<p>Please authorize {agent_name} to view your meetings by clicking the link below:</p>",
    },
    "auth_list_header": {
        "pt": "<p>Autorizado com sucesso! Aqui estão suas reuniões agendadas:</p>",
        "en": "<p>I have authorized successfully! Here are your scheduled meetings:</p>",
    },
    "auth_list_header_no_auth": {
        "pt": "<p>Aqui estão suas reuniões agendadas:</p>",
        "en": "<p>Here are your scheduled meetings:</p>",
    },
    "delete_intro": {
        "pt": "<p>Preparei uma prévia para deletar sua reunião:</p><p>ID da Reunião: <code>{meeting_id}</code></p><p>Para confirmar esta exclusão, por favor autorize {agent_name} clicando no link abaixo:</p>",
        "en": "<p>I have prepared a preview to delete your meeting:</p><p>Meeting ID: <code>{meeting_id}</code></p><p>To finalize this deletion, please authorize {agent_name} by clicking the link below:</p>",
    },
    "update_intro": {
        "pt": "<p>Preparei uma prévia para atualizar sua reunião:</p><ul><li>ID da Reunião: <code>{meeting_id}</code></li><li>Novo Tópico: <strong>{topic}</strong></li><li>Novo Horário: <strong>{start_time}</strong></li></ul><p>Para confirmar esta atualização, por favor autorize {agent_name} clicando no link abaixo:</p>",
        "en": "<p>I have prepared a preview to update your meeting:</p><ul><li>Meeting ID: <code>{meeting_id}</code></li><li>New Topic: <strong>{topic}</strong></li><li>New Time: <strong>{start_time}</strong></li></ul><p>To finalize this update, please authorize {agent_name} by clicking the link below:</p>",
    },
    "schedule_intro": {
        "pt": "<p>Preparei uma prévia para sua reunião:</p><ul><li>Tópico: <strong>{topic}</strong></li><li>Data: {date}</li><li>Horário: {start_time}</li><li>Duração: {duration} minutos</li></ul><p>Para confirmar e agendar esta reunião, por favor autorize {agent_name} clicando no link abaixo:</p>",
        "en": "<p>I have prepared a preview for your meeting:</p><ul><li>Topic: <strong>{topic}</strong></li><li>Date: {date}</li><li>Time: {start_time}</li><li>Duration: {duration} minutes</li></ul><p>To finalize and book this meeting, please authorize {agent_name} by clicking the link below:</p>",
    },
    "booking_ok": {
        "pt": "<p>Agendamento realizado com sucesso! Tópico: <strong>{topic}</strong>.</p>",
        "en": "<p>I have booked the meeting successfully! Topic: <strong>{topic}</strong>.</p>",
    },
    "booking_ok_with_id": {
        "pt": "<p>Agendamento realizado com sucesso! Tópico: <strong>{topic}</strong> (ID: <code>{meeting_id}</code>).</p>",
        "en": "<p>I have booked the meeting successfully! Topic: <strong>{topic}</strong> (ID: <code>{meeting_id}</code>).</p>",
    },
    "booking_failed": {
        "pt": "<p>Falha no agendamento: {message}</p>",
        "en": "<p>Booking failed: {message}</p>",
    },
    "meeting_list_item": {
        "pt": "<li><strong>{topic}</strong> ({date} às {start_time}, ID: <code>{id}</code>)</li>",
        "en": "<li><strong>{topic}</strong> ({date} at {start_time}, ID: <code>{id}</code>)</li>",
    },
}

AUTH_BUTTON_LABELS: dict[str, dict[str, str]] = {
    "list": {"pt": "Autorizar Lista", "en": "Authorize List"},
    "delete": {"pt": "Autorizar Exclusão", "en": "Authorize Delete"},
    "update": {"pt": "Autorizar Atualização", "en": "Authorize Update"},
    "schedule": {"pt": "Autorizar Reunião", "en": "Authorize Meeting"},
}


def _msg(language: str, key: str, **kwargs: object) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        return ""
    template = entry.get(language, entry.get("en", ""))
    safe_kwargs = {k: html.escape(str(v)) for k, v in kwargs.items()}
    return template.format(**safe_kwargs)


t = _msg


def _button_label(action: str, language: str) -> str:
    entry = AUTH_BUTTON_LABELS.get(action)
    if entry is None:
        return "Authorize"
    return entry.get(language, entry.get("en", "Authorize"))


def _auth_button(url: str, label: str) -> str:
    return (
        f'<div style="margin-top: 10px;">'
        f'<a href="{html.escape(url, quote=True)}" target="_blank" class="btn btn-primary">'
        f'{html.escape(label)}</a></div>'
    )


def _meeting_list_item(language: str, meeting: dict) -> str:
    template = MESSAGES.get("meeting_list_item", {}).get(
        language, MESSAGES.get("meeting_list_item", {}).get("en", "")
    )
    if not template:
        return ""
    return template.format(
        topic=html.escape(meeting["topic"]),
        date=html.escape(meeting["date"]),
        start_time=html.escape(meeting["start_time"]),
        id=html.escape(meeting["id"]),
    )


def _meeting_list_html(meetings: list[dict], language: str) -> str:
    return "".join(_meeting_list_item(language, m) for m in meetings)


def _build_system_instruction(custom_prompt: str, org_name: str, agent_name: str, language: str) -> str:
    prompt_template = custom_prompt or SYSTEM_PROMPT
    system_instruction = prompt_template.format(
        org_name=html.escape(org_name),
        agent_name=html.escape(agent_name),
        current_date=datetime.now().strftime("%Y-%m-%d"),
        current_weekday=datetime.now().strftime("%A"),
    )
    lang_instruction = "You must respond in English."
    if language == "pt":
        lang_instruction = "You must respond in Portuguese (Brazil)."
    return system_instruction + "\n\n" + lang_instruction


def _history_to_contents(history: list[dict], message: str) -> list[object]:
    messages: list[object] = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part.from_text(text=m["content"])],
        )
        for m in history
    ]
    messages.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
    return messages


def _select_gemini_client(gemini_api_key: str):
    if gemini_api_key:
        logger.debug("Using per-request Gemini API key (prefix=%s...)", gemini_api_key[:10])
        return genai.Client(api_key=gemini_api_key)
    client = GeminiClientFactory.get_default()
    logger.info("Using default Gemini API key (available=%s)", client is not None)
    return client


def _default_meeting_args() -> dict[str, str]:
    return {
        "date": "2026-05-22",
        "start_time": "14:00",
        "duration": "60",
        "time_zone": "America/Sao_Paulo",
    }


def _extract_meeting_id(message: str, default: str = "test-meeting-id") -> str:
    for w in message.split():
        if len(w) > 10 and "-" in w:
            return w
    return default


def _extract_topic(message: str, default: str) -> str:
    if "Topic:" in message:
        parts = message.split("Topic:")
        if len(parts) > 1:
            return parts[1].strip().rstrip(".")
    if "topic:" in message.lower():
        parts = message.lower().split("topic:")
        if len(parts) > 1:
            return parts[1].strip().rstrip(".")
    return default


async def _mock_list_response(message: str, thread_id: str, agent_name: str, language: str) -> str:
    res = await dispatch_tool("list_meetings", {}, thread_id)
    if res.get("status") == "preview_ready":
        return _msg(language, "auth_list_intro", agent_name=agent_name) + _auth_button(
            res["authorization_url"], _button_label("list", language)
        )
    meetings = res.get("meetings", [])
    if not meetings:
        return t(language, "no_meetings")
    header_key = "auth_list_header_no_auth" if res.get("status") == "success" else "auth_list_header"
    return f"{t(language, header_key)}<ul>{_meeting_list_html(meetings, language)}</ul>"


async def _mock_delete_response(message: str, thread_id: str, agent_name: str, language: str) -> str:
    meeting_id = _extract_meeting_id(message)
    args: dict[str, object] = {"meeting_id": meeting_id, "topic": "Test E2E Meeting", "action": "delete"}
    res = await dispatch_tool("delete_meeting_preview", args, thread_id)
    if "authorization_url" not in res:
        return _msg(language, "delete_preview_failed", message=res.get("message", "Unknown error"))
    return _msg(language, "delete_intro", meeting_id=meeting_id, agent_name=agent_name) + _auth_button(
        res["authorization_url"], _button_label("delete", language)
    )


async def _mock_update_response(message: str, thread_id: str, agent_name: str, language: str) -> str:
    meeting_id = _extract_meeting_id(message)
    topic = _extract_topic(message, "Updated E2E Meeting")
    args: dict[str, object] = {
        "meeting_id": meeting_id,
        "topic": topic,
        **_default_meeting_args(),
        "start_time": "15:00",
    }
    res = await dispatch_tool("update_meeting_preview", args, thread_id)
    if "authorization_url" not in res:
        return _msg(language, "update_preview_failed", message=res.get("message", "Unknown error"))
    return _msg(
        language,
        "update_intro",
        meeting_id=meeting_id,
        topic=topic,
        start_time="15:00",
        agent_name=agent_name,
    ) + _auth_button(
        res["authorization_url"], _button_label("update", language)
    )


async def _mock_schedule_response(message: str, thread_id: str, agent_name: str, language: str) -> str:
    topic = _extract_topic(message, "Test E2E Meeting")
    base_args = _default_meeting_args()
    args: dict[str, object] = {"topic": topic, **base_args}
    res = await dispatch_tool("schedule_meeting_preview", args, thread_id)
    if "authorization_url" not in res:
        return _msg(language, "schedule_preview_failed", message=res.get("message", "Unknown error"))
    return _msg(
        language,
        "schedule_intro",
        topic=topic,
        date=base_args["date"],
        start_time=base_args["start_time"],
        duration=base_args["duration"],
        agent_name=agent_name,
    ) + _auth_button(
        res["authorization_url"], _button_label("schedule", language)
    )


_MOCK_INTENT_HANDLERS = [
    (("list", "show", "listar", "reuni", "mostrar"), _mock_list_response),
    (("delete", "remove", "deletar", "excluir"), _mock_delete_response),
    (("update", "change", "reschedule", "alterar", "atualizar"), _mock_update_response),
    (("schedule", "meeting", "agendar", "marcar"), _mock_schedule_response),
]


async def _dispatch_mock_intent(message: str, lower: str, thread_id: str, agent_name: str, language: str) -> str | None:
    for keywords, handler in _MOCK_INTENT_HANDLERS:
        if any(kw in lower for kw in keywords):
            return await handler(message, thread_id, agent_name, language)
    return None


async def _mock_response(message: str, thread_id: str, agent_name: str, language: str) -> str:
    lower = message.lower()
    if "authorized" in lower or "check" in lower or "autorizado" in lower or "verifique" in lower:
        return ""

    intent_response = await _dispatch_mock_intent(message, lower, thread_id, agent_name, language)
    if intent_response is not None:
        return intent_response

    return t(language, "greeting")


def _generate_content_sync(client, contents, config):
    return client.models.generate_content(
        model="gemini-flash-latest",
        contents=contents,
        config=config,
    )


def _generate_content_stream_sync(client, contents, config):
    return client.models.generate_content_stream(
        model="gemini-flash-latest",
        contents=contents,
        config=config,
    )


def _get_default_client():
    return GeminiClientFactory.get_default()


async def _handle_delete_authorized(thread_id: str, pending: dict, language: str) -> tuple[bool, str]:
    res = await dispatch_tool("delete_meeting", {"meeting_id": pending["meeting_id"]}, thread_id)
    if res.get("status") == "deleted":
        return True, t(language, "deleted_ok")
    return True, _msg(language, "delete_failed", message=res.get("message", "Unknown error"))


async def _handle_update_authorized(thread_id: str, pending: dict, language: str) -> tuple[bool, str]:
    res = await dispatch_tool("update_meeting", pending, thread_id)
    if res.get("status") == "updated":
        return True, _msg(language, "update_ok", topic=res["meeting"]["topic"])
    return True, _msg(language, "update_failed", message=res.get("message", "Unknown error"))


async def _handle_booking_authorized(thread_id: str, pending: dict, language: str) -> tuple[bool, str]:
    res = await dispatch_tool("schedule_meeting", pending, thread_id)
    if res.get("status") == "booked":
        meeting_id = res.get("meeting", {}).get("id", "")
        # Pass plain values; _msg() escapes them once. The <code> markup lives in
        # the template (don't pre-build HTML here — _msg would escape the tags).
        if meeting_id:
            return True, t(language, "booking_ok_with_id", topic=pending["topic"], meeting_id=meeting_id)
        return True, t(language, "booking_ok", topic=pending["topic"])
    return True, t(language, "booking_failed", message=res.get("message", "Unknown error" if language == "en" else "Erro desconhecido"))


async def _handle_list_authorized(thread_id: str, pending: dict, language: str) -> tuple[bool, str]:
    res = await dispatch_tool("list_meetings", {}, thread_id)
    if res.get("status") == "success":
        meetings = res.get("meetings", [])
        if not meetings:
            return True, t(language, "no_meetings")
        return True, f"{t(language, 'auth_list_header')}<ul>{_meeting_list_html(meetings, language)}</ul>"
    return True, t(language, "not_authorized_prompt")


_AUTHORIZED_STATE_DISPATCH = (
    (FlowState.DELETE_AUTHORIZED, lambda p: p and p.get("meeting_id"), _handle_delete_authorized),
    (FlowState.UPDATE_AUTHORIZED, lambda p: p and "meeting_id" in p, _handle_update_authorized),
    (FlowState.BOOKING_AUTHORIZED, lambda p: p and "topic" in p, _handle_booking_authorized),
    (FlowState.LIST_AUTHORIZED, lambda _p: True, _handle_list_authorized),
)


async def _dispatch_authorized_state(state, thread_id: str, pending: dict, language: str) -> tuple[bool, str] | None:
    for target_state, guard, handler in _AUTHORIZED_STATE_DISPATCH:
        if state == target_state and guard(pending):
            return await handler(thread_id, pending, language)
    return None


async def _handle_authorization_callback(message: str, thread_id: str, language: str) -> tuple[bool, str]:
    state_mgr = StateManager.get_instance()
    state = state_mgr.get_state(thread_id)

    if "authorized" in message.lower() or "check" in message.lower() or "autorizado" in message.lower() or "verifique" in message.lower():
        if state in (FlowState.BOOKING_AUTHORIZED, FlowState.LIST_AUTHORIZED, FlowState.UPDATE_AUTHORIZED, FlowState.DELETE_AUTHORIZED):
            pending = state_mgr.get_pending_meeting(thread_id)
            dispatched = await _dispatch_authorized_state(state, thread_id, pending, language)
            if dispatched is not None:
                return dispatched
        return True, t(language, "not_authorized_prompt")

    return False, ""


SYSTEM_PROMPT = """You are the AI Agent {agent_name} for {org_name}.
You help users manage scheduled video conference meetings, including scheduling new ones, listing existing ones, updating meeting details, and deleting meetings.

Today is {current_date} ({current_weekday}).

IMPORTANT: You must format your responses in clean, semantic HTML. Use <p> for paragraphs, <strong> for bold text, <ul>/<li> for lists, <code> for inline code, <pre><code> for code blocks, and <a> with target="_blank" for links. Do NOT use markdown. Return raw HTML directly.

When a user wants to schedule a meeting, you need to collect:
- topic: What the meeting is about
- date: The date (YYYY-MM-DD format)
- start_time: Start time (HH:MM format, 24-hour)
- duration: Duration in minutes (default 60)
- time_zone: Timezone (default to America/Sao_Paulo)

Once you have all the details, use the schedule_meeting_preview tool to show a
preview and request user authorization. After the user authorizes, use the
schedule_meeting tool to finalize the booking.

If a user wants to view, check, or list their meetings, use the list_meetings tool. If not authorized, it will request authorization. Once authorized, list the meetings clearly.

If a user wants to delete a meeting:
1. First, call list_meetings to find the correct meeting and its ID if you don't already have it in context.
2. Call delete_meeting_preview with the meeting's ID and topic.
3. After the user authorizes, call delete_meeting to finalize the deletion.

If a user wants to update or modify a meeting (such as rescheduling time, changing topic, duration, etc.):
1. First, call list_meetings to find the correct meeting and its ID if you don't already have it in context.
2. Formulate the update by merging the changes with the existing fields.
3. Call update_meeting_preview with the meeting ID and the updated/complete values for all fields (topic, date, start_time, duration, time_zone).
4. After the user authorizes, call update_meeting with the meeting ID and all updated/complete values to finalize the update. This prevents duplicate meetings from being created!

Be conversational and helpful. If the user is vague about dates, ask for
clarification. If they say "tomorrow", calculate the actual date.
"""



def _gemma_generate_config(system_instruction):
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=TOOL_DEFINITIONS,
        temperature=0.7,
    )


async def _run_tool_call_and_append(messages, part, fn_call, thread_id: str, iteration: int):
    tool_result = await dispatch_tool(
        fn_call.name,
        dict(fn_call.args) if fn_call.args else {},
        thread_id=thread_id,
    )
    _append_tool_call_messages(messages, part, fn_call, tool_result)
    logger.debug("Tool result for %s: %s", fn_call.name, tool_result)


async def _dispatch_tool_and_refetch(gemini_client, messages, system_instruction, part, fn_call, thread_id: str, iteration: int):
    logger.info("Gemini requested tool call: %s(%s) at iteration %d for thread=%s", fn_call.name, dict(fn_call.args) if fn_call.args else {}, iteration, thread_id)
    await _run_tool_call_and_append(messages, part, fn_call, thread_id, iteration)
    return await asyncio.to_thread(
        _generate_content_sync,
        gemini_client,
        messages,
        _gemma_generate_config(system_instruction),
    )


def _summarize_unexpected_part(iteration: int, thread_id: str, part):
    logger.warning("Unexpected part type at iteration %d for thread=%s: %s", iteration, thread_id, type(part))


def _log_final_response(thread_id: str, iteration: int, text: str):
    logger.info("Gemini final response for thread=%s after %d iterations: '%s'", thread_id, iteration + 1, text[:100])


async def _run_gemini_tool_loop(gemini_client, messages, system_instruction, thread_id: str, max_iterations: int = 5) -> str:
    response = await asyncio.to_thread(
        _generate_content_sync,
        gemini_client,
        messages,
        _gemma_generate_config(system_instruction),
    )

    for iteration in range(max_iterations):
        if not response.candidates or not response.candidates[0].content.parts:
            logger.warning("No candidates/parts in Gemini response at iteration %d for thread=%s", iteration, thread_id)
            break

        part = response.candidates[0].content.parts[0]

        if hasattr(part, "function_call") and part.function_call:
            response = await _dispatch_tool_and_refetch(
                gemini_client, messages, system_instruction, part, part.function_call, thread_id, iteration,
            )
        elif hasattr(part, "text") and part.text:
            _log_final_response(thread_id, iteration, part.text)
            return part.text
        else:
            _summarize_unexpected_part(iteration, thread_id, part)
            break

    logger.error("Gemini agent exhausted %d iterations for thread=%s without text response", max_iterations, thread_id)
    return "I couldn't process that request. Could you try again?"


async def run_agent(
    message: str,
    thread_id: str,
    org_name: str,
    history: list[dict],
    gemini_api_key: str = "",
    custom_prompt: str = "",
    language: str = "en",
    agent_name: str = DEFAULT_AGENT_NAME,
) -> str:
    # Check for authorization callback interception
    intercepted, response_text = await _handle_authorization_callback(message, thread_id, language)
    if intercepted:
        return re.sub(r'<[^>]*>', '', response_text)

    if settings.MOCK_LLM:
        return await _mock_response(message, thread_id, agent_name, language)

    gemini_client = _select_gemini_client(gemini_api_key)
    if gemini_client is None:
        return "No Gemini API key configured for this organization. Ask an admin to register an AI Agent with an API key."

    system_instruction = _build_system_instruction(custom_prompt, org_name, agent_name, language)
    messages = _history_to_contents(history, message)

    logger.info("Calling Gemini for thread=%s, org=%s, history_len=%d", thread_id, org_name, len(history))
    return await _run_gemini_tool_loop(gemini_client, messages, system_instruction, thread_id)


async def _stream_text_in_chunks(text: str):
    chunk_size = 5
    for i in range(0, len(text), chunk_size):
        yield text[i:i+chunk_size]
        await asyncio.sleep(0.01)


def _consume_stream_for_text_and_fn(response_stream):
    """Yields text parts from a Gemini response stream; returns the first function_call part (or None)."""
    has_fn_call = False
    fn_call_part = None
    for chunk in response_stream:
        if not chunk.candidates or not chunk.candidates[0].content.parts:
            continue

        for part in chunk.candidates[0].content.parts:
            if hasattr(part, "function_call") and part.function_call:
                has_fn_call = True
                fn_call_part = part
                break
            elif hasattr(part, "text") and part.text:
                yield ("text", part.text)

        if has_fn_call:
            break

    yield ("fn_call", fn_call_part) if has_fn_call else ("done", None)


def _append_tool_call_messages(messages, fn_call_part, fn_call, tool_result):
    messages.append(types.Content(role="model", parts=[fn_call_part]))
    messages.append(types.Content(
        role="user",
        parts=[
            types.Part.from_function_response(
                name=fn_call.name,
                response={"result": tool_result},
            )
        ],
    ))


async def run_agent_stream(
    message: str,
    thread_id: str,
    org_name: str,
    history: list[dict],
    gemini_api_key: str = "",
    custom_prompt: str = "",
    language: str = "en",
    agent_name: str = DEFAULT_AGENT_NAME,
):
    # Check for authorization callback interception
    intercepted, response_text = await _handle_authorization_callback(message, thread_id, language)
    if intercepted:
        async for chunk in _stream_text_in_chunks(response_text):
            yield chunk
        return

    if settings.MOCK_LLM:
        response_text = await _mock_response(message, thread_id, agent_name, language)
        async for chunk in _stream_text_in_chunks(response_text):
            yield chunk
        return

    gemini_client = _select_gemini_client(gemini_api_key)
    if gemini_client is None:
        yield "<p>No Gemini API key configured for this organization. Ask an admin to register an AI Agent with an API key.</p>"
        return

    system_instruction = _build_system_instruction(custom_prompt, org_name, agent_name, language)
    messages = _history_to_contents(history, message)

    logger.info("Calling Gemini stream for thread=%s, org=%s, history_len=%d", thread_id, org_name, len(history))

    max_iterations = 5
    for iteration in range(max_iterations):
        # The Gemini SDK is synchronous; push the blocking call into a worker
        # thread so the FastAPI event loop stays responsive while we wait.
        response_stream = await asyncio.to_thread(
            _generate_content_stream_sync,
            gemini_client,
            messages,
            types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=TOOL_DEFINITIONS,
                temperature=0.7,
            ),
        )

        fn_call_part = None
        for kind, payload in _consume_stream_for_text_and_fn(response_stream):
            if kind == "text":
                yield payload
            elif kind == "fn_call":
                fn_call_part = payload
                break
            else:
                break

        if fn_call_part is None:
            break

        fn_call = fn_call_part.function_call
        logger.info("Gemini requested tool call in stream: %s(%s) at iteration %d for thread=%s", fn_call.name, dict(fn_call.args) if fn_call.args else {}, iteration, thread_id)
        tool_result = await dispatch_tool(
            fn_call.name,
            dict(fn_call.args) if fn_call.args else {},
            thread_id=thread_id,
        )

        _append_tool_call_messages(messages, fn_call_part, fn_call, tool_result)

