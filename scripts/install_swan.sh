#!/usr/bin/env bash
# install_swan.sh — Download, compile, and install SWAN 41.45 for TruShore
#
# SWAN (Simulating WAves Nearshore) is the Fortran wave physics engine used by
# the TruShore nearshore wave model (T2.1, ADR-093). It is NOT a pip package.
# This script automates the full build from source.
#
# What this script does:
#   1. Checks prerequisites (gfortran, make, curl/wget)
#   2. Downloads SWAN 41.45 source from SourceForge
#   3. Configures for gfortran + OpenMP parallelism
#   4. Compiles (2–5 minutes on typical hardware)
#   5. Installs the binary to /usr/local/bin/swan
#   6. Runs a version check to confirm success
#
# Prerequisites (Debian/Ubuntu 22.04+):
#   sudo apt install gfortran libopenmpi-dev
#
# Prerequisites (RHEL/Fedora):
#   sudo dnf install gcc-gfortran openmpi-devel
#
# Prerequisites (macOS with Homebrew — development only, not for production):
#   brew install gcc open-mpi
#
# Usage (must run as root or with sudo):
#   sudo bash scripts/install_swan.sh
#
# After installation, verify with:
#   swan < /dev/null 2>&1 | head -5
#
# The installed binary is used by services/swan_runner.py (T2.2). If SWAN is
# not found on PATH, the API logs CRITICAL at startup and the surf endpoint
# returns null surf data. No fallback model is used.
#
# Reference: SWAN-TRUSHORE-PLAN.md T2.1; ADR-093; PROVIDER-MANUAL §14 (TruShore)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — pin the exact version we have tested against
# ---------------------------------------------------------------------------
readonly SWAN_VERSION="41.45"
readonly SWAN_ARCHIVE="swan4145.tar.gz"
readonly SWAN_URL="https://sourceforge.net/projects/swanmodel/files/swan/${SWAN_VERSION}/${SWAN_ARCHIVE}/download"
readonly INSTALL_BIN="/usr/local/bin/swan"
readonly LOG_PREFIX="[install_swan]"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} WARNING: $*" >&2; }
die()  { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }

require_cmd() {
    local cmd="$1"
    local hint="$2"
    command -v "${cmd}" >/dev/null 2>&1 \
        || die "'${cmd}' not found. Install it first: ${hint}"
}

# ---------------------------------------------------------------------------
# Root check — /usr/local/bin requires root
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "This script must be run as root or with sudo (installs to ${INSTALL_BIN})."
fi

# ---------------------------------------------------------------------------
# Prerequisite check
# ---------------------------------------------------------------------------
log "Checking prerequisites..."
require_cmd gfortran  "Debian/Ubuntu: apt install gfortran | RHEL: dnf install gcc-gfortran"
require_cmd make      "Debian/Ubuntu: apt install make     | RHEL: dnf install make"

# Check that gfortran can compile a minimal OpenMP Fortran program.
# libgomp is bundled with GCC/gfortran; failure here means the compiler is
# broken or the OpenMP headers are missing.
OMP_TEST_SRC="$(mktemp /tmp/swan_omp_test.XXXXXX.f90)"
cat > "${OMP_TEST_SRC}" <<'EOF'
program omp_test
  implicit none
  integer :: tid
  !$omp parallel private(tid)
  tid = 0
  !$omp end parallel
end program omp_test
EOF
if ! gfortran -fopenmp "${OMP_TEST_SRC}" -o /dev/null 2>/dev/null; then
    rm -f "${OMP_TEST_SRC}"
    die "gfortran OpenMP support unavailable. Install the OpenMP runtime:\n  Debian/Ubuntu: apt install libopenmpi-dev\n  RHEL: dnf install openmpi-devel"
fi
rm -f "${OMP_TEST_SRC}"
log "Prerequisites OK: gfortran (OpenMP), make"

# Choose downloader
if command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget"
else
    die "Neither curl nor wget found. Install one:\n  Debian/Ubuntu: apt install curl"
fi

# ---------------------------------------------------------------------------
# Create a temporary build directory; clean it up on exit
# ---------------------------------------------------------------------------
BUILD_DIR="$(mktemp -d /tmp/swan_build.XXXXXX)"
trap 'log "Cleaning up ${BUILD_DIR}..."; rm -rf "${BUILD_DIR}"' EXIT

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
log "Downloading SWAN ${SWAN_VERSION} from SourceForge..."
log "  URL: ${SWAN_URL}"
log "  Destination: ${BUILD_DIR}/${SWAN_ARCHIVE}"

case "${DOWNLOADER}" in
    curl)
        # -L: follow redirects (SourceForge uses mirror redirects)
        # -f: fail on HTTP errors (4xx/5xx)
        # -S: show error message on failure
        # --retry: retry on transient network failures
        curl -fSL --retry 3 --retry-delay 5 \
             -o "${BUILD_DIR}/${SWAN_ARCHIVE}" \
             "${SWAN_URL}"
        ;;
    wget)
        wget -q --tries=3 --waitretry=5 \
             -O "${BUILD_DIR}/${SWAN_ARCHIVE}" \
             "${SWAN_URL}"
        ;;
esac

# Sanity-check: the archive must be larger than 1 MB (SWAN source is ~15 MB)
ARCHIVE_SIZE="$(wc -c < "${BUILD_DIR}/${SWAN_ARCHIVE}")"
if [ "${ARCHIVE_SIZE}" -lt 1048576 ]; then
    die "Downloaded archive is suspiciously small (${ARCHIVE_SIZE} bytes). " \
        "SourceForge may have returned an error page. Check the URL:\n  ${SWAN_URL}"
fi
log "Download complete (${ARCHIVE_SIZE} bytes)."

# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
log "Extracting ${SWAN_ARCHIVE}..."
tar -xzf "${BUILD_DIR}/${SWAN_ARCHIVE}" -C "${BUILD_DIR}"

# Locate the extracted source directory (swan4145, swan41.45, or similar)
SRC_DIR="$(find "${BUILD_DIR}" -maxdepth 1 -mindepth 1 -type d | head -1)"
if [ -z "${SRC_DIR}" ]; then
    die "No directory found after extracting ${SWAN_ARCHIVE}. Archive may be corrupt."
fi
log "Source directory: ${SRC_DIR}"
cd "${SRC_DIR}"

# ---------------------------------------------------------------------------
# Configure: select gfortran + OpenMP macros file
#
# SWAN 41.45 ships several pre-made macros template files. We copy the
# appropriate one to 'macros' before running make.
#
# Priority:
#   1. macros.gfortran_omp   — gfortran with OpenMP (preferred: parallel)
#   2. macros.gfortran       — gfortran serial (fallback with warning)
#   3. Abort if neither found
# ---------------------------------------------------------------------------
log "Configuring for gfortran + OpenMP..."

if [ -f "macros.gfortran_omp" ]; then
    cp macros.gfortran_omp macros
    log "Macros: macros.gfortran_omp (gfortran + OpenMP — parallel build)"
elif [ -f "macros.gfortran" ]; then
    warn "macros.gfortran_omp not found; using macros.gfortran (serial — no OpenMP)."
    warn "Performance will be lower. Consider installing libopenmpi-dev and retrying."
    cp macros.gfortran macros
else
    log "No pre-made macros file found. Files available:"
    ls macros.* 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    die "Cannot configure SWAN. Check the SWAN 41.45 source archive and the " \
        "SWAN installation manual for your platform."
fi

# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------
# Determine available CPU count for parallel make (-j N).
# Clamp to 8 to avoid overwhelming small systems with many cores.
NCORES="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
if [ "${NCORES}" -gt 8 ]; then NCORES=8; fi

log "Compiling SWAN with ${NCORES} parallel jobs (typically 2–5 minutes)..."
if ! make -j"${NCORES}" 2>&1 | tee /tmp/swan_build_log.txt; then
    die "SWAN compilation failed. Build log at /tmp/swan_build_log.txt. " \
        "Common causes:\n" \
        "  - gfortran or libgomp not installed\n" \
        "  - Incompatible macros file for this SWAN version\n" \
        "Check the last 20 lines of the build log:\n" \
        "$(tail -20 /tmp/swan_build_log.txt)"
fi
log "Compilation complete."

# ---------------------------------------------------------------------------
# Locate the compiled binary
#
# SWAN names its binary 'swan.exe' even on Linux (historical build-system
# convention from its Windows/PC origins). Some builds may produce 'swan'.
# ---------------------------------------------------------------------------
if [ -f "swan.exe" ]; then
    SWAN_BIN="${SRC_DIR}/swan.exe"
elif [ -f "swan" ]; then
    SWAN_BIN="${SRC_DIR}/swan"
else
    log "Build directory contents:"
    ls -la | head -30
    die "Build completed but SWAN binary not found. Expected 'swan.exe' or 'swan' in ${SRC_DIR}."
fi
log "Binary located: ${SWAN_BIN}"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
log "Installing to ${INSTALL_BIN}..."
install -m 755 "${SWAN_BIN}" "${INSTALL_BIN}"
log "Installed: ${INSTALL_BIN}"

# ---------------------------------------------------------------------------
# Version check
#
# SWAN prints a version banner to stderr when started, then reads INPUT from
# stdin. Piping /dev/null causes it to exit immediately (no INPUT file) while
# still printing the banner. We capture combined stdout+stderr and grep for
# the expected version string.
# ---------------------------------------------------------------------------
log "Running version check..."
SWAN_OUTPUT="$(timeout 15s "${INSTALL_BIN}" < /dev/null 2>&1 || true)"

if echo "${SWAN_OUTPUT}" | grep -q "${SWAN_VERSION}"; then
    SWAN_VER_LINE="$(echo "${SWAN_OUTPUT}" | grep -i "version\|${SWAN_VERSION}" | head -1 | tr -d '\r\n')"
    log "Version check PASSED: ${SWAN_VER_LINE}"
else
    warn "SWAN installed but version string '${SWAN_VERSION}' not found in startup output."
    warn "First 10 lines of SWAN output:"
    echo "${SWAN_OUTPUT}" | head -10 | sed 's/^/  /' >&2
    warn "The binary may still be functional. Verify manually:"
    warn "  ${INSTALL_BIN} < /dev/null 2>&1 | head -10"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "-------------------------------------------------------------------"
log "SWAN ${SWAN_VERSION} installation complete."
log "  Binary : ${INSTALL_BIN}"
log "  Usage  : swan < INPUT_FILE"
log ""
log "Next steps:"
log "  pip install 'weewx-clearskies-api[nearshore]'"
log "-------------------------------------------------------------------"
