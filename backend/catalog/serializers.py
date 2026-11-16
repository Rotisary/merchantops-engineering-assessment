import re

from rest_framework import serializers

from catalog.models import AuditLog, Category, InventoryAdjustment, Product

MAX_INTEGER = 2147483647


class ProductListSerializer(serializers.ModelSerializer):
    category = serializers.CharField(
        source="category.name",
        read_only=True,
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "sku",
            "status",
            "category",
            "image_url",
        ]


class ProductUpdateSerializer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "sku",
            "description",
            "status",
            "category",
            "image_url",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        allowed = set(self.fields)
        unexpected = set(self.initial_data) - allowed

        # Read-only fields must not be submitted either.
        unexpected |= set(self.initial_data) & {"id"}

        if unexpected:
            raise serializers.ValidationError(
                {
                    field: "This field cannot be modified."
                    for field in sorted(unexpected)
                }
            )

        return attrs

    def validate_sku(self, value):
        product = self.instance

        duplicates = Product.objects.filter(
            merchant_id=product.merchant_id,
            sku=value,
        ).exclude(pk=product.pk)

        if duplicates.exists():
            raise serializers.ValidationError(
                "A product with this SKU already exists " "for this merchant."
            )

        return value

    def validate_category(self, category):
        if category.merchant_id != self.instance.merchant_id:
            raise serializers.ValidationError(
                "Category does not belong to this merchant."
            )

        return category

    def validate_image_url(self, value):
        if value and not re.fullmatch(
            r"/product-images/[a-z0-9][a-z0-9_-]*\.webp",
            value,
        ):
            raise serializers.ValidationError(
                "Expected a local /product-images/*.webp path."
            )

        return value


class ProductActivitySerializer(serializers.ModelSerializer):
    actor = serializers.CharField(
        source="actor.username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "actor",
            "action",
            "details",
            "created_at",
        ]


class InventoryAdjustmentInputSerializer(serializers.Serializer):
    delta = serializers.IntegerField(min_value=-MAX_INTEGER - 1, max_value=MAX_INTEGER)
    reason = serializers.CharField(max_length=255)

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Adjustment must not be zero.")

        return value

    def validate(self, attrs):
        unexpected = set(self.initial_data) - set(self.fields)

        if unexpected:
            raise serializers.ValidationError(
                {
                    field: "This field cannot be submitted."
                    for field in sorted(unexpected)
                }
            )

        return attrs


class InventoryAdjustmentSerializer(serializers.ModelSerializer):
    variant_id = serializers.IntegerField(read_only=True)
    actor = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = InventoryAdjustment
        fields = [
            "id",
            "variant_id",
            "delta",
            "quantity_before",
            "quantity_after",
            "reason",
            "actor",
            "created_at",
        ]
