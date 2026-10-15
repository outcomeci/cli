# Red Hat compatibility lab

`lab.sh` boots a disposable AlmaLinux 9 VM using KVM, with SELinux enforcing,
a normal SSH user, and rootless Podman. AlmaLinux supplies a RHEL-compatible
environment; this is not certification against a subscribed RHEL installation.
The Ubuntu host needs Docker, `/dev/kvm`, curl, and OpenSSH. The VM gets four CPUs,
6 GiB RAM, and a sparse 30 GiB disk. SSH listens only on `127.0.0.1:22229`.

```sh
scripts/redhat/lab.sh up
# Wait for cloud-init to finish in /tmp/outcome-redhat-lab/console.log.
scripts/redhat/lab.sh test
# Exercise Red Hat's Docker compatibility package too:
scripts/redhat/lab.sh ssh 'sudo dnf install -y podman-docker'
scripts/redhat/lab.sh test docker
scripts/redhat/lab.sh down
```

The base image is pinned and checksum-verified. VM disks, an ephemeral SSH key,
and console output stay in `/tmp/outcome-redhat-lab`; `down` stops the VM but
preserves these for another run. Set `OUTCOMECI_REDHAT_LAB` to use another directory.
Set `OUTCOMECI_TEST_IMAGE` to an immutable runner version or digest when validating
a release. The default is the current `main` runner image.

The tests use the real container engine and runner. They check source mount
isolation, host-owned output, preservation of checkout SELinux labels, workflow
startup, and run-record writeback. The workflow deliberately skips its model step,
so no model credentials or external actions are needed. Agent/provider connectivity
is outside this smoke test's scope.

To run the same tests on another existing host:

```sh
OUTCOMECI_TEST_IMAGE=ghcr.io/outcomeci/outcome-runner:main \
OUTCOMECI_TEST_RUNTIME=podman \
python -m pytest -q tests/integration/test_container_host.py
```

Use `OUTCOMECI_TEST_RUNTIME=docker` for Docker or its Podman shim. If the host's
container bridge cannot resolve DNS, set `OUTCOMECI_TEST_NETWORK=host` to exercise
the CLI's existing network override.

The original launcher fails on a Podman-only host because it calls `docker`.
With `podman-docker` installed it starts but cannot read `/src` or write `/oci-run`:
rootless user IDs need `--userns=keep-id`, and SELinux needs container file labels.
The fix discovers the engine, applies Podman's rootless mapping, and labels only
disposable source/output directories; it does not relabel the user's checkout.
