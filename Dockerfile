FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE ${PORT:-8080}
ENV FORWARDED_ALLOW_IPS=*
CMD ["sh", "-c", "uvicorn app.main:application --host 0.0.0.0 --port ${PORT:-8080} \
     --http h11 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
