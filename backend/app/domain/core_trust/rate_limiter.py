"""
Rate Limiting Middleware
=====================
In-memory rate limiter for public endpoints.
Uses sliding window algorithm per IP address.
"""

import time
from collections import defaultdict
from typing import Callable
from fastapi import Request, HTTPException, Depends
from fastapi.responses import JSONResponse
import logging

log = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, requests_per_minute: int = 60, burst: int = 10):
        self.requests_per_minute = requests_per_minute
        self.burst = burst
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._window_size = 60.0
    
    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    
    def _cleanup_window(self, client_id: str, now: float) -> None:
        cutoff = now - self._window_size
        self._windows[client_id] = [t for t in self._windows[client_id] if t > cutoff]
    
    def is_allowed(self, request: Request) -> tuple[bool, dict]:
        now = time.time()
        client_id = self._get_client_ip(request)
        self._cleanup_window(client_id, now)
        
        window = self._windows[client_id]
        
        if len(window) >= self.requests_per_minute:
            oldest = window[0]
            wait_time = self._window_size - (now - oldest)
            return False, {"retry_after": int(wait_time)}
        
        window.append(now)
        return True, {}
    
    def __call__(self, request: Request) -> JSONResponse:
        allowed, info = self.is_allowed(request)
        if not allowed:
            log.warning("rate_limit_exceeded", client_ip=self._get_client_ip(request))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "retry_after": info.get("retry_after", 60)
                }
            )
        return None

_limiter = RateLimiter(requests_per_minute=60, burst=10)

def get_rate_limiter() -> RateLimiter:
    return _limiter

async def check_rate_limit(request: Request, limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    allowed, info = limiter.is_allowed(request)
    if not allowed:
        log.warning("rate_limit_exceeded", client_ip=request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded", 
                "retry_after": info.get("retry_after", 60)
            }
        )