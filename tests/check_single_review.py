import ast,re
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime
ROOT = Path(__file__).resolve().parents[1]
s=(ROOT / 'templates/transcript_detail.html').read_text()
js=re.search(r'<script>([\s\S]*?)</script>',s).group(1)
js=re.sub(r'\{\{.*?\}\}','sample',js)
Path('/tmp/single-review.js').write_text(js)
assert 'data-review-step' not in s
assert 'review-stepper' not in s
assert 'name="crm_reviewed"' in s and 'name="send_email"' in s
module=ast.parse((ROOT / 'main.py').read_text()); fn=next(x for x in module.body if isinstance(x,ast.FunctionDef) and x.name=='update_status')
fn.decorator_list=[]
for arg in fn.args.args:arg.annotation=None
fn.args.defaults=[ast.Constant(None)]*len(fn.args.defaults)
class DB:
 def query(self,*a):return self
 def filter(self,*a):return self
 def first(self):return self.t
 def commit(self):pass
class Field:
 def __eq__(self,other):return True
calls=[]
env={'models':NS(TranscriptResponse=NS(id=Field()),TranscriptStatus=NS(approved=NS(value='approved'))),'_clean_string':lambda x:(x or '').strip() or None,'render_template':lambda *a,**k:k['status_code'],'_assigned_agency_zoom_agent':lambda *a:(None,'Agent'),'create_agency_zoom_customer_note_for_transcript':lambda *a,**k:calls.append('note'),'datetime':datetime,'RedirectResponse':lambda **k:k,'get_logged_in_admin':lambda *a:None,'_send_note_email':lambda *a:(calls.append('email') or True,'sent')}
exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'approval','exec'),env)
for linked,reviewed,body,send,expected in [(False,True,'Note',False,400),(True,False,'Note',False,400),(True,True,'',False,400),(True,True,'Edited note',False,303),(True,True,'Edited note',True,303)]:
 db=DB();db.t=NS(id='test',status='pending',agency_zoom_customer_id='123' if linked else None,crm_note='old',agency_zoom_note_posted_at=None)
 calls.clear();r=env['update_status']('test',None,'approved',reviewed,body,send,db)
 assert (r if isinstance(r,int) else r['status_code'])==expected
 if expected==400:assert not calls and db.t.status=='pending'
 else:assert db.t.crm_note==body and calls==(['note','email'] if send else ['note'])
print('Approval checks passed: client link, CRM review, edited-note saving, optional email.')
