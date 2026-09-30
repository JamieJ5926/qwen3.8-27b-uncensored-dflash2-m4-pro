ns = {}
exec(open('slug.py', encoding='utf-8').read(), ns)
slug = ns['slug']
assert slug('Hello World') == 'hello-world'
assert slug('foo---bar   baz') == 'foo-bar-baz'
assert slug('') == ''
assert slug('  Hello, World!!  ') == 'hello,-world!!'
print('C1_OK')
