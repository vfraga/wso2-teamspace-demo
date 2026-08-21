"""Configuration surface of the federated-IdP bootstrap script.

The script is env-driven so the demo can be pointed at a differently-named IdP,
but the live E2E suite signs in as john@worklink.com and tom@worklink.com — so
the defaults have to keep producing exactly those accounts. That is what these
pin: not the WSO2 calls, which need a live server, but the values derived
before any call is made.
"""

import contextlib
import importlib
import os
from unittest.mock import MagicMock, patch

import setup_idp_server

_TENANT_VARS = (
    "FEDERATED_IDP_TENANT_DOMAIN",
    "FEDERATED_IDP_TENANT_ADMIN_USERNAME",
    "FEDERATED_IDP_TENANT_ADMIN_EMAIL",
    "FEDERATED_IS_BASE_URL",
)


@contextlib.contextmanager
def _reloaded(**env):
    """Re-import the module under a given environment, then put it back.

    Every derived value (endpoints, admin login, user emails) is computed at
    import time, so overriding one means reloading. The developer's own .env is
    restored afterwards so ordering between tests cannot matter.
    """
    saved = {k: os.environ.get(k) for k in _TENANT_VARS}
    for key in _TENANT_VARS:
        os.environ.pop(key, None)
    os.environ.update(env)
    try:
        yield importlib.reload(setup_idp_server)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(setup_idp_server)


def test_defaults_produce_the_accounts_the_live_suite_signs_in_as():
    with _reloaded(FEDERATED_IS_BASE_URL="https://localhost:9444") as m:
        assert m.TENANT_DOMAIN == "worklink.com"
        assert m.TENANT_ADMIN_AUTH[0] == "teamspaceadmin@worklink.com"
        assert m.TENANT_API == "https://localhost:9444/t/worklink.com/api/server/v1"
        assert m.TENANT_SCIM == "https://localhost:9444/t/worklink.com/scim2"
        assert [u["username"] for u in m.USERS_TO_CREATE] == ["john", "tom"]


def test_tenant_domain_drives_every_derived_value():
    with _reloaded(
        FEDERATED_IDP_TENANT_DOMAIN="example.test",
        FEDERATED_IDP_TENANT_ADMIN_USERNAME="root",
        FEDERATED_IS_BASE_URL="https://idp.example:9444",
    ) as m:
        assert m.TENANT_ADMIN_AUTH[0] == "root@example.test"
        assert m.TENANT_API == "https://idp.example:9444/t/example.test/api/server/v1"
        assert m.TENANT_SCIM == "https://idp.example:9444/t/example.test/scim2"


def test_admin_email_is_not_derived_from_the_tenant_domain():
    # Deliberate: this is the owner's email *attribute*, and setup_is.py uses
    # the same placeholder on the primary tenant. Deriving it would change what
    # a fresh bootstrap writes. Pinned so it is not "tidied up" by accident.
    with _reloaded(FEDERATED_IDP_TENANT_DOMAIN="example.test") as m:
        assert m.TENANT_ADMIN_EMAIL == "teamspaceadmin@mail.com"

    with _reloaded(FEDERATED_IDP_TENANT_ADMIN_EMAIL="owner@example.test") as m:
        assert m.TENANT_ADMIN_EMAIL == "owner@example.test"


def test_seeded_user_emails_follow_the_tenant_domain():
    with _reloaded(FEDERATED_IDP_TENANT_DOMAIN="example.test") as m:
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200, **{"json.return_value": {"Resources": []}})
        session.post.return_value = MagicMock(status_code=201, **{"json.return_value": {"id": "uid-1"}})

        with patch.object(m, "FEDERATED_USER_PASSWORD", "pw"), \
             patch.object(m, "step"), patch.object(m, "info"):
            user_ids = m._bootstrap_users(session, [
                {"username": "john", "firstname": "John", "lastname": "Doe", "group": "user"},
            ])

        assert user_ids == {"john": "uid-1"}
        payload = session.post.call_args.kwargs["json"]
        assert payload["emails"] == [{"value": "john@example.test", "primary": True}]


def test_an_existing_tenant_is_left_alone():
    with _reloaded() as m:
        session = MagicMock()
        session.get.return_value = MagicMock(
            status_code=200, **{"json.return_value": {"tenants": [{"id": "t-1"}]}}
        )

        with patch.object(m, "step"), patch.object(m, "info") as mock_info:
            m._bootstrap_tenant(session)

        session.post.assert_not_called()
        mock_info.assert_called_once_with("Tenant already exists")
