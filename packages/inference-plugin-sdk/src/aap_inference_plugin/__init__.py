"""Framework-independent detection plugin authoring API."""
from .types import Detection, DetectionConfig, DetectionModel, DetectionProvider
from .manifest import load_manifest
from .app import create_detection_app

__all__ = ["Detection", "DetectionConfig", "DetectionModel", "DetectionProvider", "create_detection_app", "load_manifest"]
