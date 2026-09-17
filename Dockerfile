FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 PYTHONUNBUFFERED=1 \
    PX_SOLVER=swiftshader PX_SWIFTSHADER_HEADFUL=1 PX_PRESS_BACKEND=xdotool \
    PORT=8890 OUTLOOK_DB_PATH=/app/data/outlook.db

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11-utils xdotool \
    ca-certificates wget gnupg fonts-liberation \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 libx11-6 libxcb1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt px_solver/docker/requirements-linux.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-linux.txt \
    && python -m patchright install --with-deps chromium

COPY . /app/
RUN chmod +x /app/px_solver/docker/entrypoint.sh \
    && mkdir -p /app/data /app/accounts

EXPOSE 8890
ENTRYPOINT ["/app/px_solver/docker/entrypoint.sh"]
CMD ["webapp"]
