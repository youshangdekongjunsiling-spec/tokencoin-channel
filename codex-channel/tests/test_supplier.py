"""Supplier lifecycle/credential boundaries; synthetic data only."""
import hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import channel,setup_adapter as adapter,run_supplier as supplier

class SupplierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_specific_private_address_requires_local_consent(self):
        supplier.validate_listener("127.0.0.1",False)
        supplier.validate_listener("100.80.10.11",True)
        supplier.validate_listener("192.168.1.20",True)
        for address,flag in [("100.80.10.11",False),("8.8.8.8",True),("0.0.0.0",True),("localhost",True)]:
            with self.assertRaises(ValueError):supplier.validate_listener(address,flag)
    def test_occupied_port_receives_no_key_and_starts_no_process(self):
        (self.root/"auth").mkdir();(self.root/"auth"/"synthetic.json").write_text("{}")
        socket=MagicMock();socket.__enter__.return_value.connect_ex.return_value=0
        with patch.object(adapter,"verify",return_value=(Path("unused"),{})),patch.object(supplier.socket,"socket",return_value=socket),patch.object(supplier.subprocess,"Popen") as start,patch.object(adapter,"request") as send:
            with self.assertRaises(ValueError):supplier.AdapterProcess(self.root).__enter__()
            start.assert_not_called();send.assert_not_called()
    def test_waits_for_catalog_after_listener_is_ready(self):
        (self.root/"auth").mkdir();(self.root/"auth"/"synthetic.json").write_text("{}")
        socket=MagicMock();socket.__enter__.return_value.connect_ex.return_value=1
        process=MagicMock();process.poll.return_value=None
        replies=[(200,b'{"data":[]}'),(200,b'{"data":[{"id":"ready-model"}]}')]
        with patch.object(adapter,"verify",return_value=(Path("unused"),{})),patch.object(supplier.socket,"socket",return_value=socket),patch.object(supplier.subprocess,"Popen",return_value=process),patch.object(adapter,"request",side_effect=replies) as request,patch.object(supplier.time,"sleep"):
            with supplier.AdapterProcess(self.root) as upstream:self.assertEqual(upstream.models,{"ready-model"})
            self.assertEqual(request.call_count,2);process.terminate.assert_called_once()
    def test_private_config_change_fails_closed(self):
        name="cli-proxy-api.exe" if os.name=="nt" else "cli-proxy-api"
        (self.root/name).write_bytes(b"synthetic binary")
        (self.root/"installation.json").write_text(json.dumps({"version":adapter.VERSION,"executable":name,"binary_sha256":hashlib.sha256(b"synthetic binary").hexdigest()}))
        cfg=adapter.make_config(self.root);(self.root/"config.yaml").write_text(json.dumps(cfg))
        adapter.verify(self.root)
        cfg["observability"]["logs"]["request-log"]=True
        (self.root/"config.yaml").write_text(json.dumps(cfg))
        with self.assertRaises(ValueError):adapter.verify(self.root)
    def test_incomplete_state_is_preserved(self):
        home=self.root/"supplier";home.mkdir();(home/"state.sqlite3").write_bytes(b"partial")
        with self.assertRaises(ValueError):supplier.make_gateway(home,"Alice","model","127.0.0.1",0,{"access":{"api-keys":["synthetic"]}})
        self.assertEqual((home/"state.sqlite3").read_bytes(),b"partial")
        self.assertFalse((home/"node.json").exists())
    def test_key_is_in_process_not_environment_or_node_and_state_is_not_replaced(self):
        home=self.root/"supplier";env=dict(os.environ);key="synthetic-adapter-key-only-for-test"
        server=supplier.make_gateway(home,"Alice","model","127.0.0.1",0,{"access":{"api-keys":[key]}})
        try:
            self.assertEqual(server.upstream_key,key)
            self.assertEqual(dict(os.environ),env)
            before=(home/"node.json").read_bytes();self.assertNotIn(key.encode(),before)
            with self.assertRaises(ValueError):supplier.make_gateway(home,"Bob","other","127.0.0.1",0,{"access":{"api-keys":[key]}})
            self.assertEqual((home/"node.json").read_bytes(),before)
        finally:server.server_close()

if __name__=="__main__":unittest.main(verbosity=2)
