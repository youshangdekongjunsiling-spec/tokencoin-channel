"""Temporary fail-closed official weekly-reserve guard for subscription suppliers."""
from __future__ import annotations
import contextlib,hashlib,json,math,socket,sqlite3,threading,time,urllib.error,urllib.request
from pathlib import Path

class ReserveError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS official_reserve_config(
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), floor_percent REAL NOT NULL,
      scope_hash TEXT NOT NULL, plan_type TEXT NOT NULL, weekly_reset REAL NOT NULL,
      valid_until REAL NOT NULL, checked_at REAL, used_percent REAL,
      remaining_percent REAL, blocked_reason TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS official_reserve_grants(
      channel_id TEXT PRIMARY KEY REFERENCES channels(id))''')

def configure(c,floor,snapshot,now=None):
    initialize(c);now=time.time() if now is None else now
    if not isinstance(floor,(int,float)) or isinstance(floor,bool) or not math.isfinite(floor) or not 1<=floor<=99:raise ReserveError('invalid_reserve_floor')
    valid=min(now+86400,snapshot['weekly_reset'])
    if valid<=now:raise ReserveError('official_window_expired')
    existing=c.execute('SELECT * FROM official_reserve_config WHERE singleton=1').fetchone()
    if existing and (existing['scope_hash']!=snapshot['scope_hash'] or existing['weekly_reset']!=snapshot['weekly_reset']):
        raise ReserveError('official_scope_or_window_changed')
    if existing:
        if float(existing['floor_percent'])!=float(floor):raise ReserveError('official_reserve_floor_frozen')
        if existing['valid_until']<=now:raise ReserveError('official_reserve_expired')
        valid=existing['valid_until']
    reason='reserve_floor_reached' if snapshot['remaining_percent']<=floor else None
    c.execute('''INSERT OR REPLACE INTO official_reserve_config VALUES(1,?,?,?,?,?,?,?,?,?)''',
      (float(floor),snapshot['scope_hash'],snapshot['plan_type'],snapshot['weekly_reset'],valid,
       snapshot['checked_at'],snapshot['used_percent'],snapshot['remaining_percent'],reason))
    return summary(c)

def create_grant(c,channel_id):
    initialize(c)
    if not c.execute('SELECT 1 FROM official_reserve_config WHERE singleton=1').fetchone():raise ReserveError('official_reserve_not_configured')
    if not c.execute('SELECT 1 FROM channels WHERE id=?',(channel_id,)).fetchone():raise ReserveError('channel_not_found')
    c.execute('INSERT INTO official_reserve_grants(channel_id) VALUES(?)',(channel_id,))
    return summary(c,channel_id)

def is_granted(c,channel_id):
    initialize(c);return c.execute('SELECT 1 FROM official_reserve_grants WHERE channel_id=?',(channel_id,)).fetchone() is not None

def summary(c,channel_id=None):
    initialize(c);row=c.execute('SELECT * FROM official_reserve_config WHERE singleton=1').fetchone()
    if not row or channel_id is not None and not is_granted(c,channel_id):return None
    return {'mode':'official_weekly_reserve','floor_percent':row['floor_percent'],
      'used_percent':row['used_percent'],'remaining_percent':row['remaining_percent'],
      'weekly_reset':row['weekly_reset'],'valid_until':row['valid_until'],'checked_at':row['checked_at'],
      'blocked_reason':row['blocked_reason'],'hard_boundary':False,
      'warning':'Official data may be delayed; accepted requests can consume beyond the displayed floor.'}

class OfficialUsageReader:
    def __init__(self,port,management_secret,opener=None):
        self.base='http://127.0.0.1:%d/v0/management/'%port;self.secret=management_secret
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*_):return None
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect)
    def _call(self,path,body=None):
        req=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),
          headers={'Authorization':'Bearer '+self.secret,'Content-Type':'application/json'})
        with self.opener.open(req,timeout=4) as r:return r.status,json.loads(r.read(2*1024*1024))
    @staticmethod
    def _window(rate):
        candidates=[]
        def walk(value):
            if isinstance(value,dict):
                duration=value.get('limit_window_seconds') or value.get('window_seconds') or value.get('window_duration_mins',0)*60 or value.get('windowDurationMins',0)*60
                if duration:candidates.append((duration,value))
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(rate)
        matches=[x for x in candidates if x[0]==604800]
        if len(matches)!=1:raise ReserveError('official_weekly_window_invalid')
        return matches[0][1]
    def read(self):
        code,meta=self._call('auth-files');rows=[x for x in meta.get('files',[]) if x.get('provider')=='codex'] if code==200 else []
        if len(rows)!=1:raise ReserveError('official_account_ambiguous')
        claim=rows[0].get('id_token');account=claim.get('chatgpt_account_id') if isinstance(claim,dict) else None
        if not isinstance(account,str) or not account or len(account)>100:raise ReserveError('official_account_missing')
        inner={'auth_index':rows[0]['auth_index'],'method':'GET','url':'https://chatgpt.com/backend-api/wham/usage',
          'header':{'Authorization':'Bearer $TOKEN$','Content-Type':'application/json','Chatgpt-Account-Id':account}}
        code,outer=self._call('api-call',inner)
        if code!=200 or outer.get('status_code')!=200:raise ReserveError('official_read_failed')
        try:payload=json.loads(outer.get('body','{}'))
        except ValueError as e:raise ReserveError('official_read_invalid') from e
        if payload.get('account_id')!=account:raise ReserveError('official_account_mismatch')
        rate=payload.get('rate_limit')
        if not isinstance(rate,dict) or rate.get('allowed') is not True or rate.get('limit_reached') is not False:
            raise ReserveError('official_usage_not_allowed')
        plan=payload.get('plan_type');window=self._window(rate)
        used=window.get('used_percent',window.get('usedPercent'));reset=window.get('reset_at',window.get('resets_at',window.get('resetsAt')))
        if not isinstance(plan,str) or not plan or isinstance(used,bool) or not isinstance(used,(int,float)) or not math.isfinite(used) or used<0:
            raise ReserveError('official_usage_invalid')
        if isinstance(reset,str):
            import datetime as dt
            try:reset=dt.datetime.fromisoformat(reset.replace('Z','+00:00')).timestamp()
            except ValueError as e:raise ReserveError('official_reset_invalid') from e
        if isinstance(reset,bool) or not isinstance(reset,(int,float)) or not math.isfinite(reset) or reset<=time.time():raise ReserveError('official_reset_invalid')
        scope=hashlib.sha256((account+'|'+plan+'|604800|'+str(reset)).encode()).hexdigest()
        return {'scope_hash':scope,'plan_type':plan,'weekly_reset':float(reset),'used_percent':float(used),
          'remaining_percent':max(0.0,100.0-float(used)),'checked_at':time.time()}

class ReserveGuard:
    STICKY={'official_scope_or_window_changed','official_reserve_expired','supplier_stopped'}
    def __init__(self,home,reader,floor=30,max_age=5,monitor_interval=2):
        self.home=Path(home);self.reader=reader;self.max_age=max_age;self.interval=monitor_interval
        self.lock=threading.RLock();self.active={};self.closed=False;self.stop_event=threading.Event()
        snap=reader.read()
        with self._db() as c:configure(c,floor,snap)
        self.snapshot=snap;self.thread=threading.Thread(target=self._monitor,daemon=True);self.thread.start()
    @contextlib.contextmanager
    def _db(self):
        c=sqlite3.connect(self.home/'state.sqlite3',timeout=5);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    @staticmethod
    def _abort_one(stream):
        try:
            sock=getattr(getattr(getattr(stream,'fp',None),'raw',None),'_sock',None)
            if sock is not None:sock.shutdown(socket.SHUT_RDWR)
        except Exception:pass
        try:stream.close()
        except Exception:pass
    def _abort(self,streams):
        workers=[]
        for stream in streams:
            worker=threading.Thread(target=self._abort_one,args=(stream,),daemon=True);worker.start();workers.append(worker)
        deadline=time.monotonic()+.25
        for worker in workers:worker.join(max(0,deadline-time.monotonic()))
    def _blocked(self,reason):
        with self._db() as c:
            initialize(c);c.execute('UPDATE official_reserve_config SET blocked_reason=? WHERE singleton=1',(reason,))
        with self.lock:
            self.snapshot=None;streams=list(self.active)
        self._abort(streams)
    def refresh(self):
        try:snap=self.reader.read()
        except Exception:self._blocked('official_read_failed');raise ReserveError('official_reserve_unavailable')
        if not 0<=time.time()-snap['checked_at']<=self.max_age:
            self._blocked('official_data_stale');raise ReserveError('official_data_stale')
        with self._db() as c:
            row=c.execute('SELECT * FROM official_reserve_config WHERE singleton=1').fetchone()
            if not row or snap['scope_hash']!=row['scope_hash'] or snap['weekly_reset']!=row['weekly_reset']:
                mismatch=True
            else:mismatch=False
            if mismatch:pass
            else:
                reason='reserve_floor_reached' if snap['remaining_percent']<=row['floor_percent'] else None
                c.execute('UPDATE official_reserve_config SET checked_at=?,used_percent=?,remaining_percent=?,blocked_reason=? WHERE singleton=1',
                  (snap['checked_at'],snap['used_percent'],snap['remaining_percent'],reason))
        if mismatch:
            self._blocked('official_scope_or_window_changed');raise ReserveError('official_scope_or_window_changed')
        with self.lock:self.snapshot=snap
        if reason:self._blocked(reason);raise ReserveError(reason)
        return snap
    def authorize(self,channel_id):
        with self._db() as c:
            initialize(c);grant=is_granted(c,channel_id);row=c.execute('SELECT * FROM official_reserve_config WHERE singleton=1').fetchone()
            ch=c.execute('SELECT revoked,expires FROM channels WHERE id=?',(channel_id,)).fetchone()
        if not grant:return
        now=time.time()
        if not ch or ch['revoked'] or ch['expires']<=now:raise ReserveError('channel_invalid_or_expired')
        if not row or row['valid_until']<=now:self._blocked('official_reserve_expired');raise ReserveError('official_reserve_expired')
        if row['blocked_reason'] in self.STICKY:raise ReserveError(row['blocked_reason'])
        with self.lock:snapshot=self.snapshot
        if row['blocked_reason'] or not snapshot or now-snapshot['checked_at']>self.max_age:snapshot=self.refresh()
        if snapshot['remaining_percent']<=row['floor_percent']:raise ReserveError('reserve_floor_reached')
    def register_stream(self,stream,channel_id):
        with self.lock:
            with self._db() as c:
                row=c.execute('SELECT blocked_reason,valid_until FROM official_reserve_config WHERE singleton=1').fetchone()
                ch=c.execute('SELECT revoked,expires FROM channels WHERE id=?',(channel_id,)).fetchone()
            now=time.time();global_expired=not row or row['valid_until']<=now
            if global_expired:reason='official_reserve_expired'
            elif not ch or ch['revoked'] or ch['expires']<=now:reason='channel_invalid_or_expired'
            else:reason=row['blocked_reason']
            if reason is None:self.active[stream]=channel_id
        if global_expired:self._blocked(reason)
        if reason is not None:self._abort([stream]);raise ReserveError(reason)
    def finish_stream(self,stream):
        with self.lock:self.active.pop(stream,None)
    def _monitor(self):
        while not self.stop_event.wait(self.interval):
            with self.lock:active=dict(self.active)
            if not active:continue
            now=time.time()
            with self._db() as c:
                config=c.execute('SELECT valid_until FROM official_reserve_config WHERE singleton=1').fetchone()
                invalid={r['id'] for r in c.execute('SELECT id FROM channels WHERE revoked=1 OR expires<=?',(now,))}
            if not config or config['valid_until']<=now:
                self._blocked('official_reserve_expired');continue
            self._abort([stream for stream,channel_id in active.items() if channel_id in invalid])
            try:self.refresh()
            except ReserveError:pass
    def close(self):
        self.closed=True;self.stop_event.set()
        self._blocked('supplier_stopped')
        self.thread.join(timeout=1)
