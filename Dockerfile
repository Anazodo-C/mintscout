FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MINTSCOUT_STATE_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mintscout/ ./mintscout/
COPY data/fixtures/ ./data/fixtures/
COPY scripts/ ./scripts/

# Spend state must survive restarts. Without a persistent volume a crash-loop
# would reset the budget counter on every restart and the caps would mean
# nothing. Mount a Railway volume at /data.
RUN mkdir -p /data
VOLUME ["/data"]

# Fails closed: BOTH switches must be flipped in Railway variables to spend.
ENV DRY_RUN=true LIVE_EXECUTION=false

CMD ["python", "-u", "-m", "mintscout.serve"]
