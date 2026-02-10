#!/usr/bin/env python3
"""
OpenClaw Watchdog V8 - Self-Healing Guardian
External health probe with rolling backup recovery.
"""

import subprocess
import time
import sys
import datetime
import os
import json
import shutil
import glob
import fcntl
import errno
import socket
import base64

# Configuration
# Configuration - Use expanduser for cross-system compatibility
HOME = os.path.expanduser("~")

def _resolve_openclaw_bin():
    # 1) Explicit override
    override = os.environ.get("OPENCLAW_BIN", "").strip()
    if override and os.path.exists(override):
        return override

    # 2) PATH lookup (works in interactive shells, sometimes not in launchd)
    which = shutil.which("openclaw")
    if which:
        return which

    # 3) NVM installs (common on macOS dev machines)
    candidates = glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin", "openclaw"))
    if candidates:
        # Pick the newest by mtime (good enough; avoids parsing semver).
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return candidates[0]

    # 4) Common global install locations
    for p in ["/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw"]:
        if os.path.exists(p):
            return p

    return None


OPENCLAW_BIN = _resolve_openclaw_bin()
GUARDIAN_DIR = os.path.join(HOME, ".openclaw", "guardian")
LOG_FILE = os.path.join(GUARDIAN_DIR, "watchdog.log")
STATE_FILE = os.path.join(GUARDIAN_DIR, "watchdog.state")
AUDIT_FILE = os.path.join(GUARDIAN_DIR, "watchdog-audit.jsonl")
PID_FILE = os.path.join(GUARDIAN_DIR, "watchdog.pid")
CONFIG_FILE = os.path.join(HOME, ".openclaw", "openclaw.json")
CONFIG_BACKUP_DIR = os.path.join(HOME, ".openclaw", "config-backups")
MAX_CONSECUTIVE_RESTARTS = 3
MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ROLLING_BACKUP_COUNT = 3

# Ensure PATH includes node location
_extra_paths = ["/usr/local/bin", "/opt/homebrew/bin"]
if OPENCLAW_BIN:
    _extra_paths.insert(0, os.path.dirname(OPENCLAW_BIN))
ENV_SETUP = "export PATH=$PATH:" + ":".join(_extra_paths) + "; "


def write_audit_event(event_type, status, details=None):
    """Write structured audit event for system-watchdog to consume."""
    # Use UTC with timezone info for reliable correlation.
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    event = {
        "timestamp": now_utc,
        "type": event_type,
        "status": status,
        "details": details or {}
    }
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"⚠️ Failed to write audit event: {e}")


def log(message):
    """Write to log file with timestamp, with rotation check."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Check log size before writing
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE_BYTES:
        _rotate_log()
    
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as e:
        # Fallback to stderr if log file fails
        print(f"[{timestamp}] {message} (log write failed: {e})", file=sys.stderr)


def _rotate_log():
    """Rotate log file: watchdog.log -> watchdog.log.1"""
    try:
        backup = f"{LOG_FILE}.1"
        if os.path.exists(backup):
            os.remove(backup)
        shutil.move(LOG_FILE, backup)
        log("📝 Log file rotated")
    except Exception as e:
        # If rotation fails, truncate current log
        try:
            with open(LOG_FILE, "w") as f:
                f.write("")
            log(f"⚠️ Log rotation failed ({e}), truncated instead")
        except:
            pass


def acquire_lock():
    """Acquire PID file lock to prevent concurrent execution."""
    try:
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Write PID
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        
        return fd
    except (IOError, OSError) as e:
        if e.errno == errno.EAGAIN or e.errno == errno.EACCES:
            log("⛔ Another watchdog instance is already running. Exiting.")
            sys.exit(0)
        raise


def release_lock(fd):
    """Release PID file lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.unlink(PID_FILE)
    except:
        pass


def notify(message, level="info"):
    """Send notification with fallback strategies."""
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
    full_msg = f"{emoji} **Watchdog:** {message}"
    
    # Strategy 1: Try openclaw CLI
    try:
        if not OPENCLAW_BIN:
            raise RuntimeError("openclaw CLI not found")
        cmd = f'{OPENCLAW_BIN} message send --target "1467890964843597988" --message "{full_msg}"'
        result = run_command(cmd, timeout=10)
        if result and result.returncode == 0:
            return
    except:
        pass
    
    # Strategy 2: macOS notification center (osascript)
    try:
        title = "OpenClaw Watchdog"
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except:
        pass
    
    # Strategy 3: Just log it
    log(f"Notification (delivered to log only): {message}")


def get_restart_count():
    if not os.path.exists(STATE_FILE):
        return 0
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return data.get("restart_count", 0)
    except:
        return 0


def set_restart_count(count):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "restart_count": count,
                "last_update": datetime.datetime.now().isoformat()
            }, f)
    except Exception as e:
        log(f"⚠️ Failed to write state file: {e}")


def run_command(cmd, timeout=30):
    try:
        full_cmd = ENV_SETUP + cmd
        result = subprocess.run(
            full_cmd, shell=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return result
    except subprocess.TimeoutExpired:
        log(f"⚠️ Command timeout after {timeout}s: {cmd[:50]}...")
        return None
    except Exception as e:
        log(f"⚠️ Exec error: {e}")
        return None


def is_config_valid():
    """Check if openclaw.json is valid JSON."""
    try:
        with open(CONFIG_FILE, "r") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, FileNotFoundError) as e:
        log(f"⚠️ Config validation failed: {e}")
        return False


def get_config_hash():
    """Get MD5 hash of config file for change detection."""
    import hashlib
    try:
        with open(CONFIG_FILE, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except:
        return None


def get_gateway_port(default_port=18789):
    """Read gateway port from openclaw.json, fallback to default."""
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        port = data.get("gateway", {}).get("port")
        return int(port) if port else default_port
    except Exception as e:
        log(f"⚠️ Failed to read gateway port: {e}")
        return default_port


def check_gateway_port(port, timeout=2):
    """Verify gateway port is accepting TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception as e:
        log(f"⚠️ Gateway port check failed ({port}): {e}")
        return False


def read_pid_from_file():
    """Try to read gateway PID from common pid file locations."""
    candidates = [
        os.path.expanduser("~/.openclaw/gateway.pid"),
        os.path.expanduser("~/.openclaw/logs/gateway.pid"),
        os.path.expanduser("~/.openclaw/run/gateway.pid"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    raw = f.read().strip()
                if raw.isdigit():
                    return int(raw)
            except Exception as e:
                log(f"⚠️ Failed to read PID file {path}: {e}")
    return None


def check_process_alive():
    """Check gateway process existence via pid file or pgrep."""
    pid = read_pid_from_file()
    if pid:
        try:
            os.kill(pid, 0)
            return True
        except Exception as e:
            log(f"⚠️ Gateway PID not alive ({pid}): {e}")
            return False

    try:
        result = subprocess.run(
            ["pgrep", "-f", "openclaw[- ]gateway"],
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
        log("⚠️ pgrep found no gateway process")
        return False
    except FileNotFoundError:
        log("⚠️ pgrep not available for process check")
        return False
    except Exception as e:
        log(f"⚠️ pgrep failed: {e}")
        return False


def check_websocket_health(port, timeout=3):
    """Attempt a lightweight WebSocket handshake to /health."""
    key = os.urandom(16)
    ws_key = base64.b64encode(key).decode("ascii")
    request = (
        "GET /health HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(request.encode("ascii"))
            sock.settimeout(timeout)
            response = sock.recv(256).decode("ascii", errors="ignore")
            if " 101 " in response or "101 Switching Protocols" in response:
                return True
            log(f"⚠️ WebSocket health check not upgraded: {response.splitlines()[:1]}")
            return False
    except Exception as e:
        log(f"⚠️ WebSocket health check failed: {e}")
        return False


def verify_gateway_health():
    """Secondary validation to avoid false positives."""
    port = get_gateway_port()
    port_ok = check_gateway_port(port)
    process_ok = check_process_alive()
    ws_ok = check_websocket_health(port) if port_ok else False

    log(
        "Health checks: "
        f"spawn_ok=true port_ok={str(port_ok).lower()} "
        f"process_ok={str(process_ok).lower()} ws_ok={str(ws_ok).lower()}"
    )

    if not port_ok:
        return False, "PORT_CLOSED", "Gateway port not listening"
    if not process_ok:
        return False, "PROCESS_MISSING", "Gateway process missing"
    if not ws_ok:
        log("⚠️ WebSocket /health not responding (soft-fail)")
    return True, None, None


def backup_known_good():
    """Rolling backup: current -> v1 -> v2 -> v3 (drop oldest)."""
    if not is_config_valid():
        log("⚠️ Config invalid, skipping backup")
        return False
    
    # Ensure backup directory exists
    os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)
    
    try:
        # Rolling: v2 -> v3, v1 -> v2, current -> v1
        v3 = os.path.join(CONFIG_BACKUP_DIR, "openclaw.json.v3")
        v2 = os.path.join(CONFIG_BACKUP_DIR, "openclaw.json.v2")
        v1 = os.path.join(CONFIG_BACKUP_DIR, "openclaw.json.v1")
        current = os.path.join(CONFIG_BACKUP_DIR, "openclaw.json.current")
        
        # Shift versions
        if os.path.exists(v2):
            shutil.move(v2, v3)
        if os.path.exists(v1):
            shutil.move(v1, v2)
        if os.path.exists(current):
            shutil.move(current, v1)
        
        # Save current
        shutil.copy2(CONFIG_FILE, current)
        
        # Also update legacy single-file location for compatibility
        legacy = CONFIG_FILE + ".known-good"
        shutil.copy2(CONFIG_FILE, legacy)
        
        config_hash = get_config_hash()
        log(f"💾 Config backed up (hash: {config_hash})")
        return True
    except Exception as e:
        log(f"⚠️ Failed to backup config: {e}")
        return False


def restore_known_good(version="current"):
    """Restore config from backup. version: current, v1, v2, v3."""
    backup_path = os.path.join(CONFIG_BACKUP_DIR, f"openclaw.json.{version}")
    
    # Fallback to legacy location
    if not os.path.exists(backup_path):
        backup_path = CONFIG_FILE + ".known-good"
    
    if not os.path.exists(backup_path):
        log(f"⛔ No {version} backup exists. Cannot restore.")
        return False
    
    try:
        # Backup current (corrupted) config for forensics
        if os.path.exists(CONFIG_FILE):
            corrupted = os.path.join(CONFIG_BACKUP_DIR, "openclaw.json.corrupted")
            shutil.copy2(CONFIG_FILE, corrupted)
        
        shutil.copy2(backup_path, CONFIG_FILE)
        notify(f"Config restored from {version} backup (corruption detected)", level="warning")
        write_audit_event("config_recovery", "success", {
            "restored_from": version,
            "config_hash": get_config_hash()
        })
        return True
    except Exception as e:
        log(f"⛔ Failed to restore config: {e}")
        return False


def classify_failure(stderr, returncode):
    """Classify failure type from error output."""
    stderr_lower = stderr.lower()
    
    if returncode == 127 or "command not found" in stderr_lower:
        return "CLI_NOT_FOUND", "⛔ FATAL: openclaw CLI not found"
    
    if "json" in stderr_lower or "parse" in stderr_lower:
        return "CONFIG_ERROR", "🔴 Config JSON error"
    
    if "timeout" in stderr_lower or returncode == 124:
        return "TIMEOUT", "⏱️ Gateway timeout"
    
    if "connection" in stderr_lower or "refused" in stderr_lower:
        return "CONNECTION", "🔌 Gateway connection failed"
    
    if "auth" in stderr_lower or "token" in stderr_lower or "key" in stderr_lower:
        return "AUTH_ERROR", "🔑 Authentication/Key error"
    
    return "UNKNOWN", f"⚠️ Unknown error (code {returncode})"


def check_health_spawn():
    """Probe Gateway health via sessions spawn."""
    if not OPENCLAW_BIN:
        log("⛔ FATAL: openclaw CLI not found (cannot probe gateway).")
        write_audit_event("watchdog_error", "fatal", {"reason": "CLI_NOT_FOUND"})
        return False, "CLI_NOT_FOUND", "openclaw CLI not found"

    cmd = f'{OPENCLAW_BIN} sessions spawn --task "Say OK" --agentId main --timeoutSeconds 25'
    
    start_t = time.time()
    result = run_command(cmd, timeout=35)
    duration = time.time() - start_t
    
    if result is None:
        log(f"⚠️ Spawn execution error (Python exception)")
        return False, "EXCEPTION", "Execution error"
    
    failure_type, failure_msg = classify_failure(result.stderr, result.returncode)
    
    if failure_type == "CLI_NOT_FOUND":
        log(f"{failure_msg}. Watchdog cannot act without CLI.")
        write_audit_event("watchdog_error", "fatal", {"reason": "CLI_NOT_FOUND"})
        return False, failure_type, failure_msg
    
    if result.returncode == 0:
        verified, fail_type, fail_msg = verify_gateway_health()
        if not verified:
            log("⚠️ Spawn OK but secondary checks failed")
            return False, fail_type, fail_msg
        log(f"🟢 Health OK ({duration:.1f}s)")
        set_restart_count(0)
        backup_known_good()
        return True, None, None
    else:
        log(f"{failure_msg}: {result.stderr.strip()[:150]}...")
        return False, failure_type, failure_msg


def restart_gateway(failure_type=None):
    """Restart Gateway with config recovery if needed."""
    count = get_restart_count()
    
    if count >= MAX_CONSECUTIVE_RESTARTS:
        msg = f"MAX RESTARTS ({count}) EXCEEDED. Manual intervention required."
        log(f"⛔ {msg}")
        notify(msg, level="critical")
        return False
    
    # Check and recover config if corrupted
    if not is_config_valid() or failure_type == "CONFIG_ERROR":
        log("🔴 Config file is corrupt! Attempting recovery...")
        if restore_known_good("current"):
            log("🔧 Config recovered from current backup. Proceeding with restart.")
            set_restart_count(0)  # Reset - different failure mode
        else:
            # Try older versions
            for version in ["v1", "v2", "v3"]:
                if restore_known_good(version):
                    log(f"🔧 Config recovered from {version} backup.")
                    set_restart_count(0)
                    break
            else:
                log("⛔ Config recovery failed from all backups.")
    
    count = get_restart_count()  # Re-check after potential reset
    msg = f"Gateway unresponsive ({failure_type or 'UNKNOWN'}). Restarting ({count + 1}/{MAX_CONSECUTIVE_RESTARTS})..."
    log(f"🔴 {msg}")
    notify(msg, level="critical" if count >= 2 else "warning")
    
    write_audit_event("gateway_restart", "initiated", {
        "reason": failure_type,
        "attempt": count + 1,
        "max_attempts": MAX_CONSECUTIVE_RESTARTS
    })
    
    result = run_command(f'{OPENCLAW_BIN} gateway restart', timeout=60)
    
    if result and result.returncode == 0:
        set_restart_count(count + 1)
        log("🔄 Restart command issued. Waiting for recovery...")
        
        # Verify recovery after restart
        time.sleep(30)
        success, _, _ = check_health_spawn()
        if success:
            notify("Gateway recovery verified ✅", level="info")
            write_audit_event("gateway_restart", "success", {"verified": True})
            return True
        else:
            log("⚠️ Restart issued but health check still failing")
            write_audit_event("gateway_restart", "failed", {"verified": False})
            return False
    else:
        error_msg = result.stderr if result else 'Unknown error'
        log(f"⛔ Restart command failed: {error_msg}")
        write_audit_event("gateway_restart", "failed", {"error": error_msg[:200]})
        return False


def heartbeat_attempt(attempt_num, wait_time):
    """Single heartbeat attempt with detailed logging."""
    log(f"Attempt {attempt_num}: Probing Gateway...")
    
    success, failure_type, failure_msg = check_health_spawn()
    if success:
        return True
    
    log(f"⚠️ Attempt {attempt_num} failed ({failure_type}). Waiting {wait_time}s...")
    time.sleep(wait_time)
    return False


def main():
    log("=" * 50)
    log("Starting Watchdog V8 (Rolling Backup + Self-Healing)")
    log("=" * 50)
    
    # Acquire lock
    lock_fd = acquire_lock()
    
    try:
        # Attempt 1
        if heartbeat_attempt(1, 30):
            return
        
        # Attempt 2
        if heartbeat_attempt(2, 30):
            return
        
        # Attempt 3 (final)
        if heartbeat_attempt(3, 30):
            return
        
        # All attempts failed - trigger recovery
        success, failure_type, _ = check_health_spawn()
        if not success:
            restart_gateway(failure_type)
    
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
