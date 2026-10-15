"""Bounded Staging Own acceptance through the installed native cron runner."""
import json
import time

from odoo import api, models
from odoo.exceptions import UserError
from odoo.addons.queue_job.job import Job

from .own_stock_policy import own_stock_scope, own_job_matches


class ITHOwnStockQueue(models.Model):
    _inherit = "queue.job"

    @api.model
    def _ith_own_stock_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param("ith.own.stock.queue.enabled", "False") == "True"

    @api.model
    def _ith_own_stock_scope(self):
        p = self.env["ir.config_parameter"].sudo()
        return own_stock_scope(p.get_param("ith.own.stock.queue.enabled", "False"),
                               p.get_param("ith.own.stock.queue.scope", "{}"), time.time())

    @api.model
    def _ith_own_stock_safe_scope(self):
        scope = self._ith_own_stock_scope()
        if not scope:
            return None
        p = self.env["ir.config_parameter"].sudo()
        if any(p.get_param(k, "False") == "True" for k in (
            "ith.orders.queue.enabled", "ith.external.queue.enabled", "ith.external.shopify.auto_sync"
        )):
            return None
        instance = self.env["shopify.instance"].sudo().browse(1).exists()
        location = self.env["shopify.location"].sudo().browse(scope["location_id"]).exists()
        other_crons = self.env["ir.cron"].sudo().with_context(active_test=False).browse([53, 58])
        own_cron = self.env["ir.cron"].sudo().with_context(active_test=False).browse(56).exists()
        if not (instance and instance.active and instance.state == "connected"
                and instance.shop_url == "uitpac-aj.myshopify.com" and not instance.inventory_import_enabled
                and location and location.instance_id == instance and location.active
                and location.shopify_id == scope["location_gid"] and not location.use_supplier_stock
                and location.odoo_location_id.usage == "internal"
                and location.odoo_location_id.id == scope["odoo_location_id"]
                and not any(c.active for c in other_crons)
                and own_cron and own_cron.active and own_cron.queue_job_runner
                and own_cron.model_id.model == "queue.job" and own_cron.code.strip() == "model._job_runner()"):
            return None
        return scope

    @api.model
    def _ith_own_stock_cron_scope(self):
        scope = self._ith_own_stock_safe_scope()
        if not scope or self.env.context.get("cron_id") != scope["cron_id"]:
            return None
        cron = self.env["ir.cron"].sudo().with_context(active_test=False).browse(scope["cron_id"]).exists()
        if not (cron and cron.active and cron.queue_job_runner
                and cron.model_id.model == "queue.job" and cron.code.strip() == "model._job_runner()"):
            return None
        return scope

    @api.model
    def _ith_own_stock_job_matches(self, record, scope):
        job = Job.load(self.env, record.uuid)
        refs = job.recordset
        return bool(len(refs) == 1 and refs.exists() and refs._name == "shopify.product.variant"
                    and own_job_matches(scope, job_id=record.id, model=refs._name, method=job.method_name,
                        channel=job.channel, args=job.args, kwargs=job.kwargs, identity_key=job.identity_key,
                        record_ids=refs.ids, instance_id=refs.instance_id.id))

    @api.model
    def _job_runner(self, commit=True):
        if "staging" not in self.env.cr.dbname.lower() or not self._ith_own_stock_enabled():
            return super()._job_runner(commit=commit)
        if not self._ith_own_stock_cron_scope():
            return False
        progress = self.env["ir.cron"]
        if not progress._commit_progress():
            return False
        processed = False
        for _ in range(5):
            record = self._acquire_one_job()
            if not record:
                progress._commit_progress(remaining=0)
                break
            scope = self._ith_own_stock_cron_scope()
            if not scope or not self._ith_own_stock_job_matches(record, scope):
                break
            # Existing OCA _process owns execution, errors, retry and dependencies.
            record._process(commit=False)
            processed = True
            if not progress._commit_progress(1):
                break
        return processed

    @api.model
    def _acquire_one_job(self):
        if "staging" not in self.env.cr.dbname.lower() or not self._ith_own_stock_enabled():
            return super()._acquire_one_job()
        scope = self._ith_own_stock_cron_scope()
        if not scope:
            return self.browse()
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id FROM queue_job
            WHERE id = ANY(%s::int[]) AND state = 'pending'
              AND channel = 'root.shopify.export'
              AND model_name = 'shopify.product.variant'
              AND method_name = '_job_push_inventory_level'
              AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
            ORDER BY priority, date_created, id
            LIMIT 5 FOR NO KEY UPDATE SKIP LOCKED
        """, ([item["job_id"] for item in scope["items"]],))
        for (identifier,) in self.env.cr.fetchall():
            record = self.sudo().browse(identifier)
            if self._ith_own_stock_job_matches(record, scope):
                return record
        return self.browse()

    @api.model
    def _ith_guard_own_stock_entry(self, variant, location_id, snapshot=None):
        scope = self._ith_own_stock_safe_scope()
        uuid = self.env.context.get("job_uuid")
        if not scope or not uuid:
            raise UserError(self.env._("The bounded Own stock execution window is closed."))
        encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"))
        if snapshot is not None and snapshot != encoded:
            raise UserError(self.env._("The Own stock execution scope changed; export stopped."))
        record = self.sudo().search([("uuid", "=", uuid), ("state", "=", "started")], limit=1)
        item = next((item for item in scope["items"] if item["binding_id"] == variant.id), None)
        if not (record and item and record.id == item["job_id"]
                and self._ith_own_stock_job_matches(record, scope)
                and type(location_id) is int and location_id == scope["location_id"]
                and variant.stock_product_id.id == item["stock_product_id"]
                and variant.stock_product_id.default_code == item["sku"] == variant.sku_snapshot
                and variant.shopify_id == item["variant_gid"]
                and variant.inventory_item_shopify_id == item["inventory_item_gid"]
                and variant._inventory_sync_eligible()):
            raise UserError(self.env._("This inventory job or canonical SKU is outside the exact Own stock scope."))
        return encoded


class ITHOwnStockVariantGuard(models.Model):
    _inherit = "shopify.product.variant"

    def _job_push_inventory_level(self, location_binding_id):
        self.ensure_one()
        location = self.env["shopify.location"].sudo().browse(location_binding_id).exists()
        if "staging" not in self.env.cr.dbname.lower() or (location and location.use_supplier_stock):
            return super()._job_push_inventory_level(location_binding_id)
        snapshot = self.env["queue.job"]._ith_guard_own_stock_entry(self, location_binding_id)
        return super(ITHOwnStockVariantGuard, self.with_context(ith_own_stock_scope_snapshot=snapshot))._job_push_inventory_level(location_binding_id)

    def _execute_inventory_set(self, client, location_binding, desired, compare_quantity):
        operational_snapshot = self.env.context.get("ith_stock_policy_snapshot")
        if operational_snapshot is not None:
            self.env["queue.job"]._ith_stock_guard_entry(
                self, location_binding.id, snapshot=operational_snapshot,
            )
            return super()._execute_inventory_set(client, location_binding, desired, compare_quantity)
        if "staging" in self.env.cr.dbname.lower() and not location_binding.use_supplier_stock:
            snapshot = self.env.context.get("ith_own_stock_scope_snapshot")
            if not snapshot:
                raise UserError(self.env._("Own inventory mutation requires the native bounded job context."))
            queue = self.env["queue.job"]
            queue._ith_guard_own_stock_entry(self, location_binding.id, snapshot=snapshot)
            data = client.execute(
                "query ITHOwnDraftWriteGuard($id:ID!){productVariant(id:$id){id sku product{status} inventoryItem{id tracked}}}",
                {"id": self.shopify_id},
            )
            node = data.get("productVariant") or {}
            item = node.get("inventoryItem") or {}
            if not (node.get("id") == self.shopify_id and node.get("sku") == self.sku_snapshot
                    and (node.get("product") or {}).get("status") == "DRAFT"
                    and item.get("id") == self.inventory_item_shopify_id and item.get("tracked")):
                raise UserError(self.env._("Own acceptance writes are allowed only to the exact tracked Draft variant."))
            # Recheck expiry, scope, SKU and gates AFTER the live guard read.
            queue._ith_guard_own_stock_entry(self, location_binding.id, snapshot=snapshot)
        return super()._execute_inventory_set(client, location_binding, desired, compare_quantity)
