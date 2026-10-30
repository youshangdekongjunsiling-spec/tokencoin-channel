"""Pinned, private iroh runtime. Never installs into the global Python environment."""
from __future__ import annotations
import argparse, hashlib, importlib.util, io, os, platform, shutil, sys, threading, urllib.request, uuid, zipfile
from pathlib import Path, PurePosixPath
import setup_adapter

VERSION = "1.2.0"
ASSETS = {
 ("Windows", "amd64"): ("https://files.pythonhosted.org/packages/e2/c0/f0fe409eee4b0afaee56ae9a8cb6a15d2f4d443d1e12ef5529141a7b0a62/iroh-1.2.0-py3-none-win_amd64.whl", "1eedafeb906f57d4252b69379673b039894e28a35bbb9b54ddfdcae83411cfa5"),
 ("Darwin", "arm64"): ("https://files.pythonhosted.org/packages/eb/47/562411d054f7b15e8aaca8fd965c42e8d61acae0c58773c3a2d0266a03ef/iroh-1.2.0-py3-none-macosx_11_0_arm64.whl", "d7c407d9483c8e0c7680e569f906e892a19d4c13af537ba9127f7de130c0d399"),
}
_lock = threading.Lock()

def default_root():
 return setup_adapter.default_root().parent / "quick-connect" / ("iroh-" + VERSION)

def asset():
 machine = {"x86_64":"amd64", "aarch64":"arm64"}.get(platform.machine().lower(), platform.machine().lower())
 try: return ASSETS[(platform.system(), machine)]
 except KeyError: raise ValueError("Quick connection supports Windows x64 and Apple Silicon macOS with native ARM64 Python.") from None

def _archive(root):
 url, digest = asset()
 data = (root / "runtime.whl").read_bytes()
 if hashlib.sha256(data).hexdigest() != digest: raise ValueError("Iroh runtime checksum mismatch")
 return zipfile.ZipFile(io.BytesIO(data))

def verify(root=None):
 root = Path(root or default_root())
 if root.is_symlink(): raise ValueError("Runtime directory cannot be a symlink")
 with _archive(root) as z:
  for item in z.infolist():
   if item.is_dir(): continue
   parts=PurePosixPath(item.filename).parts
   if not parts or any(x in ("..", ".") for x in parts) or PurePosixPath(item.filename).is_absolute() or "\\" in item.filename:
    raise ValueError("Unsafe runtime archive")
   target=root.joinpath(*parts)
   if target.is_symlink() or not target.is_file() or target.read_bytes()!=z.read(item):
    raise ValueError("Installed iroh runtime changed")
 return root

def install(root=None, archive=None):
 root=Path(root or default_root())
 if root.exists():
  verify(root); print("Quick connection runtime already verified."); return root
 url,digest=asset()
 if archive: data=Path(archive).read_bytes()
 else:
  print("Installing verified iroh " + VERSION + " for quick connections...",flush=True)
  with urllib.request.urlopen(url, timeout=45) as response: data=response.read(12*1024*1024+1)
 if hashlib.sha256(data).hexdigest()!=digest: raise ValueError("Iroh download checksum mismatch")
 setup_adapter.private_directory(root.parent)
 temp=root.with_name(root.name+".install-"+uuid.uuid4().hex)
 setup_adapter.private_directory(temp)
 try:
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   if sum(i.file_size for i in z.infolist())>80*1024*1024: raise ValueError("Runtime archive too large")
   for item in z.infolist():
    parts=PurePosixPath(item.filename).parts
    if not parts or any(x in ("..", ".") for x in parts) or PurePosixPath(item.filename).is_absolute() or "\\" in item.filename or (item.external_attr>>16)&0o170000==0o120000:
     raise ValueError("Unsafe runtime archive")
    target=temp.joinpath(*parts)
    if item.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(z.read(item))
  (temp/"runtime.whl").write_bytes(data)
  verify(temp)
  os.replace(temp,root)
 finally:
  # Only remove the UUID staging directory created above, under this runtime parent.
  if temp.exists() and temp.resolve().parent==root.parent.resolve() and temp.name.startswith(root.name+".install-"):
   shutil.rmtree(temp)
 print("Quick connection runtime installed.")
 return root

def load():
 with _lock:
  expected=(default_root()/"iroh"/"__init__.py").resolve()
  existing=sys.modules.get("iroh")
  if existing is not None:
   if Path(existing.__file__).resolve()!=expected: raise ValueError("Unexpected iroh module already loaded")
   return existing
  root=verify()
  spec=importlib.util.spec_from_file_location("iroh",root/"iroh"/"__init__.py",submodule_search_locations=[str(root/"iroh")])
  module=importlib.util.module_from_spec(spec)
  sys.modules["iroh"]=module
  try: spec.loader.exec_module(module)
  except BaseException:
   sys.modules.pop("iroh",None); raise
  return module

if __name__=="__main__":
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("--archive",type=Path);args=p.parse_args()
 install(archive=args.archive)
