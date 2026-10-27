"""Rootless execution backend (no Docker, no sudo).

Unity offers Apptainer and bubblewrap but not Docker, so the services Agensh runs as
Compose containers (MySQL, Gitea, Mattermost) run from their upstream images under
Apptainer, the Python/Node services run natively, and the worker runs the unmodified
agent/entrypoint.sh inside a bubblewrap sandbox with its own PID namespace (the stock
ACP bridge signals every process of the child uid at shutdown, which must never see
host processes). Service state lives under a per-episode live root on node-local disk.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

WORKER_UID = 10000
READY_TIMEOUT = 120


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class WorkerSpec:
    agent_name: str
    org: str
    live: Path
    repo_root: Path
    tools: Path
    victim_prompt: Path
    gitea_url: str
    mattermost_url: str
    board_url: str
    relay_url: str
    route: object
    episode_id: str

    @property
    def tokens(self):
        return self.live / "boot" / "tokens"


def worker_environment(spec):
    """Environment of the entrypoint. No real credential: the relay injects the model key."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
        "AGENT_KIND": "copilot", "AGENT_NAME": spec.agent_name, "GITEA_ORG": spec.org,
        "GITEA_HOST": spec.gitea_url, "GITEA_TOKEN_FILE": "/run/omcgitea/tokens/agent.token",
        "MATTERMOST_HOST": spec.mattermost_url, "MATTERMOST_TOKEN_FILE": "/run/omcgitea/tokens/mm.token",
        "ANTHROPIC_BASE_URL": spec.relay_url, "ANTHROPIC_API_KEY": "local-proxy-no-auth",
        "ANTHROPIC_MODEL": spec.route.model_id, "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_WIRE_API": spec.route.wire_api,
        "COPILOT_AGENT_REASONING_EFFORT": spec.route.reasoning_effort,
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": str(spec.route.max_prompt_tokens),
        # The notice shim polls BOARD_SERVICE_URL for direct messages, so it stays set;
        # only the board tool is removed and no board is subscribed.
        "BOARD_SERVICE_URL": spec.board_url, "BOARD_IDS": "", "EXPERIMENT_DISABLE_BOARD": "1",
        "ACP_CHILD_UID": str(WORKER_UID), "ACP_CHILD_GID": str(WORKER_UID), "AGENT_HOME": "/opt/data",
        "AGENT_NODE": str(spec.tools / "node-v22.14.0-linux-x64" / "bin" / "node"),
        "COPILOT_BIN": str(spec.tools / "bin" / "copilot-wrapped"),
        "PBENCH_WORKERS": "2", "JOBS": "8",
    }


def worker_bwrap_command(spec):
    live, repo, tools = spec.live, spec.repo_root, spec.tools
    mcp_mattermost = tools / "uv-tools" / "mcp-server-mattermost" / "bin" / "mcp-server-mattermost"
    ro = [
        ("/usr", "/usr"), ("/etc", "/etc"), (str(tools), str(tools)),
        (str(repo / "experiments/peer_influence/runtime"), "/opt/pi"),
        (str(live / "pi_config.json"), "/opt/pi_config.json"),
        (str(live / "protected_hashes_before.json"), "/opt/pi_baseline.json"),
        (str(repo / "agent/entrypoint.sh"), "/opt/omcgitea/entrypoint.sh"),
        (str(repo / "agent/acp-bridge.py"), "/opt/omcgitea/acp-bridge.py"),
        (str(repo / "agent/archive.py"), "/opt/omcgitea/archive.py"),
        (str(repo / "agent/config/copilot-deny-tools.json"), "/opt/omcgitea/config/copilot-deny-tools.json"),
        (str(repo / "agent/config/mcp.json.tmpl"), "/opt/omcgitea/config/mcp.json.tmpl"),
        (str(spec.victim_prompt), "/opt/omcgitea/prompts/agents.md"),
        (str(repo / "prompts/pbench-brief.txt"), "/opt/omcgitea/prompts/pbench-brief.md"),
        (str(repo / "board-service/mcp.cjs"), "/opt/mcp-board.cjs"),
        (str(repo / "mini-router/mcp-notice-shim.cjs"), "/opt/mcp-notice-shim.cjs"),
        (str(tools / "bin" / "gitea-mcp"), "/usr/local/bin/gitea-mcp"),
        (str(mcp_mattermost), "/usr/local/bin/mcp-server-mattermost"),
        (str(spec.tokens / f"agent-{spec.agent_name}.token"), "/run/omcgitea/tokens/agent.token"),
        (str(spec.tokens / f"agent-{spec.agent_name}-mm.token"), "/run/omcgitea/tokens/mm.token"),
    ]
    rw = [
        (str(live / "work"), "/work"), (str(live / "agent_data"), "/opt/data"),
        (str(live / "acp"), "/var/run/acp"), (str(live / "events"), "/opt/pi_events"),
    ]
    cmd = ["bwrap", "--unshare-user", "--uid", str(WORKER_UID), "--gid", str(WORKER_UID),
           "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
           "--clearenv"]
    for key, value in worker_environment(spec).items():
        cmd += ["--setenv", key, value]
    cmd += ["--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/usr/local"]
    for src, dest in ro[1:]:
        cmd += ["--ro-bind", src, dest]
    for src, dest in rw:
        cmd += ["--bind", src, dest]
    return cmd + ["/usr/bin/bash", "/opt/omcgitea/entrypoint.sh"]


@dataclass
class ProjectPaths:
    project_root: Path
    repo_root: Path
    images: Path
    tools: Path

    @classmethod
    def from_root(cls, project_root):
        root = Path(project_root)
        return cls(root, root / "repo" / "Agensh", root / "images", root / "tools")

    @property
    def py_exp(self):
        return str(self.tools / "venv-exp" / "bin" / "python")

    @property
    def py_board(self):
        return str(self.tools / "venv-board" / "bin" / "python")

    @property
    def node(self):
        return str(self.tools / "node-v22.14.0-linux-x64" / "bin" / "node")


@dataclass
class RootlessStack:
    paths: ProjectPaths
    live: Path
    org: str
    route: object
    api_key: str
    log: callable = print
    procs: list = field(default_factory=list)

    def __post_init__(self):
        self.live = Path(self.live)
        self.ports = {k: free_port() for k in ("mysql", "gitea", "mm", "secret", "board", "router", "relay")}
        self.pw = {k: secrets.token_hex(12) for k in ("mysql", "gitea", "mm")}
        self.secret_auth = secrets.token_hex(24)
        self.env = {**os.environ, "APPTAINER_CACHEDIR": str(self.paths.project_root / "cache/apptainer"),
                    "APPTAINER_TMPDIR": str(self.paths.project_root / "tmp")}
        for sub in ("logs", "mysql/data", "mysql/run", "mysql/tmp", "gitea/gitea/conf", "mm/data", "mm/logs",
                    "mm/config", "mm/plugins", "mm/clientplugins", "boot", "lifecycle", "work", "agent_data",
                    "acp", "events"):
            (self.live / sub).mkdir(parents=True, exist_ok=True)

    # ---- helpers
    def url(self, name):
        return f"http://127.0.0.1:{self.ports[name]}"

    def _spawn(self, name, cmd, cwd=None, env=None):
        logfile = open(self.live / "logs" / f"{name}.log", "ab")
        proc = subprocess.Popen(cmd, cwd=cwd, env=env or self.env, stdout=logfile, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.procs.append((name, proc))
        self.log(f"started {name} pid={proc.pid}")
        return proc

    def _wait(self, what, check, timeout=READY_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if check():
                    return
            except (requests.RequestException, OSError, subprocess.SubprocessError):
                pass
            for name, proc in self.procs:
                if proc.poll() is not None and name in what:
                    raise RuntimeError(f"{name} exited early (rc={proc.returncode}); see {self.live}/logs/{name}.log")
            time.sleep(0.5)
        raise TimeoutError(f"{what} not ready within {timeout}s")

    def _http_ok(self, url):
        return lambda: requests.get(url, timeout=3).status_code == 200

    def _apptainer(self, image, *args, binds=(), env=(), writable=False):
        cmd = ["apptainer", "exec", "--containall"] + (["--writable-tmpfs"] if writable else [])
        for k, v in env:
            cmd += ["--env", f"{k}={v}"]
        for src, dest in binds:
            cmd += ["--bind", f"{src}:{dest}"]
        return cmd + [str(self.paths.images / image), *args]

    # ---- MySQL
    def _mysql_binds(self):
        return [(self.live / "mysql/data", "/var/lib/mysql"), (self.live / "mysql/run", "/var/run/mysqld"),
                (self.live / "mysql/tmp", "/tmp")]

    def mysql_exec(self, sql, db=None):
        cmd = self._apptainer("mysql_8.4.sif", "mysql", "-uroot", "--socket=/var/run/mysqld/mysqld.sock",
                              "--batch", "--raw", *( [db] if db else []),
                              binds=[(self.live / "mysql/run", "/var/run/mysqld")])
        return subprocess.run(cmd, input=sql, text=True, env=self.env, check=True, capture_output=True).stdout

    def start_mysql(self):
        subprocess.run(self._apptainer("mysql_8.4.sif", "mysqld", "--initialize-insecure", "--datadir=/var/lib/mysql",
                                       "--log-error=/tmp/init.err", binds=self._mysql_binds()),
                       env=self.env, check=True, capture_output=True)
        self._spawn("mysql", self._apptainer(
            "mysql_8.4.sif", "mysqld", "--datadir=/var/lib/mysql", "--socket=/var/run/mysqld/mysqld.sock",
            "--pid-file=/var/run/mysqld/mysqld.pid", f"--port={self.ports['mysql']}", "--bind-address=127.0.0.1",
            "--mysqlx=OFF", "--innodb-use-native-aio=0", "--max-connections=300",
            "--innodb-buffer-pool-size=134217728", "--secure-file-priv=NULL", "--log-error=/tmp/mysqld.err",
            binds=self._mysql_binds()))
        self._wait("mysql", lambda: bool(self.mysql_exec("select 1;")))
        pw, sql_dir = self.pw["mysql"], self.paths.repo_root / "mini-router" / "sql"
        self.mysql_exec(
            "CREATE DATABASE omcgitea; "
            f"CREATE USER 'omcgitea'@'%' IDENTIFIED BY '{pw}'; CREATE USER 'omcgitea'@'localhost' IDENTIFIED BY '{pw}'; "
            "GRANT ALL ON omcgitea.* TO 'omcgitea'@'%'; GRANT ALL ON omcgitea.* TO 'omcgitea'@'localhost';")
        for name, db in (("01-events-schema.sql", "omcgitea"), ("03-board-schema.sql", "omcgitea"), ("02-mattermost-db.sql", None)):
            self.mysql_exec((sql_dir / name).read_text(), db)

    @property
    def mysql_dsn(self):
        return f"mysql://omcgitea:{self.pw['mysql']}@127.0.0.1:{self.ports['mysql']}/omcgitea"

    # ---- Gitea
    def start_gitea(self):
        port, g = self.ports["gitea"], self.live / "gitea"
        (g / "gitea/conf/app.ini").write_text(f"""APP_NAME = pi
RUN_MODE = prod
WORK_PATH = /data/gitea

[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
DOMAIN = localhost
ROOT_URL = http://127.0.0.1:{port}/
DISABLE_SSH = true
START_SSH_SERVER = false
LFS_START_SERVER = false
OFFLINE_MODE = true

[database]
DB_TYPE = sqlite3
PATH = /data/gitea/gitea.db

[security]
INSTALL_LOCK = true
SECRET_KEY = {secrets.token_hex(24)}

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = false
AUTO_WATCH_ON_CHANGES = false
AUTO_WATCH_NEW_REPOS = false

[actions]
ENABLED = false

[repository]
ROOT = /data/gitea/repositories

[log]
MODE = console
LEVEL = Warn
""", encoding="utf-8")
        base = self._apptainer("gitea_1.22.6.sif", "gitea", "--config", "/data/gitea/conf/app.ini",
                               binds=[(g, "/data")], env=[("GITEA_WORK_DIR", "/data/gitea"), ("GITEA_CUSTOM", "/data/gitea")])
        n = len(base) - 3  # insert the sub-command after "gitea --config <ini>"
        def sub(*args):
            return base[:n + 3] + list(args)
        subprocess.run(sub("migrate"), env=self.env, check=True, capture_output=True)
        subprocess.run(sub("admin", "user", "create", "--admin", "--username", "chairman", "--password", self.pw["gitea"],
                           "--email", "chairman@pi.local", "--must-change-password=false"),
                       env=self.env, check=True, capture_output=True)
        self._spawn("gitea", sub("web"))
        self._wait("gitea", self._http_ok(self.url("gitea") + "/api/v1/version"))

    # ---- Mattermost
    def start_mattermost(self):
        port, m = self.ports["mm"], self.live / "mm"
        env = [("MM_SQLSETTINGS_DRIVERNAME", "mysql"),
               ("MM_SQLSETTINGS_DATASOURCE", f"omcgitea:{self.pw['mysql']}@tcp(127.0.0.1:{self.ports['mysql']})/mattermost"
                "?charset=utf8mb4,utf8&readTimeout=30s&writeTimeout=30s"),
               ("MM_SERVICESETTINGS_SITEURL", self.url("mm")), ("MM_SERVICESETTINGS_LISTENADDRESS", f"127.0.0.1:{port}"),
               ("MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS", "true"), ("MM_TEAMSETTINGS_ENABLEOPENSERVER", "true"),
               ("MM_TEAMSETTINGS_MAXUSERSPERTEAM", "50"), ("MM_PLUGINSETTINGS_ENABLE", "false"),
               ("MM_PLUGINSETTINGS_ENABLEUPLOADS", "false"), ("MM_LOGSETTINGS_ENABLEFILE", "false"),
               ("MM_SERVICESETTINGS_ENABLELOCALMODE", "false"), ("MM_SQLSETTINGS_MAXOPENCONNS", "100"),
               ("MM_SQLSETTINGS_MAXIDLECONNS", "20"), ("MM_FILESETTINGS_DIRECTORY", "/mattermost/data/")]
        binds = [(m / "data", "/mattermost/data"), (m / "logs", "/mattermost/logs"), (m / "config", "/mattermost/config"),
                 (m / "plugins", "/mattermost/plugins"), (m / "clientplugins", "/mattermost/client/plugins")]
        self._spawn("mattermost", self._apptainer(
            "mattermost-team-edition_10.6.sif", "sh", "-c", "cd /mattermost && exec /mattermost/bin/mattermost",
            binds=binds, env=env, writable=True))
        self._wait("mattermost", self._http_ok(self.url("mm") + "/api/v4/system/ping"))

    # ---- identity services and bootstrap
    def start_secret_service(self):
        env = {**self.env, "SECRET_SERVICE_INTERNAL_AUTH": self.secret_auth, "SECRET_SERVICE_PORT": str(self.ports["secret"]),
               "SECRET_SERVICE_TOKENS_DIR": str(self.live / "boot/tokens")}
        self._spawn("secret-service", [self.paths.py_board, str(self.paths.repo_root / "secret-service/main.py")], env=env)
        self._wait("secret-service", self._http_ok(self.url("secret") + "/healthz"))

    def start_board(self):
        env = {**self.env, "MYSQL_DSN": self.mysql_dsn, "BOARD_LISTEN_PORT": str(self.ports["board"]),
               "BOARD_LIFECYCLE_DIR": str(self.live / "lifecycle"), "BOARD_MEMBERS_JSON": json.dumps({"pbench-top": []})}
        self._spawn("board-service", [self.paths.py_board, str(self.paths.repo_root / "board-service/main.py")],
                    cwd=self.paths.repo_root / "board-service", env=env)
        self._wait("board-service", self._http_ok(self.url("board") + "/healthz"))

    def bootstrap(self, agents):
        env = {**self.env, "GITEA_HOST": self.url("gitea"), "GITEA_ADMIN_USER": "chairman",
               "GITEA_ADMIN_PASSWORD": self.pw["gitea"], "GITEA_ORG": self.org, "AGENTS": ",".join(agents),
               "REPOS": "main", "BOOTSTRAP_DATA_DIR": str(self.live / "boot"), "MATTERMOST_HOST": self.url("mm"),
               "MATTERMOST_ADMIN_USER": "mmadmin", "MATTERMOST_ADMIN_PASSWORD": self.pw["mm"],
               "MATTERMOST_TEAM_NAME": self.org, "MATTERMOST_TEAM_DISPLAY": self.org}
        res = subprocess.run(["bash", str(self.paths.repo_root / "docker/bootstrap/bootstrap.sh")], env=env,
                             capture_output=True, text=True)
        (self.live / "logs" / "bootstrap.log").write_text(res.stdout + res.stderr)
        if res.returncode != 0 or not (self.live / "boot/tokens/.bootstrap-done").exists():
            raise RuntimeError(f"bootstrap failed (rc={res.returncode}); see {self.live}/logs/bootstrap.log")

    # ---- model relay, router, worker
    def start_relay(self):
        retries = "1" if self.route.deployment == "local_vllm" else "4"
        env = {**self.env, "UPSTREAM": self.route.upstream_url, "LISTEN_HOST": "127.0.0.1",
               "LISTEN_PORT": str(self.ports["relay"]), "UPSTREAM_AUTH_TOKEN": self.api_key,
               "UPSTREAM_AUTH_STYLE": "bearer", "UPSTREAM_RETRY_ATTEMPTS": retries}
        self._spawn("relay", [self.paths.py_exp, str(self.paths.repo_root / "scripts/strip_metadata_proxy.py")], env=env)
        self._wait("relay", self._http_ok(self.url("relay") + "/healthz"))

    def start_router(self, agent):
        env = {**self.env, "AGENT_NAME": agent, "GITEA_HOST": self.url("gitea"), "MYSQL_DSN": self.mysql_dsn,
               "SECRET_SERVICE_URL": self.url("secret"), "SECRET_SERVICE_INTERNAL_AUTH": self.secret_auth,
               "ACP_SOCKET_PATH": str(self.live / "acp/agent.sock"),
               "GITEA_PASSWORD_FILE": str(self.live / f"boot/tokens/agent-{agent}.password"),
               "MINI_ROUTER_PORT": str(self.ports["router"]), "MATTERMOST_HOST": self.url("mm"),
               "MINI_ROUTER_STUCK_MS": "1200000", "BOARD_SERVICE_URL": self.url("board"), "BOARD_IDS": "",
               "PBENCH_RUN_START_MS": str(int(time.time() * 1000))}
        self._spawn("mini-router", [self.paths.node, "dist/main.js"], cwd=self.paths.repo_root / "mini-router", env=env)

    def start_worker(self, spec):
        self._spawn("worker", worker_bwrap_command(spec), env={"PATH": os.environ["PATH"]})
        self._wait("worker", lambda: (self.live / "acp/agent.sock").exists(), timeout=90)

    # ---- driving and evidence
    def mm_login(self, username, password):
        r = requests.post(self.url("mm") + "/api/v4/users/login", json={"login_id": username, "password": password}, timeout=15)
        r.raise_for_status()
        return r.headers["Token"]

    def kickoff(self, victim_username, text):
        token = self.mm_login("mmadmin", self.pw["mm"])
        h = {"Authorization": f"Bearer {token}"}
        me = requests.get(self.url("mm") + "/api/v4/users/me", headers=h, timeout=15).json()["id"]
        victim = requests.get(self.url("mm") + f"/api/v4/users/username/{victim_username}", headers=h, timeout=15).json()["id"]
        dm = requests.post(self.url("mm") + "/api/v4/channels/direct", headers=h, json=[me, victim], timeout=15).json()["id"]
        post = requests.post(self.url("mm") + "/api/v4/posts", headers=h, json={"channel_id": dm, "message": text}, timeout=15)
        post.raise_for_status()
        return post.json()

    def router_events(self):
        out = self.mysql_exec("SELECT id, dedup_key, source, agent, state, attempts, created_at, sent_at, "
                              "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind')) AS kind FROM events ORDER BY id;", "omcgitea")
        lines = out.splitlines()
        if not lines:
            return []
        head = lines[0].split("\t")
        return [dict(zip(head, row.split("\t"))) for row in lines[1:]]

    def shown_events(self):
        out = self.mysql_exec("SELECT event_id, agent FROM shown;", "omcgitea")
        return [dict(zip(("event_id", "agent"), r.split("\t"))) for r in out.splitlines()[1:]]

    def stop_all(self):
        for name, proc in reversed(self.procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
        deadline = time.time() + 20
        for name, proc in reversed(self.procs):
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.procs.clear()
