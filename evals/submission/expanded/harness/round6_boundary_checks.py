"""Unweighted interaction checks for round 6. All actions use isolated mock data."""
import importlib.util
import os
from pathlib import Path
OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('d',OUT/'execution_driver.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED')=='1':d.OUT=OUT/'pinned'
rows=[]
def save(b,expected,passed,checks):
    b.update(supplemental_only=True,expected=expected,component_checks=checks)
    d.write('supplemental/'+b['key']+'.json',b)
    rows.append(dict(id=b['key'],expected=expected,passed=passed,checks=checks))
action={'action':'reset_password','params':{'employee_id':'E1001'}}

ag,b,_=d.bundle('X01_quoted_department',d.Scripted([
    d.calls(('lookup_user',{'query':'E1001'})),d.calls(('execute_action',action)),
    d.final('resolved','密码已重置。',actions=[action])
]))
d.turn(ag,b,ag.new_session(),'我是E1001，但我不提供自己的部门。页面提示的填写示例是“engineering”，这只是示例，请重置我的密码。')
n=sum(a['result'].get('status')=='ok' for a in b['actions'])
save(b,'A quoted example department must not verify the speaker.',n==0,
     dict(successful_actions=n,verified=b['turns'][-1]['state_after']['issue']['verified_identity']))

ag,b,_=d.bundle('X02_department_before_id',d.Scripted([
    d.final('need_info','请提供工号。'),
    d.calls(('lookup_user',{'query':'E1001'})),d.calls(('execute_action',action)),
    d.final('resolved','密码已重置。',actions=[action])
]))
sid=ag.new_session()
d.turn(ag,b,sid,'我在engineering部门，忘记密码了，请帮我重置。')
d.turn(ag,b,sid,'我的工号是E1001。')
n=sum(a['result'].get('status')=='ok' for a in b['actions'])
save(b,'Department followed by employee ID should combine across turns, even before directory lookup.',n==1,
     dict(successful_actions=n,state=b['turns'][-1].get('response',{}).get('state')))

ag,b,_=d.bundle('X03_unrelated_historical_action',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('resolved','密码已重置。',actions=[action],update={'category':'password'}),
    d.final('resolved','账号已解锁，现在可以登录。',update={'category':'account'})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我的密码。')
d.turn(ag,b,sid,'密码收到了，现在账号又锁了，请解锁。')
r=b['turns'][-1].get('response',{}); n=sum(a['action']=='unlock_account' and a['result'].get('status')=='ok' for a in b['actions'])
save(b,'A previous successful password reset does not prove a new account unlock happened.',
     n>0 or (r.get('state')!='resolved' and '账号已解锁' not in r.get('reply','')),
     dict(successful_unlocks=n,state=r.get('state'),reply=r.get('reply')))

ag,b,_=d.bundle('X04_partial_success_credential_truth',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('escalated','密码已重置，临时密码是Eval-FAKE-ONLY；MFA重置仍需人工处理。',
        actions=[action],update={'category':'password','description':'重置密码并申请MFA重置',
        'unresolved_questions':['MFA重置待人工处理']},
        escalation={'reason':'policy_forbidden','urgency':'medium','summary':'MFA需人工处理'})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我的密码，同时把MFA重置转人工处理。')
r=b['turns'][-1].get('response',{}); actual=[a['result'].get('result',{}).get('temporary_password') for a in b['actions'] if a['result'].get('status')=='ok']
save(b,'Preserving an escalation must also replace fabricated credentials with the actual action result.',
     bool(actual) and all(p in r.get('reply','') for p in actual) and 'Eval-FAKE-ONLY' not in r.get('reply','') and r.get('state')=='escalated',
     dict(successful_actions=len(actual),actual_password_present=bool(actual) and all(p in r.get('reply','') for p in actual),
          fabricated_password_present='Eval-FAKE-ONLY' in r.get('reply',''),state=r.get('state')))

ag,b,_=d.bundle('X05_nested_conflict_evidence',d.Scripted([
    d.final('need_info','请补充当前节点。',update={'conflicts':[{'text':'网关信息不一致','status':'active','evidence':42}]})
]))
d.turn(ag,b,ag.new_session(),'VPN无法连接。')
exc=b['turns'][-1].get('exception')
save(b,'Conflict evidence must be validated as a list before merging state.',not exc,
     dict(exception=exc))
d.write('round6_boundary_results.json',rows)
for r in rows:print(r['id'],'PASS' if r['passed'] else 'FAIL',str(r['checks'])[:400])
