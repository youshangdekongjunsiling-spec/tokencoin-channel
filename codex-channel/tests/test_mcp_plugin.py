import unittest,tempfile,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import mcp_server
class PluginTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.a=mcp_server.Plugin(Path(self.tmp.name)/'a');self.b=mcp_server.Plugin(Path(self.tmp.name)/'b')
 def tearDown(self):
  self.a.close();self.b.close();self.tmp.cleanup()
 def call(self,server,path,body=None):
  result=server.action({'method':'GET' if body is None else 'POST','path':path,'body':body or {}})
  self.assertFalse(result.get('isError'),result)
  return result['_meta']['data']
 def test_entrypoints_and_resource(self):
  tools=self.a.tools();self.assertEqual(tools[0]['_meta']['openai/ui']['entrypoints'],[{'type':'global'},{'type':'thread'}]);self.assertEqual(tools[1]['_meta']['ui']['visibility'],['app'])
  html=self.a.resource()['contents'][0]['text'];self.assertIn('提供我的额度',html);self.assertIn('ui/initialize',html);self.assertNotIn('src="app.js"',html);self.assertNotIn('href="styles.css"',html)
 def test_bidirectional_mock_and_revocation(self):
  for server,name in [(self.a,'Alice'),(self.b,'Bob')]:self.call(server,'/api/supplier/start',{'mode':'mock','name':name,'model':'tokencoin-demo','host':'127.0.0.1','port':0})
  for provider,consumer,name in [(self.a,self.b,'alice'),(self.b,self.a,'bob')]:
   cfg=self.call(provider,'/api/status')['supplier']['configuration']
   reply=provider.action({'method':'POST','path':'/api/supplier/invites','body':{'base_url':'http://127.0.0.1:'+str(cfg['port']),'label':name,'requests':4,'minutes':5}})
   invitation=reply['_meta']['data']['invitation'];self.assertNotIn(invitation['token'],json.dumps(reply['content']))
   self.call(consumer,'/api/peers/import',{'name':name,'invitation':invitation})
   status=self.call(consumer,'/api/peers/'+name+'/status');self.assertEqual(status['remaining_requests'],4)
   public=self.call(consumer,'/api/status');self.assertNotIn(invitation['token'],json.dumps(public))
   self.call(provider,'/api/supplier/revoke',{'channel_id':invitation['channel_id']})
   self.assertTrue(consumer.action({'method':'GET','path':'/api/peers/'+name+'/status'})['isError'])
 def test_second_controller_is_readable_ui_error(self):
  self.call(self.a,'/api/status')
  other=mcp_server.Plugin(Path(self.tmp.name)/'a')
  try:
   self.assertIn('protocolVersion',other.handle('initialize',{}))
   result=other.action({'method':'GET','path':'/api/status'})
   self.assertTrue(result['isError']);self.assertEqual(result['_meta']['data']['error']['code'],'dashboard_already_running')
  finally:other.close()
 def test_utf8_stdio(self):
  import subprocess
  request=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n'
  p=subprocess.run([sys.executable,'-B',str(Path(mcp_server.__file__))],input=request.encode('utf-8'),capture_output=True,timeout=15)
  result=json.loads(p.stdout.decode('utf-8'));self.assertIn('TokenCoin 双向通道',result['result']['tools'][0]['title']);self.assertEqual(p.returncode,0)
 def test_no_arbitrary_http_bridge(self):
  self.assertTrue(self.a.action({'method':'GET','path':'https://example.com'})['isError'])
  self.assertTrue(self.a.action({'method':'GET','path':'/api/peers/../status'})['isError'])
if __name__=='__main__':unittest.main()
