# Test harness: runs udockerd unprivileged, same shape as the real Termux deployment.
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gcc libc6-dev git && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m udocker
USER udocker
# RUN doesn't set $HOME like a login shell; needed for pip --user and udocker's Path.home() lookups.
ENV HOME=/home/udocker
WORKDIR /home/udocker/app

COPY --chown=udocker:udocker pyproject.toml /home/udocker/app/pyproject.toml
COPY --chown=udocker:udocker src /home/udocker/app/src
COPY --chown=udocker:udocker .git /home/udocker/app/.git

RUN pip install --user --no-cache-dir .

ENV PATH="/home/udocker/.local/bin:${PATH}"

EXPOSE 2375
ENTRYPOINT ["python3", "-m", "udockerd", "--host", "0.0.0.0", "--port", "2375"]
