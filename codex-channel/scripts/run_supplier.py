"""Run a supplier gateway using this user's isolated, authorized Codex adapter.
No secret arguments: the private adapter entry key is loaded inside this process.
"""
from __future__ import annotations
import argparse, ipaddress, json, os, signal, socket, subprocess, sys, time, urllib.error
from pathlib import Path
import channel
import setup_adapter as adapter

def validate_listener(host, allow_private_http):
    address = ipaddress.ip_address(host)
    if address.version != 4: raise ValueError("Use an IPv4 address for this demo listener")
    if address.is_loopback: return
    allowed = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
    if not allow_private_http or not any(address in n for n in allowed):
        raise ValueError("Bind a specific trusted private/VPN IPv4 address and explicitly allow private HTTP")

class AdapterProcess:
    def __init__(self, root): self.root, self.process = root, None
    def __enter__(self):
        exe, cfg = adapter.verify(self.root)
        if sum(1 for p in (self.root / "auth").glob("*.json") if p.is_file()) != 1:
            raise ValueError("Complete login-codex first; this demo uses one supplier account")
        with socket.socket() as sock: running = sock.connect_ex(("127.0.0.1", adapter.PORT)) == 0
        if running: raise ValueError("Adapter port is already in use. Stop your existing adapter before starting this supplier")
        if not running:
            self.process = subprocess.Popen([str(exe), "-config", str(self.root / "config.yaml"), "-local-model"],
                cwd=self.root, env=adapter.clean_environment(self.root), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(30):
                if self.process and self.process.poll() is not None: raise ValueError("Adapter exited before startup")
                try:
                    code, body = adapter.request(self.root, "/v1/models", True)
                    if code != 200: raise ValueError("Port is occupied or adapter authentication failed")
                    self.models = {x.get("id") for x in json.loads(body).get("data", [])}
                    if not self.models:
                        time.sleep(.2)
                        continue  # Listener may become ready before OAuth models finish loading.
                    self.config = cfg
                    return self
                except (OSError, urllib.error.URLError): time.sleep(.2)
            raise ValueError("Adapter model catalog did not become ready; verify the isolated login")
        except BaseException:
            self.close(); raise
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
    def __exit__(self, *_): self.close()

def make_gateway(home, name, model, host, port, cfg):
    home = home.expanduser().resolve()
    expected = {"model": model, "backend": "subscription-adapter",
                "upstream": "http://127.0.0.1:" + str(adapter.PORT) + "/v1",
                "key_env": "TOKENCOIN_SUPPLIER_ADAPTER_KEY"}
    adapter.private_directory(home)
    if (home / "node.json").exists() != (home / "state.sqlite3").exists():
        raise ValueError("Supplier state is incomplete; choose a new directory and retain this one for inspection")
    if (home / "node.json").exists():
        current = channel.config(home)
        if any(current.get(k) != v for k, v in expected.items()) or current.get("name") != name:
            raise ValueError("This supplier directory has a different name/model/backend; use its original settings or a new directory")
    else: channel.initialize(home, name, **expected)
    return channel.Gateway((host, port), home, upstream_key=cfg["access"]["api-keys"][0])

def main():
    # Dashboard-owned Windows suppliers receive CTRL_BREAK_EVENT.  Python's
    # default SIGBREAK action can exit immediately, bypassing the Gateway and
    # AdapterProcess finally blocks, so convert it to the same unwind path as
    # an interactive Ctrl+C.
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        def interrupt_for_cleanup(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, interrupt_for_cleanup)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--allow-private-http", action="store_true")
    parser.add_argument("--stop-file", type=Path,
                        help="Private dashboard-owned stop marker; normal CLI use should omit this")
    args = parser.parse_args()
    validate_listener(args.host, args.allow_private_http)
    if not 1 <= args.port <= 65535: raise ValueError("Invalid port")
    home = args.home.expanduser().resolve()
    stop_file = args.stop_file.expanduser().resolve() if args.stop_file else None
    if stop_file and (stop_file.parent != home or stop_file.exists()):
        raise ValueError("Stop marker must be a new file directly inside the supplier directory")
    with AdapterProcess(adapter.default_root().resolve()) as upstream:
        if args.model not in upstream.models: raise ValueError("Requested model is absent from adapter catalog")
        server = make_gateway(home, args.name, args.model, args.host, args.port, upstream.config)
        print(json.dumps({"ready": True, "supplier": args.name, "model": args.model,
                          "gateway": "http://" + args.host + ":" + str(server.server_port),
                          "adapter": "loopback only; started by this supplier and stopped on exit"}), flush=True)
        print("Keep this window open. Ctrl+C stops the supplier. Create the invitation in a second terminal.", flush=True)
        try:
            if stop_file:
                server.timeout = .25
                while not stop_file.exists():
                    server.handle_request()
            else:
                server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            server.server_close()
            if stop_file: stop_file.unlink(missing_ok=True)

if __name__ == "__main__":
    try: main()
    except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
        # Local validation errors are authored above; upstream bodies and keys are never logged.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print("Supplier stopped: " + message, file=sys.stderr); sys.exit(1)
