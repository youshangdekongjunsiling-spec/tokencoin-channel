import datetime,json,sys,tempfile,threading,time,unittest,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import channel,weekly_budget


def fixture_profile():
    return {"profile_schema_version":1,"label":"experimental_estimate","billing_status":"not_billing",
        "calibration_scope_hash":"c"*64,"validated_until":time.time()+3600,
        "stratum":{"model":"test-model","reasoning_effort":"low","service_tier":"default"},
        "coefficients":dict(zip(weekly_budget.COEFFICIENT_KEYS,(1000.0,200.0,500.0))),
        "validation":{"mae_percentage_points":.05,"max_error_percentage_points":.1},
        "sample_counts":{"training":8,"validation":4},"limitations":["Synthetic test fixture, not account calibration"]}


class BudgetGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)/"supplier"
        self.calls=[];self.missing_usage=False;outer=self
        class Upstream(channel.http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));outer.calls.append(body)
                response={"model":"test-model","service_tier":"default"}
                if not outer.missing_usage:response['usage']={"input_tokens":1000,"output_tokens":100,"total_tokens":1100,"input_tokens_details":{"cached_tokens":200},"output_tokens_details":{"reasoning_tokens":20}}
                event={"type":"response.completed","response":response};raw=('data: '+json.dumps(event)+'\n\n').encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.servers=[]
        self.up=channel.http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream);self.start(self.up)
        channel.initialize(self.home,'Supplier',model='test-model',backend='subscription-adapter',upstream=self.url(self.up)+'/v1',key_env='TC_SYNTHETIC_KEY')
        with channel.db(self.home) as c:weekly_budget.install_profile(c,fixture_profile())
        self.gateway=channel.Gateway(('127.0.0.1',0),self.home,upstream_key='synthetic');self.start(self.gateway)
        self.peers=[self.invite('alice'),self.invite('bob')]
    def start(self,server):
        t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True);t.start();self.servers.append((server,t))
    def url(self,server):return 'http://127.0.0.1:'+str(server.server_port)
    def invite(self,name):
        path=Path(self.temp.name)/(name+'.json');channel.invite(self.home,self.url(self.gateway),path,label=name,requests=20,weekly_percent=.95)
        return json.loads(path.read_text(encoding='utf-8'))
    def tearDown(self):
        for server,t in reversed(self.servers):server.shutdown();server.server_close();t.join(3)
        self.temp.cleanup()
    def ask(self,peer=None,effort='low',hint='task-one'):
        peer=peer or self.peers[0]
        body={'model':'test-model','stream':True,'input':'synthetic','reasoning':{'effort':effort}}
        q=urllib.request.Request(peer['base_url']+'/v1/responses',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+peer['token'],'Content-Type':'application/json','Thread-Id':hint})
        try:
            with channel.OPENER.open(q,timeout=5) as r:code,data=r.status,r.read()
        except urllib.error.HTTPError as e:code,data=e.code,e.read();e.close()
        for _ in range(100):
            if not any(x['status']=='running' for x in channel.local_status(self.home)['requests']):break
            time.sleep(.01)
        return code,data
    def budget(self,peer=None):
        peer=peer or self.peers[0]
        with channel.db(self.home) as c:return weekly_budget.summary(c,peer['channel_id'])
    def test_percentage_gate_and_friends_are_independent(self):
        self.assertEqual(self.ask()[0],200)
        self.assertEqual(self.calls[0]['service_tier'],'default')
        budget=self.budget();self.assertAlmostEqual(budget['estimated_used_percent'],.89);self.assertAlmostEqual(budget['reference_high_percent'],.99)
        self.assertEqual(self.ask()[0],429);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.budget(self.peers[1])['estimated_used_percent'],0)
        self.assertEqual(self.ask(self.peers[1])[0],200);self.assertEqual(len(self.calls),2)
        # An owner's separate direct task is not part of either friend channel.
        with channel.db(self.home) as c:
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',('owner-local','owner-unshared','owner','completed_upstream',time.time(),time.time(),json.dumps({'input_tokens':999999999})))
        self.assertAlmostEqual(self.budget()['estimated_used_percent'],.89)
    def test_wrong_effort_does_not_dispatch_or_decrement(self):
        self.assertEqual(self.ask(effort='high')[0],429);self.assertEqual(self.calls,[])
        with channel.db(self.home) as c:self.assertEqual(c.execute('SELECT remaining FROM channels WHERE id=?',(self.peers[0]['channel_id'],)).fetchone()[0],20)
    def test_missing_usage_is_unknown_and_stops_only_that_friend(self):
        self.missing_usage=True;self.assertEqual(self.ask()[0],200)
        budget=self.budget();self.assertEqual(budget['blocked_reason'],'pending_unknown');self.assertIsNone(budget['estimated_used_percent']);self.assertIsNone(budget['reference_high_percent'])
        self.assertEqual(self.ask()[0],429)
        self.missing_usage=False;self.assertEqual(self.ask(self.peers[1])[0],200)
    def test_official_correction_replaces_and_does_not_double_charge(self):
        self.assertEqual(self.ask()[0],200)
        with channel.db(self.home) as c:task=c.execute('SELECT upstream_task_id FROM task_attribution_requests').fetchone()[0]
        report={'calibration_scope_hash':'c'*64,'data_as_of':datetime.datetime.now(datetime.timezone.utc).isoformat(),'threads':[{'thread_id':task,'data_status':'available','usage_source':'included_plan','weekly_limit_percent':.4}]}
        for _ in range(2):
            with channel.db(self.home) as c:c.execute('BEGIN IMMEDIATE');weekly_budget.reconcile(c,self.peers[0]['channel_id'],report)
        self.assertAlmostEqual(self.budget()['estimated_used_percent'],.4)
        self.assertAlmostEqual(self.budget()['official_used_percent'],.4)
        self.assertEqual(self.ask(hint='new-independent-task')[0],200)
        self.assertAlmostEqual(self.budget()['estimated_used_percent'],1.29)
        self.assertEqual(self.ask(hint='third-task')[0],429)
    def test_expired_or_missing_profile_invite_rolls_back(self):
        for mode in ('missing','expired'):
            with channel.db(self.home) as c:
                c.execute('DELETE FROM weekly_budget_state')
                if mode=='expired':
                    value=fixture_profile();value['validated_until']=time.time()+1
                    weekly_budget.install_profile(c,value)
                    c.execute("UPDATE weekly_budget_profiles SET profile_json=json_set(profile_json,'$.validated_until',0)")
            path=Path(self.temp.name)/(mode+'.json')
            with self.assertRaises(weekly_budget.BudgetError):channel.invite(self.home,self.url(self.gateway),path,label=mode,weekly_percent=20)
            self.assertFalse(path.exists())
            with channel.db(self.home) as c:self.assertEqual(c.execute('SELECT count(*) FROM channels').fetchone()[0],2)

if __name__=='__main__':unittest.main()
