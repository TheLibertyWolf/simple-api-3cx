#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo 'Exécuter comme root : wget -qO- https://raw.githubusercontent.com/TheLibertyWolf/simple-api-3cx/main/install.sh | sudo bash' >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMP_REPO=''
if [[ ! -f "$SCRIPT_DIR/simple_api_3cx.py" ]]; then
  TEMP_REPO="$(mktemp -d)"
  trap 'rm -rf "$TEMP_REPO"' EXIT
  git clone --depth 1 https://github.com/TheLibertyWolf/simple-api-3cx.git "$TEMP_REPO/source"
  SCRIPT_DIR="$TEMP_REPO/source"
fi

if ! id phonesystem >/dev/null 2>&1; then
  echo '3CX (utilisateur phonesystem) est introuvable sur ce serveur.' >&2
  exit 1
fi

NGINX_CONFIG=/var/lib/3cxpbx/Bin/nginx/conf/nginx.conf
if [[ ! -f "$NGINX_CONFIG" ]]; then
  echo "Configuration nginx 3CX introuvable : $NGINX_CONFIG" >&2
  exit 1
fi

DOMAIN="$(sed -nE 's/^[[:space:]]*server_name[[:space:]]+([^;]+);/\1/p' "$NGINX_CONFIG" | head -1 | awk '{print $1}')"
DOMAIN="${DOMAIN//$'\r'/}"
if [[ -z "$DOMAIN" ]]; then
  echo 'Domaine 3CX introuvable dans nginx.' >&2
  exit 1
fi

install -d -m 755 /opt/simple-api-3cx
install -m 644 "$SCRIPT_DIR/simple_api_3cx.py" /opt/simple-api-3cx/simple_api_3cx.py
install -d -m 750 -o phonesystem -g phonesystem /var/lib/simple-api-3cx
install -m 755 "$SCRIPT_DIR/scripts/simple-api-3cx" /usr/local/bin/simple-api-3cx

# 3CX v20 Update 9 uses .NET 10 for its local QueueAgent configuration API.
# Keep the SDK private to this application and build against the PBX assembly
# installed on this server, so a one-command install supports selective queues.
DOTNET_DIR=/opt/simple-api-3cx/dotnet
if [[ ! -x "$DOTNET_DIR/dotnet" ]] || ! compgen -G "$DOTNET_DIR/sdk/10.*" >/dev/null; then
  SDK_INSTALLER="$(mktemp)"
  curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$SDK_INSTALLER"
  bash "$SDK_INSTALLER" --channel 10.0 --install-dir "$DOTNET_DIR" --no-path
  rm -f "$SDK_INSTALLER"
fi
install -d -m 755 /opt/simple-api-3cx/queue_control
DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
  "$DOTNET_DIR/dotnet" build "$SCRIPT_DIR/queue_control/QueueControl.csproj" \
  -c Release -o /opt/simple-api-3cx/queue_control --nologo -v:q

if [[ ! -f /var/lib/simple-api-3cx/config.json ]]; then
  runuser -u phonesystem -- env SIMPLE_API_3CX_DOMAIN="$DOMAIN" SIMPLE_API_3CX_URL="https://$DOMAIN" \
    /usr/bin/python3 /opt/simple-api-3cx/simple_api_3cx.py init
  /usr/bin/python3 /opt/simple-api-3cx/simple_api_3cx.py key create initial-admin \
    --scopes read,control,admin > /root/simple-api-3cx-credentials.txt
  chmod 600 /root/simple-api-3cx-credentials.txt
else
  /usr/bin/python3 - "$DOMAIN" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path=Path('/var/lib/simple-api-3cx/config.json')
data=json.loads(path.read_text())
data['sip_domain']=sys.argv[1]
data['public_url']='https://'+sys.argv[1]
stat=path.stat()
fd,name=tempfile.mkstemp(prefix='.config-',dir=path.parent)
try:
    os.fchmod(fd,0o600)
    os.fchown(fd,stat.st_uid,stat.st_gid)
    with os.fdopen(fd,'w') as stream:
        json.dump(data,stream,indent=2,sort_keys=True)
        stream.write('\n')
    os.replace(name,path)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY
fi

cat > /etc/systemd/system/simple-api-3cx.service <<'UNIT'
[Unit]
Description=Simple API 3cx
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=phonesystem
Group=phonesystem
WorkingDirectory=/opt/simple-api-3cx
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/simple-api-3cx/simple_api_3cx.py serve
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/simple-api-3cx
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT

BACKUP="$NGINX_CONFIG.simple-api-3cx.$(date +%Y%m%d%H%M%S).bak"
cp -a "$NGINX_CONFIG" "$BACKUP"
python3 - "$NGINX_CONFIG" <<'PY'
from pathlib import Path
import sys

path=Path(sys.argv[1])
text=path.read_text()
marker='        # simple-api-3cx managed location\n'
if marker not in text:
    needle='        location / {'
    if needle not in text:
        raise SystemExit('Point d’insertion nginx introuvable')
    block=(
        marker+
        '        location ^~ /simple-api-3cx/ {\n'
        '            proxy_pass http://127.0.0.1:18081;\n'
        '            proxy_set_header Host $host;\n'
        '            proxy_set_header X-Real-IP $remote_addr;\n'
        '            proxy_set_header X-Forwarded-Proto $scheme;\n'
        '            access_log off;\n'
        '            client_max_body_size 8k;\n'
        '        }\n\n'
    )
    path.write_text(text.replace(needle,block+needle,1))
PY

if ! nginx -t; then
  cp -a "$BACKUP" "$NGINX_CONFIG"
  echo 'Configuration nginx rétablie après échec du contrôle.' >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now simple-api-3cx
systemctl restart simple-api-3cx
systemctl reload nginx

HEALTHY=0
for _ in {1..25}; do
  if curl --fail --silent http://127.0.0.1:18081/simple-api-3cx/v1/health >/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 0.2
done
if [[ $HEALTHY -ne 1 ]]; then
  echo 'Le service ne répond pas ; consulter journalctl -u simple-api-3cx.' >&2
  exit 1
fi

echo
echo 'Simple API 3cx est installée.'
echo "Documentation : https://$DOMAIN/simple-api-3cx/v1/help"
echo "Santé : https://$DOMAIN/simple-api-3cx/v1/health"
echo 'Administration : sudo simple-api-3cx'
echo 'Identifiant initial : initial-admin'
echo 'Clé initiale : /root/simple-api-3cx-credentials.txt (affichable par root uniquement)'
echo 'Par défaut, seules les IP locales sont autorisées pour les données et actions.'
echo 'Autoriser un client HTTP : sudo simple-api-3cx allow add <IP-ou-CIDR>'
