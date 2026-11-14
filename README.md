# All Clear

"Lie to it, kill it, break the system under it: our agent still won't say 'all clear' until the database shows every recalled tin off every shelf."

A recall agent for a food company that sells through distributors and stores.
It finds every tin of a recalled batch, has it pulled from sale, and keeps the
recall open until the store's own records show it is gone, with the evidence in
MongoDB Atlas. Track: **Finish the Job**.

- **Built today, and how to run it:** [level-a/README.md](level-a/README.md)
  (the code keeps its codename, Level A).
- **The spec and its 49 tests:** [docs/All_Clear_build_spec_and_test_plan.md](docs/All_Clear_build_spec_and_test_plan.md)
- **The demo runbook:** [level-a/docs/RUNBOOK.md](level-a/docs/RUNBOOK.md)

**Real:** Atlas and every refusal it makes, the phone calls (ElevenLabs over
Twilio), the webhook server on a public URL, every kill of a worker.
**Simulated:** the clock, 36 parties, the distributors' order systems, the
shipment records, and the stores' tills and stock records. The product, the
batch and every name are invented.
