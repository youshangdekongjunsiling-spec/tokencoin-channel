"""High-entropy invitation URI. The fragment is a scoped credential; never log it."""
import base64, json, re, time
PREFIX="tokencoin://connect#"
MAX_LENGTH=16384

def validate(value):
 if not isinstance(value,dict) or value.get("version")!=1: raise ValueError("Invalid quick invitation")
 transport=value.get("transport")
 if not isinstance(transport,dict) or transport.get("kind")!="iroh" or not isinstance(transport.get("ticket"),str) or not 10<len(transport["ticket"])<4096:
  raise ValueError("Invalid quick connection ticket")
 if not isinstance(value.get("token"),str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",value["token"]): raise ValueError("Invalid invitation capability")
 if not isinstance(value.get("channel_id"),str) or not re.fullmatch(r"[0-9a-f]{32}",value["channel_id"]): raise ValueError("Invalid channel")
 for key,limit in (("supplier",80),("model",120),("backend",40)):
  if not isinstance(value.get(key),str) or not 0<len(value[key])<=limit: raise ValueError("Invalid invitation metadata")
 if type(value.get("expires")) not in (int,float) or not time.time()<value["expires"]<=time.time()+86460: raise ValueError("Invitation expired or invalid")
 return value

def encode(value):
 validate(value)
 raw=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode("utf-8")
 result=PREFIX+base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
 if len(result)>MAX_LENGTH: raise ValueError("Invitation too large")
 return result

def decode(text):
 if not isinstance(text,str): raise ValueError("Invalid invitation text")
 text=text.strip()
 if len(text)>MAX_LENGTH or not text.startswith(PREFIX): raise ValueError("Expected TokenCoin invitation URI")
 payload=text[len(PREFIX):]
 if not re.fullmatch(r"[A-Za-z0-9_-]+",payload): raise ValueError("Invalid invitation encoding")
 return validate(json.loads(base64.urlsafe_b64decode(payload+"="*((-len(payload))%4)).decode("utf-8")))
