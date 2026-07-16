"""Kite Connect session handling.

Kite access tokens expire daily at ~6am IST. Run scripts/kite_login.py once
each morning to mint a fresh one; it is stored in config/.access_token
(gitignored). get_kite() returns an authenticated client or raises with
instructions.
"""

import os

from ..utils import ROOT, get_logger

log = get_logger("kite.auth")


def _token_path(cfg: dict) -> str:
    return os.path.join(ROOT, cfg["kite"]["access_token_file"])


def get_api_key(cfg: dict) -> str:
    key = os.environ.get(cfg["kite"]["api_key_env"], "")
    if not key or key == "your_api_key_here":
        raise RuntimeError(
            "KITE_API_KEY not set. Copy config/secrets.env.example to "
            "config/secrets.env and fill in your Kite Connect app keys."
        )
    return key


def get_kite(cfg: dict):
    """Authenticated KiteConnect client (raises if not logged in today)."""
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=get_api_key(cfg))
    path = _token_path(cfg)
    if not os.path.exists(path):
        raise RuntimeError("No access token - run scripts/kite_login.py first (daily).")
    with open(path) as f:
        kite.set_access_token(f.read().strip())
    profile = kite.profile()  # fails fast if the token has expired
    log.info(f"Kite session OK: {profile['user_id']} ({profile['user_name']})")
    return kite


def login_flow(cfg: dict):
    """Interactive daily login: print URL, accept request_token, save access token."""
    from kiteconnect import KiteConnect

    api_key = get_api_key(cfg)
    secret = os.environ.get(cfg["kite"]["api_secret_env"], "")
    if not secret or secret == "your_api_secret_here":
        raise RuntimeError("KITE_API_SECRET not set in config/secrets.env")

    kite = KiteConnect(api_key=api_key)
    print(f"\n1. Open this URL and log in:\n   {kite.login_url()}")
    print("2. After login you land on your redirect URL - copy the request_token param.")
    request_token = input("3. Paste request_token: ").strip()

    session = kite.generate_session(request_token, api_secret=secret)
    path = _token_path(cfg)
    with open(path, "w") as f:
        f.write(session["access_token"])
    os.chmod(path, 0o600)
    print(f"Access token saved to {path} (valid until ~6am IST tomorrow).")
