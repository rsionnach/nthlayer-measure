"""Degradation detection — compares arithmetic against human-declared thresholds (ZFC)."""

from nthlayer_measure.detection.detector import ThresholdDetector
from nthlayer_measure.detection.protocol import Alert, DegradationDetector

__all__ = ["Alert", "DegradationDetector", "ThresholdDetector"]
