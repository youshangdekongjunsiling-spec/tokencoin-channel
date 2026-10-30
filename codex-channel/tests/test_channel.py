import contextlib,io,json,os,sqlite3,sys,tempfile,threading,time,unittest,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import channel

class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.a=self.root/'alice';self.b=self.root/'bob'
        channel.initialize(self.a,'Alice');channel.initialize(self.b,'Bob')
        self.servers=[];self.sa=self.start(self.a);self.sb=self.start(self.b)
        self.ia=self.root/'a-invite.json';self.ib=self.root/'b-invite.json'
        self.ca=channel.invite(self.a,self.url(self.sa),self.ia,requests=20)
        self.cb=channel.invite(self.b,self.url(self.sb),self.ib,requests=20)
        channel.join(self.b,'alice',self.ia);channel.join(self.a,'bob',self.ib)
        self.pa=channel.peer_read(self.b,'alice');self.pb=channel.peer_read(self.a,'bob')
    def start(self,home):
        s=channel.Gateway(('127.0.0.1',0),home);t=threading.Thread(target=s.serve_forever,kwargs={'poll_interval':0.02},daemon=True);t.start();self.servers.append((s,t));return s
    def url(self,s):return 'http://127.0.0.1:'+str(s.server_port)
    def tearDown(self):
        for s,t in self.servers:s.shutdown();s.server_close();t.join(2)
        self.tmp.cleanup()
    def request(self,peer=None,body=None,headers=None):
        peer=peer or self.pa
        body=body or {'model':'tokencoin-demo','stream':True,'input':'hello'}
        h={'Authorization':'Bearer '+peer['token'],'Content-Type':'application/json'};h.update(headers or {})
        q=urllib.request.Request(peer['base_url']+'/v1/responses',data=json.dumps(body).encode(),headers=h)
        try:
            with channel.OPENER.open(q,timeout=5) as r:return r.status,r.read(),dict(r.headers)
        except urllib.error.HTTPError as e:return e.code,e.read(),dict(e.headers)
    def wait_done(self,home):
        for _ in range(100):
            result=channel.local_status(home)
            if not any(r['status']=='running' for r in result['requests']):return result
            time.sleep(0.01)
        self.fail('request never finished')
    def test_bidirectional_network_stream(self):
        for peer,name,home in [(self.pa,'Alice',self.a),(self.pb,'Bob',self.b)]:
            status,data,h=self.request(peer);self.assertEqual(status,200);self.assertIn(name.encode(),data)
            self.assertIn(b'response.completed',data);self.assertEqual(h['X-TokenCoin-Backend'],'mock')
            state=self.wait_done(home);self.assertEqual(state['channels'][0]['remaining'],19)
            self.assertEqual(state['requests'][0]['status'],'completed_synthetic')
    def test_bad_credential_no_dispatch(self):
        self.assertEqual(self.request(headers={'Authorization':'Bearer invalid'})[0],401)
        self.assertEqual(channel.local_status(self.a)['requests'],[])
    def test_revoke_does_not_affect_reverse_direction(self):
        channel.revoke(self.a,self.ca['channel_id']);self.assertEqual(self.request()[0],401)
        self.assertEqual(self.request(self.pb)[0],200)
    def test_expiry_no_dispatch(self):
        with channel.db(self.a) as c:c.execute('UPDATE channels SET expires=0')
        self.assertEqual(self.request()[0],401);self.assertEqual(channel.local_status(self.a)['requests'],[])
    def test_allowance_and_duplicate(self):
        with channel.db(self.a) as c:c.execute('UPDATE channels SET remaining=1')
        self.assertEqual(self.request(headers={'Idempotency-Key':'one'})[0],200);self.wait_done(self.a)
        self.assertEqual(self.request(headers={'Idempotency-Key':'one'})[0],409)
        self.assertEqual(self.request(headers={'Idempotency-Key':'two'})[0],429)
        self.assertEqual(len(channel.local_status(self.a)['requests']),1)
    def test_concurrency_reserved_before_forward(self):
        request_id=channel.reserve(self.a,self.pa['token'],'held')
        self.assertEqual(self.request()[0],429)
        channel.finish(self.a,request_id,'unknown')
        self.assertEqual(self.request()[0],200)
    def test_foreign_model_and_hosted_tools(self):
        self.assertEqual(self.request(body={'model':'other','stream':True})[0],403)
        self.assertEqual(self.request(body={'model':'tokencoin-demo','stream':True,'tools':[{'type':'web_search'}]})[0],400)
        self.assertEqual(channel.local_status(self.a)['requests'],[])
    def test_invitation_secret_not_in_supplier_db_or_status(self):
        status=channel.dump(channel.local_status(self.a))
        self.assertNotIn(self.pa['token'],status)
        self.assertNotIn(self.pa['token'].encode(),(self.a/'state.sqlite3').read_bytes())
        with self.assertRaises(ValueError):channel.peer_path(self.a,'../../bad')
        with self.assertRaises(ValueError):channel.valid_url('http://example.com')
        with self.assertRaises(ValueError):channel.valid_url('https://key@example.com')
    def test_tool_fixture_roundtrip(self):
        first={'model':'tokencoin-demo','stream':True,'instructions':'TOKENCOIN_DEMO_TOOL_ROUNDTRIP','tools':[{'type':'function','name':'exec_command'}]}
        self.assertIn(b'function_call',self.request(body=first)[1]);self.wait_done(self.a)
        first['input']=[{'type':'function_call_output','call_id':'tc_demo_pwd','output':'test-local-directory'}]
        self.assertIn(b'Local tool result received',self.request(body=first)[1])
    def test_real_forwarder_with_synthetic_upstream(self):
        # Both connector kinds use the same fixed-endpoint Responses transport; no real API called.
        os.environ['TC_TEST_ADAPTER_KEY']=self.pa['token']
        try:
            for kind in ['api','subscription-adapter']:
                with self.subTest(kind=kind):
                    h=self.root/kind;channel.initialize(h,'Forwarder',backend=kind,upstream=self.url(self.sa)+'/v1',key_env='TC_TEST_ADAPTER_KEY')
                    sf=self.start(h);f=self.root/(kind+'-invitation.json');channel.invite(h,self.url(sf),f)
                    peer=json.loads(f.read_text(encoding='utf-8'))
                    status,data,_=self.request(peer);self.assertEqual(status,200);self.assertIn(b'DEMO supplied by',data)
                    state=self.wait_done(h);self.assertEqual(state['requests'][0]['status'],'completed_upstream')
                    self.assertNotIn(self.pa['token'],data.decode());self.assertNotEqual(peer['token'],self.pa['token'])
        finally:del os.environ['TC_TEST_ADAPTER_KEY']

    def test_private_http_permission_cannot_come_from_invitation(self):
        value=dict(self.pa,base_url='http://192.168.1.23:8781',allow_private_http=True)
        path=self.root/'untrusted-invitation.json';path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):channel.join(self.b,'untrusted',path)
        for url in ['http://8.8.8.8','http://public.example.com']:
            with self.assertRaises(ValueError):channel.valid_url(url,True)
    def test_namespace_tools_preserved(self):
        body={'model':'tokencoin-demo','stream':True,'tools':[{'type':'namespace','name':'functions','tools':[{'type':'function','name':'pwd'}]}]}
        self.assertEqual(self.request(body=body)[0],200)
    def test_stream_failure_reports_sanitized_error(self):
        os.environ['TC_TEST_ADAPTER_KEY']=self.pa['token']
        h=self.root/'limit-adapter';channel.initialize(h,'Limited',backend='api',upstream=self.url(self.sa)+'/v1',key_env='TC_TEST_ADAPTER_KEY')
        sf=self.start(h);f=self.root/'limit-invitation.json';channel.invite(h,self.url(sf),f);peer=json.loads(f.read_text(encoding='utf-8'))
        old=channel.MAX_RESPONSE;channel.MAX_RESPONSE=16
        try:
            status,data,_=self.request(peer);self.assertEqual(status,200);self.assertIn(b'response_size_limit',data)
            self.assertNotIn(b'response.completed',data);self.assertNotIn(self.pa['token'].encode(),data)
        finally:channel.MAX_RESPONSE=old;del os.environ['TC_TEST_ADAPTER_KEY']
    @unittest.skipUnless(os.name=='nt','Windows ACL check')
    def test_invitation_acl_is_not_inherited(self):
        acl=channel.subprocess.check_output(['icacls',str(self.ia)],text=True)
        self.assertNotIn('(I)',acl)
        self.assertEqual(acl.count('(F)'),1)

if __name__=='__main__':unittest.main(verbosity=2)
