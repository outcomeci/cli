#!/usr/bin/env bash
# Disposable RHEL-compatible VM; requires Docker, KVM, curl, and OpenSSH.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
lab=${OUTCOMECI_REDHAT_LAB:-/tmp/outcome-redhat-lab}
vm=outcome-redhat-vm
harness=outcome-redhat-lab:local
port=22229
mkdir -p "$lab"
ssh_args=(-i "$lab/id_ed25519" -p "$port" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$lab/known_hosts")
case ${1:-help} in
  up)
    if docker container inspect "$vm" >/dev/null 2>&1; then
      echo "$vm already exists; use '$0 test' or '$0 down'."
      exit 0
    fi
    test -c /dev/kvm
    docker build --network host -t "$harness" "$repo/scripts/redhat"
    image=AlmaLinux-9-GenericCloud-9.8-20261005.x86_64.qcow2
    checksum=25942b554efaac077e2f60d6bb27bd3332c171454cc21bbd33e5f79244d7570f
    if [ ! -f "$lab/alma.qcow2" ]; then
      curl -fL --retry 3 "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/$image" -o "$lab/alma.qcow2.part"
      mv "$lab/alma.qcow2.part" "$lab/alma.qcow2"
    fi
    printf '%s  %s\n' "$checksum" "$lab/alma.qcow2" | sha256sum --check
    if [ ! -f "$lab/id_ed25519" ]; then
      ssh-keygen -t ed25519 -N '' -f "$lab/id_ed25519" -C outcome-redhat-test
    fi
    cat > "$lab/user-data" <<EOF
#cloud-config
hostname: outcome-redhat-test
users:
  - name: tester
    uid: 1000
    groups: [wheel]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$lab/id_ed25519.pub")
ssh_pwauth: false
packages: [podman, python3.12, python3.12-pip, git, audit]
runcmd:
  - [loginctl, enable-linger, tester]
EOF
    printf 'instance-id: outcome-redhat-test\nlocal-hostname: outcome-redhat-test\n' > "$lab/meta-data"
    docker run --rm --network host -v "$lab:/lab" "$harness" cloud-localds /lab/seed.img /lab/user-data /lab/meta-data
    if [ ! -f "$lab/test.qcow2" ]; then
      docker run --rm --network host -v "$lab:/lab" "$harness" qemu-img create -f qcow2 -F qcow2 -b /lab/alma.qcow2 /lab/test.qcow2 30G
    fi
    docker run -d --name "$vm" --network host --device /dev/kvm --memory 7g --cpus 4 \
      -v "$lab:/lab" "$harness" qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 6144 \
      -drive file=/lab/test.qcow2,if=virtio -drive file=/lab/seed.img,format=raw,if=virtio \
      -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=net0 \
      -display none -serial file:/lab/console.log -monitor none
    echo "VM booting. Follow $lab/console.log; run '$0 test' once cloud-init finishes."
    ;;
  test)
    runtime=${2:-podman}
    case "$runtime" in docker|podman) ;; *) echo 'runtime must be docker or podman' >&2; exit 2 ;; esac
    image=${OUTCOMECI_TEST_IMAGE:-ghcr.io/outcomeci/outcome-runner:main}
    if [[ ! "$image" =~ ^[a-zA-Z0-9./:@_-]+$ ]]; then
      echo 'invalid image reference' >&2; exit 2
    fi
    tar -C "$repo" -czf "$lab/fixed.tar.gz" src pyproject.toml README.md tests/integration/test_container_host.py
    ssh "${ssh_args[@]}" tester@127.0.0.1 'cat > fixed.tar.gz' < "$lab/fixed.tar.gz"
    ssh "${ssh_args[@]}" tester@127.0.0.1 "
      set -eu
      sudo cloud-init status --wait
      test \"\$(getenforce)\" = Enforcing
      mkdir -p fixed
      tar -xzf fixed.tar.gz -C fixed
      python3.12 -m venv fixed-venv
      SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 fixed-venv/bin/pip install ./fixed pytest
      OUTCOMECI_TEST_IMAGE='$image' OUTCOMECI_TEST_RUNTIME='$runtime' fixed-venv/bin/pytest -q fixed/tests/integration/test_container_host.py
    "
    ;;
  ssh)
    shift
    ssh "${ssh_args[@]}" tester@127.0.0.1 "$@"
    ;;
  down)
    docker rm -f "$vm"
    echo "Stopped the VM; disks and test logs remain in $lab."
    ;;
  *)
    echo "Usage: $0 up | test [podman|docker] | ssh [command] | down"
    ;;
esac
