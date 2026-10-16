"""Temporary synthetic records only; never read real sessions or install schedules."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
ROOT = next(p for p in [Path(__file__).resolve().parent,*Path(__file__).resolve().parents] if (p/'app.py').is_file())
spec=importlib.util.spec_from_file_location('ledger',ROOT/'app.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
class PublicLedgerTests(unittest.TestCase):
 def test_status_does_not_create_storage(self):
  with tempfile.TemporaryDirectory() as tmp:
   data=Path(tmp)/'not-created';app.status(ROOT,data);self.assertFalse(data.exists())
 def test_synthetic_incremental_data_and_private_text_boundary(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp)/'source';folder=home/'sessions';folder.mkdir(parents=True);data=Path(tmp)/'result'
   usage={'input_tokens':100,'cached_input_tokens':0,'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':110}
   records=[{'type':'session_meta','payload':{'cwd':str(Path(tmp)/'synthetic-project'),'base_instructions':'SYNTHETIC_PRIVATE_MARKER'}},{'type':'turn_context','payload':{'model':'synthetic-model'}},{'type':'event_msg','timestamp':'2000-01-01T00:00:00Z','payload':{'type':'token_count','info':{'total_token_usage':usage,'last_token_usage':usage}}}]
   (folder/('rollout-'+('a'*36)+'.jsonl')).write_text(''.join(json.dumps(x)+'\n' for x in records))
   self.assertEqual(app.collect(home,data)['new_events'],1)
   self.assertEqual(app.collect(home,data)['new_events'],0)
   self.assertNotIn(b'SYNTHETIC_PRIVATE_MARKER',(data/'usage.sqlite').read_bytes())
   self.assertEqual(sum(x['total_tokens'] for x in app.report(data,{'timezone':'Asia/Shanghai'})['rows']),110)
if __name__=='__main__':unittest.main()
