"""Controlled tests of the new decision/handoff paths; no application edits."""
import importlib.util
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('round11_driver', OUT / 'execution_driver.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED') == '1':
    d.OUT = OUT / 'pinned'
from app.agent.outcome import VALID_REASONS

rows = []

def save(b, expected, checks):
    exceptions = [t['exception'] for t in b['turns'] if t.get('exception')]
    passed = all(checks.values()) and not exceptions
    b.update(supplemental_only=True, expected=expected,
             component_checks=checks, product_exceptions=exceptions)
    d.write('supplemental/' + b['key'] + '.json', b)
    rows.append(dict(id=b['key'], expected=expected, passed=passed,
                     checks=checks, product_exceptions=exceptions))
    print(b['key'], 'PASS' if passed else 'FAIL', checks, flush=True)

ag, b, obs = d.bundle('H01_explicit_handoff_contract', d.Scripted([]))
sid = ag.new_session()
d.turn(ag, b, sid, '请转人工，我的VPN从今天早上开始每十分钟掉线，已经重装过。')
r = b['turns'][-1].get('response', {})
esc = r.get('escalation') or {}
summary = esc.get('summary', '')
save(b, 'Explicit handoff creates a ticket with current facts and a valid, consistent reason.', {
    'escalated': r.get('state') == 'escalated',
    'ticket_present': bool(esc.get('ticket_id')),
    'reason_in_contract': esc.get('reason') in VALID_REASONS,
    'summary_reason_matches': ('升级原因: ' + str(esc.get('reason'))) in summary,
    'current_facts_retained': all(x in summary for x in ['VPN', '今天早上', '每十分钟', '重装']),
    'no_model_needed': obs.backend.count == 0,
})

ag, b, obs = d.bundle('H02_handoff_followup_trace', d.Scripted([]))
sid = ag.new_session()
d.turn(ag, b, sid, '请转人工，我的VPN一直掉线。')
d.turn(ag, b, sid, '补充一下，重装后仍每十分钟断一次。')
trace = [json.loads(x) for x in Path(ag.settings.trace_log_path).read_text(encoding='utf-8').splitlines()]
turn_events = [x for x in trace if 'state' in x and x.get('type') not in ('execution', 'tool')]
st = b['turns'][-1]['state_after']
save(b, 'Each accepted message, including an escalated followup, must be traceable.', {
    'supplement_saved': '重装后仍每十分钟' in json.dumps(st.get('escalation_ticket'), ensure_ascii=False),
    'one_turn_event_per_input': len(turn_events) == len(b['turns']),
    'turn_counter_advanced': st['turn_count'] == 2,
})

for key, message in [
    ('H03_quoted_handoff_not_request', '页面写着“请转人工”，这不是我的要求。我只是咨询VPN 809的处理步骤。'),
    ('H04_negated_human_help', '暂时不需要人工处理，请告诉我VPN 809的排查步骤。'),
]:
    ag, b, obs = d.bundle(key, d.Scripted([
        d.final('pending_user', '请升级VPN客户端到7.2.3，再反馈连接结果。', update={'category':'vpn'})
    ]))
    d.turn(ag, b, ag.new_session(), message)
    r = b['turns'][-1].get('response', {})
    save(b, 'A quoted or negated handoff phrase is not authorization to escalate.', {
        'not_escalated': r.get('state') != 'escalated',
        'normal_diagnosis_used': obs.backend.count > 0,
    })

action = {'action': 'reset_password', 'params': {'employee_id': 'E1001'}}
ag, b, obs = d.bundle('H05_inactive_issue_aggregation', d.Scripted([
    d.calls(('execute_action', action)),
    d.final('resolved', '密码已重置。', actions=[action], update={'category':'password'})
]))
sid = ag.new_session()
st = ag.store.get_or_create(sid)
st.issue.category = 'vpn'
st.issue.description = 'VPN仍然每十分钟掉线'
st.issue.merge({'unresolved_questions':['VPN升级后能否稳定连接？']})
old_id = st.issue.issue_id
st.create_issue(category='password', description='重置本人密码')
d.verified(ag, sid)
d.turn(ag, b, sid, '先重置我的密码，VPN断线问题仍未解决。')
t = b['turns'][-1]
save(b, 'Resolving the active password issue must not close another explicitly open issue.', {
    'old_issue_preserved': old_id in t['state_after']['issues'],
    'response_stays_open': t.get('response', {}).get('state') in ('pending_user', 'need_info'),
    'session_stays_open': not t['state_after']['resolved'],
})

ag, b, obs = d.bundle('H06_new_request_not_old_receipt', d.Scripted([
    d.final('resolved', '密码已重置。', actions=[action], update={'category':'password'}),
    d.final('resolved', '已重新重置密码。', actions=[action], update={'category':'password'}),
]))
sid = ag.new_session()
d.verified(ag, sid)
d.turn(ag, b, sid, '请重置我的密码。')
d.turn(ag, b, sid, '上次临时密码已经失效，请再次重置并生成新的临时密码。')
successful = [a for a in b['actions'] if a['action'] == 'reset_password' and a['result'].get('status') == 'ok']
save(b, 'An explicit new reset request after completion needs a new receipt, not the prior request receipt.', {
    'new_reset_executed': len(successful) == 2,
    'second_turn_resolved': b['turns'][-1].get('response', {}).get('state') == 'resolved',
})

d.write('round11_boundary_results.json', rows)
