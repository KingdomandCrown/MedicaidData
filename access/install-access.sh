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
    # The callback almost always logs "http://localhost:PORT". That is a message
    # to a human, not a bind address, and matching it reported "bound to
    # loopback" for a server listening on every interface — the one answer this
    # check exists to never give. Cut the callback off before deciding.
    LISTEN_ARGS="$(printf '%s' "$LISTEN_LINE" | sed 's/=>.*//; s/function[[:space:]]*(.*//')"
    if printf '%s' "$LISTEN_ARGS" | grep -qE '127\.0\.0\.1|"localhost"|HOST'; then
      ok "the listen call names a host — confirm it below against what is actually bound"
    else
      bad "no host argument. app.listen(PORT) listens on every interface."
      say "        Fix:  node access/patch-minerva-server.js --apply"
    fi
  else
    warn "no .listen( call found in $ENTRY"
  fi
fi

say ""
say "  What is actually bound right now (this is the answer that counts —"
say "  the source above is only what the file says):"
if command -v lsof >/dev/null 2>&1; then
  EXPOSED="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E '\*:[0-9]+ \(LISTEN\)' || true)"
  if [ -n "$EXPOSED" ]; then
    printf '%s\n' "$EXPOSED" | sed 's/^/    /'
    bad "every port above answers on every interface, so Cloudflare Access is optional"
    say "        A running process keeps its old bind until it restarts:"
    say "          pm2 restart minerva-40"
  else
    ok "nothing is listening on all interfaces"
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
for f in access-control.js benchmark.js scope.js tenant-state.js boot.js patch-minerva-server.js directory.example.json orgs.example.json README.md; do
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
  cp "$SRC/access-control.js" "$SRC/benchmark.js" "$SRC/scope.js" "$SRC/tenant-state.js" "$SRC/boot.js" "$SRC/patch-minerva-server.js" "$SRC/directory.example.json" "$SRC/orgs.example.json" "$SRC/README.md" "$DEST/"
  ok "copied modules to $DEST"

  if [ -f "$DEST/directory.json" ]; then
    ok "directory.json already present — left untouched"
  else
    cp "$SRC/directory.example.json" "$DEST/directory.json"
    warn "created directory.json from the example — EDIT IT before letting anyone in"
  fi

  if [ -f "$DEST/orgs.json" ]; then
    ok "orgs.json already present — left untouched"
  else
    cp "$SRC/orgs.example.json" "$DEST/orgs.json"
    warn "created orgs.json from the example — it currently grants one hospital"
  fi

  # Rollback that knows what this run did.
  ROLLBACK="$APP/rollback-access-$STAMP.sh"
  {
    echo "#!/usr/bin/env bash"
    echo "# Undo the access-control install of $STAMP."
    echo "set -u"
    echo "rm -f '$DEST/access-control.js' '$DEST/benchmark.js' '$DEST/scope.js' '$DEST/tenant-state.js' '$DEST/boot.js' '$DEST/patch-minerva-server.js' '$DEST/directory.example.json' '$DEST/orgs.example.json' '$DEST/README.md'"
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
  say "  would copy access-control.js, benchmark.js, scope.js, tenant-state.js, boot.js, patch-minerva-server.js, directory.example.json, orgs.example.json, README.md"
  say "  would create access/directory.json and access/orgs.json if absent"
  say "  would write a matching rollback script"
fi

# --- 6. what to wire in ----------------------------------------------------

head2 "Wiring it into the server"
cat <<'SNIPPET'
  The modules are copied but nothing calls them yet. For minerva-4.0/server.js
  that last step is scripted, because the file is known:

      node access/patch-minerva-server.js                    # report only
      node access/patch-minerva-server.js --apply            # write it

  It makes 23 edits, backs the file up first, refuses to write if any anchor
  has moved, and is safe to run twice. It also reports one optional fix that
  is off unless you add --fix-export.

  The server will refuse to start until ACCESS_TEAM_DOMAIN and ACCESS_AUD are
  set. pm2 keeps the environment a process started with, so exporting them and
  restarting is not enough on its own -- --update-env is what re-reads them,
  and pm2 save is what survives a reboot:

      export ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
      export ACCESS_AUD=<the AUD tag from the Access dashboard>
      pm2 restart minerva-40 --update-env
      pm2 save

  For any other server, three lines near the top:

      const { bootAccess } = require("./access/boot");
      const access = bootAccess({ appDir: __dirname });
      app.use("/api", access.guard);

  then in every handler that takes a hospital:

      access.scope.assertCcn(req, req.query.ccn);

  and one line after the last route:

      app.use(access.accessErrorHandler);
SNIPPET

say ""
say "Then restart, and check the two things that decide whether any of it holds:"
say "  curl -s localhost:3000/health          should report access: cloudflare-access"
say "  curl -s <lan-ip>:3000/health           should not answer at all"
say ""
if [ "$APPLY" != "yes" ]; then
  say "Nothing was changed. Re-run with --apply when the checks above look right."
fi
