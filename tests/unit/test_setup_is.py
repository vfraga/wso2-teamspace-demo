import pytest
from unittest.mock import MagicMock, patch
import setup_is

def test_ok():
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404
    
    assert setup_is._ok(mock_resp_200) is True
    assert setup_is._ok(mock_resp_404) is False
    assert setup_is._ok(mock_resp_404, accept_codes=(200, 404)) is True

def test_create_tenant_already_exists():
    mock_client = MagicMock()
    
    # Mock response for get tenants returning an existing tenant
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "tenants": [
            {"domainName": "teamspace", "id": "mock-tenant-uuid-123"}
        ]
    }
    mock_client.get.return_value = mock_get_resp

    with patch("setup_is.step") as mock_step, \
         patch("setup_is.info") as mock_info:
         
        tenant_id = setup_is.create_tenant(mock_client)
        
        assert tenant_id == "mock-tenant-uuid-123"
        mock_step.assert_called_once()
        mock_info.assert_called_once_with("Tenant already exists (id=mock-tenant-uuid-123)")
