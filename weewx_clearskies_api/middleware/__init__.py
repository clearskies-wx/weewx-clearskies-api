"""ASGI middleware stack.

Middleware execution order (outermost → innermost) as registered in app.py:
  1. RequestIdMiddleware      — generates UUID per request, stashes in ContextVar for logging
  2. BodySizeLimitMiddleware  — rejects >max_request_bytes before body is consumed
  3. ProxyAuthMiddleware      — optional X-Clearskies-Proxy-Auth constant-time compare (ADR-008);
                                sets request.state.proxy_trusted = True on success
  4. RateLimitMiddleware      — per-IP quota; bypassed when proxy_trusted is True
  5. CORSMiddleware           — same-origin default; operator-configured additional origins
  6. SecurityHeadersMiddleware — X-Content-Type-Options, Referrer-Policy, suppress Server:

Order is canonical in app.py. This comment is kept in sync — do not reorder
app.py without updating both files. The proxy-auth / rate-limit ordering is
load-bearing: ProxyAuthMiddleware (step 3) MUST execute before RateLimitMiddleware
(step 4) so the trusted bypass flag is set before the quota check.
"""
