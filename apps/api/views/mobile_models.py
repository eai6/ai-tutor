"""GET /api/v1/mobile/models/ — on-device model catalog."""

from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers

from apps.api.permissions import IsInstitutionMember
from apps.llm.models import MobileInferenceModel


class MobileInferenceModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = MobileInferenceModel
        fields = [
            'id', 'display_name', 'family', 'size_mb', 'ram_required_mb',
            'download_url', 'checksum_sha256', 'license', 'runtime',
            'capabilities', 'chat_template', 'system_prompt_style',
            'recommended_tier', 'quality_scores', 'min_app_version',
        ]


class MobileModelList(generics.ListAPIView):
    """List active models filtered by ?device_tier=flagship|mid|low.

    No tier filter → returns all active models. The client picks based
    on its own device-tier detection (see memory/mobile_rn_plan.md).
    """
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = MobileInferenceModelSerializer

    def get_queryset(self):
        qs = MobileInferenceModel.objects.filter(is_active=True)
        tier = self.request.query_params.get('device_tier')
        if tier in {'flagship', 'mid', 'low'}:
            # Flagship can run anything; mid can run mid + low; low only
            # gets low-tier suggestions.
            allow = {'flagship': ['flagship', 'mid', 'low'],
                     'mid': ['mid', 'low'],
                     'low': ['low']}[tier]
            qs = qs.filter(recommended_tier__in=allow)
        return qs
