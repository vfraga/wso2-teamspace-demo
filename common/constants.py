"""Shared constants used by multiple services.

Centralising these eliminates duplicated magic strings across webapp, api, and agent code.
"""

# Branding defaults
DEFAULT_PRIMARY_COLOR = "#4F46E5"
DEFAULT_SECONDARY_COLOR = "#E0E7FF"

# Agent defaults
DEFAULT_AGENT_NAME = "Worklink Assistant"

# Plan defaults
DEFAULT_PLAN = "basic"

# WSO2 IS Authenticator IDs
# base64('OpenIDConnectAuthenticator')
OIDC_AUTHENTICATOR_ID = "T3BlbklEQ29ubmVjdEF1dGhlbnRpY2F0b3I"
# Decoded form — WSO2 IS expects this as the `name` field on the
# federated-authenticator PUT body (per IS API contract).
OIDC_AUTHENTICATOR_NAME = "OpenIDConnectAuthenticator"
