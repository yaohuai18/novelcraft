import traceback, io, sys
buf = io.StringIO()
try:
    import app.main
    print('app import OK', file=sys.stderr)
except Exception:
    traceback.print_exc(file=buf)
open('err3.txt','w',encoding='utf-8').write(buf.getvalue())
