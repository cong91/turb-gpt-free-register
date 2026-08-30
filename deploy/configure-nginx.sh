#!/usr/bin/env bash
set -Eeuo pipefail

app_dir="${TURB_APP_DIR:-/srv/turb-gpt-free-register}"
domain="gpt-acc.v-claw.org"
nginx_available="/etc/nginx/sites-available/${domain}.conf"
nginx_enabled="/etc/nginx/sites-enabled/${domain}.conf"
acme_config="${app_dir}/deploy/nginx/${domain}.acme.conf"
final_config="${app_dir}/deploy/nginx/${domain}.conf"
webroot="/var/www/certbot"

test -f "${acme_config}"
test -f "${final_config}"
sudo install -d -m 0755 "${webroot}"
sudo install -m 0644 "${acme_config}" "${nginx_available}"
sudo ln -sfn "${nginx_available}" "${nginx_enabled}"
sudo nginx -t
sudo systemctl reload nginx

certbot_args=(
  certonly
  --webroot
  -w "${webroot}"
  -d "${domain}"
  --agree-tos
  --non-interactive
  --keep-until-expiring
)
if [[ -n "${CERTBOT_EMAIL:-}" ]]; then
  certbot_args+=(--email "${CERTBOT_EMAIL}")
else
  certbot_args+=(--register-unsafely-without-email)
fi
sudo certbot "${certbot_args[@]}"

sudo install -m 0644 "${final_config}" "${nginx_available}"
sudo nginx -t
sudo systemctl reload nginx
echo "Nginx HTTPS proxy configured for ${domain}"
