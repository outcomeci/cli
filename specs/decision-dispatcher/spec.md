# Decision steps and dispatcher workflows

A Slack app-mention webhook can enter one dispatcher workflow and route the same event to independently managed child workflows. Dispatching is asynchronous: the parent records a queued child receipt and continues. The child owns its credentials, run, and failures.

## Authoring contract

- Optional top-level `type: dispatcher` enables decision and dispatch steps. Omitted type retains existing behavior. Existing ingress triggers remain unchanged.
- Child workflows declare `trigger: {type: dispatcher, dispatcher: <parent-name>}`. Bare dispatcher triggers are invalid.
- `decision` maps question identifiers to predicate, choice, or score questions. Each has instructions; choices specify unique string/boolean values, and scores specify ordered levels. `using` selects an OpenAI or TypeSafe model profile. TypeSafe requires an explicit Vault credential and cannot run chat or policy review steps. Decision profiles cannot use fallback.
- Decision steps accept existing `with` references and `when` expressions, run no agent or tools, and return question-named typed answer objects. Refusals, missing answers, invalid probabilities, and undeclared values stop execution.
- `dispatch: <child-name>` uses one `with` reference as the child's input object. Multiple conditionally executed dispatch steps support fanout. Local dispatch requires an injected managed dispatch client.

## Evidence and transport

Managed runner requests carry only the lease, step, and resolved input; the API derives model/questions/target from the pinned revision. Decision results and dispatch receipts are retained beside each step's `outputs.json`, in `decision.json` and `dispatch.json`. Model usage is retained using existing transcript artifacts; policy permission evidence remains distinct.

Managed children receive a validated dispatcher envelope with parent/root lineage, dispatcher name, depth, and selected payload. The envelope remains in run state; workflow `trigger` references resolve to its payload. Maximum depth is eight. The server enforces membership, scope, cycles, and durable idempotency; the CLI cannot grant invocation authority by changing a request target.

## Acceptance

Existing workflows compile and execute unchanged. A dispatcher can choose a support route, enqueue support once, skip engineering, and finish without leasing an agent. Retry after dispatch failure retains the completed decision. Both provider adapters execute through LiteLLM 1.104.2's shared Decisions interface. Invalid answers never dispatch children.

## Ordinary model BYOK extension

Ordinary model steps, conversations, and their fallback profiles accept the reviewed chat providers in `outcomeci.model_providers.CHAT_PROVIDERS`: OpenAI, Anthropic, Gemini, Groq, Mistral, DeepSeek, xAI, Together AI, Fireworks AI, Cerebras, OpenRouter, Cohere Chat, SambaNova, Perplexity, NVIDIA NIM, and DeepInfra. New providers require an explicit declared Vault key, including when used as a fallback. Existing OpenAI/Anthropic platform-key behavior is preserved; policy reviewers remain restricted to those two providers. Decision providers and OpenAI decision-model constraints are unchanged.

The shared immutable registry fixes official API endpoints and explicit LiteLLM provider selection. Model identifiers permit provider namespaces and colon variants, but reject URLs, query strings, fragments, backslashes, empty segments, traversal, and identifiers longer than 256 characters. Workflows cannot configure custom endpoints. Perplexity is tool-free only: requesting capabilities or typed returns fails before a provider call. Other models must support the requested tools; unsupported parameters are never silently removed.

Output tokens are bounded by valid SDK model metadata up to the runtime limit. Unknown or malformed metadata uses at most 4096 tokens. Providers remain responsible for validating current availability and limits. Provider errors expose the model and error class without copying keys or prompts into run error artifacts.
