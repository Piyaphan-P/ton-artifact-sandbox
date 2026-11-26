"""Run on the VPS as root. On-host recovery backup; copy off-host separately."""
import datetime,os,pathlib,sqlite3,tarfile,tempfile
os.umask(0o077)
source=pathlib.Path('/opt/ton-artifacts/data')
config=pathlib.Path('/root/orca/projects/ton-agent-vps-hostinger/artifact-platform')
target=pathlib.Path('/opt/ton-artifacts/backups');target.mkdir(mode=0o700,exist_ok=True)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
dest=target/('artifacts-'+stamp+'.tar.gz')
with tempfile.TemporaryDirectory() as tmp:
    dbcopy=pathlib.Path(tmp)/'platform.db'
    with sqlite3.connect('file:'+str(source/'platform.db')+'?mode=ro',uri=True) as src,sqlite3.connect(dbcopy) as db:src.backup(db)
    with tarfile.open(dest,'w:gz') as t:
        t.add(dbcopy,arcname='data/platform.db')
        for name in ('uploads','routes'):t.add(source/name,arcname='data/'+name)
        t.add(config/'.env',arcname='config/.env')
        t.add(config/'compose.yml',arcname='config/compose.yml')
    dest.chmod(0o600)
print('Backup created:',dest)
