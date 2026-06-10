import os
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import get_db
from api.models import Base
from api.main import app as api_app
from agent.main import app as agent_app
from webapp.app import create_app

# Load environment variables
load_dotenv()


# Temporary file-based SQLite for testing to avoid in-memory multi-connection issues
TEST_DB_FILE = "test_teamspace.db"
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_FILE}"

@pytest.fixture(name="db_session")
def db_session_fixture():
    # Remove old test DB if it exists
    if os.path.exists(TEST_DB_FILE):
        try:
            os.remove(TEST_DB_FILE)
        except Exception:
            pass
            
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    # Create tables
    Base.metadata.create_all(bind=engine)
    
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        # Clean up database file
        if os.path.exists(TEST_DB_FILE):
            try:
                os.remove(TEST_DB_FILE)
            except Exception:
                pass


@pytest.fixture(autouse=True)
def reset_agent_singletons():
    try:
        from agent.state_manager import StateManager
        StateManager.reset()
    except (AttributeError, Exception):
        pass
    try:
        from agent.auth_manager import AuthManager
        AuthManager.reset()
    except (AttributeError, Exception):
        pass
    try:
        from agent.chat_history import ChatHistoryManager
        ChatHistoryManager.reset()
    except (AttributeError, Exception):
        pass
    yield


@pytest.fixture
def api_client(db_session):
    # Override get_db in api dependency injection to inject our test in-memory database
    def override_get_db():
        try:
            yield db_session
        finally:
            pass
            
    api_app.dependency_overrides[get_db] = override_get_db
    with TestClient(api_app) as client:
        yield client
    api_app.dependency_overrides.clear()

@pytest.fixture
def agent_client():
    # FastAPI client for the AI Agent service
    with TestClient(agent_app) as client:
        yield client

@pytest.fixture
def flask_app():
    # Flask application instance for testing
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": False,  # Disable CSRF for easier form submission testing
    })
    yield app

@pytest.fixture
def flask_client(flask_app):
    # Test client for making requests to the Flask Web App
    return flask_app.test_client()


def kill_process_on_port(port):
    import subprocess
    import signal
    import os
    try:
        output = subprocess.check_output(["lsof", "-t", f"-i:{port}"]).decode().strip()
        if output:
            for pid_str in output.split("\n"):
                pid = int(pid_str)
                print(f"  Killing process {pid} on port {port}")
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
    except Exception:
        pass


def _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, service_procs, db_file):
    import signal
    import os
    import shutil
    
    print("  Stopping microservices process groups...")
    for proc, log in service_procs:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log.close()
        except Exception:
            pass
            
    print("  Stopping primary WSO2 IS process group...")
    try:
        os.killpg(os.getpgid(wso2_proc.pid), signal.SIGKILL)
    except Exception:
        try:
            wso2_proc.kill()
        except Exception:
            pass
    try:
        wso2_log.close()
    except Exception:
        pass

    print("  Stopping second WSO2 IS (IdP) process group...")
    if wso2_idp_proc:
        try:
            os.killpg(os.getpgid(wso2_idp_proc.pid), signal.SIGKILL)
        except Exception:
            try:
                wso2_idp_proc.kill()
            except Exception:
                pass
    if wso2_idp_log:
        try:
            wso2_idp_log.close()
        except Exception:
            pass
        
    print("\n  ==== [DEBUG] wso2_idp_stdout.log ====")
    idp_log_path = os.path.join(temp_dir, "wso2_idp_stdout.log")
    if os.path.exists(idp_log_path):
        try:
            with open(idp_log_path, "r") as f:
                lines = f.readlines()
                print("".join(lines[-1000:]))
        except Exception as e:
            print(f"Failed to read idp log: {e}")

    print("\n  ==== [DEBUG] wso2_stdout.log ====")
    primary_log_path = os.path.join(temp_dir, "wso2_stdout.log")
    if os.path.exists(primary_log_path):
        try:
            with open(primary_log_path, "r") as f:
                lines = f.readlines()
                print("".join(lines[-1000:]))
        except Exception as e:
            print(f"Failed to read primary log: {e}")

    # Print Agent log to assist debugging
    print("\n  ==== [DEBUG] agent_stdout.log ====")
    agent_log_path = os.path.join(temp_dir, "agent_stdout.log")
    if os.path.exists(agent_log_path):
        try:
            with open(agent_log_path, "r") as f:
                print(f.read())
        except Exception as e:
            print(f"Failed to read agent_log: {e}")
            
    # Print WebApp log to assist debugging
    print("\n  ==== [DEBUG] webapp_stdout.log ====")
    webapp_log_path = os.path.join(temp_dir, "webapp_stdout.log")
    if os.path.exists(webapp_log_path):
        try:
            with open(webapp_log_path, "r") as f:
                print(f.read())
        except Exception as e:
            print(f"Failed to read webapp_log: {e}")

    # Print Business API log to assist debugging
    print("\n  ==== [DEBUG] api_stdout.log ====")
    api_log_path = os.path.join(temp_dir, "api_stdout.log")
    if os.path.exists(api_log_path):
        try:
            with open(api_log_path, "r") as f:
                print(f.read())
        except Exception as e:
            print(f"Failed to read api_log: {e}")

    print(f"  Cleaning up WSO2 temp directory: {temp_dir}")
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass
        
    print(f"  Cleaning up test live database files: {db_file}")
    for ext in ["", "-shm", "-wal"]:
        path = f"{db_file}{ext}"
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    print("  Teardown completed.")


def _wait_for_service(url: str, name: str, timeout: int = 30) -> bool:
    import time
    import requests

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                print(f"[live_server_env] {name} is healthy at {url}")
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"[live_server_env] WARNING: {name} did not become healthy at {url} within {timeout}s")
    return False


@pytest.fixture(scope="session")
def live_server_env():
    import sys
    import time
    import tempfile
    import subprocess
    import requests
    import urllib3
    
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print("\n[live_server_env] Starting programmatic environment setup...")
    
    # 1. Clean up ports
    target_ports = [9443, 9444, 5001, 9091, 8000]
    print("[live_server_env] Performing process cleanup on ports 9443, 9444, 5001, 9091, 8000...")
    for port in target_ports:
        kill_process_on_port(port)
        
    # 2. Clean up old live db
    db_file = "test_live_teamspace.db"
    for ext in ["", "-shm", "-wal"]:
        path = f"{db_file}{ext}"
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
                
    # 3. Create temp directory and clone WSO2 IS 7.2.0.24
    temp_dir = tempfile.mkdtemp(prefix="wso2_e2e_")
    src_wso2 = os.getenv("WSO2_IS_TEMPLATE_PATH")
    if not src_wso2:
        _teardown_live_env(None, None, None, None, temp_dir, [], db_file)
        pytest.skip("WSO2_IS_TEMPLATE_PATH env var is required for live E2E tests")
    
    # Clone Primary Instance
    dst_wso2 = os.path.join(temp_dir, "wso2is-7.2.0.24")
    print(f"[live_server_env] Cloning primary WSO2 IS 7.2.0.24 to {dst_wso2}...")
    subprocess.run(["cp", "-R", src_wso2, dst_wso2], check=True)
    
    # Clone Federated IdP Instance
    dst_wso2_idp = os.path.join(temp_dir, "wso2is-7.2.0.24-idp")
    print(f"[live_server_env] Cloning second WSO2 IS (IdP) to {dst_wso2_idp}...")
    subprocess.run(["cp", "-R", src_wso2, dst_wso2_idp], check=True)

    # Configure Port Offset = 1 for the second instance
    toml_path = os.path.join(dst_wso2_idp, "repository", "conf", "deployment.toml")
    with open(toml_path, "r") as f:
        content = f.read()
    if "offset =" in content:
        import re
        content = re.sub(r"offset\s*=\s*\d+", "offset = 1", content)
    else:
        content = content.replace("[server]", "[server]\noffset = 1")
    with open(toml_path, "w") as f:
        f.write(content)
    
    # 4. Start primary and secondary WSO2 IS servers concurrently
    print("[live_server_env] Spawning primary WSO2 IS server process...")
    wso2_log_path = os.path.join(temp_dir, "wso2_stdout.log")
    wso2_log = open(wso2_log_path, "w")
    wso2_proc = subprocess.Popen(
        ["bash", "wso2server.sh"],
        stdout=wso2_log,
        stderr=wso2_log,
        cwd=os.path.join(dst_wso2, "bin"),
        preexec_fn=os.setsid
    )

    print("[live_server_env] Spawning second WSO2 IS (IdP) server process...")
    wso2_idp_log_path = os.path.join(temp_dir, "wso2_idp_stdout.log")
    wso2_idp_log = open(wso2_idp_log_path, "w")
    wso2_idp_proc = subprocess.Popen(
        ["bash", "wso2server.sh", "-DportOffset=1"],
        stdout=wso2_idp_log,
        stderr=wso2_idp_log,
        cwd=os.path.join(dst_wso2_idp, "bin"),
        preexec_fn=os.setsid
    )

    print("[live_server_env] Waiting for both WSO2 IS instances to become healthy concurrently...")
    start_time = time.time()
    primary_healthy = False
    idp_healthy = False
    while time.time() - start_time < 120:
        if not primary_healthy:
            if wso2_proc.poll() is not None:
                print("[live_server_env] ERROR: Primary WSO2 IS exited prematurely!")
                break
            try:
                resp = requests.get(
                    "https://localhost:9443/api/server/v1/tenants",
                    auth=("admin", "admin"),
                    verify=False,
                    timeout=2
                )
                if resp.status_code == 200:
                    primary_healthy = True
                    print("[live_server_env] Primary WSO2 IS is healthy.")
            except Exception:
                pass

        if not idp_healthy:
            if wso2_idp_proc.poll() is not None:
                print("[live_server_env] ERROR: Second WSO2 IS exited prematurely!")
                break
            try:
                resp = requests.get(
                    "https://localhost:9444/api/server/v1/tenants",
                    auth=("admin", "admin"),
                    verify=False,
                    timeout=2
                )
                if resp.status_code == 200:
                    idp_healthy = True
                    print("[live_server_env] Second WSO2 IS (IdP) is healthy.")
            except Exception:
                pass

        if primary_healthy and idp_healthy:
            break
        time.sleep(2)

    if not primary_healthy or not idp_healthy:
        print(f"[live_server_env] ERROR: Startup health check failed. primary_healthy={primary_healthy}, idp_healthy={idp_healthy}")
        _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, [], db_file)
        pytest.fail("WSO2 IS instances failed to start within timeout!")
        
    print("[live_server_env] Both WSO2 IS instances are healthy. Bootstrapping primary...")
    
    # 6. Run bootstrap setup_is.py
    try:
        setup_res = subprocess.run(
            [sys.executable, "setup_is.py"],
            capture_output=True,
            text=True,
            check=True
        )
        stdout = setup_res.stdout
        print(f"[live_server_env] setup_is.py stdout:\n{stdout}")
    except subprocess.CalledProcessError as e:
        print(f"[live_server_env] ERROR: setup_is.py failed with code {e.returncode}!")
        print(f"Stdout:\n{e.stdout}")
        print(f"Stderr:\n{e.stderr}")
        _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, [], db_file)
        pytest.fail("setup_is.py bootstrap failed!")
        
    client_id = None
    client_secret = None
    app_id = None
    for line in stdout.splitlines():
        if "CLIENT_ID=" in line:
            client_id = line.split("CLIENT_ID=")[1].strip()
        elif "CLIENT_SECRET=" in line:
            client_secret = line.split("CLIENT_SECRET=")[1].strip()
        elif "APP_ID=" in line:
            app_id = line.split("APP_ID=")[1].strip()
            
    print("[live_server_env] Primary bootstrap successful.")
    print(f"  CLIENT_ID={client_id}")
    print(f"  CLIENT_SECRET={client_secret}")
    print(f"  APP_ID={app_id}")
    
    if not client_id or not client_secret or not app_id:
        _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, [], db_file)
        pytest.fail("Dynamic credentials parsing failed!")

    print("[live_server_env] Bootstrapping second instance with setup_idp_server.py...")
    try:
        res = subprocess.run(
            [sys.executable, "setup_idp_server.py"],
            capture_output=True,
            text=True,
            check=True
        )
        print("STDOUT from setup_idp_server.py:\n", res.stdout)
        print("STDERR from setup_idp_server.py:\n", res.stderr)
    except subprocess.CalledProcessError as e:
        print(f"[live_server_env] ERROR: setup_idp_server.py failed with code {e.returncode}!")
        print(f"Stdout:\n{e.stdout}")
        print(f"Stderr:\n{e.stderr}")
        _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, [], db_file)
        pytest.fail("setup_idp_server.py bootstrap failed!")
        
    # 7. Start Microservices
    print("[live_server_env] Starting microservices...")
    service_procs = []
    
    live_env = {
        **os.environ,
        "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret,
        "APP_ID": app_id,
        "IS_BASE_URL": "https://localhost:9443",
        "IS_ORG_HANDLE": "teamspace",
        "DATABASE_URL": f"sqlite:///{db_file}",
        "FLASK_SECRET_KEY": "live-test-secret-key-xyz",
        "AGENT_INTERNAL_SECRET": "live-test-secret-key-xyz",
        "AGENT_REDIRECT_URI": "http://localhost:8000/callback",
        "BUSINESS_API_URL": "http://localhost:9091",
        "AGENT_SERVICE_URL": "http://localhost:8000",
        "MOCK_LLM": "true",
        "IS_ADMIN_USERNAME": "teamspaceadmin@teamspace",
        "IS_ADMIN_PASSWORD": "Admin123",
    }
    
    python_bin = sys.executable
    
    # Business API
    api_log = open(os.path.join(temp_dir, "api_stdout.log"), "w")
    api_proc = subprocess.Popen(
        [python_bin, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "9091", "--ws", "none"],
        stdout=api_log,
        stderr=api_log,
        env=live_env,
        preexec_fn=os.setsid
    )
    service_procs.append((api_proc, api_log))
    
    # AI Agent
    agent_log = open(os.path.join(temp_dir, "agent_stdout.log"), "w")
    agent_proc = subprocess.Popen(
        [python_bin, "-m", "uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws", "none"],
        stdout=agent_log,
        stderr=agent_log,
        env=live_env,
        preexec_fn=os.setsid
    )
    service_procs.append((agent_proc, agent_log))
    
    # Flask Web App
    webapp_log = open(os.path.join(temp_dir, "webapp_stdout.log"), "w")
    webapp_proc = subprocess.Popen(
        [python_bin, "-m", "flask", "--app", "webapp.app:create_app", "run", "--host", "0.0.0.0", "--port", "5001"],
        stdout=webapp_log,
        stderr=webapp_log,
        env=live_env,
        preexec_fn=os.setsid
    )
    service_procs.append((webapp_proc, webapp_log))
    
    print("[live_server_env] Waiting for microservices to bind...")
    _wait_for_service("http://localhost:9091/health", "Business API", timeout=30)
    _wait_for_service("http://localhost:8000/health", "AI Agent", timeout=30)
    _wait_for_service("http://localhost:5001/health", "Flask Web App", timeout=30)
    
    yield {
        "client_id": client_id,
        "client_secret": client_secret,
        "app_id": app_id,
        "db_file": db_file,
    }
    
    print("[live_server_env] Initiating teardown...")
    _teardown_live_env(wso2_proc, wso2_log, wso2_idp_proc, wso2_idp_log, temp_dir, service_procs, db_file)
