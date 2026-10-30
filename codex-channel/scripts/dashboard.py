"""Local-only TokenCoin control dashboard (Python 3.10+, standard library).

The dashboard is a control plane, not a proxy.  It never returns adapter keys or
peer credentials except when a user explicitly creates an invitation.  Every API
request needs the random bearer printed in the launch URL.
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
import webbrowser
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import channel
import run_supplier
import setup_adapter
import setup_quick_connect
import quick_invite


MAX_BODY = 1024 * 1024
MAX_LOG = 64 * 1024
PEER_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
CHANNEL_RE = re.compile(r"[0-9a-f]{32}\Z")
JOB_RE = re.compile(r"[0-9a-f]{32}\Z")


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def default_state_root() -> Path:
    return setup_adapter.default_root().parent / "dashboard"


def _private_replace(path: Path, value: dict) -> None:
    """Replace a non-secret settings file while retaining private permissions."""
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        channel.private_write(temp, value)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ApiError(400, "invalid_text")
    return value


def _redact(text: str, secrets_to_hide=()) -> str:
    value = text
    for secret in secrets_to_hide:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", value)
    if len(value.encode("utf-8", "replace")) > MAX_LOG:
        value = value.encode("utf-8", "replace")[:MAX_LOG].decode("utf-8", "ignore") + "\n[truncated]"
    return value


class DashboardState:
    def __init__(self, root: Path | None = None, ui_root: Path | None = None, token: str | None = None):
        self.root = (root or default_state_root()).expanduser().resolve()
        setup_adapter.private_directory(self.root)
        self._state_lock_file = None
        self._acquire_state_lock()
        self.consumer_home = self.root / "consumer"
        self.exports = self.root / "exports"
        self.ui_root = (ui_root or Path(__file__).resolve().parent.parent / "ui").resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.mock_server = None
        self.mock_thread = None
        self.supplier_process = None
        self.supplier_stop_file = None
        self.jobs: dict[str, dict] = {}
        self.job_processes: dict[str, subprocess.Popen] = {}
        self.closing = False
        self.quick_lock = threading.RLock()
        self.quick_node = None
        self.quick_peers = {}
        self.settings_path = self.root / "settings.json"
        try:
            self.settings = self._load_settings()
        except BaseException:
            self._release_state_lock()
            raise
        configured_mode = (self.settings.get("supplier") or {}).get("mode", "mock")
        self.supplier_home = self.root / "supplier" / configured_mode

    def _acquire_state_lock(self) -> None:
        """Allow exactly one controller to mutate this personal state root."""
        path = self.root / "controller.lock"
        handle = path.open("a+b", buffering=0)
        try:
            if os.name == "nt":
                if path.stat().st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ApiError(409, "dashboard_already_running")
        self._state_lock_file = handle

    def _release_state_lock(self) -> None:
        handle, self._state_lock_file = self._state_lock_file, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def _load_settings(self) -> dict:
        if not self.settings_path.exists():
            value = {"version": 1, "supplier": None}
            _private_replace(self.settings_path, value)
            return value
        try:
            value = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            raise ValueError("Dashboard settings are unreadable; retain the directory for inspection")
        if value.get("version") != 1 or not isinstance(value.get("supplier"), (dict, type(None))):
            raise ValueError("Unsupported dashboard settings")
        return value

    def _save_settings(self) -> None:
        _private_replace(self.settings_path, self.settings)

    def _new_job(self, kind: str) -> dict:
        if self.closing:
            raise ApiError(409, "dashboard_closing")
        job = {"id": uuid.uuid4().hex, "kind": kind, "status": "queued", "created": time.time(),
               "started": None, "ended": None, "returncode": None, "log": ""}
        with self.lock:
            self.jobs[job["id"]] = job
            if len(self.jobs) > 100:
                oldest = sorted(self.jobs.values(), key=lambda x: x["created"])[:-100]
                for item in oldest:
                    self.jobs.pop(item["id"], None)
        return job

    @staticmethod
    def _process_group_flags() -> int:
        return ((subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                if os.name == "nt" else 0)

    @staticmethod
    def _interrupt_group(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        """Last-resort cleanup, scoped to a process group created by us."""
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)

    def _track_job_process(self, job: dict, process: subprocess.Popen) -> None:
        with self.lock:
            self.job_processes[job["id"]] = process
            closing = self.closing
        if closing:
            self._interrupt_group(process)

    def _untrack_job_process(self, job: dict) -> None:
        with self.lock:
            self.job_processes.pop(job["id"], None)

    def _run_quiet_job(self, job: dict, args: list[str]) -> None:
        job.update(status="running", started=time.time())
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False,
                                       creationflags=self._process_group_flags(),
                                       start_new_session=os.name != "nt")
            self._track_job_process(job, process)
            code = process.wait()
            job.update(status="completed" if code == 0 else "failed", returncode=code,
                       log="Process completed." if code == 0 else "Process exited without exposing its output.")
        except (OSError, subprocess.SubprocessError) as error:
            job.update(status="failed", log="Could not start process (" + type(error).__name__ + ").")
        finally:
            self._untrack_job_process(job)
            job["ended"] = time.time()

    def adapter_setup(self, action: str) -> dict:
        if action not in ("install", "login"):
            raise ApiError(400, "unsupported_adapter_action")
        if any(job["kind"].startswith("adapter_") and job["status"] in ("queued", "running")
               for job in self.jobs.values()):
            raise ApiError(409, "adapter_setup_already_running")
        job = self._new_job("adapter_" + action)
        args = [sys.executable, str(Path(setup_adapter.__file__).resolve()), action,
                "--root", str(setup_adapter.default_root().resolve())]
        threading.Thread(target=self._run_quiet_job, args=(job, args), daemon=True).start()
        return {"job_id": job["id"]}

    def adapter_status(self) -> dict:
        root = setup_adapter.default_root().resolve()
        result = {"installed": False, "auth_file_count": 0, "service": "not_ready",
                  "credentials_read": False}
        try:
            setup_adapter.verify(root)
            result["installed"] = True
            result["auth_file_count"] = sum(1 for p in (root / "auth").glob("*.json") if p.is_file())
            with socket.socket() as sock:
                sock.settimeout(.2)
                if sock.connect_ex(("127.0.0.1", setup_adapter.PORT)) == 0:
                    result["service"] = "listening"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        return result

    @staticmethod
    def _supplier_fields(body: dict) -> dict:
        mode = body.get("mode", "mock")
        if mode not in ("mock", "subscription"):
            raise ApiError(400, "unsupported_supplier_mode")
        name = _safe_text(body.get("name"), 80)
        model = _safe_text(body.get("model"), 120)
        host = body.get("host", "127.0.0.1")
        if not isinstance(host, str):
            raise ApiError(400, "invalid_host")
        allow = body.get("allow_private_http", False)
        if type(allow) is not bool:
            raise ApiError(400, "invalid_allow_private_http")
        try:
            port = int(body.get("port", 18781))
            if type(body.get("port", 18781)) is bool or not 0 <= port <= 65535:
                raise ValueError
            run_supplier.validate_listener(host, allow)
        except (ValueError, TypeError):
            raise ApiError(400, "unsafe_listener")
        if mode == "subscription" and port == 0:
            raise ApiError(400, "subscription_requires_fixed_port")
        connection = body.get("connection", "manual")
        if connection not in ("manual", "quick"):
            raise ApiError(400, "invalid_connection_mode")
        if connection == "quick" and (host != "127.0.0.1" or allow):
            raise ApiError(400, "quick_requires_loopback")
        return {"mode": mode, "name": name, "model": model, "host": host, "port": port,
                "allow_private_http": allow, "connection": connection}

    def start_supplier(self, body: dict) -> dict:
        fields = self._supplier_fields(body)
        if fields["connection"] == "quick":
            self._quick()
        with self.lock:
            if self.mock_server or self.supplier_process and self.supplier_process.poll() is None:
                raise ApiError(409, "supplier_already_running")
            self.supplier_home = self.root / "supplier" / fields["mode"]
            setup_adapter.private_directory(self.supplier_home)
            node = self.supplier_home / "node.json"
            db = self.supplier_home / "state.sqlite3"
            if node.exists() != db.exists():
                raise ApiError(409, "supplier_state_incomplete")
            if fields["mode"] == "mock":
                expected = {"name": fields["name"], "model": fields["model"], "backend": "mock"}
                if node.exists():
                    current = channel.config(self.supplier_home)
                    if any(current.get(k) != v for k, v in expected.items()):
                        raise ApiError(409, "supplier_configuration_changed")
                else:
                    channel.initialize(self.supplier_home, fields["name"], fields["model"])
                server = channel.Gateway((fields["host"], fields["port"]), self.supplier_home)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                self.mock_server, self.mock_thread = server, thread
                thread.start()
                fields["port"] = server.server_port
                state = "ready"
            else:
                self.supplier_stop_file = self.supplier_home / ".dashboard-stop"
                self.supplier_stop_file.unlink(missing_ok=True)
                args = [sys.executable, str(Path(run_supplier.__file__).resolve()),
                        "--home", str(self.supplier_home), "--name", fields["name"],
                        "--model", fields["model"], "--host", fields["host"],
                        "--port", str(fields["port"]), "--stop-file", str(self.supplier_stop_file)]
                if fields["allow_private_http"]:
                    args.append("--allow-private-http")
                self.supplier_process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                    creationflags=((subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                                   if os.name == "nt" else 0), start_new_session=os.name != "nt")
                state = "starting"
            self.settings["supplier"] = fields
            self._save_settings()
        return {"state": state, **fields, "base_url": "http://" + fields["host"] + ":" + str(fields["port"])}

    def stop_supplier(self) -> dict:
        with self.quick_lock:
            if self.quick_node:
                self.quick_node.stop_supplier()
        with self.lock:
            stopped, forced = False, False
            if self.mock_server:
                self.mock_server.shutdown()
                self.mock_server.server_close()
                if self.mock_thread:
                    self.mock_thread.join(timeout=2)
                self.mock_server = self.mock_thread = None
                stopped = True
            process = self.supplier_process
            if process and process.poll() is None:
                # Private file IPC lets run_supplier leave serve loop and unwind
                # AdapterProcess reliably even for hidden Windows processes.
                if self.supplier_stop_file:
                    self.supplier_stop_file.touch(exist_ok=True)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self._interrupt_group(process)
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        self._kill_group(process)
                        if process.poll() is None:
                            raise ApiError(409, "supplier_stop_failed")
                        forced = True
                stopped = True
            self.supplier_process = None
            if self.supplier_stop_file:
                self.supplier_stop_file.unlink(missing_ok=True)
            self.supplier_stop_file = None
        return {"stopped": stopped, "forced": forced}

    def supplier_status(self) -> dict:
        fields = self.settings.get("supplier")
        if self.mock_server:
            lifecycle = "ready"
        elif self.supplier_process and self.supplier_process.poll() is None:
            fields = self.settings.get("supplier") or {}
            try:
                with socket.socket() as sock:
                    sock.settimeout(.2)
                    lifecycle = ("ready" if sock.connect_ex((fields.get("host", "127.0.0.1"),
                                                              fields.get("port", 0))) == 0 else "starting")
            except (OSError, TypeError):
                lifecycle = "starting"
        else:
            lifecycle = "stopped"
        result = {"state": lifecycle, "configuration": fields}
        if (self.supplier_home / "node.json").exists() and (self.supplier_home / "state.sqlite3").exists():
            try:
                result["local"] = channel.local_status(self.supplier_home)
            except (OSError, ValueError, KeyError):
                result["local"] = {"error": "state_unavailable"}
        return result

    def create_invite(self, body: dict) -> dict:
        if not self.mock_server and not (self.supplier_process and self.supplier_process.poll() is None):
            raise ApiError(409, "supplier_not_running")
        base_url = _safe_text(body.get("base_url"), 300)
        label = _safe_text(body.get("label", "friend"), 80)
        requests = body.get("requests", 20)
        minutes = body.get("minutes", 60)
        allow = body.get("allow_private_http", False)
        if type(requests) is not int or type(minutes) is not int or type(allow) is not bool:
            raise ApiError(400, "invalid_invite_limits")
        active = self.settings.get("supplier") or {}
        expected_url = "http://" + str(active.get("host")) + ":" + str(active.get("port"))
        if base_url.rstrip("/") != expected_url:
            raise ApiError(400, "invite_url_must_match_supplier")
        ticket = None
        if active.get("connection") == "quick":
            if self.supplier_status()["state"] != "ready":
                raise ApiError(409, "supplier_not_ready")
            try:
                with self.quick_lock:
                    ticket = self._quick().start_supplier("127.0.0.1", active["port"])
            except ApiError:
                raise
            except Exception:
                raise ApiError(502, "quick_connection_failed") from None
        setup_adapter.private_directory(self.exports)
        out = self.exports / ("invite-" + uuid.uuid4().hex + ".json")
        try:
            metadata = channel.invite(self.supplier_home, base_url, out, label, requests, minutes, allow)
            invitation = json.loads(out.read_text("utf-8"))
        except (OSError, ValueError, KeyError):
            out.unlink(missing_ok=True)
            raise ApiError(400, "invite_creation_failed")
        if ticket:
            invitation["transport"] = {"kind": "iroh", "ticket": ticket}
            _private_replace(out, invitation)
        result = {"invitation": invitation, "metadata": metadata}
        if ticket:
            result["connection_string"] = quick_invite.encode(invitation)
        return result

    def revoke(self, body: dict) -> dict:
        channel_id = body.get("channel_id")
        if not isinstance(channel_id, str) or not CHANNEL_RE.fullmatch(channel_id):
            raise ApiError(400, "invalid_channel_id")
        try:
            return channel.revoke(self.supplier_home, channel_id)
        except (OSError, ValueError, KeyError):
            raise ApiError(404, "channel_not_found")

    def import_peer(self, body: dict) -> dict:
        name = body.get("name")
        invitation = body.get("invitation")
        if isinstance(invitation, str):
            try:
                invitation = quick_invite.decode(invitation)
            except (ValueError, TypeError, KeyError):
                raise ApiError(400, "invalid_quick_invitation") from None
        allow = body.get("allow_private_http", False)
        if not isinstance(name, str) or not PEER_RE.fullmatch(name) or not isinstance(invitation, dict) or type(allow) is not bool:
            raise ApiError(400, "invalid_peer_import")
        setup_adapter.private_directory(self.consumer_home)
        if channel.peer_path(self.consumer_home, name).exists():
            raise ApiError(409, "peer_already_exists")
        temporary = self.root / ("import-" + uuid.uuid4().hex + ".json")
        local_url = None
        if "transport" in invitation:
            try:
                quick_invite.validate(invitation)
                with self.quick_lock:
                    local_url = self._quick().connect_peer(invitation["transport"]["ticket"], invitation["token"])
                invitation = dict(invitation, base_url=local_url, allow_private_http=False)
                allow = False
            except ApiError:
                raise
            except Exception:
                raise ApiError(400, "invalid_quick_invitation") from None
        succeeded = False
        try:
            channel.private_write(temporary, invitation)
            status = channel.join(self.consumer_home, name, temporary, allow)
            if local_url:
                self.quick_peers[name] = local_url
            succeeded = True
            return {"name": name, "status": status}
        except urllib.error.HTTPError as error:
            error.close()
            raise ApiError(400, "invitation_rejected")
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            raise ApiError(400, "invitation_import_failed")
        finally:
            temporary.unlink(missing_ok=True)
            if local_url and not succeeded:
                with self.quick_lock:
                    self.quick_node.disconnect_peer(local_url)

    def _quick(self):
        with self.quick_lock:
            if self.closing:
                raise ApiError(409, "dashboard_closing")
            if self.quick_node is None:
                try:
                    import quick_connect
                    setup_quick_connect.load()
                    key_path = self.root / "connection-identity.json"
                    if not key_path.exists():
                        channel.private_write(key_path, {"version": 1, "secret": secrets.token_hex(32)})
                    key = json.loads(key_path.read_text("utf-8"))
                    if key.get("version") != 1:
                        raise ValueError("Invalid connection identity")
                    secret = bytes.fromhex(key["secret"])
                    if len(secret) != 32:
                        raise ValueError("Invalid connection identity")
                    self.quick_node = quick_connect.QuickNode(secret_key=secret, validator=self._quick_authorized)
                except (ImportError, OSError, ValueError, KeyError):
                    raise ApiError(409, "quick_runtime_unavailable") from None
            return self.quick_node

    def _quick_authorized(self, token):
        if self.closing or self.supplier_status()["state"] != "ready":
            return False
        try:
            with channel.db(self.supplier_home) as db:
                row = channel.auth(db, token)
                return row["remaining"] > 0
        except (channel.ChannelError, OSError, ValueError):
            return False

    def _ready_peer(self, name):
        with self.quick_lock:
            peer = channel.peer_read(self.consumer_home, name)
            if peer.get("transport"):
                if name not in self.quick_peers:
                    try:
                        quick_invite.validate(peer)
                        url = self._quick().connect_peer(peer["transport"]["ticket"], peer["token"])
                    except ApiError:
                        raise
                    except Exception:
                        raise ApiError(502, "quick_connection_failed") from None
                    try:
                        peer["base_url"] = url
                        _private_replace(channel.peer_path(self.consumer_home, name), peer)
                    except BaseException:
                        self.quick_node.disconnect_peer(url)
                        raise
                    self.quick_peers[name] = url
                peer["base_url"] = self.quick_peers[name]
            return peer

    def quick_setup(self):
        if any(j["kind"] == "network_install" and j["status"] in ("queued", "running") for j in self.jobs.values()):
            raise ApiError(409, "network_install_running")
        job = self._new_job("network_install")
        threading.Thread(target=self._run_quiet_job, args=(job, [sys.executable, "-B", str(Path(setup_quick_connect.__file__).resolve())]), daemon=True).start()
        return {"job_id": job["id"]}

    def peers(self) -> list[dict]:
        directory = self.consumer_home / "peers"
        result = []
        if not directory.exists():
            return result
        for path in sorted(directory.glob("*.json")):
            name = path.stem
            if not PEER_RE.fullmatch(name):
                continue
            try:
                peer = channel.peer_read(self.consumer_home, name)
                result.append({"name": name, "supplier": peer.get("supplier"), "model": peer.get("model"),
                               "backend": peer.get("backend"), "base_url": peer.get("base_url"),
                               "expires": peer.get("expires"), "connection": "quick" if peer.get("transport") else "manual"})
            except (OSError, ValueError, KeyError):
                result.append({"name": name, "error": "peer_state_unavailable"})
        return result

    def peer_status(self, name: str) -> dict:
        if not PEER_RE.fullmatch(name):
            raise ApiError(400, "invalid_peer_name")
        try:
            return channel.peer_status(self._ready_peer(name))
        except urllib.error.HTTPError as error:
            error.close()
            raise ApiError(409, "peer_rejected_status")
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            raise ApiError(502, "peer_unreachable")

    def launch_codex(self, body: dict) -> dict:
        name = body.get("peer")
        if not isinstance(name, str) or not PEER_RE.fullmatch(name):
            raise ApiError(400, "invalid_peer_name")
        prompt = _safe_text(body.get("prompt"), 200000)
        sandbox = body.get("sandbox", "read-only")
        effort = body.get("effort", "low")
        if sandbox not in ("read-only", "workspace-write") or effort not in ("low", "medium", "high"):
            raise ApiError(400, "invalid_codex_options")
        workdir = Path(_safe_text(body.get("workdir"), 1000)).expanduser().resolve()
        if not workdir.is_dir():
            raise ApiError(400, "workdir_not_found")
        try:
            peer = self._ready_peer(name)
            channel.peer_status(peer)
        except (OSError, ValueError, KeyError, urllib.error.URLError, urllib.error.HTTPError):
            raise ApiError(409, "peer_not_ready")
        executable = shutil.which("codex")
        if not executable:
            raise ApiError(409, "codex_not_installed")
        isolated = self.consumer_home.resolve() / "codex-sessions" / name
        setup_adapter.private_directory(isolated)
        core = {"PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TERM"}
        env = {k: v for k, v in os.environ.items() if k.upper() in core}
        env["CODEX_HOME"], env["TOKENCOIN_PEER_KEY"] = str(isolated), peer["token"]
        args = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--color", "never", "-s", sandbox, "-C", str(workdir),
                "-c", 'model_provider="tokencoin_peer"', "-c", "model=" + json.dumps(peer["model"]),
                "-c", "model_reasoning_effort=" + json.dumps(effort),
                "-c", 'model_providers.tokencoin_peer.name="TokenCoin friend channel"',
                "-c", "model_providers.tokencoin_peer.base_url=" + json.dumps(peer["base_url"] + "/v1"),
                "-c", 'model_providers.tokencoin_peer.env_key="TOKENCOIN_PEER_KEY"',
                "-c", 'model_providers.tokencoin_peer.wire_api="responses"',
                "-c", "model_providers.tokencoin_peer.requires_openai_auth=false",
                "-c", "model_providers.tokencoin_peer.supports_websockets=false",
                "-c", "model_providers.tokencoin_peer.request_max_retries=0",
                "-c", "model_providers.tokencoin_peer.stream_max_retries=0",
                "-c", 'shell_environment_policy.inherit="core"',
                "-c", "shell_environment_policy.ignore_default_excludes=false",
                "-c", 'shell_environment_policy.exclude=["TOKENCOIN_PEER_KEY"]',
                "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent",
                "--enable", "skip_host_skill_discovery", "--enable", "code_mode_host",
                "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"']
        if os.name == "nt":
            args += ["-c", 'windows.sandbox="mxc"']
        args.append("-")
        job = self._new_job("codex")
        threading.Thread(target=self._run_codex_job,
                         args=(job, args, env, prompt, (peer.get("token"),)), daemon=True).start()
        return {"job_id": job["id"]}

    def _run_codex_job(self, job: dict, args: list[str], env: dict, prompt: str, hidden: tuple[str, ...]) -> None:
        job.update(status="running", started=time.time())
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", env=env, shell=False,
                                       creationflags=self._process_group_flags(), start_new_session=os.name != "nt")
            self._track_job_process(job, process)
            output, _ = process.communicate(prompt, timeout=900)
            job.update(status="completed" if process.returncode == 0 else "failed",
                       returncode=process.returncode, log=_redact(output or "", hidden))
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            job.update(status="failed", returncode=None, log=_redact((output or "") + "\nTime limit reached.", hidden))
        except (OSError, subprocess.SubprocessError) as error:
            job.update(status="failed", log="Could not run Codex (" + type(error).__name__ + ").")
        finally:
            self._untrack_job_process(job)
            job["ended"] = time.time()

    def status(self) -> dict:
        with self.lock:
            jobs = [{k: v for k, v in job.items() if k != "log"} for job in self.jobs.values()]
        return {"adapter": self.adapter_status(), "supplier": self.supplier_status(),
                "peers": self.peers(), "jobs": jobs,
                "network": {"runtime_installed": (setup_quick_connect.default_root() / "runtime.whl").is_file(),
                            "active": self.quick_node is not None}}

    def close(self) -> None:
        with self.lock:
            self.closing = True
        supplier_error = None
        try:
            try:
                self.stop_supplier()
            except ApiError as error:
                supplier_error = error
            with self.lock:
                processes = list(self.job_processes.values())
            for process in processes:
                if process.poll() is None:
                    try:
                        self._interrupt_group(process)
                        process.wait(timeout=8)
                    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                        if process.poll() is None:
                            self._kill_group(process)
        finally:
            try:
                with self.quick_lock:
                    if self.quick_node:
                        self.quick_node.close()
                        self.quick_node = None
                    self.quick_peers.clear()
            finally:
                self._release_state_lock()
        if supplier_error:
            raise supplier_error


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: DashboardState):
        self.state = state
        super().__init__(address, DashboardHandler)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _reply(self, status: int, value: dict | list) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _allowed_authority(self) -> set[str]:
        port = self.server.server_port
        return {"127.0.0.1:" + str(port), "localhost:" + str(port), "[::1]:" + str(port)}

    def _guard_host(self) -> str:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            raise ApiError(403, "foreign_host")
        host = values[0]
        if host not in self._allowed_authority():
            raise ApiError(403, "foreign_host")
        return host

    def _guard_api(self, mutation=False) -> None:
        host = self._guard_host()
        auth_values = self.headers.get_all("Authorization", [])
        if len(auth_values) != 1:
            raise ApiError(401, "bearer_required")
        auth = auth_values[0]
        expected = "Bearer " + self.server.state.token
        if not secrets.compare_digest(auth, expected):
            raise ApiError(401, "bearer_required")
        origin_values = self.headers.get_all("Origin", [])
        if len(origin_values) > 1:
            raise ApiError(403, "foreign_origin")
        origin = origin_values[0] if origin_values else None
        if origin is not None and origin != "http://" + host:
            raise ApiError(403, "foreign_origin")
        if mutation:
            if origin != "http://" + host:
                raise ApiError(403, "same_origin_required")
            media = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                raise ApiError(415, "json_required")

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding") not in (None, "identity"):
            raise ApiError(400, "encoded_body_not_supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ApiError(400, "invalid_length")
        try:
            size = int(lengths[0])
        except ValueError:
            raise ApiError(400, "invalid_length")
        if size < 2 or size > MAX_BODY:
            raise ApiError(413, "body_size_limit")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ApiError(400, "incomplete_body")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ApiError(400, "invalid_json")
        if not isinstance(value, dict):
            raise ApiError(400, "object_required")
        return value

    def _static(self) -> None:
        self._guard_host()
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.query:
            raise ApiError(404, "route_not_found")
        relative = "index.html" if parsed.path in ("/", "/index.html") else urllib.parse.unquote(parsed.path.lstrip("/"))
        root = self.server.state.ui_root
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ApiError(404, "route_not_found")
        if not target.is_file():
            raise ApiError(404, "route_not_found")
        suffix = target.suffix.lower()
        content_type = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(suffix,
                                                                                          "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                         "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if not path.startswith("/api/"):
                return self._static()
            self._guard_api()
            state = self.server.state
            if path == "/api/status":
                value = state.status()
            elif path == "/api/peers":
                value = {"peers": state.peers()}
            elif path.startswith("/api/peers/") and path.endswith("/status"):
                name = urllib.parse.unquote(path[len("/api/peers/"):-len("/status")]).strip("/")
                value = state.peer_status(name)
            elif path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/"):]
                if not JOB_RE.fullmatch(job_id) or job_id not in state.jobs:
                    raise ApiError(404, "job_not_found")
                value = dict(state.jobs[job_id])
            else:
                raise ApiError(404, "route_not_found")
            self._reply(200, value)
        except ApiError as error:
            self._reply(error.status, {"error": {"code": error.code}})
        except Exception:
            self._reply(500, {"error": {"code": "internal_error"}})

    def do_POST(self):
        try:
            self._guard_api(mutation=True)
            path, body, state = urllib.parse.urlsplit(self.path).path, self._body(), self.server.state
            if path == "/api/network/setup":
                value = state.quick_setup()
            elif path == "/api/adapter/setup":
                value = state.adapter_setup(body.get("action"))
            elif path == "/api/supplier/start":
                value = state.start_supplier(body)
            elif path == "/api/supplier/stop":
                value = state.stop_supplier()
            elif path == "/api/supplier/invites":
                value = state.create_invite(body)
            elif path == "/api/supplier/revoke":
                value = state.revoke(body)
            elif path == "/api/peers/import":
                value = state.import_peer(body)
            elif path == "/api/codex/jobs":
                value = state.launch_codex(body)
            else:
                raise ApiError(404, "route_not_found")
            self._reply(200, value)
        except ApiError as error:
            self._reply(error.status, {"error": {"code": error.code}})
        except Exception:
            self._reply(500, {"error": {"code": "internal_error"}})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=18781, help="Use 0 for an ephemeral port")
    parser.add_argument("--state-root", type=Path, default=default_state_root())
    parser.add_argument("--ui-root", type=Path, default=Path(__file__).resolve().parent.parent / "ui")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535")
    server = create_server(args.state_root, args.port, ui_root=args.ui_root, token=None, host=args.host)
    state = server.state
    url = server.base_url + "/#token=" + server.token
    print("TokenCoin dashboard: " + url, flush=True)
    if not args.no_browser:
        threading.Timer(.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.close()
    return 0


def create_server(home=None, port=0, token=None, *, ui_root=None, host="127.0.0.1") -> DashboardServer:
    """Create, but do not start, an authenticated loopback dashboard server.

    The caller owns ``serve_forever()``, ``shutdown()`` and ``server.state.close()``.
    ``server.token`` stays in process memory; ``origin`` is suitable for the
    required Origin header on JSON mutations.
    """
    if host not in ("127.0.0.1", "localhost") or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Dashboard must use a loopback host and port 0..65535")
    state = DashboardState(Path(home) if home is not None else None,
                           Path(ui_root) if ui_root is not None else None, token=token)
    server = DashboardServer((host, port), state)
    server.token = state.token
    server.base_url = "http://127.0.0.1:" + str(server.server_port)
    server.origin = server.base_url
    return server


if __name__ == "__main__":
    raise SystemExit(main())
