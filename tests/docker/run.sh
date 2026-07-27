#!/usr/bin/env bash
# Build the test harness image and run udockerd in an unprivileged
# container, exposing it on the host so a real `docker` CLI can be
# pointed at it via DOCKER_HOST.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="udockerd-test-harness"
HOST_PORT="${UDOCKERD_TEST_PORT:-12375}"

docker build -f "${REPO_ROOT}/tests/docker/Dockerfile" -t "${IMAGE_TAG}" "${REPO_ROOT}"

docker run --rm \
    --name udockerd-test-harness \
    -p "127.0.0.1:${HOST_PORT}:2375" \
    "${IMAGE_TAG}" &

echo "udockerd test harness running, DOCKER_HOST=tcp://127.0.0.1:${HOST_PORT}"
echo "e.g.: DOCKER_HOST=tcp://127.0.0.1:${HOST_PORT} docker version"
wait
