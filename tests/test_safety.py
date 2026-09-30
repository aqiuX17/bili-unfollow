import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from app import create_app
from bili import Bili, BiliError
from core import Store, Engine, Conflict
from fastapi.testclient import TestClient


class FakeBili:
    def __init__(self):
        self.rows=[{'uid':str(i),'name':f'UP {i}','avatar':''} for i in range(2,67)]
        self.calls=[];self.error=None;self.list_error=False
        self.entered=None;self.release=None
    async def account(self,cookies):return {'uid':'1','name':'Test Account'}
    async def followings(self,cookies,uid):
        if self.list_error:raise BiliError('列表不完整')
        return [dict(r) for r in self.rows]
    async def unfollow(self,cookies,uid):
        self.calls.append(uid)
        if self.entered:
            self.entered.set();await self.release.wait()
        if self.error:raise self.error
        self.rows=[r for r in self.rows if r['uid']!=uid]
    async def close(self):pass
    async def qr(self):return {'url':'https://passport.bilibili.com/login?test=1','qrcode_key':'test-key'}
    async def poll(self,key):return {'code':0},{'SESSDATA':'private-value','bili_jct':'private-csrf'}


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.api=FakeBili()
        self.s.save_account({'uid':'1','name':'Test'},{'SESSDATA':'private','bili_jct':'secret'})
        self.e=Engine(self.s,self.api,interval=0)
    async def asyncTearDown(self):
        await self.e.close();self.s.db.close();self.tmp.cleanup()

    async def test_cross_page_whitelist_and_rename(self):
        await self.e.refresh();await self.e.whitelist('62',True)
        self.api.rows[-5]['name']='Renamed UP'
        p=await self.e.preview()
        self.assertNotIn('62',[r['uid'] for r in p['targets']])
        task=await self.e.start(p['id']);await self.e.worker
        self.assertNotIn('62',self.api.calls)
        self.assertEqual(self.s.task(task['id'])['status'],'completed')
        self.assertEqual(self.s.protected('1'),{'62'})

    async def test_incomplete_list_invalidates_old_snapshot(self):
        await self.e.refresh();self.api.list_error=True
        with self.assertRaises(BiliError):await self.e.preview()
        self.assertIsNone(self.s.get('snapshot'));self.assertEqual(self.api.calls,[])

    async def test_whitelist_change_invalidates_preview(self):
        p=await self.e.preview();await self.e.whitelist('2',True)
        with self.assertRaises(Conflict):await self.e.start(p['id'])
        self.assertEqual(self.api.calls,[])

    async def test_expired_preview(self):
        p=await self.e.preview()
        self.s.db.execute('UPDATE previews SET created=? WHERE id=?',(time.time()-601,p['id']))
        with self.assertRaises(Conflict):await self.e.start(p['id'])

    async def test_backend_rejects_forged_protected_target(self):
        await self.e.refresh();await self.e.whitelist('2',True);p=await self.e.preview()
        self.s.db.execute('UPDATE previews SET targets=? WHERE id=?',(json.dumps([self.api.rows[0]]),p['id']))
        with self.assertRaises(Conflict):await self.e.start(p['id'])

    async def test_duplicate_start_and_no_second_task(self):
        p=await self.e.preview();a=await self.e.start(p['id']);b=await self.e.start(p['id'])
        self.assertEqual(a['id'],b['id']);await self.e.worker
        self.assertEqual(len(self.api.calls),len(set(self.api.calls)))
        self.assertEqual(self.s.db.execute('select count(*) from tasks').fetchone()[0],1)

    async def test_timeout_pauses_without_retry_then_reconciles(self):
        self.api.error=BiliError('timeout',uncertain=True)
        p=await self.e.preview();t=await self.e.start(p['id']);await self.e.worker
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.s.task(t['id'])['items'][0]['status'],'unknown')
        # Remote operation actually took effect despite timeout.
        self.api.rows=self.api.rows[1:];self.api.error=None
        p2=await self.e.preview();self.assertNotIn('2',[r['uid'] for r in p2['targets']])
        await self.e.start(p2['id']);await self.e.worker
        self.assertEqual(self.api.calls.count('2'),1)

    async def test_login_expiry_and_risk_errors_pause(self):
        for message in ['登录失效','风控拒绝']:
            self.api.error=BiliError(message)
            p=await self.e.preview();t=await self.e.start(p['id']);await self.e.worker
            self.assertEqual(self.s.task(t['id'])['status'],'paused')
            self.assertEqual(self.s.task(t['id'])['items'][0]['status'],'failed')
        self.assertEqual(len(self.api.calls),2)

    async def test_pause_locks_whitelist_until_request_finishes(self):
        self.api.entered=asyncio.Event();self.api.release=asyncio.Event()
        p=await self.e.preview();t=await self.e.start(p['id']);await self.api.entered.wait()
        await self.e.pause(t['id'])
        with self.assertRaises(Conflict):await self.e.whitelist('3',True)
        self.api.release.set();await self.e.worker
        self.assertEqual(self.s.task(t['id'])['status'],'paused')
        await self.e.refresh();await self.e.whitelist('3',True)
        self.api.rows.append({'uid':'999','name':'New following','avatar':''})
        p2=await self.e.preview();ids={r['uid'] for r in p2['targets']}
        self.assertNotIn('3',ids);self.assertNotIn('999',ids);self.assertNotIn('2',ids)

    async def test_restart_marks_inflight_unknown_and_stays_paused(self):
        p=await self.e.preview();self.api.error=BiliError('timeout',uncertain=True)
        t=await self.e.start(p['id']);await self.e.worker
        self.s.db.execute("update items set status='inflight' where task_id=? and uid='2'",(t['id'],))
        self.s.state('running','',t['id']);self.s.db.close();self.s=Store(self.tmp.name)
        self.e.s=self.s
        self.assertEqual(self.s.task(t['id'])['status'],'paused')
        self.assertEqual(self.s.task(t['id'])['items'][0]['status'],'unknown')
        self.assertEqual(len(self.api.calls),1)

    async def test_final_verification_failure_is_not_success(self):
        original=self.api.unfollow
        async def op(cookies,uid):
            await original(cookies,uid)
            if not self.api.rows:self.api.list_error=True
        self.api.unfollow=op
        p=await self.e.preview();t=await self.e.start(p['id']);await self.e.worker
        result=self.s.task(t['id']);self.assertEqual(result['status'],'paused')
        self.assertEqual(result['counts'].get('confirmed',0),0)
        self.assertEqual(result['counts']['accepted'],65)

    async def test_credentials_encrypted_at_rest(self):
        self.assertNotIn('private',self.s.get('cookies'))
        self.assertEqual(self.s.cookies()['SESSDATA'],'private')
        self.assertEqual((Path(self.tmp.name)/'credentials.key').stat().st_mode & 0o777,0o600)

    async def test_random_interval_resampled_for_each_request(self):
        self.api.rows=self.api.rows[:2]
        self.e.interval=None
        p=await self.e.preview()
        with patch('core.random.uniform',side_effect=[0.1,2.0]) as sample, patch('core.asyncio.sleep',new_callable=AsyncMock) as sleep:
            t=await self.e.start(p['id']);await self.e.worker
            self.assertEqual(sample.call_count,2)
            self.assertEqual(sample.call_args_list[0].args,(0.1,2.0))
            self.assertEqual([c.args[0] for c in sleep.await_args_list],[0.1,2.0])
        self.assertEqual(self.s.task(t['id'])['status'],'completed')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_paging_deduplicates_and_detects_truncation(self):
        api=Bili();await api.client.aclose()
        seen=[]
        async def handler(request):
            page=int(request.url.params['pn']);seen.append(page)
            rows=[{'mid':i,'uname':str(i)} for i in (range(1,51) if page==1 else [51])]
            return httpx.Response(200,json={'code':0,'data':{'total':51,'list':rows}})
        api.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows=await api.followings({},'1');self.assertEqual(len(rows),51);self.assertEqual(seen,[1,2])
        await api.close()
        api=Bili();await api.client.aclose()
        api.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':0,'data':{'total':51,'list':[{'mid':1,'uname':'one'}]}})))
        with self.assertRaises(BiliError):await api.followings({},'1')
        await api.close()

    async def test_total_changes_abort(self):
        api=Bili();await api.client.aclose()
        def handler(r):
            page=int(r.url.params['pn'])
            return httpx.Response(200,json={'code':0,'data':{'total':51 if page==1 else 52,'list':[{'mid':i} for i in range(1,51)]}})
        api.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(BiliError):await api.followings({},'1')
        await api.close()

    async def test_qr_cookies_are_parsed_without_returning_secrets_to_browser(self):
        api=Bili();await api.client.aclose()
        api.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':0,'data':{'code':0}},headers=[('set-cookie','SESSDATA=private; Path=/; HttpOnly'),('set-cookie','bili_jct=csrf; Path=/')])))
        data,cookies=await api.poll('key');self.assertEqual(cookies['SESSDATA'],'private');await api.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=FakeBili()
        self.app=create_app(self.tmp.name,self.api,interval=0)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:22330');self.client.__enter__()
        self.origin={'Origin':'http://127.0.0.1:22330'}
    def tearDown(self):
        self.client.__exit__(None,None,None);self.tmp.cleanup()
    def login(self):
        password=(Path(self.tmp.name)/'admin-password').read_text().strip()
        r=self.client.post('/api/admin/login',json={'password':password},headers=self.origin)
        self.assertEqual(r.status_code,200)
        return dict(self.origin,**{'X-CSRF-Token':r.json()['csrf']})

    def test_authentication_origin_csrf_and_cookie_flags(self):
        self.assertEqual(self.client.get('/api/state').status_code,401)
        headers=self.login()
        self.assertEqual(self.client.get('/api/state').status_code,200)
        self.assertEqual(self.client.post('/api/preview',json={},headers=self.origin).status_code,403)
        self.assertEqual(self.client.post('/api/preview',json={},headers=dict(headers,Origin='https://evil.example')).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Host':'evil.example'}).status_code,400)
        self.assertIn('frame-ancestors',self.client.get('/').headers['content-security-policy'])

    def test_scan_search_and_whitelist_persistence(self):
        h=self.login()
        r=self.client.post('/api/bili/qr',json={},headers=h);self.assertEqual(r.status_code,200)
        self.assertTrue(r.json()['image'].startswith('data:image/svg+xml;base64,'))
        r=self.client.post('/api/bili/poll',json={},headers=h);self.assertEqual(r.json()['status'],'success')
        self.assertNotIn('SESSDATA',r.text)
        self.client.post('/api/followings/refresh',json={},headers=h)
        r=self.client.get('/api/followings?page=3');self.assertEqual(len(r.json()['rows']),5)
        self.assertEqual(self.client.get('/api/followings?q=UP%2062').json()['count'],1)
        r=self.client.post('/api/whitelist',json={'uid':'62','keep':True},headers=h);self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/followings?white_only=true').json()['rows'][0]['uid'],'62')
        self.assertEqual(self.client.post('/api/bili/logout',json={},headers=h).status_code,200)
        self.assertIsNone(self.app.state.engine.s.get('cookies'))
        self.assertEqual(self.app.state.engine.s.protected('1'),{'62'})


if __name__=='__main__':unittest.main()
