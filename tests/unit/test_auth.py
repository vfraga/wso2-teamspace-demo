from unittest.mock import MagicMock, patch, ANY
from webapp.auth import init_oauth, oauth

def test_init_oauth():
    app = MagicMock()
    app.config = {
        "IS_BASE_URL": "https://localhost:9443",
        "TENANT_PATH": "/t/teamspace",
        "CLIENT_ID": "mock-client-id",
        "CLIENT_SECRET": "mock-client-secret",
        "OIDC_SCOPES": "openid email profile",
    }

    # Patch oauth.init_app and oauth.register
    with patch.object(oauth, "init_app") as mock_init_app, \
         patch.object(oauth, "register") as mock_register:
         
        init_oauth(app)
        
        mock_init_app.assert_called_once_with(app)
        mock_register.assert_called_once_with(
            name="wso2is",
            client_id="mock-client-id",
            client_secret="mock-client-secret",
            server_metadata_url="https://localhost:9443/t/teamspace/oauth2/token/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
                "token_endpoint_auth_method": "client_secret_post",
                "verify": False,
            },
            fetch_token=ANY,
        )
