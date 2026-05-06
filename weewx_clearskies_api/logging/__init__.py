"""Logging setup (ADR-029).

JSON one-line-per-record to stdout. Stdlib logging + custom JsonFormatter.
RedactionFilter strips Authorization, X-Clearskies-Proxy-Auth, appid,
client_secret, and SQL parameter values from every log record.
"""
