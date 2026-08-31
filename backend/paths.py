"""Canonical filesystem locations used by the backend and frontend bridge."""

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
CONFIG_DIR = BACKEND_ROOT / "config"
DATA_DIR = BACKEND_ROOT / "data"
PROMPTS_DIR = BACKEND_ROOT / "prompts"
