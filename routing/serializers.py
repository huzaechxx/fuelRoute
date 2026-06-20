from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=255)
    finish = serializers.CharField(max_length=255)

    def validate_start(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Start location cannot be blank.")
        return value

    def validate_finish(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Finish location cannot be blank.")
        return value

    def validate(self, data):
        start = data.get("start", "")
        finish = data.get("finish", "")
        if start and finish and start.lower() == finish.lower():
            raise serializers.ValidationError(
                {"finish": "Start and finish must be different locations."}
            )
        return data
