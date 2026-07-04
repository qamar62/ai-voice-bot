FROM python:3.12-slim

WORKDIR /app/realtime

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Agent config (data/agents.json, data/settings.json) is mounted at /app/data
EXPOSE 7860

CMD ["python", "bot.py", "--host", "0.0.0.0", "--port", "7860"]
