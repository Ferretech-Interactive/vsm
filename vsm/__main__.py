"""Entry point: `python -m vsm` or the `vsm` console script."""
from __future__ import annotations

import os

import uvicorn


def build_server() -> uvicorn.Server:
    """Create the manager and its HTTP server. The desktop shell runs this on a background thread."""
    from .app import create_app

    app = create_app()
    # VSM_HOST (Docker, portable, tests) wins; otherwise the in-app "allow other devices" setting decides.
    remote = app.state.manager.config.get().network.remote_access
    config = uvicorn.Config(
        app,
        host=os.environ.get("VSM_HOST") or ("0.0.0.0" if remote else "127.0.0.1"),
        port=int(os.environ.get("VSM_PORT", "8810")),
        # Only behind a reverse proxy the admin names: otherwise anything that reaches the manager over
        # loopback (an SSH tunnel, another local user) could pick its own address for the login lockout.
        proxy_headers=bool(os.environ.get("VSM_TRUSTED_PROXIES")),
        forwarded_allow_ips=os.environ.get("VSM_TRUSTED_PROXIES") or None,
        timeout_graceful_shutdown=5,
        log_level="warning",
        log_config=None,  # keep the manager's own logging setup (and work without a console)
    )
    return uvicorn.Server(config)


def main() -> None:
    from .core import selfupdate

    selfupdate.chain()  # Linux: a newer version installed from the web UI takes over here
    server = build_server()
    manager = server.config.app.state.manager
    selfupdate.attach(manager, server)
    server.run()
    selfupdate.reexec(manager)


if __name__ == "__main__":
    main()
