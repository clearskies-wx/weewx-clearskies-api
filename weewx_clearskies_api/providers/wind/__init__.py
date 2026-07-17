"""Wind provider modules.

Active only when the ``[nearshore]`` pip extra is installed.  Both wind
providers are invoked by the SWAN runner (``services/swan_runner.py``), not
by the standard cache-warmer registry.

Providers
---------
hrrr (``providers/wind/hrrr.py``)
    NOAA HRRR 10m AGL wind forecast from NOMADS Grib Filter.  Covers forecast
    hours 0–48 (extended 00/06/12/18Z cycles) for the CONUS domain.  Lambert
    Conformal Conic grid — requires wind rotation from grid-relative to
    earth-relative.  See PROVIDER-MANUAL §14.14.

gfs (``providers/wind/gfs.py``)
    NOAA GFS 0.25° 10m AGL wind forecast from NOMADS Grib Filter.  Fetches
    forecast hours 48–72 (3-hourly steps) to supplement HRRR beyond its
    48-hour range, enabling the 72-hour surf forecast card.  Regular lat-lon
    grid — winds are already earth-relative, no rotation required.
    See PROVIDER-MANUAL §14.16.
"""
