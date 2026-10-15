"""Bounded joint Staging cohort. Native scheduling only, all gates default OFF."""
import json
import logging
import re
import time
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.addons.queue_job.job import Job
from odoo.addons.shopify_connector.graphql.inventory import INVENTORY_SET_QUANTITIES_MUTATION

from .joint_policy import (
    ENABLE_KEY, SCOPE_KEY, CLOSED_KEYS, JOINT_LOCK, SUPPLIER_LOCK, KIND,
    ORDER_CHANNEL, STOCK_CHANNEL, ORDER_METHOD, STOCK_METHOD, ReadBudget,
    joint_scope, encode, digest, identity, job_matches, revision_ready, quantity,
)
from ..shopify_external_query import (
    EXTERNAL_ORDER_QUERY, _issue_fresh_external_order_read_proof,
    calculate_external_quantities,
)

_logger = logging.getLogger(__name__)
JOINT_ORDER_QUERY = EXTERNAL_ORDER_QUERY.replace('cancelledAt', 'updatedAt\n    cancelledAt', 1)
LOCATION_GIDS = {1: 'gid://shopify/Location/103244792194', 2: 'gid://shopify/Location/103676412290'}
OPEN_STATES = ['pending', 'enqueued', 'started', 'wait_dependencies', 'failed']


def epoch(value):
    if not isinstance(value, datetime):
        raise UserError('Native accounting timestamps are missing.')
    return value.replace(tzinfo=timezone.utc).timestamp()


def parsed_datetime(value):
    if not isinstance(value, str):
        raise UserError('Shopify updatedAt is missing.')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise UserError('Shopify updatedAt has no timezone.')
    return result.astimezone(timezone.utc).replace(tzinfo=None)


class GuardedClient:
    """Bounded native read transport; mutation requires a consumed one-shot cap."""
    def __init__(self, raw, queue, snapshot, budget):
        self.raw, self.queue, self.snapshot, self.budget = raw, queue, snapshot, budget
        raw.timeout, raw.max_retries = 20, 0
        self.inventory_sent = False

    def execute(self, query, variables=None, **kwargs):
        self.queue._ith_joint_assert(self.snapshot)
        if not isinstance(query, str) or re.search(r'\bmutation\b', query):
            raise UserError('Joint generic transport is read-only.')
        self.budget.consume(self.snapshot)
        result = self.raw.execute(query, variables, **kwargs)
        self.queue._ith_joint_assert(self.snapshot)
        return result

    def inventory_once(self, query, variables, capability):
        snapshot = self.queue._ith_joint_guard('stock')
        if (snapshot != self.snapshot or query != INVENTORY_SET_QUANTITIES_MUTATION
                or not capability.used or capability.snapshot != snapshot
                or capability.uuid != self.queue.env.context.get('job_uuid') or self.inventory_sent):
            raise UserError('Only one admitted inventory mutation is allowed.')
        self.inventory_sent = True  # Errors and timeouts never reopen this client.
        result = self.raw.execute(query, variables)
        self.queue._ith_joint_assert(self.snapshot)
        return result


class ITHJointRevision(models.Model):
    _name = 'ith.joint.revision'
    _description = 'ITH Joint Cohort Accounting Revision'
    _order = 'id desc'

    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one('res.company', required=True, ondelete='restrict', index=True)
    scope_digest = fields.Char(required=True, index=True, readonly=True)
    generation = fields.Char(required=True, readonly=True)
    order_digest = fields.Char(required=True, readonly=True)
    read_started_at = fields.Datetime(required=True, readonly=True)
    completed_at = fields.Datetime(required=True, readonly=True)
    ready = fields.Boolean(required=True, default=False, readonly=True)
    blocked_reason = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        scope = json.loads(self.env['queue.job']._ith_joint_guard('orders'))
        for vals in vals_list:
            if (vals.get('instance_id') != 1 or vals.get('company_id') != scope['company_id']
                    or vals.get('scope_digest') != digest(scope) or vals.get('generation') != scope['generation']):
                raise UserError('Unscoped accounting revision.')
        return super().create(vals_list)

    def write(self, values):
        raise UserError('Accounting revisions are immutable; create a new complete revision.')

    def unlink(self):
        raise UserError('Accounting revision evidence cannot be deleted.')


class ITHJointQueue(models.Model):
    _inherit = 'queue.job'

    @api.model
    def _ith_joint_parameters(self):
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
            cr.execute('SELECT key,value FROM ir_config_parameter WHERE key=ANY(%s)', ([ENABLE_KEY, SCOPE_KEY, *CLOSED_KEYS],))
            return dict(cr.fetchall())

    @api.model
    def _ith_joint_requested(self):
        try:
            return self._ith_joint_parameters().get(ENABLE_KEY, 'False') == 'True'
        except Exception:
            _logger.exception('Joint controls unavailable; export path fails closed')
            return True

    @api.model
    def _ith_joint_scope(self):
        try:
            parameters = self._ith_joint_parameters()
            scope = joint_scope(parameters.get(ENABLE_KEY, 'False'), parameters.get(SCOPE_KEY, '{}'), self.env.cr.dbname, time.time())
            if not scope or any(parameters.get(key, 'False') != 'False' for key in CLOSED_KEYS):
                return None
            # Fresh control/config records, not an old repeatable-read job cache.
            with self.env.registry.cursor() as cr:
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
                env = api.Environment(cr, self.env.uid, dict(self.env.context))
                instance = env['shopify.instance'].sudo().browse(1).exists()
                cron = env.ref('ith_shopify_custom.cron_ith_joint_policy', raise_if_not_found=False)
                old = env['ir.cron'].sudo().with_context(active_test=False).browse([53, 56, 58, 59]).exists()
                others = env['ir.cron'].sudo().with_context(active_test=False).search([
                    ('active', '=', True), ('queue_job_runner', '=', True), ('id', '!=', cron.id if cron else 0)], limit=1)
                if not (instance and instance.active and instance.state == 'connected'
                        and instance.shop_url == 'uitpac-aj.myshopify.com'
                        and instance.company_id.id == scope['company_id']
                        and instance.inventory_quantity_basis == 'free_qty'
                        and not instance.inventory_import_enabled and not instance.inventory_allow_negative
                        and instance.order_confirmation_policy == 'quotation' and instance.order_invoicing_policy == 'manual'
                        and not instance.fulfillment_auto_validate_webhook
                        and cron and cron.active and cron.queue_job_runner and cron.model_id.model == 'queue.job'
                        and cron.code.strip() == 'model._ith_joint_tick()'
                        and cron.interval_number == 10 and cron.interval_type == 'minutes'
                        and len(old) == 4 and not any(c.active for c in old) and not others):
                    return None
                for loc in scope['stock']['locations']:
                    record = env['shopify.location'].sudo().browse(loc['id']).exists()
                    if not (record and record.active and record.instance_id == instance
                            and record.shopify_id == loc['gid'] and record.use_supplier_stock == loc['supplier']
                            and record.odoo_location_id.id == loc['odoo_location_id']
                            and record.odoo_location_id.usage == 'internal'
                            and (not record.odoo_location_id.company_id or record.odoo_location_id.company_id == instance.company_id)):
                        return None
            return scope
        except Exception:
            _logger.exception('Joint scope/configuration unavailable; native graph is closed')
            return None

    @api.model
    def _ith_joint_record_matches(self, record, scope, kind):
        job = Job.load(self.env, record.uuid)
        refs = job.recordset
        if len(refs) != 1 or not refs.exists():
            return False
        instance_id = refs.id if refs._name == 'shopify.instance' else refs.instance_id.id
        return job_matches(scope, kind, job_id=record.id, model=refs._name, method=job.method_name,
                           channel=job.channel, record_ids=refs.ids, args=job.args, kwargs=job.kwargs,
                           identity_key=record.identity_key, instance_id=instance_id)

    @api.model
    def _ith_joint_guard(self, kind):
        scope = self._ith_joint_scope()
        uuid = self.env.context.get('job_uuid')
        if not scope or not uuid or kind not in ('orders', 'stock'):
            raise UserError('The admitted joint native window is closed.')
        record = self.sudo().search([('uuid', '=', uuid), ('state', '=', 'started'), ('id', '>', scope['min_job_id'])], limit=1)
        if not record or not self._ith_joint_record_matches(record, scope, kind):
            raise UserError('Only the exact newly admitted joint native job is allowed.')
        return encode(scope)

    @api.model
    def _ith_joint_assert(self, snapshot):
        scope = self._ith_joint_scope()
        if not scope or snapshot != encode(scope):
            raise UserError('Joint controls expired or changed; never fall back to legacy execution.')
        uuid = self.env.context.get('job_uuid')
        record = self.sudo().search([('uuid', '=', uuid), ('state', '=', 'started'), ('id', '>', scope['min_job_id'])], limit=1)
        if not uuid or not record or not any(self._ith_joint_record_matches(record, scope, kind) for kind in ('orders', 'stock')):
            raise UserError('Joint snapshot has no exact native started job.')
        return scope

    @api.model
    def _ith_joint_lock(self):
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (JOINT_LOCK,))

    @api.model
    def _ith_joint_revision(self, scope, allow_pending=False):
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            revision = env['ith.joint.revision'].sudo().search([('scope_digest', '=', digest(scope)),
                        ('company_id', '=', scope['company_id']), ('instance_id', '=', 1)], limit=1)
            blocked = env['queue.job'].sudo().search_count([
                ('id', '>', scope['min_job_id']), ('channel', '=', ORDER_CHANNEL),
                ('identity_key', '=', identity(scope, 'orders')), ('state', 'in', OPEN_STATES)])
            data = None if not revision else {'id': revision.id, 'scope_digest': revision.scope_digest,
                    'generation': revision.generation, 'read_started_at': epoch(revision.read_started_at),
                    'completed_at': epoch(revision.completed_at), 'order_digest': revision.order_digest,
                    'ready': revision.ready}
        return data if data and data['ready'] and revision_ready(scope, data, time.time(), bool(blocked) and not allow_pending) else None

    @api.model
    def _ith_joint_stock_barrier(self, snapshot, binding_id, location_id):
        scope = self._ith_joint_assert(snapshot)
        if self._ith_joint_guard('stock') != snapshot:
            raise UserError('Missing native stock job.')
        job = Job.load(self.env, self.env.context.get('job_uuid'))
        if job.recordset.id != binding_id or job.args not in ((location_id,), [location_id]):
            raise UserError('Stock record/location differ from the exact admitted native job.')
        self._ith_joint_lock()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (SUPPLIER_LOCK,))
        revision = self._ith_joint_revision(scope)
        if not revision:
            raise UserError('Selected orders/reservations are stale, incomplete, pending or failed; stock publication is blocked.')
        return scope, revision

    @api.model
    def _ith_stock_requested(self):
        return self._ith_joint_requested() or super()._ith_stock_requested()

    @api.model
    def _ith_guard_order_job_entry(self):
        if self.env.context.get('ith_joint_snapshot') is not None or self._ith_joint_requested():
            return self._ith_joint_guard('orders')
        return super()._ith_guard_order_job_entry()

    @api.model
    def _ith_assert_order_scope_snapshot(self, snapshot):
        if self.env.context.get('ith_joint_snapshot') is not None or self._ith_joint_requested():
            if self._ith_joint_guard('orders') != snapshot:
                raise UserError('C1 bridge lost the exact joint order admission.')
            return self._ith_joint_assert(snapshot)
        return super()._ith_assert_order_scope_snapshot(snapshot)

    @api.model
    def _ith_joint_admit(self, scope, kind, binding_id=1, location_id=None):
        self._ith_joint_lock()
        if encode(scope) != encode(self._ith_joint_scope()):
            return False
        key = identity(scope, kind, binding_id, location_id)
        if kind == 'orders':
            with self.env.registry.cursor() as cr:
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
                cr.execute('SELECT ready FROM ith_joint_revision WHERE scope_digest=%s ORDER BY id DESC LIMIT 1', (digest(scope),))
                last = cr.fetchone()
            if last and last[0] is False:
                return False  # A visible hold needs operator resolution/new approved generation.
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (key,))
        existing = self.sudo().search([('identity_key', '=', key), ('state', 'in', OPEN_STATES)], limit=1)
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
            cr.execute('SELECT EXISTS(SELECT 1 FROM queue_job WHERE identity_key=%s AND state=ANY(%s))', (key, OPEN_STATES))
            committed_exists = cr.fetchone()[0]
        if existing or committed_exists:
            return False  # Failed needs operator review; no retry storm.
        if encode(scope) != encode(self._ith_joint_scope()):
            return False
        if kind == 'orders':
            self.env['shopify.instance'].sudo().browse(1).with_delay(channel=ORDER_CHANNEL,
                    identity_key=key, priority=5, description='ITH admitted joint cohort orders/reservations')._ith_joint_refresh_orders()
        else:
            variant = self.env['shopify.product.variant'].sudo().browse(binding_id).exists()
            self._ith_stock_validate_binding(scope['stock'], variant, location_id)
            variant.with_delay(channel=STOCK_CHANNEL, identity_key=key, priority=20,
                    description='ITH admitted joint availability publication')._ith_joint_push_inventory(location_id)
        return True

    @api.model
    def _ith_stock_enqueue_products(self, product_ids=None, supplier_skus=None):
        if not self._ith_joint_requested():
            return super()._ith_stock_enqueue_products(product_ids=product_ids, supplier_skus=supplier_skus)
        scope = self._ith_joint_scope()
        if not scope:
            return 0
        count = 0
        for item in scope['stock']['items']:
            if product_ids is not None and item['stock_product_id'] not in product_ids:
                continue
            if supplier_skus is not None and item['sku'] not in supplier_skus:
                continue
            for location in item['location_ids']:
                if (supplier_skus is None and location != 1) or (supplier_skus is not None and location != 2):
                    continue
                try:
                    with self.env.cr.savepoint():
                        count += bool(self._ith_joint_admit(scope, 'stock', item['binding_id'], location))
                except (UserError, ValueError):
                    _logger.exception('Joint warehouse/supplier admission blocked; physical update remains separate')
        return count

    @api.model
    def _ith_joint_cron_scope(self):
        scope = self._ith_joint_scope()
        cron = self.env.ref('ith_shopify_custom.cron_ith_joint_policy', raise_if_not_found=False)
        return scope if scope and cron and self.env.context.get('cron_id') == cron.id else None

    @api.model
    def _ith_joint_acquire(self, scope, kind):
        channel = ORDER_CHANNEL if kind == 'orders' else STOCK_CHANNEL
        method = ORDER_METHOD if kind == 'orders' else STOCK_METHOD
        model = 'shopify.instance' if kind == 'orders' else 'shopify.product.variant'
        pattern = identity(scope, 'orders') if kind == 'orders' else f'ith.joint.stock.{digest(scope)}.%'
        self.env.flush_all()
        self.env.cr.execute('''SELECT id FROM queue_job WHERE id>%s AND state='pending'
            AND channel=%s AND method_name=%s AND model_name=%s AND identity_key LIKE %s
            AND (eta IS NULL OR eta<=(now() AT TIME ZONE 'UTC'))
            ORDER BY priority,date_created,id LIMIT 100 FOR NO KEY UPDATE SKIP LOCKED''',
            (scope['min_job_id'], channel, method, model, pattern))
        for (identifier,) in self.env.cr.fetchall():
            record = self.sudo().browse(identifier)
            if self._ith_joint_record_matches(record, scope, kind):
                return record
        return self.browse()

    @api.model
    def _ith_joint_process(self, scope, kind, limit, progress):
        count = 0
        for _ in range(limit):
            if encode(scope) != encode(self._ith_joint_cron_scope()):
                break
            record = self._ith_joint_acquire(scope, kind)
            if not record:
                break
            if encode(scope) != encode(self._ith_joint_cron_scope()) or not self._ith_joint_record_matches(record, scope, kind):
                break
            record._process(commit=False)  # Installed native OCA lifecycle, not copied.
            count += 1
            if not progress._commit_progress(1):
                return count, False
        return count, True

    @api.model
    def _ith_joint_tick(self):
        scope = self._ith_joint_cron_scope()
        if not scope:
            return False
        progress = self.env['ir.cron']
        if not progress._commit_progress():
            return False
        revision = self._ith_joint_revision(scope, allow_pending=True)
        if not revision or time.time() - revision['read_started_at'] >= scope['order_refresh_seconds']:
            self._ith_joint_admit(scope, 'orders')
            if not progress._commit_progress(1):
                return False
        orders, budget = self._ith_joint_process(scope, 'orders', 1, progress)
        if not budget or not self._ith_joint_revision(scope):
            return {'orders': orders, 'stock': 0, 'blocked': True}
        cursor_key = f'ith.joint.cursor.{digest(scope)}'
        params = self.env['ir.config_parameter'].sudo()
        try:
            start = int(params.get_param(cursor_key, '0')) % len(scope['stock']['items'])
        except (TypeError, ValueError):
            start = 0
        for offset in range(min(scope['stock']['scan_limit'], len(scope['stock']['items']))):
            if encode(scope) != encode(self._ith_joint_cron_scope()) or not self._ith_joint_revision(scope):
                break
            item = scope['stock']['items'][(start + offset) % len(scope['stock']['items'])]
            for lid in item['location_ids']:
                try:
                    with self.env.cr.savepoint():
                        self._ith_joint_admit(scope, 'stock', item['binding_id'], lid)
                except (UserError, ValueError):
                    _logger.exception('Joint scoped stock admission skipped')
            params.set_param(cursor_key, str((start + offset + 1) % len(scope['stock']['items'])))
            if not progress._commit_progress(1):
                return {'orders': orders, 'stock': 0}
        stock, _ = self._ith_joint_process(scope, 'stock', scope['job_limit'] - orders, progress)
        return {'orders': orders, 'stock': stock, 'blocked': False}


class ITHJointInstance(models.Model):
    _inherit = 'shopify.instance'

    def _shopify_client(self):
        raw = super()._shopify_client()
        snapshot = self.env.context.get('ith_joint_snapshot')
        if snapshot is None:
            return raw
        queue = self.env['queue.job']
        scope = queue._ith_joint_assert(snapshot)
        budget = self.env.context.get('ith_joint_read_budget')
        if not isinstance(budget, ReadBudget) or budget.snapshot != snapshot or budget.limit != scope['api_read_limit']:
            raise UserError('No shared bounded native transport budget.')
        return GuardedClient(raw, queue, snapshot, budget)

    def _ith_joint_refresh_orders(self):
        self = self.sudo()
        self.ensure_one()
        queue = self.env['queue.job']
        snapshot = queue._ith_joint_guard('orders')
        scope = queue._ith_joint_assert(snapshot)
        queue._ith_joint_lock()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (SUPPLIER_LOCK,))
        bound = self.with_context(ith_joint_snapshot=snapshot, ith_joint_read_budget=ReadBudget(snapshot, scope['api_read_limit']))
        started = datetime.utcnow()
        fingerprints = []
        imported = []
        for oid in scope['order_ids']:
            queue._ith_joint_assert(snapshot)
            Order = bound.env['shopify.order'].sudo()
            identifier = Order._job_import_order_from_api(1, oid)
            order = Order.browse(identifier).exists() if type(identifier) is int else Order.browse()
            if not (order and order.shopify_id == oid and order.instance_id.id == 1 and order.state == 'synced'
                    and order.odoo_id and not order.ith_mapping_pending and order.source != 'pos' and not order.is_draft):
                queue._ith_joint_assert(snapshot)
                self.env['ith.joint.revision'].sudo().create({
                    'instance_id': 1, 'company_id': scope['company_id'], 'scope_digest': digest(scope),
                    'generation': scope['generation'], 'order_digest': digest({'blocked_order': oid}),
                    'read_started_at': started, 'completed_at': datetime.utcnow(), 'ready': False,
                    'blocked_reason': 'C1 import/mapping hold at ' + oid,
                })
                return {'blocked': True, 'order': oid}  # Preserve pending/error quotation; no ledger updates yet.
            imported.append(order)
        for order in imported:
            oid = order.shopify_id
            read_started = datetime.utcnow()
            payload = bound._shopify_client().execute(JOINT_ORDER_QUERY, {'id': oid}).get('order')
            queue._ith_joint_assert(snapshot)
            proof = _issue_fresh_external_order_read_proof(1, oid, payload, read_started_at=read_started)
            if order.external_updated_at != parsed_datetime(payload.get('updatedAt')):
                raise UserError('Order changed between import and allocation read; fresh reconciliation required.')
            self._ith_joint_validate_allocation_scope(order, payload, scope)
            order._ith_reconcile_external_snapshot(payload, _release_proof=proof)
            order._ith_joint_apply_own_snapshot(payload, proof, snapshot)
            queue._ith_joint_assert(snapshot)
            fingerprints.append({'id': oid, 'updatedAt': payload['updatedAt'], 'payload': payload})
        queue._ith_joint_assert(snapshot)
        if time.time() - epoch(started) > scope['max_order_age_seconds']:
            raise UserError('Cohort read aged out before complete accounting commit.')
        self.env['ith.joint.revision'].sudo().create({
            'instance_id': 1, 'company_id': scope['company_id'], 'scope_digest': digest(scope),
            'generation': scope['generation'], 'order_digest': digest(fingerprints),
            'read_started_at': started, 'completed_at': datetime.utcnow(), 'ready': True,
        })
        return len(fingerprints)

    def _ith_joint_validate_allocation_scope(self, order, payload, scope):
        known = set(LOCATION_GIDS.values())
        items = {item['stock_product_id']: item for item in scope['stock']['items']}
        for lid, gid in LOCATION_GIDS.items():
            targets = calculate_external_quantities(payload, {gid}, known)
            for line_id in targets:
                lines = order.line_binding_ids.filtered(lambda line: line.shopify_id == line_id)
                if len(lines) != 1 or not lines.odoo_id or not lines.odoo_id.product_id:
                    raise UserError('Allocation has no single canonical order line.')
                product = lines.odoo_id.product_id
                item = items.get(product.id)
                if not item or product.default_code != item['sku'] or lid not in item['location_ids']:
                    raise UserError('Allocation/shipment is outside exact SKU/location scope.')
                self.env['queue.job']._ith_stock_validate_binding(scope['stock'], self.env['shopify.product.variant'].sudo().browse(item['binding_id']), lid)


class ITHJointOrderSafety(models.Model):
    _inherit = 'shopify.order'

    def _ith_reconcile_external_snapshot(self, payload, *, _release_proof=None):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super()._ith_reconcile_external_snapshot(payload, _release_proof=_release_proof)

    def _ith_reconcile_external_stock(self):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super()._ith_reconcile_external_stock()

    def _ith_mapping_enabled(self, instance):
        snapshot = self.env.context.get('ith_joint_snapshot')
        if snapshot is not None:
            self.env['queue.job']._ith_joint_assert(snapshot)
            return instance.id == 1
        return super()._ith_mapping_enabled(instance)

    def _apply_invoice_and_payments(self, binding, order_data):
        if self.env.context.get('ith_joint_snapshot') is not None:
            self.env['queue.job']._ith_joint_assert(self.env.context['ith_joint_snapshot'])
            return None  # Preserve raw financial data; never create/post records in the cohort.
        return super()._apply_invoice_and_payments(binding, order_data)

    def _apply_returns(self, binding, order_data):
        if self.env.context.get('ith_joint_snapshot') is not None:
            self.env['queue.job']._ith_joint_assert(self.env.context['ith_joint_snapshot'])
            return None  # No automatic restocking/picking/procurement.
        return super()._apply_returns(binding, order_data)

    def _apply_refunds(self, binding, order_data):
        if self.env.context.get('ith_joint_snapshot') is not None:
            self.env['queue.job']._ith_joint_assert(self.env.context['ith_joint_snapshot'])
            return None
        return super()._apply_refunds(binding, order_data)


class ITHJointQuantLock(models.Model):
    _inherit = 'stock.quant'

    @api.model_create_multi
    def create(self, vals_list):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super().create(vals_list)

    def write(self, values):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super().write(values)

    def unlink(self):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super().unlink()


class ITHJointSupplierLock(models.Model):
    _inherit = 'ith.external.stock'

    def reconcile_order_quantity(self, *args, **kwargs):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super().reconcile_order_quantity(*args, **kwargs)

    def replace_supplier_snapshot(self, *args, **kwargs):
        if self.env['queue.job']._ith_joint_requested():
            self.env['queue.job']._ith_joint_lock()
        return super().replace_supplier_snapshot(*args, **kwargs)
