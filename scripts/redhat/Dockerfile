FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils cloud-image-utils curl ca-certificates openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
