// Jenkins Pipeline：构建 master/agent/kibana 镜像 → 推送阿里云 ACR → SSH 远程部署
//
// 前置条件（Jenkins 凭据配置）：
//   1) ACR_DOCKER_CONFIG   — Secret file，内容为 `docker login` 后生成的 ~/.docker/config.json
//   2) SSH_KEY             — Secret file，目标部署服务器的 SSH 私钥（对应目标机 authorized_keys）
//   3) DEPLOY_HOST         — Secret text，格式 user@ip（如 deployer@10.0.0.10）
//   4) PTP_ENV_FILE        — Secret file，目标机 deploy/.env 的完整内容（gitignored，不入库）
//
// 目标服务器要求：已安装 docker + docker compose plugin，SSH 公钥已加入 authorized_keys，
//   部署目录 /opt/ptp 已存在且 SSH 用户有写权限。

pipeline {
    agent any

    environment {
        // 阿里云容器镜像服务（ACR）命名空间
        REGISTRY    = 'registry.cn-hangzhou.aliyuncs.com/ptp'
        // 镜像标签 = 构建号 + git 短 sha，便于追溯与回滚；同时打 latest
        IMAGE_TAG   = "${env.BUILD_NUMBER}-${env.GIT_COMMIT.take(7)}"
        // 目标部署路径
        DEPLOY_DIR  = '/opt/ptp'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Build & Push Images') {
            steps {
                script {
                    // 用 ACR 的 docker config.json 登录（凭据类型：Secret file）
                    withCredentials([file(credentialsId: 'ACR_DOCKER_CONFIG', variable: 'DOCKER_CONFIG_FILE')]) {
                        sh '''
                            mkdir -p ~/.docker
                            cp "$DOCKER_CONFIG_FILE" ~/.docker/config.json
                            chmod 600 ~/.docker/config.json
                        '''
                    }

                    // 构建并推送 master / agent / kibana
                    // 每个镜像：build → tag:版本 → tag:latest → push 两个标签
                    def images = [
                        [name: 'ptp-master', context: 'master',         dockerfile: 'master/Dockerfile'],
                        [name: 'ptp-agent',  context: 'agent',          dockerfile: 'agent/Dockerfile'],
                        [name: 'ptp-kibana', context: 'deploy/kibana',  dockerfile: 'deploy/kibana/Dockerfile']
                    ]
                    images.each { img ->
                        sh """
                            docker build -f ${img.dockerfile} -t ${REGISTRY}/${img.name}:${IMAGE_TAG} ${img.context}
                            docker tag  ${REGISTRY}/${img.name}:${IMAGE_TAG} ${REGISTRY}/${img.name}:latest
                            docker push ${REGISTRY}/${img.name}:${IMAGE_TAG}
                            docker push ${REGISTRY}/${img.name}:latest
                        """
                    }
                }
            }
        }

        stage('Deploy Master to Remote Server') {
            steps {
                script {
                    withCredentials([
                        file(credentialsId: 'SSH_KEY',       variable: 'SSH_KEY_FILE'),
                        text(credentialsId: 'DEPLOY_HOST',   variable: 'DEPLOY_HOST'),
                        file(credentialsId: 'PTP_ENV_FILE',  variable: 'ENV_FILE')
                    ]) {
                        sh '''
                            set -e

                            # 1. 同步 compose 文件到目标机（仅 compose，不含源码）
                            scp -i "$SSH_KEY_FILE" -o StrictHostKeyChecking=no \\
                                deploy/docker-compose.yml \\
                                deploy/docker-compose.agent-prod.yml \\
                                "$DEPLOY_HOST:$DEPLOY_DIR/"

                            # 2. 同步 .env（gitignored，从 Jenkins 凭据注入）
                            scp -i "$SSH_KEY_FILE" -o StrictHostKeyChecking=no \\
                                "$ENV_FILE" "$DEPLOY_HOST/.env"

                            # 3. SSH 到目标机执行 pull + up
                            #    注意：显式 -f docker-compose.yml 会禁用 override.yml 自动加载，生产仅 pull 不构建
                            ssh -i "$SSH_KEY_FILE" -o StrictHostKeyChecking=no "$DEPLOY_HOST" \\
                                "cd $DEPLOY_DIR && \\
                                 export REGISTRY=$REGISTRY IMAGE_TAG=$IMAGE_TAG && \\
                                 docker compose -f docker-compose.yml pull && \\
                                 docker compose -f docker-compose.yml up -d"
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            // 清理本次构建产生的悬空镜像（保留 latest 不影响下次 pull）
            sh 'docker image prune -f || true'
        }
        success {
            echo "✅ 部署完成：master ${IMAGE_TAG} 已上线"
        }
        failure {
            echo "❌ 流水线失败，请查看上方日志"
        }
    }
}
