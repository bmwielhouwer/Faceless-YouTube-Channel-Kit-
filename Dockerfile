# Always-on worker image for the Faceless YouTube Kit pipeline.
# Runs identically on Railway, Render, Fly.io, or any container host.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ffmpeg (with libfreetype/libass for drawtext + subtitles) and fonts for the
# self-hosted video renderer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Long-running poller. Configuration comes entirely from environment variables
# (set them in your host's dashboard — do NOT bake secrets into the image).
CMD ["python", "run.py"]
