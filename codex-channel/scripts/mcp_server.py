"""TokenCoin local MCP server: sidebar UI plus app-only control bridge. Stdlib only."""
from pathlib import Path
import sys,json,threading,urllib.request,urllib.error,re,base64,contextlib
sys.path.insert(0,str(Path(__file__).resolve().parent))
import dashboard
ROOT=Path(__file__).resolve().parent.parent
URI="ui://tokencoin/dashboard-v020"
VERSION="0.2.0"
class Plugin:
 def __init__(self,home=None):
  self.home=home;self.server=None;self.thread=None
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def ensure_server(self):
  if self.server is None:
   self.server=dashboard.create_server(home=self.home,port=0)
   self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def close(self):
  if self.server:
   self.server.shutdown();self.server.server_close();self.server.state.close()
 def tools(self):
  icon="data:image/svg+xml;base64,"+base64.b64encode((ROOT/'assets/icon.svg').read_bytes()).decode()
  return [{"name":"tokencoin_dashboard","title":"TokenCoin 双向通道","description":"打开本机 TokenCoin 双向通道控制面板；创建邀请、连接朋友和运行任务由用户在界面操作。","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False},"icons":[{"src":icon,"mimeType":"image/svg+xml"}],"_meta":{"ui":{"resourceUri":URI},"openai/ui":{"entrypoints":[{"type":"global"},{"type":"thread"}]}}},
  {"name":"tokencoin_action","title":"TokenCoin 界面操作","description":"仅供插件界面使用。返回信息留在界面，不进入模型上下文。","inputSchema":{"type":"object","properties":{"method":{"type":"string","enum":["GET","POST"]},"path":{"type":"string"},"body":{"type":"object"}},"required":["method","path"],"additionalProperties":False},"annotations":{"readOnlyHint":False,"destructiveHint":True,"openWorldHint":True},"_meta":{"ui":{"resourceUri":URI,"visibility":["app"]}}}]
 def action(self,args):
  method,path=args.get('method'),args.get('path','')
  reads=path in ('/api/status','/api/peers') or bool(re.fullmatch(r'/api/peers/[A-Za-z0-9_-]{1,40}/status',path)) or bool(re.fullmatch(r'/api/jobs/[0-9a-f]{32}',path))
  writes=path in ('/api/adapter/setup','/api/supplier/start','/api/supplier/stop','/api/supplier/invites','/api/supplier/revoke','/api/peers/import','/api/codex/jobs')
  if not ((method=='GET' and reads) or (method=='POST' and writes)):return {'content':[{'type':'text','text':'Unsupported UI operation.'}],'isError':True,'_meta':{'data':{'error':{'code':'unsupported_action'}}}}
  try:self.ensure_server()
  except dashboard.ApiError as error:
   return {'content':[{'type':'text','text':'请关闭另一处 TokenCoin 控制面板后重试。'}],'isError':True,'_meta':{'data':{'error':{'code':error.code}}}}
  data=None if method=='GET' else json.dumps(args.get('body',{}),ensure_ascii=False).encode()
  req=urllib.request.Request(self.server.base_url+path,data=data,method=method,headers={'Authorization':'Bearer '+self.server.token,'Origin':self.server.origin,'Content-Type':'application/json'})
  try:
   with self.opener.open(req,timeout=35) as response:value=json.load(response)
   return {'content':[{'type':'text','text':'操作已完成，详情显示在 TokenCoin 界面。'}],'_meta':{'data':value}}
  except urllib.error.HTTPError as error:
   try:value=json.load(error)
   finally:error.close()
   return {'content':[{'type':'text','text':'操作未完成，请查看 TokenCoin 界面提示。'}],'isError':True,'_meta':{'data':value}}
 def resource(self):
  html=(ROOT/'ui/index.html').read_text('utf-8')
  html=html.replace('<link rel="stylesheet" href="styles.css">','<style>'+(ROOT/'ui/styles.css').read_text('utf-8')+'</style>')
  html=html.replace('<script src="app.js" defer></script>','<script defer>'+ (ROOT/'ui/app.js').read_text('utf-8').replace('</script','<\\/script')+'</script>')
  # Inline script runs after markup when consumed as an MCP App resource.
  start=html.index('<script defer>');end=html.index('</script>',start)+9;script=html[start:end].replace('<script defer>','<script>')
  html=html[:start]+html[end:];html=html.replace('</body>',script+'</body>')
  return {'contents':[{'uri':URI,'mimeType':'text/html;profile=mcp-app','text':html,'_meta':{'ui':{'prefersBorder':False,'csp':{'connectDomains':[],'resourceDomains':[]}},'openai/widgetPrefersBorder':False}}]}
 def handle(self,method,params):
  if method=='initialize':return {'protocolVersion':params.get('protocolVersion','2025-11-25'),'capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'tokencoin-channel','version':VERSION}}
  if method=='ping':return {}
  if method=='tools/list':return {'tools':self.tools()}
  if method=='resources/list':return {'resources':[{'uri':URI,'name':'TokenCoin','mimeType':'text/html;profile=mcp-app'}]}
  if method=='resources/templates/list':return {'resourceTemplates':[]}
  if method=='prompts/list':return {'prompts':[]}
  if method=='resources/read' and params.get('uri')==URI:return self.resource()
  if method=='tools/call':
   if params.get('name')=='tokencoin_dashboard':return {'content':[{'type':'text','text':'TokenCoin 控制面板已打开。请在界面内管理两个方向的授权。'}],'_meta':{'ui':{'resourceUri':URI}}}
   if params.get('name')=='tokencoin_action':return self.action(params.get('arguments') or {})
  raise ValueError('unsupported_method')
def main():
 for stream in (sys.stdin,sys.stdout,sys.stderr):
  if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
 p=Plugin()
 try:
  for line in sys.stdin:
   if len(line)>2*1024*1024:continue
   message=None
   try:
    message=json.loads(line)
    if 'id' not in message:continue
    response={'jsonrpc':'2.0','id':message['id'],'result':p.handle(message.get('method'),message.get('params') or {})}
   except Exception:response={'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,'error':{'code':-32603,'message':'TokenCoin request could not be completed.'}}
   print(json.dumps(response,ensure_ascii=False),flush=True)
 finally:p.close()
if __name__=='__main__':main()
