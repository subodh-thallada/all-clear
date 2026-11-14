"""Stand-ins for outside systems the agent talks to.

    order system   the distributor order system (V2 §7): one FastAPI process per
             distributor, state in memory, a v1 -> v2 contract change and
             three injectable faults. `python -m levela.mock.order_system --name north`.
"""
