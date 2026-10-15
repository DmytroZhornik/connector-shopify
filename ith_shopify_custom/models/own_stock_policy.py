"""Pure bounded Own stock acceptance rules; no production scheduling policy."""
import json
import math

OWN_LOCATION_GID = "gid://shopify/Location/103244792194"
OWN_CHANNEL = "root.shopify.export"


def own_stock_scope(enabled, raw, now):
    if enabled != "True" or not isinstance(raw, str):
        return None
    try:
        scope = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(scope, dict) or set(scope) != {
        "instance_id", "cron_id", "location_id", "odoo_location_id", "location_gid",
        "min_job_id", "expires_at", "items",
    }:
        return None
    for key in ("instance_id", "cron_id", "location_id", "odoo_location_id", "min_job_id"):
        if type(scope[key]) is not int or scope[key] < 1:
            return None
    if scope["instance_id"] != 1 or scope["cron_id"] != 56:
        return None
    if scope["location_gid"] != OWN_LOCATION_GID:
        return None
    expires = scope["expires_at"]
    if type(expires) not in (float, int) or not math.isfinite(expires):
        return None
    if not now < expires <= now + 1200:
        return None
    items = scope["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 5:
        return None
    ids, skus, inventories, jobs = set(), set(), set(), set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "job_id", "binding_id", "stock_product_id", "sku", "variant_gid", "inventory_item_gid"
        }:
            return None
        if any(type(item[key]) is not int or item[key] < 1 for key in ("job_id", "binding_id", "stock_product_id")):
            return None
        if item["job_id"] <= scope["min_job_id"]:
            return None
        if not isinstance(item["sku"], str) or not item["sku"].strip():
            return None
        for key, resource in (("variant_gid", "ProductVariant"), ("inventory_item_gid", "InventoryItem")):
            value = item[key]
            prefix = f"gid://shopify/{resource}/"
            if not isinstance(value, str) or not value.startswith(prefix) or not value[len(prefix):].isdigit():
                return None
        if item["binding_id"] in ids or item["sku"] in skus or item["inventory_item_gid"] in inventories or item["job_id"] in jobs:
            return None
        ids.add(item["binding_id"])
        skus.add(item["sku"])
        inventories.add(item["inventory_item_gid"])
        jobs.add(item["job_id"])
    return scope


def own_identity(scope, binding_id):
    return f"shopify.inventory.delta.1.{binding_id}.{scope['location_id']}"


def own_job_matches(scope, *, job_id, model, method, channel, args, kwargs,
                    identity_key, record_ids, instance_id):
    if not scope or type(job_id) is not int or job_id <= scope["min_job_id"]:
        return False
    if model != "shopify.product.variant" or method != "_job_push_inventory_level":
        return False
    if channel != OWN_CHANNEL or instance_id != 1:
        return False
    if not isinstance(record_ids, (list, tuple)) or len(record_ids) != 1:
        return False
    if type(record_ids[0]) is not int:
        return False
    if not isinstance(args, (list, tuple)) or len(args) != 1:
        return False
    if type(args[0]) is not int or args[0] != scope["location_id"]:
        return False
    if kwargs != {}:
        return False
    binding_id = record_ids[0]
    return bool(
        any(item["binding_id"] == binding_id and item["job_id"] == job_id for item in scope["items"])
        and identity_key == own_identity(scope, binding_id)
    )
