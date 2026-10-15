"""Read-only Shopify GraphQL query for external supplier reconciliation."""

from datetime import datetime
import hashlib
import json

EXTERNAL_ORDER_QUERY = """
query ITHExternalOrder($id: ID!) {
  order(id: $id) {
    id
    cancelledAt

    fulfillmentOrders(first: 250) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        status
        assignedLocation {
          location {
            id
          }
        }
        lineItems(first: 250) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            totalQuantity
            remainingQuantity
            lineItem {
              id
            }
          }
        }
      }
    }

    fulfillments(first: 250) {
      id
      status
      location {
        id
      }
      fulfillmentLineItems(first: 250) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          quantity
          lineItem {
            id
          }
        }
      }
    }
  }
}
"""


_FRESH_EXTERNAL_READ_CAPABILITY = object()


class _FreshExternalOrderReadProof:
    """In-memory capability bound to one fetched Shopify order payload.

    It deliberately has no JSON representation and is only issued by the
    read path after the complete query response has been validated.
    """

    __slots__ = (
        "_capability", "_instance_id", "_shopify_id", "_payload",
        "_payload_digest", "read_started_at",
    )

    def __init__(self, instance_id, shopify_id, payload, read_started_at):
        self._capability = _FRESH_EXTERNAL_READ_CAPABILITY
        self._instance_id = instance_id
        self._shopify_id = shopify_id
        self._payload = payload
        self._payload_digest = self._digest(payload)
        self.read_started_at = read_started_at

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()

    def matches(self, instance_id, shopify_id, payload):
        return (
            self._capability is _FRESH_EXTERNAL_READ_CAPABILITY
            and self._instance_id == instance_id
            and self._shopify_id == shopify_id
            and self._payload is payload
            and self._payload_digest == self._digest(payload)
        )


def _issue_fresh_external_order_read_proof(
    instance_id, shopify_id, payload, *, read_started_at=None,
):
    """Issue a non-serializable proof for one complete successful read."""
    if (
        not instance_id
        or not shopify_id
        or not isinstance(payload, dict)
        or payload.get("id") != shopify_id
    ):
        raise ValueError("Fresh Shopify read does not match the bound order")
    validate_external_order_payload(payload)
    started = read_started_at or datetime.utcnow()
    if not isinstance(started, datetime) or started.tzinfo is not None:
        raise ValueError("Invalid fresh-read start time")
    return _FreshExternalOrderReadProof(instance_id, shopify_id, payload, started)


def _has_fresh_external_order_read_proof(
    proof, instance_id, shopify_id, payload,
):
    """True only for the in-memory proof produced for this exact payload."""
    return isinstance(proof, _FreshExternalOrderReadProof) and proof.matches(
        instance_id, shopify_id, payload
    )


def validate_external_order_payload(payload):
    """Reject incomplete Shopify data before supplier stock reconciliation."""
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("Missing Shopify order")
    if "cancelledAt" not in payload:
        raise ValueError("Missing Shopify cancellation state")

    def check_connection(connection, label):
        if not isinstance(connection, dict):
            raise ValueError(f"Missing {label}")
        page = connection.get("pageInfo")
        nodes = connection.get("nodes")
        if not isinstance(page, dict) or not isinstance(nodes, list):
            raise ValueError(f"Incomplete {label}")
        if page.get("hasNextPage") is not False:
            raise ValueError(f"Unverified pagination: {label}")
        return nodes

    orders = check_connection(
        payload.get("fulfillmentOrders"), "fulfillment orders"
    )
    for order in orders:
        if not isinstance(order, dict) or not order.get("id"):
            raise ValueError("Invalid fulfillment order")
        if not isinstance(order.get("status"), str) or not order["status"]:
            raise ValueError("Missing fulfillment order status")
        lines = check_connection(order.get("lineItems"), "allocation lines")
        for line in lines:
            if not isinstance(line, dict) or not (line.get("lineItem") or {}).get("id"):
                raise ValueError("Invalid allocation line")
            remaining = line.get("remainingQuantity")
            total = line.get("totalQuantity")
            if type(remaining) is not int or type(total) is not int or not 0 <= remaining <= total:
                raise ValueError("Invalid allocation quantities")

    shipments = payload.get("fulfillments")
    if not isinstance(shipments, list) or len(shipments) >= 250:
        raise ValueError("Missing or potentially truncated shipment list")

    for shipment in shipments:
        if not isinstance(shipment, dict) or not shipment.get("id"):
            raise ValueError("Invalid shipment")
        if not isinstance(shipment.get("status"), str) or not shipment["status"]:
            raise ValueError("Missing shipment status")
        lines = check_connection(
            shipment.get("fulfillmentLineItems"), "shipment lines"
        )
        for line in lines:
            if not isinstance(line, dict) or not (line.get("lineItem") or {}).get("id"):
                raise ValueError("Invalid shipment line")
            if type(line.get("quantity")) is not int or line["quantity"] < 0:
                raise ValueError("Invalid shipped quantity")

    return True


def calculate_external_quantities(payload, external_location_ids, known_location_ids=None):
    """Calculate supplier quantities by Shopify order line."""
    validate_external_order_payload(payload)
    if known_location_ids is None:
        raise ValueError("Known Shopify locations are required")
    validate_external_locations(payload, known_location_ids)
    external_location_ids = set(external_location_ids)
    known_location_ids = set(known_location_ids)
    if not external_location_ids.issubset(known_location_ids):
        raise ValueError("External location is not registered")
    result = {}

    for fo in payload["fulfillmentOrders"]["nodes"]:
        if fo["status"] in {"CANCELLED", "CLOSED"}:
            continue
        location = ((fo.get("assignedLocation") or {}).get("location") or {})
        if location.get("id") not in external_location_ids:
            continue

        for line in fo["lineItems"]["nodes"]:
            key = line["lineItem"]["id"]
            remaining = line.get("remainingQuantity")
            total = line.get("totalQuantity")
            if (
                type(remaining) is not int
                or type(total) is not int
                or not 0 <= remaining <= total
            ):
                raise ValueError("Invalid allocation quantities")

            entry = result.setdefault(key, {"reserved": 0, "fulfilled": 0})
            entry["reserved"] += remaining

    for shipment in payload["fulfillments"]:
        if shipment.get("status") != "SUCCESS":
            continue
        location = shipment.get("location") or {}
        if location.get("id") not in external_location_ids:
            continue

        for line in shipment["fulfillmentLineItems"]["nodes"]:
            key = line["lineItem"]["id"]
            quantity = line.get("quantity")
            if type(quantity) is not int or quantity < 0:
                raise ValueError("Invalid shipped quantity")

            entry = result.setdefault(key, {"reserved": 0, "fulfilled": 0})
            entry["fulfilled"] += quantity

    for entry in result.values():
        entry["total"] = entry["reserved"] + entry["fulfilled"]

    return result


def plan_external_snapshot_reconciliation(
    payload,
    external_location_ids,
    known_location_ids,
    previous_movements,
    *,
    instance_id,
    shopify_id,
    release_proof=None,
):
    """Plan External movements, gating every supplier-stock release.

    A normal validated local snapshot may reserve or retain stock, but it
    cannot lower an existing external movement. A lower quantity, a missing
    External line (including a move to Own), or a cancellation is permitted
    only with the in-memory proof issued after a fresh complete Shopify read.
    Successfully shipped quantity is never allowed to decrease.
    """
    quantities = calculate_external_quantities(
        payload, external_location_ids, known_location_ids
    )
    cancelled_at = payload.get("cancelledAt")
    if cancelled_at is not None and not isinstance(cancelled_at, str):
        raise ValueError("Invalid Shopify cancellation timestamp")
    cancelled = bool(cancelled_at)
    has_fresh_read = _has_fresh_external_order_read_proof(
        release_proof, instance_id, shopify_id, payload
    )
    if cancelled and not has_fresh_read:
        raise ValueError(
            "Cancellation requires a fresh complete Shopify read"
        )

    previous_by_key = {}
    for movement in previous_movements:
        key = movement.get("source_line_key")
        if not isinstance(key, str) or not key:
            raise ValueError("Invalid previous Shopify External movement")
        if not key.startswith(f"{instance_id}:gid://shopify/LineItem/"):
            raise ValueError("Previous External movement belongs to another instance")
        updated = movement.get("write_date")
        if has_fresh_read and updated:
            if not isinstance(updated, datetime) or updated.tzinfo is not None:
                raise ValueError("Invalid previous movement update time")
            if updated > release_proof.read_started_at:
                raise ValueError(
                    "External movement changed during Shopify read; a new read is required"
                )
        if key in previous_by_key:
            raise ValueError("Duplicate previous Shopify External movement: " + key)
        previous_by_key[key] = movement

    targets = {}
    for shopify_line_id, values in quantities.items():
        key = f"{instance_id}:{shopify_line_id}"
        reserved = 0 if cancelled else values["reserved"]
        targets[key] = {
            "shopify_line_id": shopify_line_id,
            "reserved": reserved,
            "fulfilled": values["fulfilled"],
            "total": reserved + values["fulfilled"],
        }

    for key, previous in previous_by_key.items():
        previous_total = float(previous.get("order_quantity") or 0.0)
        previous_fulfilled = float(previous.get("fulfilled_quantity") or 0.0)
        if not 0 <= previous_fulfilled <= previous_total < float("inf"):
            raise ValueError("Invalid previous Shopify External movement: " + key)

        target = targets.get(key)
        if target is None:
            target = {
                "shopify_line_id": key.split(":", 1)[-1],
                "reserved": 0,
                "fulfilled": 0,
                "total": 0,
            }
            targets[key] = target

        # Shopify cancellation releases all outstanding reservations. The
        # already successful External shipment remains deducted.
        if cancelled:
            target["reserved"] = 0
            target["total"] = target["fulfilled"]

        if target["fulfilled"] < previous_fulfilled:
            raise ValueError(
                "Previously shipped quantity decreased: " + key
            )

        if target["total"] < previous_total and has_fresh_read:
            # A complete response is not proof that a pending/failed
            # shipment never left the supplier. Require manual review
            # rather than returning possibly shipped goods to availability.
            for shipment in payload["fulfillments"]:
                if (
                    (shipment.get("location") or {}).get("id") in external_location_ids
                    and shipment["status"] != "SUCCESS"
                    and any(
                        f"{instance_id}:{line['lineItem']['id']}" == key
                        and line["quantity"] > 0
                        for line in shipment["fulfillmentLineItems"]["nodes"]
                    )
                ):
                    raise ValueError(
                        "Unverified External shipment prevents stock release: " + key
                    )

        if target["total"] < previous_total and not has_fresh_read:
            raise ValueError(
                "Supplier stock release requires a fresh complete Shopify "
                "read for " + key
            )

    return targets



def validate_external_locations(payload, known_location_ids):
    """Reject allocations and shipments from unknown Shopify locations."""
    validate_external_order_payload(payload)
    known = set(known_location_ids)

    for fo in payload["fulfillmentOrders"]["nodes"]:
        location = ((fo.get("assignedLocation") or {}).get("location") or {})
        location_id = location.get("id")
        if location_id not in known:
            raise ValueError(f"Unknown fulfillment location: {location_id}")

    for shipment in payload["fulfillments"]:
        location = shipment.get("location") or {}
        location_id = location.get("id")
        if location_id not in known:
            raise ValueError(f"Unknown shipment location: {location_id}")

    return True
