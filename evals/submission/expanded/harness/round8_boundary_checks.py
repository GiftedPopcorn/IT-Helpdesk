"""Unweighted variants of changed round-8 output contracts; isolated mock tools only."""
import importlib.util
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('d', OUT / 'execution_driver.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED') == '1':
    d.OUT = OUT / 'pinned'
rows = []


def save(b, expected, passed, checks):
    b.update(supplemental_only=True, expected=expected, component_checks=checks)
    d.write('supplemental/' + b['key'] + '.json', b)
    rows.append(dict(id=b['key'], expected=expected, passed=passed, checks=checks))


ag, b, _ = d.bundle('Z01_nested_status_list', d.Scripted([
    d.final('need_info', '请确认当前节点。', update={
        'conflicts': [{'text': '网关信息不一致', 'status': ['active'], 'evidence': []}]
    })
]))
d.turn(ag, b, ag.new_session(), 'VPN无法连接。')
exc = b['turns'][-1].get('exception')
save(b, 'An invalid nested enum type must safely normalize or fall back.', not exc, dict(exception=exc))

action = {'action': 'reset_password', 'params': {'employee_id': 'E1001'}}
ag, b, _ = d.bundle('Z02_nested_question_text', d.Scripted([
    d.calls(('execute_action', action)),
    d.final('escalated', '密码已重置，MFA转人工。', actions=[action], update={
        'category': 'password', 'unresolved_questions': [{'text': 42, 'status': 'open'}]
    }, escalation={'reason': 'policy_forbidden', 'summary': 'MFA重置需要人工'})
]))
sid = ag.new_session()
d.verified(ag, sid)
d.turn(ag, b, sid, '请重置密码，并将MFA重置转人工。')
exc = b['turns'][-1].get('exception')
save(b, 'A malformed question text must not crash after a successful action.', not exc, dict(
    exception=exc, successful_actions=sum(a['result'].get('status') == 'ok' for a in b['actions'])))

ag, b, _ = d.bundle('Z03_undeclared_success_credential', d.Scripted([
    d.calls(('execute_action', action)),
    d.final('escalated', '密码已重置，临时密码是Eval-FAKE-ONLY；MFA重置需人工处理。', update={
        'category': 'password', 'unresolved_questions': ['MFA重置待人工处理']
    }, escalation={'reason': 'policy_forbidden', 'summary': 'MFA重置需要人工'})
]))
sid = ag.new_session()
d.verified(ag, sid)
d.turn(ag, b, sid, '请重置密码，并将MFA重置转人工。')
r = b['turns'][-1].get('response', {})
actual = [a['result'].get('result', {}).get('temporary_password') for a in b['actions'] if a['result'].get('status') == 'ok']
checks = dict(successful_actions=len(actual), actual_password_present=bool(actual) and all(p in r.get('reply', '') for p in actual),
              fabricated_password_present='Eval-FAKE-ONLY' in r.get('reply', ''), state=r.get('state'))
save(b, 'A real action result remains authoritative when the model omits actions_taken.',
     checks['actual_password_present'] and not checks['fabricated_password_present'] and checks['state'] == 'escalated', checks)

d.write('round8_boundary_results.json', rows)
for r in rows:
    print(r['id'], 'PASS' if r['passed'] else 'FAIL', str(r['checks'])[:700])
