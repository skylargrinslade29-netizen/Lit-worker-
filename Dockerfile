FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /worker
COPY video_prepare.py lit_worker.py /worker/
RUN useradd --uid 10001 --create-home litworker
USER 10001
ENTRYPOINT ["python", "/worker/lit_worker.py"]
