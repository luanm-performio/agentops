from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .tenant_lookup import TenantLookupResult


class LocateTenantViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="operator@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    def test_locate_tenant_page_renders_form(self) -> None:
        response = self.client.get(reverse("locate_tenant"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Locate Tenant")
        self.assertContains(response, 'name="host_name"')

    def test_locate_tenant_requires_host_name(self) -> None:
        response = self.client.post(
            reverse("locate_tenant"),
            {"host_name": ""},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Enter a host name to search.", status_code=422)

    @patch("agents.views.locate_tenants")
    def test_locate_tenant_displays_lookup_results(
        self, locate_tenants_mock: Mock
    ) -> None:
        locate_tenants_mock.return_value = [
            TenantLookupResult(
                shard_hosts="acme.example.com,acme-alt.example.com",
                database_server_url="mysql.example.internal",
                schema_name="tenant_acme",
                region="region-db.example.internal",
                name="Production",
            )
        ]

        response = self.client.post(
            reverse("locate_tenant"),
            {"host_name": "acme"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        locate_tenants_mock.assert_called_once_with("acme")
        self.assertContains(response, "acme.example.com")
        self.assertContains(response, "mysql.example.internal")
        self.assertContains(response, "tenant_acme")
        self.assertContains(response, 'aria-label="Copy row"')
        self.assertContains(response, 'aria-label="Copy shard hosts"')
        self.assertContains(
            response, 'data-copy-value="mysql.example.internal tenant_acme"'
        )
        self.assertContains(response, 'data-copy-value="mysql.example.internal"')
        self.assertNotContains(response, "password")

    @patch("agents.views.locate_tenants", side_effect=RuntimeError("boom"))
    def test_locate_tenant_shows_lookup_errors(self, locate_tenants_mock: Mock) -> None:
        response = self.client.post(
            reverse("locate_tenant"),
            {"host_name": "acme"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 500)
        locate_tenants_mock.assert_called_once_with("acme")
        self.assertContains(
            response,
            "Tenant lookup failed. Check the server logs for details.",
            status_code=500,
        )
