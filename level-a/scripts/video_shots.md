# One-minute video: shot list

The submission video is 60 seconds and shows only what was built today (spec
"Rules that can disqualify"). It is recorded in five segments between 4:15 and
4:45: the call segment by B, the kill, order system change, store record and
close segments by C. Each segment is one take of the screen plus one close-up.
Record the screen full size at 1080p; the feed text must be readable.

Before recording: `make reset`, then `make demo-4` for the call, kill and store
record segments (the four-party slice alone, so the feed is quiet and
readable). The order system change and close shots come from the long run
(`make reset`, `make demo`, the order system change held until you press the
switch). Nothing on screen may show a real phone number, a key or `.env`: the
screen never shows numbers, `call_once.py` masks them, and the terminal must
not `cat .env`. The product, the batch, the barcode and every name are
invented: never say or imply that anyone was hurt by it.

| Time | Shot | On screen | Voice-over |
|---|---|---|---|
| 0:00 to 0:06 | Screen, wide | Recall of Maple Farm milk powder 800 g, batch B-4471: simulated day, parties, steps, kills survived, 0 duplicate effects | "All Clear runs a food recall to the end: every tin of the batch, found and off the shelf, with the database as the referee." |
| 0:06 to 0:20 | **Call segment.** Phone close-up, then the screen | The team phone rings (real call). The person playing Harbour Market says sixty on the shelf. Feed: `Rejected by Atlas: Document failed validation ... 60 reported, 40 shipped`. The agent says both numbers; the person corrects; the claim is accepted | "Sixty is more than was delivered. The model did not catch that: the Atlas validator did, and the agent read its two numbers back." |
| 0:20 to 0:34 | **Kill segment.** Terminal running `scripts/kill_demo.sh`, the screen beside it | `failpoint after_dial_request fired`, the `WORKER KILLED (exit 137)` banner. The second phone rings once: "we received 25, all still on the shelf". The edge turns disputed: 15 tins. Enter: `restart n: ... documents read, 0 steps replayed`, `attempt ... adopted ... no redial`. Call counter for Corner Mart: 1 | "The worker died after requesting the call and before saving it. A naive restart dials again. Ours finds the evidence first: one ring, one call." |
| 0:34 to 0:44 | **Order system change segment.** The Systems strip and the feed | Switch North to v2: the contract failure, operations flagged, `adapter v2 active: canary ... = ...`, only the flagged operations retried. East: the episode retrieved, adapter v2 active, 0 model calls | "The distributor's order system changed its fields. The repair is only activated if it reproduces a count the ledger already trusts. The second one repaired itself from memory." |
| 0:44 to 0:52 | **Store record segment.** The SIMULATED store page for Corner Mart, then the feed | Move one tin to quarantine: one tin held at Corner Mart, with the event id. Scan a tin at the till: `blocked_sale`, "recalled batch, not for sale" | "A phone answer is a claim. This is the store's own stock record, and it outranks the phone." |
| 0:52 to 0:57 | **Close segment.** The Close recall button | `Close refused by Atlas: NOT ALL CLEAR: 39 tins open (24 still on a shelf, 15 disputed)`; checks completed, tins located, and held or returned side by side | "Thirty-nine tins are still open, so Atlas will not let us say all clear." |
| 0:57 to 1:00 | Disclosure card | The line below, in full | (read it) |

## Disclosure line (on screen at the end, and in the submission text)

> Real: MongoDB Atlas and every refusal it makes, the phone calls (ElevenLabs
> over Twilio), the webhook server on a public URL, and every kill of a worker.
> Simulated: the clock (ten days played in two hours), 36 parties (behaviour
> profiles), the distributors' order systems (mock processes written today), the
> shipment records, and the stores' tills and stock records (a page and a script
> labelled SIMULATED). Summit Foods' node is a second copy of the same code. The
> product, the batch and every name are invented.

## If a segment cannot be recorded live

- Voice fails: record the call segment with the party form on a phone instead
  (the same rejection appears on the screen) and say so in the voice-over.
- The second phone is flaky: record the kill with Corner Mart simulated
  (`python scripts/demo.py start --scenario recall_1 --sim-party corner-mart`);
  the call counter still shows one call.
- The order system repair stalls: show the last passing run of
  `python -m pytest tests/test_order_system_change.py -q` and say it is the test.
- The store page fails: post the two events with `make store-script` and say so.
