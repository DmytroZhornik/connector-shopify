"""Shopify owns its catalogue. These methods never create jobs or call APIs."""
from odoo import models


class ITHCatalogueWriteGuard(models.Model):
    _inherit = "shopify.product.template"

    def _enqueue_product_export(self, instance, template):
        return False

    def _job_export_product(self, instance_id, template_id):
        return False

    def _product_set_input(self, *args, **kwargs):
        # Defence in depth if a future caller tries to bypass the job method.
        from odoo.exceptions import UserError
        raise UserError(self.env._("Shopify owns the catalogue; Odoo catalogue export is disabled."))
