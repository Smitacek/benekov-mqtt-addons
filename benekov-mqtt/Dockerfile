ARG BUILD_FROM=ghcr.io/hassio-addons/base:14.1.0
FROM ${BUILD_FROM}

# Install Python and dependencies
RUN apk add --no-cache python3 py3-pip && \
    pip3 --no-cache-dir install requests paho-mqtt beautifulsoup4 lxml

# Copy service files
COPY rootfs /

# Copy app
COPY app /app

ENV PYTHONUNBUFFERED=1

