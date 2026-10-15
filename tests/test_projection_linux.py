"""Real Linux bootstrap limits and parent-death tests (no service/network)."""
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from app import projection_runtime
from app.projection_store import process_identity
from test_projection import pdf_bytes

@unittest.skipUnless(sys.platform.startswith('linux'),'Linux kernel AS/PDEATH acceptance')
class LinuxBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bootstrap=Path(projection_runtime.__file__).absolute().parent.parent/'projection_bootstrap.py'
        self.source=self.root/'source.pdf';self.source.write_bytes(pdf_bytes())
    def spec(self,parent):
        return {'operation':'parse','source':str(self.source),'output':str(self.root/'result.json'),
                'parameters':{},'parent':parent,'module_root':str(self.bootstrap.parent),'limits':{'memory':512*1024**2,'cpu':2,'file':512*1024}}
    def test_limits_precede_native_import_and_valid_pdf_parse_succeeds(self):
        path=self.root/'request.json';path.write_text(json.dumps(self.spec(os.getpid())))
        child=subprocess.Popen([sys.executable,str(self.bootstrap),str(path)],start_new_session=True,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        output,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,0,error.decode())
        self.assertEqual(json.loads((self.root/'result.json').read_text())['page_count'],1)
        # The native parse process writes its marker only after all OS limits.
        self.assertTrue((self.root/'child.json').exists())
        self.assertIsNotNone(json.loads((self.root/'child.json').read_text())['identity'])
        # Reaped PID cannot still hold an open source FD.
        with self.assertRaises(ProcessLookupError):os.kill(child.pid,0)
    def test_real_512m_address_space_refuses_larger_allocation(self):
        code='import resource\nresource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))\nbytearray(700*1024**2)\n'
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        out,err=child.communicate(timeout=5)
        self.assertNotEqual(child.returncode,0);self.assertIn(b'MemoryError',err)
        with self.assertRaises(ProcessLookupError):os.kill(child.pid,0)
    def test_child_parent_death_terminates_and_init_reaps(self):
        # A normal max-budget 4M tile leaves enough startup time to kill its
        # supervising process after the bootstrap marker, without heavy files.
        spec=self.spec(1)
        spec.update(operation='tile',parameters={'page_index':0,'layout':None,
            'values':{'x_mm':0,'y_mm':0,'width_mm':200*25.4/72,'height_mm':300*25.4/72,
                      'width_px':1600,'height_px':2400,'rotation_deg':37}})
        spec['limits']['file']=13*1024**2
        request=self.root/'request.json';request.write_text(json.dumps(spec))
        code='''import json,os,subprocess,sys,time
from pathlib import Path
path=Path(sys.argv[2]);spec=json.loads(path.read_text());spec['parent']=os.getpid();path.write_text(json.dumps(spec))
child=subprocess.Popen([sys.executable,sys.argv[1],str(path)],start_new_session=True)
Path(str(path)+'.pid').write_text(str(child.pid))
time.sleep(30)
'''
        parent=subprocess.Popen([sys.executable,'-c',code,str(self.bootstrap),str(request)],start_new_session=True)
        try:
            deadline=time.monotonic()+5
            marker=self.root/'child.json'
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(marker.exists())
            pid=json.loads(marker.read_text())['pid']
            os.kill(parent.pid,signal.SIGTERM);parent.wait(timeout=2)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:os.kill(pid,0)
                except ProcessLookupError:break
                time.sleep(.02)
            else:self.fail('orphan projection child was not killed/reaped')
        finally:
            if parent.poll() is None:parent.kill()
            parent.wait()
