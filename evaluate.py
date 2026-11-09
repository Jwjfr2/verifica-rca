"""Accuracy and speed of the Vision solver over the hand-labelled subset."""
import json
import statistics
import time

import vision_ocr

labels = json.load(open("labels.json"))
ok = wrong = skipped = 0
times = []
for name, truth in sorted(labels.items()):
    path = f"dataset/{name}"
    t0 = time.time()
    word, conf = vision_ocr.read(path)
    times.append(time.time() - t0)
    if word is None:
        skipped += 1
        mark = "abstain"
    elif word == truth:
        ok += 1
        mark = "OK"
    else:
        wrong += 1
        mark = "WRONG"
    print(f"{name:11s} truth={truth}  got={str(word):5s} conf={conf:.2f}  {mark}")

n = len(labels)
print(f"\nexact {ok}/{n}  wrong {wrong}  abstain {skipped}  "
      f"median {statistics.median(times)*1000:.0f} ms/image")
