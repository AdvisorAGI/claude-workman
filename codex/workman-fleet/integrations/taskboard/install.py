"""Install the reviewed mini board only; retain verified rollback files and tasks."""
import hashlib, json, os, signal, subprocess, time
from pathlib import Path
home=Path.home(); board=home/'.claude/board'; build=home/'.claude/tools/workman-fleet-v1.1/builds'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(board/'TaskBoard.swift')=='20178d6807ad3378bfa2d3c816ad535f50d915aeb39f879ae67c459800baae50', 'Source changed; inspect before install'
assert sha(board/'taskboard')=='180132f116230059861d2b50cdc5461d8e01411c9e385121d478777bcc63e213', 'Binary changed; inspect before install'
assert subprocess.check_output(['/bin/ps','-p','50406','-o','args='],text=True).strip()=='./taskboard'
assert 'n'+str(board) in subprocess.check_output(['/usr/sbin/lsof','-a','-p','50406','-d','cwd','-Fn'],text=True).splitlines()
fixtures=build/'model-fixture'; fixtures.mkdir(exist_ok=True)
(fixtures/'one.json').write_text(json.dumps({'title':'Fixture','items':[{'text':'active-fixture-item','state':'working'},{'text':'active-fixture-item','state':'working'},{'text':'completed-fixture-item','state':'done'}]}))
status=build/'status-fixture.json'; status.write_text(json.dumps({'schema_version':1,'device':'mini','task':'fixture','verification_event':'f'*32}))
env=dict(os.environ,WORKMAN_BOARD_SESSIONS=str(fixtures),WORKMAN_BOARD_STATUS=str(status),WORKMAN_BOARD_JOURNAL=str(build/'no-journal'))
test=subprocess.run([str(build/'taskboard-v1.1'),'--self-test'],env=env,capture_output=True,text=True,check=True)
before={p.name:sha(p) for p in (board/'sessions').glob('*.json')}
backup=board/'backups'/time.strftime('workman-v1.1-%Y%m%dT%H%M%SZ',time.gmtime());backup.mkdir(parents=True)
manifest=[]
for path in [board/'TaskBoard.swift',board/'taskboard',home/'Library/LaunchAgents/com.tariqul.taskboard.plist']:
 saved=backup/path.name;saved.write_bytes(path.read_bytes());saved.chmod(path.stat().st_mode & 0o777)
 assert sha(saved)==sha(path);manifest.append({'path':str(path),'backup':str(saved),'sha256':sha(saved)})
for source,target in [(build/'TaskBoard.v1.1.swift',board/'TaskBoard.swift'),(build/'taskboard-v1.1',board/'taskboard')]:
 tmp=target.with_suffix('.v11-new');tmp.write_bytes(source.read_bytes());tmp.chmod(0o755 if target.name=='taskboard' else 0o644);tmp.replace(target);assert sha(source)==sha(target)
# Restart only the identified managed panel; terminate only its verified duplicate.
subprocess.run(['/bin/launchctl','kickstart','-k',f'gui/{os.getuid()}/com.tariqul.taskboard'],check=True)
os.kill(50406,signal.SIGTERM)
after={p.name:sha(p) for p in (board/'sessions').glob('*.json')}
result={'installed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'backups':manifest,'new_binary_sha256':sha(board/'taskboard'),'source_sha256':sha(board/'TaskBoard.swift'),'task_hashes_unchanged':before==after,'task_count':len(after),'model_test':test.stdout.strip()}
(backup/'manifest.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
