#!/usr/bin/env python3
"""
Teamspace Secondary IdP Setup Script

Copies the WSO2 IS 7.2.0 source template to a secondary directory,
sets the port offset to 1 in deployment.toml (to run on port 9444),
starts the server in the background, waits for it to become healthy,
and bootstraps the Federated Identity Provider (worklink.com, users, groups).

Usage:
    python setup_secondary_is.py
"""

import os
import re
import shutil
import subprocess
import sys
import time
import urllib3
import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Load environment variables
load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

# No default: the template is a multi-GB WSO2 install that only the operator can
# point us at, and a fallback path is either wrong or someone's home directory.
# An unset value fails in setup_directory() with the message that says so.
DEFAULT_SRC_PATH = os.environ.get("WSO2_IS_TEMPLATE_PATH", "")
# Named after whatever the source is called, so the copy does not pin a WSO2
# version that goes stale the next time the template is upgraded.
DEFAULT_DST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    (os.path.basename(DEFAULT_SRC_PATH.rstrip("/")) or "wso2is") + "-secondary",
)

# ─── API Setup Configuration ──────────────────────────────────────────────────

# Only the hostname is configurable: the port stays 9444, it comes from the
# port offset written into the secondary instance's deployment.toml below.
BASE_URL = os.environ.get("FEDERATED_IS_BASE_URL", "https://localhost:9444").rstrip("/")
SUPER_ADMIN_USERNAME = os.environ.get("IS_SUPER_ADMIN_USERNAME", "admin")
SUPER_ADMIN_PASSWORD = os.environ.get("IS_SUPER_ADMIN_PASSWORD", "")
if not SUPER_ADMIN_PASSWORD:
    import warnings
    warnings.warn("IS_SUPER_ADMIN_PASSWORD not set — IS API calls will fail with 401", RuntimeWarning, stacklevel=2)
SUPER_ADMIN_AUTH = (SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)

# The federated tenant's identity deliberately does NOT live here. This script
# only prepares and starts the server; bootstrap_idp_config() below delegates to
# setup_idp_server.bootstrap_federated_idp(), which reads FEDERATED_IDP_TENANT_*.
# Local copies could only ever drift from the tenant actually being created.
SERVER_API = f"{BASE_URL}/api/server/v1"

# ─── Helpers ─────────────────────────────────────────────────────────────────

def step(num, msg):
    print(f"\n{'─' * 60}")
    print(f"  Step {num}: {msg}")
    print(f"{'─' * 60}")

def info(msg):
    print(f"  ✓ {msg}")

def warn(msg):
    print(f"  ⚠ {msg}")

def fail(msg):
    print(f"  ✗ {msg}", file=sys.stderr)

def _session():
    s = requests.Session()
    s.verify = False
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s

def _ok(resp, accept_codes=(200, 201)):
    return resp.status_code in accept_codes

# ─── Step Functions ──────────────────────────────────────────────────────────

def setup_directory():
    step(1, "Preparing secondary WSO2 IS directory")
    
    src_wso2 = DEFAULT_SRC_PATH
    dst_wso2 = DEFAULT_DST_PATH
    
    if not os.path.exists(src_wso2):
        fail(f"Source WSO2 IS template not found at {src_wso2}.")
        fail("Please set WSO2_IS_TEMPLATE_PATH in your .env file or run with correct permissions.")
        sys.exit(1)
        
    info(f"Source WSO2 IS path: {src_wso2}")
    info(f"Destination path:   {dst_wso2}")
    
    if os.path.exists(dst_wso2):
        print(f"  Target directory '{dst_wso2}' already exists.")
        choice = input("  Do you want to re-clone the directory? (y/N): ").strip().lower()
        if choice == 'y':
            print(f"  Removing existing directory: {dst_wso2}...")
            shutil.rmtree(dst_wso2)
            print("  Cloning primary WSO2 IS instance...")
            subprocess.run(["cp", "-R", src_wso2, dst_wso2], check=True)
            info("Cloned fresh instance successfully.")
        else:
            info("Using existing directory. Skipping clone.")
    else:
        print("  Cloning primary WSO2 IS instance...")
        os.makedirs(os.path.dirname(dst_wso2), exist_ok=True)
        subprocess.run(["cp", "-R", src_wso2, dst_wso2], check=True)
        info("Cloned fresh instance successfully.")
        
    return dst_wso2

def configure_port_offset(dst_wso2):
    step(2, "Configuring port offset = 1 in deployment.toml")
    
    toml_path = os.path.join(dst_wso2, "repository", "conf", "deployment.toml")
    if not os.path.exists(toml_path):
        fail(f"deployment.toml not found at {toml_path}!")
        sys.exit(1)
        
    with open(toml_path, "r") as f:
        content = f.read()
        
    if "offset =" in content:
        content = re.sub(r"offset\s*=\s*\d+", "offset = 1", content)
    else:
        # Check if [server] block exists
        if "[server]" in content:
            content = content.replace("[server]", "[server]\noffset = 1")
        else:
            content = "[server]\noffset = 1\n\n" + content
            
    with open(toml_path, "w") as f:
        f.write(content)
        
    info("deployment.toml updated with offset = 1")

def start_server(dst_wso2):
    step(3, "Starting WSO2 IS secondary instance")
    
    bin_dir = os.path.join(dst_wso2, "bin")
    
    # Check if a process is already running on port 9444
    try:
        output = subprocess.check_output(["lsof", "-t", "-i:9444"]).decode().strip()
        if output:
            warn(f"Port 9444 is already in use by process PID(s): {output.replace(chr(10), ', ')}")
            choice = input("  Do you want to proceed and attempt setup? (Y/n): ").strip().lower()
            if choice == 'n':
                sys.exit(0)
            return
    except Exception:
        pass

    # Launch using wso2server.sh start (which daemonizes it)
    print("  Launching daemon via `./wso2server.sh start`...")
    cmd = ["bash", "./wso2server.sh", "start"]
    subprocess.run(cmd, cwd=bin_dir, check=True)
    info("Daemon start signal sent.")

def wait_for_health():
    step(4, "Waiting for secondary WSO2 IS instance to become healthy")
    
    s = _session()
    start_time = time.time()
    healthy = False
    timeout = 180  # 3 minutes maximum
    
    print(f"  Polling health endpoint: {SERVER_API}/tenants (Timeout: {timeout}s)")
    while time.time() - start_time < timeout:
        try:
            resp = s.get(f"{SERVER_API}/tenants", auth=SUPER_ADMIN_AUTH, timeout=2)
            if resp.status_code == 200:
                healthy = True
                break
        except Exception:
            pass
        
        print(".", end="", flush=True)
        time.sleep(3)
        
    print() # New line after dots
    
    if healthy:
        info("Secondary WSO2 IS is healthy and online on port 9444!")
    else:
        fail("Timed out waiting for secondary WSO2 IS instance to become healthy.")
        fail("Please check logs in repository/logs/wso2carbon.log under the secondary directory.")
        sys.exit(1)

def bootstrap_idp_config():
    step(5, "Bootstrapping secondary IdP configuration via REST APIs")
    from setup_idp_server import bootstrap_federated_idp
    bootstrap_federated_idp()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Teamspace Secondary IdP Setup")
    print("  Port: 9444 (Offset = 1)")
    print("=" * 60)

    dst_wso2 = setup_directory()
    configure_port_offset(dst_wso2)
    start_server(dst_wso2)
    wait_for_health()
    bootstrap_idp_config()

if __name__ == "__main__":
    main()
