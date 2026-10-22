import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__)))

# Make bundled dependencies available to the add-on.
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "deps"))
