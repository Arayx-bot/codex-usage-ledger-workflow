"""Private financial rows stay in disposable synthetic ledgers during tests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import app


class ExpenseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / 'ledger'

    def tearDown(self):
        self.tmp.cleanup()

    def values(self, **extra):
        return dict({'amount': '20.10', 'currency': 'USD', 'payee': 'Synthetic AI',
                     'description': '合成月度订阅', 'request_key': 'synthetic-request',
                     'payment_date': '2026-10-16', 'category': '订阅'}, **extra)

    def test_read_only_empty_installation(self):
        self.assertEqual(app.expense_list(self.data), [])
        self.assertFalse(self.data.exists())

    def test_exact_minor_units_and_separate_currencies(self):
        for i, (amount, currency, expected) in enumerate([('0.10', 'USD', 10), ('0.20', 'USD', 20), ('3.50', 'CNY', 350), ('120', 'JPY', 120)]):
            result = app.expense_add(self.data, self.values(amount=amount, currency=currency, request_key=str(i)))
            self.assertEqual(result['expense']['amount_minor'], expected)
        totals = {}
        for row in app.expense_list(self.data):
            totals[row['currency']] = totals.get(row['currency'], 0) + row['signed_minor']
        self.assertEqual(totals, {'USD': 30, 'CNY': 350, 'JPY': 120})

    def test_invalid_money_and_metadata_do_not_create_ledger(self):
        invalid = [{'amount': '0'}, {'amount': '-1'}, {'amount': '1.001'}, {'amount': 'NaN'}, {'amount': 'Infinity'},
                   {'amount': 'True'}, {'currency': 'XXX'}, {'currency': 'JPY', 'amount': '0.1'},
                   {'payment_date': '2026-02-30'}, {'payee': ''}, {'description': ''}, {'request_key': ''}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                app.expense_add(self.data, self.values(**values))
        self.assertFalse(self.data.exists())

    def test_idempotency_and_conflicting_retry(self):
        first = app.expense_add(self.data, self.values())
        retry = app.expense_add(self.data, self.values())
        self.assertTrue(first['created']); self.assertFalse(retry['created'])
        self.assertEqual(first['expense']['id'], retry['expense']['id'])
        with self.assertRaises(ValueError):
            app.expense_add(self.data, self.values(amount='99'))
        self.assertEqual(len(app.expense_list(self.data)), 1)

    def test_duplicate_confirmation_is_not_automatic(self):
        app.expense_add(self.data, self.values())
        with self.assertRaises(app.DuplicateExpense):
            app.expense_add(self.data, self.values(request_key='second', project='另一项目'))
        result = app.expense_add(self.data, self.values(request_key='second', allow_duplicate=True))
        self.assertTrue(result['created']); self.assertEqual(len(app.expense_list(self.data)), 2)

    def test_refund_reduces_net_and_void_keeps_history(self):
        first = app.expense_add(self.data, self.values())['expense']
        refund = app.expense_add(self.data, self.values(request_key='refund', kind='refund', amount='5'))['expense']
        self.assertEqual(sum(e['signed_minor'] for e in app.expense_list(self.data)), 1510)
        app.expense_void(self.data, first['id'], '合成误记')
        app.expense_void(self.data, first['id'], '再次请求不覆写原因')
        self.assertEqual(app.expense_list(self.data)[0]['id'], refund['id'])
        history = app.expense_list(self.data, True)
        old = next(e for e in history if e['id'] == first['id'])
        self.assertEqual(old['void_reason'], '合成误记'); self.assertIsNotNone(old['voided_at'])
        with self.assertRaises(ValueError):
            app.expense_add(self.data, self.values())

    def test_expense_is_not_subscription_budget_or_token_usage(self):
        app.expense_add(self.data, self.values(project='合成项目'))
        report = app.report(self.data, {'timezone': 'Asia/Shanghai', 'subscription_monthly': 100})
        self.assertEqual(report['rows'], [])
        self.assertEqual(len(report['expenses']), 1)
        self.assertEqual(report['expenses'][0]['amount_minor'], 2010)

    def test_cli_records_only_specified_temporary_ledger(self):
        command = [sys.executable, str(app.ROOT/'app.py'), '--data-dir', str(self.data), 'expense-add',
                   '--amount', '12.34', '--currency', 'CNY', '--payee', 'Synthetic CLI',
                   '--description', '合成调用费', '--date', '2026-10-16', '--request-key', 'cli-key']
        response = subprocess.run(command, capture_output=True, text=True, check=True)
        record = json.loads(response.stdout)['expense']
        self.assertEqual(record['amount_minor'], 1234)
        self.assertEqual(record['project'], '共用/未分配')
        retry = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(retry.stdout)['created'])


if __name__ == '__main__':
    unittest.main()
