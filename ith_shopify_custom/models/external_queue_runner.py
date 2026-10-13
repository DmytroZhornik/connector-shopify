from odoo import api, models


class ITHExternalQueueRunner(models.AbstractModel):
    _name = "ith.external.queue.runner"
    _description = "ITH External Stock Queue Runner"

    @api.model
    def run_pending_external_jobs(self):
        """Safe entry point. Execution remains disabled."""
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            "ith.external.queue.enabled", "False"
        )
        if enabled != "True":
            return False

        # No execution until the isolation and transaction handling
        # have been verified in staging.
        return False

    @api.model
    def get_pending_external_jobs(self):
        """Return only jobs from the dedicated External channel."""
        return self.env["queue.job"].sudo().search([
            ("channel", "=", "root.shopify.external"),
            ("state", "=", "pending"),
            ("model_name", "in", [
                "shopify.order",
                "shopify.product.variant",
            ]),
            ("method_name", "in", [
                "_ith_fetch_and_reconcile_external",
                "_job_push_inventory_level",
            ]),
        ], order="id", limit=1)

    @api.model
    def validate_external_job(self, job_record):
        """Validate the exact Shopify instance and external location."""
        from odoo.addons.queue_job.job import Job

        assert len(job_record) == 1
        job = Job.load(self.env, job_record.uuid)

        instance = self.env["shopify.instance"].sudo().with_context(
            active_test=False
        ).browse(1)

        if not instance.exists() or instance.shop_url != "uitpac-aj.myshopify.com":
            return False

        if not instance.active:
            return False

        if job.channel != "root.shopify.external":
            return False

        if job.recordset.instance_id.id != instance.id:
            return False

        if job.method_name == "_ith_fetch_and_reconcile_external":
            return job.recordset._name == "shopify.order"

        if job.method_name == "_job_push_inventory_level":
            if job.recordset._name != "shopify.product.variant":
                return False
            if len(job.args) != 1:
                return False
            location = self.env["shopify.location"].sudo().browse(job.args[0])
            return bool(
                location.exists()
                and location.instance_id.id == instance.id
                and location.name == "Shop Location External"
                and location.use_supplier_stock
            )

        return False
