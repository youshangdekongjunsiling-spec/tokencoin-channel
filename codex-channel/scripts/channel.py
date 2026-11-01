"""Small explicit supplier gateway; credentials never leave their owning process.
Python 3.10+; standard library. Run with --help. Not a billing system.
"""
from __future__ import annotations
import argparse, contextlib, hashlib, http.server, ipaddress, json, os, re, secrets, shutil
import sqlite3, ssl, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path
from mock_responses import make_events
import task_attribution
import weekly_budget
import official_reserve
from usage_accounting import request_features, reported_usage

MAX_BODY = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024

class ChannelError(Exception):
    def __init__(self, status, code): self.status, self.code = status, code

def dump(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
def secret_hash(value): return hashlib.sha256(value.encode()).hexdigest()
def private_write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == 'nt':
            who=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True)
            sid=re.search(r'S-1-5-[0-9-]+',who)
            if not sid:raise OSError('Cannot determine current Windows user SID')
            result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid.group()+':(F)'],capture_output=True)
            if result.returncode:raise OSError('Cannot protect credential file with a user-only Windows ACL')
        else:
            os.fchmod(fd,0o600)
            if os.fstat(fd).st_mode & 0o077:raise OSError('Credential file permissions are too broad')
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            fd=None; f.write(dump(value) + '\n')
    except BaseException:
        if fd is not None:os.close(fd)
        path.unlink(missing_ok=True)
        raise

def valid_url(value, allow_private_http=False):
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('Endpoint must be an HTTP(S) URL without credentials, query or fragment')
    try: loopback = ipaddress.ip_address(u.hostname).is_loopback
    except ValueError: loopback = u.hostname == 'localhost'
    if u.scheme == 'http' and not loopback:
        networks=['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10','fc00::/7']
        try: addr=ipaddress.ip_address(u.hostname)
        except ValueError:raise ValueError('Private HTTP requires a literal private-network IP address')
        if not allow_private_http or not any(addr in ipaddress.ip_network(n) for n in networks):
            raise ValueError('Use HTTPS, or explicitly allow HTTP to a trusted private-network IP')
    return value.rstrip('/')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None

OPENER = urllib.request.build_opener(NoRedirect)

@contextlib.contextmanager
def db(home):
    c = sqlite3.connect(Path(home) / 'state.sqlite3', timeout=5)
    c.row_factory = sqlite3.Row
    try:
        with c: yield c
    finally: c.close()

def config(home): return json.loads((Path(home) / 'node.json').read_text(encoding='utf-8'))

def initialize(home, name, model='tokencoin-demo', backend='mock', upstream=None, key_env=None, allow_private_http=False):
    home = Path(home); home.mkdir(parents=True, exist_ok=True)
    if (home / 'node.json').exists(): raise ValueError('Node already exists; use its current configuration')
    if backend not in ('mock', 'api', 'subscription-adapter'): raise ValueError('Unknown backend')
    if not name or not model: raise ValueError('Supplier name and model are required')
    if backend != 'mock':
        if not upstream or not key_env or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_env):
            raise ValueError('Real backend needs an approved Responses endpoint and key environment variable name')
        upstream = valid_url(upstream, allow_private_http)
    cfg = dict(name=name, model=model, backend=backend, upstream=upstream, key_env=key_env,
               max_output_tokens=1024, allow_private_http=allow_private_http)
    with db(home) as c:
        c.executescript('''CREATE TABLE channels(id TEXT PRIMARY KEY, label TEXT, token_hash TEXT UNIQUE,
            expires REAL, remaining INTEGER, revoked INTEGER DEFAULT 0);
            CREATE TABLE requests(id TEXT PRIMARY KEY, channel_id TEXT, idem TEXT, status TEXT,
            started REAL, ended REAL, usage TEXT, UNIQUE(channel_id,idem));''')
    with db(home) as c:weekly_budget.initialize(c)
    task_attribution.initialize_schema(home)
    private_write(home / 'node.json', cfg)
    return cfg

def invite(home, base_url, out, label='friend', requests=20, minutes=60, allow_private_http=False, weekly_percent=None, budget_mode=None, **options):
    reserve_option=options.pop('official_reserve',False)
    if options:raise TypeError('Unknown invite option')
    if reserve_option:
        if budget_mode not in (None,'official_weekly_reserve','estimated_weekly_with_official_reserve'):
            raise ValueError('Conflicting budget mode')
        budget_mode=('estimated_weekly_with_official_reserve'
                     if weekly_percent is not None else 'official_weekly_reserve')
    if not 1 <= requests <= 1000 or not 1 <= minutes <= 1440: raise ValueError('Invalid request or expiry limit')
    cfg = config(home); base_url = valid_url(base_url, allow_private_http)
    if weekly_percent is not None and cfg['backend']!='subscription-adapter':
        raise ValueError('Estimated weekly budgets require a subscription supplier')
    if urllib.parse.urlsplit(base_url).path not in ('', '/'): raise ValueError('Use the gateway origin, without /v1')
    channel_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    expiry = time.time() + minutes * 60
    value = dict(version=1, channel_id=channel_id, supplier=cfg['name'], model=cfg['model'],
                 backend=cfg['backend'], base_url=base_url, token=token, expires=expiry,
                 allow_private_http=allow_private_http)
    # The invitation itself is a scoped reusable access credential until expiry/revocation.
    task_attribution.initialize_schema(home)
    budget=None
    with db(home) as c:
        weekly_budget.initialize(c)
        official_reserve.initialize(c)
        if budget_mode in ('official_weekly_reserve','estimated_weekly_with_official_reserve'):
            if cfg['backend']!='subscription-adapter':raise official_reserve.ReserveError('official_reserve_requires_subscription')
            reserve_cfg=c.execute('SELECT valid_until,weekly_reset FROM official_reserve_config WHERE singleton=1').fetchone()
            if not reserve_cfg:raise official_reserve.ReserveError('official_reserve_not_configured')
            expiry=min(expiry,reserve_cfg['valid_until'],reserve_cfg['weekly_reset']);value['expires']=expiry
        elif budget_mode not in (None,'legacy_requests','estimated_weekly'):raise ValueError('Unknown budget mode')
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO channels(id,label,token_hash,expires,remaining) VALUES(?,?,?,?,?)',
                  (channel_id,label,secret_hash(token),expiry,requests))
        estimated=weekly_budget.create_grant(c,channel_id,weekly_percent) if weekly_percent is not None else None
        reserve_budget=(official_reserve.create_grant(c,channel_id)
                        if budget_mode in ('official_weekly_reserve','estimated_weekly_with_official_reserve') else None)
        budget=_combined_budget(estimated,reserve_budget)
        try: private_write(out, value)
        except BaseException: c.rollback(); raise
    return {'channel_id':channel_id, 'invitation_file':str(Path(out).resolve()),
            'supplier':cfg['name'], 'backend':cfg['backend'], 'remaining_requests':requests, 'budget':budget}

def auth(c, token):
    row = c.execute('SELECT * FROM channels WHERE token_hash=?',(secret_hash(token),)).fetchone()
    if not row or row['revoked'] or row['expires'] <= time.time(): raise ChannelError(401,'channel_invalid_or_expired')
    return row

def _combined_budget(estimated,reserve_budget):
    if estimated is not None and reserve_budget is not None:
        return {'mode':'estimated_weekly_with_official_reserve',
                'estimated_weekly':estimated,'official_weekly_reserve':reserve_budget}
    return reserve_budget or estimated

def _channel_budget(c,channel_id):
    return _combined_budget(weekly_budget.summary(c,channel_id),official_reserve.summary(c,channel_id))

def reserve(home, token, idem, body=None):
    with db(home) as c:
        c.execute('BEGIN IMMEDIATE'); row = auth(c,token)
        if c.execute('SELECT 1 FROM requests WHERE channel_id=? AND idem=?',(row['id'],idem)).fetchone():
            raise ChannelError(409,'duplicate_request_not_replayed')
        if row['remaining'] <= 0: raise ChannelError(429,'request_allowance_exhausted')
        if isinstance(body,dict) and 'service_tier' not in body and c.execute('SELECT 1 FROM weekly_budget_grants WHERE channel_id=?',(row['id'],)).fetchone():
            body['service_tier']='default'
        try:weekly_budget.check(c,row['id'],body)
        except weekly_budget.BudgetError as e:raise ChannelError(429,e.code) from None
        if c.execute("SELECT 1 FROM requests WHERE channel_id=? AND status='running'",(row['id'],)).fetchone():
            raise ChannelError(429,'one_request_at_a_time')
        request_id = uuid.uuid4().hex
        c.execute('UPDATE channels SET remaining=remaining-1 WHERE id=?',(row['id'],))
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',(request_id,row['id'],idem,'running',time.time(),None,None))
    return request_id

def finish(home, request_id, status, usage=None):
    with db(home) as c:
        c.execute('UPDATE requests SET status=?,ended=?,usage=? WHERE id=?',
                  (status,time.time(),dump(usage) if usage else None,request_id))

def local_status(home):
    cfg = config(home)
    with db(home) as c:
        weekly_budget.initialize(c)
        calibration=weekly_budget.active_profile(c)
        channels=[dict(r) for r in c.execute('SELECT id,label,expires,remaining,revoked FROM channels')]
        official_reserve.initialize(c)
        reserve_state=official_reserve.summary(c)
        for item in channels:item['budget']=_channel_budget(c,item['id'])
        requests=[dict(r) for r in c.execute('SELECT id,channel_id,status,started,ended,usage FROM requests ORDER BY started DESC LIMIT 30')]
    return dict(supplier=cfg['name'],model=cfg['model'],backend=cfg['backend'],channels=channels,requests=requests,calibration=calibration,official_reserve=reserve_state)

def revoke(home, channel_id):
    with db(home) as c:
        result=c.execute('UPDATE channels SET revoked=1 WHERE id=?',(channel_id,))
        if not result.rowcount: raise ValueError('Channel not found')
    return {'channel_id':channel_id,'revoked':True,
            'in_flight_policy':'official-reserve streams are asked to stop; termination is best effort and other accepted requests may finish'}

class Gateway(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self, address, home, upstream_key=None, reserve_guard=None):
        self.home=Path(home); self.cfg=config(home)
        self.upstream_key=upstream_key if upstream_key is not None else (os.environ.get(self.cfg['key_env'],'') if self.cfg['key_env'] else '')
        self.reserve_guard=reserve_guard
        if self.cfg['backend']!='mock' and not self.upstream_key:
            raise ValueError('Set the configured upstream credential environment variable before starting')
        task_attribution.initialize_schema(home)
        with db(home) as c:
            weekly_budget.initialize(c)
            official_reserve.initialize(c)
            if c.execute('SELECT 1 FROM official_reserve_grants LIMIT 1').fetchone() and reserve_guard is None:
                raise ValueError('reserve_guard_required')
            c.execute("UPDATE requests SET status='unknown_after_restart',ended=? WHERE status='running'",(time.time(),))
        super().__init__(address, Handler)

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass  # No URLs, prompts, authorization headers or upstream error bodies.
    def setup(self):
        super().setup(); self.connection.settimeout(30)
    def json_reply(self,status,value):
        data=dump(value).encode(); self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close'); self.end_headers(); self.wfile.write(data); self.close_connection=True
    def token(self):
        value=self.headers.get('Authorization','')
        if not value.startswith('Bearer ') or len(value)>512: raise ChannelError(401,'bearer_required')
        return value[7:]
    def do_GET(self):
        try:
            if self.path=='/health':
                with db(self.server.home) as c:reserve_state=official_reserve.summary(c)
                safe=None if reserve_state is None else {'mode':reserve_state['mode'],'blocked_reason':reserve_state['blocked_reason']}
                return self.json_reply(200,{'ready':True,'official_reserve':safe})
            if self.path!='/v1/channel': raise ChannelError(404,'route_not_found')
            with db(self.server.home) as c:
                row=auth(c,self.token())
                budget=_channel_budget(c,row['id'])
            cfg=self.server.cfg
            self.json_reply(200,dict(channel_id=row['id'],supplier=cfg['name'],backend=cfg['backend'],
                                   model=cfg['model'],remaining_requests=row['remaining'],expires=row['expires'],budget=budget))
        except ChannelError as e:self.json_reply(e.status,{'error':{'code':e.code}})
    def do_POST(self):
        started=False; request_id=None; outcome='unknown'; usage=None; terminal_sent=False; active_upstream=None
        try:
            if self.path!='/v1/responses': raise ChannelError(404,'route_not_found')
            if self.headers.get('Origin'): raise ChannelError(403,'browser_origin_not_allowed')
            token=self.token()
            with db(self.server.home) as c:
                authrow=auth(c,token);reserve_granted=official_reserve.is_granted(c,authrow['id'])
            if reserve_granted:
                if self.server.reserve_guard is None:raise ChannelError(503,'reserve_guard_required')
                try:self.server.reserve_guard.authorize(authrow['id'])
                except official_reserve.ReserveError as e:raise ChannelError(429,e.code) from None
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding') not in (None,'identity'):
                raise ChannelError(400,'encoded_request_not_supported')
            try: size=int(self.headers.get('Content-Length','0'))
            except ValueError: raise ChannelError(400,'invalid_length')
            if size<=0 or size>MAX_BODY: raise ChannelError(413,'request_size_limit')
            data=self.rfile.read(size)
            if len(data)!=size: raise ChannelError(400,'incomplete_body')
            try: body=json.loads(data)
            except (ValueError,UnicodeError): raise ChannelError(400,'invalid_json')
            if not isinstance(body,dict): raise ChannelError(400,'object_required')
            cfg=self.server.cfg
            if body.get('model')!=cfg['model']: raise ChannelError(403,'model_not_allowed')
            if body.get('stream') is not True: raise ChannelError(400,'stream_true_required')
            if body.get('background') or body.get('previous_response_id') or body.get('conversation') is not None:
                raise ChannelError(400,'use_stateless_foreground_requests')
            functions=body.get('tools',[])
            def local_tools(ts,depth=0):
                return depth<8 and isinstance(ts,list) and all(isinstance(t,dict) and
                    (t.get('type') in ('function','custom') or t.get('type')=='namespace' and local_tools(t.get('tools'),depth+1)) for t in ts)
            if not local_tools(functions):
                raise ChannelError(400,'only_local_function_or_custom_tools_supported')
            cap=body.get('max_output_tokens',cfg['max_output_tokens'])
            if type(cap) is not int or cap<1: raise ChannelError(400,'invalid_output_limit')
            body['max_output_tokens']=min(cap,cfg['max_output_tokens']); body['store']=False
            idem=self.headers.get('Idempotency-Key') or uuid.uuid4().hex
            if len(idem)>128: raise ChannelError(400,'idempotency_key_too_long')
            metadata=body.get('client_metadata')
            hint=self.headers.get('Thread-Id') or (metadata.get('thread_id') if isinstance(metadata,dict) else None) or self.headers.get('Session-Id') or body.get('prompt_cache_key')
            if hint is not None and (not isinstance(hint,str) or not 1 <= len(hint.encode('utf-8')) <= task_attribution.MAX_HINT_BYTES):
                raise ChannelError(400,'invalid_thread_hint')
            request_id=reserve(self.server.home,token,idem,body)
            with db(self.server.home) as c:
                owner=c.execute('SELECT channel_id FROM requests WHERE id=?',(request_id,)).fetchone()['channel_id']
            attribution=task_attribution.prepare(self.server.home,owner,request_id,body,hint)
            if cfg['backend']=='subscription-adapter':body=attribution['body']
            else:body.pop('prompt_cache_key',None)
            usage={'source':'unavailable', **request_features(body), 'supplier_task_id':attribution['supplier_task_id']}
            def start_stream():
                nonlocal started
                self.send_response(200); self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-cache'); self.send_header('X-TokenCoin-Request-Id',request_id)
                self.send_header('X-TokenCoin-Supplier',urllib.parse.quote(cfg['name']))
                self.send_header('X-TokenCoin-Backend',cfg['backend']); self.send_header('Connection','close')
                self.end_headers(); self.close_connection=True; started=True
            if cfg['backend']=='mock':
                start_stream()
                for event in make_events(body,cfg['name']):
                    self.wfile.write(('event: '+event['type']+'\ndata: '+dump(event)+'\n\n').encode()); self.wfile.flush()
                    if event['type']=='response.completed':
                        terminal_sent=True;usage={'source':'synthetic','input_tokens':0,'output_tokens':0}
                outcome='completed_synthetic'
            else:
                headers={'Authorization':'Bearer '+self.server.upstream_key,'Content-Type':'application/json','Accept':'text/event-stream'}
                if cfg['backend']=='subscription-adapter':headers.update(attribution['headers'])
                req=urllib.request.Request(cfg['upstream']+'/responses',data=dump(body).encode(),headers=headers)
                try: upstream=OPENER.open(req,timeout=30)
                except urllib.error.HTTPError as e:
                    status=e.code if e.code in (400,401,403,404,409,429,500,502,503,504) else 502
                    e.close(); outcome='upstream_rejected'; raise ChannelError(status,'upstream_rejected_request')
                with upstream:
                    if reserve_granted:
                        try:self.server.reserve_guard.register_stream(upstream,owner)
                        except official_reserve.ReserveError as e:raise ChannelError(429,e.code) from None
                        active_upstream=upstream
                    if 'text/event-stream' not in upstream.headers.get('Content-Type',''):
                        raise ChannelError(502,'upstream_did_not_return_sse')
                    start_stream(); pending=b''; total=0; deadline=time.monotonic()+300; completed=False
                    while True:
                        if time.monotonic()>deadline: raise ChannelError(504,'total_time_limit')
                        chunk=upstream.read1(8192)
                        if not chunk:break
                        total+=len(chunk)
                        if total>MAX_RESPONSE:raise ChannelError(502,'response_size_limit')
                        self.wfile.write(chunk);self.wfile.flush()
                        pending=(pending+chunk).replace(b'\r\n',b'\n')
                        if len(pending)>MAX_BODY:raise ChannelError(502,'sse_event_size_limit')
                        while b'\n\n' in pending:
                            block,pending=pending.split(b'\n\n',1)
                            raw=b'\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b'data:'))
                            try:event=json.loads(raw)
                            except (ValueError,UnicodeError):continue
                            if event.get('type')=='response.completed':
                                completed=True; terminal_sent=True
                                usage=reported_usage(event.get('response'),body)
                                usage['supplier_task_id']=attribution['supplier_task_id']
                            elif event.get('type') in ('response.failed','error'):outcome='upstream_failed'
                    if completed:outcome='completed_upstream'
                    elif outcome!='upstream_failed':outcome='unknown_incomplete_stream'
        except ChannelError as e:
            outcome=e.code
            if not started:self.json_reply(e.status,{'error':{'code':e.code}})
            elif not terminal_sent:
                try:self.wfile.write(('event: error\ndata: '+dump({'type':'error','code':e.code,'message':'Gateway stopped the request; inspect local status.'})+'\n\n').encode());self.wfile.flush()
                except OSError:pass
        except (BrokenPipeError,ConnectionResetError,TimeoutError,OSError):
            outcome='unknown_disconnected_or_timeout'
            if not started:
                try:self.json_reply(502,{'error':{'code':'connection_failed'}})
                except OSError:pass
        except Exception:
            outcome='internal_error'
            if not started:
                try:self.json_reply(500,{'error':{'code':'internal_error'}})
                except OSError:pass
            elif not terminal_sent:
                try:self.wfile.write(b'event: error\ndata: {"type":"error","code":"internal_error","message":"Gateway error"}\n\n');self.wfile.flush()
                except OSError:pass
        finally:
            if active_upstream is not None:self.server.reserve_guard.finish_stream(active_upstream)
            if request_id:finish(self.server.home,request_id,outcome,usage)
            if started:self.close_connection=True

def peer_path(home,name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',name):raise ValueError('Use a short ASCII peer name')
    return Path(home)/'peers'/(name+'.json')

def peer_read(home,name):
    p=json.loads(peer_path(home,name).read_text(encoding='utf-8'))
    valid_url(p['base_url'],p.get('allow_private_http',False))
    return p

def peer_status(peer):
    req=urllib.request.Request(peer['base_url']+'/v1/channel',headers={'Authorization':'Bearer '+peer['token']})
    with OPENER.open(req,timeout=10) as r:return json.load(r)

def join(home,name,path,allow_private_http=False):
    peer=json.loads(Path(path).read_text(encoding='utf-8'))
    if peer.get('version')!=1 or not isinstance(peer.get('token'),str):raise ValueError('Invalid invitation')
    valid_url(peer['base_url'],allow_private_http)
    peer['allow_private_http']=allow_private_http
    status=peer_status(peer)
    if status['channel_id']!=peer['channel_id'] or status['model']!=peer['model']:raise ValueError('Invitation metadata mismatch')
    private_write(peer_path(home,name),peer)
    return status

def ask(home,name,prompt):
    peer=peer_read(home,name)
    body={'model':peer['model'],'stream':True,'input':prompt,'store':False}
    req=urllib.request.Request(peer['base_url']+'/v1/responses',data=dump(body).encode(),headers={'Authorization':'Bearer '+peer['token'],'Content-Type':'application/json'})
    complete=False
    with OPENER.open(req,timeout=30) as response:
        for line in response:
            if not line.startswith(b'data:'):continue
            try:event=json.loads(line[5:])
            except ValueError:continue
            if event.get('type')=='response.output_text.delta':print(event.get('delta',''),end='',flush=True)
            if event.get('type')=='response.completed':complete=True
    print()
    if not complete:raise ValueError('Stream ended without completion; execution and usage may be unknown')

def run_codex(home,name,prompt,workdir,effort="low",sandbox="read-only",json_events=False):
    if sandbox not in ("read-only","workspace-write"):raise ValueError("Unsupported sandbox")
    peer=peer_read(home,name); state=peer_status(peer)
    executable=shutil.which('codex')
    if not executable:raise ValueError('Install Codex CLI first')
    isolated=Path(home).resolve()/'codex-sessions'/name;isolated.mkdir(parents=True,exist_ok=True)
    core={'PATH','PATHEXT','SYSTEMDRIVE','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','USERPROFILE','HOME','APPDATA','LOCALAPPDATA','LANG','LC_ALL','TERM'}
    env={k:v for k,v in os.environ.items() if k.upper() in core}
    env['CODEX_HOME']=str(isolated);env['TOKENCOIN_PEER_KEY']=peer['token']
    args=[executable,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check','--color','never','-s',sandbox,'-C',str(Path(workdir).resolve()),
          '-c','model_provider="tokencoin_peer"','-c','model='+json.dumps(peer['model']),
          '-c','model_reasoning_effort='+json.dumps(effort),
          '-c','model_providers.tokencoin_peer.name="TokenCoin friend channel"',
          '-c','model_providers.tokencoin_peer.base_url='+json.dumps(peer['base_url']+'/v1'),
          '-c','model_providers.tokencoin_peer.env_key="TOKENCOIN_PEER_KEY"',
          '-c','model_providers.tokencoin_peer.wire_api="responses"',
          '-c','model_providers.tokencoin_peer.requires_openai_auth=false',
          '-c','model_providers.tokencoin_peer.supports_websockets=false',
          '-c','model_providers.tokencoin_peer.request_max_retries=0',
          '-c','model_providers.tokencoin_peer.stream_max_retries=0',
          '-c','shell_environment_policy.inherit="core"',
          '-c','shell_environment_policy.ignore_default_excludes=false',
          '-c','shell_environment_policy.exclude=["TOKENCOIN_PEER_KEY"]',
          '--disable','plugins','--disable','apps','--disable','multi_agent','--enable','skip_host_skill_discovery','--enable','code_mode_host',
          '-c','project_doc_max_bytes=0','-c','web_search="disabled"']
    if os.name=='nt':args += ['-c','windows.sandbox="mxc"']
    if json_events:args.append('--json')
    args.append('-')
    print('Supplier: '+state['supplier']+' | backend: '+state['backend']+' | request slots: '+str(state['remaining_requests']),flush=True)
    return subprocess.run(args,input=prompt,text=True,env=env).returncode

def main(argv=None):
    p=argparse.ArgumentParser(description='TokenCoin paired Codex demo (Python 3.10+)')
    s=p.add_subparsers(dest='command',required=True)
    def home(q):q.add_argument('--home',required=True,type=Path)
    q=s.add_parser('init');home(q);q.add_argument('--name',required=True);q.add_argument('--model',default='tokencoin-demo');q.add_argument('--backend',choices=['mock','api','subscription-adapter'],default='mock');q.add_argument('--upstream');q.add_argument('--key-env');q.add_argument('--allow-private-http',action='store_true')
    q=s.add_parser('serve');home(q);q.add_argument('--host',default='127.0.0.1');q.add_argument('--port',type=int,default=8781);q.add_argument('--cert');q.add_argument('--key');q.add_argument('--allow-private-http',action='store_true')
    q=s.add_parser('invite');home(q);q.add_argument('--url',required=True);q.add_argument('--out',required=True,type=Path);q.add_argument('--label',default='friend');q.add_argument('--requests',type=int,default=20);q.add_argument('--weekly-percent',type=float);q.add_argument('--budget-mode',choices=['legacy_requests','estimated_weekly','official_weekly_reserve']);q.add_argument('--minutes',type=int,default=60);q.add_argument('--allow-private-http',action='store_true')
    q=s.add_parser('join');home(q);q.add_argument('--peer',required=True);q.add_argument('--invite',required=True,type=Path);q.add_argument('--allow-private-http',action='store_true')
    q=s.add_parser('status');home(q);q.add_argument('--peer')
    q=s.add_parser('revoke');home(q);q.add_argument('--channel',required=True)
    for cmd in ('ask','codex'):
        q=s.add_parser(cmd);home(q);q.add_argument('--peer',required=True);q.add_argument('--prompt',required=True)
        if cmd=='codex':
            q.add_argument('--workdir',default='.')
            q.add_argument('--effort',choices=['low','medium','high'],default='low')
            q.add_argument('--sandbox',choices=['read-only','workspace-write'],default='read-only')
            q.add_argument('--json',action='store_true')
    a=p.parse_args(argv)
    try:
        if a.command=='init':print(dump(initialize(a.home,a.name,a.model,a.backend,a.upstream,a.key_env,a.allow_private_http)))
        elif a.command=='serve':
            if a.host not in ('127.0.0.1','localhost','::1') and not a.cert and not a.allow_private_http:
                raise ValueError('Non-loopback listener requires TLS, or explicit trusted-private-network HTTP')
            server=Gateway((a.host,a.port),a.home)
            if a.cert:
                ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(a.cert,a.key);server.socket=ctx.wrap_socket(server.socket,server_side=True)
            print(dump({'listening':a.host,'port':server.server_port,'supplier':server.cfg['name'],'backend':server.cfg['backend']}),flush=True)
            try:server.serve_forever()
            except KeyboardInterrupt:pass
            finally:server.server_close()
        elif a.command=='invite':print(dump(invite(a.home,a.url,a.out,a.label,a.requests,a.minutes,a.allow_private_http,getattr(a,'weekly_percent',None),getattr(a,'budget_mode',None))))
        elif a.command=='join':print(dump(join(a.home,a.peer,a.invite,a.allow_private_http)))
        elif a.command=='status':print(dump(peer_status(peer_read(a.home,a.peer)) if a.peer else local_status(a.home)))
        elif a.command=='revoke':print(dump(revoke(a.home,a.channel)))
        elif a.command=='ask':ask(a.home,a.peer,a.prompt)
        elif a.command=='codex':return run_codex(a.home,a.peer,a.prompt,a.workdir,a.effort,a.sandbox,a.json)
        return 0
    except urllib.error.HTTPError as e:print('Channel request failed (HTTP '+str(e.code)+'); use status to check expiry/revocation/allowance',file=sys.stderr);return 1
    except (ValueError,OSError,KeyError) as e:print('Cannot continue: '+str(e),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
