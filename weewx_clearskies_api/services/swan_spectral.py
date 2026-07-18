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


# ---------------------------------------------------------------------------
# Spectral decomposition
# ---------------------------------------------------------------------------


def decompose_spectrum(
    freqs_hz: list[float],
    dirs_deg: list[float],
    energy: list[list[float]],
    min_peak_energy_fraction: float = 0.05,
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
            (m0) is less than this fraction of the total m0.  Default 0.05 = 5%.
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
    # Neighbourhood: ±2 bins in frequency, ±30° in direction.
    components: list[dict[str, Any]] = []
    used_cells: set[tuple[int, int]] = set()

    for _, pi, pj in peaks:
        if len(components) >= max_components:
            break

        # Collect cells in neighbourhood not already claimed
        f_lo = max(0, pi - 2)
        f_hi = min(nf - 1, pi + 2)

        nbr_cells: list[tuple[int, int]] = []
        for ii in range(f_lo, f_hi + 1):
            for jj_offset in range(-2, 3):
                jj = (pj + jj_offset) % nd
                if (ii, jj) not in used_cells:
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

        # Mark cells as used
        used_cells.update(nbr_cells)

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
# Top-level: parse file + decompose all timesteps
# ---------------------------------------------------------------------------


def parse_and_decompose(
    file_path: Path,
) -> list[dict[str, Any]]:
    """Parse a SWAN SPECOUT file and decompose each timestep into swell systems.

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
