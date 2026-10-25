"""
Pytest configuration file.

This file ensures that the project root is in the Python path,
allowing tests to import from the api and open_notebook modules.
"""

import os
import sys
from pathlib import Path

# Unit tests must never import a user's live account/database configuration.
# Integration checks opt in explicitly and use separate disposable data.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPEN_NOTEBOOK_PASSWORD"] = ""
os.environ["RESEARCH_LOCAL_ONLY"] = "0"
os.environ["SURREAL_NAMESPACE"] = "research_unit_tests"
os.environ["SURREAL_DATABASE"] = "research_unit_tests"
os.environ.pop("CORS_ORIGINS", None)

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
