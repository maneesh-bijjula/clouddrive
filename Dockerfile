FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY tsconfig.json ./
COPY frontend ./frontend
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 app
COPY clouddrive ./clouddrive
COPY --from=frontend /build/dist ./dist
USER app
EXPOSE 8090
CMD ["uvicorn", "clouddrive.api:app", "--host", "0.0.0.0", "--port", "8090"]
