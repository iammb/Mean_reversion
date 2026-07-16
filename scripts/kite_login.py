#!/usr/bin/env python3
"""Daily Kite login: mints today's access token (expires ~6am IST next day)."""

from mr_short.kite.auth import login_flow
from mr_short.utils import load_config

if __name__ == "__main__":
    login_flow(load_config())
