"""The distributor order system and everything that talks to it (V2 §7).

    contract      canonical fields, dot paths, ContractError (detection is code)
    order_client OrderClient: adapter-driven calls, OrderSystemTimeout / OrderSystemError,
                  order_url(), build_order_systems() for ctx.order systems
    adapters      builtin v1, candidates, activate() in one transaction
    operations    intent -> sent -> verified by read-back; place_hold,
                  confirm_hold, read_accounts, read_inventory; resolve_in_doubt,
                  contain, mark_needs_reverify, retry_needs_reverify
    canary        a ledger count read again through a candidate adapter
    repair        request_repair(ctx, order system): memory, then Claude or rules,
                  then the canary; adapter episodes

Use the module paths (systems.operations.place_hold, systems.repair.request_repair).
"""
from . import adapters, canary, contract, operations, order_client, repair

__all__ = ["adapters", "canary", "contract", "operations", "order_client", "repair"]
