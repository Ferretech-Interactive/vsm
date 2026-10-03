"""Deploying to a Linux server over SSH (vsm/desktop/remote). Nothing here leaves this machine:
the "server" is an in-process SSH server on a loopback port."""
import json
import logging
import socket
import sys
import threading

import pytest

paramiko = pytest.importorskip("paramiko")

from vsm.desktop import servers as servers_module  # noqa: E402
from vsm.desktop.remote import deploy, keys, preflight  # noqa: E402
from vsm.desktop.remote.ssh import Root, Session, SshError, Target, fetch_host_key, fingerprint, key_line, target  # noqa: E402
from vsm.desktop.remote.tunnel import Tunnel, free_port  # noqa: E402
from vsm.desktop.servers import ServerList, tunnel_url  # noqa: E402

# ---------- input and host keys ----------


@pytest.mark.parametrize("host, port, user", [
    ("192.168.1.50", 22, "ubuntu"), ("vein.example.com", "2222", "deploy_user"), ("fe80::1", None, "root"),
])
def test_target_accepts_ordinary_values(host, port, user):
    t = target(host, port, user)
    assert (t.host, t.user) == (host, user) and 1 <= t.port <= 65535


@pytest.mark.parametrize("host, port, user", [
    ("", 22, "ubuntu"), ("host; rm -rf /", 22, "ubuntu"), ("host name", 22, "ubuntu"), ("-oProxyCommand=x", 22, "ubuntu"),
    ("host", 0, "ubuntu"), ("host", 70000, "ubuntu"), ("host", "abc", "ubuntu"),
    ("host", 22, ""), ("host", 22, "a b"), ("host", 22, "$(id)"), ("host", 22, "-x"),
])
def test_target_refuses_anything_else(host, port, user):
    with pytest.raises(SshError):
        target(host, port, user)


def test_key_lines_round_trip_and_garbage_is_refused():
    _, public = keys.generate()
    assert key_line(keys.load(keys.generate()[0])).startswith("ssh-ed25519 ")
    assert fingerprint(public).startswith("SHA256:")
    for bad in ("", "ssh-ed25519", "ssh-ed25519 not base64!", "ssh-dss AAAA", public + " comment", 'restrict ' + public):
        with pytest.raises(SshError):
            fingerprint(bad)


# ---------- what the server can take ----------

def facts(**changes):
    text = "arch=x86_64\nos_id=ubuntu\nos_version=24.04\nos_name=Ubuntu 24.04.5 LTS\npython=3.12\nsystemd=1\ndocker=0\ncompose=0\npkg=apt-get\nfree_gb=80\nport_busy=0\n"
    parsed = preflight.parse(text)
    parsed.update(changes)
    return parsed


def test_parse_ignores_what_it_does_not_expect():
    parsed = preflight.parse("arch=x86_64\npython=3.12\nexisting=something-else\nfree_gb=lots\n$(reboot)=1\nos_name=Nice \x1b[31mOS\n")
    assert parsed["arch"] == "x86_64" and parsed["python"] == (3, 12)
    assert parsed["existing"] is None and parsed["free_gb"] is None and parsed["os_name"] == "Nice [31mOS"


def test_ubuntu_2404_gets_a_native_install():
    result = preflight.assess(facts())
    assert result["recommended"] == "native" and result["native"]["ok"] and result["docker"]["ok"] and not result["blocking"]


def test_old_python_means_docker():
    for python, os_id in (((3, 10), "ubuntu"), ((3, 9), "rocky")):
        result = preflight.assess(facts(python=python, os_id=os_id, pkg="dnf" if os_id == "rocky" else "apt-get", docker=os_id == "rocky", compose=os_id == "rocky"))
        assert result["recommended"] == "docker" and not result["native"]["ok"] and "Python" in result["native"]["reason"]


def test_opensuse_without_docker_cannot_be_deployed_to():
    result = preflight.assess(facts(os_id="opensuse-leap", pkg="zypper", python=(3, 6)))
    assert result["recommended"] is None and result["blocking"] and not result["native"]["ok"] and not result["docker"]["ok"]


def test_wrong_processor_or_no_forwarding_blocks_everything():
    assert preflight.assess(facts(arch="aarch64"))["recommended"] is None
    blocked = preflight.assess(facts(), forwarding=False)
    assert blocked["recommended"] is None and "AllowTcpForwarding" in blocked["blocking"][0]
    assert preflight.forwarding_allowed("allowtcpforwarding yes") and preflight.forwarding_allowed("") and preflight.forwarding_allowed("AllowTcpForwarding local")
    assert not preflight.forwarding_allowed("allowtcpforwarding no")


def test_an_existing_install_is_updated_the_same_way():
    result = preflight.assess(facts(existing="docker", docker=True, compose=True))
    assert result["recommended"] == "docker" and not result["native"]["ok"] and result["existing"] == "docker"
    assert any("through SSH only" in note for note in result["notes"])


# ---------- the saved list ----------

def test_deployed_servers_are_saved_without_secrets(tmp_path):
    _, public = keys.generate()
    path = tmp_path / "servers.json"
    servers = ServerList(path, None)
    entry = servers.save_ssh("Linux box", "192.168.1.50", 22, public, 8810, 40123)
    assert tunnel_url(entry) == f"http://vsm-{entry['id']}.localhost:40123"
    assert servers.get(entry["id"])["url"] == tunnel_url(entry)
    assert servers.known_hosts() == {"192.168.1.50:22": public}
    # Deploying to the same machine again keeps its identity (and so the window's origin).
    _, rekeyed = keys.generate()
    again = servers.save_ssh("Renamed", "192.168.1.50", 22, rekeyed, 8810, 50999)
    assert (again["id"], again["local_port"], again["host_key"], again["name"]) == (entry["id"], 40123, rekeyed, "Renamed")
    servers.set_local_port(entry["id"], 41000)
    reloaded = ServerList(path, None)
    assert reloaded.find_ssh("192.168.1.50", 22)["local_port"] == 41000 and "url" not in json.loads(path.read_text())["servers"][0]
    reloaded.remove(entry["id"])
    assert reloaded.entries() == []


def test_damaged_ssh_entries_are_dropped(tmp_path):
    _, public = keys.generate()
    good = {"id": "abcdef", "name": "ok", "kind": "ssh", "host": "h.example", "ssh_port": 22, "host_key": public, "remote_port": 8810, "local_port": 40000}
    bad = [dict(good, id="../x"), dict(good, host="a b"), dict(good, host_key="ssh-ed25519 !!"), dict(good, ssh_port="22"), dict(good, local_port=0), dict(good, remote_port=True)]
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": [good, *bad]}))
    assert [s["id"] for s in ServerList(path, None).entries()] == ["abcdef"]


def test_a_tunnelled_page_is_never_this_pcs_manager():
    pytest.importorskip("webview")
    from vsm.desktop.shell import is_local_origin

    local = "http://127.0.0.1:8810"
    for page in ("http://vsm-abcdef.localhost:8810/", "http://vsm-abcdef.localhost:40123/", "http://localhost:8810/"):
        assert is_local_origin(page, local) is False
    # ...and the one-time sign-in after a deployment is matched to exactly that server's origin.
    assert is_local_origin("http://vsm-abcdef.localhost:40123/#/setup", "http://vsm-abcdef.localhost:40123") is True
    assert is_local_origin("http://vsm-other.localhost:40123/", "http://vsm-abcdef.localhost:40123") is False
    assert servers_module.is_unencrypted_public("http://vsm-abcdef.localhost:40123") is False


# ---------- the tunnel key ----------

def test_key_pair_and_reuse():
    private, public = keys.generate()
    assert public.startswith("ssh-ed25519 ") and len(public.split()) == 2
    assert keys.public_line(private) == public
    assert key_line(keys.load(private)) == public


@pytest.mark.skipif(sys.platform != "win32", reason="keys are stored with Windows DPAPI")
def test_stored_key_is_encrypted_and_comes_back(tmp_path):
    private, _ = keys.generate()
    store = keys.KeyStore(tmp_path / "keys")
    store.save("abcdef", private)
    on_disk = (tmp_path / "keys" / "abcdef.key").read_bytes()
    assert private not in on_disk and b"OPENSSH PRIVATE KEY" not in on_disk
    assert store.load_bytes("abcdef") == private
    store.delete("abcdef")
    assert not (tmp_path / "keys" / "abcdef.key").exists()
    with pytest.raises(ValueError):
        store.save("../escape", private)


# ---------- an SSH server in this process ----------

class _Interface(paramiko.ServerInterface):
    def __init__(self, server):
        self.server = server

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        ok = (username, password) == ("deployer", self.server.password)
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = username == "vsm-tunnel" and key_line(key) == self.server.tunnel_public
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self.server.execute, args=(channel, command.decode()), daemon=True).start()
        return True

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if destination != ("127.0.0.1", self.server.forward_port):
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.server.forwards.add(chanid)
        return paramiko.OPEN_SUCCEEDED


class FakeServer:
    """Accepts `deployer` with a password and `vsm-tunnel` with one key; runs canned commands."""

    def __init__(self, forward_port=0, sudo="password"):
        private, _ = keys.generate()
        self.host_private = keys.load(private)
        self.host_key = key_line(self.host_private)
        self.password = "correct horse"
        self.sudo = sudo
        self.tunnel_public = ""
        self.forward_port = forward_port
        self.forwards: set[int] = set()
        self.commands: list[tuple[str, bytes]] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handshake, args=(client,), daemon=True).start()

    def _handshake(self, client):
        # A client may hang up at any point (fetch_host_key does, on purpose): that must not
        # stop the server from taking the next connection.
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_private)
        try:
            transport.start_server(server=_Interface(self))
        except Exception:  # noqa: BLE001
            return
        self.transports.append(transport)
        self._channels(transport)

    def _channels(self, transport):
        while transport.is_active():
            channel = transport.accept(0.5)
            if channel is not None and channel.get_id() in self.forwards:
                threading.Thread(target=self._relay, args=(channel,), daemon=True).start()

    def _relay(self, channel):
        with socket.create_connection(("127.0.0.1", self.forward_port)) as upstream:
            while True:
                data = channel.recv(65536)
                if not data:
                    break
                upstream.sendall(data)
                channel.sendall(upstream.recv(65536))
        channel.close()

    def execute(self, channel, command):
        stdin = b""
        while True:
            data = channel.recv(65536)
            if not data:
                break
            stdin += data
        self.commands.append((command, stdin))
        code, out = 0, ""
        if command == "id -u":
            out = "1000\n"
        elif command == "sudo -n true":
            code = 0 if self.sudo == "free" else 1
        elif command.startswith("sudo -k -S -p '' "):
            code = 0 if stdin == self.password.encode() + b"\n" else 1
            out = "ran as root\n" if code == 0 else "Sorry, try again.\n"
        elif command == "printf-two":
            channel.sendall(b"one\n")
            channel.sendall_stderr(b"two\n")
        elif command == "false":
            code = 1
        channel.sendall(out.encode())
        channel.send_exit_status(code)
        channel.close()

    def close(self):
        self.listener.close()
        for transport in self.transports:
            transport.close()


@pytest.fixture
def server():
    fake = FakeServer()
    yield fake
    fake.close()


def test_host_key_is_shown_first_and_then_required(server):
    to = Target("127.0.0.1", server.port, "deployer")
    assert fetch_host_key(to) == server.host_key
    Session(to, server.host_key, password=server.password).close()
    _, other = keys.generate()
    with pytest.raises(SshError, match="different host key"):
        Session(to, other, password=server.password)
    assert server.commands == []  # nothing was sent to a server we could not identify


def test_wrong_password_is_reported_plainly(server):
    with pytest.raises(SshError, match="refused the sign-in"):
        Session(Target("127.0.0.1", server.port, "deployer"), server.host_key, password="wrong")


def test_run_quotes_arguments_and_merges_output(server):
    session = Session(Target("127.0.0.1", server.port, "deployer"), server.host_key, password=server.password)
    seen = []
    assert session.run(["printf-two"], on_line=seen.append) == (0, "one\ntwo") and seen == ["one", "two"]
    assert session.run(["false"])[0] == 1
    session.run(["echo", "a b", "$(reboot)", "it's"])
    assert server.commands[-1][0] == """echo 'a b' '$(reboot)' 'it'"'"'s'"""  # one argument each, nothing for a shell to expand
    session.close()


def test_sudo_password_travels_on_standard_input_only(server):
    session = Session(Target("127.0.0.1", server.port, "deployer"), server.host_key, password=server.password)
    with pytest.raises(SshError, match="needs a password"):
        Root(session, None)
    with pytest.raises(SshError, match="sudo refused"):
        Root(session, "wrong")
    root = Root(session, server.password)
    assert root.run(["whoami"]) == (0, "ran as root")
    for command, _ in server.commands:
        assert server.password not in command
    assert server.commands[-1] == ("sudo -k -S -p '' whoami", server.password.encode() + b"\n")
    session.close()


def test_passwordless_sudo_needs_nothing(server):
    server.sudo = "free"
    session = Session(Target("127.0.0.1", server.port, "deployer"), server.host_key, password=server.password)
    root = Root(session, None)
    assert root.prefix == ["sudo", "-n"] and root.stdin is None
    session.close()


def test_tunnel_carries_a_connection_to_the_one_allowed_port():
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(4)

    def serve():
        while True:
            try:
                conn, _ = echo.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"echo:" + conn.recv(100))

    threading.Thread(target=serve, daemon=True).start()
    fake = FakeServer(forward_port=echo.getsockname()[1])
    private, fake.tunnel_public = keys.generate()
    try:
        tunnel = Tunnel("127.0.0.1", fake.port, fake.host_key, keys.load(private), echo.getsockname()[1], free_port())
        tunnel.start()
        assert tunnel.up
        with socket.create_connection(("127.0.0.1", tunnel.local_port), timeout=10) as client:
            client.sendall(b"hello")
            assert client.recv(100) == b"echo:hello"
        tunnel.stop()
        # A key the server does not know, or the right key against the wrong host key: no tunnel.
        with pytest.raises(SshError, match="no longer accepts this app's key"):
            Tunnel("127.0.0.1", fake.port, fake.host_key, keys.load(keys.generate()[0]), 1, free_port()).start()
        with pytest.raises(SshError, match="different host key"):
            Tunnel("127.0.0.1", fake.port, keys.generate()[1], keys.load(private), 1, free_port()).start()
    finally:
        fake.close()
        echo.close()


# ---------- the deployment itself, against a recording stand-in ----------

class _Recorder:
    """Stands in for a Session and a Root: records what would be sent to the server."""

    target = Target("192.168.1.50", 22, "deployer")

    def __init__(self):
        self.commands: list[list[str]] = []
        self.uploads: dict[str, tuple[bytes, int]] = {}
        self.fail_install = False

    def run(self, argv, stdin=None, on_line=None, timeout=None):
        self.commands.append(argv)
        if argv[0] == "mktemp":
            return 0, "/tmp/vsm-deploy.Ab12Cd34\n"
        if argv[0] == "bash":
            if on_line:
                on_line("==> Installing")
            return (1, "error: disk full") if self.fail_install else (0, "")
        return 0, ""

    def upload(self, data, remote, mode=0o600):
        self.uploads[remote] = (data, mode)

    def close(self):
        pass


@pytest.fixture
def job(tmp_path, monkeypatch):
    files = {}
    for name in ("install.sh", "vsm.service", "Dockerfile", "docker-compose.yml", "entrypoint.sh", "vein_server_manager-0-py3-none-any.whl"):
        (tmp_path / name).write_bytes(b"line one\r\nline two\r\n")
        files[name] = tmp_path / name
    monkeypatch.setattr(deploy, "bundle", lambda: files)
    monkeypatch.setattr(deploy, "_WHEEL", "vein_server_manager-0-py3-none-any.whl")
    monkeypatch.setattr(deploy.Deployment, "_wait_healthy", staticmethod(lambda tunnel: None))
    monkeypatch.setattr(deploy.Tunnel, "start", lambda self, check=True: None)
    _, host_key = keys.generate()
    d = deploy.Deployment()
    d.target, d.host_key = _Recorder.target, host_key
    d.session = d.root = _Recorder()
    d.assessment = preflight.assess(facts())
    return d


def finish(job, kind="native"):
    saved = {}
    job.install(kind, lambda result, private: saved.update(result=result, private=private))
    for thread in threading.enumerate():
        if thread.name == "deploy":
            thread.join(20)
    return saved


def test_deployment_sends_fixed_commands_and_cleans_up(job, caplog):
    caplog.set_level(logging.DEBUG)
    recorder = job.session
    saved = finish(job)
    assert job.state == "done", job.error
    temp = "/tmp/vsm-deploy.Ab12Cd34"
    assert recorder.commands == [
        ["mktemp", "-d", "/tmp/vsm-deploy.XXXXXXXX"],
        ["bash", f"{temp}/install.sh", "--native", "--wheel", f"{temp}/vein_server_manager-0-py3-none-any.whl", "--bind", "127.0.0.1",
         "--tunnel-key", f"{temp}/tunnel.pub", "--no-auto-install", "--password-file", f"{temp}/initial-password"],
        ["rm", "-rf", "--", temp],
    ]
    assert all(mode == 0o600 for _, mode in recorder.uploads.values())
    assert recorder.uploads[f"{temp}/install.sh"][0] == b"line one\nline two\n"  # no CRLF for bash
    assert recorder.uploads[f"{temp}/vein_server_manager-0-py3-none-any.whl"][0] == b"line one\r\nline two\r\n"  # the package untouched
    password = job.take_password()
    assert password and len(password) >= 20 and job.take_password() is None  # handed out once
    assert recorder.uploads[f"{temp}/initial-password"][0] == password.encode() + b"\n"
    assert recorder.uploads[f"{temp}/tunnel.pub"][0].decode().strip() == keys.public_line(saved["private"])
    assert saved["result"]["host_key"] == job.host_key and saved["result"]["fresh"] is True
    # Neither the generated password nor the private key reaches a log, the window's output or a command.
    everything = caplog.text + "\n".join(job.lines) + json.dumps(recorder.commands)
    assert password not in everything and "OPENSSH PRIVATE KEY" not in everything


def test_updating_an_existing_install_sets_no_password_and_keeps_the_key(job):
    job.assessment = preflight.assess(facts(existing="native"))
    existing, public = keys.generate()
    job.existing_key = lambda host, port: existing
    saved = finish(job)
    assert job.state == "done" and job.take_password() is None and saved["private"] == existing
    assert "--password-file" not in job.session.commands[1]
    assert job.session.uploads["/tmp/vsm-deploy.Ab12Cd34/tunnel.pub"][0].decode().strip() == public
    assert "/tmp/vsm-deploy.Ab12Cd34/initial-password" not in job.session.uploads


def test_a_failed_install_reports_the_reason_and_still_cleans_up(job):
    job.session.fail_install = True
    saved = finish(job)
    assert job.state == "failed" and "disk full" in job.error and saved == {}
    assert job.session.commands[-1] == ["rm", "-rf", "--", "/tmp/vsm-deploy.Ab12Cd34"]


def test_only_a_kind_the_server_can_take_is_installed(job):
    job.assessment = preflight.assess(facts(python=(3, 10)))
    with pytest.raises(SshError):
        job.install("native", lambda *a: None)
    with pytest.raises(SshError):
        job.install("something", lambda *a: None)
    assert job.session.commands == []


def test_a_strange_temporary_folder_name_stops_the_deployment(job):
    job.session.run = lambda argv, **kw: (0, "/tmp/x; rm -rf /\n") if argv[0] == "mktemp" else (0, "")
    finish(job)
    assert job.state == "failed" and "temporary folder" in job.error


def test_key_files_are_checked_before_connecting(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from vsm.desktop.remote.ssh import load_key_file

    key = Ed25519PrivateKey.generate()
    plain, locked, junk = tmp_path / "plain", tmp_path / "locked", tmp_path / "junk"
    plain.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    locked.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.BestAvailableEncryption(b"open sesame")))
    junk.write_text("not a key")
    assert load_key_file(str(plain), None).get_name() == "ssh-ed25519"
    assert load_key_file(str(locked), "open sesame").get_name() == "ssh-ed25519"
    for path, passphrase, message in ((locked, None, "protected by a passphrase"), (locked, "wrong", "passphrase is wrong"),
                                      (junk, None, "not a private key"), (tmp_path / "missing", None, "does not exist")):
        with pytest.raises(SshError, match=message):
            load_key_file(str(path), passphrase)


def test_tunnel_says_why_the_server_will_not_forward():
    fake = FakeServer(forward_port=1)  # the fake refuses any other destination, as sshd does with forwarding off
    private, fake.tunnel_public = keys.generate()
    try:
        with pytest.raises(SshError, match="refused to forward"):
            Tunnel("127.0.0.1", fake.port, fake.host_key, keys.load(private), 2, free_port()).start()
    finally:
        fake.close()
