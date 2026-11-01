import hashlib, json, os, random, shutil, sys
root = sys.argv[1]
shutil.rmtree(root, ignore_errors=True)
r = random.Random(7)
def w(rel, n, seed=None):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(random.Random(seed if seed is not None else rel).randbytes(n))
w("p91/boot.bin", 6000); w("p91/app.bin", 40000); w("p91/proof.bin", 4096); w("p91/big.bin", 600000)   # big: ~37 blocks, a long operation
w("p91/backup-m3/boot-m3.bin", 5000); w("p91/archive/boot-old.bin", 5000); w("c33/legacy.bin", 3000)
json.dump({"name": "test build", "new_install_prompt_erase": False, "builds": [{"chipFamily": "ESP32-S3", "parts": [
    {"path": "p91/boot.bin", "offset": 0}, {"path": "p91/app.bin", "offset": 0x10000}]}]}, open(os.path.join(root, "p91/manifest.json"), "w"))
json.dump({"hello": "not a manifest"}, open(os.path.join(root, "p91/notes.json"), "w"))
for i in range(600):                      # a few hundred files; 25 share the same content
    w("p91/bulk/f%03d.bin" % i, 2048, seed="dup" if i % 24 == 0 else i)
print("fixture ok")
