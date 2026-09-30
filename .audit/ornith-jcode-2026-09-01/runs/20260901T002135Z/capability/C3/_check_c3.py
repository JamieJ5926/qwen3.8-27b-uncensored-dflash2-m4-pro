import copy
ns = {}
exec(open('merge.py', encoding='utf-8').read(), ns)
merge = ns['merge']
d = {'a': 1, 'b': 2, 'c': 3}
f = {'b': 20, 'd': 4}
e = {'c': 30}
d0, f0, e0 = copy.deepcopy(d), copy.deepcopy(f), copy.deepcopy(e)
got = merge(d, f, e)
assert got == {'a': 1, 'b': 20, 'c': 30, 'd': 4}
assert merge({'x': 1}, {}, {}) == {'x': 1}
assert merge({'x': 1}, {'y': 2}, {}) == {'x': 1, 'y': 2}
assert d == d0 and f == f0 and e == e0
assert got is not d and got is not f and got is not e
print('C3_OK')
