FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu --no-cache-dir
RUN pip install --no-cache-dir -r requirements.txt

ARG CACHEBUST=1
COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
CMD ["python", "app.py"]
