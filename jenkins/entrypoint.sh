#!/bin/bash
set -e

JENKINS_HOME_DIR="${JENKINS_HOME:-/var/jenkins_home}"

# ===== 1. 探测可用的国内 Jenkins 镜像 =====
# 清华/北外等镜像对云服务器 IP 有反爬拦截，不同机房可达性不同，
# 因此依次探测，选用第一个返回合法更新中心数据（含 connectionCheckUrl）的镜像。
MIRRORS=(
  "https://mirrors.tuna.tsinghua.edu.cn/jenkins"
  "https://mirrors.bfsu.edu.cn/jenkins"
  "https://mirrors.ustc.edu.cn/jenkins"
  "https://mirror.bit.edu.cn/jenkins"
  "https://mirrors.huaweicloud.com/jenkins"
)

MIRROR_BASE=""
for base in "${MIRRORS[@]}"; do
  if curl -fsSL --connect-timeout 5 --max-time 15 "${base}/updates/update-center.json" \
       | grep -q '"connectionCheckUrl"'; then
    MIRROR_BASE="$base"
    break
  fi
done

# 全部国内镜像不可达时回退官方源（至少保证配置完整）
if [ -z "$MIRROR_BASE" ]; then
  echo "[jenkins-entrypoint] WARNING: 国内镜像均不可达，回退官方更新中心" >&2
  MIRROR_BASE="https://updates.jenkins.io"
  UC_URL="${MIRROR_BASE}/update-center.json"
else
  UC_URL="${MIRROR_BASE}/updates/update-center.json"
fi
echo "[jenkins-entrypoint] 使用更新中心: ${UC_URL}"

# ===== 2. 写入更新中心配置 =====
# 直接覆盖写入，兼容已初始化过的 jenkins_home 卷（ref 预置对旧卷不生效）
UC_XML="${JENKINS_HOME_DIR}/hudson.model.UpdateCenter.xml"
cat > "$UC_XML" <<EOF
<?xml version='1.1' encoding='UTF-8'?>
<sites>
  <site>
    <id>default</id>
    <url>${UC_URL}</url>
  </site>
</sites>
EOF
chown jenkins:jenkins "$UC_XML"

# ===== 3. 后台守护：替换 default.json 中的插件下载地址 =====
# update-center.json 虽从镜像拉取，但里面的插件下载 URL 仍指向官方源，
# 且 Jenkins 会定期重新下载覆盖该文件，所以用守护进程在其每次变化后重新替换。
(
  UPDATES_FILE="${JENKINS_HOME_DIR}/updates/default.json"
  LAST_SIG=""
  while true; do
    if [ -f "$UPDATES_FILE" ]; then
      SIG=$(stat -c '%Y:%s' "$UPDATES_FILE" 2>/dev/null || echo "")
      if [ -n "$SIG" ] && [ "$SIG" != "$LAST_SIG" ]; then
        if [ "$MIRROR_BASE" = "https://updates.jenkins.io" ]; then
          DOWNLOAD_BASE="${MIRROR_BASE}/download"
        else
          DOWNLOAD_BASE="${MIRROR_BASE}"
        fi
        sed -i \
          -e "s#https://updates.jenkins.io/download#${DOWNLOAD_BASE}#g" \
          -e 's#http://www.google.com#https://www.baidu.com#g' \
          -e 's#https://www.google.com#https://www.baidu.com#g' \
          "$UPDATES_FILE"
        # sed -i 会替换 inode，必须把属主改回 jenkins，否则其自身无法再更新该文件
        chown jenkins:jenkins "$UPDATES_FILE"
        LAST_SIG=$(stat -c '%Y:%s' "$UPDATES_FILE" 2>/dev/null || echo "")
      fi
    fi
    sleep 10
  done
) &

# ===== 4. 预置 GitHub SSH host key =====
# 避免 Jenkins 首次通过 SSH 拉代码时因 known_hosts 为空报 Host key verification failed
SSH_DIR="${JENKINS_HOME_DIR}/.ssh"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
mkdir -p "$SSH_DIR"
if ! grep -q '^github.com ' "$KNOWN_HOSTS" 2>/dev/null; then
  ssh-keyscan -t ed25519,rsa,ecdsa github.com >> "$KNOWN_HOSTS" 2>/dev/null || true
fi
chown -R jenkins:jenkins "$SSH_DIR"
chmod 700 "$SSH_DIR"

# ===== 5. 对齐宿主机 docker.sock 的 GID =====
SOCK_GID=$(stat -c '%g' /var/run/docker.sock)

if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
  groupadd -g "$SOCK_GID" docker-host
fi

GROUP_NAME=$(getent group "$SOCK_GID" | cut -d: -f1)
usermod -aG "$GROUP_NAME" jenkins

# 修正 HOME：setpriv 只切 uid 不改环境变量，HOME 仍为 /root，
# 导致 docker login 无法写入 ~/.docker/config.json。显式设为 jenkins 的家目录。
export HOME="${JENKINS_HOME_DIR}"

# 降权为 jenkins 用户并启动官方启动脚本。
# 使用 setpriv（util-linux 自带，新版 jenkins 镜像已不含 gosu），
# --init-groups 会根据 /etc/group 初始化附属组（含上面加入的 socket 组），
# exec 后 Java 直接成为 PID 1，SIGTERM 等信号可正常透传。
JENKINS_UID=$(id -u jenkins)
JENKINS_GID=$(id -g jenkins)
exec setpriv --reuid="$JENKINS_UID" --regid="$JENKINS_GID" --init-groups \
  /usr/local/bin/jenkins.sh "$@"
