# ---- Base ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Optional but nice to have (correct time + smaller image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

# ---- Workdir ----
WORKDIR /app

# ---- Dependencies ----
# requirements.txt is still inside backend/, so copy it explicitly
COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ---- App code ----
# Copy ONLY the backend app into /app
COPY backend/ ./

# ---- Runtime ----
ENV PORT=8080
EXPOSE 8080

# Cloud Run sets $PORT; use sh to expand it
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080}"]
