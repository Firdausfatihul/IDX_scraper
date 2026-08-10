from idx_digest.summarizer import OpenRouterSummarizer


def test_invalid_cached_document_summary_is_rejected() -> None:
    assert not OpenRouterSummarizer.is_valid_document_summary({})
    assert not OpenRouterSummarizer.is_valid_document_summary({"": ""})


def test_invalid_cached_announcement_summary_is_rejected() -> None:
    assert not OpenRouterSummarizer.is_valid_announcement_summary({})
    assert not OpenRouterSummarizer.is_valid_announcement_summary({"": ""})
