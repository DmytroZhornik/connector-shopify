"""Bounded Staging contracts: original DRAFT, or one ACTIVE observe-only cohort."""
import hashlib
import json
import math
import re

from .stock_dispatch_policy import stock_scope, quantity, scoped_item

ENABLE_KEY = 'ith.joint.policy.enabled'
SCOPE_KEY = 'ith.joint.policy.scope'
JOINT_LOCK = 'ith.joint.accounting'
SUPPLIER_LOCK = 'ith.external.stock.snapshot'
CLOSED_KEYS = ('ith.stock.policy.enabled', 'ith.orders.queue.enabled',
               'ith.own.stock.queue.enabled', 'ith.external.queue.enabled',
               'ith.external.shopify.auto_sync')
ORDER_CHANNEL = 'root.shopify.ith_joint_orders'
STOCK_CHANNEL = 'root.shopify.ith_joint_stock'
ORDER_METHOD = '_ith_joint_refresh_orders'
STOCK_METHOD = '_ith_joint_push_inventory'
KIND = 'joint-stock-orders-v1'
OBSERVE_KIND = 'joint-stock-orders-observe-active-v1'
MIRROR_USAGE_KEYS = ('stock_quant', 'stock_move', 'stock_move_line',
                     'sale_order_line', 'purchase_order_line', 'account_move_line')


def encode(scope):
    return json.dumps(scope, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(scope):
    return hashlib.sha256(encode(scope).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def positive(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def observe_only(scope):
    return isinstance(scope, dict) and scope.get('kind') == OBSERVE_KIND


def joint_scope(enabled, raw, database, now):
    if enabled != 'True' or not isinstance(raw, str) or not finite(now):
        return None
    try:
        s = json.loads(raw)
        keys = {'kind', 'database', 'generation', 'instance_id', 'company_id',
                'min_job_id', 'valid_until', 'order_ids', 'order_refresh_seconds',
                'max_order_age_seconds', 'job_limit', 'api_read_limit', 'stock'}
        if not isinstance(s, dict) or set(s) != keys or s['kind'] not in (KIND, OBSERVE_KIND):
            return None
        stock = stock_scope('True', encode(s['stock']), database)
        statuses = ['ACTIVE'] if observe_only(s) else ['DRAFT']
        if not stock or stock['product_statuses'] != statuses or stock['quantity_basis'] != 'free_qty':
            return None  # Neither kind is an unbounded live-store discovery policy.
        if len(stock['items']) > 50 or stock['scan_limit'] > 50 or stock['job_limit'] > 20:
            return None
        for field in ('database', 'generation', 'instance_id', 'company_id', 'min_job_id'):
            if s[field] != stock[field] or type(s[field]) is not type(stock[field]):
                return None
        if not finite(s['valid_until']) or not now < s['valid_until'] <= now + 3600:
            return None
        if not positive(s['order_refresh_seconds'], 600) or not positive(s['max_order_age_seconds'], 600):
            return None
        if s['max_order_age_seconds'] < s['order_refresh_seconds']:
            return None
        if not positive(s['job_limit'], 20) or s['job_limit'] < 2 or not positive(s['api_read_limit'], 16) or s['api_read_limit'] < 3:
            return None
        orders = s['order_ids']
        if not isinstance(orders, list) or not 1 <= len(orders) <= 5:
            return None
        if len(orders) != len(set(orders)) or any(not isinstance(x, str) or not re.fullmatch(r'gid://shopify/Order/[1-9][0-9]*', x) for x in orders):
            return None
        if observe_only(s) and not (
                len(orders) == 1 and len(stock['items']) == 1 and stock['scan_limit'] == 1
                and stock['job_limit'] <= 2 and s['job_limit'] == 2
                and len(stock['locations']) == 1 and stock['locations'][0]['id'] == 1
                and stock['items'][0]['location_ids'] == [1] and s['valid_until'] <= now + 600):
            return None  # First ACTIVE proof: one order/item, Own only, <=10min, ZERO remote writes.
        return s
    except (ValueError, TypeError, OverflowError):
        return None


def observe_canonical_chain(item, products, bindings, mirror, usage, company_id):
    """One physical SKU master; only its exact non-stock catalogue mirror is allowed.

    Catalogue/pricelist metadata is not a second stock master. No arbitrary,
    inactive, foreign-company or commercially-used duplicate is accepted.
    This exception is used ONLY by observe-only ACTIVE, never inventory writes.
    """
    if not isinstance(item, dict) or not isinstance(products, list) or not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(mirror, dict):
        return False
    try:
        binding = bindings[0]
        if (binding['id'] != item['binding_id'] or binding['instance_id'] != 1
                or binding['stock_product_id'] != item['stock_product_id']
                or binding['sku'] != item['sku'] or binding['variant_gid'] != item['variant_gid']
                or binding['inventory_item_gid'] != item['inventory_item_gid']
                or binding['mirror_product_id'] != mirror['id']
                or binding['template_id'] != mirror['template_id']):
            return False
        canonical = [p for p in products if p['id'] == item['stock_product_id']]
        if len(canonical) != 1 or canonical[0]['sku'] != item['sku'] or canonical[0]['active'] is not True or canonical[0]['is_storable'] is not True or canonical[0]['company_id'] not in (None, company_id):
            return False
        if mirror['id'] == item['stock_product_id']:
            return len(products) == 1 and mirror == canonical[0]
        if (mirror['is_storable'] is not False or mirror['company_id'] != company_id
                or mirror['active'] is not True or mirror['sku'] not in ('', item['sku'])):
            return False
        expected = {item['stock_product_id']}
        if mirror['sku'] == item['sku']:
            expected.add(mirror['id'])
        if len(products) != len(expected) or {p['id'] for p in products} != expected:
            return False
        if any(p['sku'] != item['sku'] for p in products):
            return False
        if mirror['id'] in expected and [p for p in products if p['id'] == mirror['id']] != [mirror]:
            return False
        return (isinstance(usage, dict) and set(usage) == set(MIRROR_USAGE_KEYS)
                and all(type(value) is int and value == 0 for value in usage.values()))
    except (KeyError, TypeError, ValueError):
        return False


def identity(scope, kind, binding_id=1, location_id=None):
    if kind == 'orders':
        return 'ith.joint.orders.' + digest(scope)
    if kind == 'stock' and scoped_item(scope['stock'], binding_id, location_id):
        return f'ith.joint.stock.{digest(scope)}.{binding_id}.{location_id}'
    raise ValueError('Unscoped joint job identity')


def job_matches(scope, kind, *, job_id, model, method, channel, record_ids, args, kwargs, identity_key, instance_id):
    if not scope or type(job_id) is not int or job_id <= scope['min_job_id'] or instance_id != 1 or type(instance_id) is not int or kwargs != {}:
        return False
    if kind == 'orders':
        return (model == 'shopify.instance' and method == ORDER_METHOD and channel == ORDER_CHANNEL
                and record_ids == [1] and args in ((), []) and identity_key == identity(scope, 'orders'))
    if kind != 'stock' or model != 'shopify.product.variant' or method != STOCK_METHOD or channel != STOCK_CHANNEL:
        return False
    if not isinstance(record_ids, (list, tuple)) or len(record_ids) != 1 or not isinstance(args, (list, tuple)) or len(args) != 1:
        return False
    item = scoped_item(scope['stock'], record_ids[0], args[0])
    return bool(item and identity_key == identity(scope, 'stock', record_ids[0], args[0]))


def revision_ready(scope, revision, now, blocked_orders=False):
    return bool(not blocked_orders and isinstance(revision, dict)
                and revision.get('ready') is True
                and revision.get('scope_digest') == digest(scope)
                and revision.get('generation') == scope['generation']
                and finite(revision.get('read_started_at'))
                and finite(revision.get('completed_at'))
                and revision['read_started_at'] <= revision['completed_at'] <= now
                and 0 <= now - revision['read_started_at'] <= scope['max_order_age_seconds']
                and now < scope['valid_until'])


class WriteCapability:
    """One native job's one inventory attempt; never serializable admission."""
    __slots__ = ('uuid', 'snapshot', 'binding', 'location', 'revision', 'desired', 'compare', 'used')

    def __init__(self, uuid, snapshot, binding, location, revision, desired, compare):
        if not uuid or not snapshot or type(compare) not in (int, float) or not math.isfinite(compare) or int(compare) != compare:
            raise ValueError('Missing native write identity or integral compare')
        self.uuid, self.snapshot, self.binding, self.location = uuid, snapshot, binding, location
        self.revision, self.desired, self.compare = revision, quantity(desired), int(compare)
        self.used = False

    def consume(self, *, uuid, snapshot, binding, location, revision, desired, compare):
        if self.used or (uuid, snapshot, binding, location, revision, desired, compare) != (
                self.uuid, self.snapshot, self.binding, self.location, self.revision, self.desired, self.compare):
            raise ValueError('Inventory capability changed or already consumed; old-desired CAS retry is forbidden')
        self.used = True  # Even a timeout/compare error consumes the attempt.


class ReadBudget:
    __slots__ = ('snapshot', 'limit', 'used')

    def __init__(self, snapshot, limit):
        if not snapshot or not positive(limit, 16):
            raise ValueError('Invalid native read budget')
        self.snapshot, self.limit, self.used = snapshot, limit, 0

    def consume(self, snapshot):
        if snapshot != self.snapshot or self.used >= self.limit:
            raise ValueError('Native read budget exhausted or changed')
        self.used += 1
