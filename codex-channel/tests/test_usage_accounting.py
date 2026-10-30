import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from usage_accounting import reported_usage,request_features

class UsageAccountingTests(unittest.TestCase):
    def test_cache_and_reasoning_are_subsets(self):
        result=reported_usage({"model":"resolved-model","usage":{"input_tokens":1000,"output_tokens":200,"total_tokens":1200,"input_tokens_details":{"cached_tokens":800},"output_tokens_details":{"reasoning_tokens":150}}},{"model":"alias","reasoning":{"effort":"low"}})
        self.assertEqual(result["uncached_input_tokens"],200)
        self.assertEqual(result["cached_input_tokens"],800)
        self.assertEqual(result["output_tokens"],200)
        self.assertEqual(result["reasoning_tokens"],150)
        self.assertEqual(result["total_tokens"],1200)
        self.assertEqual(result["requested_reasoning_effort"],"low")
        self.assertTrue(result["total_consistent"])
        self.assertNotIn("weekly_limit_percent",result)
    def test_missing_details_are_unknown(self):
        result=reported_usage({"usage":{"input_tokens":50,"output_tokens":2}}, {})
        self.assertNotIn("cached_input_tokens",result)
        self.assertNotIn("uncached_input_tokens",result)
        self.assertNotIn("reasoning_tokens",result)
    def test_invalid_details_not_used(self):
        result=reported_usage({"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":99,"input_tokens_details":{"cached_tokens":11},"output_tokens_details":{"reasoning_tokens":True}}}, {})
        self.assertFalse(result["total_consistent"])
        self.assertEqual(set(result["invalid_fields"]),{"cached_input_tokens","reasoning_tokens"})
        self.assertNotIn("uncached_input_tokens",result)
    def test_no_prompt_or_fake_credential_in_features(self):
        result=request_features({"model":"m","input":"PRIVATE_PROMPT","instructions":"SECRET","token":"fake-credential","reasoning":{"effort":"high"},"service_tier":"priority"})
        self.assertEqual(set(result),{"requested_model","requested_reasoning_effort","requested_service_tier"})
    def test_bad_or_missing_usage_does_not_create_zero(self):
        for response in (None,{}, {"usage":[]},{"usage":{"input_tokens":True,"output_tokens":-1}}):
            result=reported_usage(response,{"model":"m"})
            self.assertNotIn("input_tokens",result)
            self.assertNotIn("output_tokens",result)

if __name__=='__main__':unittest.main()
