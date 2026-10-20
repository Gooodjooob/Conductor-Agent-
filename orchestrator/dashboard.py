"""Read-only task cards; no gateway credentials, prompt or workspace contents in browser."""
import hashlib
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .gateway import require


STYLE = '''
:root{font-family:Inter,system-ui,-apple-system,sans-serif;color:#183036;background:#f2f5f3;font-size:14px}*{box-sizing:border-box}body{margin:0}header{background:#133c35;color:#fff;padding:34px 5vw 26px}h1{font-size:30px;letter-spacing:-1px;margin:8px 0}header p{color:#bfd3c9;max-width:850px;line-height:1.8}.eyebrow{font-size:11px;letter-spacing:3px;color:#bce39c}main{padding:24px 5vw}#stats{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px}.stat{background:#fff;border:1px solid #dbe5df;padding:15px 22px;border-radius:12px;min-width:140px}.stat strong{display:block;font-size:27px;margin-top:4px}.toolbar{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}input,select,button{font:inherit;padding:10px 14px;border:1px solid #c8d8d0;border-radius:8px;background:white}button{cursor:pointer;color:#fff;background:#20594c}#search{flex:1;min-width:210px}.board{display:grid;grid-template-columns:repeat(4,minmax(220px,1fr));gap:17px}.lane{min-width:0}.lane h2{font-size:13px;text-transform:uppercase;color:#5c7168;display:flex;justify-content:space-between}.card{background:#fff;border:1px solid #dce5e0;border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 3px 10px #18303605}.card h3{margin:10px 0;font-size:17px;line-height:1.4}.tag{font-size:11px;border-radius:5px;padding:4px 7px;background:#eaf1ed;display:inline-block;margin-right:5px}.meta{color:#65776f;font-size:12px;margin:8px 0;word-break:break-word}.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #eef2ef;font-size:12px}.warn{color:#9a5210;background:#fff6e5;padding:9px;border-radius:6px;word-break:break-word}.next{font-size:12px;line-height:1.7;color:#3a6254;margin-top:12px}.digest{font:11px ui-monospace,monospace;overflow-wrap:anywhere}.empty{color:#84958c;padding:25px 5px;font-size:12px}.bad{color:#a63b3b}.good{color:#267052}details{margin-top:14px}summary{cursor:pointer;font-size:12px;color:#62796e}.foot{margin:25px 0;color:#75887d;font-size:12px;line-height:1.8}#status{min-height:24px;color:#62786d}#login{margin-top:15px}#login input{max-width:360px;width:60%}@media(max-width:1080px){.board{grid-template-columns:repeat(2,minmax(220px,1fr))}}@media(max-width:560px){.board{grid-template-columns:1fr}h1{font-size:25px}main{padding:18px}.stat{min-width:120px;flex:1}header{padding:24px 18px}}
'''
SCRIPT = '''
const initial=JSON.parse(document.getElementById('snapshot').textContent);let data=initial,token='';
const $=id=>document.getElementById(id);const names={queued:'等待派发',launching:'正在派发',running:'执行中',unknown:'待核对',cancel_requested:'等待停止证据',cancelled:'已取消派发',failed:'执行失败',awaiting_validation:'等待验收',accepted_pending_sync:'等待同步',done:'已交付',pending:'待完成',passed:'通过',not_synced:'未同步',synced:'已回读确认',sync_unknown:'同步待核对',pass:'通过',fail:'不通过'};
function text(tag,value,cls){const e=document.createElement(tag);e.textContent=value??'—';if(cls)e.className=cls;return e}
function row(card,label,value){const e=text('div','','row');e.append(text('span',label),text('span',names[value]||value||'—'));card.append(e)}
function lane(c){if(c.cancelRequested||c.error||['unknown','failed','cancel_requested','cancelled'].includes(c.state))return 2;if(c.state==='done'&&c.syncState==='synced')return 3;if(['awaiting_validation','accepted_pending_sync'].includes(c.state))return 1;return 0}
function render(){const cards=data.cards||[];const host=$('host').value;const q=$('search').value.toLowerCase();$('stats').replaceChildren();[['任务',cards.length],['执行中',cards.filter(c=>['running','launching'].includes(c.state)).length],['待处理',cards.filter(c=>lane(c)===2).length],['已交付',cards.filter(c=>lane(c)===3).length]].forEach(([k,v])=>{const el=text('div',k,'stat');el.append(text('strong',v));$('stats').append(el)});const board=$('board');board.replaceChildren();['排队与执行','验收与同步','问题与取消','已交付'].forEach((label,i)=>{const section=text('section','','lane');const selected=cards.filter(c=>lane(c)===i&&(!host||c.host===host)&&JSON.stringify([c.title,c.taskId,c.memberId,c.host,c.error]).toLowerCase().includes(q));const h=text('h2',label);h.append(text('span',selected.length));section.append(h);if(!selected.length)section.append(text('div','暂无任务','empty'));selected.forEach(c=>{const card=text('article','','card');card.dataset.taskId=c.taskId;card.append(text('span',c.host,'tag'),text('span','R'+c.revision,'tag'),text('h3',c.title),text('div',c.taskId+' · '+c.memberId+' / '+c.role,'meta'));row(card,'调度',c.state);row(card,'执行',c.executionState);row(card,'验收',c.validationState);row(card,'Paperclip',c.syncState);if(c.error)card.append(text('p',c.error,'warn'));if(c.blockers?.length)card.append(text('p','等待：'+c.blockers.join('、'),'meta'));if(c.cancelRequested)card.append(text('p','已阻断后续派发；进程停止'+(c.stopConfirmed?'已证实':'尚未证实'),'warn'));card.append(text('div','下一步 · '+(data.dispatchEnabled===false&&c.state==='queued'?'调度已停止，需排查后显式恢复':data.mode==='plan-preview'?'预览模式，尚未启用派发':c.nextAction),'next'));const details=text('details','');details.append(text('summary','查看依赖与证据'));details.append(text('p','依赖：'+(c.dependsOn?.join('、')||'无'),'meta'));details.append(text('p','上游卡片：'+c.issueId,'meta'));details.append(text('p','Attempt：'+(c.attemptId||'尚未派发'),'digest'));details.append(text('p','Result SHA-256：'+(c.resultDigest||'尚无受信结果'),'digest'));for(const r of ['qa','reviewer','controller'])row(details,r,c.verdicts?.[r]||'pending');card.append(details);section.append(card)});board.append(section)});renderStatus()}
function renderStatus(){
 const observed=Object.prototype.hasOwnProperty.call(data,'lastObservedAt')?data.lastObservedAt:data.generatedAt;
 const stopped=data.dispatchEnabled===false;
 const mode=data.mode==='plan-preview'?'计划预览，不会派发':data.mode==='plan-execute'?'真实执行入口（是否为测试宿主以任务配置为准）':data.evidenceType==='demo/fixture'?'合成演示，无真实宿主调用':'';
 $('status').textContent=[mode,data.evidenceType?'证据类型：'+data.evidenceType:'','最后成功观测：'+(observed?new Date(observed*1000).toLocaleString():'尚未观测'),data.schedulerError?'调度已停止：'+data.schedulerError:stopped?'调度已停止，未派发任务保留排队状态':initial.live?'只读看板，每 5 秒刷新':'静态快照'].filter(Boolean).join(' · ');
 $('status').className=data.schedulerError||stopped?'bad':'';
}
function setHosts(){const old=$('host').value;$('host').replaceChildren(text('option','全部宿主'));$('host').firstChild.value='';[...new Set((data.cards||[]).map(c=>c.host))].sort().forEach(h=>{const o=text('option',h);o.value=h;$('host').append(o)});$('host').value=old}
async function refresh(){if(!initial.live||!token)return;try{const r=await fetch('/api/snapshot',{headers:{Authorization:'Bearer '+token},cache:'no-store'});if(!r.ok)throw new Error(r.status===401?'只读令牌无效':'状态服务不可用');data=await r.json();setHosts();render()}catch(e){$('status').textContent=e.message+'；保留旧快照，请勿据此判断实时完成';$('status').className='bad'}}
$('search').oninput=render;$('host').onchange=render;$('connect').onclick=()=>{token=$('token').value;$('token').value='';refresh()};$('refresh').onclick=refresh;$('login').hidden=!initial.live;$('refresh').hidden=!initial.live;setHosts();render();if(initial.live)setInterval(refresh,5000);
'''


def render_html(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    nonce = secrets.token_urlsafe(20)
    csp = "default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'nonce-"+nonce+"'; connect-src 'self'; base-uri 'none'; form-action 'none'"
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="'''+csp+'''"><title>Agent 协作控制台</title><style nonce="'''+nonce+'">'+STYLE+'''</style></head><body>
<header><div class="eyebrow">AGENT OPERATIONS / 开发协作</div><h1>任务有去向，结果有依据。</h1><p>集中查看成员、执行进度和交付证据。每个任务完成执行后，还需通过独立验收与 Paperclip 回读确认。</p></header>
<main><div id="stats"></div><div class="toolbar"><input id="search" aria-label="搜索任务" placeholder="搜索任务、成员或问题"><select id="host" aria-label="按宿主筛选"></select><button id="refresh">刷新状态</button></div>
<div id="login" hidden><input id="token" type="password" autocomplete="off" aria-label="只读访问令牌" placeholder="只读访问令牌"><button id="connect">连接看板</button></div><p id="status" role="status"></p><div id="board" class="board"></div>
<div class="foot">执行、验收和同步分别记录。取消请求不等于进程已停止。额度路由暂未启用。</div></main>
<script id="snapshot" type="application/json">'''+raw+'''</script><script nonce="'''+nonce+'">'+SCRIPT+'</script></body></html>')


def write_snapshot(path, snapshot):
    from .runner import durable_json
    target = Path(path)
    require(not target.exists() and not target.is_symlink(), 'DASHBOARD_OUTPUT_EXISTS')
    # Export contains only projection fields, never runner config or token.
    fd = __import__('os').open(str(target), __import__('os').O_WRONLY | __import__('os').O_CREAT | __import__('os').O_EXCL, 0o600)
    with __import__('os').fdopen(fd, 'w') as stream: stream.write(render_html(snapshot))


def make_dashboard(provider, read_token, port=0):
    require(isinstance(read_token,str) and len(read_token)>=32, 'STRONG_READ_TOKEN_REQUIRED')
    hashed = hashlib.sha256(read_token.encode()).digest()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,status,body,kind='application/json'):
            raw = body.encode() if isinstance(body,str) else json.dumps(body).encode()
            self.send_response(status); self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY'); self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            allowed={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
            if self.headers.get('Host') not in allowed: return self.respond(403,{'error':'HOST_DENIED'})
            origin=self.headers.get('Origin')
            if origin and origin not in {'http://'+host for host in allowed}: return self.respond(403,{'error':'ORIGIN_DENIED'})
            if self.path=='/': return self.respond(200,render_html({'live':True,'cards':[]}),'text/html')
            if self.path!='/api/snapshot': return self.respond(404,{'error':'NOT_FOUND'})
            auth=self.headers.get('Authorization','')
            if len(self.headers.get_all('Authorization',[]))!=1 or not auth.startswith('Bearer ') or not hmac.compare_digest(hashlib.sha256(auth[7:].encode()).digest(),hashed):
                return self.respond(401,{'error':'AUTH_REQUIRED'})
            try: self.respond(200,provider())
            except Exception: self.respond(503,{'error':'SNAPSHOT_UNAVAILABLE'})
        def do_POST(self): self.respond(405,{'error':'READ_ONLY'})
        do_PATCH=do_PUT=do_DELETE=do_POST
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler); server.daemon_threads=True
    return server
