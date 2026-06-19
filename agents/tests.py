from io import BytesIO
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .squelch_lookup import SquelchQueryResult, run_squelch_query
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


class SquelchViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="squelch@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    def test_squelch_page_renders_form(self) -> None:
        response = self.client.get(reverse("squelch"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Squelch")
        self.assertContains(response, 'name="host_name"')
        self.assertContains(response, 'name="query"')
        self.assertContains(response, 'value="au-prod"')
        self.assertContains(response, 'value="us-prod"')
        self.assertContains(response, 'value="local"')

    @patch("agents.views.run_squelch_query")
    def test_squelch_allows_blank_host_name(self, run_squelch_query_mock: Mock) -> None:
        run_squelch_query_mock.return_value = SquelchQueryResult(
            columns=["tenant"],
            rows=[{"tenant": "acme"}],
        )

        response = self.client.post(
            reverse("squelch"),
            {"host_name": "", "query": "select 1", "regions": ["au-prod"]},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run_squelch_query_mock.assert_called_once_with("", "select 1", ["au-prod"])
        self.assertContains(
            response, '<td class="whitespace-nowrap">acme</td>', html=True
        )

    def test_squelch_requires_query(self) -> None:
        response = self.client.post(
            reverse("squelch"),
            {"host_name": "acme", "query": "", "regions": ["au-prod"]},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a query to search.")

    def test_squelch_requires_region(self) -> None:
        response = self.client.post(
            reverse("squelch"),
            {"host_name": "acme", "query": "select 1"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose at least one region.")

    @patch("agents.views.run_squelch_query")
    def test_squelch_displays_table_results(self, run_squelch_query_mock: Mock) -> None:
        run_squelch_query_mock.return_value = SquelchQueryResult(
            columns=["tenant", "count"],
            rows=[{"tenant": "acme", "count": 3}],
        )

        response = self.client.post(
            reverse("squelch"),
            {
                "host_name": "acme",
                "query": "select tenant, count from report",
                "regions": ["au-prod", "us-prod"],
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run_squelch_query_mock.assert_called_once_with(
            "acme", "select tenant, count from report", ["au-prod", "us-prod"]
        )
        self.assertContains(response, "<th>tenant</th>", html=True)
        self.assertContains(
            response, '<td class="whitespace-nowrap">acme</td>', html=True
        )
        self.assertContains(response, '<td class="whitespace-nowrap">3</td>', html=True)
        self.assertContains(response, 'id="squelch-result-filter"')
        self.assertContains(response, "Export Excel")

    @patch("agents.views.run_squelch_query", side_effect=ValueError("Only SELECT"))
    def test_squelch_displays_tool_errors(self, run_squelch_query_mock: Mock) -> None:
        response = self.client.post(
            reverse("squelch"),
            {"host_name": "acme", "query": "delete from thing", "regions": ["au-prod"]},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run_squelch_query_mock.assert_called_once_with(
            "acme", "delete from thing", ["au-prod"]
        )
        self.assertContains(response, "Squelch query failed: Only SELECT")


class SquelchLookupTests(TestCase):
    @patch("tools.squelch.run_squelch")
    def test_run_squelch_query_calls_tool_module(self, run_squelch_mock: Mock) -> None:
        run_squelch_mock.return_value = [{"tenant": "acme", "count": 3}]

        result = run_squelch_query(
            "acme", "select tenant, count from report", ["au-prod"]
        )

        run_squelch_mock.assert_called_once_with(
            host_name="acme",
            query="select tenant, count from report",
            regions=["au-prod"],
        )
        self.assertEqual(result.columns, ["tenant", "count"])
        self.assertEqual(result.rows, [{"tenant": "acme", "count": 3}])


class SquelchExportTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="export@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    @patch("agents.views.run_squelch_query")
    def test_squelch_export_returns_xlsx(self, run_squelch_query_mock: Mock) -> None:
        run_squelch_query_mock.return_value = SquelchQueryResult(
            columns=["tenant", "count"],
            rows=[{"tenant": "acme", "count": 3}],
        )

        response = self.client.post(
            reverse("squelch_export"),
            {
                "host_name": "acme",
                "query": "select tenant, count from report",
                "regions": ["au-prod"],
            },
        )

        self.assertEqual(response.status_code, 200)
        run_squelch_query_mock.assert_called_once_with(
            "acme", "select tenant, count from report", ["au-prod"]
        )
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="squelch-results.xlsx"',
        )
        self.assertTrue(response.content.startswith(b"PK"))
        with ZipFile(BytesIO(response.content)) as workbook:
            sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("tenant", sheet_xml)
        self.assertIn("acme", sheet_xml)

    def test_squelch_export_requires_query(self) -> None:
        response = self.client.post(
            reverse("squelch_export"),
            {"host_name": "acme", "query": "", "regions": ["au-prod"]},
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Enter a query to search.", status_code=422)
