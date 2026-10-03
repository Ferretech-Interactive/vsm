"""Deploying to, and reaching, a manager on a remote Linux machine over SSH.

Everything here runs in the desktop process as the signed-in Windows user, never in the service.

- `ssh.py`: one connection with a pinned host key; fixed commands only, secrets only over stdin.
- `preflight.py`: what the server is and which kind of install it can take (read-only).
- `deploy.py`: upload the bundled Linux package and run `install.sh` there.
- `keys.py`: the single-purpose tunnel key, encrypted for this Windows user.
- `tunnel.py`: a loopback port on this PC forwarded to the manager's loopback port over SSH.
"""
