# SPDX-License-Identifier: AGPL-3.0-only
"""Run offline QR runtime with real device payloads."""
import json
import os
import subprocess
import unittest

from support import web


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = next((name for name in ("node", "nodejs")
             if subprocess.run(["sh", "-c", "command -v %s" % name],
                               capture_output=True).returncode == 0), None)


@unittest.skipIf(NODE is None, "node is unavailable")
class TestOfflineQr(unittest.TestCase):
    def test_both_payloads_generate_a_qr_matrix(self):
        identity = {"ap_ssid": "RTK-D4E5F6-SETUP",
                    "hostname": "rtk-d4e5f6",
                    "device_code": "gnss-0123456789ab"}
        payloads = [web.wifi_qr_payload(identity),
                    web.portal_qr_payload(identity)]
        script = r"""
const fs=require('fs'),vm=require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const out=JSON.parse(process.argv[2]).map(text=>{
  const qr=new QRCode(0,QRErrorCorrectLevel.M);qr.addData(text);qr.make();
  return {size:qr.getModuleCount(),dark:qr.modules.flat().filter(Boolean).length};
});
console.log(JSON.stringify(out));
"""
        run = subprocess.run(
            [NODE, "-e", script, os.path.join(REPO, "qr.js"),
             json.dumps(payloads)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        matrices = json.loads(run.stdout)
        self.assertEqual(len(matrices), 2)
        for matrix in matrices:
            self.assertGreaterEqual(matrix["size"], 21)
            self.assertGreater(matrix["dark"], 100)


if __name__ == "__main__":
    unittest.main()
