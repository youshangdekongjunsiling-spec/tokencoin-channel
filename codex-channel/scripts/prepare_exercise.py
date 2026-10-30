"""Copy the synthetic bug exercise to a NEW directory; never overwrite existing work."""
import argparse, shutil
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument("--out",type=Path,required=True)
a=p.parse_args()
target=a.out.expanduser().resolve()
shutil.copytree(Path(__file__).resolve().parents[1]/"examples"/"bugfix",target)
print("Exercise ready: "+str(target))
