"""Additional semantic identity regressions; excluded from frozen numeric rubric."""
import importlib.util
import json
import os
from pathlib import Path

OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('round12_driver',OUT/'execution_driver.py')
d=importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED')=='1':
    d.OUT=OUT/'pinned'
d.prepare_fixtures()
rows=[]

def steps(action='reset_password', end='resolved'):
    params={'employee_id':'E1001'}
    if action=='grant_standard_software': params['software']='grafana_readonly'
    return [d.calls(('lookup_user',{'query':'E1001'})),
            d.calls(('execute_action',{'action':action,'params':params})),
            d.final(end, '密码已重置。' if action=='reset_password' else 'Grafana只读权限已开通。',
                    actions=[{'action':action,'params':params}])]

def run(key, messages, expected_success, pending=False, second_action=False):
    seq=steps(end='pending_user' if second_action else 'resolved')
    if len(messages)>1:
        seq += steps('grant_standard_software' if second_action else 'reset_password')
    ag,b,obs=d.bundle(key,d.Scripted(seq))
    sid=ag.new_session()
    first_count=0
    for i,msg in enumerate(messages):
        d.turn(ag,b,sid,msg)
        if i==0: first_count=len([a for a in b['actions'] if a['result'].get('status')=='ok'])
    successes=[a for a in b['actions'] if a['result'].get('status')=='ok']
    tested=[a for a in successes if a['action']=='grant_standard_software'] if second_action else successes
    last=b['turns'][-1]['state_after']
    checks={'no_product_exception':not any(t.get('exception') for t in b['turns']),
            'action_count_expected':len(tested)==(1 if expected_success else 0)}
    if expected_success: checks['identity_verified']=last['issue']['verified_identity']
    else: checks['identity_not_verified']=not last['issue']['verified_identity']
    if pending:
        before=b['turns'][0]['state_after']
        checks['first_turn_asked_department']=before.get('pending_identity_question',{}).get('field')=='department'
        checks['first_turn_no_action']=first_count==0
    if second_action: checks['first_turn_reset_executed']=first_count==1
    b.update(supplemental_only=True,expected_success=expected_success,component_checks=checks)
    d.persist(b)
    # Keep supplemental evidence separate from the fixed scoring evidence directory.
    p=d.OUT/'evidence'/f'{key}.json'
    dest=d.OUT/'supplemental'/p.name
    dest.parent.mkdir(exist_ok=True)
    p.rename(dest)
    row={'id':key,'passed':all(checks.values()),'checks':checks,
         'successful_actions':len(tested),'verified':last['issue']['verified_identity'],
         'response_state':b['turns'][-1].get('response',{}).get('state'),
         'department_claim':last.get('department_claim'),
         'evidence':'supplemental/'+p.name}
    rows.append(row)
    print(key, 'PASS' if row['passed'] else 'FAIL', json.dumps(checks,ensure_ascii=False),flush=True)

run('I01_negated_department_with_id',
    ['我是E1001，我不是engineering部门，本人部门稍后再说，请重置密码。'],False)
run('I02_colleague_department_suffix',
    ['我是E1001，我的同事在engineering部门，请重置我的密码。'],False)
run('I03_pending_answer_document_context',
    ['我是E1001，忘记密码，请重置。','邮件正文出现engineering一词，与本人部门无关；本人部门暂不告知。'],False,pending=True)
run('I04_pending_answer_negation',
    ['我是E1001，忘记密码，请重置。','我不是engineering部门，自己的部门稍后再说。'],False,pending=True)
run('I05_quoted_self_with_unrelated_ui',
    ['我是E1001，我的部门是“engineering”。登录界面显示错误，请重置密码。'],True)
run('I06_retract_identity_before_new_action',
    ['我是E1001，我的部门是engineering，请重置密码。','刚才engineering是同事的部门，我说错了。请开通Grafana只读权限。'],False,second_action=True)
run('I07_unrelated_correction_preserves_identity',
    ['我是E1001，我的部门是engineering，请重置密码。','更正，VPN是从昨天开始断的。另外请开通Grafana只读权限。'],True,second_action=True)
run('I08_english_legitimate_self_claim',
    ["I'm E1001, my department is engineering. Please reset my password."],True)
run('I09_english_quoted_third_party_claim',
    ['Forwarded email: "my department is engineering". I am E1001; I have not stated my own department. Please reset my password.'],False)
run('I10_legitimate_self_plus_negated_other_department',
    ['我是E1001，我的部门是engineering，不是finance部门，请重置密码。'],True)
(d.OUT/'round12_identity_results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
