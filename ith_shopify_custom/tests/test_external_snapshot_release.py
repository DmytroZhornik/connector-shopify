"""Offline regressions for proof-gated External Shopify stock release.

Run directly, without Odoo or network:
    python3 ith_shopify_custom/tests/test_external_snapshot_release.py
"""

import copy
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timedelta
import unittest


MODULE_PATH = Path(__file__).parents[1] / "shopify_external_query.py"
SPEC = importlib.util.spec_from_file_location("ith_external_query", MODULE_PATH)
external_query = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(external_query)

INSTANCE_ID = 1
ORDER_ID = "gid://shopify/Order/99000000000021"
LINE_ID = "gid://shopify/LineItem/99000000000024"
EXTERNAL_LOCATION = "gid://shopify/Location/External"
OWN_LOCATION = "gid://shopify/Location/Own"
MOVEMENT_KEY = f"{INSTANCE_ID}:{LINE_ID}"


def connection(nodes):
    return {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }


def payload(
    *,
    location=EXTERNAL_LOCATION,
    remaining=4,
    total=4,
    cancelled_at=None,
    shipped=0,
):
    fulfillments = []
    if shipped:
        fulfillments.append({
            "id": "gid://shopify/Fulfillment/1",
            "status": "SUCCESS",
            "location": {"id": EXTERNAL_LOCATION},
            "fulfillmentLineItems": connection([{
                "id": "gid://shopify/FulfillmentLineItem/1",
                "quantity": shipped,
                "lineItem": {"id": LINE_ID},
            }]),
        })
    return {
        "id": ORDER_ID,
        "cancelledAt": cancelled_at,
        "fulfillmentOrders": connection([{
            "id": "gid://shopify/FulfillmentOrder/1",
            "status": "OPEN",
            "assignedLocation": {"location": {"id": location}},
            "lineItems": connection([{
                "id": "gid://shopify/FulfillmentOrderLineItem/1",
                "totalQuantity": total,
                "remainingQuantity": remaining,
                "lineItem": {"id": LINE_ID},
            }]),
        }]),
        "fulfillments": fulfillments,
    }


def prior(*, order_quantity=4, fulfilled_quantity=0):
    return [{
        "source_line_key": MOVEMENT_KEY,
        "order_quantity": order_quantity,
        "fulfilled_quantity": fulfilled_quantity,
    }]


def plan(snapshot, movements, proof=None):
    return external_query.plan_external_snapshot_reconciliation(
        snapshot,
        {EXTERNAL_LOCATION},
        {EXTERNAL_LOCATION, OWN_LOCATION},
        movements,
        instance_id=INSTANCE_ID,
        shopify_id=ORDER_ID,
        release_proof=proof,
    )


def fresh_proof(snapshot):
    return external_query._issue_fresh_external_order_read_proof(
        INSTANCE_ID, ORDER_ID, snapshot
    )


class TestExternalSnapshotRelease(unittest.TestCase):

    def test_direct_decrease_rejects_none_bool_and_json_context_values(self):
        snapshot = payload(remaining=3, total=3)
        for invalid_proof in (None, True, {"ith_external_release_verified": True}):
            with self.subTest(invalid_proof=invalid_proof), self.assertRaisesRegex(
                ValueError, "fresh complete Shopify read"
            ):
                plan(snapshot, prior(), invalid_proof)

    def test_direct_transfer_and_cancellation_remain_fail_closed(self):
        for snapshot in (
            payload(location=OWN_LOCATION),
            payload(cancelled_at="2026-10-15T11:00:00Z"),
        ):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(
                ValueError, "fresh complete Shopify read"
            ):
                plan(snapshot, prior())

    def test_proof_is_not_json_and_is_bound_to_exact_instance_order_and_payload(self):
        snapshot = payload(remaining=3, total=3)
        proof = fresh_proof(snapshot)
        with self.assertRaises(TypeError):
            json.dumps(proof)
        self.assertFalse(external_query._has_fresh_external_order_read_proof(
            proof, INSTANCE_ID + 1, ORDER_ID, snapshot
        ))
        self.assertFalse(external_query._has_fresh_external_order_read_proof(
            proof, INSTANCE_ID, ORDER_ID, copy.deepcopy(snapshot)
        ))
        with self.assertRaisesRegex(ValueError, "fresh complete Shopify read"):
            plan(copy.deepcopy(snapshot), prior(), proof)

    def test_verified_same_snapshot_quantity_reduction_plans_three(self):
        snapshot = payload(remaining=3, total=3)
        targets = plan(snapshot, prior(), fresh_proof(snapshot))
        self.assertEqual(targets[MOVEMENT_KEY], {
            "shopify_line_id": LINE_ID,
            "reserved": 3,
            "fulfilled": 0,
            "total": 3,
        })
        self.assertEqual(15 - targets[MOVEMENT_KEY]["total"], 12)

    def test_verified_external_to_own_plans_full_release(self):
        snapshot = payload(location=OWN_LOCATION)
        targets = plan(snapshot, prior(), fresh_proof(snapshot))
        self.assertEqual(targets[MOVEMENT_KEY]["reserved"], 0)
        self.assertEqual(targets[MOVEMENT_KEY]["fulfilled"], 0)
        self.assertEqual(targets[MOVEMENT_KEY]["total"], 0)

    def test_verified_cancellation_before_shipping_plans_full_release(self):
        snapshot = payload(cancelled_at="2026-10-15T11:00:00Z")
        targets = plan(snapshot, prior(), fresh_proof(snapshot))
        self.assertEqual(targets[MOVEMENT_KEY]["reserved"], 0)
        self.assertEqual(targets[MOVEMENT_KEY]["total"], 0)

    def test_verified_cancellation_after_partial_external_shipment_releases_only_reserved(self):
        snapshot = payload(
            remaining=3,
            total=4,
            shipped=1,
            cancelled_at="2026-10-15T11:00:00Z",
        )
        targets = plan(snapshot, prior(fulfilled_quantity=1), fresh_proof(snapshot))
        self.assertEqual(targets[MOVEMENT_KEY]["reserved"], 0)
        self.assertEqual(targets[MOVEMENT_KEY]["fulfilled"], 1)
        self.assertEqual(targets[MOVEMENT_KEY]["total"], 1)

    def test_fulfilled_quantity_never_decreases_even_with_fresh_proof(self):
        snapshot = payload(location=OWN_LOCATION)
        with self.assertRaisesRegex(ValueError, "Previously shipped quantity decreased"):
            plan(snapshot, prior(fulfilled_quantity=1), fresh_proof(snapshot))

    def test_incomplete_payload_cannot_issue_fresh_read_proof(self):
        snapshot = payload(remaining=3, total=3)
        del snapshot["fulfillmentOrders"]["pageInfo"]
        with self.assertRaisesRegex(ValueError, "Incomplete fulfillment orders"):
            fresh_proof(snapshot)

    def test_new_cancelled_order_never_reserves_even_without_previous_movement(self):
        snapshot = payload(cancelled_at="2026-10-15T11:00:00Z")
        with self.assertRaisesRegex(ValueError, "fresh complete Shopify read"):
            plan(snapshot, [])
        target = plan(snapshot, [], fresh_proof(snapshot))[MOVEMENT_KEY]
        self.assertEqual(target["reserved"], 0)
        self.assertEqual(target["total"], 0)

    def test_mutating_same_payload_object_invalidates_fresh_proof(self):
        snapshot = payload(remaining=3, total=3)
        proof = fresh_proof(snapshot)
        node = snapshot["fulfillmentOrders"]["nodes"][0]["lineItems"]["nodes"][0]
        node.update(remainingQuantity=1, totalQuantity=1)
        with self.assertRaisesRegex(ValueError, "fresh complete Shopify read"):
            plan(snapshot, prior(), proof)

    def test_movement_changed_during_api_read_requires_new_read(self):
        snapshot = payload(remaining=3, total=3)
        started = datetime(2026, 10, 15, 11, 0, 0)
        proof = external_query._issue_fresh_external_order_read_proof(
            INSTANCE_ID, ORDER_ID, snapshot, read_started_at=started,
        )
        movement = prior()[0]
        movement["write_date"] = started + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "changed during Shopify read"):
            plan(snapshot, [movement], proof)
        movement["write_date"] = started - timedelta(seconds=1)
        self.assertEqual(plan(snapshot, [movement], proof)[MOVEMENT_KEY]["total"], 3)

    def test_other_instance_movement_is_not_released(self):
        snapshot = payload(location=OWN_LOCATION)
        movement = prior()[0]
        movement["source_line_key"] = f"2:{LINE_ID}"
        with self.assertRaisesRegex(ValueError, "another instance"):
            plan(snapshot, [movement], fresh_proof(snapshot))

    def test_terminal_fo_does_not_leave_a_reservation_after_verified_read(self):
        for status in ("CLOSED", "CANCELLED"):
            with self.subTest(status=status):
                snapshot = payload()
                snapshot["fulfillmentOrders"]["nodes"][0]["status"] = status
                with self.assertRaisesRegex(ValueError, "fresh complete Shopify read"):
                    plan(snapshot, prior())
                self.assertEqual(plan(snapshot, prior(), fresh_proof(snapshot))[MOVEMENT_KEY]["total"], 0)

    def test_missing_cancellation_or_malformed_own_quantity_cannot_issue_proof(self):
        snapshot = payload()
        del snapshot["cancelledAt"]
        with self.assertRaisesRegex(ValueError, "cancellation state"):
            fresh_proof(snapshot)
        snapshot = payload(location=OWN_LOCATION)
        snapshot["fulfillmentOrders"]["nodes"][0]["lineItems"]["nodes"][0]["remainingQuantity"] = None
        with self.assertRaisesRegex(ValueError, "allocation quantities"):
            fresh_proof(snapshot)

    def test_uncertain_external_shipment_cannot_release_possibly_shipped_stock(self):
        for status in ("PENDING", "FAILURE"):
            with self.subTest(status=status):
                snapshot = payload(
                    remaining=3, shipped=1,
                    cancelled_at="2026-10-15T11:00:00Z",
                )
                snapshot["fulfillments"][0]["status"] = status
                with self.assertRaisesRegex(ValueError, "Unverified External shipment"):
                    plan(snapshot, prior(), fresh_proof(snapshot))


if __name__ == "__main__":
    unittest.main(verbosity=2)
