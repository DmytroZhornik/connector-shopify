"""Use the existing Orders reconciliation cron for a finite Staging scope."""

from odoo import api, models

SELECTED_ORDERS_QUERY = """
query ITHSelectedOrders($ids: [ID!]!) {
  nodes(ids: $ids) { ... on Order { id updatedAt } }
}
"""


class ITHSelectedOrderReconcile(models.Model):
    _inherit = "shopify.instance"

    @api.model
    def _cron_reconcile_orders(self):
        if "staging" not in self.env.cr.dbname.lower():
            return super()._cron_reconcile_orders()
        scope = self.env["queue.job"]._ith_order_scope()
        if not scope or self.env.context.get("cron_id") != scope["reconcile_cron_id"]:
            return False
        cron = self.env["ir.cron"].sudo().browse(scope["reconcile_cron_id"]).exists()
        if not (
            cron and cron.active and cron.model_id.model == "shopify.instance"
            and cron.code.strip() == "model._cron_reconcile_orders()"
        ):
            return False
        instance = self.sudo().browse(1).exists()
        if not instance or instance.shop_url != "uitpac-aj.myshopify.com":
            return False
        instance.with_delay(
            identity_key=f"ith.orders.reconcile.1.{scope['min_job_id']}",
            description=self.env._("ITH bounded Orders reconciliation"),
        )._ith_job_reconcile_selected_orders()
        return True

    def _ith_job_reconcile_selected_orders(self):
        self.ensure_one()
        snapshot = self.env["queue.job"]._ith_guard_order_job_entry()
        scope = self.env["queue.job"]._ith_assert_order_scope_snapshot(snapshot)
        if not scope or self.id != 1 or not self.active:
            return False
        data = self._shopify_client().execute(
            SELECTED_ORDERS_QUERY, {"ids": scope["order_ids"]},
        )
        self.env["queue.job"]._ith_assert_order_scope_snapshot(snapshot)
        count = 0
        for node in data.get("nodes") or []:
            self.env["queue.job"]._ith_assert_order_scope_snapshot(snapshot)
            if not isinstance(node, dict) or node.get("id") not in scope["order_ids"]:
                continue
            self.env["shopify.order"].with_delay(
                identity_key=f"shopify.order.reconcile.1.{node['id']}",
                description=self.env._("Reconcile Shopify order %s", node["id"]),
            )._job_import_order_from_api(self.id, node["id"])
            count += 1
        self._write_log(
            entity="drift_orders", direction="import", level="info",
            message=self.env._(
                "Bounded reconciliation queued %(found)s of %(expected)s explicitly selected orders; "
                "no catalogue, inventory, bulk or draft-order jobs were queued.",
                found=count, expected=len(scope["order_ids"]),
            ), record=self,
        )
        return count


class ITHSelectedOrderWebhook(models.Model):
    _inherit = "shopify.webhook.event"

    def _dispatch(self):
        if "staging" not in self.env.cr.dbname.lower():
            return super()._dispatch()
        snapshot = self.env["queue.job"]._ith_guard_order_job_entry()
        result = super()._dispatch()
        self.env["queue.job"]._ith_assert_order_scope_snapshot(snapshot)
        return result
