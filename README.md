# Teamspace

[![CI](https://github.com/vfraga/wso2-teamspace-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/vfraga/wso2-teamspace-demo/actions/workflows/ci.yml)

A demonstration application for WSO2 Identity Server 7.2.0, covering B2B CIAM
(Customer Identity and Access Management) and agentic AI identity with
On-Behalf-Of delegation.

## Overview

Teamspace is a B2B collaboration and meeting management portal. It exists to show
two things working together:

1. **B2B CIAM.** Sub-organization onboarding, cross-organization application
   sharing, delegated administration, role management, federated login through an
   external identity provider, and per-tenant branding via the WSO2 IS Branding
   Preference API.
2. **Agentic AI identity.** An assistant (Worklink Assistant) backed by Google
   Gemini that acts on behalf of a user through OAuth 2.0 Token Exchange
   (RFC 8693). It holds no long-lived credentials and no administrator key; every
   action it takes is authorized by a token the user consented to.

The portal is bilingual (English and Portuguese), switchable at runtime through
the `/set_lang` route. Translation strings live in `webapp/translations/`.

This is a demo, not a product. It runs multi-instance and has a migrated schema,
rate limiting and container images, but see [Known
limitations](#known-limitations) before deploying anything based on it.

## Architecture

Three services around a single identity provider. WSO2 IS is external and
operator-provisioned; the stack talks to it rather than hosting it.

```mermaid
graph TD
    User([Enterprise User]) <--> WebApp[Flask Portal: Port 5001]
    WebApp <--> WSO2[WSO2 Identity Server 7.2.0: Port 9443]

    subgraph Microservices Stack
        WebApp <--> BizAPI[Business API FastAPI: Port 9091]
        WebApp <--> AIAgent[AI Agent Service FastAPI: Port 8000]
        AIAgent <--> Gemini[Google Gemini LLM]
        AIAgent <--> WSO2
        BizAPI <--> SQL[(Database: SQLite by default)]
        AIAgent <-.optional.-> Redis[(Redis: shared state)]
        WebApp <-.optional.-> Redis
    end

    BizAPI <--> WSO2
```

### Components

**`webapp/`** Flask portal, port 5001

The user-facing portal, built from Flask blueprints (`main`, `dashboard`,
`admin`, `personalization`, `signup`, `subscription`, `chat`, `agents`) and plain
CSS. Authentication is OpenID Connect against WSO2 IS via Authlib, including
`id_token` verification with a per-login nonce. Organization branding (colors,
logos, banners) is read from and written to the WSO2 IS organization branding
APIs. Entry point: `webapp.app:create_app`.

**`api/`** Business API (FastAPI), port 9091

Persists meetings, agent configurations, personalization and organization plans
through SQLAlchemy. SQLite by default; `DATABASE_URL` accepts any SQLAlchemy URL,
and the PostgreSQL path is covered by a CI job. Alembic owns the schema
(`migrations/`).

Every request is authorized from a verified WSO2 IS token: RS256 pinned against
JWKS with audience, issuer and expiry enforced, then a scope check
(`list_meetings`, `create_meeting`, `update_meeting`, `delete_meeting`, and the
`_agent` variants used for delegated calls). Queries are filtered by the `org`
claim, so tenant isolation does not depend on the caller passing the right
parameters. Nested actor claims (`act`) are read for On-Behalf-Of requests.
Routers: `/meetings`, `/personalization`, `/agent-config`, `/plans`. Entry point:
`api.main:app`.

**`agent/`** AI Agent service (FastAPI), port 8000

Runs the assistant against Google Gemini (`gemini-flash-latest`, via the
`google-genai` SDK) and orchestrates the OBO flow: it builds the WSO2
authorization URL, exchanges the returned code for a delegated token via RFC 8693
token exchange, and calls the Business API with it.

Per-thread state (PKCE verifier, delegated tokens, chat history) lives behind a
pluggable store (`agent/store.py`), in-process by default and Redis when
`REDIS_URL` is set. That indirection is what allows more than one agent instance:
`/authorize` and `/callback` are separate processes under gunicorn, and the
callback needs the verifier the authorize step created.

Also hosts the MCP server (see [Model Context Protocol
server](#model-context-protocol-server)). Entry point: `agent.main:app`.

**`common/`** shared library

| Module | Purpose |
| :--- | :--- |
| `config.py` | Shared defaults and a one-shot `.env` load |
| `constants.py` | Constants used by more than one service |
| `logging_setup.py` | `LOG_LEVEL` resolution for all three services |
| `jwt_validation.py` | RS256/JWKS verification primitives |
| `m2m_auth.py` | Client-credentials tokens for service-to-service calls |
| `rate_limit.py` | Limit configuration and the FastAPI enforcement dependency |
| `safe_auth_logger.py` | Credential masking and the JWT claim summary used in logs |
| `fastapi_errors.py` | Shared FastAPI exception handlers |

`jwt_validation.py` is the only implementation of that verification. The Business
API, the MCP server and the M2M path all decode through it, so none of them can
end up with weaker checks than the others.

**Deployment**

`Dockerfile` builds one non-root image; `docker-compose.yml` runs it three times
with different commands and adds Redis. `start.sh` and `stop.sh` manage local
processes, with `SERVE_MODE=production` switching from development servers to
gunicorn. `alembic.ini` and `migrations/` hold the Business API schema history.

**`tests/`**

Split into `unit/`, `integration/` and `e2e/`. `tests/conftest.py` contains a
lifecycle orchestrator for the live suite: it clones WSO2 IS into a temporary
directory, boots it, bootstraps credentials, runs the browser tests and tears
everything down. See [Tests](#tests).

## On-Behalf-Of delegation

The usual way to give an AI agent access to a system is to hand it a static API
key or an administrator account. Teamspace uses RFC 8693 OAuth 2.0 Token Exchange
instead, so delegation is scoped to a single action, time-bound, and attributable
to both the agent and the user who approved it.

```
┌──────┐             ┌──────────┐             ┌─────────────┐             ┌─────────┐
│ User │             │ AI Agent │             │  WSO2 IS    │             │ Biz API │
└──┬───┘             └────┬─────┘             └──────┬──────┘             └────┬────┘
   │                      │                          │                         │
   │ 1. "Schedule meeting"│                          │                         │
   ├─────────────────────>│                          │                         │
   │                      │ 2. Check OBO Token       │                         │
   │                      ├──┐                       │                         │
   │                      │  │ (None exists/expired) │                         │
   │                      │<─┘                       │                         │
   │ 3. Consent URL       │                          │                         │
   │<─────────────────────┤                          │                         │
   │                      │                          │                         │
   │ 4. Authenticate & Consent                       │                         │
   ├────────────────────────────────────────────────>│                         │
   │                      │                          │                         │
   │                      │ 5. Auth Code / User Token│                         │
   │                      │<─────────────────────────┤                         │
   │                      │                          │                         │
   │                      │ 6. Exchange for OBO Token│                         │
   │                      ├─────────────────────────>│                         │
   │                      │  (RFC 8693 token exchange)                         │
   │                      │                          │                         │
   │                      │ 7. OBO JWT Token Issued  │                         │
   │                      │<─────────────────────────┤                         │
   │                      │                          │                         │
   │                      │ 8. Create Meeting (with OBO Token)                 │
   │                      ├───────────────────────────────────────────────────>│
   │                      │                          │                         │ (Authorized!)
   │                      │                          │                         │<───────────
```

### CSRF and replay protection

The `state` parameter is an HMAC-signed JWT, paired with an `oauth_session`
cookie carrying the same CSRF token. Both are required at the callback, and both
expire after `OAUTH_STATE_TTL_SECONDS` (300 seconds, `agent/main.py`).

Requiring both bounds authorization-code injection. A captured `state` is
worthless without the matching cookie, and worthless once it expires. The cost is
that a consent screen left open for more than five minutes needs the
authorization restarting.

### Token structure

The delegated access token asserts two identities, which is what lets the
Business API record both the agent that acted and the user who authorized it:

```json
{
  "sub": "d54ac2dc-7cad-4842-bcce-7905da589f17",
  "act": {
    "sub": "814889a0-7730-4928-a3b6-b3cfd776262f"
  },
  "aud": "fLHf61QVhvGKJwOhM3NvekepG1Aa",
  "aut": "APPLICATION_USER",
  "client_id": "fLHf61QVhvGKJwOhM3NvekepG1Aa",
  "iss": "https://localhost:9443/t/teamspace/oauth2/token",
  "org_handle": "numbainfinite",
  "org_id": "848d1d37-2bac-4fad-98bd-eed28bacf173",
  "scope": "create_meeting list_meetings openid profile",
  "exp": 1779330751
}
```

| Claim | Meaning |
| :--- | :--- |
| `sub` | The user who delegated the authority |
| `act.sub` | The SCIM agent executing the request |
| `org_handle` | The B2B tenant, which scopes every Business API query |

## Model Context Protocol server

The AI Agent service hosts an MCP server (`agent/mcp_server.py`) so external
clients such as n8n, Cursor or Claude Desktop can use the meeting tools. It is
mounted on the same FastAPI app on port 8000 and protected by the same token
verification as everything else.

```mermaid
graph LR
    Client[External MCP Client / Agent] -- 1. SSE GET /mcp/sse --> AgentSvc[AI Agent Service: Port 8000]
    Client -- 2. HTTP POST /mcp/messages/ --> AgentSvc
    AgentSvc -- 3. Verify Bearer JWT (JWKS) --> WSO2[WSO2 IS 7.2.0: Port 9443]
    AgentSvc -- 4. Route authorized operations --> BizAPI[Business API: Port 9091]
```

**Endpoints**

| Endpoint | Purpose |
| :--- | :--- |
| `GET /mcp/sse` | Opens a Server-Sent Events channel and announces the JSON-RPC message endpoint |
| `POST /mcp/messages/` | Handles JSON-RPC 2.0 tool listings and tool calls |

**Token verification.** Requests carry `Authorization: Bearer <token>`. Browser
`EventSource` clients cannot set headers, so a `token` or `access_token` query
parameter is accepted as a fallback; see [Known
limitations](#known-limitations) for what that costs. Signatures are verified
against the WSO2 IS JWKS endpoint using RS256.

**Scopes.** Tools map to registered scopes:

| Tool | Required scope |
| :--- | :--- |
| `list_meetings` | `list_meetings_agent` or `list_meetings` |
| `schedule_meeting_preview` | `create_meeting_agent` or `create_meeting` |
| `update_meeting_preview` | `update_meeting_agent` or `update_meeting` |
| `delete_meeting_preview` | `delete_meeting_agent` or `delete_meeting` |
| `schedule_meeting` | `create_meeting_agent` only |
| `update_meeting` | `update_meeting_agent` only |
| `delete_meeting` | `delete_meeting_agent` only |

The three tools that commit a change require the `_agent` scope specifically. A
plain user scope is not enough, so a destructive action can only happen under
delegated authority.

**Tenant safety.** The active token is held in a `ContextVar` (`mcp_token_ctx`)
for the duration of a tool dispatch, so concurrent calls from different tenants
cannot read each other's credentials.

## Plan gating

The Identity Provider, Login Flow and AI Agents admin pages are enterprise
features. Access requires both the organization's plan and the user's role, with
the plan checked first (`webapp/blueprints/admin.py:check_idp_access`,
`webapp/blueprints/agents.py:_check_plan_and_role`).

The role on its own is not a safe proxy for the plan. `PLAN_ROLES` grants
`idp-manager` and the branding-editor roles when an organization upgrades
(`webapp/blueprints/subscription.py`), but that handler never revokes them and it
accepts a downgrade, so an organization that drops from enterprise to basic keeps
every role its old plan granted.

The plan lookup can also fail, which makes this a three-way decision:

| Plan lookup | Role held | Outcome |
| :--- | :--- | :--- |
| Not entitled | Either | Upgrade prompt |
| Entitled | Yes | Allowed |
| Entitled | No | "You need this role", rather than a misleading upgrade prompt |
| Unknown (Business API unreachable) | Yes | Allowed; an outage should not revoke paid features |
| Unknown | No | Upgrade prompt |

`resolve_plan_for_gating` (`webapp/api_proxy.py`) is what separates "this
organization has no subscription" (HTTP 404, since signup always writes a plan
row) from "the Business API is unreachable" (5xx). `get_organization_plan`
reports both as `basic`, so it is safe for display and unsafe for an access
decision.

## Configuration

All three services read a single `.env` file through `common/config.py`:

```bash
cp .env.example .env
```

The values in `.env.example` are placeholders. At minimum, fill in `CLIENT_ID`,
`CLIENT_SECRET` and `APP_ID` (printed by `setup_is.py`), `GEMINI_API_KEY`, and
the admin passwords the setup scripts need (`IS_SUPER_ADMIN_PASSWORD`,
`IS_TENANT_ADMIN_PASSWORD`). Service-to-service authentication needs no
additional secrets.

### WSO2 Identity Server

| Variable | Description | Default |
| :--- | :--- | :--- |
| `IS_BASE_URL` | Root URL of the WSO2 Identity Server | `https://localhost:9443` |
| `IS_ORG_HANDLE` | Root B2B tenant handle. Set to `teamspace` for tenant mode, leave empty for `carbon.super` | *(empty)* |
| `IS_VERIFY_TLS` | Verify the IS TLS certificate. Applies to every outbound call, including OIDC discovery, the token endpoint and the JWKS fetch. Set `false` for local development against the default self-signed certificate | `true` |
| `WSO2_IS_TEMPLATE_PATH` | Path to a clean, un-bootstrapped WSO2 IS 7.2.0 install, cloned by the live E2E fixture. No default; when unset, the live suite skips | *(unset)* |

### OAuth 2.0 application

Generated by `setup_is.py`.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `CLIENT_ID` | Client ID for the Teamspace portal | *(from bootstrap)* |
| `CLIENT_SECRET` | Client secret for the Teamspace portal | *(from bootstrap)* |
| `APP_ID` | Application ID of the registered app in WSO2 IS | *(from bootstrap)* |
| `APP_NAME` | Registered application name | `Teamspace` |

### Flask portal

| Variable | Description | Default |
| :--- | :--- | :--- |
| `FLASK_SECRET_KEY` | Signs session cookies. If unset, an ephemeral key is generated and a warning logged; sessions are then lost on restart | *(generated)* |
| `FLASK_HOST` | Bind host | `localhost` |
| `FLASK_PORT` | Bind port | `5001` |
| `FLASK_DEBUG` | Read by `flask run` itself, enabling debug mode and the reloader. `.env.example` sets it to `true` | *(Flask default: off)* |
| `FLASK_ENV` | Set to `production` for secure session cookies, a Content-Security-Policy header, `INFO` logging and Alembic-owned schema | *(unset)* |

### Business API and AI Agent

| Variable | Description | Default |
| :--- | :--- | :--- |
| `BUSINESS_API_URL` | Where the Business API is reachable | `http://localhost:9091` |
| `AGENT_SERVICE_URL` | Where the AI Agent service is reachable | `http://localhost:8000` |
| `AGENT_REDIRECT_URI` | OAuth redirect target for the OBO flow | `http://localhost:8000/callback` |
| `GEMINI_API_KEY` | Google Gemini API key | *(empty)* |
| `DATABASE_URL` | SQLAlchemy connection string | `sqlite:///teamspace.db` |
| `ALLOWED_ORIGINS` | Comma-separated CORS allow-list for the FastAPI services | `http://localhost:5001` |
| `MOCK_LLM` | Run the agent without calling Gemini. Used by tests | `false` |

### Agent OAuth state signing

| Variable | Description | Default |
| :--- | :--- | :--- |
| `AGENT_STATE_SIGNING_SECRET` | HMAC key for the OBO `state` JWT | Falls back to `CLIENT_SECRET` |

This key must be stable and shared across every agent instance, because
`/authorize` signs the state and `/callback` verifies it, and those are different
processes once more than one worker runs. It is never auto-generated: a random
value would only verify on the instance that produced it, which fails
intermittently rather than loudly. If neither this variable nor `CLIENT_SECRET`
is set, the agent refuses to start under `FLASK_ENV=production`, and `/authorize`
returns an error page otherwise.

### Service-to-service authentication

There is no shared secret to configure. The services authenticate to each other
with OAuth 2.0 client-credentials tokens minted from `CLIENT_ID` and
`CLIENT_SECRET`, sent in the `X-Service-Authorization` header.

| Hop | Endpoints |
| :--- | :--- |
| Portal to agent | `/chat`, `/chat/stream`, `/state/{thread_id}`, `/agent-token`, `/clear/{thread_id}` |
| Portal to Business API | Agent-config lookup backing the chat panel |
| Agent to Business API | The agent reading its own organization's config |

The receiver verifies the token the same way it verifies a user token, RS256
against JWKS with audience, issuer and expiry enforced
(`common/jwt_validation.py`), then requires the `internal_service` scope
(`common/m2m_auth.py`).

`Authorization` stays reserved for the end-user JWT. The Business API's
`GET /agent-config/org/{org_id}` wants both principals at once: the service as the
trust gate and the user for the audit record. Separate headers keep them distinct
with no precedence rules to get wrong, and the same shape works on the
portal-to-agent hop, where no user token is sent at all.

> [!IMPORTANT]
> `setup_is.py` provisions `internal_service` on a "Teamspace Internal Services"
> API resource (`urn:teamspace:internal`) authorized with
> `policyIdentifier: NO_POLICY`. This is required, not a preference. WSO2 grants
> RBAC-policy scopes through a user's roles, and a client-credentials token has no
> user, so under RBAC the token is issued successfully with an empty `scope`
> claim and every M2M call returns 403. `ServiceTokenClient` detects that case and
> logs the likely cause rather than passing on a token that cannot work.

Because `internal_service` is granted to the application and to no user role, a
user-bearing token can never carry it. The scope is therefore the real gate. The
`aut=APPLICATION` claim is checked as well, but only when present, so a WSO2 build
that omits it does not break every M2M call.

### Shared state

| Variable | Description | Default |
| :--- | :--- | :--- |
| `REDIS_URL` | Redis connection string. Unset means in-process storage, which limits the agent to a single instance | *(unset)* |
| `REDIS_KEY_PREFIX` | Prefix for Redis keys, for a Redis shared with other applications | *(empty)* |

One variable moves three things to Redis: the agent's OBO state
(`agent/store.py`), Flask sessions, and rate-limit counters. Install the driver
with `uv sync --extra redis`. A bad or unreachable `REDIS_URL` logs an error and
falls back to in-process storage rather than refusing to boot, so check the
`Agent state store: ...` line at startup to see which backend is live.

> [!WARNING]
> With Redis, OBO access tokens and per-organization agent credentials are at rest
> outside the process. Require AUTH and TLS on the connection, and keep the TTL
> short (`agent/store.py:DEFAULT_TTL_SECONDS`, 24 hours by default).

### Serving and deployment

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SERVE_MODE` | `production` makes `start.sh` serve everything behind gunicorn. Any other value keeps the development servers | `development` |
| `API_WORKERS`, `AGENT_WORKERS`, `WEBAPP_WORKERS` | Gunicorn worker counts in production mode | `4` |
| `DB_AUTO_CREATE` | Whether the Business API creates missing tables at startup | On, except under `FLASK_ENV=production` |

> [!IMPORTANT]
> Under `SERVE_MODE=production`, `start.sh` refuses to start when `AGENT_WORKERS`
> is above 1 and `REDIS_URL` is unset. The agent's OBO state would not be shared,
> so `/authorize` and `/callback` could land on different workers and the consent
> flow would fail intermittently. Catching that at boot is better than debugging
> it mid-demo.

### Rate limiting

| Variable | Description | Default |
| :--- | :--- | :--- |
| `RATE_LIMIT_ENABLED` | Set `false` to disable rate limiting | `true` |
| `RATE_LIMIT_CHAT` | Limit on the chat endpoints, which reach Gemini | `60/minute` |
| `RATE_LIMIT_AUTH` | Limit on the agent's `/authorize` and `/callback` | `120/minute` |
| `RATE_LIMIT_DEFAULT` | Backstop applied to the remaining endpoints | `600/minute` |
| `RATE_LIMIT_TRUST_FORWARDED_FOR` | Key limits on the first `X-Forwarded-For` hop instead of the peer address. Enable only behind a proxy you control; `docker-compose.yml` sets it, since Docker's NAT would otherwise put every client in one bucket | `false` |
| `MAX_MCP_SSE_STREAMS` | Ceiling on simultaneously open MCP SSE streams. Each connection is held for its lifetime, so this bounds worker exhaustion | `32` |

Counters follow `REDIS_URL`: per-process without it, shared with it. The defaults
sit well above anything a demo walkthrough produces.

On the FastAPI services these limits are dependencies ordered ahead of
`require_service_auth`, not decorators on the handler. FastAPI resolves a route's
dependencies before calling its handler, so a decorator-based limit would only
count requests that had already authenticated, leaving an unauthenticated flood
unthrottled. `tests/integration/test_rate_limiting.py` guards that ordering.

### Logging

| Variable | Description | Default |
| :--- | :--- | :--- |
| `LOG_LEVEL` | Root level for every service (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). An explicit value always wins | `DEBUG`, or `INFO` under `FLASK_ENV=production` |

`DEBUG` by default is intentional. The point of the demo is watching the identity
workflow happen: token exchange, OBO state transitions, claim contents. Setting
`FLASK_ENV=production` drops it to `INFO` without needing `LOG_LEVEL`. Resolution
lives in `common/logging_setup.py`.

Token claims are logged as a masked one-line summary
(`common/safe_auth_logger.py:format_claims`). `sub`, `org`, `aut`, `scope` and
`act.sub` stay readable because they are the interesting part; raw tokens, full
payloads and plaintext email addresses are not written. Chat message content is
`DEBUG` only, so it is visible in the demo and absent in production.

### Federated identity provider

Optional, for the external-IdP login demo.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `FEDERATED_IDP_URL` | SCIM2 Users endpoint of the federated IdP | `https://localhost:9444/t/worklink.com/scim2/Users` |
| `FEDERATED_IDP_ADMIN_USER` | Admin username for the federated IdP | `teamspaceadmin@worklink.com` |
| `FEDERATED_IDP_ADMIN_PASSWORD` | Admin password for the federated IdP | *(empty)* |

### Branding defaults

| Variable | Description | Default |
| :--- | :--- | :--- |
| `DEFAULT_LOGO_URL` | Logo used when an organization has no custom branding | jsDelivr CDN SVG |
| `DEFAULT_FAVICON_URL` | Favicon used when an organization has no custom branding | jsDelivr CDN SVG |

### Setup script credentials

Consumed by `setup_is.py`, `setup_idp_server.py` and `setup_secondary_is.py`.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `IS_SUPER_ADMIN_USERNAME` | WSO2 IS super-admin username | `admin` |
| `IS_SUPER_ADMIN_PASSWORD` | WSO2 IS super-admin password. Required, or IS API calls fail with 401 | *(empty)* |
| `IS_TENANT_ADMIN_PASSWORD` | Password for the bootstrapped `teamspaceadmin` tenant admin, which is the portal login password | *(empty)* |
| `FEDERATED_USER_PASSWORD` | Password for the federated IdP's seeded test users | *(empty)* |

> [!IMPORTANT]
> There are no global `AGENT_ID` and `AGENT_SECRET` variables, and adding them
> would be a mistake. A single root-level agent spanning tenants breaks the B2B
> isolation the demo is built to show.
>
> Instead, each sub-organization administrator provisions their own SCIM2 agent
> through the portal's AI Agents page. The returned `agent_id` and `agent_secret`
> are stored against that organization, and the portal supplies the active
> tenant's credentials per chat request, which keeps token exchange inside one
> organization boundary.

## Installation

### Prerequisites

- Python 3.10 or newer. CI covers 3.10 and 3.12.
- WSO2 Identity Server 7.2.0 running on `https://localhost:9443`.
- A Google Gemini API key, from [Google AI Studio](https://aistudio.google.com/).
- macOS or Linux. The start/stop scripts and test orchestrator assume a Unix shell.

### 1. Install dependencies

Dependencies are defined in `pyproject.toml` with a pinned `uv.lock`. There is no
`requirements.txt`.

With [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync                       # runtime dependencies
uv sync --extra dev           # plus pytest, ruff, playwright
```

Optional extras: `redis` for shared state, `postgres` for a PostgreSQL
`DATABASE_URL`.

With `venv` and `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e ".[dev]"
```

For the browser tests, install the Playwright browser:

```bash
.venv/bin/playwright install chromium
```

### 2. Bootstrap WSO2 Identity Server

With WSO2 IS running, set the admin passwords the script needs (in `.env` or the
shell) and run:

```bash
# IS_SUPER_ADMIN_PASSWORD  WSO2 super-admin; the default username is "admin"
# IS_TENANT_ADMIN_PASSWORD becomes the teamspaceadmin portal password
python setup_is.py
```

The script is idempotent. It creates the `teamspace` tenant, sets branding,
registers the B2B application, and configures API resources, scopes and roles. On
success it prints `CLIENT_ID`, `CLIENT_SECRET` and `APP_ID`; copy those into
`.env`.

For the federated login demo, `python setup_secondary_is.py` clones a second WSO2
IS onto port 9444, starts it, and bootstraps the `worklink.com` federated IdP
with its tenant, application, groups and test users. The lower-level
`setup_idp_server.py` bootstraps an already-running instance on 9444 and is what
the live E2E harness calls.

### 3. Run the stack

```bash
bash start.sh
```

The script starts the Business API and AI Agent under uvicorn, waits for each
`/health` endpoint, then starts the portal:

- Portal: `http://localhost:5001`
- Business API: `http://localhost:9091`
- AI Agent: `http://localhost:8000`

`DEV_MODE=true` adds hot reload (`--reload` for the FastAPI services, `--debug`
for Flask):

```bash
DEV_MODE=true bash start.sh
```

#### Behind gunicorn

`SERVE_MODE=production` replaces the development servers with gunicorn, using
uvicorn workers for the two FastAPI services. Pair it with `REDIS_URL` so the
agent can run more than one worker:

```bash
SERVE_MODE=production REDIS_URL=redis://localhost:6379/0 bash start.sh
```

Without `REDIS_URL` the script refuses to start a multi-worker agent, since the
consent callback would reach workers that never saw `/authorize`. Set
`AGENT_WORKERS=1` to run production mode on a single agent instance.

#### In containers

`docker-compose.yml` runs the three services plus Redis in production mode:

```bash
docker compose run --rm business-api alembic upgrade head   # first time only
docker compose up --build
```

Only the portal publishes a host port (5001). The Business API and agent are
reachable on the compose network; their `ports` entries are commented out in the
compose file if you need them exposed for debugging.

WSO2 IS is not containerised, because `setup_is.py` provisions it against an
install you control. Compose consumes it through `IS_BASE_URL`, defaulting to
`https://host.docker.internal:9443` for an instance on the host.

> [!NOTE]
> If you demo the OBO consent popup through compose, the browser opens
> `AGENT_REDIRECT_URI` directly, so the agent's port 8000 has to be published and
> `AGENT_REDIRECT_URI` set to match.

### 4. Use the portal

1. Open `http://localhost:5001`.
2. Sign in as `teamspaceadmin@teamspace` with the password from
   `IS_TENANT_ADMIN_PASSWORD`.
3. Onboard a sub-organization (`numbainfinite` or `nuvora`, for example), create
   users inside it, and try Branding Preferences: logo, background, primary color
   and typography update the sub-organization portal live.

### 5. Use the assistant

1. Open the Worklink Assistant from the portal.
2. If you have not already, register an agent under Admin, AI Agents, to
   provision its `agent_id` and `agent_secret`.
3. Ask for something that needs delegation:
   > "Schedule a meeting for tomorrow at 2 PM with the topic 'Security Review'."
4. The agent works out that it needs delegated authority and replies with an
   authorization link.
5. Consent on the WSO2 IS page, then return to the assistant.
6. The agent completes the token exchange and books the meeting.

### 6. Shut down

Press `Ctrl+C` in the terminal running `start.sh`, or from elsewhere:

```bash
bash stop.sh
```

`stop.sh` kills the PIDs recorded in `/tmp/teamspace_*.pid` and sweeps up
stragglers matching the uvicorn, flask and gunicorn command lines.

## Tests

Test tooling lives in the `dev` and `test` optional-dependency groups.
`pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`, and
registers a `live` marker.

### Unit and integration

Hermetic: no WSO2 IS, no Gemini key, no browser. 332 tests covering schemas,
database helpers, JWT parsing and verification, scope enforcement, credential
masking, the M2M service-token exchange end to end against a WSO2-shaped token
endpoint on a loopback port, the agent state store against both backends, OBO
state and CSRF handling, rate-limit enforcement, plan gating, `id_token`
verification, and log-level resolution.

```bash
.venv/bin/pytest tests/unit tests/integration -m "not live"
```

To run them against PostgreSQL instead of SQLite, point `TEST_DATABASE_URL` at a
server:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/teamspace \
  .venv/bin/pytest tests/integration
```

### Browser tests

`tests/e2e/test_e2e_mocked.py` holds eight self-contained browser tests. They
stand up a mock WSO2 IS alongside the real Business API, AI Agent and portal on
loopback ports, so they need no WSO2 install and no Gemini key:

```bash
.venv/bin/pytest tests/e2e/test_e2e_mocked.py
```

### Live tests

The remaining `tests/e2e/` files are marked `live` and run against real WSO2 IS
instances. They need `WSO2_IS_TEMPLATE_PATH` pointing at a clean 7.2.0 install
and Playwright browsers installed. Without that variable, the fixture skips.

```bash
export WSO2_IS_TEMPLATE_PATH="/path/to/wso2is-7.2.0.24"
.venv/bin/pytest tests/ -v
```

The orchestrator in `tests/conftest.py`:

1. Kills residual processes on ports 9443, 9444, 5001, 9091 and 8000.
2. Copies the template twice: a primary instance on 9443 and a federated IdP on
   9444 (via `offset = 1` in `deployment.toml`). On macOS APFS, `cp -R` uses
   copy-on-write clones.
3. Boots each instance in its own process group (`os.setsid`) and polls
   `/api/server/v1/tenants` until both answer 200.
4. Runs `setup_is.py`, parsing `CLIENT_ID`, `CLIENT_SECRET` and `APP_ID` from its
   output, then `setup_idp_server.py` for the federated IdP.
5. Starts the three services with those credentials, isolating the database as
   `test_live_teamspace.db` and setting `MOCK_LLM=true` so no Gemini calls are
   made.
6. Runs the browser tests, including the full consent and token-exchange flow.
7. Tears everything down, printing captured server logs and removing the
   temporary databases and WSO2 clones.

### Linting

Ruff is configured in `pyproject.toml` with a correctness-focused rule set:

```bash
ruff check .
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request against `main`:

| Job | What it does |
| :--- | :--- |
| `lint` | `ruff check .` |
| `security` | `pip-audit` over the locked runtime dependencies, `bandit` at medium severity and above |
| `postgres` | Applies migrations and runs the integration suite against a PostgreSQL service container |
| `test` | Unit and integration suites on Python 3.10 and 3.12 |

Dependencies come from the lockfile via `uv sync --frozen`.

`.github/workflows/e2e.yml` runs the browser suite. It is a separate workflow
because it installs a browser and boots four servers. The `live` tests stay out
of CI, since a hosted runner has no WSO2 IS install to clone.

## Production notes

This is demo software. If you build on it, these are the things to settle first.

1. **TLS.** Local development disables verification (`IS_VERIFY_TLS=false`) for
   WSO2's self-signed certificate. In production, install a CA-signed
   certificate, set `IS_VERIFY_TLS=true`, and terminate TLS in front of the
   stack. Gunicorn should not be internet-facing.
2. **Database.** Point `DATABASE_URL` at PostgreSQL or MySQL and run
   `.venv/bin/alembic upgrade head` before serving. A database created by the
   startup auto-create path should be adopted with `alembic stamp head` first, so
   the baseline migration is not replayed over existing tables.
3. **Shared state.** Set `REDIS_URL` to move Flask sessions, agent OBO state and
   rate-limit counters off local process memory. Without it, the stack is
   single-instance.
4. **Secrets.** Never commit `.env`. Keep `GEMINI_API_KEY`, `CLIENT_SECRET`,
   `AGENT_STATE_SIGNING_SECRET` and per-organization agent secrets in a secrets
   manager and inject them as environment variables.
5. **Service-to-service auth.** M2M calls already use short-lived
   client-credentials tokens verified against JWKS. Confirm the
   `internal_service` API resource is authorized with `NO_POLICY`, and run every
   hop over TLS. mTLS between services is a reasonable second layer.
6. **Production flag.** `FLASK_ENV=production` turns on secure session cookies
   and the CSP header, sets `INFO` logging, hands the schema to Alembic, and makes
   a missing OAuth state-signing key fatal at startup.
7. **Serving.** `SERVE_MODE=production` for gunicorn, or use
   `docker-compose.yml`, which already does.

## Known limitations

**Rate limiting is per-process without Redis.** With `REDIS_URL` the counters are
shared. Without it each gunicorn worker keeps its own, so the effective limit is
roughly workers times limit. For a real deployment a WAF, load balancer or API
gateway is usually the better place for this; the in-app limits are a backstop.

**The MCP token can travel in a query parameter.** `?token=` exists because
browser `EventSource` cannot set an `Authorization` header. Query strings end up
in access logs, browser history and `Referer` headers. Gunicorn's access log is
off by default in both `start.sh` and compose; leave it off, or filter those
parameters if you turn it on.

**PostgreSQL is tested but not exercised in anger.** The CI job applies the
migrations and runs the integration suite against PostgreSQL, but only SQLite
sees any sustained use here.

**UI scopes are a hint, not a boundary.** The portal's session `user_scopes` come
from a verified access token and decide what the UI offers. Authorization is
enforced server-side by the Business API and the agent. Treat the UI state
accordingly.

**No static type checking.** The Ruff rule set covers correctness, not types.
There is no mypy configuration.

## License and credits

Built by Vinicius Fraga. Powered by WSO2 Identity Server and Google Gemini.

Released under the Apache License 2.0; see [`LICENSE`](./LICENSE). Third-party
runtime dependencies are distributed under permissive licenses (BSD-3-Clause,
MIT or Apache-2.0), which are compatible with it.
