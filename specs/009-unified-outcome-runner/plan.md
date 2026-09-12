# Plan

Move the provider-neutral worker shell into `outcomeci.cloud_runner`, reuse the existing workflow compiler and phase executor, extend the executor with a separate implementation mutation boundary, and publish one hardened runner Dockerfile from this repository. Keep API transport isolated from local filesystem execution.
