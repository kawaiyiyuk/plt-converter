"""Resource identity must not depend on retry time or output directory."""
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.services.pattern_layout_resources import write_gzip, write_resources
from app.services.pattern_layout_variants import recognize_with_variants


class DeterministicCompressionTests(unittest.TestCase):
    def test_gzip_same_content_has_fixed_timestamp_and_no_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            left=Path(folder)/'one'/'first.json.gz';left.parent.mkdir()
            right=Path(folder)/'another'/'different-name.json.gz';right.parent.mkdir()
            value={'文字':'原数据保留','numbers':[0,.25,1.]}
            with patch('gzip.time.time',return_value=1600000000):
                write_gzip(left,value,{'used':0,'maximum':100000})
            with patch('gzip.time.time',return_value=1900000000):
                write_gzip(right,value,{'used':0,'maximum':100000})
            self.assertEqual(left.read_bytes(),right.read_bytes())
            self.assertEqual(left.read_bytes()[4:8],b'\0'*4)
            self.assertFalse(left.read_bytes()[3]&8)
            self.assertEqual(gzip.decompress(left.read_bytes()),gzip.decompress(right.read_bytes()))

    def test_all_resource_and_archive_bytes_reproduce_across_time_and_directory(self):
        result=recognize_with_variants(b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;')
        with tempfile.TemporaryDirectory() as folder:
            outputs=[]
            for name,stamp in [('first',1600000000),('second',1900000000)]:
                target=Path(folder)/name;target.mkdir()
                with patch('gzip.time.time',return_value=stamp):write_resources(target,result,32*1024*1024)
                outputs.append({str(path.relative_to(target)):hashlib.sha256(path.read_bytes()).hexdigest() for path in target.rglob('*') if path.is_file()})
            self.assertEqual(outputs[0],outputs[1])

    def test_runtime_identity_includes_actual_compression_implementation(self):
        from app.services.pattern_layout_api import runtime_version
        before=runtime_version()
        with patch('zlib.ZLIB_RUNTIME_VERSION','different-zlib-runtime'):
            self.assertNotEqual(runtime_version(),before)
