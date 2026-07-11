#!/bin/sh
# RAVE installer for Mac, Ubuntu, and Linux servers (VPS).
# Run:  curl -fsSL https://raw.githubusercontent.com/PrabakaranR-code/rave/main/install.sh | sh
set -e

REPO_URL="${RAVE_REPO_URL:-https://github.com/PrabakaranR-code/rave}"
DIR="${RAVE_HOME:-$HOME/rave}"
BIN_DIR="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# When piped from curl, stdin is the script itself — talk to the person
# through the terminal device instead. `[ -r /dev/tty ]` is not enough:
# on CI runners the node exists but opening it fails, so actually try.
HAS_TTY=0
if ( : < /dev/tty ) 2>/dev/null; then HAS_TTY=1; fi

wait_enter() {
  if [ "$HAS_TTY" = "1" ]; then
    printf '%s' "(press Enter when done) " > /dev/tty
    IFS= read -r _line < /dev/tty || true
  else
    say "No keyboard available — continuing."
  fi
}

open_page() {
  if have open; then open "$1" 2>/dev/null || true
  elif have xdg-open; then xdg-open "$1" >/dev/null 2>&1 || true
  else say "Open this page in your browser: $1"; fi
}

need_fail() {
  # Without a keyboard there is no install-and-press-Enter loop: fail fast
  # with a clear message instead of hanging forever.
  say "✗ $1"
  exit 1
}

OS="linux"
case "$(uname -s)" in
  Darwin) OS="mac" ;;
  Linux)
    if grep -qiE 'ubuntu|debian' /etc/os-release 2>/dev/null; then OS="ubuntu"; fi ;;
esac
say "→ Installing RAVE on $OS"

APT="apt-get"
[ "$(id -u)" != "0" ] && have sudo && APT="sudo apt-get"

# --- Python 3.11+ (the language RAVE runs on) -------------------------------
find_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
while [ -z "$PY" ]; do
  if [ "$OS" = "ubuntu" ]; then
    say "→ Installing Python (the language RAVE runs on)…"
    $APT update -y >/dev/null 2>&1 || true
    $APT install -y python3.12 python3.12-venv 2>/dev/null \
      || $APT install -y python3.11 python3.11-venv 2>/dev/null \
      || $APT install -y python3 python3-venv python3-pip || true
    PY="$(find_python || true)"
    [ -z "$PY" ] && need_fail "Could not install Python 3.11+. Install it, then re-run."
  else
    [ "$HAS_TTY" != "1" ] && need_fail "Python 3.11+ is required. Install it from python.org, then re-run."
    say "RAVE needs Python 3.11 or newer (the language RAVE runs on)."
    say "I've opened the download page. Install it, then come back."
    open_page "https://www.python.org/downloads/"
    wait_enter
    PY="$(find_python || true)"
  fi
done
say "✓ Python found: $PY"

# --- Git (a program that downloads code) ------------------------------------
while ! have git; do
  if [ "$OS" = "ubuntu" ]; then
    say "→ Installing Git (a program that downloads code)…"
    $APT update -y >/dev/null 2>&1 || true
    $APT install -y git || need_fail "Could not install Git."
  else
    [ "$HAS_TTY" != "1" ] && need_fail "Git is required. Install it from git-scm.com, then re-run."
    say "RAVE needs Git (a program that downloads code)."
    say "I've opened the download page. Install it, then come back."
    open_page "https://git-scm.com/downloads"
    wait_enter
  fi
done
say "✓ Git found"

# Docker is NOT installed here: the default search backend is SCOUT (built in,
# keyless, no server). Docker is only needed if you later pick the optional
# SearXNG backend in the wizard, which installs it then.

# --- Get the code, build the sandbox, install dependencies ------------------
if [ -d "$DIR/.git" ]; then
  say "→ Updating RAVE in $DIR"
  git -C "$DIR" pull --ff-only || true
else
  say "→ Downloading RAVE to $DIR"
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
if ! "$PY" -m venv .venv 2>/dev/null; then
  # some minimal images ship python without the venv module
  if [ "$OS" = "ubuntu" ]; then
    VENV_PKG="$("$PY" -c 'import sys; print(f"python3.{sys.version_info[1]}-venv")')"
    $APT install -y "$VENV_PKG" python3-venv >/dev/null 2>&1 || true
  fi
  "$PY" -m venv .venv || need_fail "Could not create the Python sandbox (venv)."
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
say "✓ RAVE installed"

# --- The `rave` command ------------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/rave" <<EOF
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/rave"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Note: add $BIN_DIR to your PATH to use the rave command everywhere." ;;
esac
say "✓ Command installed: rave"

# --- Hand over to the setup wizard ------------------------------------------
if [ "$HAS_TTY" = "1" ]; then
  exec "$DIR/.venv/bin/python" "$DIR/main.py" setup < /dev/tty
else
  say "Setup is next — run:  rave setup"
fi
