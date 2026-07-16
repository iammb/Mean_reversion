"""Shared plumbing: paths, config, logging, state ledger."""

import datetime as dt
import glob
import json
import logging
import os

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(ROOT, "data")
SCAN_DIR = os.path.join(ROOT, "scan_results")
ORDERS_DIR = os.path.join(ROOT, "orders")
LOGS_DIR = os.path.join(ROOT, "logs")
CONFIG_PATH = os.path.join(ROOT, "config", "config.yaml")
SECRETS_PATH = os.path.join(ROOT, "config", "secrets.env")
STATE_PATH = os.path.join(ORDERS_DIR, "positions_state.json")


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    _load_secrets()
    return cfg


def _load_secrets(path: str = SECRETS_PATH):
    """Load KEY=VALUE lines from config/secrets.env into os.environ."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_logger(name: str) -> logging.Logger:
    os.makedirs(LOGS_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(
        os.path.join(LOGS_DIR, f"mrshort_{dt.date.today().isoformat()}.log")
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def latest_file(pattern: str) -> str:
    """Newest file matching a glob pattern, or raise with a helpful message."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no files match {pattern} - run the previous step first")
    return files[-1]


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"trades": []}


def save_state(state: dict):
    os.makedirs(ORDERS_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def inr(x: float) -> str:
    """Indian-style grouped currency string: 12,34,567."""
    x = round(float(x))
    s, sign = str(abs(x)), "-" if x < 0 else ""
    if len(s) <= 3:
        return f"Rs {sign}{s}"
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return f"Rs {sign}{','.join(parts)},{tail}"
