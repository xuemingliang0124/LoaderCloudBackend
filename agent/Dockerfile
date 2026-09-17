FROM docker.m.daocloud.io/library/python:3.12.14-slim

ENV TZ=Asia/Shanghai

# 换阿里云 debian 源（国内构建加速）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-21-jre curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 安装 JMeter 5.6.3（国内可改用 https://mirrors.aliyun.com/apache/jmeter/...）
RUN curl -fsSL https://mirrors.aliyun.com/apache/jmeter/binaries/apache-jmeter-5.6.3.tgz \
        -o /tmp/jmeter.tgz \
    && mkdir -p /opt/jmeter \
    && tar -xzf /tmp/jmeter.tgz -C /opt/jmeter --strip-components=1 \
    && rm /tmp/jmeter.tgz

# 安装第三方插件（脚本用到什么装什么，放 lib/ext 即可；走阿里云 Maven 源）
# - Random CSV Data Set Config（com.blazemeter.jmeter.RandomCSVDataSetConfig）
# - cmn-jmeter 为上述插件的公共依赖
RUN curl -fsSL https://maven.aliyun.com/repository/public/com/blazemeter/jmeter-plugins-random-csv-data-set/0.8/jmeter-plugins-random-csv-data-set-0.8.jar \
        -o /opt/jmeter/lib/ext/jmeter-plugins-random-csv-data-set-0.8.jar \
    && curl -fsSL https://maven.aliyun.com/repository/public/kg/apc/jmeter-plugins-cmn-jmeter/0.6/jmeter-plugins-cmn-jmeter-0.6.jar \
        -o /opt/jmeter/lib/ext/jmeter-plugins-cmn-jmeter-0.6.jar

ENV JMETER_HOME=/opt/jmeter
ENV PATH="$JMETER_HOME/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pt_agent ./pt_agent

CMD ["python", "-m", "pt_agent.main"]
