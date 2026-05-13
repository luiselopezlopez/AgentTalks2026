FROM node:20-bookworm-slim AS frontend-build

WORKDIR /app

COPY package*.json ./
COPY tsconfig*.json ./
COPY vite.config.ts ./
COPY eslint.config.js ./
COPY index.html ./
COPY public ./public
COPY src ./src

RUN npm install
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/luiseagent

COPY luiseagent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY luiseagent ./
COPY --from=frontend-build /app/dist /app/dist

EXPOSE 8000

CMD ["/bin/sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]