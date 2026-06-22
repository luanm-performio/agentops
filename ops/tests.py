import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from sqlalchemy.exc import SQLAlchemyError

from tools.db_config import DevBox, Region
from tools.tenant import DataSource, Tunnel, find_tenant_data_sources

from .command_registry import run_registered_command
from .commands.backup_download_import import _wait_for_remote_backup
from .lock_monitor import (
    _add_explain_if_long_running,
    _fetch_lock_rows,
    _is_running_process,
    run_lock_monitor,
    summarize_mysql_explain,
)
from .models import (
    CalcLogDashboard,
    CommandRun,
    CommandSchedule,
    LockMonitorCapture,
    LockMonitoringRecord,
)
from .calc_log import CalcLogDashboardResult
from .squelch_lookup import SquelchQueryResult, run_squelch_query
from .tenant_lookup import TenantLookupResult


class CalcLogDashboardTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="calc-log@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    def test_page_lists_saved_dashboards(self) -> None:
        dashboard = CalcLogDashboard.objects.create(
            name="June comparison",
            source_folder="/tmp/calculation-logs",
            source_files=["first.log", "second.log"],
            html_content="<html>dashboard</html>",
        )

        response = self.client.get(reverse("calc_log_dashboards"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parse Calc Log")
        self.assertContains(response, dashboard.name)
        self.assertContains(response, "2 logs")
        self.assertContains(response, 'type="file"')
        self.assertContains(response, "multiple")
        self.assertContains(response, 'accept=".log,text/plain"')
        self.assertContains(
            response,
            reverse("calc_log_dashboard_delete", args=[dashboard.pk]),
        )
        self.assertContains(response, 'hx-confirm="Delete June comparison?"')

    @patch("ops.views.build_calc_log_dashboard")
    def test_post_generates_and_stores_named_dashboard(
        self,
        build_dashboard_mock: Mock,
    ) -> None:
        build_dashboard_mock.return_value = CalcLogDashboardResult(
            html="<html><body>generated dashboard</body></html>",
            source_files=["first.log", "second.log"],
        )
        response = self.client.post(
            reverse("calc_log_dashboards"),
            {
                "name": "June comparison",
                "log_files": [
                    SimpleUploadedFile("first.log", b"log one"),
                    SimpleUploadedFile("second.log", b"log two"),
                ],
            },
        )

        dashboard = CalcLogDashboard.objects.get()
        self.assertRedirects(
            response,
            reverse("calc_log_dashboard_detail", args=[dashboard.pk]),
        )
        self.assertEqual(dashboard.name, "June comparison")
        self.assertEqual(dashboard.source_folder, "")
        self.assertEqual(dashboard.source_files, ["first.log", "second.log"])
        self.assertIn("generated dashboard", dashboard.html_content)
        uploaded_files = build_dashboard_mock.call_args.args[0]
        self.assertEqual(
            [uploaded_file.name for uploaded_file in uploaded_files],
            ["first.log", "second.log"],
        )

    def test_post_requires_log_files(self) -> None:
        response = self.client.post(
            reverse("calc_log_dashboards"),
            {"name": "No files"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(
            response,
            "Select at least one .log file.",
            status_code=422,
        )
        self.assertFalse(CalcLogDashboard.objects.exists())

    def test_post_rejects_non_log_files(self) -> None:
        response = self.client.post(
            reverse("calc_log_dashboards"),
            {
                "name": "Wrong files",
                "log_files": SimpleUploadedFile("analysis.xlsx", b"not a log"),
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(
            response,
            "Only .log files are supported: analysis.xlsx",
            status_code=422,
        )

    def test_detail_embeds_stored_dashboard_and_html_endpoint_serves_it(self) -> None:
        dashboard = CalcLogDashboard.objects.create(
            name="Saved dashboard",
            source_folder="/tmp/calculation-logs",
            source_files=["run.log"],
            html_content="<!doctype html><title>Stored calculation dashboard</title>",
        )

        detail_response = self.client.get(
            reverse("calc_log_dashboard_detail", args=[dashboard.pk])
        )
        html_response = self.client.get(
            reverse("calc_log_dashboard_html", args=[dashboard.pk])
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, dashboard.name)
        self.assertContains(
            detail_response,
            reverse("calc_log_dashboard_html", args=[dashboard.pk]),
        )
        self.assertEqual(html_response.status_code, 200)
        self.assertEqual(html_response["Content-Type"], "text/html; charset=utf-8")
        self.assertContains(html_response, "Stored calculation dashboard")
        self.assertEqual(html_response["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("sandbox", html_response["Content-Security-Policy"])

    def test_delete_removes_dashboard_and_returns_updated_htmx_list(self) -> None:
        dashboard = CalcLogDashboard.objects.create(
            name="Delete me",
            source_folder="",
            source_files=["run.log"],
            html_content="<html>dashboard</html>",
        )

        response = self.client.post(
            reverse("calc_log_dashboard_delete", args=[dashboard.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "partials/_calc_log.html")
        self.assertEqual(response["HX-Push-Url"], reverse("calc_log_dashboards"))
        self.assertFalse(CalcLogDashboard.objects.filter(pk=dashboard.pk).exists())
        self.assertContains(
            response, "No calculation dashboards have been generated yet."
        )

    def test_delete_rejects_get_requests(self) -> None:
        dashboard = CalcLogDashboard.objects.create(
            name="Keep me",
            source_folder="",
            source_files=["run.log"],
            html_content="<html>dashboard</html>",
        )

        response = self.client.get(
            reverse("calc_log_dashboard_delete", args=[dashboard.pk])
        )

        self.assertEqual(response.status_code, 405)
        self.assertTrue(CalcLogDashboard.objects.filter(pk=dashboard.pk).exists())


class TenantDataSourceCredentialTests(TestCase):
    @patch("tools.tenant.select")
    @patch("tools.tenant.Table")
    @patch("tools.tenant.MetaData")
    @patch("tools.tenant.Tunnel")
    def test_tenant_data_sources_use_region_config_credentials(
        self,
        tunnel_mock: Mock,
        metadata_mock: Mock,
        table_mock: Mock,
        select_mock: Mock,
    ) -> None:
        region = Region(
            name="au-prod",
            remote_bind_address="global.example",
            username="config-user",
            password="config-password",
        )
        engine = tunnel_mock.return_value.__enter__.return_value
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.mappings.return_value = [
            {
                "shard_hosts": "tenant.performio.com",
                "username": "tenant-user",
                "password": "tenant-password",
                "database_server_url": "tenant-db.example",
                "schema_name": "tenant_schema",
            }
        ]
        table_mock.return_value.c.shard_hosts.like.return_value = object()
        select_mock.return_value.where.return_value = select_mock.return_value

        data_sources = find_tenant_data_sources("tenant.performio.com", [region])

        self.assertEqual(len(data_sources), 1)
        self.assertEqual(data_sources[0].username, "config-user")
        self.assertEqual(data_sources[0].password, "config-password")
        self.assertEqual(data_sources[0].database_server_url, "tenant-db.example")
        self.assertEqual(data_sources[0].schema_name, "tenant_schema")


class TenantTunnelTests(TestCase):
    @patch("tools.tenant.create_engine")
    @patch("tools.tenant.SSHTunnel")
    def test_devbox_tunnel_targets_tenant_database_server(
        self,
        ssh_tunnel_mock: Mock,
        create_engine_mock: Mock,
    ) -> None:
        devbox = DevBox(
            ca_file="/tmp/ca.pem",
            ssh_pkey="/tmp/key.pem",
            jumpbox="jumpbox.example",
            username="developer",
        )
        region = Region(
            name="au-prod",
            remote_bind_address="global-db.example",
            remote_bind_port=3306,
            username="config-user",
            password="config-password",
            devbox_required=True,
            devbox=devbox,
        )
        data_source = DataSource(
            username=region.username,
            password=region.password,
            database_server_url="tenant-db.example:3307",
            schema_name="ziegler_251124",
            region=region,
        )
        ssh_tunnel_mock.return_value.start.return_value = SimpleNamespace(
            host="127.0.0.1",
            port=13307,
        )

        with Tunnel(data_source, region) as engine:
            self.assertIs(engine, create_engine_mock.return_value)

        ssh_tunnel_mock.assert_called_once_with(
            dev_box=devbox,
            remote_host="tenant-db.example",
            remote_port=3307,
        )


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

    @patch("ops.views.locate_tenants")
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

    @patch("ops.views.locate_tenants", side_effect=RuntimeError("boom"))
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

    @patch("ops.views.run_squelch_query")
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

    @patch("ops.views.run_squelch_query")
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

    @patch("ops.views.run_squelch_query", side_effect=ValueError("Only SELECT"))
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

    @patch("ops.views.run_squelch_query")
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


class CommandsViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="commands@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    def test_commands_page_renders_registry(self) -> None:
        response = self.client.get(reverse("commands"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Commands")
        self.assertContains(response, "Backup, Download, Import")
        self.assertContains(response, "backup_download_import")

    @patch("ops.command_service.async_task")
    def test_command_run_create_queues_run(self, async_task_mock: Mock) -> None:
        params = {
            "backup_command": (
                "backup-db --host {database_server_url} "
                "--schema {schema_name} --output {remote_backup_path}"
            ),
            "remote_backup_path": "/tmp/backup.sql.gz",
            "local_database": "performio_local",
        }
        expected_params = {
            **params,
            "database_server_url": "mysql.example.internal",
            "schema_name": "tenant_acme",
        }

        response = self.client.post(
            reverse("command_run_create"),
            {
                "command_key": "backup_download_import",
                "database_server_url": "mysql.example.internal",
                "schema_name": "tenant_acme",
                "params_text": json.dumps(params),
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run = CommandRun.objects.get()
        self.assertEqual(run.command_key, "backup_download_import")
        self.assertEqual(run.params, expected_params)
        async_task_mock.assert_called_once_with("ops.tasks.run_command_run", run.pk)

    def test_command_run_requires_database_target(self) -> None:
        response = self.client.post(
            reverse("command_run_create"),
            {
                "command_key": "backup_download_import",
                "database_server_url": "",
                "schema_name": "",
                "params_text": json.dumps(
                    {
                        "backup_command": "backup-db",
                        "remote_backup_path": "/tmp/backup.sql.gz",
                        "local_database": "performio_local",
                    }
                ),
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Database server is required.", status_code=422)
        self.assertContains(response, "Schema is required.", status_code=422)

    @patch("ops.command_service.async_task")
    def test_lock_monitor_run_uses_tenant_host_parameter(
        self, async_task_mock: Mock
    ) -> None:
        response = self.client.post(
            reverse("command_run_create"),
            {
                "command_key": "lock_monitor",
                "tenant_host": "acme.performio.com",
                "database_server_url": "",
                "schema_name": "",
                "params_text": json.dumps({"long_running_seconds": 300}),
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run = CommandRun.objects.get()
        self.assertEqual(
            run.params,
            {
                "tenant_host": "acme.performio.com",
                "long_running_seconds": 300,
            },
        )
        async_task_mock.assert_called_once_with("ops.tasks.run_command_run", run.pk)

    def test_lock_monitor_run_form_has_synchronized_command_fields(self) -> None:
        response = self.client.get(
            reverse("command_run_create"),
            {"command": "lock_monitor"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="tenant_host"')
        self.assertContains(response, 'data-command-param="tenant_host"')
        self.assertContains(response, '"long_running_seconds": 300')
        self.assertContains(response, "syncCommandParameterForm")

    def test_lock_monitor_schedule_form_hides_database_target_fields(self) -> None:
        response = self.client.get(
            reverse("command_schedule_create"),
            {"command": "lock_monitor"},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertRegex(
            content,
            r'data-command-param="database_server_url">\s*'
            r'<legend class="fieldset-legend">Database Server</legend>',
        )
        self.assertRegex(
            content,
            r'data-command-param="schema_name">\s*'
            r'<legend class="fieldset-legend">Schema</legend>',
        )

    def test_lock_monitor_schedule_uses_only_tenant_host(self) -> None:
        response = self.client.post(
            reverse("command_schedule_create"),
            {
                "command_key": "lock_monitor",
                "tenant_host": "acme.performio.com",
                "database_server_url": "",
                "schema_name": "",
                "params_text": json.dumps({"long_running_seconds": 300}),
                "schedule_type": CommandSchedule.INTERVAL,
                "interval_minutes": "60",
                "cron_expression": "",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        schedule = CommandSchedule.objects.get()
        self.assertEqual(
            schedule.params,
            {
                "tenant_host": "acme.performio.com",
                "long_running_seconds": 300,
            },
        )

    def test_command_run_form_shows_json_errors(self) -> None:
        response = self.client.post(
            reverse("command_run_create"),
            {
                "command_key": "backup_download_import",
                "database_server_url": "mysql.example.internal",
                "schema_name": "tenant_acme",
                "params_text": "{not-json",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Invalid JSON", status_code=422)

    def test_lock_monitor_run_detail_includes_its_capture(self) -> None:
        run = CommandRun.objects.create(
            command_key="lock_monitor",
            params={"tenant_host": "ziegler.performio.co"},
            status=CommandRun.COMPLETED,
        )
        capture = LockMonitorCapture.objects.create(
            command_run=run,
            tenant_host="ziegler.performio.co",
            status=LockMonitorCapture.COMPLETED,
            process_count=1,
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="ziegler_251124",
            database_name="ziegler_251124",
            process_id=123,
            query_text="select detail_capture_query",
            is_tenant_schema=True,
        )
        unrelated_run = CommandRun.objects.create(command_key="lock_monitor")
        unrelated_capture = LockMonitorCapture.objects.create(
            command_run=unrelated_run,
            tenant_host="other.performio.co",
            status=LockMonitorCapture.COMPLETED,
        )
        LockMonitoringRecord.objects.create(
            capture=unrelated_capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="other-db.example",
            schema_name="other_schema",
            query_text="select unrelated_detail_query",
        )

        response = self.client.get(
            reverse("command_run_detail", args=[run.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "partials/_lock_monitor.html")
        self.assertContains(response, f"Command run #{run.pk}")
        self.assertContains(response, f'data-capture-id="{capture.pk}"')
        self.assertContains(response, "select detail_capture_query")
        self.assertNotContains(response, "select unrelated_detail_query")
        self.assertNotContains(response, "Run check")

    def test_non_lock_monitor_run_detail_keeps_command_output(self) -> None:
        run = CommandRun.objects.create(
            command_key="backup_download_import",
            params={"schema_name": "tenant_schema"},
            output="backup complete",
            status=CommandRun.COMPLETED,
        )

        response = self.client.get(
            reverse("command_run_detail", args=[run.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "partials/_command_run_detail.html")
        self.assertContains(response, "Parameters")
        self.assertContains(response, "backup complete")

    def test_command_schedule_create_registers_schedule(self) -> None:
        params = {
            "backup_command": (
                "backup-db --host {database_server_url} "
                "--schema {schema_name} --output {remote_backup_path}"
            ),
            "remote_backup_path": "/tmp/backup.sql.gz",
            "local_database": "performio_local",
        }
        expected_params = {
            **params,
            "database_server_url": "mysql.example.internal",
            "schema_name": "tenant_acme",
        }

        response = self.client.post(
            reverse("command_schedule_create"),
            {
                "command_key": "backup_download_import",
                "database_server_url": "mysql.example.internal",
                "schema_name": "tenant_acme",
                "params_text": json.dumps(params),
                "schedule_type": CommandSchedule.INTERVAL,
                "interval_minutes": "60",
                "cron_expression": "",
                "is_active": "on",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        schedule = CommandSchedule.objects.get()
        self.assertEqual(schedule.command_key, "backup_download_import")
        self.assertEqual(schedule.params, expected_params)
        self.assertEqual(schedule.interval_minutes, 60)

    def test_backup_download_import_formats_database_target(self) -> None:
        output = run_registered_command(
            "backup_download_import",
            {
                "database_server_url": "mysql.example.internal",
                "schema_name": "tenant_acme",
                "backup_command": (
                    "backup-db --host {database_server_url} "
                    "--schema {schema_name} --output {remote_backup_path}"
                ),
                "remote_backup_path": "/tmp/backup.sql.gz",
                "local_database": "performio_local",
            },
        )

        self.assertIn("mysql.example.internal", output)
        self.assertIn("tenant_acme", output)
        self.assertIn("/tmp/backup.sql.gz", output)
        self.assertIn("wait for remote process matching 'backup.sql.gz'", output)

    @patch("ops.commands.backup_download_import.time.sleep")
    @patch(
        "ops.commands.backup_download_import.time.monotonic",
        side_effect=[0, 1, 16],
    )
    @patch("ops.commands.backup_download_import.subprocess.run")
    def test_wait_for_remote_backup_polls_until_file_is_ready(
        self,
        subprocess_run_mock: Mock,
        monotonic_mock: Mock,
        sleep_mock: Mock,
    ) -> None:
        subprocess_run_mock.side_effect = [
            SimpleNamespace(returncode=0, stdout="RUNNING", stderr=""),
            SimpleNamespace(returncode=0, stdout="READY", stderr=""),
        ]
        output_parts: list[str] = []

        _wait_for_remote_backup(
            remote_target="devbox.performio.co",
            remote_user="staff",
            remote_backup_path="/tmp/tenant.sql",
            process_pattern="tenant.sql",
            poll_interval_seconds=15,
            timeout_seconds=120,
            output_parts=output_parts,
        )

        self.assertEqual(subprocess_run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(15)
        self.assertIn("Remote backup finished after 16s", output_parts[-1])
        self.assertEqual(monotonic_mock.call_count, 3)

    @patch("ops.commands.backup_download_import.time.sleep")
    @patch(
        "ops.commands.backup_download_import.time.monotonic",
        side_effect=[0, 120],
    )
    @patch("ops.commands.backup_download_import.subprocess.run")
    def test_wait_for_remote_backup_times_out(
        self,
        subprocess_run_mock: Mock,
        monotonic_mock: Mock,
        sleep_mock: Mock,
    ) -> None:
        subprocess_run_mock.return_value = SimpleNamespace(
            returncode=0,
            stdout="WAITING",
            stderr="",
        )

        with self.assertRaisesRegex(TimeoutError, "Last status: WAITING"):
            _wait_for_remote_backup(
                remote_target="devbox.performio.co",
                remote_user="staff",
                remote_backup_path="/tmp/tenant.sql",
                process_pattern="tenant.sql",
                poll_interval_seconds=15,
                timeout_seconds=120,
                output_parts=[],
            )

        sleep_mock.assert_not_called()
        self.assertEqual(monotonic_mock.call_count, 2)

    @patch("ops.commands.backup_download_import._import_backup")
    @patch("ops.commands.backup_download_import._wait_for_remote_backup")
    @patch("ops.commands.backup_download_import._run_command")
    def test_backup_waits_before_downloading(
        self,
        run_command_mock: Mock,
        wait_for_remote_backup_mock: Mock,
        import_backup_mock: Mock,
    ) -> None:
        events: list[str] = []
        run_command_mock.side_effect = lambda command, output: events.append(command[0])
        wait_for_remote_backup_mock.side_effect = lambda **kwargs: events.append("wait")
        import_backup_mock.side_effect = lambda *args: events.append("import")

        run_registered_command(
            "backup_download_import",
            {
                "database_server_url": "mysql.example.internal",
                "schema_name": "tenant_acme",
                "backup_command": "backup-db",
                "remote_backup_path": "/tmp/tenant.sql",
                "local_backup_path": "/tmp/tenant.sql",
                "local_database": "performio_local",
                "dry_run": False,
            },
        )

        self.assertEqual(events, ["ssh", "wait", "scp", "mysql", "import"])

    @patch("ops.lock_monitor.run_lock_monitor")
    def test_registered_lock_monitor_runs_capture(
        self,
        run_lock_monitor_mock: Mock,
    ) -> None:
        run_lock_monitor_mock.return_value = SimpleNamespace(
            pk=7,
            data_source_count=1,
            process_count=4,
            lock_count=2,
        )

        output = run_registered_command(
            "lock_monitor",
            {"tenant_host": "tenant.performio.com"},
            command_run_id=42,
        )

        run_lock_monitor_mock.assert_called_once_with(
            "tenant.performio.com",
            long_running_seconds=300,
            command_run_id=42,
        )
        self.assertIn("capture #7", output)
        self.assertIn("4 process(es)", output)
        self.assertIn("2 lock wait(s)", output)


class LockMonitorCollectorTests(TestCase):
    def test_process_filter_removes_sleep_and_monitor_query(self) -> None:
        self.assertFalse(_is_running_process({"Command": "Sleep", "Info": None}))
        self.assertFalse(
            _is_running_process({"Command": "Query", "Info": "SHOW FULL PROCESSLIST"})
        )
        self.assertTrue(
            _is_running_process(
                {"Command": "Query", "Info": "select * from commission"}
            )
        )

    @patch("ops.lock_monitor._inspect_data_source")
    @patch("ops.lock_monitor.find_tenant_data_sources")
    @patch("ops.lock_monitor.load_config")
    def test_run_lock_monitor_persists_process_and_lock_rows(
        self,
        load_config_mock: Mock,
        find_data_sources_mock: Mock,
        inspect_data_source_mock: Mock,
    ) -> None:
        from tools.db_config import Region
        from tools.tenant import DataSource

        region = Region(
            name="au-prod",
            remote_bind_address="global.example",
            username="global",
            password="secret",
        )
        data_source = DataSource(
            username="tenant",
            password="secret",
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            shard_hosts="tenant.performio.com",
            region=region,
        )
        load_config_mock.return_value = [region]
        find_data_sources_mock.return_value = [data_source]

        def inspect(
            capture: LockMonitorCapture,
            source: object,
            long_running_seconds: int,
        ):
            self.assertEqual(source, data_source)
            self.assertEqual(long_running_seconds, 300)
            return (
                [
                    LockMonitoringRecord(
                        capture=capture,
                        record_type=LockMonitoringRecord.PROCESS,
                        region="au-prod",
                        database_server_url="tenant-db.example",
                        schema_name="tenant_schema",
                        process_id=44,
                        query_text="select * from commission",
                    ),
                    LockMonitoringRecord(
                        capture=capture,
                        record_type=LockMonitoringRecord.LOCK_WAIT,
                        region="au-prod",
                        database_server_url="tenant-db.example",
                        schema_name="tenant_schema",
                        waiting_process_id=44,
                        blocking_process_id=45,
                        query_text="update commission set amount = 1",
                        blocking_query_text="update commission set amount = 2",
                    ),
                ],
                ["tenant_schema: sys view unavailable"],
            )

        inspect_data_source_mock.side_effect = inspect

        command_run = CommandRun.objects.create(
            command_key="lock_monitor",
            params={"tenant_host": "tenant.performio.com"},
        )
        capture = run_lock_monitor(
            "tenant.performio.com",
            command_run_id=command_run.pk,
        )

        self.assertEqual(capture.status, LockMonitorCapture.COMPLETED)
        self.assertEqual(capture.command_run, command_run)
        self.assertEqual(capture.data_source_count, 1)
        self.assertEqual(capture.process_count, 1)
        self.assertEqual(capture.lock_count, 1)
        self.assertIn("sys view unavailable", capture.warning)
        self.assertEqual(LockMonitoringRecord.objects.count(), 2)
        find_data_sources_mock.assert_called_once_with("tenant.performio.com", [region])

    @patch("ops.lock_monitor.find_tenant_data_sources", return_value=[])
    @patch("ops.lock_monitor.load_config", return_value=[])
    def test_run_lock_monitor_saves_failed_capture(
        self,
        load_config_mock: Mock,
        find_data_sources_mock: Mock,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "No tenant data sources"):
            run_lock_monitor("missing.performio.com")

        capture = LockMonitorCapture.objects.get()
        self.assertEqual(capture.status, LockMonitorCapture.FAILED)
        self.assertIn("No tenant data sources", capture.error)
        load_config_mock.assert_called_once()
        find_data_sources_mock.assert_called_once()

    def test_fetch_lock_rows_falls_back_to_information_schema(self) -> None:
        connection = Mock()
        fallback_result = Mock()
        fallback_result.mappings.return_value = [
            {
                "waiting_pid": 10,
                "blocking_pid": 11,
                "waiting_query": "update one",
                "blocking_query": "update two",
            }
        ]
        connection.execute.side_effect = [
            SQLAlchemyError("sys schema denied"),
            SQLAlchemyError("performance schema denied"),
            fallback_result,
        ]

        rows, warning = _fetch_lock_rows(connection)

        self.assertEqual(rows[0]["waiting_pid"], 10)
        self.assertEqual(warning, "")
        self.assertEqual(connection.execute.call_count, 3)

    def test_long_running_query_saves_json_explain_and_summary(self) -> None:
        connection = Mock()
        explain_result = Mock()
        explain_result.first.return_value = (
            json.dumps(
                {
                    "query_block": {
                        "cost_info": {"query_cost": "428.50"},
                        "table": {
                            "table_name": "commission",
                            "access_type": "ALL",
                            "possible_keys": ["idx_status"],
                            "rows_examined_per_scan": 250000,
                            "filtered": "10.00",
                        },
                    }
                }
            ),
        )
        connection.exec_driver_sql.return_value = explain_result
        record = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            database_name="tenant_schema",
            duration_seconds=300,
            query_text="select * from commission where status = 'open'",
        )

        _add_explain_if_long_running(
            connection,
            record,
            long_running_seconds=300,
            tenant_schema="tenant_schema",
        )

        connection.exec_driver_sql.assert_called_once_with(
            "EXPLAIN FORMAT=JSON select * from commission where status = 'open'"
        )
        self.assertEqual(
            record.explain_json["query_block"]["table"]["table_name"],
            "commission",
        )
        self.assertIn("Estimated query cost: 428.50", record.explanation)
        self.assertIn("full table scan on commission", record.explanation)
        self.assertIsNotNone(record.explain_generated_at)

    def test_short_running_query_does_not_run_explain(self) -> None:
        connection = Mock()
        record = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            database_name="tenant_schema",
            duration_seconds=299,
            query_text="select * from commission",
        )

        _add_explain_if_long_running(
            connection,
            record,
            long_running_seconds=300,
            tenant_schema="tenant_schema",
        )

        connection.exec_driver_sql.assert_not_called()
        self.assertIsNone(record.explain_json)
        self.assertEqual(record.explanation, "")

    def test_long_running_query_for_other_schema_is_tracked_without_explain(
        self,
    ) -> None:
        connection = Mock()
        record = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            database_name="other_schema",
            duration_seconds=600,
            query_text="select * from another_tenant_table",
        )

        _add_explain_if_long_running(
            connection,
            record,
            long_running_seconds=300,
            tenant_schema="tenant_schema",
        )

        connection.exec_driver_sql.assert_not_called()
        self.assertFalse(record.is_tenant_schema)
        self.assertTrue(record.is_long_running)
        self.assertIn("other_schema", record.explain_skipped_reason)

    def test_process_records_are_correlated_with_locks_and_resource_signals(
        self,
    ) -> None:
        from .lock_monitor import _enrich_process_records

        waiting = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            database_name="tenant_schema",
            process_id=10,
            duration_seconds=600,
        )
        blocking = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="other_schema",
            database_name="other_schema",
            process_id=11,
            duration_seconds=30,
        )

        _enrich_process_records(
            [waiting, blocking],
            lock_rows=[
                {
                    "waiting_pid": 10,
                    "blocking_pid": 11,
                    "blocking_command": "Sleep",
                    "blocking_sleep_seconds": 420,
                    "blocking_transaction_seconds": 900,
                    "blocking_rows_locked": 12,
                    "blocking_last_query": "update commission set amount = 10",
                }
            ],
            statement_metrics={
                10: {
                    "rows_examined": 500000,
                    "rows_sent": 5,
                    "created_tmp_disk_tables": 2,
                    "no_index_used": 1,
                    "lock_time_ms": 1250.0,
                }
            },
            server_metrics={
                "threads_connected": 90,
                "max_connections": 100,
                "connection_utilization_pct": 90.0,
                "innodb_row_lock_current_waits": 1,
            },
            tenant_schema="tenant_schema",
            long_running_seconds=300,
        )

        self.assertTrue(waiting.is_lock_waiting)
        self.assertEqual(waiting.blocking_process_id, 11)
        self.assertTrue(blocking.is_lock_blocking)
        self.assertIn(
            "sleeping_transaction_holds_lock",
            blocking.contention_signals,
        )
        self.assertEqual(blocking.statement_metrics["sleeping_seconds"], 420)
        self.assertEqual(blocking.statement_metrics["transaction_seconds"], 900)
        self.assertEqual(blocking.statement_metrics["rows_locked"], 12)
        self.assertIn("waiting_on_lock", waiting.contention_signals)
        self.assertIn("statement_no_index_used", waiting.contention_signals)
        self.assertIn("statement_temp_disk_tables", waiting.contention_signals)
        self.assertIn("server_connection_utilization_high", waiting.contention_signals)
        self.assertEqual(waiting.statement_metrics["rows_examined"], 500000)
        self.assertEqual(waiting.server_metrics["threads_connected"], 90)

    @patch("ops.lock_monitor._fetch_mapping_rows")
    def test_sleeping_lock_blockers_are_merged_with_last_query(
        self,
        fetch_rows_mock: Mock,
    ) -> None:
        from .lock_monitor import (
            _fetch_sleeping_lock_blockers,
            _merge_sleeping_blocker_details,
        )

        sleeping_row = {
            "waiting_pid": 10,
            "blocking_pid": 11,
            "blocking_command": "Sleep",
            "blocking_sleep_seconds": 420,
            "blocking_transaction_seconds": 900,
            "blocking_rows_locked": 12,
            "blocking_last_query": "update commission set amount = 10",
        }
        fetch_rows_mock.return_value = [sleeping_row]

        sleeping_rows, warning = _fetch_sleeping_lock_blockers(Mock())
        merged = _merge_sleeping_blocker_details(
            [
                {
                    "waiting_pid": 10,
                    "blocking_pid": 11,
                    "blocking_query": "",
                }
            ],
            sleeping_rows,
        )

        self.assertEqual(warning, "")
        self.assertEqual(merged[0]["blocking_command"], "Sleep")
        self.assertEqual(
            merged[0]["blocking_query"],
            "update commission set amount = 10",
        )
        self.assertEqual(merged[0]["blocking_transaction_seconds"], 900)

    @patch("ops.lock_monitor._fetch_mapping_rows")
    def test_statement_metrics_convert_performance_schema_timers(
        self,
        fetch_rows_mock: Mock,
    ) -> None:
        from .lock_monitor import _fetch_statement_metrics

        fetch_rows_mock.return_value = [
            {
                "process_id": 10,
                "timer_wait": 5_000_000_000,
                "lock_time": 1_250_000_000,
                "rows_examined": 500000,
                "rows_sent": 5,
            }
        ]

        metrics, warning = _fetch_statement_metrics(Mock())

        self.assertEqual(warning, "")
        self.assertEqual(metrics[10]["timer_wait_ms"], 5.0)
        self.assertEqual(metrics[10]["lock_time_ms"], 1.25)
        self.assertEqual(metrics[10]["rows_examined"], 500000)

    @patch("ops.lock_monitor._fetch_mapping_rows")
    def test_server_metrics_derive_connection_and_io_pressure(
        self,
        fetch_rows_mock: Mock,
    ) -> None:
        from .lock_monitor import _fetch_server_metrics

        fetch_rows_mock.side_effect = [
            [
                {"Variable_name": "Threads_connected", "Value": "90"},
                {"Variable_name": "Created_tmp_tables", "Value": "200"},
                {"Variable_name": "Created_tmp_disk_tables", "Value": "50"},
                {
                    "Variable_name": "Innodb_buffer_pool_read_requests",
                    "Value": "100000",
                },
                {"Variable_name": "Innodb_buffer_pool_reads", "Value": "500"},
            ],
            [{"Variable_name": "max_connections", "Value": "100"}],
        ]

        metrics, warning = _fetch_server_metrics(Mock())

        self.assertEqual(warning, "")
        self.assertEqual(metrics["connection_utilization_pct"], 90.0)
        self.assertEqual(metrics["temp_disk_table_pct"], 25.0)
        self.assertEqual(metrics["buffer_pool_disk_read_pct"], 0.5)

    def test_explain_failure_is_saved_without_failing_capture(self) -> None:
        connection = Mock()
        connection.exec_driver_sql.side_effect = SQLAlchemyError("permission denied")
        record = LockMonitoringRecord(
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            database_name="tenant_schema",
            duration_seconds=600,
            query_text="update commission set amount = 10 where id = 4",
        )

        _add_explain_if_long_running(
            connection,
            record,
            long_running_seconds=300,
            tenant_schema="tenant_schema",
        )

        self.assertIn("permission denied", record.explain_error)
        self.assertIsNone(record.explain_json)

    def test_summarize_mysql_explain_supports_new_json_format(self) -> None:
        summary = summarize_mysql_explain(
            {
                "operation": "Filter: (commission.status = 'open')",
                "estimated_total_cost": 18.4,
                "inputs": [
                    {
                        "operation": "Index range scan on commission using idx_status",
                        "table_name": "commission",
                        "access_type": "index",
                        "index_name": "idx_status",
                        "estimated_rows": 42,
                    }
                ],
            }
        )

        self.assertIn("Estimated query cost: 18.4", summary)
        self.assertIn("Table commission", summary)
        self.assertIn("key=idx_status", summary)


class LockMonitorViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="locks@example.com",
            password="password",
        )
        self.client.force_login(self.user)

    def _capture(self) -> LockMonitorCapture:
        return LockMonitorCapture.objects.create(
            tenant_host="tenant.performio.com",
            status=LockMonitorCapture.COMPLETED,
            data_source_count=1,
            process_count=2,
            lock_count=1,
        )

    def test_lock_monitor_page_renders_controls(self) -> None:
        response = self.client.get(reverse("lock_monitor"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lock Monitor")
        self.assertContains(response, 'data-theme-surface="light"')
        self.assertContains(response, 'name="tenant_host"')
        self.assertContains(response, 'name="run_id"')
        self.assertContains(response, "Export Excel")
        self.assertContains(response, 'hx-trigger="every 10s"')

    @patch("ops.command_service.async_task")
    def test_lock_monitor_queues_registered_command(
        self,
        async_task_mock: Mock,
    ) -> None:
        response = self.client.post(
            reverse("lock_monitor"),
            {"tenant_host": "tenant.performio.com"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        run = CommandRun.objects.get(command_key="lock_monitor")
        self.assertEqual(run.params, {"tenant_host": "tenant.performio.com"})
        async_task_mock.assert_called_once_with("ops.tasks.run_command_run", run.pk)
        self.assertContains(response, f"Queued lock monitor run #{run.pk}")

    def test_lock_monitor_results_filter_full_query_text(self) -> None:
        capture = self._capture()
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.PROCESS,
            region="au-prod",
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            process_id=10,
            query_text="select unique_lock_monitor_value from commission",
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.PROCESS,
            region="au-prod",
            database_server_url="tenant-db.example",
            schema_name="other_schema",
            process_id=11,
            query_text="select unrelated_value from participant",
        )

        response = self.client.get(
            reverse("lock_monitor_results"),
            {"schema": "tenant_schema", "search": "unique_lock_monitor_value"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "select unique_lock_monitor_value")
        self.assertNotContains(response, "select unrelated_value")

    def test_lock_monitor_results_filter_by_command_run_id(self) -> None:
        matching_run = CommandRun.objects.create(command_key="lock_monitor")
        other_run = CommandRun.objects.create(command_key="lock_monitor")
        matching_capture = LockMonitorCapture.objects.create(
            command_run=matching_run,
            tenant_host="matching.performio.com",
            status=LockMonitorCapture.COMPLETED,
        )
        other_capture = LockMonitorCapture.objects.create(
            command_run=other_run,
            tenant_host="other.performio.com",
            status=LockMonitorCapture.COMPLETED,
        )
        LockMonitoringRecord.objects.create(
            capture=matching_capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="matching-db.example",
            schema_name="matching_schema",
            query_text="select matching_run_query",
        )
        LockMonitoringRecord.objects.create(
            capture=other_capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="other-db.example",
            schema_name="other_schema",
            query_text="select other_run_query",
        )

        response = self.client.get(
            reverse("lock_monitor_results"),
            {"run_id": str(matching_run.pk)},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "select matching_run_query")
        self.assertContains(response, f"Run #{matching_run.pk}")
        self.assertNotContains(response, "select other_run_query")

    def test_lock_monitor_results_render_snapshot_sections(self) -> None:
        capture = LockMonitorCapture.objects.create(
            tenant_host="ziegler.performio.co",
            status=LockMonitorCapture.COMPLETED,
            data_source_count=1,
            process_count=2,
            lock_count=1,
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="ziegler_251124",
            database_name="ziegler_251124",
            process_id=10,
            user="config-user",
            command="Query",
            duration_seconds=420,
            state="executing",
            query_text="select tenant_snapshot_query",
            is_tenant_schema=True,
            is_long_running=True,
            explanation="Table commission; access=range; key=idx_status",
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.PROCESS,
            database_server_url="tenant-db.example",
            schema_name="ziegler_251124",
            database_name="other_schema",
            process_id=11,
            user="other-user",
            command="Query",
            duration_seconds=12,
            state="executing",
            query_text="select same_server_query",
            is_tenant_schema=False,
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.LOCK_WAIT,
            database_server_url="tenant-db.example",
            schema_name="ziegler_251124",
            waiting_process_id=10,
            blocking_process_id=11,
            query_text="update waiting_query",
            blocking_query_text="update blocking_query",
            is_tenant_schema=True,
        )
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.RESOURCE,
            database_server_url="tenant-db.example",
            schema_name="ziegler_251124",
            database_name="ziegler_251124",
            is_tenant_schema=True,
            server_metrics={
                "threads_connected": 18,
                "threads_running": 3,
                "connection_utilization_pct": 36.0,
            },
        )

        response = self.client.get(
            reverse("lock_monitor_results"),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-snapshot-status="alert"')
        self.assertContains(response, "Lock waits")
        self.assertContains(response, "Active queries — ziegler_251124")
        self.assertContains(response, "Active queries — other schemas")
        self.assertContains(response, "Server health")
        self.assertContains(response, "select tenant_snapshot_query")
        self.assertContains(response, "select same_server_query")
        self.assertContains(response, "Table commission; access=range")

    def test_lock_monitor_results_paginate_capture_snapshots(self) -> None:
        captures: list[LockMonitorCapture] = []
        for index in range(6):
            capture = LockMonitorCapture.objects.create(
                tenant_host="ziegler.performio.co",
                status=LockMonitorCapture.COMPLETED,
                process_count=1,
            )
            captures.append(capture)
            LockMonitoringRecord.objects.create(
                capture=capture,
                record_type=LockMonitoringRecord.PROCESS,
                database_server_url="tenant-db.example",
                schema_name="ziegler_251124",
                database_name="ziegler_251124",
                process_id=100 + index,
                query_text=f"select paged_query_{index}",
                is_tenant_schema=True,
            )

        first_page = self.client.get(
            reverse("lock_monitor_results"),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.content.count(b'data-snapshot-status="'), 5)
        self.assertContains(first_page, "Page 1 of 2")
        self.assertContains(first_page, "select paged_query_5")
        self.assertNotContains(first_page, "select paged_query_0")

        second_page = self.client.get(
            reverse("lock_monitor_results"),
            {"page": "2"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.content.count(b'data-snapshot-status="'), 1)
        self.assertContains(second_page, "Page 2 of 2")
        self.assertContains(second_page, "select paged_query_0")
        self.assertNotContains(second_page, "select paged_query_5")
        self.assertContains(second_page, "?page=2")

    def test_lock_monitor_export_returns_filtered_xlsx(self) -> None:
        capture = self._capture()
        LockMonitoringRecord.objects.create(
            capture=capture,
            record_type=LockMonitoringRecord.LOCK_WAIT,
            region="au-prod",
            database_server_url="tenant-db.example",
            schema_name="tenant_schema",
            waiting_process_id=10,
            blocking_process_id=11,
            query_text="update unique_export_value",
            blocking_query_text="update blocker_value",
            explanation="Table commission; access=ALL; key=none",
            explain_json={"query_block": {"table": {"table_name": "commission"}}},
        )

        response = self.client.get(
            reverse("lock_monitor_export"),
            {"record_type": LockMonitoringRecord.LOCK_WAIT},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="lock-monitor.xlsx"',
        )
        with ZipFile(BytesIO(response.content)) as workbook:
            sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("unique_export_value", sheet_xml)
        self.assertIn("blocker_value", sheet_xml)
        self.assertIn("access=ALL", sheet_xml)
        self.assertIn("statement_metrics", sheet_xml)
        self.assertIn("server_metrics", sheet_xml)
        self.assertIn("contention_signals", sheet_xml)
