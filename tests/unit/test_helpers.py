from webapp.utils.helpers import slugify, mask_token, get_sidebar_items
from webapp.utils.roles import TEAMSPACE_ADMIN

def test_slugify():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Teamspace B2B CIAM!!  ") == "teamspace-b2b-ciam"
    assert slugify("lowercase-only_with_underscores") == "lowercase-only-with-underscores"
    assert slugify("multiple---hyphens") == "multiple-hyphens"
    assert slugify("") == ""

def test_mask_token():
    # Long token
    token = "abcdefghijklmnopqrstuvwxyz1234567890"
    masked = mask_token(token, visible=5)
    assert masked.startswith("abcde...")
    assert masked.endswith("7890")
    assert len(masked) == 5 + 3 + 4  # visible prefix + '...' + 4 chars suffix
    
    # Short token
    short_token = "12345"
    assert mask_token(short_token, visible=10) == "12345"

def test_get_sidebar_items_admin():
    # Admin roles should receive all matching sidebar items, including user, roles, agents, idp managers
    roles = [TEAMSPACE_ADMIN]
    items = get_sidebar_items("numbainfinite", roles)
    
    # Extract list of IDs
    item_ids = [item["id"] for item in items]
    
    assert "home" in item_ids
    assert "users" in item_ids
    assert "roles" in item_ids
    assert "agents" in item_ids
    assert "security" in item_ids
    
    # Check that org_handle is formatted correctly
    for item in items:
        assert "numbainfinite" in item["href"]

def test_get_sidebar_items_regular_user():
    # Regular user with no special roles should only get basic links
    roles = []
    items = get_sidebar_items("numbainfinite", roles)
    item_ids = [item["id"] for item in items]
    
    assert "home" in item_ids
    assert "meetings" in item_ids
    assert "subscription" not in item_ids
    
    # Should not have admin links
    assert "users" not in item_ids
    assert "roles" not in item_ids
    assert "agents" not in item_ids
    assert "security" not in item_ids
