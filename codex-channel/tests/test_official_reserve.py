import socket,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import channel,official_reserve

class Reader:
    def __init__(self,remaining=31):self.remaining=remaining;self.fail=False;self.scope='scope';self.reset=time.time()+3600;self.stale=False;self.reads=0
    def read(self):
        self.reads+=1
        if self.fail:raise OSError('offline')
        checked=time.time()-10 if self.stale else time.time()
        return {'scope_hash':self.scope,'plan_type':'prolite','weekly_reset':self.reset,
          'used_percent':100-self.remaining,'remaining_percent':self.remaining,'checked_at':checked}

class Stream:
    def __init__(self):self.closed=False
    def close(self):self.closed=True

class ReserveTests(unittest.TestCase):
    def make(self,remaining=31,interval=10):
        td=tempfile.TemporaryDirectory();home=Path(td.name);channel.initialize(home,'n')
        reader=Reader(remaining);guard=official_reserve.ReserveGuard(home,reader,30,monitor_interval=interval)
        token='t';cid='c'
        with channel.db(home) as c:
            c.execute('INSERT INTO channels VALUES(?,?,?,?,?,0)',(cid,'f',channel.secret_hash(token),time.time()+300,1000))
            official_reserve.create_grant(c,cid)
        return td,home,reader,guard,cid

    def test_remaining_31_allows_30_and_29_refuse(self):
        for remaining,allowed in ((31,True),(30,False),(29,False)):
            td,home,r,g,cid=self.make(remaining)
            try:
                if allowed:g.authorize(cid)
                else:
                    with self.assertRaisesRegex(official_reserve.ReserveError,'reserve_floor_reached'):g.authorize(cid)
            finally:g.close();td.cleanup()

    def test_read_error_stale_and_cross_window_fail_closed(self):
        for mutation,code in ((lambda r:setattr(r,'fail',True),'official_reserve_unavailable'),
                              (lambda r:setattr(r,'stale',True),'official_data_stale'),
                              (lambda r:setattr(r,'scope','next-window'),'official_scope_or_window_changed')):
            td,home,r,g,cid=self.make();mutation(r);g.snapshot['checked_at']=0
            try:
                with self.assertRaisesRegex(official_reserve.ReserveError,code):g.authorize(cid)
            finally:g.close();td.cleanup()

    def test_failed_read_invalidates_fresh_snapshot_and_requires_success(self):
        td,home,r,g,cid=self.make();last=g.snapshot['checked_at'];r.fail=True
        try:
            with self.assertRaises(official_reserve.ReserveError):g.refresh()
            self.assertIsNone(g.snapshot)
            with channel.db(home) as c:self.assertEqual(official_reserve.summary(c,cid)['checked_at'],last)
            with self.assertRaisesRegex(official_reserve.ReserveError,'official_reserve_unavailable'):g.authorize(cid)
            r.fail=False;before=r.reads;g.authorize(cid);self.assertGreater(r.reads,before)
        finally:g.close();td.cleanup()

    def test_revocation_closes_active_stream(self):
        td,home,r,g,cid=self.make(interval=.01);s=Stream();g.register_stream(s,cid)
        try:
            channel.revoke(home,cid)
            deadline=time.time()+.5
            while not s.closed and time.time()<deadline:time.sleep(.01)
            self.assertTrue(s.closed)
        finally:g.finish_stream(s);g.close();td.cleanup()

    def test_profileless_grant_summary_and_no_renew(self):
        td,home,r,g,cid=self.make();
        try:
            with channel.db(home) as c:first=official_reserve.summary(c,cid)['valid_until']
            time.sleep(.01)
            with channel.db(home) as c:official_reserve.configure(c,30,r.read());second=official_reserve.summary(c,cid)['valid_until']
            self.assertEqual(first,second)
            with channel.db(home) as c:
                with self.assertRaisesRegex(official_reserve.ReserveError,'floor_frozen'):official_reserve.configure(c,29,r.read())
        finally:g.close();td.cleanup()

    def test_expired_reserve_requires_explicit_bounded_reauthorization(self):
        td,home,r,g,cid=self.make();g.close();now=time.time()
        with channel.db(home) as c:c.execute('UPDATE official_reserve_config SET valid_until=? WHERE singleton=1',(now-1,))
        with channel.db(home) as c:
            with self.assertRaisesRegex(official_reserve.ReserveError,'official_reserve_expired'):
                official_reserve.configure(c,30,r.read(),now=now)
            renewed=official_reserve.configure(c,30,r.read(),now=now,authorized_until=now+300)
        self.assertEqual(renewed['valid_until'],now+300);td.cleanup()

    def test_active_reserve_extension_requires_all_grants_revoked(self):
        td,home,r,g,cid=self.make();g.close();now=time.time()
        with channel.db(home) as c:
            with self.assertRaisesRegex(official_reserve.ReserveError,'official_reserve_active_grants'):
                official_reserve.configure(c,30,r.read(),now=now,authorized_until=now+300)
            c.execute('UPDATE channels SET revoked=1 WHERE id=?',(cid,))
            changed=official_reserve.configure(c,30,r.read(),now=now,authorized_until=now+300)
        self.assertEqual(changed['valid_until'],now+300);td.cleanup()

    def test_monitor_enforces_cutoff_and_real_loopback_abort(self):
        td,home,r,g,cid=self.make(interval=.01)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        client=socket.create_connection(listener.getsockname());server,_=listener.accept();listener.close()
        class Raw:pass
        class FP:pass
        class Response:
            def __init__(self,s):self.fp=FP();self.fp.raw=Raw();self.fp.raw._sock=s
            def close(self):self.fp.raw._sock.close()
        response=Response(client);ended=threading.Event()
        def blocked_read():
            try:client.recv(1)
            except OSError:pass
            ended.set()
        worker=threading.Thread(target=blocked_read);worker.start();g.register_stream(response,cid)
        try:
            with channel.db(home) as c:c.execute('UPDATE official_reserve_config SET valid_until=? WHERE singleton=1',(time.time()-.01,))
            self.assertTrue(ended.wait(.75),'loopback recv was not interrupted')
            with channel.db(home) as c:self.assertEqual(official_reserve.summary(c,cid)['blocked_reason'],'official_reserve_expired')
        finally:g.finish_stream(response);g.close();server.close();worker.join(1);td.cleanup()

    def test_blocking_close_is_bounded_outside_decision_lock(self):
        td,home,r,g,cid=self.make();release=threading.Event()
        class Blocking:
            def close(self):release.wait(5)
        stream=Blocking();g.register_stream(stream,cid)
        try:
            started=time.monotonic();g._blocked('official_read_failed')
            self.assertLess(time.monotonic()-started,.5)
            acquired=g.lock.acquire(timeout=.1);self.assertTrue(acquired)
            if acquired:g.lock.release()
        finally:release.set();g.finish_stream(stream);g.close();td.cleanup()

    def test_reader_accepts_observed_primary_week_schema(self):
        class Raw(official_reserve.OfficialUsageReader):
            def __init__(self):pass
            def _call(self,path,body=None):
                if path=='auth-files':return 200,{'files':[{'provider':'codex','auth_index':1,'id_token':{'chatgpt_account_id':'acct'}}]}
                return 200,{'status_code':200,'body':'{"account_id":"acct","plan_type":"prolite","rate_limit":{"allowed":true,"limit_reached":false,"primary_window":{"used_percent":56,"limit_window_seconds":604800,"reset_at":1999999999},"secondary_window":null}}'}
        snap=Raw().read();self.assertEqual((snap['used_percent'],snap['remaining_percent'],snap['plan_type']),(56,44,'prolite'))
        reader=official_reserve.OfficialUsageReader(1,'secret')
        redirects=[h for h in reader.opener.handlers if isinstance(h,official_reserve.urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirects),1);self.assertIsNone(redirects[0].redirect_request(None,None,None,None,None,None))

if __name__=='__main__':unittest.main()
