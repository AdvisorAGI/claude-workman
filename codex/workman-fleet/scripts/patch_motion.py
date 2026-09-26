"""Let the existing Mac motion loop observe Workman's local STOP switch.

The file is read only during an explicitly requested movement, never at idle.
There is no mouse/keyboard event monitor or new scheduler.
"""
import hashlib, json, os, sys, time
from pathlib import Path

OLD = '''        cx, cy = clamp_to_display(x, y)
        _post(kind, cx, cy, cx - prev_x, cy - prev_y, button)
'''
NEW = '''        # Workman fleet v1.1: STOP between emitted pointer events.
        if not _fleet_input_enabled("mouse"):
            raise RuntimeError("fleet_input_disabled")
        cx, cy = clamp_to_display(x, y)
        _post(kind, cx, cy, cx - prev_x, cy - prev_y, button)
'''
CHECK = '''
def _fleet_input_enabled(kind):
    import json
    from pathlib import Path
    path = Path(os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")).expanduser() / "fleet-input-switch.json"
    try:
        state = json.loads(path.read_text())
        return state.get(kind) is True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, AttributeError):
        return False

'''


def patched(text):
    if NEW in text: return text
    if text.count(OLD) != 1: raise ValueError("unknown motion source")
    return text.replace(OLD, NEW) + CHECK


def main():
    path = Path(sys.argv[1]).resolve() / "atmos_computer/motion.py"
    old = path.read_bytes(); new = patched(old.decode()).encode()
    if new == old: print(json.dumps({"path":str(path),"unchanged":True}));return
    compile(new,str(path),"exec")
    backup=Path.home()/'.claude/tools/workman-fleet-v1.1/backups'/time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())/'motion.py'
    backup.parent.mkdir(parents=True,exist_ok=True)
    with backup.open('xb') as f:os.chmod(backup,0o600);f.write(old)
    if backup.read_bytes()!=old:raise ValueError('backup verification failed')
    tmp=path.with_suffix('.fleet-new');tmp.write_bytes(new);os.chmod(tmp,path.stat().st_mode&0o777);os.replace(tmp,path)
    if path.read_bytes()!=new:raise ValueError('installed verification failed')
    print(json.dumps({'path':str(path),'backup':str(backup),'previous_sha256':hashlib.sha256(old).hexdigest(),'sha256':hashlib.sha256(new).hexdigest()}))


if __name__=='__main__':main()
