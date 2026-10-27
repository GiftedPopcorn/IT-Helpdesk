"""Read-only application assessment orchestration; outputs stay in this run."""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
env = dict(os.environ)
for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    env.pop(key, None)
for name in ('tmp','cache','logs','traces'):
    (OUT/name).mkdir(exist_ok=True)
env.update(TEMP=str(OUT/'tmp'), TMP=str(OUT/'tmp'), PIP_CACHE_DIR=str(OUT/'cache'),
           PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', TRACE_LOG_PATH=str(OUT/'traces/default.jsonl'))
rows=[]
def run(name, python, args, extra=None, timeout=1200):
    current={**env, **(extra or {})}
    started=datetime.now().isoformat()
    with (OUT/'logs'/f'{name}.log').open('w',encoding='utf-8') as log:
        try:
            result=subprocess.run([str(python),'-B','-X','utf8',*map(str,args)], cwd=ROOT,
                env=current, stdout=log, stderr=log, timeout=timeout)
            row=dict(name=name,returncode=result.returncode,started_at=started)
        except Exception as e:
            row=dict(name=name,error=type(e).__name__,detail=str(e),started_at=started)
    rows.append(row)
    (OUT/f'{sys.argv[1]}_orchestration.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(name, row.get('returncode',row.get('error')),flush=True)
    return row

mode=sys.argv[1]
if mode=='offline':
    run('system_local',sys.executable,[OUT/'execution_driver.py','local'])
    run('pinned_local',ROOT/'.venv/Scripts/python.exe',[OUT/'pinned_checks.py'])
    names=['supplemental_checks','additional_identity_checks','new_boundary_checks']
    names += [f'round{i}_boundary_checks' for i in range(5,12)]
    names += ['round12_identity_checks']
    for label,python,extra in [('system',sys.executable,{}),('pinned',ROOT/'.venv/Scripts/python.exe',{'ASSESSMENT_PINNED':'1'})]:
        for name in names:
            run(label+'_'+name,python,[OUT/(name+'.py')],extra)
elif mode in ('probe','real'):
    row=run(mode,sys.executable,[OUT/'execution_driver.py',mode],
        {'LLM_PROVIDER':'deepseek','LLM_MODEL':'deepseek-chat','LLM_BASE_URL':'https://api.deepseek.com',
         'LLM_TEMPERATURE':'0.2','AGENT_MAX_STEPS_PER_TURN':'8','AGENT_MAX_CONTEXT_MESSAGES':'12','TOOL_TIMEOUT_SECONDS':'15'},timeout=2400)
    sys.exit(row.get('returncode',1))
else:
    raise ValueError(mode)
