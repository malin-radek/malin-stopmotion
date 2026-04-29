FROM python:3.11-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/projects

EXPOSE 8000

ENV PHONE_IP=192.168.1.100
ENV PHONE_PORT=8081

CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
