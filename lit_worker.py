#!/usr/bin/env python3
"""Trusted Supabase adapter. Never expose this process or service key to browsers."""
import argparse,json,os,re,tempfile,time,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import quote
from video_prepare import prepare,MAX_BYTES
class ServiceError(Exception): pass
class Worker:
 def __init__(self,url,key):
  if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co',url) or not key:raise ValueError('Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.')
  self.url,self.key=url,key
 def request(self,path,method='GET',body=None,content_type='application/json',missing_ok=False,destination=None):
  headers={'apikey':self.key,'Authorization':'Bearer '+self.key,'Content-Type':content_type}
  raw=json.dumps(body).encode() if isinstance(body,(dict,list)) else body
  try:
   with urllib.request.urlopen(urllib.request.Request(self.url+path,data=raw,headers=headers,method=method),timeout=45) as response:
    if destination:
     length=response.headers.get('Content-Length')
     if length and int(length)>MAX_BYTES:raise ValueError('Recording exceeds the size limit.')
     total=0
     with Path(destination).open('xb') as target:
      while True:
       data=response.read(1024*1024)
       if not data:break
       total+=len(data)
       if total>MAX_BYTES:raise ValueError('Recording exceeds the size limit.')
       target.write(data)
     return None
    data=response.read(1024*1024)
    try:return json.loads(data) if data else None
    except (ValueError,TypeError):raise ServiceError('Unexpected service response.') from None
  except urllib.error.HTTPError as error:
   if missing_ok and error.code==404:return None
   # Do not expose URLs, headers, tokens, Auth bodies or user data in logs.
   raise ServiceError('Remote operation failed.') from None
  except (urllib.error.URLError,TimeoutError,OSError):raise ServiceError('Service connection unavailable.') from None
 def rpc(self,name,args={}):return self.request('/rest/v1/rpc/'+name,'POST',args)
 def delete_media(self,bucket,path):return self.request('/storage/v1/object/'+quote(bucket,safe=''),'DELETE',{'prefixes':[path]},missing_ok=True)
 def result(self,job,ok,metadata={}):return self.rpc('lit_worker_result',{'p_kind':job['kind'],'p_id':job['id'],'p_lease':job['lease'],'p_ok':ok,'p_metadata':metadata})
 def process(self,job):
  uploaded=False
  try:
   if job['kind']=='cleanup':self.delete_media(job['bucket'],job['path']);self.result(job,True)
   elif job['kind']=='deletion':
    self.rpc('lit_worker_purge_account',{'p_id':job['id'],'p_lease':job['lease']})
    self.request('/auth/v1/admin/users/'+quote(job['user_id'],safe=''),'DELETE',missing_ok=True)
    self.result(job,True)
   elif job['kind']=='video':
    # Job paths never become filesystem paths or shell arguments. All media uses fixed local names.
    suffix=Path(job['path']).suffix
    if suffix not in('.webm','.mp4','.mov'):raise ValueError('Unsupported media format.')
    with tempfile.TemporaryDirectory(prefix='lit-job-') as tmp:
     source=Path(tmp)/('recording'+suffix);output=Path(tmp)/'processed.mp4'
     self.request('/storage/v1/object/'+quote(job['bucket'],safe='')+'/'+quote(job['path'],safe='/'),destination=source)
     meta=prepare(source,output);meta['duration_ms']=round(meta['duration_seconds']*1000)
     if abs(meta['duration_ms']-job['declared_ms'])>1000:raise ValueError('Recorded and measured duration differ.')
     self.request('/storage/v1/object/lit-fresh-videos/'+quote(job['output_path'],safe='/'),'POST',output.read_bytes(),'video/mp4');uploaded=True
     self.result(job,True,meta)
   else:raise ValueError('Unknown worker task.')
   print(json.dumps({'event':'worker_task_finished','kind':job['kind'],'id':job['id']}),flush=True)
  except Exception as error:
   if uploaded:
    try:
     status=self.rpc('lit_worker_result_status',{'p_id':job['id'],'p_lease':job['lease']})
     if status and status.get('published'):
      print(json.dumps({'event':'worker_publication_confirmed','id':job['id']}),flush=True);return
     self.delete_media('lit-fresh-videos',job['output_path'])
    except Exception:pass # Uncertain commits keep the object; SQL sweeps only unreferenced orphans.

   code='MEDIA_INVALID' if isinstance(error,ValueError) else 'SERVICE_FAILED'
   try:self.result(job,False,{'code':code})
   except Exception:pass # Expired/removed leases must never publish; a later claim recovers the task.
   print(json.dumps({'event':'worker_task_failed','kind':job.get('kind'),'id':job.get('id'),'code':code}),flush=True)
 def once(self):
  job=self.rpc('lit_worker_claim')
  if job:self.process(job)
  return bool(job)
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');options=parser.parse_args()
 worker=Worker(os.environ.get('SUPABASE_URL','').rstrip('/'),os.environ.get('SUPABASE_SERVICE_ROLE_KEY',''))
 while True:
  try:busy=worker.once()
  except Exception:print(json.dumps({'event':'worker_connection_failed'}),flush=True);busy=False
  if options.once:break
  if not busy:time.sleep(5)
if __name__=='__main__':main()
