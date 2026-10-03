#!/usr/bin/env bash
# Vein Server Manager installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/Ferretech-Interactive/vsm/main/install.sh | sudo bash
#
# Checks for everything the manager needs, installs what is missing, then starts
# it. Safe to re-run: it updates the manager and keeps your data and password.
#
# Options (append after `sudo bash -s --`, or set the VSM_* variable):
#   --docker            Run in Docker (default). Installs Docker if missing.
#   --native            Run as a systemd service without Docker.
#   --dir PATH          Install location            [VSM_INSTALL_DIR, /opt/vein-server-manager]
#   --port N            Web UI port                 [VSM_WEB_PORT, 8810]
#   --ref NAME          Branch or tag to install    [VSM_REF, main]
#
# Used by the Windows app when it deploys to this machine over SSH (nothing is downloaded from GitHub):
#   --wheel PATH        Install this package; the files beside it replace the source download
#   --bind ADDR         Address the web UI listens on (127.0.0.1 = reachable through SSH only)
#   --password-file P   First admin password, read once by the manager and then deleted
#   --tunnel-key PATH   Public key allowed to forward to the web UI port, and nothing else
#   --no-auto-install   Do not download the game server at first start (the guided setup does it)
set -euo pipefail

REPO="${VSM_REPO:-Ferretech-Interactive/vsm}"
REF="${VSM_REF:-main}"
MODE="${VSM_MODE:-docker}"
DIR="${VSM_INSTALL_DIR:-/opt/vein-server-manager}"
PORT="${VSM_WEB_PORT:-8810}"
SOURCE_DIR="${VSM_SOURCE_DIR:-}" # install from a local checkout instead of downloading
WHEEL="" BIND="" PASSWORD_FILE="" TUNNEL_KEY="" NO_AUTO_INSTALL=""
TUNNEL_USER="vsm-tunnel"
NODE_MIN="20.19.0"
MIN_FREE_GB=15

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

pkg_install() {
    say "Installing: $*"
    if have apt-get; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
    elif have dnf; then dnf install -y -q "$@"
    elif have yum; then yum install -y -q "$@"
    elif have zypper; then zypper --non-interactive install "$@"
    elif have pacman; then pacman -Sy --noconfirm --needed "$@"
    else die "No supported package manager found. Install manually: $*"
    fi
}

preflight() {
    [ "$(id -u)" = "0" ] || die "Run as root: pipe to 'sudo bash' instead of 'bash'."
    [ "$(uname -s)" = "Linux" ] || die "This installer is for Linux. On Windows use the Setup.exe from the releases page."
    [ "$(uname -m)" = "x86_64" ] || die "The Vein server only runs on x86-64 (this machine is $(uname -m))."
    have curl || pkg_install curl
    have tar || pkg_install tar
    mkdir -p "$DIR"
    local free_gb
    free_gb=$(df -Pk "$DIR" | awk 'NR==2 {print int($4 / 1048576)}')
    [ "$free_gb" -ge "$MIN_FREE_GB" ] || warn "Only ${free_gb} GB free in $DIR; the server needs about ${MIN_FREE_GB} GB."
}

fetch_source() { # $1 = destination directory (replaced)
    [ ! -e "$1/data" ] || die "$1/data exists and would be deleted by an update. Move it outside $1 first."
    rm -rf "$1" && mkdir -p "$1"
    if [ -n "$SOURCE_DIR" ]; then
        say "Copying source from $SOURCE_DIR"
        tar -C "$SOURCE_DIR" --exclude=.git --exclude=.venv --exclude=node_modules --exclude=data -cf - . | tar -C "$1" -xf -
    else
        say "Downloading $REPO@$REF"
        curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz --strip-components=1 -C "$1"
    fi
    [ -f "$1/pyproject.toml" ] || die "Downloaded source looks wrong (no pyproject.toml). Check --ref."
}

stage_bundle() { # $1 = destination directory (replaced): the files uploaded beside the wheel
    [ ! -e "$1/data" ] || die "$1/data exists and would be deleted by an update. Move it outside $1 first."
    local from
    from=$(dirname "$WHEEL")
    rm -rf "$1" && mkdir -p "$1"
    cp "$WHEEL" "$1/"
    for f in Dockerfile docker-compose.yml entrypoint.sh vsm.service; do
        [ -f "$from/$f" ] || die "The upload is incomplete: $f is missing beside the package."
        cp "$from/$f" "$1/"
    done
}

set_env() { # $1 = file, $2 = KEY, $3 = value: one line per key, replacing an earlier one
    touch "$1"
    sed -i "/^$2=/d" "$1"
    printf '%s=%s\n' "$2" "$3" >>"$1"
}

deploy_settings() { # $1 = env file, $2 = name of the bind variable in that file
    [ -z "$BIND" ] || set_env "$1" "$2" "$BIND"
    [ -z "$NO_AUTO_INSTALL" ] || set_env "$1" VSM_AUTO_INSTALL false
}

# An account that can do one thing: forward a connection to the web UI port. The Windows app
# reaches the manager through it, so the web UI does not have to listen on the network.
setup_tunnel() {
    local key home shell
    key=$(head -n 1 "$TUNNEL_KEY")
    # The line goes into authorized_keys as root: accept exactly one bare ed25519 key, no options.
    printf '%s' "$key" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/]{60,80}={0,2}$' || die "The tunnel key is not a plain ed25519 public key."
    shell=/usr/sbin/nologin
    [ -x "$shell" ] || shell=/sbin/nologin
    home=/var/lib/$TUNNEL_USER
    if ! id "$TUNNEL_USER" >/dev/null 2>&1; then
        say "Creating the '$TUNNEL_USER' account (port forwarding only)"
        useradd --system --create-home --home-dir "$home" --shell "$shell" "$TUNNEL_USER"
    fi
    usermod -p '*' "$TUNNEL_USER" # no password, but not "locked": some sshd builds refuse keys for locked accounts
    install -d -m 700 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "$home/.ssh"
    touch "$home/.ssh/authorized_keys"
    if ! grep -qF "$key" "$home/.ssh/authorized_keys"; then
        printf 'restrict,port-forwarding,permitopen="127.0.0.1:%s" %s\n' "$PORT" "$key" >>"$home/.ssh/authorized_keys"
    fi
    chown "$TUNNEL_USER:$TUNNEL_USER" "$home/.ssh/authorized_keys"
    chmod 600 "$home/.ssh/authorized_keys"
    if have sshd && sshd -T 2>/dev/null | grep -qi '^allowtcpforwarding no'; then
        warn "This server's sshd has AllowTcpForwarding off, so the app cannot reach the manager through SSH."
    fi
}

# A first install with no password handed in makes one up. It reaches the manager the same way
# one from the Windows app does: a file it reads once and deletes. That keeps the password
# changeable under System > Password, which a VSM_ADMIN_PASSWORD line in the env file would not.
first_password() {
    [ -z "$PASSWORD_FILE" ] || return 0
    GENERATED_PASSWORD=$(new_password)
    PASSWORD_FILE=$(mktemp)
    TEMP_PASSWORD_FILE=$PASSWORD_FILE
    printf '%s\n' "$GENERATED_PASSWORD" >"$PASSWORD_FILE"
}

new_password() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20; }

open_firewall() { # game ports only; the web UI port is left to the admin
    local game="${VSM_GAME_PORT:-7777}" query="${VSM_QUERY_PORT:-27015}"
    if have ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
        say "Opening UDP $game and $query in ufw"
        ufw allow "$game/udp" >/dev/null && ufw allow "$query/udp" >/dev/null
    elif have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
        say "Opening UDP $game and $query in firewalld"
        firewall-cmd -q --permanent --add-port="$game/udp" --add-port="$query/udp" && firewall-cmd -q --reload
    fi
}

wait_healthy() {
    say "Waiting for the manager to answer on port $PORT"
    for _ in $(seq 1 60); do
        curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

# ---------------------------------------------------------------- docker mode

install_docker_mode() {
    if ! have docker; then
        say "Docker not found; installing it with Docker's official script"
        curl -fsSL https://get.docker.com | sh
    fi
    if have systemctl && ! docker info >/dev/null 2>&1; then systemctl enable --now docker; fi
    docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start it and re-run."
    if ! docker compose version >/dev/null 2>&1; then
        pkg_install docker-compose-plugin || true
        docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required: https://docs.docker.com/compose/install/linux/"
    fi

    # Only src/ is replaced on a re-run. Settings live in $DIR/.env and game data
    # in the "vein-server-manager_vsm-data" Docker volume, so neither is touched.
    if [ -n "$WHEEL" ]; then stage_bundle "$DIR/src"; else fetch_source "$DIR/src"; fi
    local first=""
    if [ ! -f "$DIR/.env" ]; then
        first=1
        first_password
        printf 'VSM_WEB_PORT=%s\n' "$PORT" >"$DIR/.env" # the password comes from the file, below
    fi
    deploy_settings "$DIR/.env" VSM_BIND
    chmod 600 "$DIR/.env"
    ln -sf ../.env "$DIR/src/.env"

    say "Building and starting the container (the first build takes a few minutes)"
    if [ -n "$PASSWORD_FILE" ] && [ -n "$first" ]; then
        # Into the data volume before the manager first starts; it reads the file once and deletes it.
        (cd "$DIR/src" && docker compose build --quiet \
            && docker compose run --rm --no-deps -T --entrypoint sh vsm -c \
                'umask 077; cat >"$VSM_DATA_DIR/initial-password"; chown "${PUID:-1000}:${PGID:-1000}" "$VSM_DATA_DIR/initial-password"' <"$PASSWORD_FILE")
    fi
    (cd "$DIR/src" && docker compose up -d --build --quiet-pull)
    LOGS_HINT="cd $DIR/src && docker compose logs -f"
}

# ---------------------------------------------------------------- native mode

python_ok() { python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }

ensure_python() {
    # Say so before installing anything if this distribution's Python can never be new enough.
    if have python3 && ! python_ok; then
        die "Python 3.11+ is required (found $(python3 -V 2>&1)). Use the Docker install instead: re-run without --native."
    fi
    if have apt-get; then
        dpkg -s python3-venv lib32gcc-s1 >/dev/null 2>&1 || pkg_install python3 python3-venv lib32gcc-s1
    elif have dnf || have yum; then
        (have python3 && rpm -q glibc.i686 >/dev/null 2>&1) || pkg_install python3 glibc.i686 libstdc++.i686
    elif have pacman; then
        have python3 || pkg_install python
        pacman -Q lib32-gcc-libs >/dev/null 2>&1 || warn "Enable [multilib] and install lib32-gcc-libs (SteamCMD needs it)."
    else
        have python3 || pkg_install python3
    fi
    python_ok \
        || die "Python 3.11+ is required (found $(python3 -V 2>&1)). Use the Docker install instead: re-run without --native."
}

ensure_node() { # only needed to build the web UI; a private copy is used if the system has none
    if have node && have npm && version_ge "$(node -v | tr -d v)" "$NODE_MIN"; then return; fi
    say "Fetching a temporary Node.js to build the web UI"
    local base="https://nodejs.org/dist/latest-v22.x" line file sum
    line=$(curl -fsSL "$base/SHASUMS256.txt" | grep 'linux-x64.tar.gz$')
    sum=${line%% *} && file=${line##* }
    NODE_TMP=$(mktemp -d)
    curl -fsSL "$base/$file" -o "$NODE_TMP/node.tgz"
    echo "$sum  $NODE_TMP/node.tgz" | sha256sum -c --quiet - || die "Node.js download failed its checksum."
    tar -xzf "$NODE_TMP/node.tgz" --strip-components=1 -C "$NODE_TMP"
    export PATH="$NODE_TMP/bin:$PATH"
}

install_native_mode() {
    ensure_python
    local unit
    if [ -n "$WHEEL" ]; then
        # The package already contains the built web UI: no source, no Node.js.
        stage_bundle "$DIR/src"
        unit="$DIR/src/vsm.service"
        say "Installing the manager into $DIR/venv"
        python3 -m venv "$DIR/venv"
        "$DIR/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$DIR/src/$(basename "$WHEEL")[discord]"
        # The same version number can carry different files (a rebuilt package): always replace ours.
        "$DIR/venv/bin/pip" install --quiet --disable-pip-version-check --force-reinstall --no-deps "$DIR/src/$(basename "$WHEEL")"
    else
        ensure_node
        fetch_source "$DIR/src"
        unit="$DIR/src/deploy/vsm.service"

        say "Building the web UI"
        (cd "$DIR/src/web" && npm ci --no-audit --no-fund --silent && npm run build --silent)
        [ -n "${NODE_TMP:-}" ] && rm -rf "$NODE_TMP"

        say "Installing the manager into $DIR/venv"
        python3 -m venv "$DIR/venv"
        "$DIR/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$DIR/src[discord]"
    fi

    # The Unreal server refuses to run as root, so the service gets its own account.
    id vsm >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin vsm
    mkdir -p "$DIR/data"
    if [ ! -f "$DIR/vsm.env" ]; then
        printf 'VSM_PORT=%s\n' "$PORT" >"$DIR/vsm.env"
        if [ ! -f "$DIR/data/auth.json" ]; then # data kept from an earlier install already has its password
            first_password
            # Read once by the manager at its first start, then deleted.
            install -m 600 "$PASSWORD_FILE" "$DIR/data/initial-password"
        fi
    fi
    deploy_settings "$DIR/vsm.env" VSM_HOST
    chmod 600 "$DIR/vsm.env"
    chown -R vsm: "$DIR"
    open_firewall

    if have systemctl && [ -d /run/systemd/system ]; then
        sed "s#/opt/vein-server-manager#$DIR#g" "$unit" >/etc/systemd/system/vsm.service
        systemctl daemon-reload
        systemctl enable --quiet vsm
        systemctl restart vsm
        LOGS_HINT="journalctl -u vsm -f"
    else
        warn "systemd not found: the manager was installed but not started. Start it as the 'vsm' user with:"
        warn "  set -a; . $DIR/vsm.env; set +a; VSM_DATA_DIR=$DIR/data VSM_HOST=0.0.0.0 $DIR/venv/bin/python -m vsm"
        NOT_STARTED=1
    fi
}

# ---------------------------------------------------------------------- main

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --docker) MODE=docker ;;
            --native) MODE=native ;;
            --dir) DIR="$2"; shift ;;
            --port) PORT="$2"; shift ;;
            --ref) REF="$2"; shift ;;
            --wheel) WHEEL="$2"; shift ;;
            --bind) BIND="$2"; shift ;;
            --password-file) PASSWORD_FILE="$2"; shift ;;
            --tunnel-key) TUNNEL_KEY="$2"; shift ;;
            --no-auto-install) NO_AUTO_INSTALL=1 ;;
            -h | --help) sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
            *) die "Unknown option: $1" ;;
        esac
        shift
    done
    GENERATED_PASSWORD="" LOGS_HINT="" NOT_STARTED="" TEMP_PASSWORD_FILE=""
    trap '[ -z "$TEMP_PASSWORD_FILE" ] || rm -f "$TEMP_PASSWORD_FILE"' EXIT
    [ -z "$WHEEL" ] || [ -f "$WHEEL" ] || die "No such package: $WHEEL"
    [ -z "$PASSWORD_FILE" ] || [ -s "$PASSWORD_FILE" ] || die "The password file is missing or empty."
    [ -z "$TUNNEL_KEY" ] || [ -s "$TUNNEL_KEY" ] || die "The tunnel key file is missing or empty."
    case "$BIND" in "" | 127.0.0.1 | 0.0.0.0) ;; *) die "--bind takes 127.0.0.1 or 0.0.0.0." ;; esac

    preflight
    case "$MODE" in
        docker) install_docker_mode ;;
        native) install_native_mode ;;
        *) die "Unknown mode '$MODE' (use docker or native)." ;;
    esac
    [ -z "$TUNNEL_KEY" ] || setup_tunnel
    [ -n "$NOT_STARTED" ] && exit 0

    wait_healthy || die "The manager did not come up. Check: $LOGS_HINT"

    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}') || true # not every hostname supports -I
    echo
    say "Vein Server Manager is running."
    if [ "$BIND" = "127.0.0.1" ]; then
        echo "    Web UI:    on this machine only (port $PORT); the Windows app reaches it through SSH"
    else
        echo "    Web UI:    http://${ip:-<this-host>}:$PORT"
    fi
    if [ -n "$GENERATED_PASSWORD" ]; then
        echo "    Password:  $GENERATED_PASSWORD   (shown only now: note it, or change it under System > Password)"
    elif [ -n "$PASSWORD_FILE" ]; then
        echo "    Password:  the one chosen by the app that installed this (shown there)"
    else
        echo "    Password:  unchanged from the previous install"
    fi
    echo "    Logs:      $LOGS_HINT"
    echo
    if [ -n "$NO_AUTO_INSTALL" ]; then
        echo "No game server is installed yet: the guided setup in the manager downloads it."
    else
        echo "The game server is downloading now (several GB); follow it on the Install / Update page."
    fi
    echo "Forward UDP ${VSM_GAME_PORT:-7777} and ${VSM_QUERY_PORT:-27015} to this machine so players can join."
    echo "Do not expose port $PORT to the internet without an HTTPS reverse proxy."
}

# Wrapped in a function so a truncated download cannot run half a script.
main "$@"
