#!/usr/bin/env bash
#
# Install MinervaAI tenant + role access control into a running app.
#
# Checks first, changes nothing, and prints a report. Re-run with --apply to
# copy the modules in; the one edit it will not make for you is the wiring into
# your server file, because guessing at an unseen server is how installers
# break working systems. It prints exactly what to add and where.
#
# Usage:
#   bash install-access.sh --app ~/minerva                 # inspect only
#   bash install-access.sh --app ~/minerva --apply         # copy modules in
#
# Written for macOS bash 3.2 — no associative arrays, no ${var,,}.

set -u

APP=""
APPLY="no"
SRC="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="${2:-}"; shift 2 ;;
    --apply) APPLY="yes"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$APP" ]; then
  echo "ERROR: --app <directory> is required (e.g. --app ~/minerva)" >&2
  exit 2
fi

APP="${APP/#\~/$HOME}"

if [ ! -d "$APP" ]; then
  echo "ERROR: no such directory: $APP" >&2
  exit 2
fi

say()  { printf '%s\n' "$*"; }
head2() { printf '\n== %s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FIX   %s\n' "$*"; }

say "MinervaAI access control installer"
say "app:  $APP"
if [ "$APPLY" = "yes" ]; then say "mode: APPLY"; else say "mode: inspect only (add --apply to install)"; fi

# --- 1. node ---------------------------------------------------------------

head2 "Node"
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "$NODE_MAJOR" -ge 16 ]; then
    ok "node $(node -v) (crypto can verify the Access token)"
  else
    bad "node $(node -v) is too old; JWK verification needs Node 16+"
  fi
else
  bad "node not found on PATH"
fi

# --- 2. server entry point -------------------------------------------------

head2 "Server entry point"
ENTRY=""
if [ -f "$APP/package.json" ]; then
  ENTRY="$(node -e 'try{const p=require(process.argv[1]);process.stdout.write(p.main||"")}catch(e){}' "$APP/package.json" 2>/dev/null)"
fi
if [ -n "$ENTRY" ] && [ -f "$APP/$ENTRY" ]; then
  ok "package.json main: $ENTRY"
else
  ENTRY=""
  for candidate in server.js app.js index.js src/server.js src/app.js; do
    if [ -f "$APP/$candidate" ]; then ENTRY="$candidate"; break; fi
  done
  if [ -n "$ENTRY" ]; then ok "found $ENTRY"; else warn "could not identify the server entry file"; fi
fi

# --- 3. is the origin exposed? ---------------------------------------------
#
# Cloudflare Access protects the hostname, not the port. If the app answers on
# a LAN address, anyone who finds it walks straight past Access.

head2 "Origin exposure (the check that matters most)"
if [ -n "$ENTRY" ] && [ -f "$APP/$ENTRY" ]; then
  LISTEN_LINE="$(grep -n "\.listen(" "$APP/$ENTRY" | head -5)"
  if [ -n "$LISTEN_LINE" ]; then
    say "  in $ENTRY:"
    printf '    %s\n' "$LISTEN_LINE"
    if printf '%s' "$LISTEN_LINE" | grep -q "127.0.0.1\|localhost"; then
      ok "bound to loopback — the tunnel is the only way in"
    else
      bad "no loopback bind found. Change it to:  app.listen(PORT, \"127.0.0.1\")"
    fi
  else
    warn "no .listen( call found in $ENTRY"
  fi
fi

say ""
say "  Ports currently listening on all interfaces:"
if command -v lsof >/dev/null 2>&1; then
  EXPOSED="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E '\*:(3000|4000)' || true)"
  if [ -n "$EXPOSED" ]; then
    printf '%s\n' "$EXPOSED" | sed 's/^/    /'
    bad "the above are reachable from the network, not just the tunnel"
  else
    ok "nothing on :3000 or :4000 is bound to all interfaces"
  fi
else
  warn "lsof not available; check by hand"
fi

# --- 4. Access configuration ----------------------------------------------

head2 "Cloudflare Access settings"
if [ -n "${ACCESS_AUD:-}" ]; then
  ok "ACCESS_AUD is set"
else
  bad "ACCESS_AUD is not set — it is the application AUD tag from the Access dashboard"
  say "        Zero Trust > Access > Applications > your app > Overview > Application Audience (AUD) Tag"
fi
if [ -n "${ACCESS_TEAM_DOMAIN:-}" ]; then
  ok "ACCESS_TEAM_DOMAIN is set ($ACCESS_TEAM_DOMAIN)"
else
  bad "ACCESS_TEAM_DOMAIN is not set — e.g. yourteam.cloudflareaccess.com"
fi

# --- 5. copy the modules ---------------------------------------------------

head2 "Modules"
DEST="$APP/access"
for f in access-control.js benchmark.js directory.example.json README.md; do
  if [ ! -f "$SRC/$f" ]; then
    echo "ERROR: missing $SRC/$f — run this from the access/ directory of the repo" >&2
    exit 2
  fi
done

if [ "$APPLY" = "yes" ]; then
  BACKUP=""
  if [ -d "$DEST" ]; then
    BACKUP="$APP/.access-backup-$STAMP"
    cp -R "$DEST" "$BACKUP"
    ok "backed up existing access/ to $(basename "$BACKUP")"
  fi
  mkdir -p "$DEST"
  cp "$SRC/access-control.js" "$SRC/benchmark.js" "$SRC/directory.example.json" "$SRC/README.md" "$DEST/"
  ok "copied modules to $DEST"

  if [ -f "$DEST/directory.json" ]; then
    ok "directory.json already present — left untouched"
  else
    cp "$SRC/directory.example.json" "$DEST/directory.json"
    warn "created directory.json from the example — EDIT IT before letting anyone in"
  fi

  # Rollback that knows what this run did.
  ROLLBACK="$APP/rollback-access-$STAMP.sh"
  {
    echo "#!/usr/bin/env bash"
    echo "# Undo the access-control install of $STAMP."
    echo "set -u"
    echo "rm -f '$DEST/access-control.js' '$DEST/benchmark.js' '$DEST/directory.example.json' '$DEST/README.md'"
    if [ -n "$BACKUP" ]; then
      echo "cp -R '$BACKUP/.' '$DEST/'"
      echo "echo 'restored access/ from $(basename "$BACKUP")'"
    else
      echo "rmdir '$DEST' 2>/dev/null || echo 'left $DEST in place (directory.json still there)'"
    fi
    echo "echo 'Modules removed. Now take the accessGuard lines back out of your server file.'"
  } > "$ROLLBACK"
  chmod +x "$ROLLBACK"
  ok "wrote $(basename "$ROLLBACK")"
else
  say "  would copy access-control.js, benchmark.js, directory.example.json, README.md"
  say "  would create access/directory.json if absent"
  say "  would write a matching rollback script"
fi

# --- 6. what to wire in ----------------------------------------------------

head2 "Add to ${ENTRY:-your server file}"
cat <<'SNIPPET'
    const { createAccessGuard, orgFilter, requireRole, ROLES } =
      require("./access/access-control");

    app.use(createAccessGuard({
      teamDomain: process.env.ACCESS_TEAM_DOMAIN,
      aud: process.env.ACCESS_AUD,
      directory: require("./access/directory.json"),
      onAudit: (e) => console.log("[access]", JSON.stringify(e)),
    }));

  Then, in every handler that reads data:

    const org = orgFilter(req);   // never req.query.org / req.body.org
SNIPPET

say ""
say "This installer does not edit your server file. Send me ${ENTRY:-the entry file}"
say "and I will give you the exact diff for it."
say ""
if [ "$APPLY" != "yes" ]; then
  say "Nothing was changed. Re-run with --apply when the checks above look right."
fi
