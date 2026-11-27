"""Read-only application resources and per-user writable desktop data."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FROZEN=bool(getattr(sys,'frozen',False))
USER_DATA_ROOT=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')/'LingxiWorkspace'
DATA_DIR=Path(os.environ['LINGXI_DATA_DIR']).resolve() if os.environ.get('LINGXI_DATA_DIR') else (
    USER_DATA_ROOT/'data')
if DATA_DIR.resolve().is_relative_to(ROOT):
    raise ValueError('账号数据目录必须位于项目目录外，请设置外部 LINGXI_DATA_DIR。')
RECHARGE_DATA_DIR=DATA_DIR/'recharge'
VERSION='2.2.1'


def prepare_runtime_data():
    if os.environ.get('LINGXI_DATA_DIR'):
        DATA_DIR.mkdir(parents=True,exist_ok=True)
    else:
        from workspace.data_directory import prepare_default_data
        prepare_default_data(ROOT,DATA_DIR,USER_DATA_ROOT)
