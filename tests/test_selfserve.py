import json
import unittest
from copy import deepcopy
from datetime import date

from remedy.sample_data import RAWS
from remedy.selfserve import check, parse_json

class SelfServeTests(unittest.TestCase):
    def test_deviation_is_not_issuer_ground(self):
        raw=deepcopy(RAWS[0]); raw["selfserve"]={"return_days":0,"final_sale":True,"charges":[]}
        result=check(parse_json(json.dumps(raw)),date(2026,10,24))
        self.assertNotEqual("Talk to your card issuer",result["route"])

    def test_not_as_described_never_reaches_issuer(self):
        raw=deepcopy(RAWS[0]); raw["claim"]["reason"]="not_as_described"; raw["claim"]["merchant_response"]=None
        result=check(parse_json(json.dumps(raw)),date(2026,10,24))
        self.assertEqual("Missing info",result["route"])

    def test_luhn_card_rejected_but_order_label_allowed(self):
        raw=deepcopy(RAWS[0]); raw["order"]["order_id"]="Amazon-123-4567890-1234567"
        parse_json(json.dumps(raw))
        raw["claim"]["note"]="4111 1111 1111 1111"
        with self.assertRaisesRegex(ValueError,"Card"):
            parse_json(json.dumps(raw))

if __name__=="__main__": unittest.main()
