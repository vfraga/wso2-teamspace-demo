import json
import logging
import sys
import secrets

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from google.genai import errors
from starlette.responses import Response
from mcp.server.sse import SseServerTransport

from agent.config import settings
from agent.gemini_agent import run_agent, run_agent_stream
from agent.auth_manager import AuthManager
from agent.state_manager import StateManager, FlowState, FrontendState
from agent.chat_history import ChatHistoryManager
from agent.schemas import ChatRequest, ChatResponse

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

app = FastAPI(title="Teamspace AI Agent", version="1.0.0")


from common.fastapi_errors import handle_validation_error, handle_global_error


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return await handle_validation_error(request, exc, logger, "AI Agent")


@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    return await handle_global_error(request, exc, logger, "AI Agent")


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

from typing import Optional

class ChatHistoryManagerProxy:
    def __getattr__(self, name):
        return getattr(ChatHistoryManager.get_instance(), name)

chat_history = ChatHistoryManagerProxy()


def require_internal_secret(x_internal_secret: Optional[str] = Header(None)) -> None:
    # Fail-closed: agent/config.py generates a random secret if AGENT_INTERNAL_SECRET
    # is unset, so settings.INTERNAL_SECRET is always truthy in normal operation. The
    # outer `if settings.INTERNAL_SECRET:` guard remains a defensive belt-and-braces
    # check — if it ever becomes falsy, we fall through and treat the request as
    # unauthorised (returns None, so the endpoint still executes but cannot be hit
    # by an unknown client because every caller must present the rotating secret).
    if settings.INTERNAL_SECRET:
        if not x_internal_secret or x_internal_secret != settings.INTERNAL_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")


async def _prepare_chat(req: ChatRequest) -> dict:
    """Shared pre-execution setup for /chat and /chat/stream.

    Stores incoming agent credentials, captures initial token snapshots (in case a
    tool clears them mid-execution), eagerly refreshes the agent JWT when missing,
    and records the user message in chat history. Returns a dict with everything
    the response/stream generators need afterwards.
    """
    logger.info("Chat request: thread=%s, org=%s, message='%s'", req.thread_id, req.org_name, req.message[:100])
    state_mgr = StateManager.get_instance()
    if req.org_name:
        state_mgr.set_org_name(req.thread_id, req.org_name)
    if req.agent_id:
        state_mgr.set_agent_credentials(req.thread_id, req.agent_id or "", req.agent_secret or "")
    if req.agent_name:
        state_mgr.set_agent_name(req.thread_id, req.agent_name)

    auth_mgr = AuthManager.get_instance()
    initial_obo_jwt = auth_mgr.get_obo_jwt_raw(req.thread_id)
    initial_agent_jwt = auth_mgr.get_agent_jwt_raw(req.thread_id)

    if not initial_agent_jwt:
        agent_id, agent_secret = state_mgr.get_agent_credentials(req.thread_id)
        if agent_id and agent_secret:
            try:
                logger.debug("Pre-fetching agent credentials token for thread=%s", req.thread_id)
                initial_agent_jwt = await auth_mgr.fetch_agent_token(agent_id, agent_secret, thread_id=req.thread_id)
            except Exception:
                logger.exception("Failed to pre-fetch agent token")

    history = chat_history.get_history(req.thread_id)
    chat_history.add_message(req.thread_id, "user", req.message)

    return {
        "state_mgr": state_mgr,
        "auth_mgr": auth_mgr,
        "initial_obo_jwt": initial_obo_jwt,
        "initial_agent_jwt": initial_agent_jwt,
        "history": history,
    }


def _collect_response_metadata(
    thread_id: str,
    state_mgr: StateManager,
    auth_mgr: AuthManager,
    initial_obo_jwt,
    initial_agent_jwt,
) -> dict:
    """Build the post-execution metadata block shared by /chat and /chat/stream."""
    auth_url = None
    frontend_state = state_mgr.get_frontend_state(thread_id)
    if frontend_state == FrontendState.AWAITING_AUTHORIZATION:
        auth_url = state_mgr.get_auth_url(thread_id)

    obo_jwt = auth_mgr.get_obo_jwt_raw(thread_id) or initial_obo_jwt
    agent_jwt = auth_mgr.get_agent_jwt_raw(thread_id) or initial_agent_jwt
    return {
        "state": frontend_state.value,
        "authorization_url": auth_url,
        "obo_jwt": obo_jwt,
        "agent_jwt": agent_jwt,
    }


_AI_API_ERROR_MESSAGES = {
    503: "I'm sorry, the AI service is currently experiencing very high demand and is temporarily unavailable. Please try again in a few moments.",
    429: "I'm sorry, the rate limit for the AI service has been exceeded. Please try again in a few moments.",
    400: "I'm sorry, the request sent to the AI service was invalid. This could be due to a configuration issue or an incorrect API key.",
}


def _map_exception_to_user_message(e: Exception, thread_id: str) -> str:
    if isinstance(e, (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout)):
        logger.warning("AI service request timed out: %s", e)
        return "I'm sorry, the request to the AI service timed out. Please try again."

    if isinstance(e, (httpx.HTTPError, httpx.ReadError, httpx.WriteError, httpx.ConnectError)):
        logger.warning("AI service communication failure: %s", e)
        return "I'm sorry, I failed to communicate with the AI service. Please verify your connection and try again."

    if isinstance(e, errors.APIError):
        logger.warning("AI service API error (Code %s): %s", e.code or "unknown", e)
        if e.code in _AI_API_ERROR_MESSAGES:
            return _AI_API_ERROR_MESSAGES[e.code]
        return f"I'm sorry, the AI service encountered an error (Code {e.code or 'unknown'}). Please try again."

    logger.exception("Agent error for thread=%s", thread_id)
    return "I'm sorry, I encountered an unexpected error while processing your request. Please try again."


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_internal_secret)])
async def chat(req: ChatRequest):
    ctx = await _prepare_chat(req)
    state_mgr = ctx["state_mgr"]
    auth_mgr = ctx["auth_mgr"]

    try:
        response_text = await run_agent(
            message=req.message,
            thread_id=req.thread_id,
            org_name=req.org_name,
            history=ctx["history"],
            gemini_api_key=req.gemini_api_key or "",
            custom_prompt=req.custom_prompt or "",
            language=req.language or "en",
            agent_name=req.agent_name or "Worklink Assistant",
        )
    except Exception as e:
        response_text = _map_exception_to_user_message(e, req.thread_id)

    chat_history.add_message(req.thread_id, "assistant", response_text)

    metadata = _collect_response_metadata(
        req.thread_id, state_mgr, auth_mgr, ctx["initial_obo_jwt"], ctx["initial_agent_jwt"]
    )
    logger.info("Chat response: thread=%s, state=%s, response='%s'", req.thread_id, metadata["state"], response_text[:100])
    return ChatResponse(
        message=response_text,
        state=metadata["state"],
        authorization_url=metadata["authorization_url"],
        obo_jwt=metadata["obo_jwt"],
        agent_jwt=metadata["agent_jwt"],
    )


@app.post("/chat/stream", dependencies=[Depends(require_internal_secret)])
async def chat_stream(req: ChatRequest):
    ctx = await _prepare_chat(req)
    state_mgr = ctx["state_mgr"]
    auth_mgr = ctx["auth_mgr"]

    async def stream_generator():
        full_response_text = ""
        try:
            async for chunk in run_agent_stream(
                message=req.message,
                thread_id=req.thread_id,
                org_name=req.org_name,
                history=ctx["history"],
                gemini_api_key=req.gemini_api_key or "",
                custom_prompt=req.custom_prompt or "",
                language=req.language or "en",
                agent_name=req.agent_name or "Worklink Assistant",
            ):
                full_response_text += chunk
                yield chunk
        except Exception:
            logger.exception("Error in run_agent_stream")
            yield " [Error: Connection to Gemini lost. Please try again.]"
            return

        chat_history.add_message(req.thread_id, "assistant", full_response_text)
        metadata = _collect_response_metadata(
            req.thread_id, state_mgr, auth_mgr, ctx["initial_obo_jwt"], ctx["initial_agent_jwt"]
        )
        yield f"__METADATA_START__{json.dumps(metadata)}"

    return StreamingResponse(stream_generator(), media_type="text/plain")


@app.get("/authorize")
async def initiate_oauth(request: Request, thread_id: str, action: str):
    logger.info("Initiating secure OBO OAuth: thread_id=%s, action=%s", thread_id, action)
    auth_mgr = AuthManager.get_instance()
    state_mgr = StateManager.get_instance()

    agent_id, _ = state_mgr.get_agent_credentials(thread_id)
    csrf_token = secrets.token_urlsafe(16)

    if action == "list":
        scopes = ["list_meetings_agent"]
    elif action == "update":
        scopes = ["list_meetings_agent", "update_meeting_agent"]
    elif action == "delete":
        scopes = ["list_meetings_agent", "delete_meeting_agent"]
    elif action == "book":
        scopes = ["create_meeting_agent"]
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")

    # Incorporate thread_id and action directly into a signed state parameter to protect against CSRF stateless-ly
    state_payload = {
        "thread_id": thread_id,
        "action": action,
        "state": csrf_token,
    }
    secret = settings.INTERNAL_SECRET or settings.CLIENT_SECRET or "default_secret_key_123"
    signed_state = jwt.encode(state_payload, secret, algorithm="HS256")

    auth_url = auth_mgr.get_real_wso2_authorization_url(
        thread_id=thread_id,
        scopes=scopes,
        state_token=signed_state,
        agent_id=agent_id,
    )

    cookie_payload = {
        "thread_id": thread_id,
        "action": action,
        "state": csrf_token,
    }
    token = jwt.encode(cookie_payload, secret, algorithm="HS256")

    response = RedirectResponse(auth_url)
    
    is_secure = request.url.scheme == "https"
    response.set_cookie(
        "oauth_session",
        token,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/callback",
        max_age=300,
    )
    return response


@app.get("/callback")
async def oauth_callback(request: Request, code: str, state: str):
    logger.info("OBO callback received: state=%s", state)
    auth_mgr = AuthManager.get_instance()
    state_mgr = StateManager.get_instance()

    cookie_val = request.cookies.get("oauth_session")
    secret = settings.INTERNAL_SECRET or settings.CLIENT_SECRET or "default_secret_key_123"

    thread_id, action, csrf_state = _decode_signed_state(state, secret)

    if cookie_val:
        cookie_error = _verify_oauth_cookie(cookie_val, csrf_state, secret)
        if cookie_error is not None:
            return cookie_error

    if not thread_id:
        if not settings.MOCK_LLM:
            logger.error("Refusing callback: no signed state JWT resolved and MOCK_LLM is disabled")
            return _oauth_error_response("invalid_state")
        thread_id, action = _fallback_parse_state(state)

    auth_error = await _complete_obo_authorization(auth_mgr, thread_id, code, state_mgr)
    if auth_error is not None:
        return auth_error

    flow_state = _action_to_flow_state(action or "")
    state_mgr.set_state(thread_id, flow_state)
    logger.debug("OBO authorization complete for thread=%s, action=%s, set state to %s", thread_id, action, flow_state)

    response = HTMLResponse("""
    <html><body>
    <script>
        window.opener && window.opener.postMessage('authorized', '*');
        window.close();
    </script>
    <p>Authorization successful. This window will close automatically.</p>
    </body></html>
    """)
    response.delete_cookie("oauth_session", path="/callback")
    return response


_OAUTH_ERROR_HTML = {
    "csrf_mismatch": "<html><body><p>CSRF verification failed. Please close this window and try again.</p></body></html>",
    "invalid_state": "<html><body><p>Invalid OAuth state. Please close this window and try again.</p></body></html>",
    "invalid_session": "<html><body><p>Invalid OAuth session. Please close this window and try again.</p></body></html>",
    "auth_failed": "<html><body><p>Authorization failed. Please close this window and try again.</p></body></html>",
}


def _oauth_error_response(kind: str, status_code: int = 400) -> HTMLResponse:
    return HTMLResponse(_OAUTH_ERROR_HTML[kind], status_code=status_code)


def _action_to_flow_state(action: str) -> FlowState:
    return {
        "list": FlowState.LIST_AUTHORIZED,
        "update": FlowState.UPDATE_AUTHORIZED,
        "delete": FlowState.DELETE_AUTHORIZED,
    }.get(action, FlowState.BOOKING_AUTHORIZED)


def _decode_signed_state(state: str, secret: str) -> tuple[str | None, str | None, str | None]:
    try:
        payload = jwt.decode(state, secret, algorithms=["HS256"])
        thread_id = payload.get("thread_id")
        action = payload.get("action")
        csrf_state = payload.get("state")
        logger.info("Resolved thread_id=%s, action=%s from signed state parameter", thread_id, action)
        return thread_id, action, csrf_state
    except Exception as e:
        logger.info("Failed to decode state parameter as signed JWT (this is normal for tests/mock triggers): %s", e)
        return None, None, None


def _verify_oauth_cookie(cookie_val: str, csrf_state: str | None, secret: str) -> HTMLResponse | None:
    try:
        cookie_payload = jwt.decode(cookie_val, secret, algorithms=["HS256"])
    except Exception:
        logger.exception("Failed to decode oauth_session cookie")
        return _oauth_error_response("invalid_session")

    if not csrf_state:
        logger.warning("Signed state JWT is missing CSRF token; refusing legacy fallback")
        return _oauth_error_response("invalid_state")

    if cookie_payload.get("state") != csrf_state:
        logger.warning("CSRF cookie mismatch: expected %s, got %s", csrf_state, cookie_payload.get("state"))
        return _oauth_error_response("csrf_mismatch")

    logger.info("CSRF cookie verification succeeded")
    return None


def _fallback_parse_state(state: str) -> tuple[str, str]:
    logger.info("Falling back to parsing thread_id and action directly from state parameter (MOCK_LLM)")
    parts = state.split(":", 1)
    thread_id = parts[0]
    action = parts[1] if len(parts) > 1 else "book"
    return thread_id, action


async def _complete_obo_authorization(auth_mgr, thread_id: str, code: str, state_mgr) -> HTMLResponse | None:
    agent_id, agent_secret = state_mgr.get_agent_credentials(thread_id)
    try:
        await auth_mgr.exchange_obo_code(thread_id, code, agent_id, agent_secret)
    except ValueError:
        logger.exception("OBO token exchange failed for thread=%s", thread_id)
        return _oauth_error_response("auth_failed")
    return None


@app.get("/state/{thread_id}", dependencies=[Depends(require_internal_secret)])
async def get_state(thread_id: str):
    state_mgr = StateManager.get_instance()
    auth_mgr = AuthManager.get_instance()
    return {
        "state": state_mgr.get_frontend_state(thread_id).value,
        "obo_jwt": auth_mgr.get_obo_jwt_raw(thread_id),
        "agent_jwt": auth_mgr.get_agent_jwt_raw(thread_id),
    }


@app.post("/agent-token", dependencies=[Depends(require_internal_secret)])
async def create_agent_token():
    """Authenticate the agent with its own credentials (for demo purposes)."""
    auth_mgr = AuthManager.get_instance()
    token = await auth_mgr.fetch_agent_token()
    return {"access_token": token}


# Initialize SseServerTransport for MCP
mcp_transport = SseServerTransport("/mcp/messages/")


@app.get("/mcp/sse")
async def handle_mcp_sse(request: Request):
    """Establishes persistent Server-Sent Events stream for MCP."""
    logger.info("MCP Client connection requested at /mcp/sse")
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        # Support clients (like standard browser EventSources) that cannot easily send headers
        token = request.query_params.get("token") or request.query_params.get("access_token") or ""

    from agent.mcp_server import mcp_server, mcp_token_ctx
    ctx_token = mcp_token_ctx.set(token)
    try:
        async with mcp_transport.connect_sse(
            request.scope,
            request.receive,
            request._send
        ) as streams:
            logger.info("MCP Server stream connected, running event loop")
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options()
            )
    except Exception:
        logger.exception("MCP Server stream encountered an error")
    finally:
        mcp_token_ctx.reset(ctx_token)
        logger.info("MCP Client connection closed")
    return Response()


@app.post("/mcp/messages/")
async def handle_mcp_messages(request: Request):
    """Receives JSON-RPC messages from the MCP Client."""
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("token") or request.query_params.get("access_token") or ""

    from agent.mcp_server import mcp_token_ctx
    ctx_token = mcp_token_ctx.set(token)
    try:
        # Delegate post message handling to MCP transport
        return await mcp_transport.handle_post_message(request.scope, request.receive, request._send)
    finally:
        mcp_token_ctx.reset(ctx_token)


@app.post("/clear/{thread_id}", dependencies=[Depends(require_internal_secret)])
async def clear_thread(thread_id: str):
    logger.debug("Clearing state and tokens for thread=%s", thread_id)
    auth_mgr = AuthManager.get_instance()
    state_mgr = StateManager.get_instance()
    auth_mgr.clear_obo_tokens(thread_id)
    chat_history.clear(thread_id)
    state_mgr.clear_state(thread_id)
    return {"status": "cleared"}


@app.get("/health")
async def health():
    return {"status": "ok"}
