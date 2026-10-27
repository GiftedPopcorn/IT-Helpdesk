"""Unweighted follow-up probes of revised guards; no application files changed."""
import importlib.util
import os
from pathlib import Path
OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('d', OUT / 'execution_driver.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED') == '1':
    d.OUT = OUT / 'pinned'
results = []

def save(b, expected, passed, checks):
    b.update(supplemental_only=True, expected=expected, component_checks=checks)
    d.write('supplemental/' + b['key'] + '.json', b)
    results.append(dict(id=b['key'], expected=expected, passed=passed, checks=checks))

action = {'action': 'reset_password', 'params': {'employee_id': 'E1001'}}
ag,b,_ = d.bundle('D01_colleague_only_department', d.Scripted([
    d.calls(('lookup_user', {'query':'E1001'})),
    d.calls(('execute_action', action)),
    d.final('resolved', '重置完成。', actions=[action])
]))
d.turn(ag,b,ag.new_session(),'我是E1001，我暂不提供自己的部门；同事在engineering部门，请重置我本人的密码。')
n = sum(a['result'].get('status') == 'ok' for a in b['actions'])
save(b,'A colleague-only department claim must not authenticate the speaker.',n == 0,
     dict(successful_actions=n, verified=b['turns'][-1]['state_after']['issue']['verified_identity']))

ag,b,_ = d.bundle('D02_split_identity', d.Scripted([
    d.calls(('lookup_user', {'query':'E1001'})),
    d.final('need_info','请提供你的部门。'),
    d.calls(('execute_action', action)),
    d.final('resolved','密码已重置。',actions=[action])
]))
sid=ag.new_session()
d.turn(ag,b,sid,'我工号E1001，忘记密码了，请帮我重置。')
d.turn(ag,b,sid,'我的部门是engineering。')
n = sum(a['result'].get('status') == 'ok' for a in b['actions'])
save(b,'An unambiguous first-person employee ID and department supplied across two turns should verify.',n == 1,
     dict(successful_actions=n, returned_state=b['turns'][-1].get('response',{}).get('state')))

ag,b,_ = d.bundle('D03_nested_hypothesis_evidence',d.Scripted([
    d.final('need_info','请提供更多信息。',update={'hypotheses':[{'text':'客户端问题','status':'testing','evidence':42}]})
]))
d.turn(ag,b,ag.new_session(),'VPN连接失败。')
exc=b['turns'][-1].get('exception')
save(b,'A non-list hypothesis evidence field must safely fail validation without crashing.',not exc,dict(exception=exc))

ag,b,_ = d.bundle('D04_question_not_observation',d.Scripted([
    d.final('pending_user','请查看客户端连接节点后反馈。')
]))
sid=ag.new_session(); st=ag.store.get_or_create(sid)
st.issue.merge({'conflicts':['当前实际网关 SH-01/HZ-01 尚未确认']})
d.turn(ag,b,sid,'现在能否帮我确认连接的是SH-01还是HZ-01？')
save(b,'A question asking which gateway is in use is not a resolving observation.',bool(st.issue.active_conflicts),
     dict(active_conflicts=st.issue.active_conflicts,returned_state=b['turns'][-1].get('response',{}).get('state')))

ag,b,_ = d.bundle('D05_no_action_success_claim',d.Scripted([
    d.final('resolved','密码已重置，可以使用新密码登录。')
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置我本人的密码。')
r=b['turns'][-1].get('response',{})
n=sum(a['result'].get('status')=='ok' for a in b['actions'])
save(b,'No successful action evidence means a password reset must not be reported as complete.',n>0 or (r.get('state')!='resolved' and '密码已重置' not in r.get('reply','')),
     dict(successful_actions=n,returned_state=r.get('state'),reply=r.get('reply')))

grant={'action':'grant_standard_software','params':{'employee_id':'E1001','software':'grafana_readonly'}}
ag,b,_=d.bundle('D06_partial_success_with_pending_approval',d.Scripted([
    d.calls(('execute_action',grant)),
    d.final('escalated','Grafana只读权限已开通；Snowflake生产库仍需人工审批，已转人工。',
            actions=[grant],update={'category':'access','description':'申请Grafana只读和Snowflake生产库',
            'unresolved_questions':['Snowflake生产库权限待人工审批']},
            escalation={'reason':'policy_forbidden','urgency':'medium','summary':'Snowflake生产库需人工审批'})
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'工号E1001，engineering部门。请开通Grafana只读和Snowflake生产库权限。')
r=b['turns'][-1].get('response',{})
save(b,'A successful standard grant must preserve the separate production approval handoff.',
     r.get('state')=='escalated' and bool(r.get('escalation')) and 'Snowflake' in r.get('reply',''),
     dict(returned_state=r.get('state'),reply=r.get('reply'),escalation=r.get('escalation'),
          successful_actions=sum(a['result'].get('status')=='ok' for a in b['actions'])))

d.write('round5_boundary_results.json',results)
for r in results:
    print(r['id'], 'PASS' if r['passed'] else 'FAIL', str(r['checks'])[:500])
