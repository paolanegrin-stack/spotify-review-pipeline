import unittest
from unittest.mock import patch

from src.audit import injection_test, metrics


class AuditTests(unittest.TestCase):
    def test_abstention_remains_in_accuracy_and_f1_denominator(self):
        cases=[dict(human=dict(topic='a'),prediction=dict(topic='a')),
               dict(human=dict(topic='b'),prediction={})]
        result=metrics(cases,'topic',('a','b'))
        self.assertEqual(result['accuracy'],0.5)
        self.assertEqual(result['macro_f1'],0.5)
        self.assertEqual(result['per_class']['b']['support'],1)

    def test_injection_structural_test_makes_no_model_call(self):
        with patch('openai.OpenAI') as client,patch('src.audit.write_json') as write:
            injection_test()
        client.assert_not_called()
        result=write.call_args.args[1]
        self.assertTrue(result['passed'])
        self.assertFalse(result['live_model_test'])
