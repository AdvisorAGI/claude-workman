"""Explicit Air-only local fixture benchmark. Grants are checked before any UI setup.

No human-relative score until a person performs identical trials. 'human' here
means smooth pointer automation; keyboard cadence is the existing helper's.
No network page, account, clipboard, customer API or real business submission.
"""
import argparse,asyncio,base64,hashlib,json,math,shlex,statistics,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import fleet

SCENARIOS={
 'coder':'assert 12 * 3 == 36',
 'marketer':'Draft: Spring launch | budget 250 | review only',
 'business':'Funding plan: 120 + 80 = 200 | no transaction',
}


def require_ready(result):
    p=(result.get('data') or {}).get('permissions') or {}
    if not result.get('ok') or p.get('screen_recording') is not True or p.get('accessibility') is not True:
        raise RuntimeError('Air benchmark blocked: human Screen Recording and Accessibility grants must both be freshly verified.')
    if (result.get('data') or {}).get('input_switches') != {'mouse':True,'keyboard':True}:
        raise RuntimeError('Air benchmark blocked: user input switch is OFF.')


def exact(observed,text):
    return observed.get('chars')==len(text) and observed.get('sha256')==hashlib.sha256(text.encode()).hexdigest()


async def run(out,repeats=3,start_at=0):
    out.mkdir(parents=True,exist_ok=True);out.chmod(0o700)
    status=await fleet.control('air','status');require_ready(status)
    c=fleet.registry()['air'];root=c['home']+'/.claude/tools/workman-fleet-v1.1'
    async def call(action,args=None):
        start=time.perf_counter();r=await fleet.control('air',action,args)
        r['elapsed_ms']=(time.perf_counter()-start)*1000
        if not r.get('ok'):raise RuntimeError('Workman '+action+' failed: '+r.get('code','unknown'))
        return r
    def remote(argv,stdin=None):
        p=subprocess.run(fleet.SSH+[c['ssh'],shlex.join(argv)],input=stdin,capture_output=True,timeout=30)
        if p.returncode:raise RuntimeError('Fixture setup/read failed')
        return p.stdout
    original=(await call('active'))['data'];pointer=(await call('pointer'))['data']
    reserved=await call('reserve',{'seconds':300})
    results=[];fixture_pid=None;current=None;failure=None;index=0
    try:
        # Source setup only after grants pass; fixed private fixture with no observer.
        source=Path(__file__).with_name('gui_fixture.py').read_bytes()
        remote([c['python'],'-c',"from pathlib import Path;import sys;p=Path.home()/'.claude/tools/workman-fleet-v1.1/gui_fixture.py';p.write_bytes(sys.stdin.buffer.read())"],source)
        launch=shlex.join([c['python'],root+'/gui_fixture.py',root+'/benchmark-result.json'])
        subprocess.run(fleet.SSH+[c['ssh'],'nohup '+launch+' >/tmp/workman-benchmark-fixture.log 2>&1 </dev/null &'],capture_output=True,check=True,timeout=10)
        for _ in range(10):
            wins=(await call('windows'))['data'];fixture=next((w for w in wins if w.get('title')=='Workman v1.1 Input Check'),None)
            if fixture:break
            await asyncio.sleep(.3)
        if not fixture:raise RuntimeError('Fixture did not appear')
        fixture_pid=fixture['pid'];region={k:round(fixture[k]) for k in ('x','y','w','h')}
        x,y=region['x'],region['y'];entry=(x+205,y+128);mark=(x+105,y+202)
        for repeat in range(repeats):
            for mode in ('direct','human'):
                for scenario,text in SCENARIOS.items():
                    trial_index=index;index+=1
                    if trial_index<start_at:continue
                    current={'trial_index':trial_index,'repeat':repeat,'mode':mode,'scenario':scenario}
                    require_ready(await call('status'))
                    await call('focus',{'query':'Workman v1.1 Input Check'})
                    before=await call('shot',{'region':region});before['data'].pop('image')
                    t=time.perf_counter();click=await call('click',{'x':entry[0],'y':entry[1],'motion':mode,'speed':1})
                    active=(await call('active'))['data'];token=active['focus_token']
                    if active['pid']!=fixture_pid:raise RuntimeError('User takeover: focus left fixture')
                    await call('key',{'key':'cmd+a','expect_focus':token})
                    typed=await call('type',{'text':text,'expect_focus':token})
                    marked=await call('click',{'x':mark[0],'y':mark[1],'motion':mode,'speed':1})
                    latency=(time.perf_counter()-t)*1000
                    p=(await call('pointer'))['data']
                    observed=json.loads(remote(['cat',root+'/benchmark-result.json']))
                    shot=await call('shot',{'region':region})
                    filename=f'{repeat}-{mode}-{scenario}.png'
                    (out/filename).write_bytes(base64.b64decode(shot['data'].pop('image')))
                    results.append({'repeat':repeat,'mode':mode,'scenario':scenario,'success':exact(observed,text) and observed['clicks']==len(results)+1,
                        'exact_text':exact(observed,text),'chars':observed['chars'],'digest':observed['sha256'],'click_error_points':math.dist((p['x'],p['y']),mark),
                        'retries':0,'task_ms':latency,'type_ms':typed['elapsed_ms'],'pointer_click_ms':marked['elapsed_ms'],'action_event':typed['event']['id'],'capture_event':shot['event']['id'],'screenshot':filename})
                    if not results[-1]['success']:raise RuntimeError('Fixture mismatch: stopped without input retry')
        return results
    except Exception as error:
        # Keep interruption denominators; no arbitrary exception text or input.
        known=('focus_changed','input_disabled','permission_required','device_busy')
        code=next((c for c in known if c in str(error)),'fixture_or_transport_failure')
        failure={'trial':current,'code':code,'automatic_retry':False}
        raise
    finally:
        active=await fleet.control('air','active')
        if fixture_pid and active.get('data',{}).get('pid')==fixture_pid:
            # Own fixture close, then original app/pointer. Never move over takeover.
            await fleet.control('air','click',{'x':x+316,'y':y+202})
            await fleet.control('air','finish',{'restore_focus':original['app'],'restore_x':round(pointer['x']),'restore_y':round(pointer['y'])})
        else:await fleet.control('air','release')
        planned=repeats*6-start_at;attempted=len(results)+(1 if failure else 0)
        report={'trials':results,'planned':planned,'attempted':attempted,'not_attempted':planned-attempted,'failure':failure,'start_at':start_at,
                'human_baseline':None,'human_baseline_state':'missing: person must run identical trials',
                'comparison':'direct pointer vs smooth pointer automation; same existing keyboard cadence',
                'stop_latency':'not measured by this runner','cpu_memory_idle':'measure separately; do not infer from planner cost',
                'gate_observation':status['event']['id'],'retries':0}
        (out/'results.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--repeats',type=int,choices=range(1,11),default=3)
    p.add_argument('--start-at',type=int,default=0,help='Explicit observed recovery only, zero-based trial index; use a new output directory')
    a=p.parse_args()
    if not 0<=a.start_at<a.repeats*6:p.error('start-at must name a planned trial')
    asyncio.run(run(a.output,a.repeats,a.start_at))
