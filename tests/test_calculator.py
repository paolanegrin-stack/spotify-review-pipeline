from copy import deepcopy
from decimal import Decimal
import unittest

from cost.calculator import bill, measure, estimates


class CalculatorTests(unittest.TestCase):
    def test_doubling_rates_doubles_api_subtotal_leaves_time_unchanged(self):
        rates = {('openai','gpt-6-luna',item):dict(price_usd_per_million_tokens=price)
                 for item,price in (('input','.10'),('cached_input','.01'),('output','.50'))}
        doubled = {key:dict(price_usd_per_million_tokens=str(Decimal(row['price_usd_per_million_tokens'])*2)) for key,row in rates.items()}
        calls = [dict(request_id=role,role=role,model='gpt-6-luna',outcome='succeeded',input_tokens=100,
                      cached_input_tokens=20,output_tokens=50,reasoning_tokens=0,review_ids=['one'],
                      request=dict(input='{"issue_id":"ISS-BILLING"}')) for role in ('enrich','verify','group','memo')]
        stages = [dict(stage=role,wall_clock_seconds=2) for role in ('prepare','enrich','verify','group','rank','memo')]
        source = [dict(review_id='one',text_sha256='same',status='completed')]
        base = measure(calls,stages,rates,source,{})
        changed = measure(calls,stages,doubled,source,{})
        self.assertEqual(bill(calls[0],rates)[0]['units'],80)
        self.assertEqual(bill(calls[0],rates)[1]['units'],20)
        for role in ('enrich','verify','group','memo'):
            self.assertEqual(changed[role]['cost'],base[role]['cost']*2)
            self.assertEqual(changed[role]['seconds'],base[role]['seconds'])
        a = estimates(base,Decimal(100),20)
        b = estimates(changed,Decimal(100),20)
        self.assertEqual(b['api_cost'],a['api_cost']*2)
        self.assertEqual(b['seconds'],a['seconds'])


if __name__=='__main__':
    unittest.main()
