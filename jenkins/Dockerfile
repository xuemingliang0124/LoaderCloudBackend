# 固定具体 LTS 版本 + JDK21（2.545+ 要求 Java21；写死版本可绕过镜像加速器缓存旧 lts）
FROM jenkins/jenkins:2.568.3-lts-jdk21

USER root

# 启动时动态对齐宿主机 docker.sock 的 GID（不同机器 GID 不一致，不能写死）
COPY entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 官方源拉取慢时，可在 compose 中传入阿里云镜像：
#   args:
#     DOCKER_APT_MIRROR: https://mirrors.aliyun.com/docker-ce
ARG DOCKER_APT_MIRROR=https://download.docker.com

# 只添加 Docker 官方源并安装 docker CLI + compose 插件。
# 注意：基础镜像已自带 curl/ca-certificates，切勿显式 install 它们，
# 否则 apt 会尝试升级到 bullseye-security 中已被归档删除的版本，导致 404。
RUN set -eux; \
    . /etc/os-release; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL "${DOCKER_APT_MIRROR}/linux/debian/gpg" -o /etc/apt/keyrings/docker.asc; \
    chmod a+r /etc/apt/keyrings/docker.asc; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] ${DOCKER_APT_MIRROR}/linux/debian ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin; \
    docker --version; \
    docker compose version; \
    rm -rf /var/lib/apt/lists/*

# 安装 Ansible + community.docker collection（Jenkinsfile.agents 部署 Agent 需要）
# ansible-core 轻量够用，无需完整 ansible 包；
# collection 必须装到系统路径 /usr/share/ansible/collections，
# 否则装到 root 家目录后，降权的 jenkins 用户无法访问
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3-pip; \
    pip3 install --break-system-packages --no-cache-dir "ansible-core>=2.14"; \
    ansible-galaxy collection install community.docker -p /usr/share/ansible/collections; \
    ansible-playbook --version; \
    rm -rf /var/lib/apt/lists/*

# 容器以 root 启动，entrypoint 中完成 GID 对齐后降权为 jenkins
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
