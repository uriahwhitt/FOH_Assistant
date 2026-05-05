import sys
from pathlib import Path

# Make foh-assistant/ the root so imports like "from models.channel import ..." work
sys.path.insert(0, str(Path(__file__).parent.parent))
