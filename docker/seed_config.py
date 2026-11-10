"""Run only with application workers stopped. Merge config without resetting rotated tokens."""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config_sync import sync
from app.integrations.hh.token_lock import token_file_lock
folder=Path('/config');folder.mkdir(exist_ok=True)
with token_file_lock(folder/'hh.env'):
    changed=sync('/run/secrets/hh_seed',folder)
    for p in folder.iterdir():
        if p.is_file():os.chown(p,10001,10001);p.chmod(0o600)
os.chown(folder,10001,10001);folder.chmod(0o700)
print('CONFIG_SYNC_READY; source auth updated' if changed else 'CONFIG_SYNC_READY; rotated tokens preserved')

