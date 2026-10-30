import json,tempfile,unittest
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import run_supplier

class AdapterCleanupTests(unittest.TestCase):
    def test_spawn_failure_removes_temporary_management_config(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'auth').mkdir();(root/'auth'/'one.json').write_text('{}')
            cfg={'management':{'secret-key':''},'access':{'api-keys':['x'*32]}}
            def write(path,value):Path(path).write_text(json.dumps(value))
            fake_socket=mock.MagicMock();fake_socket.__enter__.return_value.connect_ex.return_value=1
            with mock.patch.object(run_supplier.adapter,'verify',return_value=(root/'adapter.exe',cfg)),\
                 mock.patch.object(run_supplier.adapter,'private_write',side_effect=write),\
                 mock.patch.object(run_supplier.socket,'socket',return_value=fake_socket),\
                 mock.patch.object(run_supplier.subprocess,'Popen',side_effect=OSError('spawn failed')):
                with self.assertRaises(OSError):run_supplier.AdapterProcess(root).__enter__()
            self.assertEqual(list(root.glob('supplier-*.json')),[])

if __name__=='__main__':unittest.main()
