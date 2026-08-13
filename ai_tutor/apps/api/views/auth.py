"""Auth endpoints under /api/v1/auth/*.

  POST /api/v1/auth/login/    — issue access + refresh tokens
  POST /api/v1/auth/register/ — create student account + tokens
  POST /api/v1/auth/refresh/  — rotate access token
  POST /api/v1/auth/logout/   — blacklist refresh token
  GET  /api/v1/me/            — current user + memberships + profile
"""

from rest_framework import status
from rest_framework.decorators import (
    api_view, permission_classes, authentication_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from ai_tutor.apps.api.serializers.auth import (
    LoginSerializer, RegisterSerializer, UserSerializer,
)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def login(request):
    ser = LoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    return Response(ser.save(), status=status.HTTP_200_OK)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def register(request):
    ser = RegisterSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    return Response(ser.save(), status=status.HTTP_201_CREATED)


# Token refresh — use simplejwt's built-in view directly.
refresh = TokenRefreshView.as_view()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """Blacklist the supplied refresh token so it can't be reused."""
    refresh_token = request.data.get('refresh')
    if not refresh_token:
        return Response({'detail': 'refresh token required'}, status=400)
    try:
        token = RefreshToken(refresh_token)
        token.blacklist()
    except (TokenError, InvalidToken):
        return Response({'detail': 'invalid or already-blacklisted token'}, status=400)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)
