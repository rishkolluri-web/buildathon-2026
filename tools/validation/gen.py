"""Offline validation only: random single-line mutants of the pinned sqlglot.

Usage: python gen.py <sqlglot-checkout> <out-dir> <count> <seed>
Writes <name>.patch (unified diff) and <name>.kind per mutant, for run.sh to
label by running the real test suite. Nothing here is shipped in the image.
"""
import ast, difflib, os, random, re, sys
root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
files = [os.path.join(d, f)[len(root)+1:] for d, _, fs in os.walk(os.path.join(root, "sqlglot")) for f in fs if f.endswith(".py")]
fixtures = [os.path.join(d, f)[len(root)+1:] for d, _, fs in os.walk(os.path.join(root, "tests/fixtures")) for f in fs if f.endswith(".sql")]
OPS = [
    (r" is not ", " is "), (r" is ", " == "), (r" and ", " or "), (r" or ", " and "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" > ", " >= "), (r"\bnot ", ""), (r"(?<![\w.])(\d+)(?![\w.])", None),
    (r'"([A-Z_]{2,})"', None),
]
os.makedirs(out, exist_ok=True)
made = 0
while made < count:
    kind = rng.random()
    if kind < 0.15:
        rel = rng.choice(fixtures); lines = open(os.path.join(root, rel)).read().split("\n")
        idx = rng.randrange(len(lines))
        if not lines[idx].strip() or lines[idx].startswith(("#", "--")): continue
        words = lines[idx].split(" ")
        j = rng.randrange(len(words)); words[j] = words[j] + "x"
        new = lines[:idx] + [" ".join(words)] + lines[idx+1:]
        label = "fixture"
    else:
        rel = rng.choice(files); text = open(os.path.join(root, rel)).read(); lines = text.split("\n")
        idx = rng.randrange(len(lines)); line = lines[idx]; s = line.strip()
        if not s or s.startswith(("#", '"', "'")): continue
        if kind < 0.25:
            indent = line[:len(line) - len(line.lstrip())]
            new = lines[:idx] + [indent + "# Keep in sync"] + lines[idx:]
            label = "comment"
        else:
            pat, rep = rng.choice(OPS)
            m = list(re.finditer(pat, line))
            if not m: continue
            mm = rng.choice(m)
            if rep is None and mm.group(0)[0].isdigit(): r = str(int(mm.group(1)) + 1)
            elif rep is None: r = '"' + mm.group(1) + '_X"'
            else: r = rep
            newline = line[:mm.start()] + r + line[mm.end():]
            new = lines[:idx] + [newline] + lines[idx+1:]
            label = "code"
            try:
                if ast.dump(ast.parse("\n".join(new))) == ast.dump(ast.parse(text)): continue
            except SyntaxError: continue
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{rel}", f"b/{rel}")
    body = "".join(diff)
    name = f"m{seed}_{made:03d}"
    with open(os.path.join(out, name + ".patch"), "w") as h:
        h.write(f"diff --git a/{rel} b/{rel}\n" + body)
    with open(os.path.join(out, name + ".kind"), "w") as h: h.write(label)
    made += 1
print(made)
