"""Unweighted round-7 variants for the changed identity and action guards."""
import importlib.util
import os
from pathlib import Path
OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('d',OUT/'execution_driver.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED')=='1':d.OUT=OUT/'pinned'
rows=[]
action={'action':'reset_password','params':{'employee_id':'E1001'}}
def save(b,expected,passed,checks):
    b.update(supplemental_only=True,expected=expected,component_checks=checks)
    d.write('supplemental/'+b['key']+'.json',b)
    rows.append(dict(id=b['key'],expected=expected,passed=passed,checks=checks))
def reset_case(key,msg,should_succeed):
    ag,b,_=d.bundle(key,d.Scripted([
        d.calls(('lookup_user',{'query':'E1001'})),d.calls(('execute_action',action)),
        d.final('resolved','密码已重置。',actions=[action])
    ]))
    d.turn(ag,b,ag.new_session(),msg)
    n=sum(a['result'].get('status')=='ok' for a in b['actions'])
    st=b['turns'][-1]['state_after']
    save(b,'Accept only a real first-person department claim; punctuation alone does not decide ownership.',
         n==int(should_succeed),dict(successful_actions=n,verified=st['issue']['verified_identity'],identity_claim=st.get('identity_claim')))

reset_case('Y01_unquoted_example_department',
    '我是E1001，但我不提供自己的部门。页面提示的填写示例是 engineering，这只是示例，请重置我的密码。',False)
reset_case('Y02_quoted_own_department',
    '我的工号是E1001，本人所在部门是“engineering”。请重置我的密码。',True)

ag,b,_=d.bundle('Y03_same_turn_unrelated_success',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('resolved','账号已解锁，现在可以登录。',update={'category':'account'})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我的密码并解锁账号。')
r=b['turns'][-1].get('response',{})
n=sum(a['action']=='unlock_account' and a['result'].get('status')=='ok' for a in b['actions'])
save(b,'A reset in this turn must not prove an unlock occurred in the same turn.',
     n>0 or (r.get('state')!='resolved' and '账号已解锁' not in r.get('reply','')),
     dict(successful_unlocks=n,successful_actions=sum(a['result'].get('status')=='ok' for a in b['actions']),state=r.get('state'),reply=r.get('reply')))

ag,b,_=d.bundle('Y04_repeat_prior_receipt',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('resolved','密码已重置。',actions=[action],update={'category':'password'}),
    d.final('resolved','刚才的密码重置已完成，临时密码有效期30分钟。',actions=[action],update={'category':'password'})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我的密码。')
d.turn(ag,b,sid,'请再告诉我刚才重置的结果和有效期，不要再次重置。')
r=b['turns'][-1].get('response',{})
actual=[a['result'].get('result',{}).get('temporary_password') for a in b['actions'] if a['result'].get('status')=='ok']
save(b,'A request to repeat a prior successful receipt should reuse its result without rerunning or claiming the operation failed.',
     len(actual)==1 and bool(actual[0]) and actual[0] in r.get('reply','') and r.get('state')!='escalated',
     dict(successful_resets=len(actual),state=r.get('state'),actual_result_present=bool(actual) and actual[0] in r.get('reply',''),reply=r.get('reply')))
d.write('round7_boundary_results.json',rows)
for r in rows:print(r['id'],'PASS' if r['passed'] else 'FAIL',str(r['checks'])[:420])
