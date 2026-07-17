"""Wind provider modules.

Active only when the ``[nearshore]`` pip extra is installed.  The HRRR wind
provider (``providers/wind/hrrr.py``) is invoked by the SWAN runner
(``services/swan_runner.py``), not by the standard cache-warmer registry.
"""
