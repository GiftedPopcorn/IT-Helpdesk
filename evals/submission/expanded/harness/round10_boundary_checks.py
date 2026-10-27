"""Unweighted adjacent variants; isolated fixtures and simulated actions only."""
import importlib.util
import os
from pathlib import Path

OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('d10',OUT/'execution_driver.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED')=='1':d.OUT=OUT/'pinned'
rows=[]
action={'action':'reset_password','params':{'employee_id':'E1001'}}

def save(b,expected,passed,checks):
    b.update(supplemental_only=True,expected=expected,component_checks=checks)
    d.write('supplemental/'+b['key']+'.json',b)
    rows.append(dict(id=b['key'],expected=expected,passed=passed,checks=checks))

ag,b,_=d.bundle('N01_nonpersonal_document_department',d.Scripted([
    d.calls(('lookup_user',{'query':'E1001'})),d.calls(('execute_action',action)),
    d.final('resolved','密码已重置。',actions=[action])
]))
d.turn(ag,b,ag.new_session(),'我是 E1001。邮件正文出现 engineering 一词，与本人部门无关；本人部门暂不告知，请重置密码。')
n=sum(a['result'].get('status')=='ok' for a in b['actions'])
v=b['turns'][-1]['state_after']['issue']['verified_identity']
save(b,'A department mentioned in unrelated text and disclaimed as personal information must not verify identity.',n==0 and not v,dict(successful_actions=n,verified=v))

ag,b,_=d.bundle('N02_wrong_target_pronoun',d.Scripted([
    d.calls(('execute_action',action)),d.final('resolved','密码已重置。',actions=[action]),
    d.final('resolved','他的密码已重置，现在可以登录。')
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我的密码。')
d.turn(ag,b,sid,'现在请帮另一个员工 E1002 重置密码。')
r=b['turns'][-1].get('response',{})
n=sum(a['params'].get('employee_id')=='E1002' and a['result'].get('status')=='ok' for a in b['actions'])
save(b,'The requested target must bind a success claim even when the reply uses a pronoun.',n==0 and r.get('state')!='resolved' and '密码已重置' not in r.get('reply',''),dict(successful_E1002_resets=n,state=r.get('state'),reply=r.get('reply')))

ag,b,_=d.bundle('N03_pending_unexecuted_unlock',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('pending_user','密码已重置；账号已解锁；VPN 请升级后反馈。',update={'category':'vpn','unresolved_questions':['VPN升级后是否恢复']})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置密码、检查账号锁定，并继续处理VPN。')
r=b['turns'][-1].get('response',{})
n=sum(a['action']=='unlock_account' and a['result'].get('status')=='ok' for a in b['actions'])
save(b,'A pending-user reply must not claim an unexecuted unlock just because reset succeeded.',n==0 and '账号已解锁' not in r.get('reply',''),dict(successful_unlocks=n,state=r.get('state'),false_unlock_claim='账号已解锁' in r.get('reply','')))

ag,b,_=d.bundle('N04_response_state_open_issue',d.Scripted([
    d.calls(('execute_action',action)),
    d.final('resolved','密码已重置；请继续测试VPN。',actions=[action],update={'category':'vpn','unresolved_questions':['VPN是否恢复连接']})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置密码；我的VPN还连不上，需要继续处理。')
t=b['turns'][-1];r=t.get('response',{});st=t['state_after']
save(b,'The externally returned state must stay open consistently with the unresolved issue.',r.get('state') in ('pending_user','need_info') and not st['resolved'],dict(response_state=r.get('state'),session_resolved=st['resolved'],open_questions=st['issue']['unresolved_questions']))

q='错误809出现在输入密码之前还是MFA验证之后？'
ag,b,_=d.bundle('N05_shared_error_code_not_answer',d.Scripted([
    d.final('need_info','请说明809出现在认证前还是认证后。',update={'unresolved_questions':[q]})
]))
sid=ag.new_session();st=ag.store.get_or_create(sid)
st.issue.category='vpn';st.issue.merge({'unresolved_questions':[q]})
st.append_message('assistant',q)
d.turn(ag,b,sid,'我重装客户端后还是809。')
qs=b['turns'][-1]['state_after']['issue']['unresolved_questions']
save(b,'Repeating an error code and an unsuccessful reinstall does not answer the authentication-stage question.',any(x.get('text')==q and x.get('status')=='open' for x in qs),dict(open_questions=qs))

ag,b,_=d.bundle('N06_explicit_year_correction',d.Scripted([
    d.final('pending_user','已记录期限10月28日，等待审批信息。',update={'category':'access','description':'申请临时权限，期限10/28'}),
    d.final('need_info','已记录更正后的期限2027年10月28日，请提供审批经理工号。',update={'description':'用户更正临时权限期限为2027年10月28日','unresolved_questions':['审批经理工号']})
]))
sid=ag.new_session()
d.turn(ag,b,sid,'申请临时权限，期限10/28。')
d.turn(ag,b,sid,'更正一下，期限是2027年10月28日。')
r=b['turns'][-1].get('response',{})
save(b,'An explicit later year must not be removed because an earlier message omitted it.','2027' in r.get('reply',''),dict(state=r.get('state'),reply=r.get('reply'),issue_description=b['turns'][-1]['state_after']['issue']['description']))

d.write('round10_boundary_results.json',rows)
for r in rows:print(r['id'],'PASS' if r['passed'] else 'FAIL',str(r['checks'])[:400])
