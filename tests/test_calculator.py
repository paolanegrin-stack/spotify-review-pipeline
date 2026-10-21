from copy import deepcopy
from decimal import Decimal
import unittest

from cost.calculator import bill, measure, estimates, scope_seconds


class CalculatorTests(unittest.TestCase):
    def test_throughput_uses_elapsed_time_and_scales_workers_once(self):
        measured = [{'enrich': dict(completed=10000,seconds=300,workers=300)}]
        self.assertEqual(scope_seconds(measured,100000,300), Decimal(3000))
        self.assertEqual(scope_seconds(measured,100000,150), Decimal(6000))

    def test_unknown_usage_preserves_known_subtotal_and_prices_projected_attempts(self):
        rates = {('openai','gpt-6-luna',item):dict(price_usd_per_million_tokens=price)
                 for item,price in (('input','.10'),('cached_input','.01'),('output','.50'))}
        good = dict(request_id='good',role='enrich',model='gpt-6-luna',outcome='succeeded',
                    input_tokens=100,cached_input_tokens=0,output_tokens=50,reasoning_tokens=0,review_ids=['one'])
        bad = dict(good,request_id='timeout',outcome='failed',input_tokens=None,cached_input_tokens=None,output_tokens=None)
        measured = measure([good,bad],[dict(stage='enrich',wall_clock_seconds=10)],rates,
                           [dict(review_id='one',text_sha256='text',status='completed')],{})
        self.assertEqual(measured['enrich']['cost'],Decimal('.000035'))
        self.assertEqual(measured['enrich']['unknown_usage'],1)
        self.assertEqual(measured['enrich']['known_calls'],1)
        self.assertEqual(measured['enrich']['unknown_request_ids'],['timeout'])
        for role in ('verify','group','memo'):
            measured[role].update(work=1,calls=1,known_calls=1,cost=Decimal(0))
        self.assertEqual(estimates(measured,Decimal(100),20)['api_cost'],Decimal('.000035')*2*484189)

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
