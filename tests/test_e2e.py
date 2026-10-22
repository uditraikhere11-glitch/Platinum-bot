"""Local smoke test helpers for the SQLite signal transport and dry-run broker.

Run the normal automated suite with: pytest tests/ -v
No live broker credentials or remote server are required for the SQLite transport tests.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def test_dry_run_broker_does_not_submit_live_orders():
    """The broker's dry-run branch should return a simulated result."""
    from client_agent.client import DhanBroker

    broker = DhanBroker(client_id="TEST_CLIENT", access_token="TEST_TOKEN", dry_run=True)
    result = broker._place_dhan_order({"transactionType": "BUY", "quantity": 1})
    assert result["order_id"] == "DRY_RUN"
    assert result["status"] == "SIMULATED"
