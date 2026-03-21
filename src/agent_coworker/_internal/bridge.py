"""XMTP Bridge lifecycle management for coworker-protocol.

Manages the Node.js XMTP bridge process that connects Python agents
to the XMTP decentralized messaging network.

Architecture:
    Python Agent ↔ HTTP (localhost) ↔ XMTP Bridge (Node.js) ↔ XMTP Network
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


def _bridge_source_dir() -> Path:
    """Path to the bundled bridge JS files inside the SDK package."""
    return Path(__file__).parent / "bridge"


DEFAULT_BRIDGE_PORT = 3500


def find_free_port(start: int = DEFAULT_BRIDGE_PORT, attempts: int = 100) -> int:
    """Find a free TCP port starting from `start` (default: 3500)."""
    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + attempts}")


def find_node() -> str | None:
    """Find a working Node.js binary (>= v22, required by @xmtp/node-sdk)."""
    for name in ("node", "nodejs"):
        path = shutil.which(name)
        if path:
            try:
                out = subprocess.check_output([path, "--version"], text=True).strip()
                major = int(out.lstrip("v").split(".")[0])
                if major >= 22:
                    return path
            except (subprocess.CalledProcessError, ValueError):
                continue
    return None


def find_npm() -> str | None:
    """Find npm binary."""
    return shutil.which("npm")


def setup_bridge(data_dir: Path) -> Path:
    """Copy bridge files to data_dir/bridge/ and install npm deps.

    Returns the bridge directory path.
    """
    bridge_dir = data_dir / "bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)

    src = _bridge_source_dir()
    for filename in ("xmtp_bridge.js", "package.json"):
        src_file = src / filename
        dst_file = bridge_dir / filename
        if src_file.exists() and (not dst_file.exists() or
                src_file.stat().st_mtime > dst_file.stat().st_mtime):
            shutil.copy2(str(src_file), str(dst_file))

    # Install npm deps if needed
    if not (bridge_dir / "node_modules").exists():
        npm = find_npm()
        if not npm:
            raise RuntimeError(
                "npm not found. Install Node.js >= 22 with npm: https://nodejs.org/"
            )
        print("  Installing XMTP bridge dependencies...")
        result = subprocess.run(
            [npm, "install", "--production"],
            cwd=str(bridge_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"npm install failed:\n{result.stderr}")
        print("  XMTP bridge dependencies installed.")

    return bridge_dir


def check_running(data_dir: Path) -> dict | None:
    """Check if bridge is already running. Returns status dict or None."""
    port_file = data_dir / "bridge_port"
    if port_file.exists():
        try:
            port = int(port_file.read_text().strip())
            req = Request(f"http://127.0.0.1:{port}/health")
            with urlopen(req, timeout=3) as resp:
                health = json.loads(resp.read())
                if health.get("status") == "connected":
                    return {
                        "status": "running",
                        "address": health.get("address"),
                        "env": health.get("env"),
                        "port": port,
                    }
        except (URLError, OSError, ValueError):
            # Stale PID/port files — clean up
            port_file.unlink(missing_ok=True)
            pid_file = data_dir / "bridge.pid"
            pid_file.unlink(missing_ok=True)
    return None


def start(data_dir: Path, env: str = "dev") -> dict:
    """Start the XMTP bridge for an agent.

    Returns dict with status, address, port, etc.
    """
    data_dir = Path(data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Already running?
    running = check_running(data_dir)
    if running:
        return running

    # Wallet required
    wallet_path = data_dir / "wallet.json"
    if not wallet_path.exists():
        return {"error": f"No wallet.json at {wallet_path}. Run: coworker init"}

    wallet = json.loads(wallet_path.read_text())
    private_key = wallet.get("private_key", "")
    if not private_key:
        return {"error": "wallet.json has no private_key"}

    # Node.js required
    node = find_node()
    if not node:
        return {"error": "Node.js >= 22 not found. Install: https://nodejs.org/"}

    # Check glibc version on Linux (XMTP node-bindings require >= 2.33)
    if sys.platform == "linux":
        try:
            ldd_out = subprocess.check_output(["ldd", "--version"],
                                               stderr=subprocess.STDOUT, text=True)
            ver_str = ldd_out.split("\n")[0].split()[-1]
            major, minor = int(ver_str.split(".")[0]), int(ver_str.split(".")[1])
            if (major, minor) < (2, 33):
                return {
                    "error": f"System glibc ({ver_str}) is too old for XMTP node-bindings "
                             f"(requires >= 2.33).\n"
                             f"  Solutions:\n"
                             f"  1. Use Docker: docker run -d --name coworker-bridge "
                             f"-p 3500:3500 ghcr.io/ziwayz/coworker-bridge\n"
                             f"  2. Upgrade OS to Ubuntu 22.04+, Debian 12+, or CentOS 9+",
                }
        except Exception:
            pass  # Can't detect — proceed and let it fail with the bridge log

    # Setup bridge files + npm deps
    try:
        bridge_dir = setup_bridge(data_dir)
    except RuntimeError as e:
        return {"error": str(e)}

    bridge_script = bridge_dir / "xmtp_bridge.js"
    if not bridge_script.exists():
        return {"error": f"Bridge script not found: {bridge_script}"}

    # Find free port
    port = find_free_port()

    # Launch bridge process
    bridge_env = os.environ.copy()
    bridge_env["XMTP_PRIVATE_KEY"] = private_key
    bridge_env["XMTP_ENV"] = env
    bridge_env["BRIDGE_PORT"] = str(port)
    bridge_env["COWORKER_DATA_DIR"] = str(data_dir)

    log_path = data_dir / "bridge.log"
    log_file = open(str(log_path), "w")

    proc = subprocess.Popen(
        [node, str(bridge_script)],
        cwd=str(bridge_dir),
        env=bridge_env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Save PID + port
    (data_dir / "bridge.pid").write_text(str(proc.pid))
    (data_dir / "bridge_port").write_text(str(port))

    # Wait for ready (up to 30s)
    for attempt in range(30):
        time.sleep(1)
        if proc.poll() is not None:
            log_content = log_path.read_text() if log_path.exists() else ""
            return {"error": f"Bridge exited with code {proc.returncode}", "log": log_content[-500:]}
        try:
            req = Request(f"http://127.0.0.1:{port}/health")
            with urlopen(req, timeout=3) as resp:
                health = json.loads(resp.read())
                if health.get("status") == "connected":
                    return {
                        "status": "started",
                        "address": health.get("address"),
                        "env": health.get("env"),
                        "port": port,
                        "pid": proc.pid,
                    }
        except (URLError, OSError):
            continue

    return {"error": "Bridge did not connect within 30 seconds"}


def stop(data_dir: Path) -> dict:
    """Stop the running XMTP bridge."""
    data_dir = Path(data_dir).expanduser()
    pid_path = data_dir / "bridge.pid"
    port_file = data_dir / "bridge_port"
    stopped = False

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, ValueError):
            pass
        pid_path.unlink(missing_ok=True)

    port_file.unlink(missing_ok=True)
    return {"status": "stopped" if stopped else "not_running"}


def get_port(data_dir: Path) -> int | None:
    """Read the bridge port from data_dir/bridge_port."""
    port_file = Path(data_dir).expanduser() / "bridge_port"
    if port_file.exists():
        try:
            return int(port_file.read_text().strip())
        except (ValueError, IOError):
            pass
    return None
