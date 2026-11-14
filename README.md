# All Clear

An agent that follows a product recall until every unit has an explanation.
Track: **Finish the Job**.

The backend runs a milk powder recall end to end: store records and complaints,
a second node for a chain that runs its own copy, and an audit trail. The workflow
screen also has its own simulated demo. The build plan records every deviation
from the product spec.

**Real mechanisms:** database validators, transactions, worker leases and recovery.
Live voice calls require the operator's configured providers.

**Simulated:** the bundled parties, shipment records, distributor order systems,
clock and screen demo. Every product and company in the demo is invented.

- [Run the engine and its checks](level-a/README.md)
- [Current product spec and test plan](docs/All_Clear_build_spec_and_test_plan.md)
- [Backend implementation plan](level-a/docs/BUILD_PLAN.md)
- [Workflow screen](level-a/web/README.md)
- [Demo runbook](level-a/docs/RUNBOOK.md)
- [Deployment](level-a/deploy/README.md)

GitHub Actions runs database smoke checks, the fast and slow Python suites on
Python 3.11 and 3.12 with MongoDB 8.0, and the workflow screen's tests and build.
