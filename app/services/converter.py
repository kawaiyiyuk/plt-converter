class ConversionEngineNotReady(RuntimeError):
    """Raised until the production vector conversion engine is connected."""

    def __init__(self):
        super().__init__('PDF 转换引擎尚未接入')
