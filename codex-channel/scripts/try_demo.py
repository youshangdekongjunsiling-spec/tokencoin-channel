"""One-command two-node walk-through. No account login or paid inference."""
import argparse,contextlib,io,json,sys,tempfile,threading,time
from pathlib import Path
import channel

def main():
    p=argparse.ArgumentParser();p.add_argument('--codex',action='store_true',help='Also run the real Codex CLI against the synthetic gateways');p.add_argument('--evidence',type=Path);a=p.parse_args()
    observed=[];original=channel.make_events
    def observe(body,supplier):
        inp=body.get('input',[]);inp=inp if isinstance(inp,list) else []
        outputs=[x for x in inp if isinstance(x,dict) and x.get('type')=='function_call_output' and x.get('call_id')=='tc_demo_pwd']
        rejected=any('rejected' in json.dumps(x.get('output','')).lower() or 'blocked by policy' in json.dumps(x.get('output','')).lower() for x in outputs)
        observed.append({'supplier':supplier,'local_tool_result_received':bool(outputs),'local_tool_execution_rejected':rejected})
        return original(body,supplier)
    channel.make_events=observe
    demo_base=(a.evidence.parent/'runtime') if a.evidence else (Path.home()/'.tokencoin-demo-runtime')
    demo_base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='session-',dir=demo_base) as temp:
        root=Path(temp);alice=root/'alice';bob=root/'bob';work=root/'empty-project';work.mkdir()
        channel.initialize(alice,'Alice');channel.initialize(bob,'Bob')
        servers=[]
        try:
            for home in [alice,bob]:
                s=channel.Gateway(('127.0.0.1',0),home);t=threading.Thread(target=s.serve_forever,kwargs={'poll_interval':0.03},daemon=True);t.start();servers.append((s,t))
            ia=root/'alice-invitation.json';ib=root/'bob-invitation.json'
            ca=channel.invite(alice,'http://127.0.0.1:'+str(servers[0][0].server_port),ia)
            cb=channel.invite(bob,'http://127.0.0.1:'+str(servers[1][0].server_port),ib)
            channel.join(bob,'alice',ia);channel.join(alice,'bob',ib)
            print('\n1. Alice and Bob are running separate supplier nodes and have exchanged scoped invitations.',flush=True)
            print('2. Bob -> Alice:',flush=True);channel.ask(bob,'alice','hello')
            print('3. Alice -> Bob:',flush=True);channel.ask(alice,'bob','hello')
            cli=[]
            if a.codex:
                print('\n4. Real Codex CLI -> Alice (synthetic upstream):',flush=True)
                cli.append(channel.run_codex(bob,'alice','Reply with one short greeting.',work))
                print('\n5. Real Codex CLI -> Bob, local pwd tool round-trip:',flush=True)
                cli.append(channel.run_codex(alice,'bob','TOKENCOIN_DEMO_TOOL_ROUNDTRIP. Run the harmless pwd command once and report the result.',work))
                if any(cli):raise RuntimeError('Codex CLI verification failed; see its output')
                if not any(r['local_tool_result_received'] for r in observed):raise RuntimeError('Codex CLI did not complete the expected local tool round-trip')
            channel.revoke(alice,ca['channel_id'])
            try:channel.peer_status(channel.peer_read(bob,'alice'));raise RuntimeError('Revocation did not block the channel')
            except channel.urllib.error.HTTPError as e:
                if e.code!=401:raise
            print('\n6. Alice revoked her invitation: Bob is rejected. The reverse channel remains valid.',flush=True)
            channel.peer_status(channel.peer_read(alice,'bob'))
            for _ in range(100):
                states=[channel.local_status(alice),channel.local_status(bob)]
                if not any(r['status']=='running' for state in states for r in state['requests']):break
                time.sleep(0.01)
            result={'passed':True,'real_model_calls':0,'network':'loopback HTTP, two independent node databases',
                    'codex_cli_checked':a.codex,'codex_exit_codes':cli,'tool_roundtrip':any(r['local_tool_result_received'] for r in observed),
                    'tool_execution_rejected':any(r['local_tool_execution_rejected'] for r in observed),
                    'observed_requests':observed,'supplier_states':states,'revocation_verified':True}
            if a.evidence:a.evidence.parent.mkdir(parents=True,exist_ok=True);a.evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            if result['tool_execution_rejected']:print('Tool request/response round-trip verified; local command execution was REJECTED by CLI policy.',flush=True)
            print('\nPASS: channel path, supplier direction and revocation verified. Real subscription/API spending has not been tested.',flush=True)
        finally:
            for s,t in servers:s.shutdown();s.server_close();t.join(3)
    return 0

if __name__=='__main__':raise SystemExit(main())
