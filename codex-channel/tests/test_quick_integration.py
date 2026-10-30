import json, sys, tempfile, time, unittest, urllib.request
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import channel, dashboard, quick_invite, setup_quick_connect

class InvitationTests(unittest.TestCase):
 def value(self):
  return dict(version=1,channel_id="a"*32,supplier="Alice",model="mock",backend="mock",token="x"*43,expires=time.time()+600,base_url="http://127.0.0.1:1",transport={"kind":"iroh","ticket":"synthetic-ticket"})
 def test_roundtrip_and_reject_invalid(self):
  value=self.value();self.assertEqual(quick_invite.decode(quick_invite.encode(value)),value)
  for raw in ["123456",quick_invite.PREFIX+"!!",quick_invite.PREFIX+"a"*20000]:
   with self.assertRaises(ValueError):quick_invite.decode(raw)
  for change in [{"expires":time.time()-1},{"transport":{"kind":"http","ticket":"synthetic-ticket"}},{"token":"short"}]:
   with self.assertRaises(ValueError):quick_invite.encode(dict(value,**change))

class QuickDashboardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  try:setup_quick_connect.load()
  except (OSError,ValueError):raise unittest.SkipTest("Install verified quick connection runtime first")
 def setUp(self):
  import quick_connect
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  constructor=quick_connect.QuickNode
  self.patch=patch.object(quick_connect,"QuickNode",side_effect=lambda **kw:constructor(**kw,relay_mode="disabled"))
  self.patch.start();self.a=dashboard.DashboardState(self.root/"a");self.b=dashboard.DashboardState(self.root/"b")
 def tearDown(self):
  self.b.close();self.a.close();self.patch.stop();self.temp.cleanup()
 def invite(self):
  supplier=self.a.start_supplier(dict(mode="mock",name="Alice",model="tokencoin-demo",host="127.0.0.1",port=0,connection="quick"))
  return self.a.create_invite(dict(base_url=supplier["base_url"],label="Bob",requests=2,minutes=10))
 def test_full_quick_flow_stream_allowance_restart_revoke(self):
  result=self.invite();self.assertTrue(result["connection_string"].startswith(quick_invite.PREFIX))
  self.b.import_peer(dict(name="alice",invitation=result["connection_string"]))
  self.assertEqual(self.b.peer_status("alice")["remaining_requests"],2)
  peer=self.b._ready_peer("alice");self.assertNotEqual(peer["base_url"],result["invitation"]["base_url"])
  req=urllib.request.Request(peer["base_url"]+"/v1/responses",data=channel.dump({"model":peer["model"],"stream":True,"input":"synthetic quick connection test"}).encode(),headers={"Authorization":"Bearer "+peer["token"],"Content-Type":"application/json"})
  with urllib.request.urlopen(req,timeout=10) as r:body=r.read()
  self.assertIn(b"response.output_text.delta",body);self.assertIn(b"response.completed",body)
  self.assertEqual(self.b.peer_status("alice")["remaining_requests"],1)
  self.b.close();self.b=dashboard.DashboardState(self.root/"b")
  self.assertEqual(self.b.peer_status("alice")["remaining_requests"],1)
  self.a.revoke({"channel_id":result["invitation"]["channel_id"]})
  with self.assertRaises(dashboard.ApiError):self.b.peer_status("alice")
  safe=json.dumps(self.b.status());self.assertNotIn(peer["token"],safe);self.assertNotIn(result["invitation"]["transport"]["ticket"],safe)
 def test_untrusted_url_ignored_and_failed_import_cleaned(self):
  result=self.invite();inv=dict(result["invitation"],base_url="http://169.254.169.254:80")
  self.b.import_peer(dict(name="alice",invitation=inv));self.assertTrue(self.b._ready_peer("alice")["base_url"].startswith("http://127.0.0.1:"))
  forged=dict(inv,token="z"*43)
  with self.assertRaises(dashboard.ApiError):self.b.import_peer(dict(name="bad",invitation=forged))
  self.assertFalse(channel.peer_path(self.b.consumer_home,"bad").exists());self.assertNotIn("bad",self.b.quick_peers)

if __name__=="__main__":unittest.main()
