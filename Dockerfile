FROM python:3.12-slim

WORKDIR /app/realtime

# System libs required by opencv (imported by pipecat's webrtc transport)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libglib2.0-0 libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Agent config (data/agents.json, data/settings.json) is mounted at /app/data
EXPOSE 7860

CMD ["python", "bot.py", "--host", "0.0.0.0", "--port", "7860"]
