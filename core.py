"""SQLite-backed task engine: immutable previews, whitelist protection, safe restart."""
import asyncio
import json
import os
import random
from pathlib import Path
import secrets
import sqlite3
import time
from cryptography.fernet import Fernet
from bili import BiliError


class Conflict(Exception):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory); self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        key = self.directory / "credentials.key"
        if not key.exists(): key.write_bytes(Fernet.generate_key())
        os.chmod(key, 0o600)
        self.cipher = Fernet(key.read_bytes())
        self.db = sqlite3.connect(self.directory / "app.sqlite", isolation_level=None, check_same_thread=False)
        os.chmod(self.directory / "app.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS whitelist (account TEXT, uid TEXT, name TEXT, PRIMARY KEY(account,uid));
        CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, account TEXT, revision INTEGER,
          snapshot INTEGER, created REAL, task_id TEXT, targets TEXT);
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, account TEXT, status TEXT, message TEXT,
          created REAL, updated REAL, source_preview TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS items (task_id TEXT, uid TEXT, name TEXT, status TEXT, message TEXT,
          PRIMARY KEY(task_id,uid));
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT, expires REAL);
        ''')
        self.db.execute("UPDATE items SET status='unknown',message='服务中断，需核对结果' WHERE status='inflight'")
        self.db.execute("UPDATE tasks SET status='paused',message='服务重启，请重新预览并核对后恢复' WHERE status IN ('running','pausing','verifying')")

    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def cookies(self):
        encrypted = self.get("cookies")
        if not encrypted: raise Conflict("请先扫码登录 B 站")
        return json.loads(self.cipher.decrypt(encrypted.encode()))

    def save_account(self, info, cookies):
        self.set("cookies", self.cipher.encrypt(json.dumps(cookies).encode()).decode())
        self.set("account", info); self.set("snapshot", None)

    def protected(self, account):
        return {r[0] for r in self.db.execute("SELECT uid FROM whitelist WHERE account=?", (account,))}

    def active(self):
        return self.db.execute("SELECT * FROM tasks WHERE status IN ('running','pausing','verifying','paused') ORDER BY created DESC LIMIT 1").fetchone()

    def task(self, task_id):
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r: raise Conflict("任务不存在")
        out = dict(r)
        out['items'] = [dict(x) for x in self.db.execute("SELECT uid,name,status,message FROM items WHERE task_id=? ORDER BY rowid", (task_id,))]
        counts = {}
        for x in out['items']: counts[x['status']] = counts.get(x['status'], 0) + 1
        out['counts'] = counts
        return out

    def state(self, status, message, task_id):
        self.db.execute("UPDATE tasks SET status=?,message=?,updated=? WHERE id=?", (status,message,time.time(),task_id))


class Engine:
    def __init__(self, store, api, interval=5):
        self.s, self.api, self.interval = store, api, interval
        self.lock = asyncio.Lock(); self.worker = None

    def editable(self):
        t = self.s.active()
        if t and t['status'] != 'paused': raise Conflict("任务尚在执行或核对，请先暂停并等待当前请求结束")

    async def refresh(self):
        async with self.lock:
            self.editable()
            return await self._sync()

    async def _sync(self):
        # Invalidated before fetching: an old successful snapshot must not survive a failed refresh.
        self.s.set('snapshot', None)
        account = self.s.get('account')
        if not account: raise Conflict("请先扫码登录")
        cookies = self.s.cookies()
        verified = await self.api.account(cookies)
        if verified['uid'] != account['uid']: raise Conflict("登录账号发生变化，请重新登录")
        rows = await self.api.followings(cookies, account['uid'])
        generation = self.s.get('generation',0) + 1
        self.s.set('generation',generation)
        snapshot = {'generation':generation,'created':time.time(),'rows':rows,'account':account['uid']}
        self.s.set('snapshot',snapshot)
        for up in rows:
            self.s.db.execute('UPDATE whitelist SET name=? WHERE account=? AND uid=?',(up['name'],account['uid'],up['uid']))
        return snapshot

    async def whitelist(self, uid, keep):
        async with self.lock:
            self.editable()
            account = self.s.get('account')
            if not account: raise Conflict("请先扫码登录")
            snapshot = self.s.get('snapshot')
            row = next((r for r in (snapshot or {}).get('rows',[]) if r['uid']==uid),None)
            if keep and not row: raise Conflict("只能将完整列表中的关注加入白名单")
            if keep: self.s.db.execute('INSERT OR REPLACE INTO whitelist VALUES (?,?,?)',(account['uid'],uid,row['name']))
            else: self.s.db.execute('DELETE FROM whitelist WHERE account=? AND uid=?',(account['uid'],uid))
            self.s.set('revision',self.s.get('revision',0)+1)

    async def preview(self):
        async with self.lock:
            self.editable()
            snapshot = await self._sync()  # Fresh full list for every preview/resume.
            account = snapshot['account']; protected = self.s.protected(account)
            active = self.s.active(); task_id = active['id'] if active else None
            rows = snapshot['rows']; following = {r['uid'] for r in rows}
            if active:
                if active['account'] != account: raise Conflict("请登录原任务账号")
                originals = self.s.task(task_id)['items']
                # Never broaden a paused task to newly followed accounts.
                eligible = set()
                for item in originals:
                    if item['status'] == 'confirmed': continue
                    if item['uid'] not in following:
                        self._item(task_id,item['uid'],'confirmed','核对确认：已不在关注列表')
                    elif item['uid'] in protected:
                        self._item(task_id,item['uid'],'skipped','白名单保护')
                    else: eligible.add(item['uid'])
                rows = [r for r in rows if r['uid'] in eligible]
            targets = [r for r in rows if r['uid'] not in protected]
            token = secrets.token_urlsafe(24)
            self.s.db.execute('INSERT INTO previews VALUES (?,?,?,?,?,?,?)',(
                token,account,self.s.get('revision',0),snapshot['generation'],time.time(),task_id,json.dumps(targets)))
            return {'id':token,'targets':targets,'total':len(snapshot['rows']),
                'protected':len(following & protected),'count':len(targets),'task_id':task_id,'expires_in':600}

    async def start(self, token):
        async with self.lock:
            p = self.s.db.execute('SELECT * FROM previews WHERE id=?',(token,)).fetchone()
            if not p: raise Conflict("预览不存在，请重新预览")
            already = self.s.db.execute('SELECT id FROM tasks WHERE source_preview=?',(token,)).fetchone()
            if already: return self.s.task(already[0])
            snapshot = self.s.get('snapshot'); account = self.s.get('account')
            if (not account or not snapshot or p['account'] != account['uid'] or
                p['revision'] != self.s.get('revision',0) or p['snapshot'] != snapshot['generation'] or
                time.time()-p['created']>600): raise Conflict("名单已变化或预览过期，请重新预览")
            if self.worker and not self.worker.done(): raise Conflict("当前请求尚未结束")
            active = self.s.active()
            if active and (active['id'] != p['task_id'] or active['status'] != 'paused'):
                raise Conflict("已有任务，请先暂停并重新预览")
            if p['task_id'] and not active: raise Conflict("原任务状态已变化，请重新预览")
            targets = json.loads(p['targets']); protected = self.s.protected(account['uid'])
            if any(t['uid'] in protected for t in targets): raise Conflict("白名单已变化，请重新预览")
            task_id = p['task_id'] or secrets.token_urlsafe(16)
            db = self.s.db; db.execute('BEGIN IMMEDIATE')
            try:
                if not p['task_id']:
                    db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',(task_id,account['uid'],'running','逐个处理中',time.time(),time.time(),token))
                else:
                    db.execute('UPDATE tasks SET source_preview=? WHERE id=?',(token,task_id))
                    self.s.state('running','核对后恢复，逐个处理中',task_id)
                for up in targets:
                    db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)',(task_id,up['uid'],up['name'],'pending','等待处理'))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK'); raise
            self.s.set('snapshot',None)
            self.worker = asyncio.create_task(self.run(task_id))
            return self.s.task(task_id)

    async def pause(self, task_id):
        async with self.lock:
            task = self.s.task(task_id)
            if task['status'] == 'running': self.s.state('pausing','正在暂停；等待当前请求结束',task_id)
            return self.s.task(task_id)

    def _item(self, task_id, uid, status, message):
        self.s.db.execute('UPDATE items SET status=?,message=? WHERE task_id=? AND uid=?',(status,message,task_id,uid))

    async def run(self, task_id):
        try:
            while True:
                async with self.lock:
                    task = self.s.task(task_id)
                    if task['status'] == 'pausing':
                        self.s.state('paused','已暂停，可调整白名单并重新预览',task_id); return
                    if task['status'] != 'running': return
                    row = self.s.db.execute("SELECT * FROM items WHERE task_id=? AND status='pending' ORDER BY rowid LIMIT 1",(task_id,)).fetchone()
                    if not row: break
                    if row['uid'] in self.s.protected(task['account']):
                        self._item(task_id,row['uid'],'skipped','白名单保护'); continue
                    cookies = self.s.cookies()
                    self._item(task_id,row['uid'],'inflight','请求处理中；尚未确认')
                try:
                    await self.api.unfollow(cookies,row['uid'])
                except BiliError as e:
                    async with self.lock:
                        self._item(task_id,row['uid'],'unknown' if e.uncertain else 'failed',str(e))
                        self.s.state('paused',str(e),task_id)
                    return
                async with self.lock:
                    self._item(task_id,row['uid'],'accepted','接口已接受，等待完整列表核对')
                    if self.s.task(task_id)['status']=='pausing':
                        self.s.state('paused','已暂停，当前请求已结束',task_id); return
                delay = random.uniform(0.1, 2.0) if self.interval is None else self.interval
                await asyncio.sleep(delay)
            async with self.lock:
                self.s.state('verifying','操作结束，正在核对完整关注列表',task_id)
                snapshot = await self._sync()
                remaining = {r['uid'] for r in snapshot['rows']}
                for item in self.s.task(task_id)['items']:
                    if item['status']=='accepted':
                        self._item(task_id,item['uid'],'confirmed' if item['uid'] not in remaining else 'unknown',
                            '核对确认：已取关' if item['uid'] not in remaining else '接口已接受，但仍在关注列表，需重新核对')
                unresolved = any(r['status']=='unknown' for r in self.s.task(task_id)['items'])
                self.s.state('paused' if unresolved else 'completed',
                    '部分结果未确认，请重新预览并核对' if unresolved else '已完成完整关注列表核对',task_id)
        except asyncio.CancelledError:
            self.s.db.execute("UPDATE items SET status='unknown',message='服务退出，需核对' WHERE task_id=? AND status='inflight'",(task_id,))
            self.s.state('paused','服务退出，任务已暂停',task_id)
            raise
        except Exception:
            self.s.db.execute("UPDATE items SET status='unknown',message='异常中断，需核对' WHERE task_id=? AND status='inflight'",(task_id,))
            self.s.state('paused','接口或服务异常，已暂停；请重新预览核对',task_id)

    async def close(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            try: await self.worker
            except asyncio.CancelledError: pass
        await self.api.close()
