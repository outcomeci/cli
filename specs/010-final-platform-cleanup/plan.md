# Plan

Introduce typed claim/backend normalization at the cloud runner boundary. Materialize either API-supplied state or an explicitly selected GitHub repository into a common state directory, run the existing compiler and phase logic unchanged, then persist through a backend adapter. Keep provider-specific command construction isolated in the process adapter and expose one normalized invocation contract to phase execution.
