"""Pure, fail-closed policy helpers for the bounded Staging Orders pilot."""

import json
import math
import re

ORDER_ID = re.compile(r"gid://shopify/Order/[1-9][0-9]*\Z")
ORDER_TOPICS = frozenset(("orders/create", "orders/updated", "orders/cancelled"))
ORDER_METHODS = frozenset(("_job_import_order", "_job_import_order_from_api"))


def order_scope(enabled, raw, now):
    """Require a short-lived, finite, exact scope; no wildcards or old jobs."""
    if enabled != "True":
        return None
    try:
        scope = json.loads(raw)
        if not isinstance(scope, dict):
            return None
        expected = {"min_job_id", "order_ids", "webhook_ids", "expires_at",
                    "cron_id", "reconcile_cron_id"}
        if set(scope) != expected:
            return None
        for key in ("min_job_id", "cron_id", "reconcile_cron_id"):
            if type(scope[key]) is not int or scope[key] <= 0:
                return None
        if scope["cron_id"] == scope["reconcile_cron_id"]:
            return None
        until = scope["expires_at"]
        if type(until) not in (int, float) or not math.isfinite(until):
            return None
        if not now < until <= now + 1800:
            return None
        orders, webhooks = scope["order_ids"], scope["webhook_ids"]
        if not isinstance(orders, list) or not 1 <= len(orders) <= 20:
            return None
        if any(not isinstance(item, str) or not ORDER_ID.fullmatch(item) for item in orders):
            return None
        if len(set(orders)) != len(orders):
            return None
        if not isinstance(webhooks, list) or len(webhooks) > 20:
            return None
        if any(not isinstance(item, str) or not item or len(item) > 200 for item in webhooks):
            return None
        if len(set(webhooks)) != len(webhooks):
            return None
        return scope
    except (TypeError, ValueError, KeyError):
        return None


def mapping_issue(line, variant_exists, canonical_exists, canonical_sku):
    """Never silently substitute a different SKU or make unknown inventory."""
    if line.get("gift_card"):
        return ""
    if not variant_exists:
        return "No Shopify variant binding"
    if not canonical_exists:
        return "No canonical stock product"
    if not line.get("sku") or line["sku"] != canonical_sku:
        return "Shopify SKU differs from canonical stock SKU"
    return ""


def scoped_job(scope, *, job_id, model, method, channel, args, kwargs,
               identity_key=None, record_ids=(), instance_id=None, order_id=None,
               webhook_id=None, topic=None):
    """Check arguments AND the exact source identity on each graph edge."""
    if not scope or type(job_id) is not int or job_id <= scope["min_job_id"]:
        return False
    if not isinstance(identity_key, str) or not identity_key:
        return False
    if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
        return False
    if model == "shopify.order" and method in ORDER_METHODS:
        if channel != "root.shopify.import" or record_ids:
            return False
        if len(args) not in (2, 3) or type(args[0]) is not int or args[0] != 1:
            return False
        if method == "_job_import_order_from_api":
            if kwargs or not isinstance(args[1], str) or args[1] not in scope["order_ids"]:
                return False
            gid = args[1]
            if len(args) == 3:
                return bool(
                    isinstance(args[2], dict)
                    and identity_key in {
                        f"shopify.order.webhook.1.{gid}.{wid}"
                        for wid in scope["webhook_ids"]
                    }
                )
            return identity_key in {
                f"ith.c1.order.api.1.{gid}",
                f"shopify.order.reconcile.1.{gid}",
                f"shopify.order.mapping.retry.1.{gid}",
            }
        if len(args) != 2 or not isinstance(args[1], dict):
            return False
        if set(kwargs) - {"payload_normalized", "is_draft"}:
            return False
        if any(type(value) is not bool for value in kwargs.values()):
            return False
        gid = args[1].get("id")
        return bool(
            not kwargs.get("is_draft") and not args[1].get("is_draft")
            and gid in scope["order_ids"]
            and identity_key == f"ith.c1.order.snapshot.1.{gid}"
        )
    if args or kwargs or len(record_ids) != 1 or instance_id != 1:
        return False
    if model == "shopify.order" and method == "_job_refresh_fulfillment_orders":
        return bool(
            channel == "root.shopify.import" and order_id in scope["order_ids"]
            and identity_key == f"shopify.fulfillment.orders.refresh.1.{order_id}"
        )
    if model == "shopify.webhook.event" and method == "_dispatch":
        return bool(
            channel == "root.shopify.webhook" and order_id in scope["order_ids"]
            and webhook_id in scope["webhook_ids"] and topic in ORDER_TOPICS
            and identity_key == f"shopify.webhook.dispatch.1.{webhook_id}"
        )
    if model == "shopify.instance" and method == "_ith_job_reconcile_selected_orders":
        return bool(
            channel == "root.shopify.import" and list(record_ids) == [1]
            and identity_key == f"ith.orders.reconcile.1.{scope['min_job_id']}"
        )
    return False
