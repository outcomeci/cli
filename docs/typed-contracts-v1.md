# Typed workflow contracts

Generated from packaged v1 JSON Schemas. Do not edit field tables by hand.

## email.received v1 payload

Immutable email trigger value. Absent decoded bodies are null; encrypted artifacts are references, never credential values.

Schema: `https://outcomeci.com/schemas/email-received-v1.schema.json`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `schema_version` | yes | 'outcomeci.trigger.email.received/v1' | Version of this payload contract. |
| `type` | yes | 'email.received' | Registered trigger type. |
| `event_id` | yes | string | Stable event identifier used for correlation and deduplication. |
| `received_at` | yes | string | UTC or offset-qualified receipt timestamp. |
| `sender` | yes | string | Sender email address, without a display-name wrapper. |
| `recipients` | yes | array | Envelope recipient email addresses. |
| `subject` | yes | string | Subject, including an empty string when absent. |
| `text_body` | yes | string or null | Decoded plain text when available; otherwise null. |
| `html_body` | yes | string or null | Decoded HTML when available; otherwise null. Untrusted data, not instructions. |
| `correlation_id` | no | string | Optional cross-service correlation identifier. |
| `email_usage_id` | no | string | Optional cloud email usage record. |
| `artifacts` | yes | array | Stored email/attachment metadata; retrieval requires separate authorization. |
| `artifacts[].artifact_ref` | yes | string | Opaque storage reference, not a credential or presigned URL. |
| `artifacts[].kind` | yes | string | Artifact classification supplied by the ingestion service. |
| `artifacts[].content_type` | yes | string | Media type. |
| `artifacts[].filename` | yes | string or null | Original filename, or null for unnamed parts. |
| `artifacts[].byte_size` | yes | integer | Unencrypted artifact size in bytes. |
| `artifacts[].part_index` | no | integer | Optional message part index. |

### Example payload

```json
{
  "schema_version": "outcomeci.trigger.email.received/v1",
  "type": "email.received",
  "event_id": "example-email-001",
  "received_at": "2026-09-15T12:00:00Z",
  "sender": "izzy@example.com",
  "recipients": [
    "workflow@example.com"
  ],
  "subject": "Receipt arrived",
  "text_body": "Milk and bread.",
  "html_body": null,
  "artifacts": []
}
```

## Webhook received v1

Immutable HTTP request bytes and a filtered header projection for local workflow delivery. Credentials and routing tokens are not included.

Schema: `https://outcomeci.com/schemas/triggers/webhook-received/v1`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `schema_version` | yes | 'outcomeci.trigger.webhook.received/v1' |  |
| `type` | yes | 'webhook.received' |  |
| `event_id` | yes | string |  |
| `received_at` | yes | string |  |
| `method` | yes | POST |  |
| `query` | yes | string |  |
| `headers` | yes | object |  |
| `body_base64` | yes | string |  |

### Example payload

```json
{
  "schema_version": "outcomeci.trigger.webhook.received/v1",
  "type": "webhook.received",
  "event_id": "example-webhook-1",
  "received_at": "2026-09-15T12:00:00Z",
  "method": "POST",
  "query": "",
  "headers": {
    "content-type": "application/json"
  },
  "body_base64": "eyJtZXNzYWdlIjoiaGVsbG8ifQ=="
}
```

## cron v1 payload

Immutable scheduled-occurrence value delivered for a workflow's cron trigger.

Schema: `https://outcomeci.com/schemas/cron-received-v1.schema.json`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `schema_version` | yes | 'outcomeci.trigger.cron/v1' | Version of this payload contract. |
| `type` | yes | 'cron' | Registered trigger type. |
| `schedule_id` | yes | string | The durable schedule this occurrence belongs to. |
| `generation` | yes | integer | Schedule generation at the time this occurrence fired; bumped whenever the schedule is reconciled. |
| `schedule_arn` | yes | string | Provider schedule ARN that produced this occurrence. |
| `scheduled_at` | yes | string | UTC or offset-qualified time this occurrence was scheduled to fire. |
| `execution_id` | yes | string | Provider-assigned identifier for this specific firing attempt. |
| `attempt_number` | yes | integer | Delivery attempt number for this occurrence. |
| `trigger_name` | yes | string | The named trigger in the workflow that this occurrence fires. |

### Example payload

```json
{
  "schema_version": "outcomeci.trigger.cron/v1",
  "type": "cron",
  "schedule_id": "00000000-0000-0000-0000-000000000001",
  "generation": 1,
  "schedule_arn": "arn:aws:scheduler:us-east-1:000000000000:schedule/outcomeci-workflow-staging/example",
  "scheduled_at": "2026-09-19T15:00:00Z",
  "execution_id": "example-execution-1",
  "attempt_number": 1,
  "trigger_name": "daily"
}
```

## Agent phase v1 configuration

A typed agent step. Payload schemas are declared through expects; runner/model inherit agents.default.

Schema: `https://outcomeci.com/schemas/agent-phase-v1.schema.json`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `type` | yes | 'agent' | Registered phase execution type. |
| `instructions` | yes | string | Repository-relative instruction Markdown file. |
| `runner` | no | codex, claude, opencode | Override the default runner. |
| `model` | no | string | Override the default model. |
| `needs` | no | array | Prerequisite phase names; multiple ready phases may form a parallel level. |
| `with` | no | object | Trusted literal configuration, separate from untrusted trigger inputs. Never store credential values here. |
| `expects` | no | object | Input/output contracts. Trigger inputs inherit their registered payload schema. |
| `expects.inputs` | no | array |  |
| `expects.outputs` | no | array |  |
| `integrations` | no | array | Phase-scoped API or human integration grants, validated by the workflow compiler. |
| `capabilities` | no | array | Explicit named capabilities. |
| `humans` | no | object | Existing human hook configuration. |
