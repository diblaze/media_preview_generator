"""Scratch: the base and work rows (full paths) of the files whose season verdict or answer changed."""

import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
a = pickle.load(open(os.path.join(HERE, "season_base.pkl"), "rb"))
b = pickle.load(open(os.path.join(HERE, sys.argv[1]), "rb"))
for key in sorted(set(a) & set(b)):
    ra, rb = a[key], b[key]
    if ra["verdict"] != rb["verdict"] or ra["answer"] != rb["answer"]:
        print(key[0], key[1])
        print("   truth", ra["truth"], "| base", ra["answer"], ra["verdict"], "| work", rb["answer"], rb["verdict"])
