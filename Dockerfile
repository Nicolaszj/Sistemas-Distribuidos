FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dfsha ./dfsha

ENV DFSHA_HOME=/data
VOLUME /data
EXPOSE 8000

# TLS: montar los certificados y definir DFSHA_TLS_CERT / DFSHA_TLS_KEY.
CMD ["python", "-m", "dfsha.server.run"]
