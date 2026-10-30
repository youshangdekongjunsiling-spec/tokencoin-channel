"""Install a pinned, private CLIProxyAPI instance. Python 3.10+, standard library.
Login is interactive. Never imports or inspects existing Codex/browser credentials.
"""
from __future__ import annotations
import argparse, hashlib, io, json, os, platform, re, secrets, socket, subprocess, sys
import tarfile, time, urllib.error, urllib.request, zipfile
from pathlib import Path
from channel import private_write, NoRedirect

VERSION = "8.0.47"
PORT = 18317
ASSETS = {
    ("Windows", "amd64"): ("windows_amd64.zip", "8e23cbb2f9f695411b7df46320855820b352fb886c3c3bdf45ab4634f2f14632"),
    ("Windows", "arm64"): ("windows_aarch64.zip", "9a947eec7234e5a2e2ce57a9fe55c82328fc823ffd2f41cca85f9da41d11eae0"),
    ("Darwin", "arm64"): ("darwin_aarch64.tar.gz", "a5b260ec0f843d2291488acd76e5595232da8f3b8ec2d334154f1570f5f3ca08"),
    ("Darwin", "amd64"): ("darwin_amd64.tar.gz", "2e72cb7b28905b9135563e4fc01af00009ce72a444729eb95bdb634543ce7d41"),
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

def default_root():
    if os.name == "nt":
        return Path.home() / ".tokencoin" / ("cliproxy-" + VERSION)
    return Path.home() / "Library" / "Application Support" / "Tokencoin" / ("cliproxy-" + VERSION)

def private_directory(path):
    if path.is_symlink(): raise ValueError("Private directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        who = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        sid = re.search(r"S-1-5-[0-9-]+", who)
        if not sid: raise ValueError("Cannot identify current user")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid.group() + ":(OI)(CI)(F)"], check=True, capture_output=True)
    else: path.chmod(0o700)

def make_config(root):
    return {
        "config-version": 8,
        "server": {"host": "127.0.0.1", "port": PORT, "max-request-body-bytes": 1048576,
                   "commercial-mode": True, "discovery": {"enabled": False}},
        "management": {"allow-remote": False, "secret-key": "", "disable-control-panel": True,
                       "disable-auto-update-panel": True},
        "access": {"api-keys": [secrets.token_urlsafe(32)]},
        "oauth": {"auth-dir": str(root / "auth")},
        "routing": {"strategy": "fill-first", "session-affinity": False,
                    "retry": {"request-retry": 0, "max-retry-credentials": 1, "max-retry-interval": 0}},
        "requests": {"streaming": {"bootstrap-retries": 0}},
        "observability": {"logs": {"debug": False, "logging-to-file": False, "request-log": False,
                                   "logs-max-total-size-mb": 1, "error-logs-max-files": 1},
                          "usage": {"usage-statistics-enabled": False}, "pprof": {"enable": False}},
        "plugins": {"enabled": False},
    }

def install(root, archive=None):
    if (root / "installation.json").exists():
        verify(root); print("Already installed; existing private configuration retained."); return
    if root.exists() and any(root.iterdir()):
        raise ValueError("Installation directory is not empty; refusing to overwrite")
    machine = platform.machine().lower()
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    asset, expected = ASSETS[(platform.system(), machine)]
    name = "CLIProxyAPI_" + VERSION + "_" + asset
    url = "https://github.com/router-for-me/CLIProxyAPI/releases/download/v" + VERSION + "/" + name
    if archive: data = Path(archive).read_bytes()
    else:
        print("Downloading official release " + name + " ...", flush=True)
        with urllib.request.urlopen(url, timeout=90) as r: data = r.read(64 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != expected: raise ValueError("Release checksum mismatch")
    exe_name = "cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            binary, license_text = z.read(exe_name), z.read("LICENSE")
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as z:
            member = z.getmember(exe_name)
            if not member.isfile(): raise ValueError("Unexpected executable member")
            binary = z.extractfile(member).read()
            license_text = z.extractfile("LICENSE").read()
    private_directory(root)
    private_directory(root / "auth")
    exe = root / exe_name
    with exe.open("xb") as f: f.write(binary)
    if os.name != "nt": exe.chmod(0o700)
    (root / "LICENSE").write_bytes(license_text)
    private_write(root / "config.yaml", make_config(root))  # JSON is valid YAML.
    private_write(root / "installation.json", {"version": VERSION, "source": url,
        "archive_sha256": expected, "binary_sha256": hashlib.sha256(binary).hexdigest(), "executable": exe_name})
    print("Installed " + VERSION + " at " + str(root))
    print("Private local endpoint: http://127.0.0.1:" + str(PORT) + "/v1")

def verify(root):
    metadata = json.loads((root / "installation.json").read_text("utf-8"))
    exe_name = "cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api"
    exe = root / exe_name
    if exe.is_symlink() or metadata["version"] != VERSION or metadata["executable"] != exe_name:
        raise ValueError("Unexpected installation")
    if hashlib.sha256(exe.read_bytes()).hexdigest() != metadata["binary_sha256"]:
        raise ValueError("Installed executable checksum changed")
    cfg = json.loads((root / "config.yaml").read_text("utf-8"))
    if cfg["server"]["host"] != "127.0.0.1" or cfg["server"]["port"] != PORT:
        raise ValueError("Adapter must remain bound to the configured loopback endpoint")
    if Path(cfg["oauth"]["auth-dir"]).resolve() != (root / "auth").resolve(): raise ValueError("Unexpected auth directory")
    required = {
        ("server", "commercial-mode"): True, ("server", "discovery", "enabled"): False,
        ("management", "allow-remote"): False, ("management", "secret-key"): "",
        ("management", "disable-control-panel"): True, ("management", "disable-auto-update-panel"): True,
        ("routing", "retry", "request-retry"): 0, ("routing", "retry", "max-retry-credentials"): 1,
        ("routing", "retry", "max-retry-interval"): 0, ("requests", "streaming", "bootstrap-retries"): 0,
        ("observability", "logs", "debug"): False, ("observability", "logs", "logging-to-file"): False,
        ("observability", "logs", "request-log"): False, ("observability", "usage", "usage-statistics-enabled"): False,
        ("observability", "pprof", "enable"): False, ("plugins", "enabled"): False,
    }
    for keys, expected in required.items():
        actual = cfg
        for key in keys: actual = actual.get(key, {}) if isinstance(actual, dict) else {}
        if actual != expected or type(actual) is not type(expected):
            raise ValueError("Adapter isolation setting changed: " + ".".join(keys))
    keys = cfg.get("access", {}).get("api-keys")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str) or len(keys[0]) < 32:
        raise ValueError("Exactly one nonempty private adapter entry key is required")
    return exe, cfg

def clean_environment(root):
    names = {"PATH", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME",
             "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    # Do not inherit cloud stores, GITHUB_TOKEN, management passwords or provider keys.
    return env

def request(root, path, authenticated=False):
    _, cfg = verify(root)
    headers = {"Authorization": "Bearer " + cfg["access"]["api-keys"][0]} if authenticated else {}
    req = urllib.request.Request("http://127.0.0.1:" + str(PORT) + path, headers=headers)
    try:
        with OPENER.open(req, timeout=5) as r: return r.status, r.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        e.close(); return e.code, b""

def status(root):
    verify(root)
    result = {"version": VERSION, "endpoint": "http://127.0.0.1:" + str(PORT) + "/v1",
              "auth_file_count": sum(1 for p in (root / "auth").glob("*.json") if p.is_file()),
              "login_contents_read": False, "inference_requested_by_status": False}
    try:
        result["unauthenticated_models_http"] = request(root, "/v1/models")[0]
        code, body = request(root, "/v1/models", True)
        result["authenticated_models_http"] = code
        result["management_http"] = request(root, "/v0/management/config")[0]
        if code == 200:
            result["models"] = [x["id"] for x in json.loads(body).get("data", []) if isinstance(x.get("id"), str)]
    except (OSError, ValueError, urllib.error.URLError): result["service"] = "not_ready"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

def run(root, login=False):
    exe, _ = verify(root)
    if login and any((root / "auth").glob("*.json")):
        raise ValueError("This isolated instance already has an auth file; check status before adding another account")
    if not login:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0: raise ValueError("Adapter port already in use; check status")
    args = [str(exe), "-config", str(root / "config.yaml"), "-local-model"]
    if login:
        args.append("-codex-login")
        print("Complete login in the official browser page opened by the adapter.", flush=True)
        print("Keep this window open. Do not paste credentials or callback URLs into chat.", flush=True)
    else: print("Adapter listening locally. Ctrl+C stops this process.", flush=True)
    # Login stdout belongs to the user's interactive terminal; never capture it in an agent log.
    p = subprocess.Popen(args, cwd=root, env=clean_environment(root),
                         stdout=None if login else subprocess.DEVNULL,
                         stderr=None if login else subprocess.DEVNULL)
    try: code = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        try: p.wait(timeout=5)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        return
    if code: raise ValueError("Adapter exited with code " + str(code))
    if login:
        count = sum(1 for x in (root / "auth").glob("*.json") if x.is_file())
        print("Authorization files present: " + str(count) + ". Real inference remains untested.")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "login", "serve", "status"])
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--archive", type=Path, help="Previously downloaded official archive; checksum is always verified")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.action == "install": install(root, args.archive)
    elif args.action == "login":
        if not (root / "installation.json").exists(): install(root, args.archive)
        run(root, login=True)
    elif args.action == "serve": run(root)
    else: status(root)

if __name__ == "__main__":
    try: main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, urllib.error.URLError) as e:
        # Exception text from upstream libraries may contain URLs; keep output bounded and secret-free.
        print("Setup failed (" + type(e).__name__ + "). Check install path, port, network and release checksum.", file=sys.stderr)
        sys.exit(1)
