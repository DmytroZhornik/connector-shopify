"""Explicit recurring stock policy. No activation and no Production authority."""
import hashlib
import json
import math
import re

STAGING_DATABASE = "dmytrozhornik-ireland-tackle-hub-odoo-staging-impor-38725572"
STAGING_DATABASE_PREFIX = "dmytrozhornik-ireland-tackle-hub-odoo-staging-impor-"
CHANNEL = "root.shopify.ith_stock"
METHOD = "_ith_job_push_stock"
ENABLE_KEY = "ith.stock.policy.enabled"
SCOPE_KEY = "ith.stock.policy.scope"
OTHER_GATES = ("ith.own.stock.queue.enabled", "ith.orders.queue.enabled",
               "ith.external.queue.enabled", "ith.external.shopify.auto_sync")
LOCATION_GIDS = {1: "gid://shopify/Location/103244792194",
                 2: "gid://shopify/Location/103676412290"}


def positive_int(value):
    return type(value) is int and value > 0


def quantity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("Only finite nonnegative integral available quantities are allowed")
    return int(value)


def stock_scope(enabled, raw, database):
    valid_database = isinstance(database, str) and re.fullmatch(re.escape(STAGING_DATABASE_PREFIX) + r"[1-9][0-9]*", database)
    if enabled != "True" or not valid_database or not isinstance(raw, str):
        return None
    try:
        s = json.loads(raw)
        if not isinstance(s, dict) or set(s) != {
            "schema", "database", "generation", "instance_id", "company_id", "min_job_id",
            "quantity_basis", "product_statuses", "scan_limit", "job_limit", "recheck_seconds", "locations", "items",
        }:
            return None
        if s["schema"] != 1 or type(s["schema"]) is not int or s["database"] != database:
            return None
        if s["instance_id"] != 1 or not positive_int(s["instance_id"]) or not positive_int(s["company_id"]):
            return None
        if not positive_int(s["min_job_id"]) or s["quantity_basis"] not in ("free_qty", "qty_available"):
            return None
        if not isinstance(s["generation"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", s["generation"]):
            return None
        statuses = s["product_statuses"]
        if not isinstance(statuses, list) or not statuses or len(statuses) != len(set(statuses)) or not set(statuses) <= {"DRAFT", "ACTIVE"}:
            return None
        for field, maximum in (("scan_limit", 1000), ("job_limit", 100)):
            if not positive_int(s[field]) or s[field] > maximum:
                return None
        if not positive_int(s["recheck_seconds"]) or not 600 <= s["recheck_seconds"] <= 86400:
            return None
        locations = s["locations"]
        if not isinstance(locations, list) or not 1 <= len(locations) <= 2:
            return None
        location_ids = set()
        for loc in locations:
            if not isinstance(loc, dict) or set(loc) != {
                "id", "gid", "odoo_location_id", "supplier", "snapshot_version", "valid_until",
            }:
                return None
            lid = loc["id"]
            if not positive_int(lid) or lid not in LOCATION_GIDS or lid in location_ids:
                return None
            if loc["gid"] != LOCATION_GIDS[lid] or type(loc["supplier"]) is not bool or loc["supplier"] != (lid == 2):
                return None
            if not positive_int(loc["odoo_location_id"]):
                return None
            if lid == 1:
                if loc["odoo_location_id"] != 5 or loc["snapshot_version"] is not None or loc["valid_until"] is not None:
                    return None
            else:
                if not positive_int(loc["snapshot_version"]) or type(loc["valid_until"]) not in (int, float) or not math.isfinite(loc["valid_until"]) or loc["valid_until"] <= 0:
                    return None
            location_ids.add(lid)
        items = s["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 10000:
            return None
        seen = {"binding_id": set(), "stock_product_id": set(), "sku": set(), "variant_gid": set(), "inventory_item_gid": set()}
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "binding_id", "stock_product_id", "sku", "variant_gid", "inventory_item_gid", "location_ids",
            }:
                return None
            if not positive_int(item["binding_id"]) or not positive_int(item["stock_product_id"]):
                return None
            if not isinstance(item["sku"], str) or not item["sku"] or item["sku"] != item["sku"].strip():
                return None
            for field, resource in (("variant_gid", "ProductVariant"), ("inventory_item_gid", "InventoryItem")):
                if not isinstance(item[field], str) or not re.fullmatch(r"gid://shopify/" + resource + r"/[1-9][0-9]*", item[field]):
                    return None
            lids = item["location_ids"]
            if not isinstance(lids, list) or not lids or any(not positive_int(x) for x in lids) or len(lids) != len(set(lids)) or not set(lids) <= location_ids:
                return None
            for field, values in seen.items():
                if item[field] in values:
                    return None
                values.add(item[field])
        return s
    except (ValueError, TypeError, OverflowError):
        return None


def encode(scope):
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def digest(scope):
    return hashlib.sha256(encode(scope).encode()).hexdigest()


def identity(scope, binding_id, location_id):
    return f"ith.stock.v1.{digest(scope)}.{binding_id}.{location_id}"


def scoped_item(scope, binding_id, location_id):
    if type(binding_id) is not int or type(location_id) is not int:
        return None
    return next((i for i in scope["items"] if i["binding_id"] == binding_id and location_id in i["location_ids"]), None)


def location_ready(loc, now, snapshot_version=None):
    return bool(not loc["supplier"] or (
        type(now) in (int, float) and math.isfinite(now) and now < loc["valid_until"]
        and snapshot_version == loc["snapshot_version"] and type(snapshot_version) is int
    ))


def job_matches(scope, *, job_id, model, method, channel, args, kwargs,
                identity_key, record_ids, instance_id):
    if not scope or not positive_int(job_id) or job_id <= scope["min_job_id"]:
        return False
    if model != "shopify.product.variant" or method != METHOD or channel != CHANNEL or type(instance_id) is not int or instance_id != 1:
        return False
    if not isinstance(record_ids, (list, tuple)) or len(record_ids) != 1:
        return False
    if not isinstance(args, (list, tuple)) or len(args) != 1 or kwargs != {}:
        return False
    item = scoped_item(scope, record_ids[0], args[0])
    return bool(item and identity_key == identity(scope, item["binding_id"], args[0]))


def live_level(node, item, loc, statuses):
    """No missing level => implicit zero; no inventory activation or catalogue write."""
    inventory = (node or {}).get("inventoryItem") or {}
    if not (node and node.get("id") == item["variant_gid"] and node.get("sku") == item["sku"]
            and (node.get("product") or {}).get("status") in statuses
            and inventory.get("id") == item["inventory_item_gid"] and inventory.get("tracked") is True):
        raise ValueError("Remote identity/status/tracking changed")
    level = inventory.get("inventoryLevel") or {}
    if not level.get("id"):
        raise ValueError("Remote inventory level missing; never create or assume zero")
    values = [v for v in level.get("quantities", []) if v.get("name") == "available"]
    if len(values) != 1:
        raise ValueError("Remote available quantity is incomplete")
    # Shopify can be negative; CAS must retain the exact integral compare quantity.
    value = values[0].get("quantity")
    if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value:
        raise ValueError("Remote available quantity is not integral")
    return int(value)
