"""Nearshore wave model provider modules.

Active only when the ``[nearshore]`` pip extra is installed (eccodes or pygrib
present for GRIB2 processing).  The SWAN provider
(``providers/nearshore/swan.py``) is a thin wrapper around
``services/swan_runner.py`` — it orchestrates a locally-run SWAN subprocess
instead of making a network call.
"""
