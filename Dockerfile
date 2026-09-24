FROM ubuntu:24.04
ARG APP_VERSION=0.4.0
ARG BUILD_SHA=dev
ENV APP_VERSION=${APP_VERSION}
ENV BUILD_SHA=${BUILD_SHA}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends     python3 python3-pip smartmontools util-linux snapraid ca-certificates docker.io  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8099
CMD ["gunicorn","-w","1","--threads","4","-b","0.0.0.0:8099","--timeout","180","app:app"]
