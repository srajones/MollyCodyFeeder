"""BowlAnalyzer facade: audio + vision + optional LLM + stream capture."""

from watchdog.config import WatchdogConfig
from watchdog.analysis.audio import AudioAnalysisMixin
from watchdog.analysis.llm import LlmVisionMixin
from watchdog.analysis.stream_capture import StreamCaptureMixin
from watchdog.analysis.vision import VisionMixin


class BowlAnalyzer(AudioAnalysisMixin, VisionMixin, LlmVisionMixin, StreamCaptureMixin):
    """Fuses visual bowl analysis with acoustic analysis (motor hum vs. kibble impacts) so household
    noise cannot mask an empty bowl or a clogged auger. Composed of:
    AudioAnalysisMixin (audio.py), VisionMixin (vision.py), LlmVisionMixin (llm.py),
    StreamCaptureMixin (stream_capture.py).
    """

    def __init__(self, config: WatchdogConfig):
        self.cfg = config
