"""
conftest.py — pytest configuration for the tests/ directory.

Adds the project root to sys.path so that `import postprocess`,
`import aws_secrets_loader`, etc. resolve correctly regardless of
whether pytest is invoked from the project root or from tests/.
"""
import sys
import pathlib

# Project root = one directory above this file
ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
