# shellcheck shell=bash
# Common helpers sourced by other scripts. Not a standalone script.

set -euo pipefail

# ---- pretty logging ----
log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- preconditions ----
require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "this script must be run as root (try: sudo $0 $*)"
  fi
}

require_ubuntu() {
  local id ver
  if [ ! -r /etc/os-release ]; then die "/etc/os-release not found; this script targets Ubuntu 22.04 or 24.04"; fi
  # shellcheck disable=SC1091
  . /etc/os-release
  id="${ID:-}"
  ver="${VERSION_ID:-}"
  if [ "$id" != "ubuntu" ]; then
    die "this script targets Ubuntu (detected: $id $ver). Use the manual instructions in STANDALONE_DEPLOYMENT.md for other distros."
  fi
  case "$ver" in
    22.04|24.04) : ;;
    *) warn "tested on Ubuntu 22.04 and 24.04; you're running $ver — proceed at your own risk." ;;
  esac
}

# Random URL-safe secret of given length (default 32).
gen_secret() {
  local len="${1:-32}"
  openssl rand -base64 "$((len * 3 / 4 + 4))" | tr -dc 'A-Za-z0-9_-' | head -c "$len"
}

# Replace a key=value line in a file or append if missing. Idempotent.
set_env_var() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    # Use a delimiter unlikely to appear in env values
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file" && rm -f "${file}.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}
