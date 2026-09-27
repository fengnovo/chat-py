FROM node:24.14.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      file \
      fonts-dejavu-core \
      git \
      python3 \
      python3-pip \
      python-is-python3 \
      zip \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --break-system-packages --no-cache-dir \
      matplotlib \
      openpyxl \
      pillow \
      python-docx \
      python-pptx \
      reportlab

# 只预装离线 React/Vite 依赖，不预置用户网站源码。用户项目完全由 Agent
# 写入本轮沙箱工作区，再通过符号链接复用只读 node_modules。
COPY web-runtime/package.json /opt/chat-web-runtime/package.json
RUN cd /opt/chat-web-runtime \
    && npm install --no-audit --no-fund
COPY prepare-web-project /usr/local/bin/prepare-web-project
RUN chmod 0755 /usr/local/bin/prepare-web-project

WORKDIR /mnt/user-data/workspace

CMD ["/bin/bash"]
