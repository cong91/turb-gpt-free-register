"""pytest conftest — add project root to sys.path so `import core` resolves."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
