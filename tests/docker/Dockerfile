# Test harness image: runs udockerd unprivileged, same shape as the real
# Termux deployment (no host docker socket, no privileged mode, no
# extra capabilities). udocker itself runs entirely as a non-root user.
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m udocker
USER udocker
WORKDIR /home/udocker/app

COPY --chown=udocker:udocker pyproject.toml /home/udocker/app/pyproject.toml
COPY --chown=udocker:udocker src /home/udocker/app/src

RUN pip install --user --no-cache-dir .

ENV PATH="/home/udocker/.local/bin:${PATH}"

EXPOSE 2375
ENTRYPOINT ["python3", "-m", "udockerd", "--host", "0.0.0.0", "--port", "2375"]
