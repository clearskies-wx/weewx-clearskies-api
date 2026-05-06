"""Configuration loading (ADR-027).

ConfigObj/INI from /etc/weewx-clearskies/api.conf
(path overridable via CLEARSKIES_CONFIG env var).

Secrets from /etc/weewx-clearskies/secrets.env loaded as env vars before
this module is imported. The operator is responsible for mode 0600 on that file.
"""
