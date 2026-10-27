"""Additional unweighted checks for the latest changes; all backends are mock."""
import importlib.util
import json
import os
from pathlib import Path
OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('d',OUT/'execution_driver.py')
d=importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get('ASSESSMENT_PINNED')=='1':
    d.OUT=OUT/'pinned'
results=[]
def save(b, expected, passed):
    b['supplemental_only']=True
    b['expected']=expected
    d.write('supplemental/'+b['key']+'.json',b)
    results.append({'id':b['key'],'expected':expected,'passed':passed,'checks':b['component_checks']})

ag,b,_=d.bundle('C01_nested_action_params',d.Scripted([
    d.final('resolved','密码已重置。',actions=[{'action':'reset_password','params':'invalid-string'}])
]))
sid=ag.new_session();d.verified(ag,sid)
d.turn(ag,b,sid,'请重置本人密码。')
b['component_checks']={'exception':b['turns'][-1].get('exception'),'successful_actions':sum(a['result'].get('status')=='ok' for a in b['actions'])}
save(b,'Malformed nested params must not crash or execute an action.',not b['component_checks']['exception'] and b['component_checks']['successful_actions']==0)

ag,b,_=d.bundle('C02_unresolved_conflict',d.Scripted([
    d.final('pending_user','请查看客户端连接节点后反馈。')
]))
sid=ag.new_session();st=ag.store.get_or_create(sid)
st.issue.merge({'conflicts':['当前实际网关 SH-01/HZ-01 尚未确认']})
d.turn(ag,b,sid,'现在仍无法确认实际连接的是 SH-01 还是 HZ-01，请继续核查。')
b['component_checks']={'active_conflicts':st.issue.active_conflicts,'returned_state':b['turns'][-1].get('response',{}).get('state')}
save(b,'Explicit uncertainty must not resolve the gateway conflict.',bool(st.issue.active_conflicts))

ag,b,_=d.bundle('C03_other_person_department',d.Scripted([
    d.calls(('lookup_user',{'query':'E1001'})),
    d.calls(('execute_action',{'action':'reset_password','params':{'employee_id':'E1001'}})),
    d.final('resolved','重置完成。',actions=[{'action':'reset_password','params':{'employee_id':'E1001'}}])
]))
d.turn(ag,b,ag.new_session(),'我是E1001，我的部门是sales；同事在engineering部门，请重置我本人的密码。')
b['component_checks']={'successful_actions':sum(a['result'].get('status')=='ok' for a in b['actions']),'identity_verified':b['turns'][-1]['state_after']['issue']['verified_identity']}
save(b,'A department attributed to a colleague must not verify the claimant.',b['component_checks']['successful_actions']==0 and not b['component_checks']['identity_verified'])
d.write('new_boundary_results.json',results)
print(json.dumps(results,ensure_ascii=False,indent=2))
