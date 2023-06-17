import aiohttp
import asyncio
import datetime
import typing as t

__all__ = ("BucketMigrated", "Bucket", "GlobalBucket",)

class BaseBucket:
    def __init__(self, lag: float = 0.2):
        self.lag: float = lag

        self.bucket: str = ""
        self.reset_after: float = 0.0
        self._lock: asyncio.Event = asyncio.Event()
        self._lock.set()

    def update_from(self, response: aiohttp.ClientResponse):
        pass

    def lock_for(self, time: float):
        if not self._lock.is_set():
            return

        print(f"Bucket {self.bucket} will be locked for {time} seconds.")
        self._lock.clear()
        asyncio.create_task(self._unlock(time))

    async def _unlock(self, time: float):
        await asyncio.sleep(time)
        print(f"Bucket {self.bucket} will be unlocked.")
        self._lock.set()

    async def acquire(self):
        await self._lock.wait()
        # log debug "Bucket {self.bucket} has been acquired!"

    async def __aenter__(self):
        await self.acquire()
        return self
    
    async def __aexit__(self, *_):
        pass

class BucketMigrated(Exception):
    def __init__(self, old: str, new: str):
        self.old: str = old
        self.new: str = new
        super().__init__(f"Bucket {old} has migrated to {new}.")

class Bucket(BaseBucket):
    def __init__(self, lag: float = 0.2):
        super().__init__(lag)

        self.limit: int = 1
        self.remaining: int = 1
        self.reset: t.Optional[datetime.datetime] = None
        self.bucket: str = ""
        self.enabled: bool = True

    def update_from(self, response: aiohttp.ClientResponse):
        headers = response.headers

        x_bucket: t.Optional[str] = headers.get("X-RateLimit-Bucket")
        if x_bucket is None:
            # log debug "Ratelimiting is not supported for this bucket."
            self.enabled = False
            return
        
        if self.bucket == "":
            self.bucket = x_bucket
        elif self.bucket != x_bucket:
            self.migrate_to(x_bucket)

        # from here on, the route has ratelimits

        if headers.get("X-RateLimit-Global", False):
            # log debug "This ratelimit is globally applied."
            return
        
        x_limit: int = int(headers["X-RateLimit-Limit"])
        if x_limit != self.limit:
            self.limit = x_limit

        x_remaining: int = int(headers.get("X-RateLimit-Remaining", 1))
        if x_remaining < self.remaining:
            self.remaining = x_remaining

        x_reset: datetime.datetime = datetime.datetime.fromtimestamp(float(headers["X-RateLimit-Reset"]))
        if x_reset != self.reset:
            self.reset = x_reset

        x_reset_after: float = float(headers["X-RateLimit-Reset-After"])
        if x_reset_after > self.reset_after:
            self.reset_after = x_reset_after
            self.reset_after += self.lag

    def migrate_to(self, new: str):
        self.enabled = False
        raise BucketMigrated(self.bucket, new)

    async def acquire(self):
        if self.remaining == 0:
            # log debug "Bucket {self.bucket} will be auto-locked."
            self.lock_for(self.reset_after)
            # prevent the bucket from being locked again until after we actually make a request
            self.remaining = 1

        await super().acquire()

class GlobalBucket(BaseBucket):
    def __init__(self, lag: float = 0.2):
        super().__init__(lag)

        self.bucket = "GLOBAL"

    def update_from(self, response: aiohttp.ClientResponse):
        headers = response.headers

        if not headers.get("X-RateLimit-Global", False):
            # log debug "This ratelimit is not globally applied."
            return

        self.reset_after: float = float(headers["Retry-After"]) + self.lag
