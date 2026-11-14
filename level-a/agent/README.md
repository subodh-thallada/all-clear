# The voice agent, as code

Everything ElevenLabs needs for the All Clear recall line lives in this folder and
is pushed by one script. Nothing is configured by hand in the dashboard except
importing the Twilio number, so the agent the judges hear is the agent in git.

| File | What it is |
|---|---|
| `prompt.md` | The system prompt: call flow, number read-back, how to act on each tool reply, question-only calls, open questions, call backs, voicemail / screening / refusal / noise handling, guardrails. |
| `first_message.txt` | The opening line, spoken as soon as the call connects. Says it is the automated recall line for `{{company}}` and that the call is a simulation. |
| `tool_submit_count.json` | The `POST /v1/convai/tools` body for the `submit_count` webhook tool: the counts the model fills, the four ids bound from dynamic variables, the `X-Tool-Secret` header from a workspace secret, a 20 s timeout. |
| `tool_submit_answer.json` | The `submit_answer` webhook tool (`POST /tools/submit_answer`): the model fills `question_id` and `value` (strings) after reading the answer back; the same four ids are bound. |
| `tool_request_callback.json` | The `request_callback` webhook tool (`POST /tools/request_callback`): the model fills `hour_of_day` (integer 0 to 23) and `day_offset` (integer: 0 today, 1 tomorrow; left out when only a time was named); the same four ids are bound. The API turns them into the party's timer. |
| `agent_config.json` | The agent create/update body: LLM, voice model, turn and silence timeouts, ASR keywords, the three tool ids, the post-call webhook override, and a sample value for every dynamic variable (used by the dashboard's test mode and by `scripts/call_once.py`). |
| `planner_prompt.md`, `repair_prompt.md` | Not the voice agent: the planner decider's and the order system repairer's prompts (owned by the harness and systems code, used only with `PLANNER=claude` or an `ANTHROPIC_API_KEY`). |

`{{UPPER_CASE}}` placeholders are filled by `scripts/provision_agent.py`
(`PUBLIC_BASE_URL`, `TOOL_SECRET_ID`, `PROMPT`, `FIRST_MESSAGE`,
`SUBMIT_COUNT_TOOL_ID`, `SUBMIT_ANSWER_TOOL_ID`, `REQUEST_CALLBACK_TOOL_ID`,
`POST_CALL_WEBHOOK_ID`). `{{lower_case}}` names are ElevenLabs dynamic
variables, filled per call from the worker's
`conversation_initiation_client_data.dynamic_variables`.

## The one rule this folder must keep

The agent never knows how many tins a party should have. Nothing here contains
`shipped_in`, a delivered count or a delivery note number (the sample values and
examples are numbers no scenario uses), and the worker never sends one. The
agent hears about a mismatch only from the tool reply, after Atlas has refused
the write, so every challenge on the call originates in the database.
`tests/test_agent_config.py` enforces this.

## Dynamic variables

The worker sends all of these, always as strings, on every call (DESIGN.md §5,
plus the question variables of the long run, spec "Questions as data"):

`attempt_id party_id party_name batch_code batch_spoken brand product hazard unit tier
purpose company sender_name told_qty ask_delivery_note followup_note
ask_counts questions question_ids`

The tools also bind `system__conversation_id` and `system__call_sid`, which
ElevenLabs provides on Twilio calls. `sender_name`, `told_qty` and
`followup_note` are empty strings when they do not apply.

- `ask_counts` is `"no"` when the party's counts are already on the books: the
  call is a question-only call, and the prompt then never asks for counts.
- `questions` lists the party's open questions, each starting with its id, for
  example `q-07: Did you also receive batch ZX-5521? (answer: yes or no, and how
  many tins).` It is empty when there are none. The agent asks each one, reads
  the answer back and records it with `submit_answer`; the API removes the id
  from the party's `open_questions`.
- `question_ids` is the comma-separated list of those ids (the agent does not
  need it; it is there for the post-call transcript and tests).

A call back works the same way on every call: "call me back tomorrow at nine"
becomes `request_callback {hour_of_day: 9, day_offset: 1}` after a read-back,
the API books the time on the attempt, and the agent confirms it and ends the
call. The ladder then sets the party's timer to that time.

## Provisioning

Prerequisites, in `level-a/.env` (see `.env.example`): `ELEVEN_API_KEY`,
`TOOL_SECRET`, and `PUBLIC_BASE_URL` (the API's public `https://` URL: a Render,
Fly or Railway host, or a tunnel to `python -m levela api`). Import the Twilio
number once in the ElevenLabs dashboard (Phone Numbers, with the Twilio SID and
auth token).

```sh
cd level-a
python scripts/provision_agent.py --dry-run      # print every request body, send nothing
python scripts/provision_agent.py                # create or update everything
```

The script is idempotent: run it again after any change to this folder, and
whenever `PUBLIC_BASE_URL` changes (a new tunnel URL). In order, it:

1. creates the workspace secret holding `TOOL_SECRET`, named
   `levela_tool_secret_<8 hex of its SHA-256>`, or reuses the one with that name.
   Rotating `TOOL_SECRET` therefore creates a new secret and repoints the tool,
   instead of quietly keeping the old value;
2. creates the `submit_count`, `submit_answer` and `request_callback` tools, or
   PATCHes the one with each name (the workspace's tools are listed once);
3. creates the HMAC post-call webhook for `PUBLIC_BASE_URL/hooks/post_call`,
   unless `ELEVEN_WEBHOOK_ID` is set or a webhook already posts to that URL.
   **The webhook's signing secret is printed once and stored nowhere.** Put it
   in `ELEVEN_WEBHOOK_SECRET` in `.env` and in the API host's environment. It
   warns if `ELEVEN_WEBHOOK_ID` points at a different URL (a stale tunnel);
4. creates the agent "All Clear recall line", or PATCHes it (`ELEVEN_AGENT_ID`,
   else found by name), with `prompt.md`, `first_message.txt`, the three tool ids and
   the webhook override (events `transcript`, `call_initiation_failure`);
5. picks the phone number: `ELEVEN_PHONE_ID`, or the only Twilio number;
6. prints the `.env` lines to set: `ELEVEN_AGENT_ID`, `ELEVEN_PHONE_ID`,
   `ELEVEN_WEBHOOK_ID`.

It refuses to run unless `PUBLIC_BASE_URL` (or `--base-url`) is `https://`, and,
outside a dry run, unless `ELEVEN_API_KEY` and a non-default `TOOL_SECRET` are
set. It exits 1 when something still needs doing (no phone number, missing
webhook secret), and it never writes a file.

## Smoke call

```sh
python -m levela api                       # in another terminal; reachable at PUBLIC_BASE_URL
python scripts/call_once.py --watch        # rings PHONE_HARBOUR
python scripts/call_once.py --to +1... --var tier=downstream --var "sender_name=Queen St Grocer"
```

`call_once.py` sends exactly the variables the worker sends for a direct party's
first call, with `attempt_id` `att_smoke_<hex>`, and prints the
`conversation_id`, the `callSid` and the request's round-trip time. `--watch`
polls Twilio every 2 s and prints the status timeline. The agent's
`submit_count` call is logged by the API but answered **"call not recognised"**
(there is no attempt `att_smoke_...`), so nothing is booked; a
`request_callback` on the smoke call is logged and refused the same way. Pass
`--attempt` with a live attempt's id (and `--var party_id=...` for its party)
to book for real. To hear a question-only call, add
`--var ask_counts=no --var "questions=q-07: Did you also receive batch ZX-5521? (answer: yes or no)." --var question_ids=q-07`.

## Unverified (from `docs/vendor_reference.md` and this build)

Each item has a check. Run the checks tonight.

- **Tool reply contract** (vendor §2): we assume the HTTP 200 JSON body is
  handed to the LLM as-is. Check: a rejection on a real call is spoken with both
  numbers.
- **Missing or empty dynamic variables** (vendor §1): whether a variable the
  prompt references but the call omits fails the call. The worker always sends
  all sixteen. Whether an **empty string** value is accepted is also
  unconfirmed. `call_once.py` sends three empty ones, so the smoke call settles
  it.
- **Failed outbound calls** (vendor §1): `success:false` with HTTP 200, or
  non-2xx, and whether ids come back. Owned by `providers/voice.py`. Note what
  you see.
- **`system__call_sid` in tool calls**: documented for Twilio calls only. Check
  that `tool_events.call_sid` is filled after a real call.
- **`eleven_flash_v2_5` for an English agent**: if agent creation rejects the TTS
  model for English, switch `tts.model_id` to `eleven_flash_v2` and run the
  script again.
- **List endpoints the script relies on for idempotency**: `GET
  /v1/convai/secrets` (`{secrets:[{secret_id,name}]}`), `GET
  /v1/workspace/webhooks` (`{webhooks:[{webhook_id,webhook_url}]}`; a 404 is
  treated as "none"), and `GET /v1/convai/agents?search=` (`{agents, has_more,
  next_cursor}`). Check: a second run of the script prints "reusing" and
  "updated", never "created".
- **Tool timeout**: `response_timeout_secs` is 20 (vendor range 5 to 300). The
  real round trip has not been measured yet (below).
- **Ranges on integer tool parameters**: the vendor's body-schema properties
  document no `minimum`/`maximum`, so `hour_of_day`'s 0 to 23 is stated in its
  description and enforced by the API, which answers "Not booked: ..." for an
  hour out of range (the prompt then asks for another time).
- **Spoken times**: "nine" for nine in the morning and "half past three" for
  fifteen are the model's to convert. Check on a real call that "tomorrow at
  nine" arrives as `hour_of_day` 9 and `day_offset` 1 (the `callback` row in
  `tool_events`).
- **Voicemail and call screening**: the prompt ends the call on voicemail or an
  automated menu, and answers a screening assistant in one sentence. Neither has
  been heard on a real phone.

## Measurements to record tonight

Write each result down, with the phone and carrier used.

1. **It rings a Canadian mobile.** `call_once.py --watch` to each team phone.
   The timeline should reach `in-progress`, then `completed`.
2. **Tool round-trip time.** During the smoke call, answer the counts. Then read
   the `tool_call` row in `tool_events` (`received_at`) and the API log line for
   `POST /tools/submit_count`. The post-call payload (`tool_events`, kind
   `post_call`) has `time_in_call_secs` for the user turn and for the agent turn
   after the tool result. Their gap is the pause the caller hears. If it is over
   about 2 s, host the API in the same region as the Atlas cluster and measure
   again.
3. **Numbers heard over noise.** Play crowd noise near the phone and say
   numbers that are easy to confuse: thirteen and thirty, fourteen and forty,
   fifteen and fifty, sixteen and sixty, seventeen and seventy. Count how many
   the digit read-back gets wrong, and whether the "no, that's wrong" correction
   recovers each one.
4. **The agent speaks a canned rejection.** Run the API and the worker, start
   `recall_1` with `PHONE_HARBOUR` set, answer as Harbour Market and over-count.
   The agent must say both numbers from the tool reply and ask again. After two
   challenges it must stop.
5. **Call screening does not block it.** Try with iOS "Silence Unknown Callers"
   and the Android / Pixel call screen on. If the call is held, add the Twilio
   number to the phone's contacts before the demo.
