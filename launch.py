from pathlib import Path
import urllib.request,webbrowser,sys
url='http://127.0.0.1:8765'
try:
    with urllib.request.urlopen(url+'/api/status',timeout=1) as r:
        if r.status==200:webbrowser.open(url);sys.exit(0)
except Exception:pass
import server
server.main()
