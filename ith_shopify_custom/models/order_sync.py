import logging
import re

from odoo import models

from odoo.addons.shopify_connector.lib.client import ShopifyUserError

_logger = logging.getLogger(__name__)


def _ith_shipping_stock_route(order_data):
    """ITH warehouse memo: IE/NI own; Benelux supplier; others review.

    This is a confirmation safeguard, not fulfillment allocation routing.
    Never infer the shipping destination from a billing/customer address.
    """
    raw = order_data.get("raw") or {}
    address = raw.get("shippingAddress") or raw.get("shipping_address") or {}
    country = str(
        address.get("countryCodeV2") or address.get("country_code") or ""
    ).upper().strip()
    postcode = str(address.get("zip") or "").upper().strip()
    if country == "IE":
        route = "own"
    elif country in ("GB", "UK") and re.match(r"^BT\d", postcode):
        route = "own"
    elif country in ("BE", "NL", "LU"):
        route = "external"
    else:
        return "review"
    # If explicit fulfillment allocations are present, do not contradict them.
    # Full split-routing is deliberately not implemented by this safeguard.
    connection = raw.get("fulfillmentOrders")
    if connection is not None:
        if not isinstance(connection, dict):
            return "review"
        if connection.get("pageInfo", {}).get("hasNextPage"):
            return "review"
        nodes = connection.get("nodes")
        if nodes is None:
            nodes = [edge.get("node", {}) for edge in connection.get("edges", [])]
        expected = {
            "own": "gid://shopify/Location/103244792194",
            "external": "gid://shopify/Location/103676412290",
        }[route]
        for fulfillment_order in nodes:
            if fulfillment_order.get("status") == "CANCELLED":
                continue
            assigned = fulfillment_order.get("assignedLocation") or {}
            location = assigned.get("location") or {}
            if location.get("id") != expected:
                return "review"
    return route


def _ith_may_auto_confirm(instance, order_data, sale_order, base_confirmation):
    """Do not let the default WH reserve ITH supplier/unknown shipments.

    Supplier and mixed allocations require a separate fulfillment-aware
    workflow. Keep those quotations, rather than invent physical stock or
    procurements. Preserve core eligibility, payment and draft policies.
    """
    if instance.id == 1 and _ith_shipping_stock_route(order_data) != "own":
        return False
    return base_confirmation(instance, order_data, sale_order)


class ShopifyInstance(models.Model):
    _inherit = "shopify.instance"

    def _queue_draft_orders(self, date_from=False, date_to=False):
        try:
            return super()._queue_draft_orders(date_from, date_to)
        except ShopifyUserError as exc:
            message = str(exc)

            if "Access denied for draftOrders field" in message:
                _logger.warning(
                    "Skipping Shopify draft orders for instance %s: %s",
                    self.display_name,
                    message,
                )
                return 0

            raise


class ShopifyOrderStockConfirmation(models.Model):
    _inherit = "shopify.order"

    def _should_confirm_shopify_order(self, instance, order_data, sale_order):
        return _ith_may_auto_confirm(
            instance,
            order_data,
            sale_order,
            super()._should_confirm_shopify_order,
        )
