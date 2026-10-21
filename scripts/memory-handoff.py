"""Current clients can use this immediately; newly registered MCP tools need a reload."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from mcp_bridge import call

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('devices')
    handoff=sub.add_parser('project');handoff.add_argument('project');handoff.add_argument('--query',default='')
    catalog=sub.add_parser('catalog');catalog.add_argument('--file',required=True)
    for verb in ('preview','apply'):
        p=sub.add_parser(verb);p.add_argument('--device',required=True);p.add_argument('--file',required=True)
        if verb=='apply':
            p.add_argument('--sha256',required=True);p.add_argument('--previous-sha256',default='')
    args=parser.parse_args()
    if args.command=='devices':result=call('GET','/api/devices')
    elif args.command=='project':result=call('POST','/api/handoff',dict(project=args.project,query=args.query))
    elif args.command=='catalog':result=call('POST','/api/devices/catalog',json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
    else:
        payload=dict(device=args.device,path=str(Path(args.file).resolve()))
        if args.command=='apply':payload.update(expected_sha256=args.sha256,expected_previous_sha256=args.previous_sha256)
        result=call('POST','/api/imports/'+args.command,payload)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
