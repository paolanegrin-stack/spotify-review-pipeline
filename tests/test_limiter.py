import threading
import unittest
from unittest.mock import patch

from src import config as C
from src.enrich import GlobalLimiter, GLOBAL_LIMITER, Ledger


class LimiterTests(unittest.TestCase):
    def test_request_and_token_windows_are_shared(self):
        clock = [0.0]
        stop = threading.Event()
        limiter = GlobalLimiter(2,10,clock=lambda:clock[0])
        def advance(seconds):
            clock[0] += seconds
            return False
        with patch.object(stop,'wait',side_effect=advance):
            limiter.acquire(6,stop)
            limiter.acquire(6,stop)
            self.assertEqual(clock[0],60)
            limiter.acquire(1,stop)
            limiter.acquire(1,stop)
            self.assertEqual(clock[0],120)
        self.assertEqual(C.TIMEOUT_SECONDS,120)
        self.assertEqual((GLOBAL_LIMITER.rpm,GLOBAL_LIMITER.tpm),(4000,1800000))
        self.assertEqual(C.BATCH_SIZE,50)
        self.assertEqual(C.VERIFY_BATCH_SIZE,25)
        self.assertGreaterEqual(C.MAX_WORKERS,60)

    def test_worker_ledgers_share_the_same_limiter(self):
        first = Ledger(None,None,None)
        second = Ledger(None,None,None)
        self.assertIs(first.limiter,second.limiter)


if __name__=='__main__':
    unittest.main()
