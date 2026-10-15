"""Fresh joint availability and ONE CAS attempt; never call legacy push/retry."""
import math

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.addons.shopify_connector.graphql.inventory import INVENTORY_SET_QUANTITIES_MUTATION
from odoo.addons.shopify_connector.lib.inventory import build_inventory_set_payload

from .joint_policy import WriteCapability, ReadBudget, quantity
from .stock_dispatch_policy import live_level
from .stock_dispatch import LIVE_QUERY
from .joint_dispatch import GuardedClient


class ITHJointInventory(models.Model):
    _inherit = 'shopify.product.variant'

    def _ith_joint_validate_subject(self, scope, location_id):
        # A new environment/cursor also makes canonical mapping/state fresh.
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            variant = env['shopify.product.variant'].sudo().browse(self.id).exists()
            return env['queue.job']._ith_stock_validate_binding(scope['stock'], variant, location_id)

    def _ith_joint_desired(self, snapshot, location_id, revision_id):
        self.ensure_one()
        queue = self.env['queue.job']
        scope, revision = queue._ith_joint_stock_barrier(snapshot, self.id, location_id)
        if revision['id'] != revision_id:
            raise UserError('Accounting revision changed during inventory publication.')
        item, loc = self._ith_joint_validate_subject(scope, location_id)
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY')
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            if loc['supplier']:
                _, version, available = env['queue.job']._ith_stock_fresh_supplier(item['sku'])
                if version != loc['snapshot_version']:
                    raise UserError('Supplier snapshot revision changed.')
                desired = quantity(available)
            else:
                product = env['product.product'].sudo().browse(item['stock_product_id']).with_company(scope['company_id'])
                free = product.with_context(location=5).free_qty
                if type(free) not in (int, float) or not math.isfinite(free) or int(free) != free:
                    raise UserError('Own physical free quantity is not a finite integer.')
                extra = env['ith.own.commitment']._ith_joint_extra_for_product(product.id, scope)
                desired = quantity(max(0, int(free) - extra))
        queue._ith_joint_assert(snapshot)
        return desired

    def _ith_joint_live_read(self, client, snapshot, location_id, revision_id):
        queue = self.env['queue.job']
        scope, revision = queue._ith_joint_stock_barrier(snapshot, self.id, location_id)
        if revision['id'] != revision_id:
            raise UserError('Accounting revision changed before inventory read.')
        item, loc = self._ith_joint_validate_subject(scope, location_id)
        data = client.execute(LIVE_QUERY, {'id': item['variant_gid'], 'location': loc['gid']})
        queue._ith_joint_assert(snapshot)
        try:
            return live_level(data.get('productVariant'), item, loc, scope['stock']['product_statuses'])
        except (ValueError, TypeError, AttributeError) as error:
            raise UserError('Inventory identity/status/tracking/available changed or is incomplete.') from error

    def _ith_joint_push_inventory(self, location_id):
        self = self.sudo()
        self.ensure_one()
        queue = self.env['queue.job']
        snapshot = queue._ith_joint_guard('stock')
        scope, revision = queue._ith_joint_stock_barrier(snapshot, self.id, location_id)
        bound = self.with_context(ith_joint_snapshot=snapshot,
                                  ith_joint_read_budget=ReadBudget(snapshot, scope['api_read_limit']))
        client = bound.instance_id._shopify_client()
        remote = bound._ith_joint_live_read(client, snapshot, location_id, revision['id'])
        desired = bound._ith_joint_desired(snapshot, location_id, revision['id'])
        location = bound.env['shopify.location'].sudo().browse(location_id)
        changed = desired != remote
        if changed:
            capability = WriteCapability(bound.env.context.get('job_uuid'), snapshot, bound.id,
                                         location_id, revision['id'], desired, remote)
            guarded = bound.with_context(ith_joint_write_capability=capability)
            guarded._execute_inventory_set(client, location, desired, remote)
            # Compare/timeout failures propagate to native OCA. NO blind refresh.
        actual = bound._ith_joint_live_read(client, snapshot, location_id, revision['id'])
        if actual != desired:
            raise UserError('Joint inventory readback mismatch; manual reconciliation required, no automatic retry.')
        if bound._ith_joint_desired(snapshot, location_id, revision['id']) != desired:
            raise UserError('Availability changed before final accounting readback.')
        queue._ith_joint_assert(snapshot)
        state = bound._inventory_state(location)
        now = fields.Datetime.now()
        values = {'last_shopify_quantity': desired, 'last_sync_date': now}
        if changed:
            values.update({'last_pushed_quantity': desired, 'last_pushed_at': now})
        state.write(values)
        bound.instance_id._write_log(entity='inventory', direction='export', level='info', record=bound,
            message='ITH joint native availability %s -> %s; accounting revision %s; %s' % (
                remote, desired, revision['id'], 'one CAS/readback' if changed else 'no change/no mutation'))
        return {'changed': changed, 'quantity': desired, 'revision': revision['id']}

    def _execute_inventory_set(self, client, location_binding, desired, compare_quantity):
        snapshot = self.env.context.get('ith_joint_snapshot')
        if snapshot is None and not self.env['queue.job']._ith_joint_requested():
            return super()._execute_inventory_set(client, location_binding, desired, compare_quantity)
        if not snapshot:
            raise UserError('Inventory writes require the admitted joint native job.')
        queue = self.env['queue.job']
        scope, revision = queue._ith_joint_stock_barrier(snapshot, self.id, location_binding.id)
        capability = self.env.context.get('ith_joint_write_capability')
        if not isinstance(capability, WriteCapability) or not isinstance(client, GuardedClient):
            raise UserError('No one-shot native inventory capability/transport.')
        if self._ith_joint_desired(snapshot, location_binding.id, revision['id']) != desired:
            raise UserError('Old desired quantity cannot be republished.')
        live = self._ith_joint_live_read(client, snapshot, location_binding.id, revision['id'])
        if live != compare_quantity:
            raise UserError('Shopify available changed; stop instead of retrying old desired with new compare.')
        try:
            capability.consume(uuid=self.env.context.get('job_uuid'), snapshot=snapshot, binding=self.id,
                location=location_binding.id, revision=revision['id'], desired=desired, compare=compare_quantity)
        except ValueError as error:
            raise UserError(str(error)) from error
        item, loc = self._ith_joint_validate_subject(scope, location_binding.id)
        reference = f'odoo://ith-joint/{self.env.cr.dbname}/{revision["id"]}/{self.id}/{location_binding.id}'
        payload = build_inventory_set_payload(item['inventory_item_gid'], loc['gid'], desired,
                                              compare_quantity, reference_document_uri=reference)
        queue._ith_joint_assert(snapshot)
        return client.inventory_once(INVENTORY_SET_QUANTITIES_MUTATION, {'input': payload}, capability)

    def _read_shopify_inventory_quantity(self, client, location_binding):
        snapshot = self.env.context.get('ith_joint_snapshot')
        if snapshot is None and not self.env['queue.job']._ith_joint_requested():
            return super()._read_shopify_inventory_quantity(client, location_binding)
        if not snapshot:
            raise UserError('Inventory reads require the admitted joint native stock job.')
        _, revision = self.env['queue.job']._ith_joint_stock_barrier(snapshot, self.id, location_binding.id)
        return self._ith_joint_live_read(client, snapshot, location_binding.id, revision['id'])

    def _odoo_inventory_quantity(self, location_binding, *, apply_export_policy=True):
        snapshot = self.env.context.get('ith_joint_snapshot')
        if snapshot is None and not self.env['queue.job']._ith_joint_requested():
            return super()._odoo_inventory_quantity(location_binding, apply_export_policy=apply_export_policy)
        if not snapshot:
            raise UserError('Availability calculation requires the admitted joint native stock job.')
        _, revision = self.env['queue.job']._ith_joint_stock_barrier(snapshot, self.id, location_binding.id)
        return self._ith_joint_desired(snapshot, location_binding.id, revision['id'])
