FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# 安装 Node.js 与编译基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

ENV CLOAKBROWSER_SUPPRESS_FONT_WARNING=1

COPY . .

EXPOSE 5000

ENV HOST=0.0.0.0 \
    PORT=5000

CMD ["python", "web.py", "--host", "0.0.0.0", "--port", "5000"]
