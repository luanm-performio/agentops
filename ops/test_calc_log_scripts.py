from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import TestCase

import openpyxl

from ops.calc_log_scripts import generate_dashboard, parsecalclogdetail


def _write_workbook(path, sheets):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(
            [
                "fn",
                "step",
                "transform",
                "start_time",
                "end_time",
                "duration_in_seconds",
                "records_updated",
                "query",
                "detail",
                "trace_id",
            ]
        )
        for index, row in enumerate(rows, start=1):
            sheet.append(
                [
                    sheet_name,
                    row["step"],
                    row.get("transform", "Plan Calculation"),
                    datetime(2026, 6, 25, 5, index, 0),
                    datetime(2026, 6, 25, 5, index, 1),
                    row.get("duration", 1.0),
                    row.get("records", 1),
                    row["query"],
                    row.get("detail"),
                    "trace-id",
                ]
            )

    workbook.save(path)


class GenerateDashboardAlignmentTests(TestCase):
    def test_plan_calculation_rows_use_step_name_and_source_indexes(self):
        with TemporaryDirectory() as temporary_directory:
            workbook_path = Path(temporary_directory) / "calculation_logs.xlsx"
            _write_workbook(
                workbook_path,
                {
                    "before.log": [
                        {
                    "step": 'Batch "North" Create credits',
                    "query": "select before_create_credits",
                    "detail": "before create detail",
                },
                {
                    "step": 'Batch "North" Roll up totals',
                    "query": "select before_rollup_totals",
                    "detail": "before rollup detail",
                },
            ],
            "after.log": [
                {
                    "step": 'Batch "North" Create credits',
                    "query": "select after_create_credits",
                    "detail": "after create detail",
                },
                {
                    "step": 'Batch "North" Roll up totals',
                    "query": "select after_rollup_totals",
                    "detail": "after rollup detail",
                },
                    ],
                },
            )

            data = generate_dashboard.build_data(str(workbook_path))

        self.assertIs(data["same_sequence"], True)
        self.assertEqual(data["match_mode"], "step + event + occurrence")
        self.assertEqual(data["transforms"][0]["transform"], 'Batch "North" Create credits')
        self.assertEqual(data["transforms"][0]["event"], "Plan Calculation")
        self.assertEqual(data["transforms"][0]["sourceIndexes"], [1, 1])
        self.assertEqual(
            data["transforms"][0]["queries"],
            ["select before_create_credits", "select after_create_credits"],
        )
        self.assertEqual(
            data["transforms"][0]["details"],
            ["before create detail", "after create detail"],
        )

    def test_same_keys_in_different_order_are_not_line_by_line(self):
        with TemporaryDirectory() as temporary_directory:
            workbook_path = Path(temporary_directory) / "calculation_logs.xlsx"
            create_credits = {
                "step": 'Batch "North" Create credits',
                "query": "select create_credits",
            }
            rollup_totals = {
                "step": 'Batch "North" Roll up totals',
                "query": "select rollup_totals",
            }
            _write_workbook(
                workbook_path,
                {
                    "before.log": [create_credits, rollup_totals],
                    "after.log": [rollup_totals, create_credits],
                },
            )

            data = generate_dashboard.build_data(str(workbook_path))

        self.assertIs(data["same_length"], True)
        self.assertIs(data["same_sequence"], False)
        self.assertEqual(data["transforms"][0]["sourceIndexes"], [1, 2])


class CalcLogParserTests(TestCase):
    def test_parse_body_ignores_plan_calculation_status_messages(self):
        lines = [
            "Calculating Plan Commission Per Credit [Sales Commission] "
            "for Participant Jane Example in Period 78",
            "Calculating Sum [Total Cases] for Participant Jane Example in Period 78",
            "Calculating Plan Formula [Weekly Payable] "
            "for Participant Jane Example in Period 78",
            "No eligible participants found for plan Example Plan in period 78, "
            "ending calculation.",
        ]
        records, query = parsecalclogdetail.parse_body(lines)
        _records, _query, detail = parsecalclogdetail.parse_body_with_detail(lines)

        self.assertIsNone(records)
        self.assertIsNone(query)
        self.assertIn("Calculating Sum [Total Cases]", detail)
        self.assertIn("No eligible participants found", detail)

    def test_parse_body_keeps_multiline_sql_after_status_messages(self):
        records, query = parsecalclogdetail.parse_body(
            [
                "Calculating Plan Commission Per Credit [Sales Commission] "
                "for Participant Jane Example in Period 78",
                "UPDATE commission",
                "SET amount = 1",
                "WHERE period_id = 78",
                "Updated 1 rows.",
            ]
        )

        self.assertEqual(records, 1)
        self.assertEqual(query, "UPDATE commission\nSET amount = 1\nWHERE period_id = 78")
