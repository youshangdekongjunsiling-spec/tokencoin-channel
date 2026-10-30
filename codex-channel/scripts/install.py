"""Install the local TokenCoin marketplace using the selected Python runtime."""
from pathlib import Path
import argparse,json,shutil,subprocess,sys,os

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
 if sys.version_info<(3,10):raise SystemExit('Python 3.10+ is required.')
 codex=shutil.which('codex')
 if not codex:raise SystemExit('Codex CLI was not found. Install it, reopen this terminal, and retry.')
 plugin=Path(__file__).resolve().parent.parent;marketplace=plugin.parent
 if not (marketplace/'.agents/plugins/marketplace.json').is_file():raise SystemExit('Extract the complete release folder, including the hidden .agents directory.')
 calls=[[codex,'plugin','marketplace','add',str(marketplace)],[codex,'plugin','add','tokencoin-channel@tokencoin-demo']]
 if args.dry_run:
  print(json.dumps({'python':sys.executable,'plugin':str(plugin),'commands':calls},ensure_ascii=False,indent=2));return
 config=plugin/'.mcp.json';original=config.read_bytes();data=json.loads(original);data['mcpServers']['tokencoin']['command']=sys.executable
 temp=config.with_name('.mcp.install.tmp')
 try:
  temp.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8');os.replace(temp,config)
  for call in calls:subprocess.run(call,check=True)
 except BaseException:
  temp.write_bytes(original);os.replace(temp,config);raise
 finally:temp.unlink(missing_ok=True)
 print('TokenCoin installed. Open a new Codex window/session and find TokenCoin in the sidebar. If the host has not refreshed, restart Codex.')
 print('Fallback: run the start-panel launcher in this download folder.')
if __name__=='__main__':main()
