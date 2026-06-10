import base64
import json
import logging
import uuid

import requests
from flask import Blueprint, render_template, request, session, current_app, jsonify, Response, stream_with_context

import jwt
from webapp.utils.decorators import login_required
from webapp.api_proxy import get_agent_config_via_internal_secret
from webapp.utils.i18n import get_locale
from common.constants import DEFAULT_AGENT_NAME

logger = logging.getLogger(__name__)


def decode_jwt(jwt_str: str) -> dict:
    if not jwt_str:
        return {}
    try:
        header = jwt.get_unverified_header(jwt_str)
        payload = jwt.decode(jwt_str, options={"verify_signature": False})
        return {"header": header, "payload": payload}
    except jwt.InvalidTokenError:
        # Fallback to manual parsing for mock/test tokens with non-standard/unpadded signatures
        try:
            parts = jwt_str.split(".")
            if len(parts) != 3:
                return {}
            # Decode Header
            header_b64 = parts[0]
            header_b64 += "=" * (-len(header_b64) % 4)
            header_bytes = base64.urlsafe_b64decode(header_b64)
            header = json.loads(header_bytes.decode("utf-8"))

            # Decode Payload
            payload_b64 = parts[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))

            return {"header": header, "payload": payload}
        except Exception as e:
            logger.error("Failed to decode JWT: %s", e)
            logger.debug("JWT fallback parse failure trace", exc_info=True)
            return {}



bp = Blueprint("chat", __name__)


def _ensure_thread_id() -> str:
    thread_id = session.get("chat_thread_id")
    if not thread_id:
        thread_id = str(uuid.uuid4())
        session["chat_thread_id"] = thread_id
        session.pop("last_obo_jwt", None)
        session.pop("last_agent_jwt", None)
    return thread_id


def _agent_headers() -> dict:
    headers = {}
    internal_secret = current_app.config.get("AGENT_INTERNAL_SECRET")
    if internal_secret:
        headers["X-Internal-Secret"] = internal_secret
    return headers


def _build_agent_chat_payload(thread_id: str, message: str, user: dict, agent_cfg) -> dict:
    payload = {
        "thread_id": thread_id,
        "message": message,
        "org_name": user.get("org_name", ""),
        "language": get_locale(),
    }
    if agent_cfg:
        payload["agent_id"] = agent_cfg.get("agent_id") or ""
        payload["agent_secret"] = agent_cfg.get("agent_secret") or ""
        payload["gemini_api_key"] = agent_cfg.get("gemini_api_key") or ""
        payload["custom_prompt"] = agent_cfg.get("custom_prompt") or ""
        payload["agent_name"] = agent_cfg.get("display_name") or DEFAULT_AGENT_NAME
    return payload


def _build_agent_chat_payload_via_m2m(thread_id: str, message: str, user: dict) -> dict:
    """Build the agent chat payload using the M2M internal-secret path.

    The Business API's GET /agent-config/org/{org_id} now accepts an
    `X-Internal-Secret` header for trusted callers, with the user's JWT
    forwarded for audit. Non-admin users no longer hit the
    `view_agent_config` scope wall; the agent receives the agent_id/
    agent_secret/gemini_key it needs to start the OBO flow (which
    requires the agent's own token as the actor_token per RFC 8693).

    Falls back to an empty agent_cfg if the internal secret is not
    configured or the API returns 404 — the agent service can still
    discover config from its own state_manager / M2M cache.
    """
    org_id = user.get("org_id", "")
    agent_cfg = (
        get_agent_config_via_internal_secret(org_id) if org_id else None
    )
    return _build_agent_chat_payload(thread_id, message, user, agent_cfg)


def _build_jwt_inspector_html(obo_jwt, agent_jwt) -> str:
    if not agent_jwt:
        return ""
    obo_jwt_data = decode_jwt(obo_jwt) if obo_jwt else {}
    agent_jwt_data = decode_jwt(agent_jwt)
    user_jwt_data = session.get("access_token_claims", {})
    return render_template(
        "chat/jwt_inspector_oob.html",
        obo_jwt_raw=obo_jwt,
        obo_jwt_data=obo_jwt_data,
        agent_jwt_raw=agent_jwt,
        agent_jwt_data=agent_jwt_data,
        user_token=user_jwt_data,
        obo_token=obo_jwt_data.get("payload", {}),
        agent_token=agent_jwt_data.get("payload", {}),
        user_jwt_raw=session.get("access_token"),
        user_jwt_data=decode_jwt(session.get("access_token")) if session.get("access_token") else {},
    )


def _stream_response_chunks(resp):
    """Yields text chunks and returns accumulated metadata string (empty if none)."""
    full_text = ""
    metadata_str = ""
    is_metadata = False

    for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
        if not chunk:
            continue

        if "__METADATA_START__" in chunk:
            parts = chunk.split("__METADATA_START__")
            yield ("text", parts[0])
            full_text += parts[0]
            metadata_str = parts[1]
            is_metadata = True
        elif is_metadata:
            metadata_str += chunk
        else:
            yield ("text", chunk)
            full_text += chunk

    yield ("done", full_text, metadata_str)


def _finalize_stream_metadata(metadata_str: str):
    try:
        metadata = json.loads(metadata_str.strip())
        obo_jwt = metadata.get("obo_jwt") or None
        agent_jwt = metadata.get("agent_jwt") or None

        session["last_obo_jwt"] = obo_jwt
        session["last_agent_jwt"] = agent_jwt

        inspector_html = _build_jwt_inspector_html(obo_jwt, agent_jwt)
        return f"__METADATA_START__{json.dumps({'inspector_html': inspector_html})}"
    except Exception as e:
        logger.error("Failed to parse/process stream metadata: %s", e)
        return None


@bp.route("/send_stream", methods=["POST"])
@login_required
def send_message_stream(org_handle):
    message = request.form.get("message", "").strip()
    if not message:
        return Response("Please type a message.", status=400)

    thread_id = _ensure_thread_id()

    agent_url = current_app.config["AGENT_SERVICE_URL"]
    user = session.get("user", {})

    payload = _build_agent_chat_payload_via_m2m(thread_id, message, user)
    headers = _agent_headers()

    def generate():
        try:
            resp = requests.post(
                f"{agent_url}/chat/stream",
                json=payload,
                headers=headers,
                stream=True,
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error("FastAPI returned status %d: %s", resp.status_code, resp.text)
                yield f"<p>Error from Agent Service: status {resp.status_code}</p>"
                return

            metadata_str = ""
            for kind, *payload_parts in _stream_response_chunks(resp):
                if kind == "text":
                    yield payload_parts[0]
                else:
                    metadata_str = payload_parts[1]

            if metadata_str:
                final_metadata_chunk = _finalize_stream_metadata(metadata_str)
                if final_metadata_chunk is not None:
                    yield final_metadata_chunk
        except requests.ConnectionError:
            yield "<p>AI Agent service is not running. Start it on port 8000.</p>"
        except Exception:
            logger.exception("Error in Flask send_message_stream generator")
            yield "<p>An error occurred while streaming the response.</p>"

    return Response(stream_with_context(generate()), mimetype="text/plain")


@bp.route("/clear", methods=["POST"])
@login_required
def clear_chat(org_handle):
    thread_id = session.get("chat_thread_id")
    session.pop("chat_thread_id", None)
    session.pop("last_obo_jwt", None)
    session.pop("last_agent_jwt", None)

    if thread_id:
        agent_url = current_app.config["AGENT_SERVICE_URL"]
        try:
            headers = {}
            internal_secret = current_app.config.get("AGENT_INTERNAL_SECRET")
            if internal_secret:
                headers["X-Internal-Secret"] = internal_secret
            requests.post(f"{agent_url}/clear/{thread_id}", headers=headers, timeout=5)
        except Exception as e:
            logger.warning("Failed to clear agent service state for thread %s: %s", thread_id, e)

    return jsonify({"status": "cleared"})

