from __future__ import annotations
from typing import Any

import aiohttp
import asyncio
import logging
import typing as t

from ..internal.json import JSONObject, JSONArray, load_json
from .auth import Auth
from .errors import HTTPException, Unauthorized, Forbidden, NotFound, ServerError
from .ratelimit import Bucket, Lock
from .route import Route

__all__ = ("Route", "HTTPClient", "json_or_text",)

_log = logging.getLogger(__name__)

BASE_API_URL = "https://discord.com/api/v{0}"
API_VERSION = 10

def _get_user_agent():
    return f"DiscordBot (https://github.com/EmreTech/discord-api-wrapper, 1.0 Prototype)"

def _get_base_url():
    return BASE_API_URL.format(API_VERSION)

class HTTPClient:
    def __init__(self, default_auth: Auth, *, bucket_lag: float = 0.2):
        self._http: t.Optional[aiohttp.ClientSession] = None
        self._default_headers: dict[str, str] = {"User-Agent": _get_user_agent(), "Authorization": default_auth.header}
        self._base_url = _get_base_url()
        self._default_bucket_lag = bucket_lag
        self._local_to_discord: dict[str, str] = {}
        self._buckets: dict[str, Bucket] = {}
        self._global_lock: Lock = Lock()
        self._global_lock.set()

    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *_):
        await self.close()

    @property
    def http(self):
        if self._http is None:
            self._http = aiohttp.ClientSession(headers=self._default_headers)

        return self._http
    
    async def close(self):
        if self._http is None:
            return

        await self._http.close()

    @t.overload
    def _get_bucket(self, key: str, *, autocreate: t.Literal[True] = True) -> Bucket:
        pass

    @t.overload
    def _get_bucket(self, key: str, *, autocreate: t.Literal[False]) -> t.Optional[Bucket]:
        pass

    def _get_bucket(self, key: str, *, autocreate: bool = True):
        bucket = self._buckets.get(key)

        if not bucket and autocreate:
            bucket = Bucket(self._default_bucket_lag)
            self._buckets[key] = bucket

        return bucket

    async def request(
        self,
        route: Route,
        *, 
        json: t.Optional[JSONObject | JSONArray] = None,
        query: t.Optional[dict[str, str]] = None,
        headers: t.Optional[dict[str, str]] = None,
    ):
        if route.method == "GET" and json:
            raise TypeError("json parameter cannot be mixed with GET method!")

        params: dict[str, t.Any] = {}

        if json:
            params["json"] = json

        if query:
            params["params"] = query

        if headers:
            params["headers"] = headers

        local_bucket = route.bucket
        discord_hash = self._local_to_discord.get(local_bucket)

        key = local_bucket
        if discord_hash:
            key = f"{discord_hash}:{local_bucket}"

        bucket = self._get_bucket(key)

        MAX_RETRIES = 5

        for try_ in range(MAX_RETRIES):
            async with self._global_lock:
                _log.debug("The global lock has been acquired.")
                async with bucket:
                    _log.debug("The local bucket has been acquired.")
                    async with self.http.request(
                        route.method, 
                        self._base_url + route.formatted_url, 
                        **params
                    ) as resp:
                        bucket.update_from(resp)

                        if bucket.enabled:
                            if discord_hash != bucket.bucket:
                                discord_hash = bucket.bucket
                                key = f"{discord_hash}:{local_bucket}"
                                self._local_to_discord[local_bucket] = discord_hash

                                if (new_bucket := self._get_bucket(key, autocreate=False)):
                                    bucket = new_bucket
                                else:
                                    self._buckets[key] = bucket

                            await bucket.acquire()

                        if 300 > resp.status >= 200:
                            if resp.status == 204:
                                return

                            content = await resp.text()
                            return json_or_text(content, resp.content_type)

                        if resp.status == 429:
                            is_global = bool(resp.headers.get("X-RateLimit-Global", False))
                            retry_after = float(resp.headers["Retry-After"])

                            if is_global:
                                self._global_lock.lock_for(retry_after)
                                await self._global_lock.wait()
                            else:
                                bucket.lock_for(retry_after)
                                await bucket.acquire(auto_lock=False)

                            continue                                

                        if 500 > resp.status >= 400:
                            raw_content = await resp.text()
                            content = json_or_text(raw_content, resp.content_type)

                            if resp.status == 401:
                                raise Unauthorized(content)
                            if resp.status == 403:
                                raise Forbidden(content)
                            if resp.status == 404:
                                raise NotFound(content)

                            raise HTTPException(content, resp.status, resp.reason)

                        if 600 > resp.status >= 500:
                            if resp.status in (500, 502):
                                await asyncio.sleep(2 * try_ + 1)
                                continue
                            raise ServerError(None, resp.status, resp.reason)

        _log.error("Tried to make request to %s with method %s %d times.", route.formatted_url, route.method, MAX_RETRIES)

def json_or_text(content: str | None, content_type: str) -> str | JSONObject | JSONArray | None:
    content_type = content_type.lower()

    if content and content_type != "":
        if content_type == "application/json":
            return load_json(content)
        return content

    return None