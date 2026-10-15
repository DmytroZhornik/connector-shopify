"""First joint native cohort contract: Staging, DRAFT-only, never activation."""
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


def encode(scope):
    return json.dumps(scope, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(scope):
    return hashlib.sha256(encode(scope).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def positive(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def joint_scope(enabled, raw, database, now):
    if enabled != 'True' or not isinstance(raw, str) or not finite(now):
        return None
    try:
        s = json.loads(raw)
        keys = {'kind', 'database', 'generation', 'instance_id', 'company_id',
                'min_job_id', 'valid_until', 'order_ids', 'order_refresh_seconds',
                'max_order_age_seconds', 'job_limit', 'api_read_limit', 'stock'}
        if not isinstance(s, dict) or set(s) != keys or s['kind'] != KIND:
            return None
        stock = stock_scope('True', encode(s['stock']), database)
        if not stock or stock['product_statuses'] != ['DRAFT'] or stock['quantity_basis'] != 'free_qty':
            return None  # This first cohort is NOT a live-store discovery policy.
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
        return s
    except (ValueError, TypeError, OverflowError):
        return None


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
