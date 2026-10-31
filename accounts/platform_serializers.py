from rest_framework import serializers

from .identity import USERNAME_TAKEN_MESSAGE, normalize_platform, normalize_username, username_taken
from .models import PlatformUsername


class PlatformUsernameSerializer(serializers.ModelSerializer):
    """Allow users to add and view additional platform usernames."""

    class Meta:
        model = PlatformUsername
        fields = (
            "id",
            "platform_name",
            "username",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate_platform_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Platform name cannot be empty."
            )

        return value

    def validate_username(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Username cannot be empty."
            )

        return value

    def validate(self, attrs):
        request = self.context.get("request")

        if request and request.user.is_authenticated:
            platform_name = attrs.get("platform_name", "").strip()
            username = attrs.get("username", "").strip()

            duplicate_exists = (
                PlatformUsername.objects.filter(
                    user=request.user,
                    platform_name__iexact=platform_name,
                    username__iexact=username,
                ).exists()
            )

            if duplicate_exists:
                raise serializers.ValidationError(
                    "This platform username has already been added."
                )

            # One account per (platform, username), and never another account's primary username.
            if username_taken(normalize_username(username), platform_key=normalize_platform(platform_name), exclude_user_id=request.user.pk):
                raise serializers.ValidationError({"username": USERNAME_TAKEN_MESSAGE})

        return attrs
    