FROM node:24-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY scripts/vendor.mjs ./scripts/vendor.mjs
RUN node scripts/vendor.mjs

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data HOME=/tmp XDG_CACHE_HOME=/data/cache
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY tracker ./tracker
COPY tests ./tests
COPY scripts ./scripts
COPY README.md ./README.md
COPY LICENSE ./LICENSE
COPY --from=assets /build/tracker/static/vendor ./tracker/static/vendor
USER 1000:1000
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "tracker.api:app", "--host", "0.0.0.0", "--port", "8000"]
