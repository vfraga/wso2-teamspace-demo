import os
import time
import pytest
from playwright.sync_api import Page, expect
from dotenv import load_dotenv
from webapp.plans import PLANS

# Load real environment variables from .env
load_dotenv()

@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # Ignore HTTPS errors for the self-signed certificates on WSO2 (port 9443)
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
@pytest.mark.parametrize("plan_id", [p["id"] for p in PLANS])
def test_live_e2e_obo_flow(page: Page, live_server_env, plan_id: str):
    # Generate unique sub-organization handle and username to guarantee reproducibility and isolate E2E state
    timestamp = int(time.time())
    sub_org = f"e2e-{plan_id}-{timestamp}"
    org_name = f"E2E {plan_id.capitalize()} Org {timestamp}"
    username = f"admin@{sub_org}.com"
    password = "AdminPassword123!"
    
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
    
    # Select the specific plan
    plan_name = next(p["name"] for p in PLANS if p["id"] == plan_id)
    page.click(f"label.plan-card:has-text('{plan_name}')")
    
    # Click Create Organization.
    page.click("button:has-text('Create Organization')")
    
    # We are redirected to the landing page first to display registration logs. Click "Sign In" to proceed.
    page.wait_for_selector(".landing-actions a:has-text('Sign In')", timeout=10000)
    page.click(".landing-actions a:has-text('Sign In')")
    
    # Step 3: We are redirected to WSO2 Identity Server login page
    # Dismiss cookie consent banner if present to avoid overlap issues
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
    
    # Step 4: Handle WSO2 consent screen if it appears
    handle_wso2_consent(page)
        
    # Step 5: Verify we are redirected back to the sub-organization dashboard
    expected_dashboard_url = f"http://localhost:5001/o/{sub_org}/"
    page.wait_for_url(expected_dashboard_url, timeout=25000, wait_until="domcontentloaded")
    expect(page.locator("h1")).to_contain_text("Welcome")
    expect(page.locator(".subtitle")).to_contain_text(org_name)
    expect(page.locator("body")).to_contain_text("Sign Out")
    
    # Step 5.5: Navigate to subscription page and verify the registered plan matches
    page.goto(f"http://localhost:5001/o/{sub_org}/subscription/", wait_until="domcontentloaded")
    page.wait_for_selector(".current-plan-badge", timeout=10000)
    expect(page.locator(".current-plan-badge")).to_have_text(plan_id.capitalize())
    
    if plan_id != "enterprise":
        # For non-enterprise plans, verify that AI Agents, Identity Providers, and Login Flow are gated
        page.goto(f"http://localhost:5001/o/{sub_org}/admin/agents/", wait_until="domcontentloaded")
        expect(page.locator(".upgrade-lock")).to_be_visible()
        expect(page.locator("body")).to_contain_text("Upgrade Required")
        
        page.goto(f"http://localhost:5001/o/{sub_org}/admin/idp", wait_until="domcontentloaded")
        expect(page.locator(".upgrade-lock")).to_be_visible()
        expect(page.locator("body")).to_contain_text("Upgrade Required")
        
        page.goto(f"http://localhost:5001/o/{sub_org}/admin/security/login-flow", wait_until="domcontentloaded")
        expect(page.locator(".upgrade-lock")).to_be_visible()
        expect(page.locator("body")).to_contain_text("Upgrade Required")
        return

    # Step 6: Configure and Deploy the AI Agent for this sub-organization
    # Go directly to AI Agents admin page
    page.goto(f"http://localhost:5001/o/{sub_org}/admin/agents/", wait_until="domcontentloaded")
    expect(page.locator("body")).to_contain_text("No AI agent deployed yet.")
    
    page.click("text=Deploy Agent")
    page.wait_for_selector("#display_name", timeout=10000)
    page.fill("#display_name", "E2E Agent")
    page.fill("#description", "Playwright E2E Meeting Scheduling Agent")
    
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    page.fill("#gemini_api_key", gemini_key)
    page.click("button[type='submit']")
    
    # Confirm successful deployment redirection
    expect(page.locator("body")).to_contain_text("deployed successfully")
    expect(page.locator("body")).to_contain_text("E2E Agent")
    
    # Navigate back to dashboard home
    page.goto(f"http://localhost:5001/o/{sub_org}/", wait_until="domcontentloaded")
    
    # Step 7: Open the AI Assistant side panel
    expect(page.locator(".chat-toggle")).to_be_visible()
    page.click("text=AI Assistant")
    expect(page.locator(".chat-window")).to_be_visible()
    
    # Step 8: Ask the assistant to schedule a meeting
    meeting_topic = f"E2E Live Meeting {timestamp}"
    page.fill(".chat-input input[name='message']", f"Schedule a meeting for tomorrow at 2 PM. Topic: {meeting_topic}.")
    page.click(".chat-input button[type='submit']")
    
    # Wait for the AI preview response and authorize link
    expect(page.locator(".chat-messages")).to_contain_text("Authorize Meeting", timeout=30000)
    
    # Step 9: Click the authorization link and handle the OBO popup
    with page.expect_popup() as popup_info:
        page.click("a:has-text('Authorize Meeting')")
    popup = popup_info.value
    
    # Dismiss cookie consent in popup if present
    try:
        popup.click("button[data-testid='cookie-consent-banner-confirm-button']", timeout=3000)
    except Exception:
        pass

    # Handle credentials inside OBO popup if prompted
    popup_username_selector = "input[name='usernameUserInput'], input[id='usernameUserInput'], input[name='username']"
    try:
        popup.wait_for_selector(popup_username_selector, timeout=5000)
        popup.fill(popup_username_selector, username)
        popup.fill(password_selector, password)
        popup.click(submit_selector)
    except Exception:
        pass
        
    # Handle OBO consent in popup
    handle_wso2_consent(popup)
        
    # Wait for popup to close itself after callback redirect
    try:
        popup.wait_for_event("close", timeout=15000)
    except Exception:
        pass
        
    # Step 10: Tell the assistant we authorized it (if not already automatically submitted by the callback event listener)
    try:
        expect(page.locator(".chat-messages")).to_contain_text("Authorized. Please check.", timeout=5000)
    except AssertionError:
        page.fill(".chat-input input[name='message']", "Authorized. Please check.")
        page.click(".chat-input button[type='submit']")
    
    # Wait for the confirmation response from the assistant
    expect(page.locator(".chat-messages")).to_contain_text("successfully", timeout=30000)
    
    # Step 11: Navigate to the Meetings page and verify the live booked meeting appears
    page.goto(f"http://localhost:5001/o/{sub_org}/meetings", wait_until="domcontentloaded")
    expect(page.locator(".meeting-list")).to_contain_text(meeting_topic, timeout=15000)
    expect(page.locator(".meeting-list")).to_contain_text("14:00", timeout=15000)
