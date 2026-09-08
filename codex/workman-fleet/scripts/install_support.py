"""Install only the fleet adapter and reporter, with verified per-file backups.

Does not install apps/dependencies, alter MCP config, touch virtual environments,
start services, change privacy grants, or add schedules.
"""
import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from fleet import SSH, ROOT, registry

INSTALL = r'''
import base64,hashlib,json,os,time
from pathlib import Path
payload=json.load(__import__('sys').stdin)
home=Path.home()
dest=home/'.claude/tools/workman-fleet-v1.1'
dest.mkdir(parents=True,exist_ok=True)
backup=dest/'backups'/time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
manifest=[]
for name,encoded in payload.items():
    if name not in ('device.py','learning.py','lease.py','input_switch.py','input_panel.py','permission_hint.py','motion_profile.py','fleet-report.py','Workman Input Controls.command','workman-input-controls.desktop'):
        raise ValueError('unexpected file')
    path=home/name if name=='fleet-report.py' else dest/name
    if name.endswith('.command'): path=home/'Desktop'/name
    if name.endswith('.desktop'): path=home/'.local/share/applications'/name
    path.parent.mkdir(parents=True,exist_ok=True)
    data=base64.b64decode(encoded)
    sha=lambda b:hashlib.sha256(b).hexdigest()
    old=path.read_bytes() if path.exists() else None
    if old==data:
        manifest.append({'path':str(path),'unchanged':True,'sha256':sha(data)})
        continue
    if old is not None:
        backup.mkdir(parents=True,exist_ok=True)
        saved=backup/name
        if saved.exists() and saved.read_bytes()!=old:
            raise ValueError('backup collision')
        saved.write_bytes(old)
        os.chmod(saved,0o600)
        if saved.read_bytes()!=old:raise ValueError('backup verification failed')
    tmp=path.with_suffix('.fleet-new')
    tmp.write_bytes(data)
    os.chmod(tmp,0o700)
    os.replace(tmp,path)
    if path.read_bytes()!=data:raise ValueError('installed verification failed')
    manifest.append({'path':str(path),'sha256':sha(data),'previous_sha256':sha(old) if old else None,'backup':str(backup/name) if old else None})
print(json.dumps(manifest))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nodes", nargs="+", choices=list(registry()))
    args = parser.parse_args()
    data = {n: base64.b64encode((ROOT / "scripts" / n).read_bytes()).decode()
            for n in ("device.py", "learning.py", "lease.py", "input_switch.py", "input_panel.py", "permission_hint.py", "motion_profile.py", "fleet-report.py")}
    for node in args.nodes:
        conf = registry()[node]
        import shlex
        panel = conf['home'] + '/.claude/tools/workman-fleet-v1.1/input_panel.py'
        launch = shlex.join(['/usr/bin/env', 'WORKMAN_FLEET_NODE='+node,
                             conf.get('gtk_python', conf['python']), panel])
        payload = dict(data)
        name = 'workman-input-controls.desktop' if node == 'dgx' else 'Workman Input Controls.command'
        shortcut = ('[Desktop Entry]\nType=Application\nName=Workman Input Controls\nExec='+launch+'\nTerminal=false\n') if node == 'dgx' else '#!/bin/sh\nexec '+launch+'\n'
        payload[name] = base64.b64encode(shortcut.encode()).decode()
        if node == "dgx":
            cmd = [sys.executable, "-c", INSTALL]
        else:
            import shlex
            cmd = SSH + [conf["ssh"], shlex.join([conf["python"], "-c", INSTALL])]
        r = subprocess.run(cmd, input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        if r.returncode:
            raise SystemExit(f"{node}: install failed ({r.returncode}); no secrets or remote error text echoed")
        print(json.dumps({"node": node, "files": json.loads(r.stdout)}))


if __name__ == "__main__":
    main()
