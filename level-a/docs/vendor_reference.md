# Vendor reference (verified against official docs, 2026-11-14)

Facts the code relies on, with sources. "UNCONFIRMED" marks anything not stated by a vendor doc; code must tolerate both readings.

## 1. ElevenLabs outbound call via Twilio
Source: https://elevenlabs.io/docs/eleven-agents/api-reference/integrations/twilio/outbound-call.md (+ OpenAPI `TelephonyCallConfigInput`, `TwilioOutboundCallResponse`)

- `POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call`, header `xi-api-key: <key>`, `Content-Type: application/json`.
- Body: `agent_id` (req), `agent_phone_number_id` (req), `to_number` (req), `conversation_initiation_client_data` {`dynamic_variables` map, `conversation_config_override`, `user_id`, ...}, `call_recording_enabled`, `telephony_call_config` {`ringing_timeout_secs` int 1–999 default 60, `twilio_call_recording_enabled`, `twilio_machine_detection`}.
- `conversation_config_override` only works if the agent enables `platform_settings.overrides.conversation_config_override.<path>: true` (all default false).
- 200 response: `success` (bool), `message` (str), `conversation_id` (optional), `callSid` (optional, camelCase). 422: `{"detail":[{loc,msg,type}]}`.

```json
{"agent_id":"<agent>","agent_phone_number_id":"<phone_number_id>","to_number":"+14165550123",
 "conversation_initiation_client_data":{"dynamic_variables":{"attempt_id":"att_..","party_id":"harbour-market"}},
 "telephony_call_config":{"ringing_timeout_secs":20}}
→ {"success":true,"message":"...","conversation_id":"conv_...","callSid":"CA..."}
```
UNCONFIRMED: whether a rejected call is 200 with `success:false` or non-2xx (handle both); whether ids are present on failure; whether a missing dynamic variable referenced by the prompt fails the call (always send every variable).

## 2. ElevenLabs webhook (server) tools
Sources: https://elevenlabs.io/docs/eleven-agents/customization/tools/server-tools.md, https://elevenlabs.io/docs/eleven-agents/api-reference/tools/create.md

- Current pattern: `POST /v1/convai/tools` with `{"tool_config": {...}}` → `{id, tool_config, ...}`; agent references it in `conversation_config.agent.prompt.tool_ids`. Inline `prompt.tools` is **deprecated**. `GET/PATCH/DELETE /v1/convai/tools/{tool_id}` (PATCH takes `{tool_config}`).
- Webhook tool: `type:"webhook"`, `name` (`^[a-zA-Z0-9_-]{1,64}$`), `description`, `api_schema`, `response_timeout_secs` (default 20, range 5–300), `execution_mode`, `pre_tool_speech` (auto|force|off), `tool_error_handling_mode`, `assignments[]`, `dynamic_variables{dynamic_variable_placeholders}`.
- `api_schema`: `url`, `method`, `request_headers` (value: string | `{"secret_id"}` | `{"variable_name"}` | `{"env_var_label"}`), `request_body_schema` (object schema), `content_type`, `response_filter`.
- Body property sources (mutually exclusive): `description` (LLM fills), `dynamic_variable: "<name>"`, `constant_value`, `is_omitted`, `is_system_provided`. There is no `value_type` field.
- Workspace secret: `POST /v1/convai/secrets` `{"type":"new","name","value"}` → `{"secret_id"}`.

```json
{"tool_config":{"type":"webhook","name":"submit_count","description":"...","response_timeout_secs":20,
 "api_schema":{"url":"https://<host>/tools/submit_count","method":"POST","content_type":"application/json",
  "request_headers":{"X-Tool-Secret":{"secret_id":"<id>"}},
  "request_body_schema":{"type":"object","required":["attempt_id","party_id"],"properties":{
    "attempt_id":{"type":"string","dynamic_variable":"attempt_id"},
    "system__conversation_id":{"type":"string","dynamic_variable":"system__conversation_id"},
    "on_hand":{"type":"integer","description":"Tins still on the shelf or in the back room"}}}}}}
```
UNCONFIRMED: the response contract — return HTTP 200 with a small JSON object; it is passed to the LLM.

## 3. Dynamic variables
Source: https://elevenlabs.io/docs/eleven-agents/customization/personalization/dynamic-variables.md
- `{{variable_name}}` in prompts, first message, tool params. Case-sensitive. Types string/number/boolean (send strings).
- System: `system__conversation_id`, `system__call_sid` (Twilio only), `system__called_number`, `system__caller_id`, `system__agent_id`, `system__time_utc`, `system__call_duration_secs`, ... (`system__` prefix reserved).
- Test defaults: `conversation_config.agent.dynamic_variables.dynamic_variable_placeholders`.

## 4. Post-call webhooks
Sources: https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks.md, https://elevenlabs.io/docs/api-reference/webhooks/create.md, SDK `webhooks_custom.py`

- Create: `POST /v1/workspace/webhooks` `{"settings":{"auth_type":"hmac","name","webhook_url","request_headers"?}}` → `{webhook_id, webhook_secret}`.
- Enable: workspace `PATCH /v1/convai/settings` `{"webhooks":{"post_call_webhook_id","events":[...]}}`, or per agent `platform_settings.workspace_overrides.webhooks = {post_call_webhook_id, events}`. Events: `transcript`, `audio`, `call_initiation_failure`, `answering_machine_detection`, ...
- Must return 200. Retries off by default; when on, retries 5xx/429/408 with identical payloads (dedupe on conversation_id).
- **HMAC**: header `ElevenLabs-Signature: t=<unix>,v0=<hex>`; signed string `f"{t}.{raw_body}"`; HMAC-SHA256 with the webhook secret, hex. Reject if `t` older than 30 min. Verify the raw body before parsing.
- `post_call_transcription`: `{type, event_timestamp, data:{agent_id, conversation_id, status, transcript[{role:"agent"|"user", message, tool_calls, tool_results, time_in_call_secs}], metadata{start_time_unix_secs, call_duration_secs, termination_reason, phone_call{type:"twilio", direction, external_number, agent_number, call_sid, call_result}}, analysis{call_successful, transcript_summary}, conversation_initiation_client_data{dynamic_variables{..., system__*}}}}`.
- `post_call_audio`: `data{agent_id, conversation_id, full_audio(base64)}` — ignore.
- **`call_initiation_failure`**: `data{agent_id, conversation_id, failure_reason ("busy"|"no-answer"|"unknown"), metadata{type:"twilio", body{CallSid, CallStatus, To, From, Direction, Timestamp}}}` — **no dynamic variables**: match by conversation_id, then CallSid, then `To` (the party's number) + the newest live attempt to that number.

## 5. Agents and phone numbers
- Create `POST /v1/convai/agents/create` `{name, conversation_config, platform_settings}` → `{agent_id}`. Update `PATCH /v1/convai/agents/{id}`.
- `conversation_config.agent`: `first_message`, `language`, `dynamic_variables.dynamic_variable_placeholders`, `prompt{prompt, llm, temperature, max_tokens, tool_ids[], built_in_tools{end_call{type:"system",name:"end_call",description:"",params:{system_tool_type:"end_call"}}}}`.
- `tts{model_id (eleven_flash_v2_5, eleven_turbo_v2_5, ...), voice_id, stability, speed}`; `conversation{max_duration_seconds}` (default 600); `turn{turn_timeout (7), silence_end_call_timeout, turn_eagerness}`; `asr{keywords[]}`.
- `llm` examples: `gemini-2.5-flash`, `gpt-4.1-mini`, `claude-haiku-4-5`, `claude-sonnet-4-5`.
- Phone numbers: `GET /v1/convai/phone-numbers` → array of `{provider, phone_number, label, phone_number_id, assigned_agent}`. Import Twilio: `POST /v1/convai/phone-numbers` `{provider:"twilio", phone_number, label, sid, token}` → `{phone_number_id}`.

## 6. Twilio Calls
Source: https://www.twilio.com/docs/voice/api/call-resource
- List `GET https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Calls.json`; fetch `GET .../Calls/{Sid}.json`. HTTP Basic `AccountSid:AuthToken`.
- Query params: `To` (URL-encode `+`), `From`, `Status`, `StartTime>` (on/after; documented as `YYYY-MM-DD` UTC), `StartTime<`, `PageSize` (≤1000), `PageToken`. Filter precisely on `start_time` in code.
- Page: `calls[]`, `next_page_uri` (relative to https://api.twilio.com; null on last page).
- Call: `sid` (CA…), `to`, `from`, `status` (queued ringing in-progress canceled completed busy no-answer failed), `direction` (outbound-api), `start_time`/`date_created` as RFC 2822 (`"Fri, 18 Oct 2019 17:02:00 +0000"`; `start_time` empty if not started), `duration` (string seconds).

## 7. Atlas Free / Flex
Sources: https://www.mongodb.com/docs/atlas/reference/free-shared-limitations.md and related
- MongoDB 8.0, 3-node replica set. 100 ops/s (Flex 500). 500 connections, 500 collections, 0.5 GB.
- Change streams supported; at most 5 open on Free. `ns.db` filters may not use `$in` (collection filters may).
- Vector Search allowed; **max 3 search indexes** on Free. `$vectorSearch` `exact: true` supported on 8.0 (omit `numCandidates`).
- **`collMod` needs dbAdmin** (not in readWrite). `createCollection` with a validator works for readWrite users → create collections with their validators; use collMod only to update.
- Transactions: not listed as a limitation (expected to work; verify with test 1).

## 8. Voyage through Atlas
- `POST https://ai.mongodb.com/v1/embeddings`, `Authorization: Bearer <key>` (Atlas-created keys only work here; Voyage-dashboard keys only at api.voyageai.com).
- Body `input` (str or ≤1000 strings), `model`, `input_type` (query|document|null), `output_dimension` (256|512|1024|2048).
- Response `{data:[{embedding, index}], model, usage}`. `voyage-4` / `voyage-3.5` default 1024 dims. Don't mix model families between stored and query vectors.
