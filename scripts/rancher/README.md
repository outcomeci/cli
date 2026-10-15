# Rancher Desktop runner checks

Run this with Rancher Desktop using **dockerd (moby)** and the OutcomeCI CLI on PATH:

```sh
python3 scripts/check-rancher-desktop.py ghcr.io/outcomeci/outcome-runner:0.63.1
```

Use the runner tag or digest you intend to release. The probe selects the
`rancher-desktop` Docker context for its own commands without changing the user's
current context. It checks the entrypoint import, stdin, init, UID mapping,
read-only source, writable HOME, private working copy, bundled agent executables,
DNS, and workflow record writeback. The workflow skips its model step, so this
uses no agent login or model API key and performs no external actions.

The generic integration tests also exercise checkouts outside the shared home:

```sh
DOCKER_CONTEXT=rancher-desktop \
OUTCOMECI_TEST_RUNTIME=docker \
OUTCOMECI_TEST_IMAGE=ghcr.io/outcomeci/outcome-runner:0.63.1 \
python -m pytest -q tests/integration/test_container_host.py
```

## Reproduction and fix

Tested on Rancher Desktop 1.24.0, Moby 29.5.3, x86_64 Linux. The container-only
probe passed, but the unpatched launcher failed with:

```text
invalid mount config for type "bind": bind source path does not exist: /tmp/oci-run-...
```

Rancher's Lima VM shares the user's home and `/tmp/rancher-desktop`, rather than
the host's general temporary directory. See [Rancher's storage documentation](https://docs.rancherdesktop.io/tutorials/using-persistent-storage/).
The launcher recognizes that VM, allocates private run directories in its shared
scratch area, and stages a read-only source copy there. This also works when the
workflow checkout itself is outside Rancher's default shares. Scratch directories
are removed after writeback, including on failure.

The test environment runs inside the [AlmaLinux lab](../redhat/README.md), using
nested KVM. Its outer VM has 6 GiB RAM; Rancher's VM has 3 GiB RAM and 2 CPUs.
Rancher's default 1 GiB allocation in this lab could not boot its Alpine image.
The graphical app runs under Xvfb with Kubernetes, telemetry, and auto-updates off.
The official Linux ZIP and its SHA-512 checksum come from the
[1.24.0 release](https://github.com/rancher-sandbox/rancher-desktop/releases/tag/v1.24.0).

To recreate the additional Rancher setup inside a disposable Linux VM, install
`unzip gtk3 nss alsa-lib libXScrnSaver mesa-libgbm dbus-x11 xorg-x11-server-Xvfb`,
verify the release ZIP with its published `sha512sum`, and extract it. Configure
the extracted `chrome-sandbox` as root-owned mode 4755, then launch the app as the
normal test user:

```sh
xvfb-run -a dbus-run-session -- ./rancher-desktop \
  --no-modal-dialogs \
  --kubernetes.enabled=false \
  --containerEngine.name=moby \
  --virtualMachine.memoryInGB=3 \
  --virtualMachine.numberCPUs=2 \
  --application.adminAccess=false \
  --application.telemetry.enabled=false \
  --application.updater.enabled=false \
  --application.pathManagementStrategy=manual
```

Add `resources/resources/linux/bin` from the extracted app to PATH for `rdctl`
and Rancher's Docker CLI. Wait for `docker --context rancher-desktop info` before
testing. This SSH-only lab uses Rancher's `docker-credential-none` helper for the
public runner image because it has no desktop keyring or registry credentials.
Do not replace a real user's configured credential helper.

The test covers Linux Rancher Desktop's Moby mode. It does not certify macOS,
Windows/WSL, ARM images, containerd/nerdctl mode, or Rancher-managed Kubernetes.
