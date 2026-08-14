"""Auth serializers — used by /api/v1/auth/* and /api/v1/me/."""

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile


class MembershipSerializer(serializers.ModelSerializer):
    institution_id = serializers.IntegerField(source='institution.id', read_only=True)
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    institution_slug = serializers.CharField(source='institution.slug', read_only=True)

    class Meta:
        model = Membership
        fields = ['id', 'institution_id', 'institution_name', 'institution_slug', 'role', 'is_active']


class StudentProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentProfile
        fields = [
            'student_id', 'school', 'grade_level',
            'is_tutor_suspended', 'tutor_suspended_reason',
        ]


class UserSerializer(serializers.ModelSerializer):
    memberships = MembershipSerializer(many=True, read_only=True)
    student_profile = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'first_name', 'last_name', 'email',
            'is_staff', 'memberships', 'student_profile',
        ]

    def get_student_profile(self, obj):
        profile = StudentProfile.objects.filter(user=obj).first()
        if not profile:
            return None
        return StudentProfileSerializer(profile).data


class TokenPairSerializer(serializers.Serializer):
    """Output shape for login/register/refresh."""
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserSerializer()


def _issue_tokens(user) -> dict:
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'user': UserSerializer(user).data,
    }


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        # The request is what django-axes keys a failed attempt on. DRF puts it
        # in context when the view passes it; a serializer constructed without
        # one would raise AxesBackendRequestParameterRequired rather than
        # silently skip the lockout, which is the failure mode we want.
        user = authenticate(
            self.context['request'],
            username=attrs['username'], password=attrs['password'],
        )
        if not user:
            raise serializers.ValidationError('Invalid credentials.')
        if not user.is_active:
            raise serializers.ValidationError('Account inactive.')
        # StudentProfile suspension shouldn't block login (teachers may
        # need to lift it via the dashboard) but we surface it on /me/.
        attrs['user'] = user
        return attrs

    def save(self, **kwargs):
        return _issue_tokens(self.validated_data['user'])


class RegisterSerializer(serializers.Serializer):
    """Student self-registration. Creates a User, StudentProfile, and a
    Membership with role=student in the requested institution."""

    username = serializers.CharField(min_length=3, max_length=150)
    password = serializers.CharField(min_length=8, write_only=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    institution_slug = serializers.CharField(required=False, allow_blank=True)
    school = serializers.CharField(required=False, allow_blank=True, max_length=100)
    grade_level = serializers.CharField(required=False, allow_blank=True, max_length=20)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('Username already taken.')
        return value

    def validate(self, attrs):
        slug = (attrs.get('institution_slug') or '').strip()
        institution = None
        if slug:
            institution = Institution.objects.filter(slug=slug, is_active=True).first()
            if not institution:
                raise serializers.ValidationError({'institution_slug': 'Unknown institution.'})
        attrs['_institution'] = institution
        return attrs

    def create(self, validated_data):
        institution = validated_data.pop('_institution', None)
        password = validated_data.pop('password')
        user = User.objects.create_user(
            username=validated_data['username'],
            password=password,
            email=validated_data.get('email', '') or '',
            first_name=validated_data.get('first_name', '') or '',
            last_name=validated_data.get('last_name', '') or '',
        )
        if institution:
            Membership.objects.create(
                user=user, institution=institution, role='student', is_active=True,
            )
        StudentProfile.objects.create(
            user=user,
            school=validated_data.get('school', '') or '',
            grade_level=validated_data.get('grade_level', '') or '',
        )
        return _issue_tokens(user)
