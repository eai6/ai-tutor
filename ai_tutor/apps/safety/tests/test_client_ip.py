"""Client-IP resolution behind a load balancer.

The last X-Forwarded-For hop is trusted because the app is only ever reachable
through the load balancer — on Azure because the Container App is VNet-internal
behind App Gateway, on AWS because the ECS task security group accepts traffic
only from the ALB security group. If either of those is loosened, none of these
guarantees hold.
"""
from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase

from ai_tutor.apps.safety.client_ip import get_client_ip


class ClientIPTests(SimpleTestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def test_alb_appends_a_bare_ip_and_we_take_it(self):
        """An ALB appends the connecting client without a port, unlike
        App Gateway."""
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7")

        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_app_gateway_style_port_suffix_is_stripped(self):
        """App Gateway formats its hop as ip:port, and an ALB does too when
        routing.http.xff_client_port.enabled is turned on. Postgres inet
        columns reject the suffix."""
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7:59633")

        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_a_spoofed_leading_entry_is_ignored(self):
        """The leftmost entry is client-supplied and forgeable. Only the
        hop appended by the load balancer is trustworthy."""
        request = self.factory.get(
            "/", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.7"
        )

        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_a_long_spoofed_chain_still_yields_the_last_hop(self):
        request = self.factory.get(
            "/", HTTP_X_FORWARDED_FOR="9.9.9.9, 8.8.8.8, 1.1.1.1, 203.0.113.7"
        )

        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_ipv6_survives_intact(self):
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="2001:db8::1")

        self.assertEqual(get_client_ip(request), "2001:db8::1")

    def test_bracketed_ipv6_with_a_port_is_unwrapped(self):
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="[2001:db8::1]:443")

        self.assertEqual(get_client_ip(request), "2001:db8::1")

    def test_no_forwarded_header_falls_back_to_remote_addr(self):
        request = self.factory.get("/", REMOTE_ADDR="10.30.1.5")

        self.assertEqual(get_client_ip(request), "10.30.1.5")

    def test_a_garbage_last_hop_falls_back_to_remote_addr(self):
        request = self.factory.get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.7, not-an-ip", REMOTE_ADDR="10.30.1.5"
        )

        self.assertEqual(get_client_ip(request), "10.30.1.5")

    def test_a_garbage_header_with_no_remote_addr_yields_none(self):
        """Postgres inet columns reject junk, so None keeps the column NULL
        rather than raising a DataError mid-request."""
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="not-an-ip")
        request.META.pop("REMOTE_ADDR", None)

        self.assertIsNone(get_client_ip(request))
