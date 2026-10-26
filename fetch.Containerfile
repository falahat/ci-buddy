FROM docker.io/library/rust:1.99.0-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git openssh-client python3
COPY fetch.sh /work/fetch.sh
WORKDIR /work/src
