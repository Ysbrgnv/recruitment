"""Validate first, then update host secret files. No network, no credential output."""
from pathlib import Path
import sys,secrets,re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from scripts.setup.config_sync import allowed,atomic_env

def prepare(root):
    root=Path(root);source=root/'.env';folder=root/'deploy/secrets'
    if not source.is_file():raise SystemExit('Place .env next to START_LOCAL.ps1.')
    values=dotenv_values(source,interpolate=False,encoding='utf-8-sig')
    required=['HH_CLIENT_ID','HH_CLIENT_SECRET','EMPLOYER_ID','NANO_GPT_API_KEY','GMAIL_USER']
    missing=[k for k in required if not (values.get(k) or '').strip()]
    if not (values.get('HH_BEARER') or values.get('HH_REFRESH_TOKEN')):missing.append('HH_BEARER or HH_REFRESH_TOKEN')
    password=''.join((values.get('GMAIL_APP_PASSWORD') or '').split())
    if not re.fullmatch('[A-Za-z0-9]{16}',password):missing.append('GMAIL_APP_PASSWORD (16 characters)')
    if missing:raise SystemExit('Missing/invalid .env fields: '+', '.join(missing))
    names=['db_admin','db_app','db_backup','browser_key','metrics_token','gmail_password','hh_seed']
    existing=folder.exists()
    if existing and not all((folder/n).is_file() and (folder/n).stat().st_size for n in names):
        raise SystemExit('Incomplete secrets or directories instead of files. Nothing overwritten. Use a NEW folder.')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not existing:
        for n in names[:5]:
            p=folder/n;p.write_text(secrets.token_hex(32)+'\n');p.chmod(0o444)
    p=folder/'gmail_password'
    if p.exists():p.chmod(0o600)
    p.write_text(password+'\n');p.chmod(0o444)
    atomic_env(folder/'hh_seed',{k:v for k,v in values.items() if v is not None and allowed(k)},0o444)
    (root/'deploy/backups').mkdir(exist_ok=True)
    print('CONFIG_PREPARED: seven secret FILES; database passwords preserved; source .env imported.')

if __name__=='__main__':prepare('/setup')

