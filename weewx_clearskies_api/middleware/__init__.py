"""ASGI middleware stack.

Middleware execution order (outermost → innermost) as registered in app.py:
  0. MetricsMiddleware        — outermost; times total request duration for ADR-031 metrics
  1. RequestIdMiddleware      — generates UUID per request, stashes in ContextVar for logging
  2. BodySizeLimitMiddleware  — rejects >max_request_bytes before body is consumed
  3. ProxyAuthMiddleware      — optional X-Clearskies-Proxy-Auth constant-time compare (ADR-008);
                                sets request.state.proxy_trusted = True on success
  4. CORSMiddleware           — same-origin default; operator-configured additional origins
  5. SecurityHeadersMiddleware — X-Content-Type-Options, Referrer-Policy, suppress Server:

Order is canonical in app.py. This comment is kept in sync — do not reorder
app.py without updating both files.

MetricsMiddleware (step 0) is registered outermost so request timing includes
the full processing time through all inner middleware.
"""
