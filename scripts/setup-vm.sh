#!/bin/bash
# ========================================================================
# Setup inicial da VM (Ubuntu 22.04 LTS)
# Executar como root na DigitalOcean Droplet logo após criar
#
# Uso:
#   curl -sL https://raw.githubusercontent.com/.../scripts/setup-vm.sh | bash
#   ou copiar e correr: bash scripts/setup-vm.sh
# ========================================================================
set -euo pipefail

echo "==> Actualizando sistema..."
apt-get update -qq && apt-get upgrade -y -qq

echo "==> Instalando dependências..."
apt-get install -y -qq \
  curl git ufw fail2ban \
  ca-certificates gnupg lsb-release

echo "==> Instalando Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable --now docker
echo "==> Docker $(docker --version) instalado"

echo "==> Configurando firewall (UFW)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
# Não abrir 5678 — o acesso é só pelo Cloudflare Tunnel
ufw --force enable
echo "==> Firewall activo (só SSH permitido inbound)"

echo "==> Configurando fail2ban..."
systemctl enable --now fail2ban

echo "==> Criando utilizador deploy (sem root)..."
if ! id "deploy" &>/dev/null; then
  useradd -m -s /bin/bash deploy
  usermod -aG docker deploy
  # Copiar chaves SSH do root para deploy
  mkdir -p /home/deploy/.ssh
  cp /root/.ssh/authorized_keys /home/deploy/.ssh/ 2>/dev/null || true
  chown -R deploy:deploy /home/deploy/.ssh
  chmod 700 /home/deploy/.ssh
  chmod 600 /home/deploy/.ssh/authorized_keys 2>/dev/null || true
fi
echo "==> Utilizador 'deploy' criado (usa 'su - deploy' para mudar)"

echo ""
echo "========================================="
echo " Setup completo! Próximos passos:"
echo "========================================="
echo " 1. su - deploy"
echo " 2. git clone <repo> ~/app && cd ~/app"
echo " 3. cp .env.prod.example .env.prod && nano .env.prod"
echo " 4. bash scripts/deploy.sh"
echo "========================================="
