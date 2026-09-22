import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__)))

# IMPORTANT: The 'cherrypy' module cannot be imported as a submodule from 'httpproxy.py'.
#   I.e, 'from deps import cherrypy' will not work. Not sure why. So we do the following
#   path hack to put 'cherrypy' on the module search path:
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "deps"))

