"""dvhectare — поиск свободной земли под «Дальневосточный гектар» по данным НСПД."""
from .analysis import Scanner, ScanResult
from .config import Settings, load_settings
from .nspd.client import NspdClient

__version__ = "0.1.0"
__all__ = ["NspdClient", "Scanner", "ScanResult", "Settings", "load_settings"]
