"""Read-only application resources and per-user writable desktop data."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FROZEN=bool(getattr(sys,'frozen',False))
DATA_DIR=Path(os.environ['LINGXI_DATA_DIR']).resolve() if os.environ.get('LINGXI_DATA_DIR') else (
    Path(os.environ['LOCALAPPDATA'])/'LingxiWorkspace' if FROZEN else ROOT/'.local')
RECHARGE_DATA_DIR=DATA_DIR/'recharge' if FROZEN or os.environ.get('LINGXI_DATA_DIR') else ROOT/'recharge/.local'
VERSION='1.0.1'
