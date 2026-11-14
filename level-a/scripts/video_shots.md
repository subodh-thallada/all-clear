# One-minute video: shot list

The submission video is 60 seconds, recorded in three segments between 4:15 and
4:45 (spec "Schedule"): the call segment by B, the kill and order system-change
segments by C. Each segment is one take of the wall plus one close-up. Record
the wall full screen at 1080p; the feed text must be readable.

Before recording: `make reset`, then `make demo-4` for the call and kill
segments (the four-party slice alone, so the feed is quiet and readable). The
order system-change and closing shots come from the long run (`make reset`,
`make demo`, the order system change held until you press the switch).
Nothing on screen may show a real phone number, a key or `.env`: the wall
never shows numbers, `call_once.py` masks them, and the terminal must not
`cat .env`.

| Time | Shot | On screen | Voice-over |
|---|---|---|---|
| 0:00 to 0:06 | Wall, wide | Recall of batch B-4471, simulated day, parties, steps, kills survived, 0 duplicate effects | "All Clear runs a tin recall to the end: every party, every tin, with the database as the referee." |
| 0:06 to 0:24 | **Call segment.** Phone close-up, then the wall | The team phone rings (real call). The person playing Harbour Market says sixty on hand. Feed: `REJECTED by Atlas: Document failed validation ... Reported 60, shipped_in 40`. The agent says both numbers; the person corrects; the claim is accepted | "Sixty is more than was delivered. The model did not catch that: the Atlas validator did, and the agent read its two numbers back." |
| 0:24 to 0:42 | **Kill segment.** Terminal running `scripts/kill_demo.sh`, the wall beside it | `Failpoint after_dial_request fired`, the `WORKER KILLED (exit 137)` banner. The second phone rings once; "received twenty-five". The edge turns disputed: 15 tins. Enter: `restart n: ... documents read, 0 steps replayed`, `Attempt ... adopted ... No redial`. Call counter for Corner Mart: 1 | "The worker died after requesting the call and before saving it. A naive restart dials again. Ours finds the evidence first: one ring, one call." |
| 0:42 to 0:54 | **Order system-change segment.** The wall's Systems tin and feed | Switch North orders to v2: `contract failed: field qty_in_stock missing`, operations flagged, `adapter v2 active: canary ... = ...`, only the flagged operations retried. East orders: `contract failed. Episode ... retrieved. Adapter v2 active, 0 model calls` | "The distributor's order system changed its fields. The repair is only activated if it reproduces a count the ledger already trusts. The second order system repaired itself from memory." |
| 0:54 to 0:58 | Wall, the Close button | `Close refused by Atlas: 15 open units`; documented, recovered and open side by side | "Fifteen tins are unexplained, so Atlas will not let the recall close." |
| 0:58 to 1:00 | Disclosure card | The line below, in full | (read it) |

## Disclosure line (on screen at the end, and in the submission text)

> Real: MongoDB Atlas and every refusal it makes, two outbound phone calls
> (ElevenLabs over Twilio), the webhooks on a public URL, and every kill of the
> worker. Simulated: the clock (ten days played in two hours), the other 36
> parties (behaviour profiles), the two distributor order systems (mock processes
> written today) and the shipment records. All seeded data is synthetic.

## If a segment cannot be recorded live

- Voice fails: record the call segment with the party form on a phone instead
  (the same rejection appears on the wall) and say so in the voice-over.
- The second phone is flaky: record the kill with Corner Mart simulated
  (`python scripts/demo.py start --scenario recall_1 --sim-party corner-mart`);
  the call counter still shows one call.
- The order system repair stalls: show the last passing run of
  `python -m pytest tests/test_order_system_change.py -q` and say it is the test.
