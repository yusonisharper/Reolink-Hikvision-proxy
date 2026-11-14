FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bcproxy ./bcproxy
# H.265 tile merging library (bcproxy/hevcmerge.py)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && sh bcproxy/native/build.sh \
    && apt-get purge -y gcc libc6-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY tools ./tools
# 建议以 macvlan 网络运行，让每台虚拟摄像头拥有独立 IP
CMD ["python", "-m", "bcproxy", "-c", "/config/config.yaml"]
