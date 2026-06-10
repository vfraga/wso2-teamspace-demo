import os
import time
import pytest
import json
from playwright.sync_api import Page, expect
from dotenv import load_dotenv

load_dotenv()

@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # Ignore HTTPS errors for the self-signed certificates on WSO2 (ports 9443 and 9444)
    return {
        **browser_context_args,
        "ignore_https_errors": True
    }

def handle_wso2_consent(page_or_popup):
    consent_selector = (
        "input[id='approve'], button[id='consent-approve-button'], "
        "button:has-text('Approve'), button:has-text('Accept'), "
        "button:has-text('Authorize'), input[type='button'][value^='Allow'], "
        "input[type='submit'][value^='Allow']"
    )
    try:
        page_or_popup.wait_for_selector(consent_selector, timeout=5000)
        
        # Check select all claims or other checkboxes if present
        try:
            select_all = page_or_popup.locator("#select_all_claims, input[id='select_all_claims']")
            if select_all.is_visible() and not select_all.is_checked():
                select_all.check()
            
            checkboxes = page_or_popup.locator("input[type='checkbox'][name='consentAgreement'], input[id='consentAgreement'], input[type='checkbox'][id^='consent_']")
            for i in range(checkboxes.count()):
                cb = checkboxes.nth(i)
                if cb.is_visible() and not cb.is_checked():
                    cb.check()
        except Exception:
            pass
            
        approve_btn = page_or_popup.locator(consent_selector).first
        if approve_btn.is_visible():
            approve_btn.click()
    except Exception:
        pass

@pytest.mark.live
def test_live_e2e_federated_idp_flow(browser, live_server_env):
    # Generate unique sub-organization handle and username to isolate state
    timestamp = int(time.time())
    sub_org = f"e2e-idp-{timestamp}"
    org_name = f"E2E IdP Org {timestamp}"
    username = f"admin@{sub_org}.com"
    password = "AdminPassword123!"

    # 1. Setup the Identity Provider as Org Admin
    context_admin = browser.new_context(ignore_https_errors=True)
    page = context_admin.new_page()

    # Step 1: Visit the landing page of the running live application (port 5001)
    page.goto("http://localhost:5001/", wait_until="domcontentloaded")
    expect(page).to_have_title("Teamspace")

    # Click "Get Started" to initiate registration
    page.click("text=Get Started")

    # Step 2: Register the new sub-organization dynamically
    page.wait_for_selector("#org_name", timeout=10000)
    page.fill("#org_name", org_name)
    page.fill("#org_handle", sub_org)
    page.fill("#first_name", "E2E")
    page.fill("#last_name", "Admin")
    page.fill("#email", username)
    page.fill("#password", password)

    # Click Next to go to Step 2 (Plan Selection)
    page.click("button:has-text('Next')")

    # Select the Enterprise plan (has access to IDPs)
    page.click("label.plan-card:has-text('Enterprise')")

    # Click Create Organization
    page.click("button:has-text('Create Organization')")

    # We are redirected to the landing page first to display registration logs. Click "Sign In" to proceed.
    page.wait_for_selector(".landing-actions a:has-text('Sign In')", timeout=10000)
    page.click(".landing-actions a:has-text('Sign In')")

    # Step 3: We are redirected to WSO2 Identity Server login page (port 9443)
    try:
        page.click("button[data-testid='cookie-consent-banner-confirm-button']", timeout=3000)
    except Exception:
        pass

    # Wait for username input and fill credentials
    username_selector = "input[name='usernameUserInput'], input[id='usernameUserInput']"
    page.wait_for_selector(username_selector, timeout=25000)
    page.fill(username_selector, username)

    password_selector = "input[name='password'], input[id='password']"
    page.fill(password_selector, password)

    # Click submit
    submit_selector = "button[id='sign-in-button'], button[type='submit'], #login-button"
    page.click(submit_selector)

    # Handle WSO2 consent screen if it appears
    handle_wso2_consent(page)

    # Step 4: Verify we are redirected back to the sub-organization dashboard
    expected_dashboard_url = f"http://localhost:5001/o/{sub_org}/"
    page.wait_for_url(expected_dashboard_url, timeout=25000, wait_until="domcontentloaded")
    expect(page.locator("h1")).to_contain_text("Welcome")

    # Step 5: Navigate to Identity Providers connection settings
    page.goto(f"http://localhost:5001/o/{sub_org}/admin/idp", wait_until="domcontentloaded")
    expect(page.locator("body")).to_contain_text("Identity Providers")

    # Click Add Connection
    page.click("text=Add Connection")
    page.wait_for_selector("#name", timeout=10000)

    # Load client credentials of the second WSO2 IS instance (port 9444)
    with open("scratch/idp_credentials.json", "r") as f:
        creds = json.load(f)
    client_id = creds["client_id"]
    client_secret = creds["client_secret"]

    # Fill form
    page.fill("#name", "Corporate-IDP")
    page.fill("#client_id", client_id)
    page.fill("#client_secret", client_secret)
    page.fill("#auth_endpoint", "https://localhost:9444/t/worklink.com/oauth2/authorize")
    page.fill("#token_endpoint", "https://localhost:9444/t/worklink.com/oauth2/token")

    # Create connection
    page.click("button:has-text('Create Connection')")

    # Verify connection created
    expect(page.locator("body")).to_contain_text("Identity Provider created.")
    expect(page.locator("body")).to_contain_text("Corporate-IDP")

    # Close Admin Context to clean up session
    context_admin.close()

    # ==========================================
    # TEST USER 1: John (should get teamspace-user role)
    # ==========================================
    context_john = browser.new_context(ignore_https_errors=True)
    page_john = context_john.new_page()

    page_john.goto("http://localhost:5001/login", wait_until="domcontentloaded")
    page_john.fill("input[name='org_handle']", sub_org)
    page_john.click("button[type='submit']")

    idp_button_selector = "button:has-text('Corporate-IDP'), a:has-text('Corporate-IDP'), [data-testid='idp-selector-Corporate-IDP']"
    page_john.wait_for_selector(idp_button_selector, timeout=25000)
    
    def log_request(request):
        if "9444" in request.url:
            print(f"DEBUG E2E REQUEST to 9444: {request.url}")
    page_john.on("request", log_request)
    
    page_john.click(idp_button_selector)

    # We are redirected to second server login (port 9444)
    page_john.wait_for_selector(username_selector, timeout=25000)
    page_john.fill(username_selector, "john@worklink.com")
    page_john.fill(password_selector, "Password123")
    page_john.click(submit_selector)

    # Handle port 9444 and port 9443 consents if they appear
    handle_wso2_consent(page_john)
    handle_wso2_consent(page_john)

    # Wait to land back on dashboard
    page_john.wait_for_url(expected_dashboard_url, timeout=25000, wait_until="domcontentloaded")
    expect(page_john.locator("h1")).to_contain_text("Welcome")
    expect(page_john.locator("body")).to_contain_text("john@worklink.com")

    # Assert John gets 403 when accessing administrative users console
    response = page_john.goto(f"http://localhost:5001/o/{sub_org}/admin/users", wait_until="domcontentloaded")
    assert response.status == 403

    # Close John's context to clean up federated session
    context_john.close()

    # ==========================================
    # TEST USER 2: Tom (should get teamspace-admin role)
    # ==========================================
    context_tom = browser.new_context(ignore_https_errors=True)
    page_tom = context_tom.new_page()

    page_tom.goto("http://localhost:5001/login", wait_until="domcontentloaded")
    page_tom.fill("input[name='org_handle']", sub_org)
    page_tom.click("button[type='submit']")

    # Click Corporate-IDP
    page_tom.wait_for_selector(idp_button_selector, timeout=25000)
    page_tom.click(idp_button_selector)

    # Login on second server (port 9444)
    page_tom.wait_for_selector(username_selector, timeout=25000)
    page_tom.fill(username_selector, "tom@worklink.com")
    page_tom.fill(password_selector, "Password123")
    page_tom.click(submit_selector)

    # Handle port 9444 and port 9443 consents if they appear
    handle_wso2_consent(page_tom)
    handle_wso2_consent(page_tom)

    # Wait to land back on dashboard
    page_tom.wait_for_url(expected_dashboard_url, timeout=25000, wait_until="domcontentloaded")
    expect(page_tom.locator("h1")).to_contain_text("Welcome")
    expect(page_tom.locator("body")).to_contain_text("tom@worklink.com")

    # Assert Tom can access the administrative users console (status 200)
    response = page_tom.goto(f"http://localhost:5001/o/{sub_org}/admin/users", wait_until="domcontentloaded")
    assert response.status == 200
    expect(page_tom.locator("body")).to_contain_text("Manage Users")

    # Close Tom's context
    context_tom.close()

    # ==========================================
    # EDIT & DELETE TEST: Admin (has idp-manager role)
    # ==========================================
    context_admin_edit = browser.new_context(ignore_https_errors=True)
    page_admin_edit = context_admin_edit.new_page()

    page_admin_edit.goto("http://localhost:5001/login", wait_until="domcontentloaded")
    page_admin_edit.fill("input[name='org_handle']", sub_org)
    page_admin_edit.click("button[type='submit']")

    # We are redirected to WSO2 Identity Server login page
    page_admin_edit.wait_for_selector(username_selector, timeout=25000)
    page_admin_edit.fill(username_selector, username)
    page_admin_edit.fill(password_selector, password)
    page_admin_edit.click(submit_selector)

    handle_wso2_consent(page_admin_edit)

    # Land on dashboard
    page_admin_edit.wait_for_url(expected_dashboard_url, timeout=25000, wait_until="domcontentloaded")

    # Navigate to IdP admin list page
    page_admin_edit.goto(f"http://localhost:5001/o/{sub_org}/admin/idp", wait_until="domcontentloaded")
    expect(page_admin_edit.locator("body")).to_contain_text("Corporate-IDP")

    # Click Edit on Corporate-IDP
    page_admin_edit.click("a:has-text('Edit')")

    # Wait for the Edit form to load
    page_admin_edit.wait_for_selector("#name", timeout=10000)
    expect(page_admin_edit.locator("#client_id")).to_have_value(client_id)

    # Modify client ID and add "manager" to the comma-separated external groups
    page_admin_edit.fill("#client_id", client_id + "-edited")
    page_admin_edit.fill("#groups", "admin, user, manager")

    # Submit the form
    page_admin_edit.click("button:has-text('Save Changes')")

    # Redirected back to /admin/idp, check for success message
    page_admin_edit.wait_for_url(f"http://localhost:5001/o/{sub_org}/admin/idp", timeout=10000, wait_until="domcontentloaded")
    expect(page_admin_edit.locator("body")).to_contain_text("Identity Provider updated successfully.")

    # Edit again to check the new "manager" checkbox and assign it
    page_admin_edit.click("a:has-text('Edit')")
    page_admin_edit.wait_for_selector("#name", timeout=10000)
    expect(page_admin_edit.locator("#client_id")).to_have_value(client_id + "-edited")
    
    # Check the "manager" checkbox for the teamspace-admin role
    manager_cb = page_admin_edit.locator(".role-mapping-row:has-text('teamspace-admin') input[value='manager']")
    expect(manager_cb).to_be_visible()
    manager_cb.check()

    # Save changes
    page_admin_edit.click("button:has-text('Save Changes')")
    page_admin_edit.wait_for_url(f"http://localhost:5001/o/{sub_org}/admin/idp", timeout=10000, wait_until="domcontentloaded")
    expect(page_admin_edit.locator("body")).to_contain_text("Identity Provider updated successfully.")

    # Test Deletion
    page_admin_edit.once("dialog", lambda dialog: dialog.accept())
    page_admin_edit.click("button:has-text('Delete')")

    # Redirected back, check for deletion success message
    page_admin_edit.wait_for_url(f"http://localhost:5001/o/{sub_org}/admin/idp", timeout=10000, wait_until="domcontentloaded")
    expect(page_admin_edit.locator("body")).to_contain_text("Identity Provider deleted successfully.")
    expect(page_admin_edit.locator(".idp-card")).to_have_count(0)

    # Close Admin context
    context_admin_edit.close()

