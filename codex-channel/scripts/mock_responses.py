"""Deterministic Responses fixtures. No inference, tokenization or billing."""
import json,time,uuid

def make_events(body,supplier):
    rid='resp_'+uuid.uuid4().hex; mid='msg_'+uuid.uuid4().hex
    base={'id':rid,'object':'response','created_at':int(time.time()),'status':'in_progress','model':body.get('model','demo'),'output':[],'error':None}
    events=[{'type':'response.created','response':dict(base)}]
    inputs=body.get('input',[])
    if not isinstance(inputs,list):inputs=[]
    has_output=any(isinstance(x,dict) and x.get('type')=='function_call_output' and x.get('call_id')=='tc_demo_pwd' for x in inputs)
    names={t.get('name',(t.get('function') or {}).get('name')) for t in body.get('tools',[]) if isinstance(t,dict)}
    tool_mode='TOKENCOIN_DEMO_TOOL_ROUNDTRIP' in json.dumps(body,ensure_ascii=False) and 'exec_command' in names and not has_output
    if tool_mode:
        item={'id':'fc_'+uuid.uuid4().hex,'type':'function_call','call_id':'tc_demo_pwd','name':'exec_command','arguments':json.dumps({'cmd':'pwd'}),'status':'completed'}
        events.append({'type':'response.output_item.done','output_index':0,'item':item})
    else:
        text='DEMO supplied by '+supplier+'; no real model or billing.'
        if has_output:text+=' Local tool result received by the supplier.'
        part={'type':'output_text','text':text,'annotations':[]}
        item={'id':mid,'type':'message','role':'assistant','status':'completed','content':[part]}
        events += [
            {'type':'response.output_item.added','output_index':0,'item':{'id':mid,'type':'message','role':'assistant','status':'in_progress','content':[]}},
            {'type':'response.content_part.added','item_id':mid,'output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}}]
        for i in range(0,len(text),12):events.append({'type':'response.output_text.delta','item_id':mid,'output_index':0,'content_index':0,'delta':text[i:i+12]})
        events += [
            {'type':'response.output_text.done','item_id':mid,'output_index':0,'content_index':0,'text':text},
            {'type':'response.content_part.done','item_id':mid,'output_index':0,'content_index':0,'part':part},
            {'type':'response.output_item.done','output_index':0,'item':item}]
    base.update(status='completed',output=[item],usage={'input_tokens':0,'output_tokens':0,'total_tokens':0,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}})
    events.append({'type':'response.completed','response':base})
    return [dict(event,sequence_number=i) for i,event in enumerate(events)]
