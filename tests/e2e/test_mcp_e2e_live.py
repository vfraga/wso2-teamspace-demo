import os
import time
import pytest
import requests
import httpx
import urllib3
import json
import threading

# Ignore self-signed HTTPS certificate warnings for localhost WSO2 IS
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@pytest.mark.live
def test_live_mcp_server_sse_endpoints(live_server_env):
    """
    Directly exercises the secure MCP Server endpoints over SSE and HTTP
    utilizing a live OAuth 2.1 Bearer token from the spawned WSO2 IS instance.
    Runs the SSE stream reader on a daemon background thread to keep the
    session connection alive during concurrent HTTP POST tool dispatches.
    """
    client_id = live_server_env["client_id"]
    client_secret = live_server_env["client_secret"]

    print("\n[E2E MCP Test] Using Dynamic B2B Client Credentials:")
    print(f"  CLIENT_ID: {client_id}")

    # -------------------------------------------------------------
    # Step 1: Fetch a real OAuth 2.1 access token from live WSO2 IS first
    # -------------------------------------------------------------
    token_url = "https://localhost:9443/t/teamspace/oauth2/token"
    print(f"[E2E MCP Test] Exchanging client credentials at: {token_url}")

    token_payload = {
        "grant_type": "client_credentials",
        "scope": "create_meeting_agent list_meetings_agent delete_meeting update_meeting",
    }
    
    resp = requests.post(
        token_url,
        data=token_payload,
        auth=(client_id, client_secret),
        verify=False,
        timeout=10.0
    )
    assert resp.status_code == 200, f"Token exchange failed: {resp.text}"
    token_data = resp.json()
    access_token = token_data.get("access_token")
    assert access_token is not None, "No access_token found in WSO2 response"
    print("[E2E MCP Test] Successfully acquired live OAuth 2.1 JWT Access Token")

    # -------------------------------------------------------------
    # Step 2: Establish persistent SSE stream in a background thread
    # -------------------------------------------------------------
    mcp_sse_url = "http://localhost:8000/mcp/sse"
    print(f"[E2E MCP Test] Spawning background thread to connect to SSE: {mcp_sse_url}")

    session_id_list = [None]
    ready_event = threading.Event()
    stop_stream_event = threading.Event()

    def sse_reader_thread():
        try:
            with httpx.Client(timeout=30.0) as client:
                with client.stream("GET", mcp_sse_url) as stream_resp:
                    if stream_resp.status_code != 200:
                        print(f"Background thread SSE connection failed: {stream_resp.status_code}")
                        ready_event.set()
                        return
                    
                    event_buf = ""
                    for line in stream_resp.iter_lines():
                        if stop_stream_event.is_set():
                            break
                        if line:
                            event_buf += line + "\n"
                            if line.startswith("data:"):
                                # Parse out session_id
                                for l in event_buf.splitlines():
                                    if l.startswith("data:"):
                                        data_val = l.replace("data:", "").strip()
                                        if "session_id=" in data_val:
                                            session_id_list[0] = data_val.split("session_id=")[1]
                                            ready_event.set()
                                            break
        except Exception as e:
            print(f"Background SSE thread encountered an exception: {e}")
            ready_event.set()

    t = threading.Thread(target=sse_reader_thread, daemon=True)
    t.start()

    # Wait for the background thread to negotiate the SSE Session ID
    assert ready_event.wait(timeout=10.0), "Timeout waiting for background SSE Session ID"
    session_id = session_id_list[0]
    assert session_id is not None, "Failed to negotiate MCP SSE Session ID"
    print(f"[E2E MCP Test] Negotiated MCP Session ID: {session_id}")

    try:
        # -------------------------------------------------------------
        # Step 3: Call JSON-RPC tools/list while the SSE stream is open
        # -------------------------------------------------------------
        messages_url = f"http://localhost:8000/mcp/messages/?session_id={session_id}"
        print(f"[E2E MCP Test] Invoking JSON-RPC 'tools/list' via: {messages_url}")

        rpc_list_tools = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        }

        list_headers = {
            "Authorization": f"Bearer {access_token}"
        }
        
        list_resp = requests.post(
            messages_url,
            json=rpc_list_tools,
            headers=list_headers,
            timeout=5.0
        )
        assert list_resp.status_code == 202 or list_resp.status_code == 200, f"List tools failed: {list_resp.text}"
        print(f"[E2E MCP Test] List tools response status: {list_resp.status_code}")

        # -------------------------------------------------------------
        # Step 4: Verify Zero-Trust Authorization on tool execution
        # -------------------------------------------------------------
        rpc_call_tool = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "list_meetings",
                "arguments": {
                    "thread_id": "e2e-mcp-thread-99"
                }
            },
            "id": 2
        }

        # Call WITHOUT token (must fail or reject with Unauthorized inside execution)
        print("[E2E MCP Test] Verifying Zero-Trust boundary (missing Bearer token)...")
        bad_resp = requests.post(
            messages_url,
            json=rpc_call_tool,
            timeout=5.0
        )
        assert bad_resp.status_code in (200, 202), f"Missing token POST failed: {bad_resp.text}"
        body_text = bad_resp.text or ""
        assert (
            "Unauthorized" in body_text or "Missing bearer token" in body_text
        ), f"Expected 'Unauthorized' or 'Missing bearer token' in response body, got: {body_text!r}"
        print("[E2E MCP Test] Missing token POST correctly returned Unauthorized in body.")

        # Call WITH valid token (Query Parameter)
        print("[E2E MCP Test] Calling list_meetings tool WITH query parameter token...")
        query_messages_url = f"{messages_url}&token={access_token}"
        query_resp = requests.post(
            query_messages_url,
            json=rpc_call_tool,
            timeout=5.0
        )
        assert query_resp.status_code == 200 or query_resp.status_code == 202, f"Query token call failed: {query_resp.text}"
        print("[E2E MCP Test] Query parameter token call accepted successfully.")

        # Call WITH valid token (Header)
        print("[E2E MCP Test] Calling list_meetings tool WITH live Bearer header token...")
        good_resp = requests.post(
            messages_url,
            json=rpc_call_tool,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5.0
        )
        assert good_resp.status_code == 200 or good_resp.status_code == 202, f"Bearer header call failed: {good_resp.text}"
        print("[E2E MCP Test] Secure MCP server live test completed successfully!")
    finally:
        # Clean up the background thread
        stop_stream_event.set()
