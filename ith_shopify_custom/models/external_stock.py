from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ITHExternalStock(models.Model):
    _name = "ith.external.stock"
    _description = "Supplier Stock Availability"
    _rec_name = "sku"
    _order = "sku"

    sku = fields.Char(required=True, index=True)
    quantity = fields.Float(
        string="Supplier Available Quantity",
        required=True,
        default=0.0,
    )
    updated_at = fields.Datetime(
        string="Last Updated",
        default=fields.Datetime.now,
        readonly=True,
    )

    _unique_sku = models.Constraint(
        "UNIQUE(sku)",
        "Only one supplier availability record is allowed per SKU.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "sku" in vals:
                vals["sku"] = (vals["sku"] or "").strip()
            vals["updated_at"] = fields.Datetime.now()
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if "sku" in vals:
            vals["sku"] = (vals["sku"] or "").strip()
        vals["updated_at"] = fields.Datetime.now()
        return super().write(vals)

    @api.constrains("sku")
    def _check_sku(self):
        for record in self:
            if not record.sku:
                raise ValidationError(
                    self.env._("Supplier stock SKU cannot be empty.")
                )

    @api.model
    def replace_supplier_snapshot(self, rows, *, zero_missing=True):
        """Replace the complete external supplier stock snapshot.

        rows: iterable of {"sku": str, "quantity": number}.
        Only ith.external.stock is modified; stock.quant is untouched.
        """
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValidationError(
                self.env._("A full supplier stock file cannot be empty.")
            )

        incoming = {}

        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError(
                    self.env._("Invalid supplier stock row.")
                )

            sku = str(row.get("sku") or "").strip()
            if not sku:
                raise ValidationError(
                    self.env._("Supplier stock row has an empty SKU.")
                )

            if sku in incoming:
                raise ValidationError(
                    self.env._("Duplicate SKU in supplier file: %s", sku)
                )

            try:
                quantity = float(row["quantity"])
            except (KeyError, TypeError, ValueError):
                raise ValidationError(
                    self.env._("Invalid quantity for SKU %s", sku)
                )

            if not 0 <= quantity < float("inf"):
                raise ValidationError(
                    self.env._("Invalid quantity for SKU %s", sku)
                )

            incoming[sku] = quantity

        with self.env.cr.savepoint():
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("ith.external.stock.snapshot",),
            )

            existing = {r.sku: r for r in self.sudo().search([])}
            zeroed = 0

            for sku, quantity in incoming.items():
                if sku in existing:
                    if existing[sku].quantity != quantity:
                        existing[sku].write({"quantity": quantity})
                else:
                    self.sudo().create({
                        "sku": sku,
                        "quantity": quantity,
                    })

            for sku, record in existing.items():
                if zero_missing and sku not in incoming and record.quantity:
                    record.write({"quantity": 0})
                    zeroed += 1

        return {
            "imported": len(incoming),
            "zeroed": zeroed,
        }

    @api.model
    def import_supplier_xlsx(self, content, *, zero_missing=False):
        """Import a complete supplier availability Excel snapshot."""
        from io import BytesIO
        import re
        from openpyxl import load_workbook

        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=True,
        )
        try:
            sheet = workbook["Order"]
            rows = sheet.iter_rows(values_only=True)
            headers = [
                str(value or "").strip()
                for value in next(rows)
            ]

            required = ("Internal Reference", "Availability")
            if not all(name in headers for name in required):
                raise ValidationError(
                    self.env._("Required Excel columns are missing.")
                )

            sku_col = headers.index("Internal Reference")
            qty_col = headers.index("Availability")
            incoming = []

            for line_no, row in enumerate(rows, start=2):
                if not any(value is not None for value in row):
                    continue

                sku = str(row[sku_col] or "").strip()
                value = str(row[qty_col] or "").strip()

                if not sku or not value:
                    raise ValidationError(
                        self.env._("Missing SKU or availability at row %s", line_no)
                    )

                if value.lower() == "out of stock":
                    quantity = 0
                elif re.fullmatch(
                    r"over\s+15(?:\.0+)?\s+units available",
                    value,
                    re.IGNORECASE,
                ):
                    quantity = 15
                else:
                    match = re.fullmatch(
                        r"(\d+)\s+units? available",
                        value,
                        re.IGNORECASE,
                    )
                    if not match:
                        raise ValidationError(
                            self.env._(
                                "Unknown availability '%(value)s' at row %(row)s",
                                value=value,
                                row=line_no,
                            )
                        )
                    quantity = int(match.group(1))

                incoming.append({"sku": sku, "quantity": quantity})

            if zero_missing and len(incoming) < 1000:
                raise ValidationError(
                    self.env._(
                        "Full supplier snapshot has fewer than 1000 rows. "
                        "Import refused to prevent accidental stock zeroing."
                    )
                )

            return self.replace_supplier_snapshot(incoming, zero_missing=zero_missing)
        finally:
            workbook.close()

    @api.constrains("quantity")
    def _check_quantity(self):
        for record in self:
            if record.quantity < 0:
                raise ValidationError(
                    self.env._("Supplier availability cannot be negative.")
                )
