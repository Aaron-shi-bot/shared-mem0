"""GPT queue CLI. Claim output is transient untrusted source data, never a prompt."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from mcp_bridge import call

p=argparse.ArgumentParser()
p.add_argument('action',choices=['status','scan','claim','submit','fail','backup','cloud-prepare'])
p.add_argument('--client',choices=['codex','workbuddy','pi','claude'])
p.add_argument('--limit',type=int,default=2)
p.add_argument('--max-chars',type=int,default=32000)
p.add_argument('--force',action='store_true',help='scan only: bypass the quiet-time delay, retaining full source/hash validation')
p.add_argument('--session',help='scan only: exact client:session_id to scan without reparsing other sessions')
p.add_argument('--file',help='UTF-8 JSON containing one submission or a list; only summaries, no raw chats')
a=p.parse_args()
if (a.force or a.session) and a.action!='scan':p.error('--force and --session are only valid with scan')
if a.action=='status':result=call('GET','/api/automation/status')
elif a.action=='scan':result=call('POST','/api/automation/scan',{'force':a.force,'session_key':a.session})
elif a.action=='claim':result=call('POST','/api/automation/claim',{'limit':a.limit,'max_chars':a.max_chars,'client':a.client})
elif a.action=='backup':result=call('POST','/api/backup',{})
elif a.action=='cloud-prepare':result=call('POST','/api/cloud/prepare',{})
else:
    if not a.file:p.error('--file is required')
    payload=json.loads(Path(a.file).read_text(encoding='utf-8-sig'))
    rows=payload if isinstance(payload,list) else [payload]
    result=[]
    for row in rows:
        try:result.append({'job_id':row['job_id'],'result':call('POST','/api/automation/'+a.action,row)})
        except Exception as exc:
            detail=str(exc)[:200]
            if getattr(exc,'response',None) is not None:
                try:detail=exc.response.json().get('detail',detail)
                except ValueError:pass
            result.append({'job_id':row.get('job_id'),'error':type(exc).__name__,'detail':detail})
print(json.dumps(result,ensure_ascii=False,indent=2))
if isinstance(result,list) and any('error' in row for row in result):raise SystemExit(1)
