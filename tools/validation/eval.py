"""Offline validation only: per-commit breakdown of a selector output.

Usage: python3 eval.py <output.jsonl> sample/answers.json sample/tests.json
Prints tests selected, workers, cost and any missed failing tests per commit.
"""
import json,sys
out,ans,tests=sys.argv[1],sys.argv[2],sys.argv[3]
a=json.load(open(ans))['commits']; t={x['number']:x for x in json.load(open(tests))['tests']}
for line in open(out):
    r=json.loads(line); sel={n for w in r['workers'] for n in w}
    f=a[r['commit']]; miss=[n for n in f['failing'] if n not in sel]
    ms=lambda w: sum(int(t[n]['seconds']*1000+.5) for n in w)
    cost=(max(map(ms,r['workers']))+2000*len(r['workers'])) if r['workers'] else 0
    print(f"{r['commit']} {f['kind']:8s} fail={len(f['failing']):3d} sel={len(sel):5d} w={len(r['workers'])} cost={cost/1000:7.2f} {'MISS '+str([t[n]['id'] for n in miss]) if miss else ''}")
