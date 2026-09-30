import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import qrcode
import qrcode.image.svg

from bili import Bili, BiliError
from core import Store, Engine, Conflict


class LoginBody(BaseModel):
    password: str = Field(max_length=256)


class WhiteBody(BaseModel):
    uid: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    keep: bool


class StartBody(BaseModel):
    preview_id: str = Field(max_length=100)


def create_app(directory=None, api=None, interval=5):
    os.umask(0o077)
    store = Store(directory or os.environ.get('BILI_DATA_DIR', './data'))
    pw_file = store.directory/'admin-password'
    if not pw_file.exists(): pw_file.write_text(secrets.token_urlsafe(24)+'\n')
    os.chmod(pw_file,0o600)
    password_hash = hashlib.sha256(pw_file.read_text().strip().encode()).digest()
    engine = Engine(store,api or Bili(),interval)
    qrs = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        await engine.close()
        store.db.close()

    app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.engine = engine

    @app.middleware('http')
    async def protect(request: Request, call_next):
        host = request.url.hostname
        if host not in ('127.0.0.1','localhost'):
            return JSONResponse({'detail':'无效访问地址'},status_code=400)
        if request.url.path.startswith('/api/'):
            if request.method != 'GET':
                origin = request.headers.get('origin','')
                if origin != f'{request.url.scheme}://{request.headers.get("host","")}':
                    return JSONResponse({'detail':'来源校验失败，请从管理页面操作'},status_code=403)
            if request.url.path != '/api/admin/login':
                token = request.cookies.get('bili_admin','')
                hashed = hashlib.sha256(token.encode()).hexdigest()
                session = store.db.execute('SELECT * FROM sessions WHERE token=? AND expires>?',(hashed,time.time())).fetchone()
                if not session:
                    return JSONResponse({'detail':'请登录管理页面'},status_code=401)
                request.state.session = dict(session)
                if request.method != 'GET' and not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
                    return JSONResponse({'detail':'操作令牌失效，请重新登录'},status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://*.hdslb.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request,e):
        return JSONResponse({'detail':str(e)},status_code=409)

    @app.exception_handler(BiliError)
    async def bili_handler(request,e):
        return JSONResponse({'detail':str(e)},status_code=502)

    @app.exception_handler(Exception)
    async def unknown_handler(request,e):
        return JSONResponse({'detail':'服务异常；任务不会自动继续，请重新核对'},status_code=500)

    @app.post('/api/admin/login')
    async def login(body: LoginBody):
        limited = store.get('admin_failures',{'count':0,'since':0})
        now=time.time()
        if now-limited['since']>900: limited={'count':0,'since':now}
        if limited['count']>=10: raise HTTPException(429,'尝试次数过多，请 15 分钟后重试')
        if not hmac.compare_digest(hashlib.sha256(body.password.encode()).digest(),password_hash):
            limited['count']+=1;store.set('admin_failures',limited)
            raise HTTPException(401,'管理密码错误')
        store.set('admin_failures',{'count':0,'since':now})
        store.db.execute('DELETE FROM sessions WHERE expires<?',(now,))
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        store.db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,now+86400))
        response=JSONResponse({'csrf':csrf})
        response.set_cookie('bili_admin',token,httponly=True,samesite='strict',max_age=86400)
        return response

    @app.post('/api/admin/logout')
    async def admin_logout(request: Request):
        store.db.execute('DELETE FROM sessions WHERE token=?',(request.state.session['token'],))
        response=JSONResponse({'ok':True});response.delete_cookie('bili_admin');return response

    @app.get('/api/state')
    async def state(request: Request):
        account=store.get('account');snapshot=store.get('snapshot'); active=store.active()
        last=active or store.db.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 1').fetchone()
        white=[]
        if account:
            white=[dict(r) for r in store.db.execute('SELECT uid,name FROM whitelist WHERE account=? ORDER BY name',(account['uid'],))]
        return {'account':account,'whitelist':white,'snapshot_at':(snapshot or {}).get('created'),
            'total':len((snapshot or {}).get('rows',[])), 'task':store.task(last['id']) if last else None,
            'csrf':request.state.session['csrf'],'locked':bool(active and active['status']!='paused')}

    @app.get('/api/followings')
    async def followings(page: int=1,q: str='',white_only: bool=False):
        page=max(1,page);q=q[:100].strip().casefold()
        account=store.get('account');snapshot=store.get('snapshot')
        rows=(snapshot or {}).get('rows',[])
        protected=store.protected(account['uid']) if account else set()
        if white_only:
            cached={r['uid']:r for r in rows}
            rows=[cached.get(r['uid'],dict(r)) for r in store.db.execute('SELECT uid,name FROM whitelist WHERE account=?',(account['uid'] if account else '',))]
        rows=[dict(r,keep=r['uid'] in protected) for r in rows if not q or q in r['name'].casefold() or q in r['uid']]
        return {'rows':rows[(page-1)*30:page*30],'count':len(rows),'page':page,'complete':bool(snapshot)}

    @app.post('/api/followings/refresh')
    async def refresh():
        s=await engine.refresh();return {'total':len(s['rows'])}

    @app.post('/api/whitelist')
    async def whitelist(body: WhiteBody):
        await engine.whitelist(body.uid,body.keep);return {'ok':True}

    @app.post('/api/preview')
    async def preview(): return await engine.preview()

    @app.post('/api/tasks/start')
    async def start(body: StartBody): return await engine.start(body.preview_id)

    @app.post('/api/tasks/{task_id}/pause')
    async def pause(task_id: str): return await engine.pause(task_id)

    @app.get('/api/tasks/{task_id}')
    async def task(task_id: str): return store.task(task_id)

    @app.post('/api/bili/qr')
    async def qr(request: Request):
        async with engine.lock:
            engine.editable()
            data=await engine.api.qr()
            # The key is held server-side, bound to this administrator session.
            qrs.clear()
            qrs[request.state.session['token']]={'key':data['qrcode_key'],'created':time.time()}
            image=qrcode.make(data['url'],image_factory=qrcode.image.svg.SvgPathImage)
            buf=io.BytesIO();image.save(buf)
            return {'image':'data:image/svg+xml;base64,'+base64.b64encode(buf.getvalue()).decode(),'expires_in':180}

    @app.post('/api/bili/poll')
    async def poll(request: Request):
        async with engine.lock:
            engine.editable()
            qr=qrs.get(request.state.session['token'])
            if not qr or time.time()-qr['created']>180:return {'status':'expired'}
            data,cookies=await engine.api.poll(qr['key'])
            if data['code']==0:
                if not all(cookies.get(k) for k in ('SESSDATA','bili_jct')):
                    raise BiliError('扫码成功但凭据不完整，请重新扫码')
                account=await engine.api.account(cookies);active=store.active()
                if active and account['uid']!=active['account']:raise Conflict('存在未完成任务，请登录原账号')
                store.save_account(account,cookies);qrs.clear();return {'status':'success','account':account}
            if data['code']==86038:return {'status':'expired'}
            if data['code']==86090:return {'status':'scanned'}
            if data['code']==86101:return {'status':'waiting'}
            raise BiliError('扫码状态异常，请重新生成二维码')

    @app.post('/api/bili/logout')
    async def bili_logout():
        async with engine.lock:
            engine.editable()
            if store.active():raise Conflict('仍有暂停的任务，请先完成或结束任务')
            store.db.execute("DELETE FROM meta WHERE key IN ('cookies','account','snapshot')")
            qrs.clear();return {'ok':True}

    @app.post('/api/tasks/{task_id}/cancel')
    async def cancel(task_id: str):
        async with engine.lock:
            engine.editable()
            task=store.task(task_id)
            if task['status']!='paused':raise Conflict('仅可结束已暂停任务')
            store.state('cancelled','用户结束任务；已发生的取关不会自动撤销',task_id)
            return store.task(task_id)

    static=Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')

    @app.get('/')
    async def index(): return FileResponse(static/'index.html')

    return app
