"""Keep unknown-SKU orders visible without inventing a stock product or SKU."""

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.addons.shopify_connector.lib.order import shopify_gid

from .order_policy import mapping_issue


class ITHOrderMapping(models.Model):
    _inherit = "shopify.order"

    ith_mapping_pending = fields.Boolean(
        related="odoo_id.ith_shopify_mapping_pending", readonly=True, store=True,
    )

    def _ith_c1_execution_context(self):
        if "staging" not in self.env.cr.dbname.lower():
            return self
        snapshot = self.env["queue.job"]._ith_guard_order_job_entry()
        return self.with_context(ith_c1_order_execution=True, ith_c1_scope=snapshot)

    def _ith_check_c1_execution(self):
        if self.env.context.get("ith_c1_order_execution"):
            return self.env["queue.job"]._ith_assert_order_scope_snapshot(
                self.env.context.get("ith_c1_scope")
            )
        return None

    def _job_import_order_from_api(self, instance_id, order_id, webhook_payload=None):
        self = self._ith_c1_execution_context()
        result = super()._job_import_order_from_api(instance_id, order_id, webhook_payload)
        self._ith_check_c1_execution()
        return result

    def _job_import_order(self, instance_id, payload, *, payload_normalized=False, is_draft=False):
        self = self._ith_c1_execution_context()
        scope = self._ith_check_c1_execution()
        if scope and (
            instance_id != 1 or is_draft or not isinstance(payload, dict)
            or shopify_gid("Order", payload.get("id")) not in scope["order_ids"]
        ):
            raise UserError(self.env._("The incoming order is outside the C1 scope."))
        result = super()._job_import_order(
            instance_id, payload, payload_normalized=payload_normalized, is_draft=is_draft,
        )
        self._ith_check_c1_execution()
        return result

    def _job_refresh_fulfillment_orders(self):
        self = self._ith_c1_execution_context()
        result = super()._job_refresh_fulfillment_orders()
        self._ith_check_c1_execution()
        return result

    def _ith_reconcile_external_stock(self):
        self.odoo_id._ith_check_mapping()
        return super()._ith_reconcile_external_stock()

    def _ith_reconcile_external_snapshot(self, payload):
        self.odoo_id._ith_check_mapping()
        return super()._ith_reconcile_external_snapshot(payload)

    def _ith_fetch_and_reconcile_external(self):
        self.odoo_id._ith_check_mapping()
        return super()._ith_fetch_and_reconcile_external()

    def _ith_mapping_enabled(self, instance):
        return bool(
            "staging" in self.env.cr.dbname.lower()
            and instance.id == 1
            and self.env["ir.config_parameter"].sudo().get_param(
                "ith.orders.mapping.enabled", "False"
            ) == "True"
        )

    def _ith_mapping_issue(self, instance, line_data):
        if not self._ith_mapping_enabled(instance) or line_data.get("gift_card"):
            return ""
        variant = self.env["shopify.product.variant"].search([
            ("instance_id", "=", instance.id),
            ("shopify_id", "=", line_data.get("variant_id")),
        ], limit=1)
        return mapping_issue(
            line_data, bool(variant), bool(variant.stock_product_id),
            variant.stock_product_id.default_code,
        )

    def _line_product(self, instance, line_data):
        if not self._ith_mapping_issue(instance, line_data):
            return super()._line_product(instance, line_data)
        product = self.env.ref("ith_shopify_custom.product_unmapped_order_line")
        if (
            product.type != "service" or product.is_storable or product.default_code
            or (product.company_id and product.company_id != instance.company_id)
        ):
            raise UserError(self.env._("The unmapped-line service is not safe for this company."))
        return product

    def _build_order(self, binding, instance, order_data):
        self._ith_check_c1_execution()
        if (
            self.env.context.get("ith_c1_order_execution")
            and binding.odoo_id and binding.odoo_id.state not in ("draft", "sent")
        ):
            raise UserError(self.env._(
                "C1 acceptance cannot change a confirmed or cancelled existing order."
            ))
        return super()._build_order(binding, instance, order_data)

    def _apply_lines(self, binding, sale_order, instance, order_data):
        self._ith_check_c1_execution()
        if sale_order.state not in ("draft", "sent") and any(
            line["quantity"] > 0 and self._ith_mapping_issue(instance, line)
            for line in order_data["lines"]
        ):
            raise UserError(self.env._(
                "An unknown SKU cannot replace a line in a confirmed order. "
                "Resolve the canonical mapping before retrying."
            ))
        result = super()._apply_lines(binding, sale_order, instance, order_data)
        self._ith_check_c1_execution()
        incoming = {line["id"]: line for line in order_data["lines"]}
        for line_binding in binding.line_binding_ids.filtered(lambda line: not line.is_shipping):
            line = incoming.get(line_binding.shopify_id)
            if not line:
                continue
            issue = self._ith_mapping_issue(instance, line)
            line_binding.odoo_id.write({
                "ith_shopify_unmapped": bool(issue),
                "ith_shopify_source_sku": line.get("sku") or False,
                "ith_shopify_mapping_issue": issue or False,
            })
            if issue:
                line_binding.write({"state": "pending", "error_message": issue})
        return result

    def _should_confirm_shopify_order(self, instance, order_data, sale_order):
        if sale_order.ith_shopify_mapping_pending:
            return False
        return super()._should_confirm_shopify_order(instance, order_data, sale_order)

    def _apply_state(self, binding, sale_order, order_data):
        self._ith_check_c1_execution()
        result = super()._apply_state(binding, sale_order, order_data)
        self._ith_check_c1_execution()
        return result

    def _apply_invoice_and_payments(self, binding, order_data):
        self._ith_check_c1_execution()
        if binding.odoo_id.ith_shopify_mapping_pending:
            return None
        return super()._apply_invoice_and_payments(binding, order_data)

    def _apply_returns(self, binding, order_data):
        self._ith_check_c1_execution()
        if binding.odoo_id.ith_shopify_mapping_pending:
            return None
        return super()._apply_returns(binding, order_data)

    def _apply_refunds(self, binding, order_data):
        self._ith_check_c1_execution()
        if binding.odoo_id.ith_shopify_mapping_pending:
            # Original refunds remain in raw_payload. Never post credits or
            # restock returns while the underlying stock line is unknown.
            return None
        return super()._apply_refunds(binding, order_data)

    def _write_success(self, binding, sale_order, order_data, digest):
        self._ith_check_c1_execution()
        result = super()._write_success(binding, sale_order, order_data, digest)
        self._ith_check_c1_execution()
        if sale_order.ith_shopify_mapping_pending:
            pending = sale_order.order_line.filtered("ith_shopify_unmapped")
            message = self.env._(
                "Requires canonical SKU mapping: %s. Confirmation, reservation "
                "and invoicing are blocked. Refund processing is deferred.",
                ", ".join(line.ith_shopify_source_sku or "(empty SKU)" for line in pending),
            )
            # Keep pending, not synced: the same payload may be retried AFTER
            # mapping changes without clearing the original digest or history.
            binding.write({"state": "pending", "error_message": message})
            binding.instance_id._write_log(
                entity="order", direction="import", level="warning",
                message=message, record=binding,
            )
        return result

    def action_ith_retry_mapping(self):
        """Queue a fresh authoritative read; never perform a job in the UI."""
        for binding in self.filtered("ith_mapping_pending"):
            self.env["shopify.order"].with_delay(
                identity_key=f"shopify.order.mapping.retry.{binding.instance_id.id}.{binding.shopify_id}",
                description=self.env._("Retry SKU mapping for %s", binding.order_name),
            )._job_import_order_from_api(binding.instance_id.id, binding.shopify_id)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": self.env._("Mapping retry queued"),
                "message": self.env._("A fresh Shopify order read is queued; no job was run manually."),
                "type": "info", "sticky": False,
            },
        }


class ITHSaleOrderMapping(models.Model):
    _inherit = "sale.order"

    ith_shopify_mapping_pending = fields.Boolean(
        string="Requires Shopify SKU mapping", compute="_compute_ith_mapping_pending",
        store=True, readonly=True, copy=False,
    )

    @api.depends("order_line.ith_shopify_unmapped", "order_line.product_id")
    def _compute_ith_mapping_pending(self):
        technical = self.env.ref(
            "ith_shopify_custom.product_unmapped_order_line", raise_if_not_found=False,
        )
        for order in self:
            order.ith_shopify_mapping_pending = bool(
                any(order.order_line.mapped("ith_shopify_unmapped"))
                or (technical and technical in order.order_line.product_id)
            )

    def _ith_check_mapping(self):
        if self.filtered("ith_shopify_mapping_pending"):
            raise UserError(self.env._(
                "Resolve the canonical Shopify SKU mapping before confirming, "
                "reserving or invoicing this order. Queue a fresh import after mapping."
            ))

    def copy_data(self, default=None):
        self._ith_check_mapping()
        return super().copy_data(default=default)

    def action_confirm(self):
        self._ith_check_mapping()
        return super().action_confirm()

    def _create_invoices(self, *args, **kwargs):
        self._ith_check_mapping()
        return super()._create_invoices(*args, **kwargs)


class ITHSaleOrderLineMapping(models.Model):
    _inherit = "sale.order.line"

    ith_shopify_unmapped = fields.Boolean(readonly=True, copy=False)
    ith_shopify_source_sku = fields.Char(string="Original Shopify SKU", readonly=True, copy=False)
    ith_shopify_mapping_issue = fields.Char(readonly=True, copy=False)

    def _action_launch_stock_rule(self, *args, **kwargs):
        self.order_id._ith_check_mapping()
        return super()._action_launch_stock_rule(*args, **kwargs)


class ITHPickingMapping(models.Model):
    _inherit = "stock.picking"

    def button_validate(self):
        self.sale_id._ith_check_mapping()
        return super().button_validate()
