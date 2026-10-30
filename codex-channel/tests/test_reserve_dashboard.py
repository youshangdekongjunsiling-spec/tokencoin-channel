import sys,time,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import channel,dashboard,official_reserve
from test_budget_dashboard import RunningDashboard,FakeProcess

class ReserveDashboardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=RunningDashboard(Path(self.tmp.name));self.home=self.app.subscription_node()
 def tearDown(self):self.app.close();self.tmp.cleanup()
 def payload(self):return {'base_url':'http://127.0.0.1:8781','label':'trial','budget_mode':'official_weekly_reserve','minutes':60}
 def test_reserve_mode_requires_enabled_supplier_and_does_not_create_legacy_invite(self):
  code,result=self.app.request('/api/supplier/invites',self.payload())
  self.assertEqual((code,result['error']['code']),(409,'official_reserve_not_enabled'))
  with channel.db(self.home) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM channels').fetchone()[0],0)
 def test_reserve_invite_requires_owner_auth_and_caps_expiry_without_profile(self):
  self.app.state.settings['supplier']['official_reserve_floor']=30
  until=time.time()+600
  with channel.db(self.home) as c:
   official_reserve.configure(c,30,{'scope_hash':'a'*64,'plan_type':'test','weekly_reset':until,'checked_at':time.time(),'used_percent':56,'remaining_percent':44})
  self.assertEqual(self.app.request('/api/supplier/invites',self.payload(),token=False)[0],401)
  self.assertEqual(self.app.request('/api/supplier/invites',self.payload(),origin=False)[0],403)
  code,x=self.app.request('/api/supplier/invites',self.payload());self.assertEqual(code,200)
  self.assertLessEqual(x['invitation']['expires'],until)
  self.assertEqual(x['metadata']['budget']['mode'],'official_weekly_reserve')
  self.assertEqual(x['metadata']['budget']['remaining_percent'],44)
  with channel.db(self.home) as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM weekly_budget_grants').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT COUNT(*) FROM official_reserve_grants').fetchone()[0],1)
 def test_invalid_reserve_settings_are_rejected_before_launch(self):
  base={'mode':'subscription','name':'test','model':'gpt-6.1-sol','port':8781}
  for value in (True,'30',29,31):
   with self.subTest(value=value),self.assertRaises(dashboard.ApiError):dashboard.DashboardState._supplier_fields({**base,'official_reserve_floor':value})
  with self.assertRaises(dashboard.ApiError):dashboard.DashboardState._supplier_fields({**base,'mode':'mock','official_reserve_floor':30})
 def test_explicit_reserve_flag_reaches_supplier_process(self):
  self.app.state.supplier_process=None
  real_popen=dashboard.subprocess.Popen; launched=[]
  def launch(args,**kwargs):
   if len(args)>1 and str(args[1]).endswith('run_supplier.py'):
    launched.append(args);return FakeProcess()
   return real_popen(args,**kwargs)
  with patch('dashboard.subprocess.Popen',side_effect=launch):
   self.app.state.start_supplier({'mode':'subscription','name':'Owner','model':'gpt-6.1-sol','port':8781,'official_reserve_floor':30})
  self.assertEqual(len(launched),1);args=launched[0]
  self.assertEqual(args[args.index('--official-reserve-floor')+1],'30')
  self.assertEqual(self.app.state.settings['supplier']['official_reserve_floor'],30)

if __name__=='__main__':unittest.main()
