# Kibana 8.14.3（与 Elasticsearch 8.14.3 版本对齐）
# 数据可视化：可直接查看 pt-metrics-* 时序与 pt-summary 汇总索引

# 注意：kibana 是 Docker Hub 官方镜像（library 命名空间），不能写 docker.elastic.co/kibana/kibana
FROM docker.m.daocloud.io/library/kibana:8.14.3

# 预置配置：连接 ES、关闭安全认证（与 ES 容器 xpack.security.enabled=false 一致）、中文界面
COPY kibana.yml /usr/share/kibana/config/kibana.yml

EXPOSE 5601
