import sys

try:
    from solution import outliers
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

csv1 = "name,value\na,10\nb,10\nc,10\nd,10\ne,100\n"
# with one extreme point, sd inflates so that |100-mean| == 2.0*sd exactly:
# strictly-greater semantics must EXCLUDE it at k=2.0 and include it below
assert outliers(csv1) == [], outliers(csv1)
assert outliers(csv1, k=1.9) == ["e"], outliers(csv1, k=1.9)

csv2 = "name,value\n a , 5 \n b , 5 \n\n c , 5 \n"
assert outliers(csv2) == []

assert outliers("name,value\nonly,1\n") == []

csv3 = "name,value\na,0\nb,0\nc,30\nd,0\n"
assert outliers(csv3, k=1.5) == ["c"]

try:
    outliers("name,value\na,1\nb,notanumber\n")
    raise SystemExit("expected ValueError")
except ValueError as e:
    assert "2" in str(e), f"line number missing from: {e}"

print("ok")
sys.exit(0)
