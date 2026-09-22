FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装依赖（使用阿里镜像源加速，并带官方源作为自动回退）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --extra-index-url https://pypi.org/simple \
    -r requirements.txt

# 复制代码与示例配置
COPY simsync/ ./simsync/
COPY config.example.yaml ./config.example.yaml

# 创建数据持久化目录
RUN mkdir -p /app/data

EXPOSE 8088

# 启动命令
CMD ["python", "-m", "simsync.main", "-c", "/app/data/config.yaml"]
