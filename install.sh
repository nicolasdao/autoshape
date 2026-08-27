#!/bin/sh
# Install autoshape so it can be run as `sudo autoshape` from anywhere.
#
#   curl -fsSL https://raw.githubusercontent.com/nicolasdao/autoshape/main/install.sh | sh
# or, from a clone:
#   ./install.sh
set -e

PREFIX="${PREFIX:-/usr/local}"
LIBDIR="$PREFIX/lib/autoshape"
BINDIR="$PREFIX/bin"
GH="https://raw.githubusercontent.com/nicolasdao/autoshape"
# try main, fall back to master
REPO="$GH/main"
curl -fsSL -o /dev/null "$REPO/VERSION" 2>/dev/null || REPO="$GH/master"

case "$(uname -s)" in
  Darwin) ;;
  *) echo "autoshape currently supports macOS only (it uses pfctl and dnctl)."; exit 1 ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "python3 is required."; exit 1; }

SUDO=""
[ -w "$BINDIR" ] || SUDO="sudo"
$SUDO mkdir -p "$LIBDIR" "$BINDIR"

if [ -f "$(dirname "$0")/autoshape.py" ]; then
  SRC="$(cd "$(dirname "$0")" && pwd)"
  for f in autoshape.py control.py tcpsense.py VERSION; do
    $SUDO cp "$SRC/$f" "$LIBDIR/$f"
  done
else
  echo "downloading..."
  for f in autoshape.py control.py tcpsense.py VERSION; do
    $SUDO curl -fsSL "$REPO/$f" -o "$LIBDIR/$f"
  done
fi
$SUDO chmod +x "$LIBDIR/autoshape.py"

$SUDO tee "$BINDIR/autoshape" >/dev/null <<WRAP
#!/bin/sh
exec python3 "$LIBDIR/autoshape.py" "\$@"
WRAP
$SUDO chmod +x "$BINDIR/autoshape"

echo
echo "  installed. start it with:"
echo "      sudo autoshape"
echo
echo "  stop it with Ctrl-C. if anything is ever left behind:"
echo "      sudo autoshape --off"
echo
