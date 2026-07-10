#!/usr/bin/env bash
# Gitea — the vault's GitHub-like web UI (localhost-only, sqlite, no cloud).
# Browsing, diffs, blame, search, issues/wiki if you want them — over mirrors
# of the vault's bare repos, which remain the source of truth.
#
#   bash connectors/gitea/setup-gitea.sh install    brew install + config + admin user + service
#   bash connectors/gitea/setup-gitea.sh start|stop|status
#
# After install: open http://127.0.0.1:3300, sign in, then for each vault repo:
#   + New Migration -> Git -> URL: file:///Users/<you>/oracle-git-vault/<name>.git
#   -> tick "This repository will be a mirror"
# Mirrors re-sync from the vault periodically, so Gitea always shows current
# history without ever being written to directly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.sentivue.gitea"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GITEA_HOME="$HOME/.oracle-gitea"
APPINI="$GITEA_HOME/app.ini"
LOGDIR="$ROOT/logs"

write_config() {
  mkdir -p "$GITEA_HOME" "$LOGDIR"
  if [[ ! -f "$APPINI" ]]; then
    SECRET="$(openssl rand -hex 24)"
    cat > "$APPINI" <<EOF
APP_NAME = Oracle Vault
RUN_MODE = prod
WORK_PATH = $GITEA_HOME

[server]
PROTOCOL  = http
HTTP_ADDR = 127.0.0.1
HTTP_PORT = 3300
ROOT_URL  = http://127.0.0.1:3300/
DISABLE_SSH = true
OFFLINE_MODE = true

[database]
DB_TYPE = sqlite3
PATH    = $GITEA_HOME/gitea.db

[security]
INSTALL_LOCK = true
SECRET_KEY   = $SECRET
IMPORT_LOCAL_PATHS = true

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW  = false

[mirror]
ENABLE = true
DEFAULT_INTERVAL = 1h

[repository]
ROOT = $GITEA_HOME/repositories

[other]
SHOW_FOOTER_VERSION = false
EOF
    echo "wrote $APPINI"
  fi
}

write_plist() {
  GITEA_BIN="$(command -v gitea || echo /opt/homebrew/bin/gitea)"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$GITEA_BIN</string><string>web</string>
    <string>--config</string><string>$APPINI</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/gitea.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/gitea.err.log</string>
</dict></plist>
EOF
}

case "${1:-}" in
  install)
    command -v gitea >/dev/null || { echo "==> brew install gitea"; brew install gitea && brew pin gitea; }
    write_config
    if ! gitea admin user list --config "$APPINI" 2>/dev/null | grep -q oracle; then
      PW="$(openssl rand -base64 15)"
      gitea admin user create --config "$APPINI" --admin \
        --username oracle --password "$PW" --email oracle@localhost --must-change-password=false
      echo "=============================================="
      echo " Gitea admin login:  oracle / $PW"
      echo " (change it in the UI; it is not stored anywhere)"
      echo "=============================================="
    fi
    write_plist
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Gitea: http://127.0.0.1:3300  (mirror vault repos via + New Migration -> file:// path)"
    ;;
  start)   write_config; write_plist
           launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
           launchctl bootstrap "gui/$(id -u)" "$PLIST"
           echo "Gitea starting on http://127.0.0.1:3300" ;;
  stop)    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; echo "Gitea stopped" ;;
  status)  curl -sf -m 3 http://127.0.0.1:3300/api/healthz >/dev/null 2>&1 \
             && echo "Gitea: HEALTHY (http://127.0.0.1:3300)" || echo "Gitea: DOWN" ;;
  *) echo "usage: setup-gitea.sh {install|start|stop|status}"; exit 1 ;;
esac
