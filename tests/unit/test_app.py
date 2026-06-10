from unittest.mock import patch, MagicMock
from webapp.app import create_app
from webapp.config import Config

def test_flask_create_app():
    with patch("webapp.app.init_oauth") as mock_init_oauth, \
         patch("flask_session.Session") as mock_session:
         
        app = create_app()
        assert app is not None
        
        # Verify custom configs were initialized
        assert app.config["SESSION_TYPE"] == "cachelib"
        assert app.config["SESSION_CACHELIB"] is not None
        
        # Verify TENANT_PATH computed dynamically
        expected_tenant_path = f"/t/{app.config['IS_ORG_HANDLE']}" if app.config["IS_ORG_HANDLE"] else ""
        assert app.config["TENANT_PATH"] == expected_tenant_path

        mock_init_oauth.assert_called_once_with(app)
        mock_session.assert_called_once_with(app)

def test_webapp_config_structure():
    assert Config.SECRET_KEY is not None
    assert Config.SESSION_TYPE == "cachelib"
    assert Config.SESSION_CACHELIB is None  # Lazy loading initial state
    assert Config.TENANT_PATH in ("", f"/t/{Config.IS_ORG_HANDLE}", "/t/teamspace")

