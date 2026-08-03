from fastapi import Request, Response
from jose import jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


class AuditUserMiddleware(BaseHTTPMiddleware):
    """Extract the authenticated user ID from the JWT cookie and store it in
    ``request.state.audit_user_id``.  The ``get_db`` dependency will then
    execute ``SET LOCAL app.current_user_id`` so that DB audit triggers can
    record who made the change.
    """

    async def dispatch(self, request: Request, call_next):
        user_id = None
        try:
            token = request.cookies.get("access_token")
            if token and token.startswith("Bearer "):
                payload = jwt.decode(
                    token.split(" ")[1],
                    settings.SECRET_KEY,
                    algorithms=[settings.ALGORITHM],
                )
                if payload.get("type") == "access" and payload.get("sub"):
                    user_id = payload["sub"]
        except Exception:
            pass
        request.state.audit_user_id = user_id
        response = await call_next(request)
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    EXEMPT_PATHS = {"/auth/login", "/sso/callback", "/sso/login", "/tasks/"}

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            request_path = request.url.path

            is_exempt = any(
                path in request_path for path in self.EXEMPT_PATHS
            )

            if not is_exempt:
                csrf_token_header = request.headers.get("X-CSRF-Token")
                csrf_token_cookie = request.cookies.get("csrf_token")

                if (
                    not csrf_token_header
                    or not csrf_token_cookie
                    or not secrets_compare(csrf_token_header, csrf_token_cookie)
                ):
                    return Response(content="CSRF Token Mismatch", status_code=403)

        response = await call_next(request)
        return response


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiter for authentication endpoints."""

    RATE_LIMIT_PATHS = {"/auth/login"}
    MAX_REQUESTS = 10
    WINDOW_SECONDS = 60

    def __init__(self, app):
        super().__init__(app)
        self._requests: dict = {}

    async def dispatch(self, request: Request, call_next):
        request_path = request.url.path
        is_rate_limited = any(
            request_path.endswith(path) for path in self.RATE_LIMIT_PATHS
        )

        if is_rate_limited and request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            now = __import__("time").time()

            if client_ip not in self._requests:
                self._requests[client_ip] = []

            self._requests[client_ip] = [
                t for t in self._requests[client_ip]
                if now - t < self.WINDOW_SECONDS
            ]

            if len(self._requests[client_ip]) >= self.MAX_REQUESTS:
                return Response(
                    content="Too many requests. Please try again later.",
                    status_code=429,
                )

            self._requests[client_ip].append(now)

        response = await call_next(request)
        return response
