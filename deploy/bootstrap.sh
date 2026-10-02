#!/usr/bin/env bash
# Prepare a fresh Ubuntu/Debian server (run ONCE, as root, on the server):
#   ssh root@SERVER 'bash -s' < deploy/bootstrap.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl rsync ufw fail2ban unattended-upgrades

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh      # Docker's official install script
fi
systemctl enable --now docker

# Firewall: SSH, and the web server for the media links. Nothing else is reachable.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Key-only SSH, but only if a key is already installed (never lock yourself out).
if [ -s /root/.ssh/authorized_keys ]; then
  printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/10-dk.conf
  systemctl reload ssh || systemctl reload sshd || true
else
  echo "WARNING: no SSH key installed for root, so password login stays on. Add one, then re-run."
fi

systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades || true

# Folders. 10001 is the user inside the app image.
mkdir -p /srv/dk/app /srv/dk/secrets /srv/dk/backups
chown 10001:10001 /srv/dk/secrets
chmod 700 /srv/dk/secrets /srv/dk/backups
echo "Server ready. Next, from your computer: deploy/deploy.sh root@THIS_SERVER media.example.com --secrets"
