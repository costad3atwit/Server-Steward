FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STEWARD_CONFIG=/config/steward.yaml \
    STEWARD_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home steward
COPY steward_agent ./steward_agent

USER 10001
CMD ["python", "-m", "steward_agent.main"]
