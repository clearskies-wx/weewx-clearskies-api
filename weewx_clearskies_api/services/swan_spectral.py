"""SWAN SPECOUT file parser and 2D spectral decomposition (T3.3).

Parses SWAN ASCII SPECOUT files (written by `SPECOUT 'sname' SPEC2D ABS 'fname'`)
and decomposes the 2-D directional-frequency spectrum into individual swell systems
by finding energy peaks in (frequency, direction) space.

Output matches the SpectralWaveComponent schema used by the surf endpoint
(API-MANUAL §17, models/responses.py).

SWAN SPECOUT ASCII format (reference: swan-commands-extract.md, SWAN appendix D):
  Line 1: "SWAN   1" version header
  Comment lines starting with "$"
  "LOCATIONS" section with n output locations and (lon, lat) per location
  "AFREQ  nf" — nf frequency bins in Hz
  "NDIR  nd" — nd direction bins in degrees
  "QUANT" section with quantity metadata
  Per-timestep blocks:
    Timestamp line (YYYYMMDD.HHmmss)
    "FACTOR" / "NODATA"
    Scaling factor (if FACTOR)
    Data matrix: nf rows × nd columns (scaled variance density)

The output from SWAN uses `SPEC2D ABS` so frequencies are absolute.
Energy density units are m²/Hz/deg (variance density per frequency per direction).

Swell classification thresholds (from plan T3.3, SpectralWaveComponent model):
  groundswell: T > 12.5 s  (f < 0.08 Hz)
  swell:       10 ≤ T ≤ 12.5 s  (0.08 ≤ f ≤ 0.1 Hz)
  wind_swell:  T < 10 s    (f > 0.1 Hz)

References:
  - SWAN-CORRECTIONS-PLAN.md §T3.3
  - models/responses.py SpectralWaveComponent
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

_GROUNDSWELL_PERIOD_S = 12.5  # T > 12.5s → groundswell
_SWELL_PERIOD_MIN_S = 10.0    # 10 ≤ T ≤ 12.5s → swell
# T < 10s → wind_swell


def _classify_period(period_s: float) -> str:
    """Classify a wave period into swell type per SpectralWaveComponent schema."""
    if period_s >= _GROUNDSWELL_PERIOD_S:
        return "groundswell"
    if period_s >= _SWELL_PERIOD_MIN_S:
        return "swell"
    return "wind_swell"


# ---------------------------------------------------------------------------
# SPECOUT file parser
# ---------------------------------------------------------------------------


def parse_specout_file(text: str) -> list[dict[str, Any]]:
    """Parse a SWAN SPECOUT ASCII file and return per-timestep spectra.

    Args:
        text: Full content of a SWAN SPECOUT ASCII file.

    Returns:
        List of dicts, one per output timestep:
          {
            "time":       ISO-8601 UTC string,
            "freqs_hz":   list[float] — frequency axis (Hz),
            "dirs_deg":   list[float] — direction axis (degrees nautical),
            "energy":     list[list[float]] — E[i_freq][i_dir] in m²/Hz/deg,
          }
        Returns an empty list if the file cannot be parsed or contains no data.
    """
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    if not lines:
        return []

    freqs_hz: list[float] = []
    dirs_deg: list[float] = []
    results: list[dict[str, Any]] = []

    i = 0
    n_lines = len(lines)

    def _skip_comments_and_blanks() -> None:
        nonlocal i
        while i < n_lines and (lines[i].startswith("$") or not lines[i].strip()):
            i += 1

    def _read_count_on_next_line() -> int:
        """Read an integer count from the next non-comment line.

        SWAN standard spectral file format: keyword + description on the
        keyword line, count on the NEXT line.  E.g.:
            AFREQ                       absolute frequencies in Hz
                32                      number of frequencies
        """
        nonlocal i
        i += 1
        _skip_comments_and_blanks()
        if i >= n_lines:
            return 0
        try:
            count = int(lines[i].split()[0])
        except (ValueError, IndexError):
            count = 0
        i += 1
        return count

    # ---- Parse header sections ----
    # SWAN standard spectral file layout (Appendix D):
    #   SWAN 1  ...           version line
    #   $ comments
    #   TIME                  (keyword + description)
    #        1                (time coding option)
    #   LONLAT                (keyword + description)
    #        1                (number of locations)
    #   lon  lat              (one per location)
    #   AFREQ                 (keyword + description)
    #       32                (number of frequencies)
    #   f1 ... f32            (one per line)
    #   NDIR                  (keyword + description)
    #       36                (number of directions)
    #   d1 ... d36            (one per line)
    #   QUANT
    #        1                (number of quantities)
    #   VaDens                (quantity name)
    #   m2/Hz/degr            (unit)
    #   -0.99E+02             (exception value)
    #   YYYYMMDD.HHmmss       (first timestep)
    #   FACTOR / NODATA
    #   ...
    while i < n_lines:
        _skip_comments_and_blanks()
        if i >= n_lines:
            break
        tok = lines[i].split()
        if not tok:
            i += 1
            continue

        keyword = tok[0].upper()

        if keyword == "AFREQ":
            nf = _read_count_on_next_line()
            for _ in range(nf):
                _skip_comments_and_blanks()
                if i < n_lines:
                    try:
                        freqs_hz.append(float(lines[i].split()[0]))
                    except (ValueError, IndexError):
                        pass
                    i += 1
            continue

        if keyword == "NDIR":
            nd = _read_count_on_next_line()
            for _ in range(nd):
                _skip_comments_and_blanks()
                if i < n_lines:
                    try:
                        dirs_deg.append(float(lines[i].split()[0]))
                    except (ValueError, IndexError):
                        pass
                    i += 1
            continue

        if keyword in ("LONLAT", "LOCATIONS"):
            nloc = _read_count_on_next_line()
            for _ in range(nloc):
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
            continue

        if keyword == "TIME":
            i += 1
            _skip_comments_and_blanks()
            if i < n_lines:
                i += 1
            continue

        if keyword == "QUANT":
            nq = _read_count_on_next_line()
            for _ in range(nq):
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
            continue

        if keyword in ("SWAN", "NODATA"):
            i += 1
            continue

        # Timestamp line: 8 digits (date) + "." + 6 digits (time) = 15 chars
        stripped = lines[i].strip()
        if len(stripped) >= 15 and stripped[8:9] == "." and stripped[:8].isdigit() and stripped[9:15].isdigit():
            # This is a timestep block
            timestamp = stripped[:15]
            # Convert YYYYMMDD.HHmmss → ISO-8601
            try:
                iso = (
                    f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
                    f"T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
                )
            except IndexError:
                i += 1
                continue

            i += 1
            _skip_comments_and_blanks()

            if i >= n_lines:
                break

            line_upper = lines[i].strip().upper()
            if line_upper == "NODATA":
                i += 1
                continue

            if line_upper == "FACTOR":
                i += 1
                _skip_comments_and_blanks()
                # Next line is the scaling factor
                try:
                    factor = float(lines[i].split()[0])
                except (ValueError, IndexError):
                    factor = 1.0
                i += 1
            else:
                factor = 1.0

            # Read energy matrix: nf rows × nd columns
            if not freqs_hz or not dirs_deg:
                # Can't parse data without axis info — skip
                logger.warning("SWAN SPECOUT: timestep %s encountered before AFREQ/NDIR", timestamp)
                i += 1
                continue

            nf = len(freqs_hz)
            nd = len(dirs_deg)
            energy: list[list[float]] = []
            rows_read = 0

            while rows_read < nf and i < n_lines:
                _skip_comments_and_blanks()
                if i >= n_lines:
                    break
                row_vals = lines[i].split()
                if not row_vals:
                    i += 1
                    continue

                # Each row should have nd values.  Sometimes all nd values are
                # on one line; sometimes they span multiple lines.  Collect
                # until we have nd values for this frequency bin.
                row_floats: list[float] = []
                while len(row_floats) < nd and i < n_lines:
                    for v in lines[i].split():
                        try:
                            row_floats.append(float(v) * factor)
                        except ValueError:
                            pass
                        if len(row_floats) >= nd:
                            break
                    if len(row_floats) >= nd:
                        break
                    i += 1

                energy.append(row_floats[:nd])
                rows_read += 1
                i += 1

            if len(energy) == nf and all(len(row) == nd for row in energy):
                results.append({
                    "time": iso,
                    "freqs_hz": list(freqs_hz),
                    "dirs_deg": list(dirs_deg),
                    "energy": energy,
                })
            else:
                logger.warning(
                    "SWAN SPECOUT: incomplete energy matrix at %s "
                    "(got %d/%d rows)", iso, len(energy), nf,
                )
            continue

        i += 1

    return results


class SpecoutParseError(Exception):
    """Raised by ``parse_specout_file_multi()`` when a SPECOUT file's
    declared location count does not match the number of per-location
    spectral blocks actually found for some timestep.

    Deliberately fails loudly rather than returning a short list: a parser
    that silently keeps only the first N-of-M locations reintroduces the
    defect this function exists to fix — see the module-level note on
    ``parse_specout_file_multi()`` (Phase 4A T4A.9).
    """


@dataclass
class MultiLocationSpecout:
    """Result of ``parse_specout_file_multi()`` (Phase 4A T4A.9).

    Carries station coordinates alongside the spectra so callers can align a
    SPECOUT file's stations against another file's stations (e.g. the
    matching ``TABLE_n.txt``'s XP/YP columns) BY COORDINATE rather than by
    trusting that both files iterate the same named CURVE in the same index
    order. The coordinator flagged this as the highest-risk part of the
    per-hour handoff mechanism: an unverified index-order assumption between
    the TABLE (depth/QB) and SPECOUT (spectrum) parses would silently pair a
    spectrum from one station with the depth/QB of a different one — a
    passing-looking, wrong result, the exact shape T4A.10 exists to catch.
    """

    station_lonlat: list[tuple[float, float]]
    """(lon, lat) per station, 0-based, in the file's LONLAT/LOCATIONS
    declaration order — the same order ``station_timesteps`` is indexed by."""

    station_timesteps: list[list[dict[str, Any]]]
    """Outer index = station (matches ``station_lonlat``), inner index =
    timestep in file order. Same per-timestep dict shape as
    ``parse_specout_file()``: ``{"time", "freqs_hz", "dirs_deg", "energy"}``."""


def parse_specout_file_multi(text: str) -> MultiLocationSpecout:
    """Parse a MULTI-location SWAN SPECOUT ASCII file (Phase 4A T4A.9).

    ``parse_specout_file()`` above assumes exactly one output location per
    file — correct for the L2 deep-water reference SPECOUT (a genuine
    single-point emission) but wrong for the L3 per-hour handoff mechanism,
    which emits ``SPECOUT 'CVn' SPEC2D ABS`` on the spot's full cross-shore
    CURVE (multiple stations) so a station can be selected per forecast hour
    without moving any grid geometry (ADR-093 Amendment 2 §2; C2).

    Per the SWAN 41.51 User Manual, Appendix D "Formal description of the
    1D- and 2D-spectral file" (verified on librewxr, `/tmp/swanuse.txt`):
    for a 2D spectrum, each timestep's table repeats, **for each location in
    the order declared under LONLAT/LOCATIONS**, one of:
      - ``FACTOR`` <scale> <nf x nd energy matrix>
      - ``ZERO`` (spectrum is identically zero — no matrix follows)
      - ``NODATA`` (spectrum undefined, e.g. on land — no matrix follows)
    There is no per-location index marker for 2D spectra (that only exists
    for 1D spectra, under the keyword ``LOCATION <n>``) — location identity
    is purely positional, so a station's index in this function's output
    must be read against the SAME point order the CURVE was emitted with.

    This function is intentionally NOT unified with ``parse_specout_file()``
    despite the header-parsing overlap: the lead's explicit constraint on
    this rewrite is that the L2 single-location path must keep working
    unchanged, and the surest way to guarantee that is to not touch it at
    all. See ``parse_specout_file()`` for the single-location parser.

    Args:
        text: Full content of a SWAN multi-location SPECOUT ASCII file.

    Returns:
        ``MultiLocationSpecout`` with ``station_lonlat`` (coordinates, for
        aligning against another file's stations by position rather than by
        trusting index order) and ``station_timesteps`` (the spectra). A
        station with ``ZERO``/``NODATA`` for a given timestep gets an
        all-zero energy matrix (of the correct shape) rather than a missing
        entry, so every station's list stays aligned to the same timestep
        index across all stations.

    Raises:
        SpecoutParseError: the LONLAT/LOCATIONS location count could not be
            determined, or some timestep did not contain exactly that many
            per-location blocks. Never returns a partial/truncated result.
    """
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    if not lines:
        raise SpecoutParseError("parse_specout_file_multi: empty file")

    freqs_hz: list[float] = []
    dirs_deg: list[float] = []
    n_locations: int | None = None
    station_lonlat: list[tuple[float, float]] = []
    # results[station_idx] = list of per-timestep dicts, station order fixed
    # once n_locations is known.
    results: list[list[dict[str, Any]]] = []

    i = 0
    n_lines = len(lines)

    def _skip_comments_and_blanks() -> None:
        nonlocal i
        while i < n_lines and (lines[i].startswith("$") or not lines[i].strip()):
            i += 1

    def _read_count_on_next_line() -> int:
        nonlocal i
        i += 1
        _skip_comments_and_blanks()
        if i >= n_lines:
            return 0
        try:
            count = int(lines[i].split()[0])
        except (ValueError, IndexError):
            count = 0
        i += 1
        return count

    while i < n_lines:
        _skip_comments_and_blanks()
        if i >= n_lines:
            break
        tok = lines[i].split()
        if not tok:
            i += 1
            continue

        keyword = tok[0].upper()

        if keyword in ("LONLAT", "LOCATIONS"):
            n_locations = _read_count_on_next_line()
            for _ in range(n_locations):
                _skip_comments_and_blanks()
                if i < n_lines:
                    coord_tok = lines[i].split()
                    try:
                        station_lonlat.append((float(coord_tok[0]), float(coord_tok[1])))
                    except (ValueError, IndexError):
                        station_lonlat.append((float("nan"), float("nan")))
                    i += 1
            results = [[] for _ in range(n_locations)]
            continue

        if keyword == "AFREQ":
            nf = _read_count_on_next_line()
            for _ in range(nf):
                _skip_comments_and_blanks()
                if i < n_lines:
                    try:
                        freqs_hz.append(float(lines[i].split()[0]))
                    except (ValueError, IndexError):
                        pass
                    i += 1
            continue

        if keyword == "NDIR":
            nd = _read_count_on_next_line()
            for _ in range(nd):
                _skip_comments_and_blanks()
                if i < n_lines:
                    try:
                        dirs_deg.append(float(lines[i].split()[0]))
                    except (ValueError, IndexError):
                        pass
                    i += 1
            continue

        if keyword == "TIME":
            i += 1
            _skip_comments_and_blanks()
            if i < n_lines:
                i += 1
            continue

        if keyword == "QUANT":
            nq = _read_count_on_next_line()
            for _ in range(nq):
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
                _skip_comments_and_blanks()
                if i < n_lines:
                    i += 1
            continue

        if keyword == "SWAN":
            i += 1
            continue

        stripped = lines[i].strip()
        if (
            len(stripped) >= 15
            and stripped[8:9] == "."
            and stripped[:8].isdigit()
            and stripped[9:15].isdigit()
        ):
            # Timestep block.
            if n_locations is None:
                raise SpecoutParseError(
                    "parse_specout_file_multi: timestep block encountered "
                    "before LONLAT/LOCATIONS — cannot determine location "
                    "count"
                )
            if not freqs_hz or not dirs_deg:
                raise SpecoutParseError(
                    "parse_specout_file_multi: timestep block encountered "
                    "before AFREQ/NDIR"
                )

            timestamp = stripped[:15]
            iso = (
                f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
                f"T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
            )
            i += 1
            _skip_comments_and_blanks()

            nf = len(freqs_hz)
            nd = len(dirs_deg)

            for loc_idx in range(n_locations):
                if i >= n_lines:
                    raise SpecoutParseError(
                        f"parse_specout_file_multi: timestep {iso} ended "
                        f"after {loc_idx}/{n_locations} location block(s) — "
                        "file truncated"
                    )
                _skip_comments_and_blanks()
                block_kw = lines[i].strip().upper()

                if block_kw in ("NODATA", "ZERO"):
                    # No matrix follows for either keyword (Appendix D: NODATA
                    # explicitly has none; ZERO is a spectrum identically 0 —
                    # treated the same way here rather than risk consuming
                    # the NEXT location's FACTOR line as if it were data).
                    i += 1
                    results[loc_idx].append({
                        "time": iso,
                        "freqs_hz": list(freqs_hz),
                        "dirs_deg": list(dirs_deg),
                        "energy": [[0.0] * nd for _ in range(nf)],
                    })
                    continue

                if block_kw != "FACTOR":
                    raise SpecoutParseError(
                        f"parse_specout_file_multi: timestep {iso} location "
                        f"{loc_idx}/{n_locations}: expected FACTOR/ZERO/"
                        f"NODATA, got {lines[i]!r}"
                    )

                i += 1
                _skip_comments_and_blanks()
                try:
                    factor = float(lines[i].split()[0])
                except (ValueError, IndexError) as exc:
                    raise SpecoutParseError(
                        f"parse_specout_file_multi: timestep {iso} location "
                        f"{loc_idx}/{n_locations}: could not parse FACTOR "
                        f"value from {lines[i]!r}"
                    ) from exc
                i += 1

                energy: list[list[float]] = []
                rows_read = 0
                while rows_read < nf and i < n_lines:
                    _skip_comments_and_blanks()
                    if i >= n_lines:
                        break
                    row_floats: list[float] = []
                    while len(row_floats) < nd and i < n_lines:
                        for v in lines[i].split():
                            try:
                                row_floats.append(float(v) * factor)
                            except ValueError:
                                pass
                            if len(row_floats) >= nd:
                                break
                        if len(row_floats) >= nd:
                            break
                        i += 1
                    energy.append(row_floats[:nd])
                    rows_read += 1
                    i += 1

                if len(energy) != nf or not all(len(row) == nd for row in energy):
                    raise SpecoutParseError(
                        f"parse_specout_file_multi: timestep {iso} location "
                        f"{loc_idx}/{n_locations}: incomplete energy matrix "
                        f"(got {len(energy)}/{nf} rows)"
                    )

                results[loc_idx].append({
                    "time": iso,
                    "freqs_hz": list(freqs_hz),
                    "dirs_deg": list(dirs_deg),
                    "energy": energy,
                })
            continue

        i += 1

    if n_locations is None:
        raise SpecoutParseError(
            "parse_specout_file_multi: no LONLAT/LOCATIONS block found"
        )

    n_timesteps = {len(station_results) for station_results in results}
    if len(n_timesteps) > 1:
        raise SpecoutParseError(
            f"parse_specout_file_multi: stations disagree on timestep count "
            f"— {[len(r) for r in results]}"
        )

    return MultiLocationSpecout(station_lonlat=station_lonlat, station_timesteps=results)


# ---------------------------------------------------------------------------
# Spectral decomposition
# ---------------------------------------------------------------------------


def decompose_spectrum(
    freqs_hz: list[float],
    dirs_deg: list[float],
    energy: list[list[float]],
    min_peak_energy_fraction: float = 0.005,
    max_components: int = 5,
) -> list[dict[str, Any]]:
    """Decompose a 2-D SWAN directional spectrum into swell systems.

    Finds local energy peaks in the (frequency, direction) space and extracts
    a SpectralWaveComponent dict for each peak.

    Args:
        freqs_hz: Frequency axis (Hz), len nf.
        dirs_deg: Direction axis (degrees nautical), len nd.
        energy:   E[i_freq][i_dir] in m²/Hz/deg, shape (nf, nd).
        min_peak_energy_fraction: A peak is discarded if its integrated energy
            (m0) is less than this fraction of the total m0.  Default 0.005 = 0.5%.
        max_components: Maximum number of swell systems to return.

    Returns:
        List of dicts matching SpectralWaveComponent schema, sorted by descending
        energy.  Empty list if no valid peaks are found.
    """
    if not freqs_hz or not dirs_deg or not energy:
        return []

    nf = len(freqs_hz)
    nd = len(dirs_deg)

    if nf < 2 or nd < 2:
        return []

    # Compute df and dθ arrays for integration.
    # Use midpoint spacing; edge bins get half-spacing.
    df = [0.0] * nf
    for i in range(nf):
        if i == 0:
            df[i] = freqs_hz[1] - freqs_hz[0]
        elif i == nf - 1:
            df[i] = freqs_hz[-1] - freqs_hz[-2]
        else:
            df[i] = (freqs_hz[i + 1] - freqs_hz[i - 1]) / 2.0

    dd = [0.0] * nd
    for j in range(nd):
        if j == 0:
            dd[j] = abs(dirs_deg[1] - dirs_deg[0])
        elif j == nd - 1:
            dd[j] = abs(dirs_deg[-1] - dirs_deg[-2])
        else:
            dd[j] = abs(dirs_deg[j + 1] - dirs_deg[j - 1]) / 2.0

    # Total m0 for normalisation
    total_m0 = sum(
        energy[i][j] * df[i] * dd[j]
        for i in range(nf)
        for j in range(nd)
        if energy[i][j] > 0
    )
    if total_m0 <= 0:
        return []

    threshold_m0 = total_m0 * min_peak_energy_fraction

    # ---- Find local maxima in 2-D ----
    # A cell is a local maximum if it is greater than all 8 neighbours.
    peaks: list[tuple[float, int, int]] = []  # (energy_value, i_f, i_d)
    for i in range(nf):
        for j in range(nd):
            val = energy[i][j]
            if val <= 0:
                continue
            is_peak = True
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    if di == 0 and dj == 0:
                        continue
                    ni2 = i + di
                    nj2 = (j + dj) % nd  # direction axis wraps (0°=360°)
                    if 0 <= ni2 < nf:
                        if energy[ni2][nj2] >= val and not (ni2 == i and nj2 == j):
                            is_peak = False
                            break
                if not is_peak:
                    break
            if is_peak:
                peaks.append((val, i, j))

    if not peaks:
        # No local maxima — return total spectrum as single component
        peak_f_idx = max(range(nf), key=lambda ii: sum(energy[ii]))
        peak_d_idx = max(range(nd), key=lambda jj: sum(energy[ii][jj] for ii in range(nf)))
        peaks = [(energy[peak_f_idx][peak_d_idx], peak_f_idx, peak_d_idx)]

    # Sort by descending peak value
    peaks.sort(key=lambda x: x[0], reverse=True)
    peaks = peaks[:max_components * 3]  # pre-filter to limit work

    # ---- For each peak, integrate energy in a neighbourhood ----
    # Neighbourhood: ±4 bins in frequency and direction (≈±40° for 10°/bin grids).
    # No greedy cell exclusion — each peak integrates over its full neighbourhood
    # independently so secondary swell systems are not masked by the dominant peak.
    components: list[dict[str, Any]] = []

    for _, pi, pj in peaks:
        if len(components) >= max_components:
            break

        # Integration window: ±4 bins in frequency and direction
        f_lo = max(0, pi - 4)
        f_hi = min(nf - 1, pi + 4)

        nbr_cells: list[tuple[int, int]] = []
        for ii in range(f_lo, f_hi + 1):
            for jj_offset in range(-4, 5):
                jj = (pj + jj_offset) % nd
                nbr_cells.append((ii, jj))

        if not nbr_cells:
            continue

        # Integrate energy over the neighbourhood
        m0 = sum(energy[ii][jj] * df[ii] * dd[jj] for ii, jj in nbr_cells if energy[ii][jj] > 0)
        if m0 < threshold_m0:
            continue

        # Energy-weighted frequency
        total_e = sum(energy[ii][jj] * df[ii] * dd[jj] for ii, jj in nbr_cells if energy[ii][jj] > 0)
        if total_e <= 0:
            continue

        weighted_f = sum(
            freqs_hz[ii] * energy[ii][jj] * df[ii] * dd[jj]
            for ii, jj in nbr_cells
            if energy[ii][jj] > 0
        ) / total_e

        # Energy-weighted direction (circular mean to handle wrap-around)
        sin_sum = sum(
            math.sin(math.radians(dirs_deg[jj])) * energy[ii][jj] * df[ii] * dd[jj]
            for ii, jj in nbr_cells
            if energy[ii][jj] > 0
        )
        cos_sum = sum(
            math.cos(math.radians(dirs_deg[jj])) * energy[ii][jj] * df[ii] * dd[jj]
            for ii, jj in nbr_cells
            if energy[ii][jj] > 0
        )
        mean_dir = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0

        if weighted_f <= 0:
            continue

        peak_period = 1.0 / weighted_f
        peak_hs = 4.0 * math.sqrt(max(0.0, m0))

        # Frequency range for this partition
        f_values_in_nbr = sorted(set(freqs_hz[ii] for ii, _ in nbr_cells))
        freq_range = [
            f_values_in_nbr[0] if f_values_in_nbr else freqs_hz[f_lo],
            f_values_in_nbr[-1] if f_values_in_nbr else freqs_hz[f_hi],
        ]

        components.append({
            "height": round(peak_hs, 3),
            "period": round(peak_period, 2),
            "direction": round(mean_dir, 1),
            "energy": round(m0, 6),
            "frequencyRange": [round(freq_range[0], 4), round(freq_range[1], 4)],
            "classification": _classify_period(peak_period),
        })

    # Sort by descending height
    components.sort(key=lambda c: c["height"], reverse=True)

    # Sanity check: sum of m0 should be close to total_m0
    sum_m0 = sum(c["energy"] for c in components)
    if sum_m0 > 0 and abs(sum_m0 - total_m0) / total_m0 > 0.5:
        logger.debug(
            "SWAN spectral decomposition: components account for %.1f%% of total energy",
            100.0 * sum_m0 / total_m0,
        )

    return components


# ---------------------------------------------------------------------------
# SWAN TABLE PT* (watershed partitioning) parsing — T4B.2
#
# SWAN's own spectral partitioning (Hanson & Phillips 2001 watershed
# algorithm) is available directly from TABLE output via the PTHSIGN /
# PTRTP / PTDIR / PTDSPR quantities (see docs/reference/swan-commands-extract.md
# "Spectral partitioning output (PT* quantities)" and swanmain.ftn:2649+).
# This is a candidate REPLACEMENT for decompose_spectrum()'s ±4-bin
# neighbourhood peak-finder — that replacement is trigger 1 (algorithm
# change) and is NOT approved. The functions below exist to PARSE this
# output and let scripts/compare_partitioning.py MEASURE the two algorithms
# against each other on real data. decompose_spectrum() above is untouched
# and remains the only partitioning path any production caller uses.
# ---------------------------------------------------------------------------

# Column-name prefix (10-column block, "<PREFIX>01".."<PREFIX>10") -> the
# SWAN exception ("no partition here") value for that quantity.
#
# PARSER TRAP, deliberately called out per LC-4B-4: PTDIR's exception value
# is -999, every other PT* quantity's is -9. Verified against
# swanmain.ftn:2649+ (OVEXCV per IVTYPE: 100->-9, 110->-9, 120->-9,
# 130->-999, 140->-9, 150->-9, 160->-9). A parser that assumes one uniform
# sentinel across all PT* columns will read an absent partition's direction
# as a real (if implausible after a raw %360) heading instead of recognising
# it as absent. This table is intentionally per-prefix, not a single module
# constant, so that mistake cannot be made by construction.
_PT_PREFIX_EXCEPTION_VALUES: dict[str, float] = {
    "HSPT": -9.0,    # PTHSIGN -> HsPT01..HsPT10 (m)
    "TPPT": -9.0,    # PTRTP   -> TpPT01..TpPT10 (s)
    "WLPT": -9.0,    # PTWLEN  -> WlPT01..WlPT10 (m)
    "DRPT": -999.0,  # PTDIR   -> DrPT01..DrPT10 (deg)  <- the odd one out
    "DSPT": -9.0,    # PTDSPR  -> DsPT01..DsPT10 (deg)
    "WFPT": -9.0,    # PTWFRAC -> WfPT01..WfPT10 (dimensionless)
    "STPT": -9.0,    # PTSTEE  -> StPT01..StPT10 (dimensionless)
}

# Tolerance for matching a parsed float against its prefix's exception value.
# SWAN emits the exception value with full floating-point precision, but a
# generous tolerance guards against any formatting rounding in the ASCII
# TABLE file without ever risking a real reading (Hs/Tp/Wl/Dspr/Wfrac/Steep
# are always >= 0; Dir is always in [0, 360)) being mistaken for absent.
#
# NOTE (T4B.2, LC-4B-4 correction): this sentinel check is belt-and-braces
# only. Measured against a real SWAN TABLE run (1273 rows, 24 PT columns,
# /home/claude/p4b/TABLE_1_20260725T1014Z.txt) the documented -9/-999
# exception values never appeared — every absent partition was written as
# an exact 0.00000. ``_PT_HS_ZERO_TOLERANCE`` below is the PRIMARY absence
# signal for real data; this sentinel exists only for a SWAN build/config
# that might emit it instead.
_PT_EXCEPTION_TOLERANCE = 0.5

# Primary absence signal (T4B.2): a partition is absent when its HsPT0k is
# (numerically) zero. SWAN's own HSPMIN = 0.05 m floor (SwanSpectPart.ftn:96)
# means any real partition has Hs >= 0.05 m, so this tolerance has a wide,
# safe margin below that floor while still absorbing ASCII formatting
# rounding in the TABLE file's fixed-width float columns.
_PT_HS_ZERO_TOLERANCE = 0.0005

_PT_MAX_PARTITIONS = 10


def _is_pt_exception(value: float, prefix: str) -> bool:
    """True if *value* is the "no partition here" sentinel for *prefix*.

    Looks the sentinel up per-prefix (see ``_PT_PREFIX_EXCEPTION_VALUES``) —
    never a single shared constant. Unknown prefixes are treated as never-
    exceptional (defensive default; callers only pass prefixes they found in
    the file's own header).
    """
    sentinel = _PT_PREFIX_EXCEPTION_VALUES.get(prefix)
    if sentinel is None:
        return False
    return abs(value - sentinel) < _PT_EXCEPTION_TOLERANCE


def compute_total_m0(
    freqs_hz: list[float],
    dirs_deg: list[float],
    energy: list[list[float]],
) -> float:
    """Integrate a 2-D SWAN directional spectrum to total zeroth moment (m0).

    Same integral ``decompose_spectrum()`` computes internally to normalise
    its peak-energy threshold (variable-width df/dtheta via midpoint
    spacing). Kept as a standalone function — rather than refactoring
    ``decompose_spectrum()`` to expose it — so this addition carries zero
    risk to the live default partitioning path; the two are independently
    computed from the same well-established Hs = 4*sqrt(m0) definition.

    Used by ``scripts/compare_partitioning.py`` as the conservation
    reference: whichever algorithm's partitions are being measured, "does
    Σ partition m0 equal total m0" needs a total computed from the full 2-D
    spectrum, which is what this function provides for the SPECOUT-file side
    of the comparison (LC-4B-6 — the comparison is a measurement, not an
    assertion).

    Returns 0.0 for a degenerate spectrum (fewer than 2 frequency or
    direction bins), matching ``decompose_spectrum()``'s own guard.
    """
    nf = len(freqs_hz)
    nd = len(dirs_deg)
    if nf < 2 or nd < 2 or not energy:
        return 0.0

    df = [0.0] * nf
    for i in range(nf):
        if i == 0:
            df[i] = freqs_hz[1] - freqs_hz[0]
        elif i == nf - 1:
            df[i] = freqs_hz[-1] - freqs_hz[-2]
        else:
            df[i] = (freqs_hz[i + 1] - freqs_hz[i - 1]) / 2.0

    dd = [0.0] * nd
    for j in range(nd):
        if j == 0:
            dd[j] = abs(dirs_deg[1] - dirs_deg[0])
        elif j == nd - 1:
            dd[j] = abs(dirs_deg[-1] - dirs_deg[-2])
        else:
            dd[j] = abs(dirs_deg[j + 1] - dirs_deg[j - 1]) / 2.0

    return sum(
        energy[i][j] * df[i] * dd[j]
        for i in range(nf)
        for j in range(nd)
        if i < len(energy) and j < len(energy[i]) and energy[i][j] > 0
    )


def parse_table_pt_partitions(table_text: str) -> list[dict[str, Any]]:
    """Parse SWAN TABLE ASCII output's PT* (watershed partitioning) columns.

    Reads whichever ``HsPT##`` / ``TpPT##`` / ``DrPT##`` / ``DsPT##`` /
    ``WlPT##`` / ``WfPT##`` / ``StPT##`` columns are present in the file's
    ``HEAD`` header line (only PTHSIGN/PTRTP/PTDIR/PTDSPR are emitted as of
    T4B.1 — see ``docs/reference/swan-commands-extract.md``), applies the
    correct per-quantity exception value to each (LC-4B-4), and returns one
    entry per output row (one row = one station at one timestep) containing
    only the partitions that actually exist at that row — never a partition
    padded in as a fabricated 0.

    A TABLE row's ``HsPT0k`` column is treated as the presence signal for
    partition *k*. **Real SWAN output signals an absent partition with
    ``HsPT0k`` written as an exact 0.00000, not the documented -9/-999
    exception value** — measured directly against a real TABLE run (1273
    rows, 24 PT columns, zero sentinels observed; see
    ``docs/planning/MARINE-SERVICE-SEPARATION-PLAN.md`` Phase 4B T4B.2 for
    the measurement). Zero (within ``_PT_HS_ZERO_TOLERANCE``) is therefore
    the PRIMARY absence test this parser applies to ``HsPT0k``; the
    documented exception-value sentinel (``_is_pt_exception``) is checked
    too, belt-and-braces, in case some other SWAN build/config does emit
    it, but it is never the only signal relied on. Anchoring on Hs (always
    present when any PT* quantity was requested) avoids depending on which
    of the optional quantities were actually emitted.

    Per SWAN convention (docs/reference/swan-commands-extract.md,
    docs/planning/MARINE-SERVICE-SEPARATION-PLAN.md Phase 4B "Verified SWAN
    mechanics"): partition 1 is the wind sea; partitions 2-10 are swells in
    descending significant wave height. That ordering is preserved as-is —
    this parser does not re-sort.

    Args:
        table_text: Full content of a SWAN TABLE ASCII file (``HEAD`` format)
            that includes at least the ``HsPT01``..``HsPT10`` columns.

    Returns:
        List of dicts, one per parsed row:
          {
            "time":  ISO-8601 UTC string, or None if no TIME column,
            "xp":    float, station longitude/x (degrees or UTM, as emitted),
            "yp":    float, station latitude/y,
            "partitions": [
              {
                "partition_index": int,       # 1-10, SWAN's own numbering
                "is_wind_sea":     bool,       # True only for partition_index == 1
                "height":          float,      # m, from HsPT0k
                "period":          float | None,   # s, from TpPT0k if present
                "direction":       float | None,   # deg, from DrPT0k if present
                "spread":          float | None,   # deg, from DsPT0k if present
                "energy":          float,      # m0 (m^2), back-solved from height
                "classification":  str,        # groundswell/swell/wind_swell
              },
              ...
            ],
          }
        Rows with no header match, or with a missing/zero/exception-valued
        HsPT column across all 10 slots (no partitions at all — dry point or
        zero energy), are omitted entirely. Never returns a row with a
        fabricated partition.
    """
    lines = [ln.rstrip("\r\n") for ln in table_text.splitlines()]

    col_idx: dict[str, int] = {}
    header_found = False
    results: list[dict[str, Any]] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("%"):
            tokens = line.lstrip("%").split()
            if not header_found and tokens and tokens[0].upper() not in ("RUN:V1", "[DEGR]", "[M]", "[SEC]"):
                if any(t.upper() in ("XP", "YP", "TIME") or t.upper().startswith("HSPT") for t in tokens):
                    col_idx = {tok.upper(): idx for idx, tok in enumerate(tokens)}
                    header_found = True
            continue

        if not header_found:
            continue

        parts = line.split()
        if not parts:
            continue

        i_time = col_idx.get("TIME")
        i_xp = col_idx.get("XP")
        i_yp = col_idx.get("YP")
        if i_xp is None or i_yp is None:
            continue

        # Per-prefix column index lists: prefix -> [idx_01, idx_02, ..., idx_10 | None]
        prefix_cols: dict[str, list[int | None]] = {}
        for prefix in _PT_PREFIX_EXCEPTION_VALUES:
            cols: list[int | None] = []
            any_present = False
            for k in range(1, _PT_MAX_PARTITIONS + 1):
                idx = col_idx.get(f"{prefix}{k:02d}")
                cols.append(idx)
                if idx is not None:
                    any_present = True
            if any_present:
                prefix_cols[prefix] = cols

        if "HSPT" not in prefix_cols:
            # No presence signal available at all — nothing to parse.
            continue

        max_idx = max(i_xp, i_yp)
        if i_time is not None:
            max_idx = max(max_idx, i_time)
        for cols in prefix_cols.values():
            for idx in cols:
                if idx is not None:
                    max_idx = max(max_idx, idx)
        if len(parts) < max_idx + 1:
            continue

        try:
            xp = float(parts[i_xp])
            yp = float(parts[i_yp])
        except (ValueError, IndexError):
            continue

        time_iso: str | None = None
        if i_time is not None and i_time < len(parts):
            time_token = parts[i_time]
            try:
                date_part, time_part = time_token.split(".")
                time_iso = (
                    f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
                )
            except (ValueError, IndexError):
                time_iso = None

        def _value_at(prefix: str, k_zero_based: int) -> float | None:
            cols = prefix_cols.get(prefix)
            if cols is None:
                return None
            idx = cols[k_zero_based]
            if idx is None or idx >= len(parts):
                return None
            try:
                v = float(parts[idx])
            except (ValueError, TypeError):
                return None
            if _is_pt_exception(v, prefix):
                return None
            return v

        row_partitions: list[dict[str, Any]] = []
        for k in range(1, _PT_MAX_PARTITIONS + 1):
            k0 = k - 1
            # HsPT0k drives presence. Fetch the raw parsed value (bypassing
            # _value_at's exception-only filtering) so the zero check below
            # sees the real number, not an already-None'd sentinel match.
            cols = prefix_cols.get("HSPT")
            hs_idx = cols[k0] if cols is not None else None
            if hs_idx is None or hs_idx >= len(parts):
                continue  # column absent from this file's header
            try:
                hs_raw = float(parts[hs_idx])
            except (ValueError, TypeError):
                continue

            # T4B.2 (LC-4B-4 correction): zero is the PRIMARY absence
            # signal for real SWAN output — the documented -9/-999
            # exception value is checked too, belt-and-braces, but never
            # relied on alone (see module-level note by
            # _PT_HS_ZERO_TOLERANCE and this function's docstring).
            if abs(hs_raw) < _PT_HS_ZERO_TOLERANCE or _is_pt_exception(hs_raw, "HSPT"):
                # Absent partition. Do NOT treat this slot as height 0 in
                # the OUTPUT — it does not exist, so we never append it.
                continue
            hs = hs_raw

            tp = _value_at("TPPT", k0)
            direction = _value_at("DRPT", k0)
            spread = _value_at("DSPT", k0)

            classification = _classify_period(tp) if tp is not None and tp > 0 else "wind_swell"

            row_partitions.append({
                "partition_index": k,
                "is_wind_sea": k == 1,
                "height": hs,
                "period": tp,
                "direction": direction,
                "spread": spread,
                "energy": (hs / 4.0) ** 2,
                "classification": classification,
            })

        if not row_partitions:
            continue

        results.append({
            "time": time_iso,
            "xp": xp,
            "yp": yp,
            "partitions": row_partitions,
        })

    return results


def watershed_partitions_to_component_format(
    watershed_partitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert one row's SWAN TABLE PT* watershed partitions into the same
    SpectralWaveComponent-shaped dict list ``decompose_spectrum()`` produces
    (T4B.2 — the production conversion used at the live L3 CURVE call sites
    in ``swan_runner.py`` and by ``surf_1d_pipeline.run_pipeline()``'s
    explicit ``partition_source="watershed"`` opt-in).

    Input is one row's ``"partitions"`` list from
    ``parse_table_pt_partitions()``: ordered per SWAN's own convention —
    partition 1 is the wind sea, partitions 2-10 are swells in descending
    Hs — and carrying an ``is_wind_sea`` flag that
    ``decompose_spectrum()``'s output has no equivalent for.

    Every existing consumer of ``decompose_spectrum()``'s output (the
    dashboard multi-swell card, the per-partition SwellTrack loop in
    ``surf_1d_pipeline.py``, ``_aggregate_partition_breaks()``) assumes that
    output's own contract: sorted by descending height, no wind-sea concept.
    Rather than silently re-sorting without comment, this function
    explicitly re-sorts by descending Hs to match that contract, AND
    explicitly documents, at this exact point, that the wind-sea/swell
    distinction watershed partitioning carries is discarded here because the
    existing consumers have no concept of it. Consuming that distinction is
    a separate, not-yet-approved change.

    ``frequencyRange`` is not derivable from TABLE bulk PT* output (SWAN's
    TABLE only emits the partition's bulk Hs/Tp/Dir/Dspr, not its frequency
    bin bounds) — set to ``[0.0, 0.0]`` for every partition produced this
    way.

    Partitions with no period or direction (should not occur — SWAN emits
    all PT* quantities for a given partition simultaneously — but handled
    defensively since a partial TABLE emission is a plausible operator
    misconfiguration) are dropped rather than fed through with a bogus 0.
    """
    converted = [
        {
            "height": p["height"],
            "period": p["period"],
            "direction": p["direction"],
            "energy": p["energy"],
            "frequencyRange": [0.0, 0.0],  # not derivable from TABLE bulk PT* output
            "classification": p["classification"],
        }
        for p in watershed_partitions
        if p.get("period") is not None and p.get("direction") is not None
    ]
    # Wind-sea/swell distinction discarded here — see docstring above.
    converted.sort(key=lambda c: c["height"], reverse=True)
    return converted


# ---------------------------------------------------------------------------
# Top-level: parse file + decompose all timesteps
# ---------------------------------------------------------------------------


def parse_and_decompose(
    file_path: Path,
) -> list[dict[str, Any]]:
    """Parse a SWAN SPECOUT file and decompose each timestep into swell systems.

    T4B.2 (2026-07-25): this function's own ``decompose_spectrum()`` call is
    no longer the production path anywhere in ``swan_runner.py`` — the L2
    deep-water-reference (DWR) baseline and its T4B.4 per-transect-cell
    variant now read SWAN's own watershed (PT*) partitions instead (see
    ``run_3level()``'s DWR TABLE output and
    ``_watershed_by_time_for_point()``), closing the gap this whole round
    exists to close. This function is retained, unchanged, as a general
    SPECOUT-decomposition utility and for
    ``scripts/compare_partitioning.py``'s direct measurement of the two
    algorithms against each other — it is not dead code, just no longer a
    production caller.

    Args:
        file_path: Path to the SWAN SPECOUT ASCII file.

    Returns:
        List of dicts, one per timestep:
          {
            "time":       ISO-8601 UTC string,
            "components": list[dict] matching SpectralWaveComponent schema,
          }
        Empty list if the file does not exist or cannot be parsed.
    """
    if not file_path.exists():
        logger.debug("SWAN SPECOUT: file not found: %s", file_path)
        return []

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("SWAN SPECOUT: could not read %s", file_path, exc_info=True)
        return []

    raw_spectra = parse_specout_file(text)
    if not raw_spectra:
        logger.debug("SWAN SPECOUT: no spectra parsed from %s", file_path)
        return []

    results: list[dict[str, Any]] = []
    for spectrum in raw_spectra:
        components = decompose_spectrum(
            spectrum["freqs_hz"],
            spectrum["dirs_deg"],
            spectrum["energy"],
        )
        results.append({
            "time": spectrum["time"],
            "components": components,
        })

    logger.debug(
        "SWAN SPECOUT: parsed %d timestep(s) from %s, avg %.1f component(s)",
        len(results),
        file_path.name,
        sum(len(r["components"]) for r in results) / len(results) if results else 0,
    )
    return results
