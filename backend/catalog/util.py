import re

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from catalog.exceptions import StockConflict
from catalog.models import Inventory, InventoryAdjustment, Merchant
from catalog.serializers import MAX_INTEGER, InventoryAdjustmentSerializer

IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")


def member_merchant_id(request):
    merchant_id = request.query_params.get("merchant_id")

    if not merchant_id:
        raise ValidationError({"merchant_id": "This parameter is required."})

    try:
        merchant_id = int(merchant_id)
    except (TypeError, ValueError):
        raise ValidationError({"merchant_id": "Must be a valid integer."})

    if merchant_id <= 0:
        raise ValidationError({"merchant_id": "Must be a positive integer."})

    get_object_or_404(
        Merchant.objects.filter(members=request.user),
        pk=merchant_id,
    )

    return merchant_id


def idempotency_key(request):
    key = request.headers.get("Idempotency-Key")

    if key is not None and not IDEMPOTENCY_KEY.fullmatch(key):
        raise ValidationError(
            {
                "idempotency_key": "Idempotency-Key must be 1-255 visible "
                "ASCII characters."
            }
        )

    return key


def previous_adjustment(actor, key, variant, delta, reason):
    """Return the adjustment this key already produced, if it matches."""

    if key is None:
        return None

    existing = InventoryAdjustment.objects.filter(
        actor=actor,
        idempotency_key=key,
    ).first()

    if existing is None:
        return None

    if (existing.variant_id, existing.delta, existing.reason) != (
        variant.pk,
        delta,
        reason,
    ):
        raise StockConflict(
            {
                "idempotency_key": "This Idempotency-Key was already used "
                "for a different adjustment."
            }
        )

    return existing


def apply_adjustment(variant, actor, delta, reason, key):
    """Lock the stock row, then record the change. Returns a replay flag."""

    with transaction.atomic():
        inventory = (
            Inventory.objects.select_for_update().filter(variant=variant).first()
        )

        if inventory is None:
            raise StockConflict({"variant": "This variant has no inventory record."})

        # Checked under the lock, so a concurrent request with the same key
        # sees the row the first one committed.
        existing = previous_adjustment(actor, key, variant, delta, reason)

        if existing is not None:
            return existing, inventory, True

        before = inventory.quantity
        after = before + delta

        if after < inventory.reserved:
            raise StockConflict(
                {
                    "delta": "Stock cannot fall below the "
                    f"{inventory.reserved} units already reserved."
                }
            )

        if after > MAX_INTEGER:
            raise StockConflict({"delta": f"Stock cannot exceed {MAX_INTEGER} units."})

        inventory.quantity = after
        inventory.save(update_fields=["quantity"])

        adjustment = InventoryAdjustment.objects.create(
            variant=variant,
            actor=actor,
            delta=delta,
            quantity_before=before,
            quantity_after=after,
            reason=reason,
            idempotency_key=key,
        )

    return adjustment, inventory, False


def adjustment_body(adjustment, inventory):
    return {
        "adjustment": InventoryAdjustmentSerializer(adjustment).data,
        "inventory": {
            "id": inventory.pk,
            "quantity": inventory.quantity,
            "reserved": inventory.reserved,
        },
    }
