"""Recurring stock-only Staging candidate. All controls default CLOSED."""
import json
import time
import logging
from datetime import timezone

from odoo import api, models
from odoo.exceptions import UserError
from odoo.addons.queue_job.job import Job

from .stock_dispatch_policy import (
    CHANNEL, METHOD, ENABLE_KEY, SCOPE_KEY, OTHER_GATES, stock_scope, encode,
    digest, identity, scoped_item, job_matches, quantity, location_ready, live_level,
)

_logger = logging.getLogger(__name__)
LIVE_QUERY = """query ITHStockIdentityLevel($id: ID!, $location: ID!) {
  productVariant(id: $id) { id sku product { status }
    inventoryItem { id tracked inventoryLevel(locationId: $location) {
      id quantities(names: [\"available\"]) { name quantity }
    } }
  }
}"""


class ITHStockDispatcher(models.Model):
    _inherit = "queue.job"

    @api.model
    def _ith_stock_parameters(self):
        # Read committed controls in a separate READ ONLY cursor: do not rely on
        # ir.config_parameter cache or this job's repeatable-read snapshot.
        keys = [ENABLE_KEY, SCOPE_KEY, *OTHER_GATES]
        with self.env.registry.cursor() as cr:
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY")
            cr.execute("SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,))
            return dict(cr.fetchall())

    @api.model
    def _ith_stock_fresh_supplier(self, sku):
        with self.env.registry.cursor() as cr:
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY")
            cr.execute("SELECT sku, snapshot_version, quantity FROM ith_external_stock WHERE sku = %s", (sku,))
            rows = cr.fetchall()
        if len(rows) != 1 or rows[0][0] != sku:
            raise UserError(self.env._("Supplier availability is missing or ambiguous; never assume zero."))
        return rows[0]

    @api.model
    def _ith_stock_requested(self):
        try:
            return self._ith_stock_parameters().get(ENABLE_KEY, "False") == "True"
        except Exception:
            # Unknown controls must route to the closed new policy, never back
            # into legacy admissions and never abort a physical stock update.
            _logger.exception("ITH stock controls unavailable: exports fail closed")
            return True

    @api.model
    def _ith_stock_safe_scope(self):
        try:
            params = self._ith_stock_parameters()
        except Exception:
            _logger.exception("ITH stock scope unavailable: exports fail closed")
            return None
        scope = stock_scope(params.get(ENABLE_KEY, "False"), params.get(SCOPE_KEY, "{}"), self.env.cr.dbname)
        if not scope or any(params.get(k, "False") != "False" for k in OTHER_GATES):
            return None
        instance = self.env["shopify.instance"].sudo().browse(1).exists()
        cron = self.env.ref("ith_shopify_custom.cron_ith_stock_policy", raise_if_not_found=False)
        old_crons = self.env["ir.cron"].sudo().with_context(active_test=False).browse([53, 56, 58]).exists()
        other_runners = self.env["ir.cron"].sudo().with_context(active_test=False).search([
            ("active", "=", True), ("queue_job_runner", "=", True), ("id", "!=", cron.id if cron else 0),
        ], limit=1)
        if not (instance and instance.active and instance.state == "connected"
                and instance.shop_url == "uitpac-aj.myshopify.com"
                and instance.company_id.id == scope["company_id"]
                and instance.inventory_quantity_basis == scope["quantity_basis"]
                and not instance.inventory_import_enabled and not instance.inventory_allow_negative
                and instance.order_confirmation_policy == "quotation"
                and instance.order_invoicing_policy == "manual"
                and not instance.fulfillment_auto_validate_webhook
                and cron and cron.active and cron.queue_job_runner
                and cron.model_id.model == "queue.job" and cron.code.strip() == "model._ith_stock_tick()"
                and len(old_crons) == 3 and not any(c.active for c in old_crons) and not other_runners):
            return None
        for loc in scope["locations"]:
            record = self.env["shopify.location"].sudo().browse(loc["id"]).exists()
            if not (record and record.active and record.instance_id == instance
                    and record.shopify_id == loc["gid"] and record.use_supplier_stock == loc["supplier"]
                    and record.odoo_location_id.id == loc["odoo_location_id"]
                    and record.odoo_location_id.usage == "internal"
                    and (not record.odoo_location_id.company_id or record.odoo_location_id.company_id == instance.company_id)):
                return None
        return scope

    @api.model
    def _ith_stock_cron_scope(self):
        scope = self._ith_stock_safe_scope()
        cron = self.env.ref("ith_shopify_custom.cron_ith_stock_policy", raise_if_not_found=False)
        return scope if scope and cron and self.env.context.get("cron_id") == cron.id else None

    @api.model
    def _ith_stock_validate_binding(self, scope, variant, location_id):
        item = scoped_item(scope, variant.id, location_id)
        if not (item and len(variant) == 1 and variant.exists() and variant.instance_id.id == 1
                and variant.stock_product_id.id == item["stock_product_id"]
                and variant.stock_product_id.default_code == item["sku"] == variant.sku_snapshot
                and variant.stock_product_id.active and variant.shopify_id == item["variant_gid"]
                and variant.inventory_item_shopify_id == item["inventory_item_gid"]
                and variant._inventory_sync_eligible()):
            raise UserError(self.env._("Stock-only binding/canonical SKU is outside the approved scope."))
        # Reject a second canonical product/binding with the same SKU/item.
        products = self.env["product.product"].sudo().with_context(active_test=False).search([
            ("default_code", "=", item["sku"]),
        ], limit=2)
        bindings = self.env["shopify.product.variant"].sudo().with_context(active_test=False).search([
            ("instance_id", "=", 1), "|", ("sku_snapshot", "=", item["sku"]),
            ("inventory_item_shopify_id", "=", item["inventory_item_gid"]),
        ], limit=2)
        if products.ids != [item["stock_product_id"]] or bindings.ids != [item["binding_id"]]:
            raise UserError(self.env._("Ambiguous canonical SKU or duplicate Shopify inventory binding."))
        loc = next(l for l in scope["locations"] if l["id"] == location_id)
        if loc["supplier"]:
            _, version, available = self._ith_stock_fresh_supplier(item["sku"])
            if not location_ready(loc, time.time(), version):
                raise UserError(self.env._("Supplier snapshot is missing, changed, or past its accepted validity boundary."))
            quantity(available)
        return item, loc

    @api.model
    def _ith_stock_job_matches(self, record, scope):
        job = Job.load(self.env, record.uuid)
        refs = job.recordset
        return bool(len(refs) == 1 and refs.exists() and job_matches(
            scope, job_id=record.id, model=refs._name, method=job.method_name,
            channel=job.channel, args=job.args, kwargs=job.kwargs,
            identity_key=job.identity_key, record_ids=refs.ids, instance_id=refs.instance_id.id,
        ))

    @api.model
    def _ith_stock_guard_entry(self, variant, location_id, snapshot=None):
        scope = self._ith_stock_safe_scope()
        uuid = self.env.context.get("job_uuid")
        if not scope or not uuid or (snapshot is not None and snapshot != encode(scope)):
            raise UserError(self.env._("The approved stock-only execution policy is closed or changed."))
        record = self.sudo().search([("uuid", "=", uuid), ("state", "=", "started")], limit=1)
        if not record or not self._ith_stock_job_matches(record, scope):
            raise UserError(self.env._("Only a newly admitted native stock job can export inventory."))
        job = Job.load(self.env, record.uuid)
        if job.recordset.id != variant.id or job.args != (location_id,):
            # OCA currently stores args as tuple; accept the equivalent list too.
            if job.recordset.id != variant.id or job.args != [location_id]:
                raise UserError(self.env._("Stock job record or location changed."))
        self._ith_stock_validate_binding(scope, variant, location_id)
        return encode(scope)

    @api.model
    def _ith_stock_enqueue_item(self, scope, item, location_id, force=False):
        if encode(scope) != encode(self._ith_stock_safe_scope()):
            return False
        variant = self.env["shopify.product.variant"].sudo().browse(item["binding_id"]).exists()
        _, loc = self._ith_stock_validate_binding(scope, variant, location_id)
        location = self.env["shopify.location"].sudo().browse(location_id)
        if loc["supplier"]:
            _, version, available = self._ith_stock_fresh_supplier(item["sku"])
            if not location_ready(loc, time.time(), version):
                raise UserError(self.env._("Supplier snapshot changed before stock admission."))
            desired = quantity(available)
        else:
            desired = quantity(variant._odoo_inventory_quantity(location))
        key = identity(scope, variant.id, location_id)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        existing = self.sudo().search([("identity_key", "=", key),
            ("state", "in", ["pending", "enqueued", "started", "wait_dependencies", "failed"])], limit=1)
        if existing:
            return False  # failed remains for operator review, no automatic retry storm
        # A concurrent producer may have committed while our repeatable-read
        # transaction waited for the lock. Check its committed identity afresh.
        with self.env.registry.cursor() as cr:
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY")
            cr.execute("""SELECT EXISTS(SELECT 1 FROM queue_job WHERE identity_key = %s
                AND state IN ('pending','enqueued','started','wait_dependencies','failed'))""", (key,))
            if cr.fetchone()[0]:
                return False
        if encode(scope) != encode(self._ith_stock_safe_scope()):
            return False
        state = self.env["shopify.inventory.state"].sudo().search([
            ("variant_binding_id", "=", variant.id), ("location_binding_id", "=", location_id), ("instance_id", "=", 1),
        ], limit=1)
        if not force and state and state.last_sync_date and state.last_shopify_quantity == desired:
            age = time.time() - state.last_sync_date.replace(tzinfo=timezone.utc).timestamp()
            if 0 <= age < scope["recheck_seconds"]:
                return False
        variant.with_delay(channel=CHANNEL, identity_key=key,
            description="ITH admitted stock-only inventory delta")._ith_job_push_stock(location_id)
        return True

    @api.model
    def _ith_stock_enqueue_products(self, product_ids=None, supplier_skus=None):
        scope = self._ith_stock_safe_scope()
        if not scope:
            return 0
        count = 0
        for item in scope["items"]:
            if product_ids is not None and item["stock_product_id"] not in product_ids:
                continue
            if supplier_skus is not None and item["sku"] not in supplier_skus:
                continue
            for lid in item["location_ids"]:
                # Own quant changes never enqueue the supplier location and vice versa.
                if supplier_skus is None and lid != 1 or supplier_skus is not None and lid != 2:
                    continue
                try:
                    with self.env.cr.savepoint():
                        count += bool(self._ith_stock_enqueue_item(scope, item, lid))
                except (UserError, ValueError) as exc:
                    _logger.warning("ITH stock admission skipped binding %s/location %s: %s", item["binding_id"], lid, exc)
        return count

    @api.model
    def _ith_stock_acquire(self, scope):
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id FROM queue_job
            WHERE id > %s AND state = 'pending' AND channel = %s
              AND model_name = 'shopify.product.variant' AND method_name = %s
              AND identity_key LIKE %s AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
            ORDER BY priority, date_created, id LIMIT 200 FOR NO KEY UPDATE SKIP LOCKED
        """, (scope["min_job_id"], CHANNEL, METHOD, f"ith.stock.v1.{digest(scope)}.%"))
        for (jid,) in self.env.cr.fetchall():
            record = self.sudo().browse(jid)
            if self._ith_stock_job_matches(record, scope):
                return record
        return self.browse()

    @api.model
    def _ith_stock_process_some(self, scope, limit, progress):
        processed = 0
        for _ in range(limit):
            current = self._ith_stock_cron_scope()
            if not current or encode(current) != encode(scope):
                break
            record = self._ith_stock_acquire(current)
            if not record:
                break
            current = self._ith_stock_cron_scope()
            if not current or encode(current) != encode(scope) or not self._ith_stock_job_matches(record, current):
                break
            record._process(commit=False)  # Installed OCA error/retry/state lifecycle.
            processed += 1
            if not progress._commit_progress(1):
                return processed, False
        return processed, True

    @api.model
    def _ith_stock_tick(self):
        scope = self._ith_stock_cron_scope()
        if not scope:
            return False  # Never fall back to the unrestricted general runner.
        progress = self.env["ir.cron"]
        if not progress._commit_progress():
            return False
        # Even a large/slow sweep cannot starve yesterday's pending stock jobs.
        processed, budget = self._ith_stock_process_some(scope, scope["job_limit"], progress)
        if not budget:
            return {"scanned": 0, "queued": 0, "processed": processed}
        params = self.env["ir.config_parameter"].sudo()
        cursor_key = f"ith.stock.policy.cursor.{digest(scope)}"
        try:
            start = int(params.get_param(cursor_key, "0")) % len(scope["items"])
        except (ValueError, TypeError):
            start = 0
        scanned = queued = 0
        for offset in range(min(scope["scan_limit"], len(scope["items"]))):
            if encode(scope) != encode(self._ith_stock_cron_scope()):
                break
            item = scope["items"][(start + offset) % len(scope["items"])]
            for lid in item["location_ids"]:
                try:
                    with self.env.cr.savepoint():
                        queued += bool(self._ith_stock_enqueue_item(scope, item, lid))
                except (UserError, ValueError) as exc:
                    _logger.warning("ITH stock sweep skipped binding %s/location %s: %s", item["binding_id"], lid, exc)
            scanned += 1
            params.set_param(cursor_key, str((start + scanned) % len(scope["items"])))
            if not progress._commit_progress(1):
                return {"scanned": scanned, "queued": queued, "processed": processed}
        more, budget = self._ith_stock_process_some(scope, scope["job_limit"] - processed, progress)
        processed += more
        return {"scanned": scanned, "queued": queued, "processed": processed}


class ITHStockOnlyVariant(models.Model):
    _inherit = "shopify.product.variant"

    def _odoo_inventory_quantity(self, location_binding, *, apply_export_policy=True):
        snapshot = self.env.context.get("ith_stock_policy_snapshot")
        if snapshot is not None and location_binding.use_supplier_stock:
            self.ensure_one()
            # Same lock used by replace_supplier_snapshot/reconcile_order_quantity.
            # Held until the native job commits: fresh desired + mutation/readback
            # cannot race a committed replacement supplier snapshot.
            self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ith.external.stock.snapshot",))
            queue = self.env["queue.job"]
            queue._ith_stock_guard_entry(self, location_binding.id, snapshot=snapshot)
            scope = json.loads(snapshot)
            loc = next(l for l in scope["locations"] if l["id"] == location_binding.id)
            _, version, available = queue._ith_stock_fresh_supplier(self.sku_snapshot)
            if not location_ready(loc, time.time(), version):
                raise UserError(self.env._("Supplier snapshot changed or expired before inventory calculation."))
            return quantity(available)
        return super()._odoo_inventory_quantity(location_binding, apply_export_policy=apply_export_policy)

    def _ith_stock_live_read(self, client, scope, location_id):
        queue = self.env["queue.job"]
        snapshot = self.env.context.get("ith_stock_policy_snapshot")
        if not snapshot or snapshot != encode(scope):
            raise UserError(self.env._("Live inventory reads require the exact admitted policy snapshot."))
        queue._ith_stock_guard_entry(self, location_id, snapshot=snapshot)
        item, loc = queue._ith_stock_validate_binding(scope, self, location_id)
        data = client.execute(LIVE_QUERY, {"id": item["variant_gid"], "location": loc["gid"]})
        queue._ith_stock_guard_entry(self, location_id, snapshot=snapshot)
        try:
            return live_level(data.get("productVariant"), item, loc, scope["product_statuses"])
        except ValueError as exc:
            raise UserError(str(exc)) from exc

    def _read_shopify_inventory_quantity(self, client, location_binding):
        snapshot = self.env.context.get("ith_stock_policy_snapshot")
        if snapshot is not None or self.env["queue.job"]._ith_stock_requested():
            if not snapshot:
                raise UserError(self.env._("Inventory reads require the admitted stock-only native job context."))
            # Includes the inherited CAS-refresh branch: no unguarded read.
            return self._ith_stock_live_read(client, json.loads(snapshot), location_binding.id)
        return super()._read_shopify_inventory_quantity(client, location_binding)

    def _ith_job_push_stock(self, location_id):
        self = self.sudo()
        self.ensure_one()
        queue = self.env["queue.job"]
        snapshot = queue._ith_stock_guard_entry(self, location_id)
        bound = self.with_context(ith_stock_policy_snapshot=snapshot)
        scope = json.loads(snapshot)
        client = self.instance_id._shopify_client()
        client.timeout = 20
        client.max_retries = 0
        remote = bound._ith_stock_live_read(client, scope, location_id)
        queue._ith_stock_guard_entry(self, location_id, snapshot=snapshot)
        location = self.env["shopify.location"].sudo().browse(location_id)
        desired = quantity(bound._odoo_inventory_quantity(location))
        result = bound._push_inventory_level(location, remote_quantity=remote, reason="delta")
        queue._ith_stock_guard_entry(self, location_id, snapshot=snapshot)
        actual = bound._ith_stock_live_read(client, scope, location_id)
        if actual != desired:
            raise UserError(self.env._("Stock-only readback differs from requested availability; review before retry."))
        return result

    def _execute_inventory_set(self, client, location_binding, desired, compare_quantity):
        snapshot = self.env.context.get("ith_stock_policy_snapshot")
        queue = self.env["queue.job"]
        if snapshot is not None or queue._ith_stock_requested():
            if not snapshot:
                raise UserError(self.env._("Inventory writes require the admitted stock-only native job context."))
            queue._ith_stock_guard_entry(self, location_binding.id, snapshot=snapshot)
            client.timeout = 20
            client.max_retries = 0
            quantity(desired)
            self._ith_stock_live_read(client, json.loads(snapshot), location_binding.id)
            queue._ith_stock_guard_entry(self, location_binding.id, snapshot=snapshot)
        return super()._execute_inventory_set(client, location_binding, desired, compare_quantity)


class ITHStockOnlyQuantProducer(models.Model):
    _inherit = "stock.quant"

    def _enqueue_shopify_inventory_pairs(self, pairs):
        queue = self.env["queue.job"]
        if queue._ith_stock_requested():
            if self.env.context.get("skip_shopify_inventory_export"):
                return False
            return queue._ith_stock_enqueue_products(product_ids={p.id for p, loc in pairs if p and loc})
        return super()._enqueue_shopify_inventory_pairs(pairs)


class ITHStockOnlySupplierProducer(models.Model):
    _inherit = "ith.external.stock"

    def _enqueue_external_inventory(self):
        queue = self.env["queue.job"]
        if queue._ith_stock_requested():
            return queue._ith_stock_enqueue_products(supplier_skus=set(self.mapped("sku")))
        return super()._enqueue_external_inventory()
