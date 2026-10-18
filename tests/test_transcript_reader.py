import hashlib
import io
import json
import random
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import transcript_reader as reader


class StreamingTranscriptTests(unittest.TestCase):
    def parse(self, raw, **kwargs):
        return list(reader.jsonl_records(io.BytesIO(raw), end=len(raw), **kwargs))

    def test_explicit_projection_discards_large_text_but_preserves_escaped_metadata(self):
        raw = b'{"text":"' + b'x' * 4096 + b'","t\\u0079pe":"emergency_alert_acknowledged","seq":4}\n'
        value = self.parse(raw, project=False, retained_fields=frozenset({"type", "seq"}), max_retained_bytes=512)[0][0]
        self.assertEqual(value, {"text": None, "type": "emergency_alert_acknowledged", "seq": 4})
        with self.assertRaises(reader.TranscriptBudgetExceeded):
            self.parse(raw, project=False, max_retained_bytes=512)

    def test_journal_projection_uses_separate_limits_without_relaxing_native_import(self):
        raw = b'{"seq":3,"output":[' + b'0,' * 200 + b'0],"type":"tool_finished"}\n'
        with self.assertRaisesRegex(reader.TranscriptBudgetExceeded, 'value-count'):
            self.parse(raw, max_nodes=100)
        value = self.parse(raw, project=False, retained_fields=frozenset({"seq", "type"}), max_nodes=None)[0][0]
        self.assertEqual(value, {"seq": 3, "output": None, "type": "tool_finished"})
        nested = b'{"seq":4,"output":' + b'[' * 100 + b'0' + b']' * 100 + b'}\n'
        with self.assertRaisesRegex(reader.TranscriptBudgetExceeded, 'nesting'):
            self.parse(nested)
        projected = self.parse(nested, project=False, retained_fields=frozenset({"seq"}), max_nodes=None, max_depth=512)[0][0]
        self.assertEqual(projected, {"seq":4, "output":None})

    def test_discarded_object_keys_do_not_exhaust_retained_metadata_budget(self):
        raw = json.dumps({"seq":1,"output":{str(i):"value" for i in range(2000)}}).encode()+b'\n'
        result = self.parse(raw, project=False, retained_fields=frozenset({"seq"}), max_retained_bytes=512, max_nodes=None)[0][0]
        self.assertEqual(result, {"seq":1,"output":None})

    def test_chunk_boundaries_preserve_json_strings_and_duplicate_keys(self):
        raw = b'{"text":"first","text":"Chinese \\u4e2d \\ud83d\\ude00 \\\" \\\\ /\\n","role":"user","x":[1,-2.5e+3,true,false,null,{"a":""}]}\n'
        for size in (1,2,3,7,64,65536):
            with self.subTest(chunk=size), patch.object(reader,'CHUNK_BYTES',size):
                self.assertEqual(self.parse(raw)[0][0],json.loads(raw))
                h=hashlib.sha256(); self.parse(raw)[0][2].update_hash(h)
                self.assertEqual(h.hexdigest(),hashlib.sha256(raw).hexdigest())

    def test_unicode_and_escaped_opaque_keys_validate_and_do_not_retain_data(self):
        raw='{"message":{"content":[{"type":"text","text":"中文🙂"},{"source":{"d\\u0061ta":"大🙂\\n\\u1234","type":"base64"},"type":"image"}]}}\n'.encode()
        for size in (1,2,5,65536):
            with patch.object(reader,'CHUNK_BYTES',size):
                result=self.parse(raw)[0][0]
                self.assertEqual(result['message']['content'][0]['text'],'中文🙂')
                self.assertIsNone(result['message']['content'][1]['source']['data'])

    def test_tool_output_projection_keeps_adjacent_text_and_unknown_fields(self):
        raw=json.dumps({'payload':{'type':'function_call_output','output':{'raw':['x'*2000]*4}},'unknown':{'text':'real'},'text':'kept'}).encode()+b'\n'
        value=self.parse(raw)[0][0]
        self.assertIsNone(value['payload']['output'])
        self.assertEqual(value['unknown'],{'text':'real'})
        self.assertEqual(value['text'],'kept')
        self.assertEqual(self.parse(raw,project=False)[0][0],json.loads(raw))

    def test_malformed_skipped_payload_never_becomes_valid_proof(self):
        for payload in (b'"bad\\x"',b'"\\u000z"',b'"\xff"',b'"line\t"',b'[1,]',b'{"a":1,}',b'"unterminated'):
            raw=b'{"payload":{"output":'+payload+b'}}\n'+b'{"text":"after"}\n'
            with self.subTest(payload=payload):
                self.assertIsNone(self.parse(raw)[0][0])
                self.assertEqual(self.parse(raw)[1][0],{'text':'after'})
                with self.assertRaises(ValueError): self.parse(raw,strict=True)

    def test_budget_exhaustion_and_cancellation_are_not_ignored_as_bad_json(self):
        raw=b'{"text":"'+b'x'*2048+b'"}\n'
        with self.assertRaises(reader.TranscriptBudgetExceeded):
            self.parse(raw,max_retained_bytes=1024)
        with self.assertRaises(reader.TranscriptBudgetExceeded):
            self.parse(raw,deadline=time.monotonic()-1)
        calls=0
        def cancelled():
            nonlocal calls
            calls+=1
            return calls>3
        raw=b'{"payload":{"output":"'+b'x'*4096+b'"}}\n'
        with patch.object(reader,'CHUNK_BYTES',128), self.assertRaises(reader.TranscriptBudgetExceeded):
            self.parse(raw,cancelled=cancelled)

    def test_partial_end_and_raw_offsets(self):
        raw=b'{"a":1}\n{broken}\n{"b":2}'
        with self.assertRaisesRegex(ValueError,'complete line'): self.parse(raw)
        records=self.parse(raw,require_newline=False)
        self.assertEqual([r[1] for r in records],[8,17,len(raw)])
        digest=hashlib.sha256()
        for _,_,receipt in records: receipt.update_hash(digest)
        self.assertEqual(digest.hexdigest(),hashlib.sha256(raw).hexdigest())

    def test_size_error_identifies_single_record_limit_without_exposing_text(self):
        prefix = b'{"text":"first"}\n'
        raw = prefix + b'{"text":"PRIVATE-CONTENT' + b'x' * 2048 + b'"}\n'
        for strict in (False, True):
            with self.subTest(strict=strict), self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
                self.parse(raw, max_retained_bytes=1024, strict=strict)
            error = str(raised.exception)
            for part in ('Single transcript record', 'retained text/metadata', 'observed at least',
                         'limit 1,024 bytes', 'accounting overhead',
                         f'Record 2 in this scan (source byte offset {len(prefix)})'):
                self.assertIn(part, error)
            self.assertNotIn('PRIVATE-CONTENT', error)
            self.assertLess(len(error), 1000)  # Survives the import API's error limit.
        stream = io.BytesIO(raw)
        stream.seek(len(prefix))
        with self.assertRaisesRegex(reader.TranscriptBudgetExceeded,
                                   rf'Record 1 in this scan \(source byte offset {len(prefix)}\)'):
            list(reader.jsonl_records(stream, end=len(raw), max_retained_bytes=1024))

    def test_structure_and_record_count_errors_name_the_specific_limit(self):
        cases = [
            (b'{"secret-key":0}\n', 'MAX_KEY_BYTES', 3, 'JSON field name', 'limit 3 bytes'),
            (b'{"\\u0073\\u0065":0}\n', 'MAX_KEY_BYTES', 6, 'JSON field name', 'limit 6 bytes'),
            (b'{"x":12345}\n', 'MAX_SCALAR_BYTES', 4, 'JSON scalar', 'limit 4 bytes'),
            (b'{"x":[[0]]}\n', 'MAX_DEPTH', 2, 'JSON nesting', 'observed depth 3; limit 2'),
            (b'{"x":[0,1]}\n', 'MAX_NODES', 3, 'JSON value-count', 'observed at least 4 values; limit 3'),
        ]
        for raw, constant, limit, scope, detail in cases:
            with self.subTest(scope=scope), patch.object(reader, constant, limit):
                with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
                    self.parse(raw)
                self.assertIn(scope, str(raised.exception))
                self.assertIn(detail, str(raised.exception))
                self.assertIn('Record 1 in this scan', str(raised.exception))
                self.assertNotIn('secret-key', str(raised.exception))
        with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
            self.parse(b'{}\n{}\n{}\n', max_records=2)
        self.assertIn('observed at least 3 records; limit 2', str(raised.exception))
        self.assertIn('JSONL records, not chat messages', str(raised.exception))
        self.assertIn('Record 3 in this scan (source byte offset 6)', str(raised.exception))

    def test_timeout_reports_configured_budget_and_preserves_record_location(self):
        with patch.object(reader.time, 'monotonic', side_effect=[100, 161]):
            with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
                self.parse(b'{}\n')
        self.assertIn('(60 seconds)', str(raised.exception))
        self.assertIn('processing timeout, not a file-size limit', str(raised.exception))
        self.assertIn('Record 1 in this scan (source byte offset 0)', str(raised.exception))
        # Caller deadlines may represent a different, larger operation.
        with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
            self.parse(b'{}\n', deadline=0)
        self.assertNotIn('60 seconds', str(raised.exception))
        receipt = self.parse(b'{}\n')[0][2]
        with patch.object(reader.time, 'monotonic', return_value=receipt.deadline + 1):
            with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
                receipt.update_hash(hashlib.sha256())
        self.assertIn('(60 seconds)', str(raised.exception))
        self.assertIn('Record 1 in this scan', str(raised.exception))

    def test_timeout_while_skipping_malformed_record_still_reports_location(self):
        raw = b'invalid-' + b'x' * 4096 + b'\n'
        with patch.object(reader, 'CHUNK_BYTES', 8), patch.object(reader.time, 'monotonic', side_effect=[100, 101, 161]):
            with self.assertRaises(reader.TranscriptBudgetExceeded) as raised:
                self.parse(raw)
        self.assertIn('Record 1 in this scan', str(raised.exception))

    def test_generated_nonopaque_json_matches_standard_decoder(self):
        rng=random.Random(71)
        def value(depth=0):
            simple=[None,True,False,rng.randrange(-10000,10000),rng.random(),'🙂中\\\n"'+str(rng.randrange(20))]
            if depth<4:
                simple += [[value(depth+1) for _ in range(3)],{str(i):value(depth+1) for i in range(2)}]
            return rng.choice(simple)
        for size in (3,31,65536):
            with patch.object(reader,'CHUNK_BYTES',size):
                for _ in range(30):
                    expected={'payload':value()};raw=(json.dumps(expected,ensure_ascii=rng.choice([True,False]))+'\n').encode()
                    self.assertEqual(self.parse(raw)[0][0],expected)

    def test_claude_tool_bodies_do_not_hide_neighboring_text_or_tool_type(self):
        raw=json.dumps({'message':{'content':[
            {'content':'x'*2048,'type':'tool_result','tool_use_id':'tool-1'},
            {'input':{'payload':'x'*2048},'type':'tool_use','id':'tool-1'},
            {'type':'text','text':'Real message'}]}}).encode()+b'\n'
        value=self.parse(raw,max_retained_bytes=2048)[0][0]
        self.assertEqual(value['message']['content'],[
            {'content':None,'type':'tool_result','tool_use_id':'tool-1'},
            {'input':None,'type':'tool_use','id':'tool-1'},
            {'type':'text','text':'Real message'}])

    @unittest.skipIf(sys.platform == "win32", "resource peak RSS is Unix-only")
    def test_opaque_payload_growth_does_not_grow_process_memory(self):
        probe = """
import json,resource,sys
from pathlib import Path
from transcript_reader import jsonl_records
p=Path(sys.argv[1])
with p.open('rb') as stream:
    for event, _offset, _raw in jsonl_records(stream,end=p.stat().st_size):
        assert event['payload']['output'] is None
scale=1 if sys.platform=='darwin' else 1024
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale)
"""
        peaks=[]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"media.jsonl"
            for size in (8,96):
                with path.open("wb") as stream:
                    stream.write(b'{"payload":{"output":"')
                    for _ in range(size): stream.write(b'x' * (1024*1024))
                    stream.write(b'"}}\n')
                result=subprocess.run([sys.executable,'-c',probe,str(path)],
                    cwd=Path(reader.__file__).parent,capture_output=True,text=True,check=True,timeout=30)
                peaks.append(int(result.stdout.strip()))
        self.assertLess(peaks[1]-peaks[0],32*1024*1024,peaks)
