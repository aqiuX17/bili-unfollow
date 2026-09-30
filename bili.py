"""Direct Bilibili adapter. Never logs URLs containing login keys or cookies."""
import math
from http.cookies import SimpleCookie
import httpx


class BiliError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class Bili:
    def __init__(self):
        self.client = httpx.AsyncClient(
            trust_env=False, timeout=20, follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method, url, cookies=None, **kwargs):
        # Explicit Cookie header prevents shared httpx cookie jar mixing account credentials.
        kwargs.setdefault("headers", {})["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in (cookies or {}).items()
        )
        try:
            r = await self.client.request(method, url, **kwargs)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise BiliError("网络超时或接口异常；结果可能未确认，请暂停后核对", uncertain=True) from None
        if data.get("code") != 0:
            code = data.get("code")
            if code in (-101, -111):
                raise BiliError(f"登录失效或凭据无效（{code}），请重新扫码")
            raise BiliError(f"B 站拒绝请求（{code}）；可能触发风控，已暂停")
        return data.get("data"), r

    async def qr(self):
        data, _ = await self.request("GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        return data

    async def poll(self, key):
        data, r = await self.request("GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/poll", params={"qrcode_key": key})
        cookies = {}
        for h in r.headers.get_list("set-cookie"):
            c = SimpleCookie(); c.load(h)
            cookies.update({k: v.value for k, v in c.items()})
        return data, cookies

    async def account(self, cookies):
        data, _ = await self.request("GET", "https://api.bilibili.com/x/web-interface/nav", cookies)
        if not data or not data.get("isLogin"):
            raise BiliError("账号未登录，请重新扫码")
        return {"uid": str(data["mid"]), "name": data["uname"]}

    async def followings(self, cookies, uid):
        all_rows, total = {}, None
        for page in range(1, 1001):
            data, _ = await self.request("GET", "https://api.bilibili.com/x/relation/followings", cookies,
                params={"vmid": uid, "pn": page, "ps": 50, "order": "desc", "order_type": "attention"})
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise BiliError("关注列表响应不完整，禁止执行")
            current_total = int(data["total"])
            if total is None: total = current_total
            if total != current_total:
                raise BiliError("读取期间关注数量发生变化，请重新加载")
            before = len(all_rows)
            for up in data["list"]:
                mid = str(up["mid"])
                all_rows[mid] = {"uid": mid, "name": str(up.get("uname", mid)), "avatar": str(up.get("face", ""))}
            if page >= max(1, math.ceil(total / 50)):
                if len(all_rows) != total:
                    raise BiliError(f"关注列表不完整：获取 {len(all_rows)} / {total}，禁止执行")
                return list(all_rows.values())
            if len(all_rows) == before:
                raise BiliError("关注列表分页受限或重复，禁止执行")
        raise BiliError("关注列表超过可处理范围，禁止执行")

    async def unfollow(self, cookies, uid):
        if not cookies.get("bili_jct"):
            raise BiliError("缺少登录凭据，请重新扫码")
        await self.request("POST", "https://api.bilibili.com/x/relation/modify", cookies,
            data={"fid": uid, "act": 2, "csrf": cookies["bili_jct"], "re_src": 11})
