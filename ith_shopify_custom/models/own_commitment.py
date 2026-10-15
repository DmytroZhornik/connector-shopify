"""Own-location commitment ledger for the bounded joint dispatcher.

The ledger is intentionally separate from ``ith.external.stock.movement``:
External reconciliation ignores the Own location, while this model accounts for
unreflected Own reservations and shipments when availability is calculated.
It never creates quants, confirms orders, or calls Shopify.
"""

from datetime import datetime
import hashlib
import json
import math

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .joint_policy import JOINT_LOCK, quantity
from .own_commitment_policy import total_unreflected_extra


OWN_LOCATION_GID = "gid://shopify/Location/103244792194"
EXTERNAL_LOCATION_GID = "gid://shopify/Location/103676412290"
KNOWN_LOCATION_GIDS = frozenset((OWN_LOCATION_GID, EXTERNAL_LOCATION_GID))
_REQUIRED_MOVE_FIELDS = frozenset((
    "sale_line_id", "quantity", "product_uom", "location_id",
    "location_dest_id", "state", "product_id",
))
_ZERO_RELEASE_PROOF_CONTEXT = "_ith_joint_zero_release_proof"
_ZERO_RELEASE_PAYLOAD_CONTEXT = "_ith_joint_zero_release_payload"


class ITHOwnCommitment(models.Model):
    """A cumulative Shopify Own allocation linked to one canonical sale line."""

    _name = "ith.own.commitment"
    _description = "ITH Own Physical Commitment"
    _order = "id"
    _check_company_auto = True

    instance_id = fields.Many2one(
        "shopify.instance", required=True, index=True, ondelete="restrict",
        readonly=True,
    )
    company_id = fields.Many2one(
        "res.company", related="instance_id.company_id", store=True,
        readonly=True, index=True,
    )
    order_binding_id = fields.Many2one(
        "shopify.order", required=True, index=True, check_company=True,
        ondelete="restrict", readonly=True,
    )
    source_line_key = fields.Char(required=True, index=True, readonly=True)
    product_id = fields.Many2one(
        "product.product", required=True, index=True, check_company=True,
        ondelete="restrict", readonly=True,
    )
    sale_line_id = fields.Many2one(
        "sale.order.line", index=True, check_company=True,
        ondelete="set null", readonly=True,
    )
    reserved_quantity = fields.Float(required=True, default=0.0, readonly=True)
    fulfilled_quantity = fields.Float(required=True, default=0.0, readonly=True)
    order_quantity = fields.Float(required=True, default=0.0, readonly=True)
    generation = fields.Char(required=True, readonly=True, index=True)
    snapshot_digest = fields.Char(required=True, readonly=True, index=True)

    _unique_source_line = models.Constraint(
        "UNIQUE(instance_id, source_line_key)",
        "An Own commitment already exists for this Shopify source line.",
    )

    @api.model
    def _ith_joint_quantity(self, value, label):
        """Use the shared joint arithmetic and translate policy failures."""
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or int(value) != value
        ):
            raise UserError(self.env._("%(label)s must be a finite non-negative integer.", label=label))
        try:
            result = quantity(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise UserError(self.env._("Invalid %(label)s.", label=label)) from error
        if type(result) is not int or result < 0:
            raise UserError(self.env._("Invalid %(label)s.", label=label))
        return result

    @api.model
    def _ith_joint_validate_scope(self, scope):
        """Accept only an already asserted joint scope; never manufacture one."""
        if (
            not isinstance(scope, dict)
            or type(scope.get("instance_id")) is not int
            or scope["instance_id"] != 1
            or type(scope.get("company_id")) is not int
            or scope["company_id"] <= 0
            or not isinstance(scope.get("order_ids"), list)
            or not scope["order_ids"]
            or not all(isinstance(order_id, str) and order_id for order_id in scope["order_ids"])
            or not isinstance(scope.get("stock"), dict)
        ):
            raise UserError(self.env._("The asserted joint scope is incomplete."))
        return scope

    @api.model
    def _ith_joint_scope_item(self, scope, product):
        """Return the one approved stock item for the exact canonical product."""
        stock_scope = scope["stock"]
        generation = stock_scope.get("generation")
        items = stock_scope.get("items")
        sku = product.default_code
        if (
            not isinstance(generation, str)
            or not generation
            or not isinstance(items, list)
            or not isinstance(sku, str)
            or not sku
        ):
            raise UserError(self.env._("The joint stock scope lacks a canonical product identity."))
        matches = [
            item for item in items
            if isinstance(item, dict)
            and item.get("stock_product_id") == product.id
            and item.get("sku") == sku
        ]
        if len(matches) != 1:
            raise UserError(self.env._(
                "The sale-line product is not represented by exactly one approved Own stock item."
            ))
        if product.company_id and product.company_id.id != scope["company_id"]:
            raise UserError(self.env._("The canonical product belongs to another company."))
        return matches[0]

    @api.model
    def _ith_joint_canonical_line(self, scope, order, source_line_key):
        """Resolve one, and only one, canonical Shopify line without SKU guessing."""
        if not order or not order.exists() or not order.odoo_id:
            raise UserError(self.env._("The Shopify order has no canonical quotation."))
        if (
            order.instance_id.id != scope["instance_id"]
            or order.company_id.id != scope["company_id"]
            or order.odoo_id.company_id.id != scope["company_id"]
        ):
            raise UserError(self.env._("The Shopify order is outside the joint company/instance."))
        expected_prefix = f"{scope['instance_id']}:"
        if not isinstance(source_line_key, str) or not source_line_key.startswith(expected_prefix):
            raise UserError(self.env._("The Own commitment source line has the wrong instance."))
        shopify_line_id = source_line_key[len(expected_prefix):]
        if not shopify_line_id:
            raise UserError(self.env._("The Own commitment source line is missing."))
        line_bindings = order.line_binding_ids.filtered(
            lambda line: (
                line.instance_id.id == scope["instance_id"]
                and line.shopify_id == shopify_line_id
            )
        )
        if len(line_bindings) != 1:
            raise UserError(self.env._(
                "Exactly one canonical Shopify sale-line binding is required for Own commitment accounting."
            ))
        line_binding = line_bindings
        sale_line = line_binding.odoo_id
        product = sale_line.product_id
        if (
            line_binding.order_binding_id != order
            or line_binding.company_id.id != scope["company_id"]
            or not sale_line
            or sale_line.order_id != order.odoo_id
            or sale_line.company_id.id != scope["company_id"]
            or not product
            or line_binding.is_shipping
            or line_binding.is_gift_card
            or sale_line.shopify_line_id != shopify_line_id
            or not product.default_code
            or line_binding.sku != product.default_code
        ):
            raise UserError(self.env._(
                "The Shopify line is not a canonical Own stock sale line."
            ))
        self._ith_joint_scope_item(scope, product)
        return line_binding, sale_line, product

    @api.model
    def _ith_joint_validate_quantities(self, reserved, fulfilled, total):
        reserved = self._ith_joint_quantity(reserved, "Reserved quantity")
        fulfilled = self._ith_joint_quantity(fulfilled, "Fulfilled quantity")
        total = self._ith_joint_quantity(total, "Order quantity")
        if total != reserved + fulfilled:
            raise UserError(self.env._(
                "Own commitment order quantity must equal reserved plus fulfilled quantity."
            ))
        return reserved, fulfilled, total

    @api.model
    def _ith_joint_asserted_order_scope(self, snapshot=None):
        """Require the exact currently-started native orders job on every mutation."""
        queue = self.env["queue.job"]
        admitted_snapshot = queue._ith_joint_guard("orders")
        if snapshot is not None and snapshot != admitted_snapshot:
            raise UserError(self.env._("The joint orders job snapshot changed."))
        scope = queue._ith_joint_assert(admitted_snapshot)
        return admitted_snapshot, self._ith_joint_validate_scope(scope)

    @api.model
    def _ith_joint_validate_create_values(self, values, scope):
        """Check the immutable commitment identity before it reaches SQL."""
        required_ids = ("instance_id", "order_binding_id", "product_id", "sale_line_id")
        if any(type(values.get(name)) is not int or values[name] <= 0 for name in required_ids):
            raise UserError(self.env._("Own commitment identity is incomplete."))
        if values["instance_id"] != scope["instance_id"]:
            raise UserError(self.env._("Own commitment has the wrong Shopify instance."))
        order = self.env["shopify.order"].sudo().browse(values["order_binding_id"]).exists()
        if len(order) != 1 or order.shopify_id not in scope["order_ids"]:
            raise UserError(self.env._("Own commitment order is outside the bounded cohort."))
        line_binding, sale_line, product = self._ith_joint_canonical_line(
            scope, order, values.get("source_line_key")
        )
        if values["product_id"] != product.id or values["sale_line_id"] != sale_line.id:
            raise UserError(self.env._("Own commitment does not match its canonical sale line."))
        company_id = values.get("company_id")
        if company_id not in (None, False, scope["company_id"]):
            raise UserError(self.env._("Own commitment has the wrong company."))
        if values.get("generation") != scope["stock"].get("generation"):
            raise UserError(self.env._("Own commitment generation is outside the current stock scope."))
        digest = values.get("snapshot_digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise UserError(self.env._("Own commitment requires an exact snapshot digest."))
        self._ith_joint_validate_quantities(
            values.get("reserved_quantity"), values.get("fulfilled_quantity"),
            values.get("order_quantity"),
        )
        return line_binding

    @api.model
    def _ith_joint_validate_existing_record(self, record, scope):
        """Validate a persisted identity; generation is intentionally not filtered.

        The read-only stock calculator must include old generations, so this
        helper never treats an older generation as a reason to ignore a record.
        """
        if (
            record.instance_id.id != scope["instance_id"]
            or record.company_id.id != scope["company_id"]
            or not record.order_binding_id
            or record.order_binding_id.instance_id.id != scope["instance_id"]
            or record.order_binding_id.company_id.id != scope["company_id"]
        ):
            raise UserError(self.env._("Own commitment identity is outside the joint scope."))
        reserved, fulfilled, total = self._ith_joint_validate_quantities(
            record.reserved_quantity, record.fulfilled_quantity, record.order_quantity,
        )
        if not record.sale_line_id:
            # C1 may unlink a zeroed draft line.  It is safe to retain only an
            # all-zero audit row; a non-zero row has no physical link and must
            # never be used to guess physical cover.
            if reserved or fulfilled or total:
                raise UserError(self.env._(
                    "A non-zero Own commitment lost its sale line; physical cover is unknown."
                ))
            if not record.product_id:
                raise UserError(self.env._("A zero Own commitment lost its canonical product."))
            self._ith_joint_scope_item(scope, record.product_id)
            return self.env["sale.order.line"], record.product_id
        _line_binding, sale_line, product = self._ith_joint_canonical_line(
            scope, record.order_binding_id, record.source_line_key
        )
        if record.sale_line_id != sale_line or record.product_id != product:
            raise UserError(self.env._("Own commitment canonical line changed or is inconsistent."))
        return sale_line, product

    @api.model
    def _ith_joint_validate_missing_line_zero_release(self, record, values, scope):
        """Authorize the one C1-deleted-line release path, or fail closed.

        C1 may unlink a zero-quantity draft/sent sale line before this ledger
        receives the same fresh C2 snapshot.  A stale non-zero audit row cannot
        otherwise pass ``_ith_joint_validate_existing_record`` because it has no
        physical line.  This is deliberately not a boolean context bypass: the
        in-memory C2 proof and its exact payload are required, and the approved
        generic planner is run again for this one previous ledger row.
        """
        target_reserved, target_fulfilled, target_total = self._ith_joint_validate_quantities(
            values.get("reserved_quantity", record.reserved_quantity),
            values.get("fulfilled_quantity", record.fulfilled_quantity),
            values.get("order_quantity", record.order_quantity),
        )
        if (target_reserved, target_fulfilled, target_total) != (0, 0, 0):
            raise UserError(self.env._(
                "A deleted Own sale line can only be released to an all-zero commitment."
            ))

        previous_reserved, previous_fulfilled, previous_total = self._ith_joint_validate_quantities(
            record.reserved_quantity, record.fulfilled_quantity, record.order_quantity,
        )
        if previous_fulfilled:
            raise UserError(self.env._(
                "A deleted Own sale line with a prior shipment cannot be released automatically."
            ))
        if (
            not record.order_binding_id
            or record.instance_id.id != scope["instance_id"]
            or record.company_id.id != scope["company_id"]
            or record.order_binding_id.instance_id.id != scope["instance_id"]
            or record.order_binding_id.company_id.id != scope["company_id"]
            or record.order_binding_id.shopify_id not in scope["order_ids"]
        ):
            raise UserError(self.env._("Own commitment identity is outside the joint scope."))

        product = record.product_id.exists()
        if len(product) != 1:
            raise UserError(self.env._("A deleted Own sale line lost its canonical product."))
        self._ith_joint_scope_item(scope, product)

        proof = self.env.context.get(_ZERO_RELEASE_PROOF_CONTEXT)
        payload = self.env.context.get(_ZERO_RELEASE_PAYLOAD_CONTEXT)
        from ..shopify_external_query import (
            _has_fresh_external_order_read_proof,
            plan_external_snapshot_reconciliation,
        )

        try:
            fresh = _has_fresh_external_order_read_proof(
                proof,
                record.instance_id.id,
                record.order_binding_id.shopify_id,
                payload,
            )
        except (TypeError, ValueError) as error:
            raise UserError(self.env._("The deleted-line release proof is invalid.")) from error
        if not fresh:
            raise UserError(self.env._(
                "A deleted Own sale line requires its exact fresh Shopify read proof."
            ))
        read_started_at = getattr(proof, "read_started_at", None)
        if (
            not isinstance(read_started_at, datetime)
            or read_started_at.tzinfo is not None
            or not isinstance(record.write_date, datetime)
            or record.write_date.tzinfo is not None
            or read_started_at < record.write_date
        ):
            raise UserError(self.env._(
                "The fresh Shopify read predates the deleted Own sale-line commitment."
            ))

        try:
            payload_digest = hashlib.sha256(json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode()).hexdigest()
        except (TypeError, ValueError) as error:
            raise UserError(self.env._("The deleted-line release payload cannot be fingerprinted.")) from error
        if values.get("snapshot_digest", record.snapshot_digest) != payload_digest:
            raise UserError(self.env._(
                "The deleted-line release digest does not match its fresh Shopify payload."
            ))

        previous = [{
            "source_line_key": record.source_line_key,
            "order_quantity": previous_total,
            "fulfilled_quantity": previous_fulfilled,
            "write_date": record.write_date,
        }]
        try:
            targets = plan_external_snapshot_reconciliation(
                payload,
                {OWN_LOCATION_GID},
                KNOWN_LOCATION_GIDS,
                previous,
                instance_id=record.instance_id.id,
                shopify_id=record.order_binding_id.shopify_id,
                release_proof=proof,
            )
            allowed = targets.get(record.source_line_key)
            if not isinstance(allowed, dict):
                raise ValueError("Missing deleted-line release target")
            allowed_quantities = self._ith_joint_validate_quantities(
                allowed.get("reserved"), allowed.get("fulfilled"), allowed.get("total"),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise UserError(self.env._(
                "The deleted Own sale line cannot safely be released: %s", error
            )) from error
        if allowed_quantities != (0, 0, 0):
            raise UserError(self.env._(
                "The fresh Shopify snapshot does not authorize the deleted Own sale-line release."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        _snapshot, scope = self._ith_joint_asserted_order_scope()
        for values in vals_list:
            self._ith_joint_validate_create_values(dict(values), scope)
        return super().create(vals_list)

    def write(self, values):
        _snapshot, scope = self._ith_joint_asserted_order_scope()
        immutable = {
            "instance_id", "company_id", "order_binding_id", "source_line_key",
            "product_id", "sale_line_id",
        }
        if immutable.intersection(values):
            raise UserError(self.env._("Own commitment identity is immutable."))
        allowed = {
            "reserved_quantity", "fulfilled_quantity", "order_quantity",
            "generation", "snapshot_digest",
        }
        if set(values).difference(allowed):
            raise UserError(self.env._("Unsupported Own commitment update."))
        deleted_line_zero_releases = self.browse()
        for record in self:
            # This is intentionally a DML-only cohort guard.  The read-only
            # availability calculation includes valid older generations and
            # must not be narrowed to the current orders-job cohort.
            if (
                not record.order_binding_id
                or record.order_binding_id.shopify_id not in scope["order_ids"]
            ):
                raise UserError(self.env._("Own commitment order is outside the bounded cohort."))
            generation = values.get("generation", record.generation)
            digest = values.get("snapshot_digest", record.snapshot_digest)
            if generation != scope["stock"].get("generation"):
                raise UserError(self.env._("Own commitment generation is outside the current stock scope."))
            if not isinstance(digest, str) or len(digest) != 64:
                raise UserError(self.env._("Own commitment requires an exact snapshot digest."))
            target_quantities = self._ith_joint_validate_quantities(
                values.get("reserved_quantity", record.reserved_quantity),
                values.get("fulfilled_quantity", record.fulfilled_quantity),
                values.get("order_quantity", record.order_quantity),
            )
            if record.sale_line_id:
                self._ith_joint_validate_existing_record(record, scope)
                continue

            previous_quantities = self._ith_joint_validate_quantities(
                record.reserved_quantity, record.fulfilled_quantity, record.order_quantity,
            )
            if previous_quantities == (0, 0, 0):
                self._ith_joint_validate_existing_record(record, scope)
                if target_quantities != (0, 0, 0):
                    raise UserError(self.env._(
                        "A deleted Own sale line cannot be reactivated."
                    ))
                continue

            self._ith_joint_validate_missing_line_zero_release(record, values, scope)
            deleted_line_zero_releases |= record

        result = super().write(values)
        # A capability-gated release leaves a legitimate all-zero audit row.
        # Re-run the normal read-only validator after mutation; this also keeps
        # the null link incapable of contributing guessed physical cover.
        for record in deleted_line_zero_releases:
            self._ith_joint_validate_existing_record(record, scope)
        return result

    def unlink(self):
        raise UserError(self.env._("Own commitments are immutable audit records and cannot be deleted."))

    @api.constrains("reserved_quantity", "fulfilled_quantity", "order_quantity")
    def _check_ith_commitment_quantities(self):
        for record in self:
            try:
                self._ith_joint_validate_quantities(
                    record.reserved_quantity, record.fulfilled_quantity,
                    record.order_quantity,
                )
            except UserError as error:
                raise ValidationError(str(error)) from error

    @api.model
    def _ith_joint_move_quantity(self, move, product):
        """Convert Odoo 19's stock.move move-line aggregate to product UoM."""
        raw = move.quantity
        if (
            type(raw) not in (int, float)
            or not math.isfinite(raw)
            or raw < 0
            or not move.product_uom
            or not product.uom_id
        ):
            raise UserError(self.env._("A physical stock move has an invalid quantity or UoM."))
        try:
            converted = move.product_uom._compute_quantity(
                raw, product.uom_id, round=False,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise UserError(self.env._("Physical move UoM conversion is unavailable.")) from error
        return self._ith_joint_quantity(converted, "Physical move quantity")

    @api.model
    def _ith_joint_physical_cover(self, sale_line, product):
        """Read, but never alter, physical outbound cover for a canonical line."""
        StockMove = self.env["stock.move"].sudo()
        missing = _REQUIRED_MOVE_FIELDS.difference(StockMove._fields)
        if missing:
            raise UserError(self.env._(
                "Native stock.move fields are required for Own cover accounting: %s",
                ", ".join(sorted(missing)),
            ))
        moves = StockMove.search([
            ("sale_line_id", "=", sale_line.id),
            ("product_id", "=", product.id),
            ("location_id", "child_of", 5),
            ("location_dest_id.usage", "=", "customer"),
        ])
        reserved = done = 0
        for move in moves:
            if move.state == "cancel":
                continue
            move_quantity = self._ith_joint_move_quantity(move, product)
            if move.state in ("assigned", "partially_available"):
                reserved += move_quantity
            elif move.state == "done":
                done += move_quantity
            elif move_quantity:
                raise UserError(self.env._(
                    "A non-final physical outbound move has quantity; Own cover cannot guess its state."
                ))
        return reserved, done

    @api.model
    def _ith_joint_extra_for_product(self, product_id, scope):
        """Return only the unreflected Own extra for an approved stock product.

        The caller is responsible for providing a freshly asserted, read-only
        joint scope while holding ``JOINT_LOCK``.  This method deliberately
        performs no queue admission, API request, or ORM write: it is safe for
        the fresh READ COMMITTED cursor used by the stock calculation.
        """
        scope = self._ith_joint_validate_scope(scope)
        if type(product_id) is not int or product_id <= 0:
            raise UserError(self.env._("Own cover needs one canonical product."))
        product = self.env["product.product"].sudo().browse(product_id).exists()
        if len(product) != 1:
            raise UserError(self.env._("Own cover product is missing or ambiguous."))
        self._ith_joint_scope_item(scope, product)

        # No generation predicate: reservations from older accepted cohorts
        # remain deducted until their physical outbound cover catches up.
        commitments = self.sudo().search([
            ("instance_id", "=", scope["instance_id"]),
            ("company_id", "=", scope["company_id"]),
            ("product_id", "=", product.id),
        ])
        lines = []
        seen_sale_lines = set()
        for commitment in commitments:
            sale_line, committed_product = self._ith_joint_validate_existing_record(
                commitment, scope
            )
            if committed_product != product:
                raise UserError(self.env._("Own commitment product changed during calculation."))
            if not sale_line:
                # _ith_joint_validate_existing_record permits this only for an
                # all-zero row left after C1 unlinked a draft sale line.  It
                # contributes no extra and cannot be assigned guessed cover.
                continue
            if sale_line.id in seen_sale_lines:
                raise UserError(self.env._(
                    "More than one Own commitment claims one physical sale line."
                ))
            seen_sale_lines.add(sale_line.id)
            physical_reserved, physical_done = self._ith_joint_physical_cover(
                sale_line, product
            )
            lines.append({
                "sale_line_id": sale_line.id,
                "reserved": self._ith_joint_quantity(
                    commitment.reserved_quantity, "Reserved quantity"
                ),
                "fulfilled": self._ith_joint_quantity(
                    commitment.fulfilled_quantity, "Fulfilled quantity"
                ),
                "physical_reserved": physical_reserved,
                "physical_done": physical_done,
            })
        try:
            return total_unreflected_extra(lines)
        except ValueError as error:
            raise UserError(self.env._("Invalid Own physical-cover arithmetic.")) from error


class ShopifyOrderOwnCommitment(models.Model):
    """Apply a fresh C2-compatible Own allocation snapshot to the local ledger."""

    _inherit = "shopify.order"

    def _ith_joint_snapshot_digest(self, payload):
        try:
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise UserError(self.env._("The Own snapshot cannot be fingerprinted.")) from error
        return hashlib.sha256(encoded).hexdigest()

    def _ith_joint_apply_own_snapshot(self, payload, proof, snapshot):
        """Reconcile Own ledger rows after C1 and C2 used one fresh payload.

        C2 remains the sole External ledger writer.  The existing C2 planner is
        reused with Own as its selected location, so all pagination, unknown
        location, release, shipment, and stale-write-date protections stay
        identical instead of being copied here.
        """
        self.ensure_one()
        Commitment = self.env["ith.own.commitment"].sudo()

        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (JOINT_LOCK,)
        )
        admitted_snapshot, scope = Commitment._ith_joint_asserted_order_scope(snapshot)
        if (
            self.instance_id.id != scope["instance_id"]
            or self.company_id.id != scope["company_id"]
            or self.shopify_id not in scope["order_ids"]
            or not self.odoo_id
        ):
            raise UserError(self.env._("The Shopify order is outside the bounded joint cohort."))
        self.odoo_id._ith_check_mapping()

        # The joint native reader augments EXTERNAL_ORDER_QUERY with updatedAt.
        # The read proof binds that exact payload object and digest; accepting a
        # payload without updatedAt would make its freshness unverifiable here.
        if (
            not isinstance(payload, dict)
            or payload.get("id") != self.shopify_id
            or not isinstance(payload.get("updatedAt"), str)
            or not payload["updatedAt"].strip()
        ):
            raise UserError(self.env._("Own reconciliation requires the fresh Shopify updatedAt snapshot."))

        from ..shopify_external_query import (
            _has_fresh_external_order_read_proof,
            plan_external_snapshot_reconciliation,
        )

        try:
            fresh = _has_fresh_external_order_read_proof(
                proof, self.instance_id.id, self.shopify_id, payload,
            )
        except (TypeError, ValueError) as error:
            raise UserError(self.env._("Own reconciliation has an invalid fresh-read proof.")) from error
        if not fresh:
            raise UserError(self.env._("Own reconciliation requires its exact fresh-read proof."))

        with self.env.cr.savepoint():
            # Read after the joint lock.  Passing write_date into the shared
            # planner rejects a release if an Own row changed during this read.
            Commitment.invalidate_model([
                "reserved_quantity", "fulfilled_quantity", "order_quantity",
                "write_date", "generation", "snapshot_digest",
            ])
            previous = Commitment.search([
                ("instance_id", "=", self.instance_id.id),
                ("order_binding_id", "=", self.id),
            ])
            previous_values = [
                {
                    "source_line_key": record.source_line_key,
                    "order_quantity": record.order_quantity,
                    "fulfilled_quantity": record.fulfilled_quantity,
                    "write_date": record.write_date,
                }
                for record in previous
            ]
            try:
                targets = plan_external_snapshot_reconciliation(
                    payload,
                    {OWN_LOCATION_GID},
                    KNOWN_LOCATION_GIDS,
                    previous_values,
                    instance_id=self.instance_id.id,
                    shopify_id=self.shopify_id,
                    release_proof=proof,
                )
            except (TypeError, ValueError) as error:
                raise UserError(self.env._("Own snapshot cannot safely reconcile: %s", error)) from error

            previous_by_key = {}
            for record in previous:
                if record.source_line_key in previous_by_key:
                    raise UserError(self.env._("Duplicate Own commitment source line."))
                previous_by_key[record.source_line_key] = record

            snapshot_digest = self._ith_joint_snapshot_digest(payload)
            generation = scope["stock"].get("generation")
            if not isinstance(generation, str) or not generation:
                raise UserError(self.env._("The joint stock generation is missing."))
            created = updated = unchanged = 0
            for source_line_key in sorted(targets):
                target = targets[source_line_key]
                try:
                    reserved, fulfilled, total = Commitment._ith_joint_validate_quantities(
                        target["reserved"], target["fulfilled"], target["total"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise UserError(self.env._("Invalid Own target quantities.")) from error
                existing = previous_by_key.get(source_line_key)
                if not existing and not total:
                    # C1 may already have unlinked an independently zeroed
                    # draft/sent line.  A new zero target has never affected
                    # this ledger, so it must not resolve (or guess) a line.
                    unchanged += 1
                    continue
                deleted_line_zero_release = False
                if existing and not existing.sale_line_id:
                    if total:
                        raise UserError(self.env._(
                            "A deleted Own sale line cannot be reactivated without a new canonical commitment."
                        ))
                    # Only a pre-existing non-zero audit row needs the narrow
                    # capability-gated path in write().  An already zeroed row
                    # can and will pass the regular read-only validator below.
                    deleted_line_zero_release = bool(
                        existing.reserved_quantity
                        or existing.fulfilled_quantity
                        or existing.order_quantity
                    )
                    product = existing.product_id
                    sale_line = existing.sale_line_id
                else:
                    _line_binding, sale_line, product = Commitment._ith_joint_canonical_line(
                        scope, self, source_line_key
                    )
                values = {
                    "instance_id": self.instance_id.id,
                    "order_binding_id": self.id,
                    "source_line_key": source_line_key,
                    "product_id": product.id,
                    "sale_line_id": sale_line.id,
                    "reserved_quantity": reserved,
                    "fulfilled_quantity": fulfilled,
                    "order_quantity": total,
                    "generation": generation,
                    "snapshot_digest": snapshot_digest,
                }
                if not existing:
                    # C1 is safe for both an unconfirmed quotation and a sent
                    # quotation.  This never confirms or otherwise activates
                    # the sale order.
                    if self.odoo_id.state not in ("draft", "sent"):
                        raise UserError(self.env._(
                            "Only a draft or sent quotation can create an Own commitment."
                        ))
                    Commitment.create(values)
                    created += 1
                    continue
                if not deleted_line_zero_release:
                    Commitment._ith_joint_validate_existing_record(existing, scope)
                changes = {
                    name: value for name, value in values.items()
                    if name not in {
                        "instance_id", "order_binding_id", "source_line_key",
                        "product_id", "sale_line_id",
                    }
                    and getattr(existing, name) != value
                }
                if changes:
                    writer = existing
                    if deleted_line_zero_release:
                        # This context contains the actual non-serializable C2
                        # proof plus the same payload object.  write() verifies
                        # both again and re-plans before it may zero a deleted
                        # C1 line; no boolean bypass is accepted.
                        writer = writer.with_context(**{
                            _ZERO_RELEASE_PROOF_CONTEXT: proof,
                            _ZERO_RELEASE_PAYLOAD_CONTEXT: payload,
                        })
                    writer.write(changes)
                    updated += 1
                else:
                    unchanged += 1

        # Reassert exact current job/scope after the only mutation boundary.
        Commitment._ith_joint_asserted_order_scope(admitted_snapshot)
        return {"created": created, "updated": updated, "unchanged": unchanged}
