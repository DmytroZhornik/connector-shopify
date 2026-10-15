"""Restrict the installed OCA cron jobrunner; never drain Staging's backlog."""

import json
import time

from odoo import api, models
from odoo.exceptions import UserError
from odoo.addons.queue_job.job import Job
from odoo.addons.shopify_connector.lib.order import shopify_gid

from .order_policy import order_scope, scoped_job


class ITHOrderQueue(models.Model):
    _inherit = "queue.job"

    @api.model
    def _ith_order_scope(self):
        params = self.env["ir.config_parameter"].sudo()
        return order_scope(
            params.get_param("ith.orders.queue.enabled", "False"),
            params.get_param("ith.orders.queue.scope", "{}"), time.time(),
        )

    @api.model
    def _ith_order_business_safe(self):
        instance = self.env["shopify.instance"].sudo().browse(1).exists()
        params = self.env["ir.config_parameter"].sudo()
        return bool(
            instance and instance.active and instance.shop_url == "uitpac-aj.myshopify.com"
            and instance.order_confirmation_policy == "quotation"
            and instance.order_invoicing_policy == "manual"
            and not instance.fulfillment_auto_validate_webhook
            and not instance.inventory_import_enabled
            and params.get_param("ith.external.queue.enabled", "False") != "True"
            and params.get_param("ith.external.shopify.auto_sync", "False") != "True"
        )

    @api.model
    def _ith_order_cron_scope(self):
        scope = self._ith_order_scope()
        if not scope or self.env.context.get("cron_id") != scope["cron_id"]:
            return None
        cron = self.env["ir.cron"].sudo().browse(scope["cron_id"]).exists()
        if not (
            cron and cron.active and cron.queue_job_runner
            and cron.model_id.model == "queue.job"
            and cron.code.strip() == "model._job_runner()"
            and self._ith_order_business_safe()
        ):
            return None
        return scope

    @api.model
    def _ith_job_matches_order_scope(self, record, scope):
        job = Job.load(self.env, record.uuid)
        refs = job.recordset
        instance_id = None
        order_id = webhook_id = topic = None
        if len(refs) == 1 and refs.exists():
            if refs._name == "shopify.instance":
                instance_id = refs.id
            elif refs._name in ("shopify.order", "shopify.webhook.event"):
                instance_id = refs.instance_id.id
                if refs._name == "shopify.order":
                    order_id = refs.shopify_id
                else:
                    payload = refs.payload or {}
                    order_id = shopify_gid(
                        "Order", payload.get("admin_graphql_api_id") or payload.get("id")
                    )
                    webhook_id, topic = refs.webhook_id, refs.topic
        return scoped_job(
            scope, job_id=record.id, model=refs._name,
            method=job.method_name, channel=job.channel,
            args=job.args, kwargs=job.kwargs, identity_key=record.identity_key,
            record_ids=refs.ids, instance_id=instance_id, order_id=order_id,
            webhook_id=webhook_id, topic=topic,
        )

    @api.model
    def _ith_guard_order_job_entry(self):
        """Native Job.func sets job_uuid; do not use a guessed context key."""
        if "staging" not in self.env.cr.dbname.lower():
            return None
        scope = self._ith_order_scope()
        uuid = self.env.context.get("job_uuid")
        if not scope or not uuid or not self._ith_order_business_safe():
            raise UserError(self.env._("The bounded C1 execution window is closed."))
        record = self.sudo().search([
            ("uuid", "=", uuid), ("state", "=", "started"),
            ("id", ">", scope["min_job_id"]),
        ], limit=1)
        if not record or not self._ith_job_matches_order_scope(record, scope):
            raise UserError(self.env._("This job is not in the exact C1 execution graph."))
        return json.dumps(scope, sort_keys=True, separators=(",", ":"))

    @api.model
    def _ith_assert_order_scope_snapshot(self, snapshot):
        """Never let expiry or gate closure remove an in-flight safety guard."""
        params = self.env["ir.config_parameter"].sudo()
        scope = order_scope(
            params.get_param("ith.orders.queue.enabled", "False"), snapshot, time.time(),
        )
        if not scope or scope != self._ith_order_scope() or not self._ith_order_business_safe():
            raise UserError(self.env._("The C1 scope expired or changed; changes are stopped."))
        return scope

    @api.model
    def _job_runner(self, commit=True):
        if "staging" not in self.env.cr.dbname.lower():
            return super()._job_runner(commit=commit)
        if not self._ith_order_cron_scope():
            return False
        progress = self.env["ir.cron"]
        if not progress._commit_progress():
            return False
        processed = False
        for _ in range(100):
            job = self._acquire_one_job()
            if not job:
                progress._commit_progress(remaining=0)
                break
            # Recheck AFTER acquiring a row, immediately BEFORE native execution.
            scope = self._ith_order_cron_scope()
            if not scope or not self._ith_job_matches_order_scope(job, scope):
                progress._commit_progress(remaining=0)
                break
            # Native OCA state/error/retry/dependency handling, never copied.
            job._process(commit=False)
            processed = True
            if not progress._commit_progress(1):
                break
        return processed

    @api.model
    def _acquire_one_job(self):
        if "staging" not in self.env.cr.dbname.lower():
            return super()._acquire_one_job()
        scope = self._ith_order_cron_scope()
        if not scope:
            return self.browse()
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id FROM queue_job
            WHERE id > %s AND state = 'pending'
              AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
              AND (
                (channel = 'root.shopify.import' AND model_name = 'shopify.order'
                 AND method_name IN ('_job_import_order',
                    '_job_import_order_from_api', '_job_refresh_fulfillment_orders'))
                OR (channel = 'root.shopify.webhook'
                    AND model_name = 'shopify.webhook.event' AND method_name = '_dispatch')
                OR (channel = 'root.shopify.import' AND model_name = 'shopify.instance'
                    AND method_name = '_ith_job_reconcile_selected_orders')
              )
            ORDER BY priority, date_created, id
            LIMIT 200 FOR NO KEY UPDATE SKIP LOCKED
        """, (scope["min_job_id"],))
        for (identifier,) in self.env.cr.fetchall():
            record = self.sudo().browse(identifier)
            if self._ith_job_matches_order_scope(record, scope):
                return record
        return self.browse()
